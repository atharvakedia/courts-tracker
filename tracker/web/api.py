"""The dashboard's JSON API over the slot store. Deployed as a Vercel function.

Two routes. ``/api/overview`` answers a whole view -- one sport, one window --
from a single query, so a page costs one database read whether it shows two
venues or sixty. ``/api/health`` says when the daily pass last ran.

Windows are settled business dates only (yesterday and back), in Jaipur time.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tracker.insights import (
    Verdict,
    by,
    daily_series,
    heatmap,
    lead_summary,
    lead_times,
    occupancy,
    price_per_hour,
    reliability,
    settled,
)
from tracker.store import Store
from tracker.types import Sport, local_wall_clock

TZ = "Asia/Kolkata"
WINDOWS = {"7": 7, "30": 30, "all": 90}
#: A venue first seen this recently is flagged as new on the dashboard.
NEW_VENUE_DAYS = 14
#: The daily pass runs once a day; older than this and the dashboard says so.
STALE_AFTER = dt.timedelta(hours=36)

app = FastAPI(title="Hudle tracker", docs_url="/api/docs", openapi_url="/api/openapi.json")


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
def health() -> dict[str, Any]:
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
def overview(
    sport: Sport = Sport.PADEL,
    window: str = Query("7", pattern="^(7|30|all)$"),
) -> dict[str, Any]:
    today = _today()
    days = WINDOWS[window]
    start, end = today - dt.timedelta(days=days), today - dt.timedelta(days=1)
    with _store() as store:
        # Read two weeks ahead too: reliability looks for permanent holds there.
        rows = store.slots_between(
            sport=sport, date_from=start, date_to=today + dt.timedelta(days=14)
        )
        courts = {c.facility_uuid: c for c in store.tracked_courts(sport)}
        venues = store.venues()

    per_court = by(rows, "facility_uuid")
    verdicts = {fid: reliability(court_rows, today) for fid, court_rows in per_court.items()}
    past = settled(rows, today)
    past = [r for r in past if r["business_date"] >= start]
    counted = [r for r in past if verdicts[r["facility_uuid"]]["verdict"] == Verdict.RELIABLE.value]

    venue_rows = []
    for venue_uuid, vrows in by(past, "venue_uuid").items():
        court_ids = sorted({r["facility_uuid"] for r in vrows})
        court_verdicts = [verdicts[f]["verdict"] for f in court_ids]
        v = venues.get(venue_uuid, {})
        venue_rows.append(
            {
                "venue_uuid": venue_uuid,
                "name": v.get("name", venue_uuid),
                "new": bool(
                    v.get("first_seen_at")
                    and v["first_seen_at"].date() >= today - dt.timedelta(days=NEW_VENUE_DAYS)
                ),
                "courts": [
                    {
                        "facility_uuid": f,
                        "name": courts[f].name if f in courts else f,
                        **verdicts[f],
                    }
                    for f in court_ids
                ],
                "verdict": _venue_verdict(court_verdicts),
                "price_per_hour": price_per_hour(vrows),
                **occupancy(vrows).as_dict(),
            }
        )
    venue_rows.sort(key=lambda v: (-(v["occupancy"] or 0), v["name"]))

    hours = lead_times(counted)
    return {
        "sport": sport.value,
        "window": {"key": window, "start": start.isoformat(), "end": end.isoformat()},
        "rule": "A slot is booked or vacant; slots a venue blocks count as booked.",
        "totals": {
            **occupancy(counted).as_dict(),
            "courts_counted": len({r["facility_uuid"] for r in counted}),
            "courts_tracked": len(per_court),
        },
        "venues": venue_rows,
        "daily": daily_series(counted),
        "heatmap": heatmap(counted),
        "lead_time": lead_summary(hours),
        "lead_time_hours": [round(h, 1) for h in hours],
    }


def _venue_verdict(court_verdicts: list[str]) -> str:
    """A venue is as trustworthy as its best court: courts share a calendar."""
    for v in (Verdict.RELIABLE, Verdict.PARTIAL, Verdict.UNRELIABLE):
        if v.value in court_verdicts:
            return v.value
    return Verdict.NO_DATA.value
