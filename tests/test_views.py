"""Views built once by the daily pass and served whole by the API.

Each test names the regression it guards. The seeded market is the one the
overview tests use: two pickleball venues over the last fourteen days.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_insights import TODAY, _seed
from tracker.insights import lead_histogram
from tracker.store import Store
from tracker.types import Sport
from tracker.views import all_views, overview, rows_for, view_key, views_as_of

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_an_evening_build_is_for_the_next_day() -> None:
    """Regression: the 23:00 pass building for today would leave the day it
    just read out of every window until the following evening."""
    evening = dt.datetime(2026, 9, 22, 22, 0, tzinfo=IST)
    morning = dt.datetime(2026, 9, 22, 7, 30, tzinfo=IST)
    assert views_as_of(evening, "Asia/Kolkata") == dt.date(2026, 9, 23)
    assert views_as_of(evening - dt.timedelta(minutes=1), "Asia/Kolkata") == dt.date(2026, 9, 22)
    assert views_as_of(morning, "Asia/Kolkata") == dt.date(2026, 9, 22)


def test_lead_histogram_buckets_on_the_lower_edge() -> None:
    """Regression: a booking exactly 24h ahead counted as '12-24h', or a
    week-plus booking dropped off the end."""
    counts = [b["count"] for b in lead_histogram([0.5, 3.0, 23.9, 24.0, 500.0])]
    # <3h, 3-6h, 6-12h, 12-24h, 1-2d, 2-3d, 3-7d, 7d+
    assert counts == [1, 1, 0, 1, 1, 0, 0, 1]


def test_every_view_is_built_and_matches_the_live_answer(tmp_path: Any) -> None:
    """Regression: a stored view drifting from what the API would compute, or
    a venue the page can click having no stored view (it would fall back to
    the slow path)."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    built = dict(all_views(store, TODAY))
    for sport in Sport:
        for window in ("7", "30", "all"):
            assert view_key(sport, window, None) in built
    for venue in ("v1", "v2"):
        assert view_key(Sport.PICKLEBALL, "30", venue) in built

    rows = rows_for(store, Sport.PICKLEBALL, TODAY)
    courts = {c.facility_uuid: c for c in store.tracked_courts(Sport.PICKLEBALL)}
    live = overview(
        rows, courts, store.venues(), sport=Sport.PICKLEBALL, window="7", today=TODAY, venue="v1"
    )
    stored = json.loads(json.dumps(built[view_key(Sport.PICKLEBALL, "7", "v1")], default=str))
    assert stored == json.loads(json.dumps(live, default=str))
    store.close()


def test_the_api_serves_the_stored_view_with_edge_caching(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the API recomputing on every click (the 3-9 s it used to
    take), or answering without the header that lets Vercel's edge cache it."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    marker = {"sport": "pickleball", "stored": True}
    store.replace_views(
        [(view_key(Sport.PICKLEBALL, "30", None), json.dumps(marker))],
        as_of=TODAY,
        built_at=dt.datetime(2026, 9, 22, 17, 30, tzinfo=dt.UTC),
    )
    store.close()
    monkeypatch.setenv("DATABASE_URL", url)
    from tracker.web import api

    monkeypatch.setattr(api, "_today", lambda: TODAY)
    client = TestClient(api.app)
    hit = client.get("/api/overview", params={"sport": "pickleball", "window": "30"})
    assert hit.json() == marker
    assert "s-maxage" in hit.headers["cache-control"]

    # A view that was never built is still answered, computed on the spot.
    miss = client.get("/api/overview", params={"sport": "pickleball", "window": "7"})
    assert miss.status_code == 200 and miss.json()["totals"]["courts_counted"] == 2
