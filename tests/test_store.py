"""The slot store: one row per slot, rewritten only when something changes.

Each test names the regression it guards. They run on in-memory SQLite; the
upsert is built from the dialect, and test_store_live runs the same logic on
the Neon Postgres the tracker uses.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from tracker.slots import SlotReading, parse_grid, parse_slot
from tracker.store import Store
from tracker.types import Sport

T0 = dt.datetime(2026, 9, 20, 3, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(days=1)
T2 = T1 + dt.timedelta(days=1)
VENUE, COURT = "venue-1", "court-1"


def raw(
    *,
    booked: bool = False,
    available: bool = True,
    updated: str = "2026-09-19 10:00:00",
    start: str = "2026-09-25 19:00:00",
    end: str = "2026-09-25 19:30:00",
    price: str = "900.00",
    slot_id: str = "slot-1",
) -> dict[str, Any]:
    return {
        "id": slot_id,
        "start_time": start,
        "end_time": end,
        "price": price,
        "is_booked": booked,
        "is_available": available,
        "created_at": "2026-08-25 02:00:00",
        "updated_at": updated,
    }


def reading(**kw: Any) -> SlotReading:
    return parse_slot(
        raw(**kw),
        venue_uuid=VENUE,
        facility_uuid=COURT,
        sport=Sport.PICKLEBALL,
        tz="Asia/Kolkata",
        business_day_start_hour=4,
    )


@pytest.fixture()
def store() -> Store:
    s = Store("sqlite://")
    s.initialize()
    return s


def row(store: Store) -> dict[str, Any]:
    rows = store.slots_between(
        sport=Sport.PICKLEBALL, date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 10, 30)
    )
    assert len(rows) == 1
    return rows[0]


def test_a_venue_block_counts_as_booked() -> None:
    """Regression: blocked slots read as vacant, so a venue that records its
    offline sales by blocking looks empty. The agreed rule is booked-or-vacant."""
    assert reading(booked=True).booked
    assert reading(booked=False, available=False).booked
    assert not reading(booked=False, available=True).booked


def test_hudle_times_become_utc_and_late_night_slots_belong_to_the_previous_day() -> None:
    """Regression: IST wall-clock stored as UTC (5.5h off), or a 00:30 Saturday
    slot counted as Saturday demand when it is Friday night's session."""
    r = reading(
        start="2026-09-26 00:30:00", end="2026-09-26 01:00:00", updated="2026-09-24 21:15:00"
    )
    assert r.start_utc == dt.datetime(2026, 9, 25, 19, 0, tzinfo=dt.UTC)
    assert r.upstream_updated_at == dt.datetime(2026, 9, 24, 15, 45, tzinfo=dt.UTC)
    assert r.business_date == dt.date(2026, 9, 25)
    assert r.duration_minutes == 30


def test_a_slot_already_booked_when_first_seen_takes_its_booking_time_from_hudle(
    store: Store,
) -> None:
    """Regression: bookings made before we started watching get no booking time.
    Backfill depends on this -- Hudle's updated_at is the only record of it."""
    store.apply([reading(booked=True, updated="2026-09-18 09:00:00")], seen_at=T0)
    r = row(store)
    assert r["booked"] and r["booked_at"] == dt.datetime(2026, 9, 18, 3, 30, tzinfo=dt.UTC)
    assert r["cancel_count"] == 0


def test_vacant_to_booked_stamps_the_booking_and_the_pass_that_saw_it(store: Store) -> None:
    """Regression: a booking seen on a later pass keeps a null booked_at."""
    store.apply([reading(booked=False)], seen_at=T0)
    assert row(store)["booked_at"] is None
    store.apply([reading(booked=True, updated="2026-09-20 18:00:00")], seen_at=T1)
    r = row(store)
    assert r["booked"]
    assert r["booked_at"] == dt.datetime(2026, 9, 20, 12, 30, tzinfo=dt.UTC)
    assert r["booked_seen_at"] == T1
    # the cross-check the daily job relies on: the stamp falls between the passes
    assert T0 < r["booked_at"] < T1


def test_booked_to_vacant_counts_a_cancellation_and_clears_the_booking(store: Store) -> None:
    """Regression: cancellations silently erased, or a cancelled slot still
    counted as booked because booked_at was left behind."""
    store.apply([reading(booked=True)], seen_at=T0)
    store.apply([reading(booked=False, updated="2026-09-20 20:00:00")], seen_at=T1)
    r = row(store)
    assert not r["booked"] and r["booked_at"] is None and r["booked_seen_at"] is None
    assert r["cancel_count"] == 1 and r["last_cancelled_at"] == T1
    store.apply([reading(booked=True, updated="2026-09-21 08:00:00")], seen_at=T2)
    r = row(store)
    assert r["booked"] and r["cancel_count"] == 1
    assert r["booked_at"] == dt.datetime(2026, 9, 21, 2, 30, tzinfo=dt.UTC)


def test_an_unchanged_slot_is_not_rewritten(store: Store) -> None:
    """Regression: every pass rewriting every row, which is the storage growth
    this design exists to remove. A second identical pass writes nothing."""
    assert store.apply([reading(booked=True)], seen_at=T0) == 1
    assert store.apply([reading(booked=True)], seen_at=T1) == 0
    assert row(store)["first_seen_at"] == T0


def test_a_price_change_is_recorded(store: Store) -> None:
    """Regression: price tracking lost because only state changes write."""
    store.apply([reading(price="900.00")], seen_at=T0)
    assert store.apply([reading(price="1100.00")], seen_at=T1) == 1
    assert float(row(store)["price"]) == 1100.0


def test_a_whole_grid_payload_parses_every_slot(raw_slots_padel_fort: Any) -> None:
    """Regression: slots dropped between the Hudle payload and the store."""
    readings = parse_grid(
        raw_slots_padel_fort,
        venue_uuid=VENUE,
        facility_uuid=COURT,
        sport=Sport.PADEL,
        tz="Asia/Kolkata",
        business_day_start_hour=4,
    )
    total = sum(len(d.get("slots") or []) for d in raw_slots_padel_fort["data"]["slot_data"])
    assert len(readings) == total == 1116
    # 6 bought through Hudle + 18 blocked by the venue, all booked under the rule
    assert sum(r.booked for r in readings) == 24


def test_runs_are_recorded(store: Store) -> None:
    """Regression: a failed pass indistinguishable from a quiet day."""
    run = store.start_run(
        "daily", started_at=T0, window=(dt.date(2026, 9, 19), dt.date(2026, 10, 3))
    )
    store.finish_run(
        run,
        finished_at=T0 + dt.timedelta(minutes=35),
        courts_ok=142,
        courts_failed=1,
        slots_seen=40000,
        slots_written=900,
        error=None,
    )
    latest = store.latest_runs(1)[0]
    assert latest["courts_failed"] == 1 and latest["finished_at"] == T0 + dt.timedelta(minutes=35)
