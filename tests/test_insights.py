"""The dashboard's numbers and the API that serves them.

Rows are built by hand so every expected figure is checkable by eye. Each test
names the regression it guards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracker.insights import (
    by_date,
    by_hour,
    by_weekday,
    lead_summary,
    lead_times,
    occupancy,
    peak,
    reliability,
    revenue,
)
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
    blocked: bool = False,
    booked_at: dt.datetime | None = None,
) -> dict[str, Any]:
    start_local = dt.datetime.combine(day, dt.time(hour))
    return {
        "business_date": day,
        "start_local": start_local,
        "start_utc": (start_local - dt.timedelta(hours=5, minutes=30)).replace(tzinfo=dt.UTC),
        "duration_minutes": minutes,
        "booked": booked,
        "hudle_booked": booked,
        "hudle_available": not blocked,
        "booked_at": booked_at,
        "price": 900,
        "facility_uuid": "f",
        "venue_uuid": "v",
    }


def test_occupancy_is_in_court_minutes_and_leaves_venue_blocks_out() -> None:
    """Regression: slot counts compared across 30- and 60-minute grids, or a
    venue block counted as a booking (a court blocked all day reading full)."""
    day = TODAY - dt.timedelta(days=1)
    rows = [
        row(day, 18, minutes=60, booked=True),
        row(day, 19, minutes=30, blocked=True),
        row(day, 20, minutes=30),
    ]
    occ = occupancy(rows)
    assert (occ.booked_minutes, occ.total_minutes, occ.blocked_minutes) == (60, 90, 30)
    assert occ.rate == pytest.approx(2 / 3)
    assert occupancy([row(day, 18, blocked=True)]).rate is None


def test_hour_profile_runs_in_business_day_order_and_counts_its_days() -> None:
    """Regression: a 01:00 slot (which belongs to the evening before) drawn at
    the start of the day, or court-hours per day divided by the wrong day count."""
    d1, d2 = TODAY - dt.timedelta(days=2), TODAY - dt.timedelta(days=1)
    rows = [
        row(d1, 1, booked=True),
        row(d1, 19, booked=True),
        row(d2, 19, minutes=30),
        row(d2, 6),
    ]
    hours = by_hour(rows)
    assert [h["hour"] for h in hours] == [6, 19, 1]
    nineteen = hours[1]
    assert (nineteen["days"], nineteen["booked_hours"], nineteen["vacant_hours"]) == (2, 1.0, 0.5)
    assert nineteen["occupancy"] == pytest.approx(1 / 1.5, abs=1e-4)


def test_weekday_and_date_profiles_split_the_same_court_minutes() -> None:
    """Regression: the weekday or daily breakdown losing or double-counting
    court time relative to the headline."""
    monday = dt.date(2026, 9, 21)
    rows = [row(monday, 18, booked=True), row(monday, 19), row(monday + dt.timedelta(days=1), 18)]
    weekdays = by_weekday(rows)
    assert [(w["weekday"], w["occupancy"], w["days"]) for w in weekdays] == [
        (0, 0.5, 1),
        (1, 0.0, 1),
    ]
    dates = by_date(rows)
    assert [d["business_date"] for d in dates] == ["2026-09-21", "2026-09-22"]
    assert sum(d["total_hours"] for d in dates) == occupancy(rows).total_minutes / 60


def test_peak_ignores_an_hour_with_too_little_court_time_to_mean_anything() -> None:
    """Regression: "peak hour" naming 05:00 because one court sold its only
    dawn slot, over an evening with ten times the court time."""
    entries = [
        {"hour": 5, "occupancy": 1.0, "total_hours": 1.0},
        {"hour": 19, "occupancy": 0.6, "total_hours": 10.0},
        {"hour": 20, "occupancy": 0.5, "total_hours": 10.0},
    ]
    assert peak(entries) == entries[1]
    assert peak([]) is None


def test_lead_time_ignores_blocks_and_bookings_entered_after_play() -> None:
    """Regression: a venue block's stamp, or an offline sale reconciled after
    the slot, read as a customer booking with a real lead time."""
    day = TODAY - dt.timedelta(days=1)
    r = row(day, 19, booked=True)
    r["booked_at"] = r["start_utc"] - dt.timedelta(hours=6)
    late = row(day, 20, booked=True)
    late["booked_at"] = late["start_utc"] + dt.timedelta(hours=2)
    block = row(day, 21, blocked=True)
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
                out.append(row(day, 6 + h, booked=True))
            elif h < hudle_per_day + blocks_per_day:
                out.append(row(day, 6 + h, blocked=True))
            else:
                out.append(row(day, 6 + h))
    return out


def test_fully_blocked_courts_and_dead_listings_are_left_out() -> None:
    """Regression: a court the venue blocks every day counted as full (it
    offered nothing, so it has no occupancy), a quiet court dropped (inflating
    the market), or a dead listing's 0% counted as demand."""

    def judge(hudle: int, blocks: int) -> dict[str, Any]:
        return reliability(_court(14, hudle_per_day=hudle, blocks_per_day=blocks), TODAY)

    assert judge(3, 1)["verdict"] == "reliable"
    assert judge(3, 0)["verdict"] == "reliable"
    assert judge(0, 4)["verdict"] == "unreliable", "blocks are not sales"
    everything_blocked = judge(0, 16)
    assert everything_blocked["verdict"] == "unreliable"
    assert everything_blocked["occupancy"] is None
    assert "blocked" in everything_blocked["reasons"][0]
    assert judge(0, 0)["verdict"] == "unreliable"
    quiet = (
        _court(3, hudle_per_day=2, blocks_per_day=0)
        + _court(14, hudle_per_day=0, blocks_per_day=0)[48:]
    )
    assert reliability(quiet, TODAY)["verdict"] == "partial"
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
    # Three sold of the fifteen offered; the 21:00 block is out of both sides.
    assert body["totals"]["occupancy"] == pytest.approx(3 / 15, abs=1e-3)
    assert body["totals"]["blocked_hours"] == pytest.approx(body["window"]["days"] * 0.5)
    assert body["venues"][0]["price_per_hour"] == 600
    assert {c["hour"] for c in body["heatmap"]} == set(range(6, 22))
    assert body["lead_time"]["n"] > 0


