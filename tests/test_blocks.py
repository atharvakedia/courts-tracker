"""Hours people mark as blocked at a venue, and what they do to every figure.

Each test names the regression it guards. The seeded market is the one the
overview tests use: Good Club (v1) sells 18:00, 19:00 and 20:00 every day and
blocks 21:00 itself; Quiet Club (v2) sells 19:00.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_insights import TODAY, _seed
from tracker.blocks import Block, apply_blocks
from tracker.insights import is_blocked, is_booked, occupancy, revenue
from tracker.store import Store
from tracker.types import Sport
from tracker.views import all_views, view_key

#: Noon in Jaipur on the seeded TODAY, so a rebuild builds the seeded day's views.
NOON = dt.datetime(2026, 9, 23, 6, 30, tzinfo=dt.UTC)
PASSWORD = "let-me-in"
EVERY_DAY_AT_18 = [[wd, 18] for wd in range(7)]


def _row(day: dt.date, hour: int, *, venue: str = "v1", booked: bool = True) -> dict[str, Any]:
    return {
        "venue_uuid": venue,
        "business_date": day,
        "start_local": dt.datetime.combine(day, dt.time(hour)),
        "duration_minutes": 60,
        "price": 500,
        "hudle_booked": booked,
        "hudle_available": not booked,
    }


def _block(**kw: Any) -> Block:
    return Block(
        **{
            "block_id": 1,
            "venue_uuid": "v1",
            "cells": frozenset({(0, 7)}),
            "date_from": None,
            "date_to": None,
            "note": "",
            "created_at": NOON,
            **kw,
        }
    )


MONDAY = dt.date(2026, 9, 21)


def test_a_block_covers_only_its_venue_cells_and_dates() -> None:
    """Regression: a block leaking onto another venue, another hour, another
    weekday, or days outside its dates."""
    block = _block(date_from=MONDAY, date_to=MONDAY + dt.timedelta(days=7))
    rows = [
        _row(MONDAY, 7),  # covered
        _row(MONDAY, 8),  # another hour
        _row(MONDAY, 7, venue="v2"),  # another venue
        _row(MONDAY + dt.timedelta(days=1), 7),  # a Tuesday
        _row(MONDAY + dt.timedelta(days=7), 7),  # the last Monday in range
        _row(MONDAY + dt.timedelta(days=14), 7),  # after date_to
        _row(MONDAY - dt.timedelta(days=7), 7),  # before date_from
    ]
    marked = [is_blocked(r) for r in apply_blocks(rows, [block])]
    assert marked == [True, False, False, False, True, False, False]


def test_a_marked_booking_is_a_block_not_a_sale() -> None:
    """Regression: a venue's fake booking still counted as booked, as offered
    court time, or as revenue once someone marked the hour blocked."""
    rows = apply_blocks([_row(MONDAY, 7), _row(MONDAY, 8)], [_block()])
    assert [is_booked(r) for r in rows] == [False, True]
    occ = occupancy(rows)
    assert (occ.booked_minutes, occ.total_minutes, occ.blocked_minutes) == (60, 60, 60)
    assert revenue(rows) == 500


def test_no_blocks_leaves_the_rows_untouched() -> None:
    rows = [_row(MONDAY, 7)]
    assert apply_blocks(rows, []) == rows


def test_the_views_read_blocks_from_the_store(tmp_path: Any) -> None:
    """Regression: a saved block stored but never applied to the figures."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    store = Store(url)

    def v1() -> dict[str, Any]:
        view = dict(all_views(store, TODAY))[view_key(Sport.PICKLEBALL, "7", "v1")]
        return dict(view["totals"])

    before = v1()
    assert (before["booked_hours"], before["total_hours"], before["blocked_hours"]) == (
        10.5,
        52.5,
        3.5,
    )
    store.add_block(
        venue_uuid="v1",
        cells=[(wd, 18) for wd in range(7)],
        date_from=None,
        date_to=None,
        note="coaching",
        created_at=NOON,
    )
    after = v1()
    assert (after["booked_hours"], after["total_hours"], after["blocked_hours"]) == (
        7.0,
        49.0,
        7.0,
    )
    store.close()


def test_a_database_without_the_blocks_table_has_no_blocks(tmp_path: Any) -> None:
    """Regression: a preview deploy on production, before the next pass has
    created the table, failing every view it computes on the spot."""
    store = Store(f"sqlite:///{tmp_path / 'db.sqlite'}")
    assert store.blocks() == []
    store.close()


@pytest.fixture
def client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    from tracker.web import api

    monkeypatch.setattr(api, "_now", lambda: NOON)
    return TestClient(api.app)


def _v1_week(client: TestClient) -> dict[str, Any]:
    params = {"sport": "pickleball", "window": "7", "venue": "v1"}
    totals: dict[str, Any] = client.get("/api/overview", params=params).json()["totals"]
    return totals


