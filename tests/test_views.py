"""Views built once by the daily pass and served whole by the API.

Each test names the regression it guards. The seeded market is the one the
overview tests use: two pickleball venues over the last fourteen days.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.test_insights import TODAY, _seed
from tracker.store import VIEW_COLUMNS, Store
from tracker.store import views as views_table
from tracker.types import Sport
from tracker.views import VIEWS_VERSION, all_views, view_key, views_as_of

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_an_evening_build_is_for_the_next_day() -> None:
    """Regression: the 23:00 pass building for today would leave the day it
    just read out of every window until the following evening."""
    evening = dt.datetime(2026, 9, 22, 22, 0, tzinfo=IST)
    morning = dt.datetime(2026, 9, 22, 7, 30, tzinfo=IST)
    assert views_as_of(evening, "Asia/Kolkata") == dt.date(2026, 9, 23)
    assert views_as_of(evening - dt.timedelta(minutes=1), "Asia/Kolkata") == dt.date(2026, 9, 22)
    assert views_as_of(morning, "Asia/Kolkata") == dt.date(2026, 9, 22)


BUILT_AT = dt.datetime(2026, 9, 22, 17, 30, tzinfo=dt.UTC)


def _json(views: Any) -> Any:
    return json.loads(json.dumps(dict(views), default=str))


def test_every_view_the_page_can_ask_for_is_built(tmp_path: Any) -> None:
    """Regression: a sport, window or venue the page can click having no stored
    view, which the API answers with an error instead of charts."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    built = dict(all_views(store, TODAY))
    for sport in Sport:
        for window in ("7", "30", "all"):
            assert view_key(sport, window, None) in built
    for venue in ("v1", "v2"):
        assert view_key(Sport.PICKLEBALL, "30", venue) in built
    store.close()


