"""The dashboard's JSON API over the slot store. Deployed as a Vercel function.

``/api/overview`` answers a whole view -- one sport, one window, optionally
one venue. The daily pass stores every view ready-made (see tracker.views), so
a request is one row read. The API never computes a view itself, since that
reads three months of slots from the database: a version whose views are not
built yet answers 503 until the views workflow runs for it, and a venue with no
view in a built version is a 404. ``/api/health`` says when the daily pass last
ran and when the views were built. ``/api/blocks`` lists, adds and removes the
hours people marked as blocked at a venue (see tracker.blocks); a change
rebuilds every view.

The data changes once a day, so responses are cached at Vercel's edge for an
hour and served stale while a fresh copy is fetched behind the reader. The
page asks for each view with the build time from ``/api/health``, so a rebuild
(the daily pass, or a block saved) is a new URL, not an hour of stale answers.

Changing blocks takes the shared password in ``ADMIN_PASSWORD``; with it
unset, blocks can be read but not changed. With ``GITHUB_DISPATCH_TOKEN`` set
(production), the rebuild after a change runs as the views workflow on GitHub,
which reads the slots from its mirror; without it (local), it runs here.
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator

from tracker.store import Store
from tracker.types import Sport
from tracker.views import VIEWS_VERSION, publish_views, view_key

logger = logging.getLogger("tracker.web.api")

TZ = "Asia/Kolkata"
#: The daily pass runs once a day; older than this and the dashboard says so.
STALE_AFTER = dt.timedelta(hours=36)
#: Edge caching: fresh for an hour, then served stale for up to a day while
#: Vercel refetches in the background, so no reader waits on a rebuild.
CACHE = "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400"
HEALTH_CACHE = "public, max-age=0, s-maxage=300, stale-while-revalidate=3600"
NO_STORE = "no-store"
#: GitHub's endpoint that starts the views workflow on a branch.
DISPATCH_URL = "https://api.github.com/repos/{repo}/actions/workflows/views.yml/dispatches"

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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@app.get("/api/health")
def health(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = HEALTH_CACHE
    with _store() as store:
        runs = store.latest_runs(1)
        built = store.views_built_at(VIEWS_VERSION)
    views_built_at = built.isoformat() if built else None
    if not runs:
        return {"status": "no_data", "last_run": None, "views_built_at": views_built_at}
    run = runs[0]
    finished = run["finished_at"]
    age = dt.datetime.now(dt.UTC) - finished if finished else None
    status = "running" if finished is None else ("stale" if age and age > STALE_AFTER else "ok")
    return {
        "status": status,
        "views_built_at": views_built_at,
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
            if venue is not None and store.views_built_at(VIEWS_VERSION):
                raise HTTPException(404, f"no {sport.value} venue {venue} in this window")
            raise HTTPException(
                503, "These views are not built yet: run the views workflow on this branch"
            )
    return Response(stored, media_type="application/json", headers={"Cache-Control": CACHE})


Weekday = Annotated[int, Field(ge=0, le=6)]
Hour = Annotated[int, Field(ge=0, le=23)]


class BlockIn(BaseModel):
    """Hours to mark as blocked at one venue. A missing date is open-ended."""

    venue_uuid: str = Field(min_length=1)
    cells: list[tuple[Weekday, Hour]] = Field(min_length=1, max_length=7 * 24)
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    note: str = Field("", max_length=200)

    @model_validator(mode="after")
    def _dates_in_order(self) -> BlockIn:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


def _authorize(password: str | None) -> None:
    expected = os.environ.get("ADMIN_PASSWORD")
    if not expected:
        raise HTTPException(503, "Editing blocks is off: ADMIN_PASSWORD is not set")
    if not password or not hmac.compare_digest(password.encode(), expected.encode()):
        raise HTTPException(401, "Wrong password")


def _rebuild(store: Store) -> dict[str, str | None]:
    """Rebuild every view so a change shows, and say how.

    ``done``: rebuilt here; ``views_built_at`` is the new revision.
    ``queued``: the views workflow on GitHub rebuilds within minutes; the new
    revision will be newer than ``views_built_at``, the current one.
    ``failed``: neither happened; the next daily pass rebuilds.

    The workflow always runs main's code: blocks live in the one database
    production reads, so production's views are the ones to rebuild.
    """
    built = store.views_built_at(VIEWS_VERSION)
    current = built.isoformat() if built else None
    token = os.environ.get("GITHUB_DISPATCH_TOKEN")
    if not token:
        now = _now()
        try:
            publish_views(store, now=now, tz=TZ)
        except Exception:
            # The change is saved already; saying so stops a second save.
            logger.exception("views_rebuild_failed")
            return {"rebuild": "failed", "views_built_at": current}
        return {"rebuild": "done", "views_built_at": now.isoformat()}
    # Vercel's own variables name the repository.
    repo = f"{os.environ.get('VERCEL_GIT_REPO_OWNER')}/{os.environ.get('VERCEL_GIT_REPO_SLUG')}"
    try:
        httpx.post(
            DISPATCH_URL.format(repo=repo),
            json={"ref": "main"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("views_dispatch_failed", extra={"repo": repo, "error": str(exc)})
        return {"rebuild": "failed", "views_built_at": current}
    return {"rebuild": "queued", "views_built_at": current}


@app.get("/api/blocks")
def list_blocks(response: Response, venue: str | None = None) -> dict[str, Any]:
    response.headers["Cache-Control"] = NO_STORE
    with _store() as store:
        return {"blocks": [b.as_dict() for b in store.blocks(venue)]}


@app.post("/api/blocks", status_code=201)
def add_block(
    block: BlockIn,
    response: Response,
    x_admin_password: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _authorize(x_admin_password)
    response.headers["Cache-Control"] = NO_STORE
    with _store() as store:
        if block.venue_uuid not in store.venues():
            raise HTTPException(404, f"no venue {block.venue_uuid}")
        block_id = store.add_block(
            venue_uuid=block.venue_uuid,
            cells=block.cells,
            date_from=block.date_from,
            date_to=block.date_to,
            note=block.note.strip(),
            created_at=_now(),
        )
        saved = next(b for b in store.blocks(block.venue_uuid) if b.block_id == block_id)
        return {"block": saved.as_dict(), **_rebuild(store)}


@app.delete("/api/blocks/{block_id}")
def delete_block(
    block_id: int,
    response: Response,
    x_admin_password: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _authorize(x_admin_password)
    response.headers["Cache-Control"] = NO_STORE
    with _store() as store:
        if not store.delete_block(block_id):
            raise HTTPException(404, f"no block {block_id}")
        return {"deleted": block_id, **_rebuild(store)}