def test_saving_a_block_rebuilds_the_views(client: TestClient) -> None:
    """Regression: a saved block not showing until the next daily pass, or
    deleting it leaving the figures blocked."""
    body = {"venue_uuid": "v1", "cells": EVERY_DAY_AT_18, "note": "coaching"}
    saved = client.post("/api/blocks", json=body, headers={"X-Admin-Password": PASSWORD})
    assert saved.status_code == 201, saved.text
    block = saved.json()["block"]
    assert block["cells"] == EVERY_DAY_AT_18 and block["note"] == "coaching"
    assert saved.json()["rebuild"] == "done" and saved.json()["views_built_at"]
    assert _v1_week(client)["booked_hours"] == 7.0

    listed = client.get("/api/blocks", params={"venue": "v1"}).json()["blocks"]
    assert [b["block_id"] for b in listed] == [block["block_id"]]
    assert client.get("/api/blocks", params={"venue": "v2"}).json()["blocks"] == []

    gone = client.delete(f"/api/blocks/{block['block_id']}", headers={"X-Admin-Password": PASSWORD})
    assert gone.status_code == 200
    assert _v1_week(client)["booked_hours"] == 10.5
    assert client.get("/api/blocks").json()["blocks"] == []


@pytest.mark.parametrize(("github", "rebuild"), [(204, "queued"), (403, "failed")])
def test_with_a_token_a_change_starts_the_views_workflow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, github: int, rebuild: str
) -> None:
    """Regression: a block saved in production rebuilding inside the function,
    which reads three months of slots from the database on every save; a save
    on a preview rebuilding production's views from the branch's code; or a
    refused dispatch losing the block that was saved."""
    from tracker.web import api

    for name, value in {
        "GITHUB_DISPATCH_TOKEN": "token",
        "VERCEL_GIT_REPO_OWNER": "owner",
        "VERCEL_GIT_REPO_SLUG": "repo",
        "VERCEL_GIT_COMMIT_REF": "feat/x",
    }.items():
        monkeypatch.setenv(name, value)
    sent: list[tuple[str, Any]] = []

    def post(url: str, **kw: Any) -> httpx.Response:
        sent.append((url, kw["json"]))
        return httpx.Response(github, request=httpx.Request("POST", url))

    monkeypatch.setattr(api.httpx, "post", post)
    body = {"venue_uuid": "v1", "cells": EVERY_DAY_AT_18}
    saved = client.post("/api/blocks", json=body, headers={"X-Admin-Password": PASSWORD})
    assert saved.status_code == 201
    assert (saved.json()["rebuild"], saved.json()["views_built_at"]) == (rebuild, None)
    assert sent == [
        (
            "https://api.github.com/repos/owner/repo/actions/workflows/views.yml/dispatches",
            {"ref": "main"},
        )
    ]
    assert client.get("/api/health").json()["views_built_at"] is None, "nothing built here"
    assert len(client.get("/api/blocks").json()["blocks"]) == 1


def test_a_rebuild_that_fails_here_still_reports_the_save(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a rebuild error after the block was stored answering 500,
    so the reader saves again and the block is stored twice."""
    from tracker.web import api

    def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("timed out")

    monkeypatch.setattr(api, "publish_views", broken)
    body = {"venue_uuid": "v1", "cells": EVERY_DAY_AT_18}
    saved = client.post("/api/blocks", json=body, headers={"X-Admin-Password": PASSWORD})
    assert saved.status_code == 201 and saved.json()["rebuild"] == "failed"
    assert len(client.get("/api/blocks").json()["blocks"]) == 1


def test_changing_blocks_takes_the_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: any visitor to the public dashboard rewriting its figures."""
    body = {"venue_uuid": "v1", "cells": EVERY_DAY_AT_18}
    assert client.post("/api/blocks", json=body).status_code == 401
    wrong = client.post("/api/blocks", json=body, headers={"X-Admin-Password": "guess"})
    assert wrong.status_code == 401
    assert client.delete("/api/blocks/1").status_code == 401
    monkeypatch.delenv("ADMIN_PASSWORD")
    off = client.post("/api/blocks", json=body, headers={"X-Admin-Password": PASSWORD})
    assert off.status_code == 503
    assert client.get("/api/blocks").json()["blocks"] == []


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"cells": []}, 422),
        ({"cells": [[7, 18]]}, 422),
        ({"cells": [[0, 24]]}, 422),
        ({"date_from": "2026-09-10", "date_to": "2026-09-01"}, 422),
        ({"note": "x" * 201}, 422),
        ({"venue_uuid": "nowhere"}, 404),
    ],
)
def test_a_malformed_block_is_refused(
    client: TestClient, change: dict[str, Any], status: int
) -> None:
    body = {"venue_uuid": "v1", "cells": EVERY_DAY_AT_18, **change}
    got = client.post("/api/blocks", json=body, headers={"X-Admin-Password": PASSWORD})
    assert got.status_code == status, got.text


def test_deleting_a_missing_block_is_a_404(client: TestClient) -> None:
    got = client.delete("/api/blocks/99", headers={"X-Admin-Password": PASSWORD})
    assert got.status_code == 404


def test_health_names_the_views_revision(client: TestClient) -> None:
    """Regression: the page keying its views on nothing that changes when they
    are rebuilt, so a saved block waits out the edge cache."""
    assert client.get("/api/health").json()["views_built_at"] is None
    client.post(
        "/api/blocks",
        json={"venue_uuid": "v1", "cells": EVERY_DAY_AT_18},
        headers={"X-Admin-Password": PASSWORD},
    )
    assert client.get("/api/health").json()["views_built_at"] == NOON.isoformat()
