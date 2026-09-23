"""The dashboard's JSON API over the slot store. Deployed as a Vercel function.

Two routes. ``/api/overview`` answers a whole view -- one sport, one window,
optionally one venue. The daily pass stores every view ready-made (see
tracker.views), so a request is one row read; a view that was never built is
computed on the spot instead. ``/api/health`` says when the daily pass last ran.

The data changes once a day, so responses are cached at Vercel's edge for an
hour and served stale while a fresh copy is fetched behind the reader.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response

from tracker.store import Store
from tracker.types import Sport, local_wall_clock
from tracker.views import UnknownVenueError, overview, rows_for, view_key

TZ = "Asia/Kolkata"
#: The daily pass runs once a day; older than this and the dashboard says so.
STALE_AFTER = dt.timedelta(hours=36)
#: Edge caching: fresh for an hour, then served stale for up to a day while
#: Vercel refetches in the background, so no reader waits on a rebuild.
CACHE = "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400"
HEALTH_CACHE = "public, max-age=0, s-maxage=300, stale-while-revalidate=3600"

app = FastAPI(title="Courts tracker", docs_url="/api/docs", openapi_url="/api/openapi.json")


@contextmanager
def _store() -> Iterator[Store]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(503, "DATABASE_URL is not configured")
    store = Store(url)
    try:
        yield store
    finally:
        store.close()


def _today() -> dt.date:
    return local_wall_clock(dt.datetime.now(dt.UTC), TZ).date()


@app.get("/api/health")
def health(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = HEALTH_CACHE
    with _store() as store:
        runs = store.latest_runs(1)
    if not runs:
        return {"status": "no_data", "last_run": None}
    run = runs[0]
    finished = run["finished_at"]
    age = dt.datetime.now(dt.UTC) - finished if finished else None
    status = "running" if finished is None else ("stale" if age and age > STALE_AFTER else "ok")
    return {
        "status": status,
        "last_run": {
            "started_at": run["started_at"].isoformat(),
            "finished_at": finished.isoformat() if finished else None,
            "courts_ok": run["courts_ok"],
            "courts_failed": run["courts_failed"],
            "slots_written": run["slots_written"],
        },
    }


@app.get("/api/overview")
def overview_view(
    sport: Sport = Sport.PADEL,
    window: str = Query("7", pattern="^(7|30|all)$"),
    venue: str | None = Query(None, description="Narrow every chart to one venue_uuid"),
) -> Response:
    with _store() as store:
        stored = store.view(view_key(sport, window, venue))
        if stored is None:
            today = _today()
            courts = {c.facility_uuid: c for c in store.tracked_courts(sport)}
            try:
                payload = overview(
                    rows_for(store, sport, today),
                    courts,
                    store.venues(),
                    sport=sport,
                    window=window,
                    today=today,
                    venue=venue,
                )
            except UnknownVenueError:
                raise HTTPException(404, f"no {sport.value} venue {venue} in this window") from None
            stored = json.dumps(payload, default=str)
    return Response(stored, media_type="application/json", headers={"Cache-Control": CACHE})
