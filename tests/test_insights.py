"""The dashboard's numbers and the API that serves them.

Rows are built by hand so every expected figure is checkable by eye. Each test
names the regression it guards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracker.insights import lead_summary, lead_times, occupancy, reliability
from tracker.slots import parse_slot
from tracker.store import Store
from tracker.types import Sport

TODAY = dt.date(2026, 9, 23)


def row(
    day: dt.date,
    hour: int,
    *,
    minutes: int = 60,
    booked: bool = False,
    hudle: bool = False,
    booked_at: dt.datetime | None = None,
) -> dict[str, Any]:
    start_local = dt.datetime.combine(day, dt.time(hour))
    return {
        "business_date": day,
        "start_local": start_local,
        "start_utc": (start_local - dt.timedelta(hours=5, minutes=30)).replace(tzinfo=dt.UTC),
        "duration_minutes": minutes,
        "booked": booked,
        "hudle_booked": hudle and booked,
        "booked_at": booked_at,
        "price": 900,
        "facility_uuid": "f",
        "venue_uuid": "v",
    }


def test_occupancy_is_in_court_minutes_and_counts_venue_blocks_as_booked() -> None:
    """Regression: slot counts compared across 30- and 60-minute grids, or a
    venue's blocked (offline-sold) slots read as vacant."""
    day = TODAY - dt.timedelta(days=1)
    rows = [
        row(day, 18, minutes=60, booked=True, hudle=True),
        row(day, 19, minutes=30, booked=True, hudle=False),
        row(day, 20, minutes=30),
    ]
    occ = occupancy(rows)
    assert (occ.booked_minutes, occ.total_minutes) == (90, 120)
    assert (occ.hudle_booked_minutes, occ.blocked_minutes) == (60, 30)
    assert occ.rate == 0.75


def test_lead_time_ignores_blocks_and_bookings_entered_after_play() -> None:
    """Regression: a venue block's stamp, or an offline sale reconciled after
    the slot, read as a customer booking with a real lead time."""
    day = TODAY - dt.timedelta(days=1)
    r = row(day, 19, booked=True, hudle=True)
    r["booked_at"] = r["start_utc"] - dt.timedelta(hours=6)
    late = row(day, 20, booked=True, hudle=True)
    late["booked_at"] = late["start_utc"] + dt.timedelta(hours=2)
    block = row(day, 21, booked=True, hudle=False)
    block["booked_at"] = block["start_utc"] - dt.timedelta(days=3)
    assert lead_times([r, late, block]) == [6.0]
    assert lead_summary([6.0])["median_hours"] == 6.0


def _court(
    days: int, *, hudle_per_day: int, blocks_per_day: int, hours: int = 16
) -> list[dict[str, Any]]:
    out = []
    for d in range(1, days + 1):
        day = TODAY - dt.timedelta(days=d)
        for h in range(hours):
            if h < hudle_per_day:
                out.append(row(day, 6 + h, booked=True, hudle=True))
            elif h < hudle_per_day + blocks_per_day:
                out.append(row(day, 6 + h, booked=True, hudle=False))
            else:
                out.append(row(day, 6 + h))
    return out


def test_reliability_verdicts_follow_the_evidence() -> None:
    """Regression: a shopfront listing (the Padel Up pattern) counted as a real
    market signal, or a venue that runs its calendar on Hudle excluded."""
    assert (
        reliability(_court(14, hudle_per_day=3, blocks_per_day=1), TODAY)["verdict"] == "reliable"
    )
    assert reliability(_court(14, hudle_per_day=3, blocks_per_day=0), TODAY)["verdict"] == "partial"
    assert reliability(_court(14, hudle_per_day=0, blocks_per_day=4), TODAY)["verdict"] == "partial"
    assert (
        reliability(_court(14, hudle_per_day=0, blocks_per_day=0), TODAY)["verdict"] == "unreliable"
    )
    assert (
        reliability(_court(14, hudle_per_day=0, blocks_per_day=15), TODAY)["verdict"]
        == "unreliable"
    )
    assert reliability([], TODAY)["verdict"] == "no_data"


def test_the_overview_endpoint_serves_a_whole_view(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the dashboard's one read returning a shape the page cannot
    draw, or counting unreliable courts in the market totals."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    store = Store(url)
    store.initialize()
    seen = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)
    store.upsert_venue(venue_uuid="v1", name="Good Club", slug="g", numeric_id="1", seen_at=seen)
    store.upsert_court(
        facility_uuid="c1", venue_uuid="v1", name="Court 1", sport=Sport.PICKLEBALL, seen_at=seen
    )
    readings = []
    for d in range(1, 8):
        day = dt.date.today() - dt.timedelta(days=d)
        for h in range(6, 22):
            state = {"is_booked": h in (18, 19, 20), "is_available": h != 21}
            readings.append(
                parse_slot(
                    {
                        "id": f"s{d}-{h}",
                        "start_time": f"{day} {h:02d}:00:00",
                        "end_time": f"{day} {h:02d}:30:00",
                        "price": "300.00",
                        "updated_at": f"{day} 08:00:00",
                        **state,
                    },
                    venue_uuid="v1",
                    facility_uuid="c1",
                    sport=Sport.PICKLEBALL,
                    tz="Asia/Kolkata",
                    business_day_start_hour=4,
                )
            )
    store.apply(readings, seen_at=seen)
    store.close()

    monkeypatch.setenv("DATABASE_URL", url)
    from tracker.web.api import app

    body = (
        TestClient(app).get("/api/overview", params={"sport": "pickleball", "window": "7"}).json()
    )
    assert body["venues"][0]["name"] == "Good Club"
    assert body["venues"][0]["verdict"] == "reliable"
    assert body["totals"]["courts_counted"] == 1
    assert body["totals"]["occupancy"] == pytest.approx(4 / 16, abs=1e-3)
    assert body["venues"][0]["price_per_hour"] == 600
    assert {c["hour"] for c in body["heatmap"]} == set(range(6, 22))
    assert body["lead_time"]["n"] > 0