def _seed(url: str) -> None:
    """Two venues on one sport: Good Club sells 3 of 16 hours and blocks one
    every day; Quiet Club sells one evening hour. Both over the last 14 days."""
    store = Store(url)
    store.initialize()
    seen = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)
    readings = []
    for venue, court, name, sold in (
        ("v1", "c1", "Good Club", (18, 19, 20)),
        ("v2", "c2", "Quiet Club", (19,)),
    ):
        store.upsert_venue(venue_uuid=venue, name=name, slug=venue, numeric_id=venue, seen_at=seen)
        store.upsert_court(
            facility_uuid=court,
            venue_uuid=venue,
            name="Court 1",
            sport=Sport.PICKLEBALL,
            seen_at=seen,
        )
        for d in range(1, 15):
            day = TODAY - dt.timedelta(days=d)
            for h in range(6, 22):
                readings.append(
                    parse_slot(
                        {
                            "id": f"{court}-{d}-{h}",
                            "start_time": f"{day} {h:02d}:00:00",
                            "end_time": f"{day} {h:02d}:30:00",
                            "price": "300.00",
                            "updated_at": f"{day} 08:00:00",
                            "is_booked": h in sold,
                            "is_available": not (venue == "v1" and h == 21),
                        },
                        venue_uuid=venue,
                        facility_uuid=court,
                        sport=Sport.PICKLEBALL,
                        tz="Asia/Kolkata",
                        business_day_start_hour=4,
                    )
                )
    store.set_venue_location("v1", latitude=26.85, longitude=75.80)
    store.apply(readings, seen_at=seen)
    store.close()


def test_a_venue_narrows_every_chart(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the venue drill-down still drawing the market's hours and
    weekdays, or a mistyped venue silently answering with the whole market."""
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    _seed(url)
    monkeypatch.setenv("DATABASE_URL", url)
    from tracker.web import api

    # Pin the API's clock to the seeded one: the machine's date and Jaipur's
    # differ for half of every day.
    monkeypatch.setattr(api, "_today", lambda: TODAY)
    client = TestClient(api.app)
    market = client.get("/api/overview", params={"sport": "pickleball", "window": "7"}).json()
    assert market["totals"]["occupancy"] == pytest.approx(4 / 31, abs=1e-3)
    assert market["window"]["days"] == 7

    good = client.get(
        "/api/overview", params={"sport": "pickleball", "window": "7", "venue": "v1"}
    ).json()
    assert good["venue"] == "v1"
    assert good["totals"]["occupancy"] == pytest.approx(3 / 15, abs=1e-3)
    assert good["totals"]["courts_counted"] == 1
    assert len(good["venues"]) == 2, "the list stays whole so the reader can switch venue"
    where = {v["venue_uuid"]: (v["latitude"], v["longitude"]) for v in good["venues"]}
    assert where == {"v1": (26.85, 75.80), "v2": (None, None)}, "the map places only located venues"
    by_hr = {h["hour"]: h for h in good["hours"]}
    assert by_hr[18]["occupancy"] == 1.0 and by_hr[6]["occupancy"] == 0.0
    assert by_hr[18]["days"] == 7
    assert sum(d["total_hours"] for d in good["days"]) == pytest.approx(7 * 15 * 0.5)
    assert {w["weekday"] for w in good["weekdays"]} == set(range(7))
    assert good["peaks"]["hour"]["hour"] in (18, 19, 20, 21)

    missing = client.get(
        "/api/overview", params={"sport": "pickleball", "window": "7", "venue": "nope"}
    )
    assert missing.status_code == 404


def test_revenue_counts_customer_bookings_at_listed_price_and_nothing_else() -> None:
    """Regression: a venue block earning revenue (it was never sold), vacant
    slots adding their price, or a day's revenue missing from the by-day data."""
    day = TODAY - dt.timedelta(days=1)
    rows = [row(day, 18, booked=True), row(day, 19, blocked=True), row(day, 20)]
    assert revenue(rows) == 900
    assert [d["revenue"] for d in by_date(rows)] == [900]