def test_the_api_serves_only_stored_views_with_edge_caching(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the API computing a view it has not got from three months
    of slots per request (a preview of a new view version used up the
    database's monthly transfer that way), or answering without the header
    that lets Vercel's edge cache it."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    marker = {"sport": "pickleball", "stored": True}
    store.replace_views(
        [(view_key(Sport.PICKLEBALL, "30", None), json.dumps(marker))],
        version=VIEWS_VERSION,
        as_of=TODAY,
        built_at=BUILT_AT,
    )
    store.close()
    monkeypatch.setenv("DATABASE_URL", url)
    from tracker.web import api

    client = TestClient(api.app)
    hit = client.get("/api/overview", params={"sport": "pickleball", "window": "30"})
    assert hit.json() == marker
    assert "s-maxage" in hit.headers["cache-control"]

    unbuilt = client.get("/api/overview", params={"sport": "padel", "window": "30"})
    assert unbuilt.status_code == 503
    venue = client.get(
        "/api/overview", params={"sport": "pickleball", "window": "30", "venue": "x"}
    )
    assert venue.status_code == 404, "a built version without the venue: the page falls back"


def test_a_build_keeps_the_version_before_its_own() -> None:
    """Regression: a preview building a new version's views deleting
    production's (the dashboard then answers 503), or retired versions'
    views staying forever."""
    store = Store("sqlite://")
    store.initialize()
    for key, version in (
        ("retired", VIEWS_VERSION - 2),
        ("production", VIEWS_VERSION - 1),
        ("mine", VIEWS_VERSION),
    ):
        store.replace_views([(key, "{}")], version=version, as_of=TODAY, built_at=BUILT_AT)
    later = BUILT_AT + dt.timedelta(days=1)
    # Stored before views had a version column: known by their key alone.
    with store._engine.begin() as conn:
        conn.execute(
            sa.insert(views_table),
            [
                {
                    "view_key": f"v{n}|padel|7|",
                    "as_of": TODAY,
                    "built_at": BUILT_AT,
                    "payload": "{}",
                }
                for n in (VIEWS_VERSION - 2, VIEWS_VERSION - 1)
            ],
        )
    store.replace_views([("mine again", "{}")], version=VIEWS_VERSION, as_of=TODAY, built_at=later)
    keys = ("retired", "production", "mine", "mine again")
    kept = {k: store.view(k) for k in keys}
    assert kept == {"retired": None, "production": "{}", "mine": None, "mine again": "{}"}
    old, before = (f"v{n}|padel|7|" for n in (VIEWS_VERSION - 2, VIEWS_VERSION - 1))
    assert (store.view(old), store.view(before)) == (None, "{}")
    assert store.views_built_at(VIEWS_VERSION) == later
    assert store.views_built_at(VIEWS_VERSION - 1) == BUILT_AT


def test_a_build_from_the_mirror_matches_one_from_the_database(tmp_path: Any) -> None:
    """Regression: the mirror losing a column or a row the views read, so the
    daily build (which reads the mirror) draws different charts."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store, mirror = Store(url), Store(f"sqlite:///{tmp_path / 'mirror.sqlite'}")
    mirror.initialize()
    first = TODAY - dt.timedelta(days=90)
    assert mirror.refresh_mirror(store, from_date=first) == 2 * 14 * 16
    assert _json(all_views(store, TODAY, mirror=mirror)) == _json(all_views(store, TODAY))
    store.close()
    mirror.close()


def test_the_views_read_only_their_columns(tmp_path: Any) -> None:
    """Regression: the views' read carrying every slot column again, half as
    many bytes again per build for columns no view draws."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    rows = store.slots_between(
        sport=Sport.PICKLEBALL, date_from=TODAY - dt.timedelta(days=3), date_to=TODAY
    )
    assert rows and set(rows[0]) == set(VIEW_COLUMNS)
    store.close()


def test_the_map_adds_up_to_the_headline_figures(tmp_path: Any) -> None:
    """Regression: the map's demand total drifting from the booked court-hours
    in the headline card, because it summed venues or courts the headline
    leaves out."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    view = dict(all_views(store, TODAY))[view_key(Sport.PICKLEBALL, "30", None)]
    counted = [
        c
        for v in view["venues"]
        if v["verdict"] != "unreliable"
        for c in v["courts"]
        if c["verdict"] in ("reliable", "partial")
    ]
    assert sum(c["booked_hours"] for c in counted) == pytest.approx(view["totals"]["booked_hours"])
    assert len(counted) == view["totals"]["courts_counted"]
    store.close()


def test_a_failing_court_is_flagged_and_left_out_of_every_figure(tmp_path: Any) -> None:
    """Regression: a court Hudle stopped answering for (its recent days never
    read) dragging the market's figures down, with nothing on the page to say
    why its venue went quiet."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    key = view_key(Sport.PICKLEBALL, "7", None)
    before = dict(all_views(store, TODAY))[key]
    store.record_court_read("c2", at=BUILT_AT, error="hudle returned HTTP 404")
    after = dict(all_views(store, TODAY))[key]

    quiet = next(v for v in after["venues"] if v["venue_uuid"] == "v2")
    assert quiet["failing"] and quiet["verdict"] == "unreliable"
    assert quiet["courts"][0]["failing"] and "did not read" in quiet["courts"][0]["reasons"][0]
    good = next(v for v in after["venues"] if v["venue_uuid"] == "v1")
    assert not good["failing"] and good["verdict"] == "reliable"
    assert (before["totals"]["courts_counted"], after["totals"]["courts_counted"]) == (2, 1)
    assert after["totals"]["booked_hours"] == good["booked_hours"]
    store.close()


def test_a_court_the_latest_pass_did_not_reach_is_flagged(tmp_path: Any) -> None:
    """Regression: a pass stopped by the circuit breaker leaving the courts after
    it counted, their latest days read only while still ahead (under-booked),
    with nothing on the page to say so."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)
    store.record_court_read("c1", at=BUILT_AT)
    store.record_court_read("c2", at=BUILT_AT + dt.timedelta(days=1))
    store.start_run("daily", started_at=BUILT_AT + dt.timedelta(days=1))
    view = dict(all_views(store, TODAY))[view_key(Sport.PICKLEBALL, "7", None)]
    failing = {v["venue_uuid"]: v["failing"] for v in view["venues"]}
    assert failing == {"v1": True, "v2": False}
    assert view["totals"]["courts_counted"] == 1
    store.close()


def test_a_mirror_file_is_replaced_only_by_a_finished_build(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a job killed while refreshing the mirror caching a half-written
    file, which then fails every later run; or a file that will not open
    stopping the build."""
    from tracker.__main__ import _mirror

    path = tmp_path / "mirror.db"
    path.write_bytes(b"not a database")
    monkeypatch.setenv("SLOT_MIRROR", str(path))
    with _mirror() as mirror:
        assert mirror is not None
        assert mirror.slots_between(sport=Sport.PICKLEBALL, date_from=TODAY, date_to=TODAY) == []
    whole = path.read_bytes()
    assert whole.startswith(b"SQLite format 3")

    with pytest.raises(RuntimeError), _mirror():
        raise RuntimeError("killed halfway")
    assert path.read_bytes() == whole
