"""Tests for :class:`tracker.storage_sqlite.SQLiteStorage`.

Every test runs against in-memory SQLite. The named regressions here are the
ones that would quietly corrupt an irreplaceable, forward-only dataset:

* a re-run of collect double-writing a cadence bucket;
* an observation being overwritten instead of skipped;
* a datetime coming back naive and silently reinterpreted as local;
* a court-minute aggregate counting slots instead of summing minutes, which
  makes a 60-minute grid and a 30-minute grid compare equal;
* a coverage chart smoothing over a poll gap instead of showing it;
* a half-written derived table after a failed backfill.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
from collections.abc import Iterator
from typing import Any

import pytest

from tests.conftest import (
    BUSINESS_DAY_START_HOUR,
    PADEL_FORT_COURT,
    PADEL_FORT_VENUE,
    PADEL_UP_COURT,
    PADEL_UP_VENUE,
    TZ,
    SyntheticHistory,
)
from tracker.analytics.occupancy import occupancy_by_venue_day
from tracker.schema import VIEW_NAMES, metadata
from tracker.storage_sqlite import SQLiteStorage
from tracker.types import (
    DiscoveredFacility,
    FacilityDim,
    FacilityFetch,
    FacilityKind,
    SlotFirstBooked,
    SlotObservation,
    SlotState,
    Sport,
    StateTransition,
    VenueDim,
    business_date_for,
    duration_minutes_for,
    from_local_text,
    slot_start_utc_for,
    to_local_text,
)

OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
SLOT_DAY = "2026-09-14"


@pytest.fixture()
def storage() -> Iterator[SQLiteStorage]:
    """A fresh, initialized in-memory backend, typed as the concrete class."""
    backend = SQLiteStorage("sqlite://")
    backend.initialize()
    yield backend
    backend.close()


def _poll_key(position: int) -> str:
    return f"2026-09-11T{10 + position:02d}:00:00Z"


def _observed_at(position: int) -> dt.datetime:
    return OBSERVED_AT + dt.timedelta(hours=position)


def _observation(
    snapshot_id: int,
    slot_uuid: str,
    *,
    start_local: str,
    duration_minutes: int,
    state: SlotState,
    venue_uuid: str = PADEL_FORT_VENUE,
    facility_uuid: str = PADEL_FORT_COURT,
    price: float | None = 900.0,
    is_past: bool = False,
    sport: Sport = Sport.PADEL,
) -> SlotObservation:
    start = from_local_text(start_local)
    end = start + dt.timedelta(minutes=duration_minutes)
    return SlotObservation(
        snapshot_id=snapshot_id,
        slot_uuid=slot_uuid,
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        slot_start_local=start_local,
        slot_end_local=to_local_text(end),
        tz=TZ,
        slot_start_utc=slot_start_utc_for(start, TZ),
        duration_minutes=duration_minutes_for(start, end),
        price=price,
        total_count=1,
        available_count=0 if state is SlotState.BOOKED else 1,
        is_available=state is not SlotState.BLOCKED,
        is_booked=state is SlotState.BOOKED,
        state=state,
        days_ahead=3,
        business_date=business_date_for(start, BUSINESS_DAY_START_HOUR),
        is_past=is_past,
    )


def _transition(slot_uuid: str, to_state: SlotState) -> StateTransition:
    return StateTransition(
        slot_uuid=slot_uuid,
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        from_state=SlotState.OPEN,
        to_state=to_state,
        first_seen_at=OBSERVED_AT,
        prev_seen_at=OBSERVED_AT - dt.timedelta(minutes=30),
        uncertainty_minutes=30,
        slot_start_utc=dt.datetime(2026, 9, 14, 13, 30, tzinfo=dt.UTC),
        days_ahead_at_change=3,
    )


def _first_booked(slot_uuid: str, *, censored_left: bool) -> SlotFirstBooked:
    return SlotFirstBooked(
        slot_uuid=slot_uuid,
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        slot_start_utc=dt.datetime(2026, 9, 14, 13, 30, tzinfo=dt.UTC),
        business_date=dt.date(2026, 9, 14),
        first_booked_at=None if censored_left else OBSERVED_AT,
        last_booked_at=None if censored_left else OBSERVED_AT,
        lead_time_hours=None if censored_left else 72.5,
        uncertainty_minutes=None if censored_left else 30,
        censored_left=censored_left,
        rebooked=False,
        cancelled=False,
    )


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_initialize_is_idempotent(storage: SQLiteStorage) -> None:
    """A second initialize() must not duplicate or drop anything.

    Regression: collect calls initialize() on every run. If that were not
    safe, either startup would fail or -- far worse -- a table would be
    recreated and observations lost.
    """
    first = storage.query_rows(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'view') "
        "ORDER BY type, name"
    )
    storage.initialize()
    storage.initialize()
    second = storage.query_rows(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'view') "
        "ORDER BY type, name"
    )

    assert first == second
    names = [row["name"] for row in second]
    assert len(names) == len(set(names))
    assert set(metadata.tables) <= set(names)
    assert set(VIEW_NAMES) <= set(names)


def test_initialize_preserves_existing_observations(storage: SQLiteStorage) -> None:
    """Re-initializing an existing database must not touch the append-only table."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-keep",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.BOOKED,
            )
        ]
    )

    storage.initialize()

    assert len(storage.observations_for_slots(["slot-keep"])) == 1


# --------------------------------------------------------------------------
# Idempotency -- the core requirement
# --------------------------------------------------------------------------


def test_create_snapshot_returns_the_same_id_for_one_poll_key(storage: SQLiteStorage) -> None:
    """Regression: a retried collect run opening a second snapshot for one
    cadence bucket, which would double every court-minute aggregate."""
    first = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    second = storage.create_snapshot(_poll_key(0), _observed_at(0) + dt.timedelta(minutes=7), 31)

    assert first == second
    assert len(storage.query_rows("SELECT snapshot_id FROM snapshots")) == 1

    stored = storage.get_snapshot_by_poll_key(_poll_key(0))
    assert stored is not None
    # The first call's observed_at wins: the bucket is not re-stamped.
    assert stored.observed_at == _observed_at(0)


def test_appending_the_same_observations_twice_is_a_no_op(storage: SQLiteStorage) -> None:
    """THE idempotency test. Replaying a poll must change nothing and report 0."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    batch = [
        _observation(
            snapshot_id,
            f"slot-{index}",
            start_local=f"{SLOT_DAY} {18 + index}:00:00",
            duration_minutes=30,
            state=SlotState.OPEN,
        )
        for index in range(5)
    ]

    assert storage.append_observations(batch) == 5
    after_first = storage.query_rows("SELECT COUNT(*) AS n FROM slot_observations")[0]["n"]

    assert storage.append_observations(batch) == 0
    after_second = storage.query_rows("SELECT COUNT(*) AS n FROM slot_observations")[0]["n"]

    assert after_first == 5
    assert after_second == 5


def test_partial_replay_inserts_only_the_new_rows(storage: SQLiteStorage) -> None:
    """A poll that failed half way must be resumable without duplicating."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    first_half = [
        _observation(
            snapshot_id,
            f"slot-{index}",
            start_local=f"{SLOT_DAY} 1{index}:00:00",
            duration_minutes=30,
            state=SlotState.OPEN,
        )
        for index in range(3)
    ]
    both_halves = first_half + [
        _observation(
            snapshot_id,
            f"slot-{index}",
            start_local=f"{SLOT_DAY} 1{index}:00:00",
            duration_minutes=30,
            state=SlotState.OPEN,
        )
        for index in range(3, 6)
    ]

    assert storage.append_observations(first_half) == 3
    assert storage.append_observations(both_halves) == 3
    assert storage.query_rows("SELECT COUNT(*) AS n FROM slot_observations")[0]["n"] == 6


def test_unique_snapshot_slot_key_is_enforced_by_the_database(storage: SQLiteStorage) -> None:
    """The append-only guarantee must live in the schema, not just in the INSERT.

    Regression: without a real unique key, a re-run would append a second row
    per slot and every occupancy number would silently double.
    """
    indexes = storage.query_rows(
        "SELECT name, origin, \"unique\" AS is_unique FROM pragma_index_list('slot_observations')"
    )
    primary_keys = [row for row in indexes if row["origin"] == "pk"]
    assert len(primary_keys) == 1
    assert primary_keys[0]["is_unique"] == 1

    key_columns = storage.query_rows(
        "SELECT name FROM pragma_index_info(:index_name) ORDER BY seqno",
        {"index_name": primary_keys[0]["name"]},
    )
    assert [row["name"] for row in key_columns] == ["snapshot_id", "slot_uuid"]


def test_conflicting_observation_is_skipped_not_overwritten(storage: SQLiteStorage) -> None:
    """A conflicting re-append must DO NOTHING, never update.

    Regression: an upsert here would let a later poll rewrite history, and the
    first observation of a slot -- the one lead time is anchored on -- is
    exactly what cannot be recovered.
    """
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    original = _observation(
        snapshot_id,
        "slot-conflict",
        start_local=f"{SLOT_DAY} 19:00:00",
        duration_minutes=30,
        state=SlotState.OPEN,
    )
    storage.append_observations([original])

    mutated = dataclasses.replace(
        original, state=SlotState.BOOKED, is_booked=True, available_count=0
    )
    assert storage.append_observations([mutated]) == 0

    stored = storage.observations_for_slots(["slot-conflict"])
    assert len(stored) == 1
    assert stored[0] == original


def test_facility_fetch_upsert_replaces_the_earlier_attempt(storage: SQLiteStorage) -> None:
    """A retry's outcome must replace the failed attempt, not sit beside it."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    failed = FacilityFetch(
        snapshot_id=snapshot_id,
        facility_uuid=PADEL_FORT_COURT,
        ok=False,
        http_status=502,
        error="bad gateway",
        duration_ms=900,
        slot_count=0,
        attempts=1,
    )
    succeeded = dataclasses.replace(
        failed, ok=True, http_status=200, error=None, slot_count=36, attempts=2
    )

    storage.record_facility_fetch(failed)
    storage.record_facility_fetch(succeeded)

    stored = storage.facility_fetches_for_snapshot(snapshot_id)
    assert stored == [succeeded]


# --------------------------------------------------------------------------
# Round-trip fidelity and timezone discipline
# --------------------------------------------------------------------------


def test_observation_round_trips_field_for_field(storage: SQLiteStorage) -> None:
    """Regression: any lossy column (price, the raw flags, is_past) silently
    changes what the analytics layer computes."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    written = _observation(
        snapshot_id,
        "slot-roundtrip",
        start_local="2026-09-12 00:30:00",
        duration_minutes=30,
        state=SlotState.BOOKED,
        venue_uuid=PADEL_UP_VENUE,
        facility_uuid=PADEL_UP_COURT,
        price=1000.0,
        is_past=True,
    )
    storage.append_observations([written])

    read_back = storage.observations_for_slots(["slot-roundtrip"])[0]

    for field in dataclasses.fields(SlotObservation):
        assert getattr(read_back, field.name) == getattr(written, field.name), field.name
    # business_date rolls back one day for a pre-04:00 slot, and must survive.
    assert read_back.business_date == dt.date(2026, 9, 11)


def test_datetimes_come_back_aware_and_in_utc(storage: SQLiteStorage) -> None:
    """Regression: a naive datetime crossing this boundary gets reinterpreted
    as local time, shifting every lead time by 5h30m."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-tz",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
            )
        ]
    )
    storage.record_facility_fetch(
        FacilityFetch(
            snapshot_id=snapshot_id,
            facility_uuid=PADEL_FORT_COURT,
            ok=True,
            http_status=200,
            error=None,
            duration_ms=350,
            slot_count=1,
            attempts=1,
        )
    )
    storage.replace_derived_transitions([_transition("slot-tz", SlotState.BOOKED)])
    storage.replace_derived_first_booked([_first_booked("slot-tz", censored_left=False)])

    observation = storage.observations_for_slots(["slot-tz"])[0]
    snapshot = storage.latest_snapshot()
    transition = storage.list_transitions()[0]
    booked = storage.list_first_booked()[0]
    assert snapshot is not None

    aware = [
        observation.slot_start_utc,
        snapshot.observed_at,
        transition.first_seen_at,
        transition.prev_seen_at,
        transition.slot_start_utc,
        booked.slot_start_utc,
        booked.first_booked_at,
        booked.last_booked_at,
    ]
    for value in aware:
        assert value is not None
        assert value.tzinfo is not None
        assert value.utcoffset() == dt.timedelta(0)

    # 19:00 IST on 2026-09-14 is 13:30Z; a naive read would report 19:00Z.
    assert observation.slot_start_utc == dt.datetime(2026, 9, 14, 13, 30, tzinfo=dt.UTC)


def test_snapshot_reads_are_ordered_and_bounded(storage: SQLiteStorage) -> None:
    for position in range(4):
        storage.create_snapshot(_poll_key(position), _observed_at(position), 31)

    window = storage.snapshots_between(_observed_at(1), _observed_at(3))
    assert [s.poll_key for s in window] == [_poll_key(1), _poll_key(2)]

    latest = storage.latest_snapshot()
    assert latest is not None
    assert latest.poll_key == _poll_key(3)
    assert storage.get_snapshot_by_poll_key("no-such-bucket") is None


def test_finalize_snapshot_is_last_write_wins_and_spares_observations(
    storage: SQLiteStorage,
) -> None:
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-final",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
            )
        ]
    )

    storage.finalize_snapshot(snapshot_id, False, "timeout", 4000)
    storage.finalize_snapshot(snapshot_id, True, None, 1200)

    stored = storage.get_snapshot_by_poll_key(_poll_key(0))
    assert stored is not None
    assert (stored.ok, stored.error, stored.duration_ms) == (True, None, 1200)
    assert len(storage.observations_for_slots(["slot-final"])) == 1


# --------------------------------------------------------------------------
# The views
# --------------------------------------------------------------------------


def test_v_occupancy_daily_aggregates_court_minutes_not_slot_counts(
    storage: SQLiteStorage,
) -> None:
    """The guard on the entire normalization story.

    One 60-minute BOOKED slot against two 30-minute OPEN slots:

    * summing court-minutes gives 60 / (60 + 60) = 0.5  <- correct
    * counting slots gives      1  / (1 + 2)   = 0.333  <- wrong

    Regression: Padel Up sells a 60-minute grid and the other two sell 30, so
    any slot-count aggregate makes an hour of booked time look like half an
    hour and ranks the venues wrongly.
    """
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-60-booked",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=60,
                state=SlotState.BOOKED,
            ),
            _observation(
                snapshot_id,
                "slot-30-open-a",
                start_local=f"{SLOT_DAY} 20:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
            ),
            _observation(
                snapshot_id,
                "slot-30-open-b",
                start_local=f"{SLOT_DAY} 20:30:00",
                duration_minutes=30,
                state=SlotState.OPEN,
            ),
        ]
    )

    rows = storage.query_rows(
        "SELECT * FROM v_occupancy_daily WHERE business_date = :business_date",
        {"business_date": SLOT_DAY},
    )
    assert len(rows) == 1
    row = rows[0]

    assert row["booked_minutes"] == 60
    assert row["open_minutes"] == 60
    assert row["sellable_minutes"] == 120
    assert row["occupancy_strict"] == pytest.approx(0.5)
    assert row["occupancy_strict"] != pytest.approx(1 / 3)


def test_v_occupancy_daily_keeps_blocked_minutes_out_of_the_strict_numerator(
    storage: SQLiteStorage,
) -> None:
    """A blocked evening must show up as blocked, not as demand and not as empty.

    Regression: folding BLOCKED into occupancy_strict makes a venue that sells
    offline look fully booked; dropping it entirely makes it look dead.
    """
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-blocked",
                start_local=f"{SLOT_DAY} 17:00:00",
                duration_minutes=30,
                state=SlotState.BLOCKED,
            ),
            _observation(
                snapshot_id,
                "slot-open",
                start_local=f"{SLOT_DAY} 17:30:00",
                duration_minutes=30,
                state=SlotState.OPEN,
            ),
            _observation(
                snapshot_id,
                "slot-booked",
                start_local=f"{SLOT_DAY} 18:00:00",
                duration_minutes=30,
                state=SlotState.BOOKED,
            ),
        ]
    )

    row = storage.query_rows("SELECT * FROM v_occupancy_daily")[0]

    assert row["blocked_minutes"] == 30
    assert row["total_minutes"] == 90
    assert row["sellable_minutes"] == 60
    assert row["occupancy_strict"] == pytest.approx(0.5)  # 30 / (30 + 30)
    assert row["occupancy_gross"] == pytest.approx(60 / 90)  # (booked + blocked) / total
    assert row["blocked_share"] == pytest.approx(30 / 90)


def test_occupancy_ratios_are_null_when_nothing_is_sellable(storage: SQLiteStorage) -> None:
    """A wholly blocked day must report NULL, never 0% and never a div-by-zero."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-all-blocked",
                start_local=f"{SLOT_DAY} 17:00:00",
                duration_minutes=30,
                state=SlotState.BLOCKED,
            )
        ]
    )

    row = storage.query_rows("SELECT * FROM v_occupancy_daily")[0]

    assert row["occupancy_strict"] is None
    assert row["blocked_minutes"] == 30
    assert row["occupancy_gross"] == pytest.approx(1.0)


def test_v_slot_settled_collapses_repeated_polls_to_one_row_per_slot(
    storage: SQLiteStorage,
) -> None:
    """Regression: summing duration_minutes off slot_observations multiplies
    every slot by the number of polls that saw it -- ~48x per day."""
    slot_uuid = "slot-repolled"
    for position in range(3):
        snapshot_id = storage.create_snapshot(_poll_key(position), _observed_at(position), 31)
        state = SlotState.BOOKED if position == 2 else SlotState.OPEN
        storage.append_observations(
            [
                _observation(
                    snapshot_id,
                    slot_uuid,
                    start_local=f"{SLOT_DAY} 19:00:00",
                    duration_minutes=30,
                    state=state,
                )
            ]
        )

    assert len(storage.observations_for_slots([slot_uuid])) == 3

    latest = storage.query_rows("SELECT slot_uuid, state FROM v_slot_settled")
    assert latest == [{"slot_uuid": slot_uuid, "state": "BOOKED"}]

    minutes = storage.query_rows(
        "SELECT state, court_minutes, slots FROM v_court_minutes_daily ORDER BY state"
    )
    assert minutes == [{"state": "BOOKED", "court_minutes": 30, "slots": 1}]


def test_v_court_minutes_daily_tracks_past_minutes_and_price_separately(
    storage: SQLiteStorage,
) -> None:
    """is_past is orthogonal to state: an elapsed unsold slot is still OPEN
    inventory and still belongs in the denominator."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-elapsed",
                start_local=f"{SLOT_DAY} 07:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
                is_past=True,
            ),
            _observation(
                snapshot_id,
                "slot-future",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
                is_past=False,
            ),
        ]
    )

    row = storage.query_rows(
        "SELECT court_minutes, past_court_minutes, slot_price_total "
        "FROM v_court_minutes_daily WHERE state = 'OPEN'"
    )[0]

    assert row["court_minutes"] == 60
    assert row["past_court_minutes"] == 30
    assert float(row["slot_price_total"]) == pytest.approx(1800.0)


def test_v_coverage_daily_reports_a_missing_snapshot_as_a_gap(storage: SQLiteStorage) -> None:
    """Regression: a coverage chart that interpolates across a poll gap hides
    exactly the outages that make an occupancy series untrustworthy."""
    backend = SQLiteStorage("sqlite://", expected_snapshots_per_day=4)
    backend.initialize()
    try:
        # Three of four expected polls land; the fourth never ran.
        for position in range(3):
            snapshot_id = backend.create_snapshot(_poll_key(position), _observed_at(position), 31)
            backend.record_facility_fetch(
                FacilityFetch(
                    snapshot_id=snapshot_id,
                    facility_uuid=PADEL_FORT_COURT,
                    ok=position != 1,
                    http_status=200 if position != 1 else 503,
                    error=None if position != 1 else "unavailable",
                    duration_ms=350,
                    slot_count=36 if position != 1 else 0,
                    attempts=1,
                )
            )

        rows = backend.query_rows("SELECT * FROM v_coverage_daily")
        assert len(rows) == 1
        row = rows[0]

        assert row["observed_date"] == "2026-09-11"
        assert row["snapshots_expected"] == 4
        assert row["snapshots_received"] == 3
        assert row["fetches_ok"] == 2
        assert row["fetches_failed"] == 1
        assert row["coverage_ratio"] == pytest.approx(0.75)
    finally:
        backend.close()


# --------------------------------------------------------------------------
# Streaming reads
# --------------------------------------------------------------------------


def test_iter_observations_is_a_generator(storage: SQLiteStorage) -> None:
    """Regression: materializing this result set does not fit in memory once
    the table reaches tens of millions of rows."""
    assert inspect.isgeneratorfunction(SQLiteStorage.iter_observations)
    assert inspect.isgenerator(storage.iter_observations())


def test_iter_observations_honours_every_filter(storage: SQLiteStorage) -> None:
    snapshot_one = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    snapshot_two = storage.create_snapshot(_poll_key(1), _observed_at(1), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_one,
                "fort-booked",
                start_local="2026-09-14 19:00:00",
                duration_minutes=30,
                state=SlotState.BOOKED,
            ),
            _observation(
                snapshot_one,
                "fort-elapsed",
                start_local="2026-09-14 07:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
                is_past=True,
            ),
            _observation(
                snapshot_two,
                "up-open",
                start_local="2026-09-16 19:00:00",
                duration_minutes=60,
                state=SlotState.OPEN,
                venue_uuid=PADEL_UP_VENUE,
                facility_uuid=PADEL_UP_COURT,
            ),
        ]
    )

    def uuids(**kwargs: object) -> list[str]:
        return [o.slot_uuid for o in storage.iter_observations(**kwargs)]  # type: ignore[arg-type]

    assert uuids() == ["fort-booked", "fort-elapsed", "up-open"]
    assert uuids(venue_uuid=PADEL_UP_VENUE) == ["up-open"]
    assert uuids(facility_uuid=PADEL_FORT_COURT) == ["fort-booked", "fort-elapsed"]
    assert uuids(snapshot_id=snapshot_two) == ["up-open"]
    assert uuids(state=SlotState.BOOKED) == ["fort-booked"]
    assert uuids(include_past=False) == ["fort-booked", "up-open"]
    assert uuids(business_date_from=dt.date(2026, 9, 16)) == ["up-open"]
    assert uuids(business_date_to=dt.date(2026, 9, 14)) == ["fort-booked", "fort-elapsed"]
    assert uuids(
        business_date_from=dt.date(2026, 9, 14), business_date_to=dt.date(2026, 9, 14)
    ) == ["fort-booked", "fort-elapsed"]


# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


def test_upsert_venue_dim_preserves_first_seen_and_advances_last_seen(
    storage: SQLiteStorage,
) -> None:
    """Regression: overwriting first_seen makes "when did this venue appear?"
    unanswerable, which is the whole point of keeping a dimension table."""
    original = VenueDim(
        venue_uuid=PADEL_FORT_VENUE,
        name="Padel Fort",
        short_name="padel_fort",
        slug="padel-fort",
        numeric_id="155289",
        tz=TZ,
        active=True,
        first_seen=_observed_at(0),
        last_seen=_observed_at(0),
    )
    storage.upsert_venue_dim(original)

    renamed = dataclasses.replace(
        original,
        name="Padel Fort | Jaipur",
        first_seen=_observed_at(5),
        last_seen=_observed_at(5),
    )
    storage.upsert_venue_dim(renamed)

    stored = storage.get_venue_dim(PADEL_FORT_VENUE)
    assert stored is not None
    assert stored.first_seen == _observed_at(0)
    assert stored.last_seen == _observed_at(5)
    assert stored.name == "Padel Fort | Jaipur"
    assert storage.list_venue_dims() == [stored]
    assert storage.get_venue_dim("no-such-venue") is None


def test_upsert_facility_dim_preserves_first_seen(storage: SQLiteStorage) -> None:
    storage.upsert_venue_dim(
        VenueDim(
            venue_uuid=PADEL_FORT_VENUE,
            name="Padel Fort",
            short_name="padel_fort",
            slug="padel-fort",
            numeric_id="155289",
            tz=TZ,
            active=True,
            first_seen=_observed_at(0),
            last_seen=_observed_at(0),
        )
    )
    court = FacilityDim(
        facility_uuid=PADEL_FORT_COURT,
        venue_uuid=PADEL_FORT_VENUE,
        name="Padel Court",
        kind=FacilityKind.COURT,
        sport=Sport.PADEL,
        grid_minutes=30,
        active=True,
        first_seen=_observed_at(0),
        last_seen=_observed_at(0),
    )
    storage.upsert_facility_dim(court)
    storage.upsert_facility_dim(
        dataclasses.replace(court, name="Padel Court 1", first_seen=_observed_at(3))
    )

    stored = storage.list_facility_dims()
    assert len(stored) == 1
    assert stored[0].first_seen == _observed_at(0)
    assert stored[0].name == "Padel Court 1"
    assert stored[0].kind is FacilityKind.COURT
    assert stored[0].sport is Sport.PADEL


def test_name_history_and_discovery_log_are_append_only(storage: SQLiteStorage) -> None:
    storage.append_venue_name_change(
        PADEL_FORT_VENUE, _observed_at(0), "Play Padel | Clarks Amer Hotel", "Play Padel"
    )
    storage.append_venue_name_change(
        PADEL_FORT_VENUE, _observed_at(1), "Play Padel", "Play Padel 2"
    )

    history = storage.query_rows(
        "SELECT old_name, new_name FROM venue_name_history ORDER BY observed_at"
    )
    assert [row["new_name"] for row in history] == ["Play Padel", "Play Padel 2"]

    entries = [
        DiscoveredFacility(
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            facility_name="Padel Court",
            activity_id=44,
            activity_name="Padel",
            in_config=True,
            suggested_kind=FacilityKind.COURT,
        ),
        DiscoveredFacility(
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid="1f332de3-43e9-4e44-b789-3b216c2dd46a",
            facility_name="Padel Racket",
            activity_id=44,
            activity_name="Padel",
            in_config=False,
            suggested_kind=FacilityKind.EQUIPMENT,
        ),
    ]
    assert storage.append_discovery_log(_observed_at(0), entries) == 2
    assert storage.append_discovery_log(_observed_at(1), entries) == 2
    assert storage.append_discovery_log(_observed_at(2), []) == 0
    assert storage.query_rows("SELECT COUNT(*) AS n FROM facility_discovery_log")[0]["n"] == 4
    # The log never mutates the dimension it audits.
    assert storage.list_facility_dims() == []


# --------------------------------------------------------------------------
# Derived tables
# --------------------------------------------------------------------------


def test_replace_derived_transitions_swaps_the_whole_table(storage: SQLiteStorage) -> None:
    assert storage.replace_derived_transitions([_transition("slot-a", SlotState.BOOKED)]) == 1
    assert (
        storage.replace_derived_transitions(
            [
                _transition("slot-b", SlotState.BOOKED),
                _transition("slot-c", SlotState.BLOCKED),
            ]
        )
        == 2
    )

    assert [t.slot_uuid for t in storage.list_transitions()] == ["slot-b", "slot-c"]
    assert [t.slot_uuid for t in storage.list_transitions(to_state=SlotState.BLOCKED)] == ["slot-c"]
    assert storage.list_transitions(facility_uuid="no-such-court") == []


def test_replace_derived_transitions_rolls_back_a_failed_backfill(storage: SQLiteStorage) -> None:
    """Regression: delete-then-insert without a transaction leaves the derived
    table empty when the producer raises, and an empty table reads as
    "nothing was ever booked" rather than as a failure."""
    storage.replace_derived_transitions(
        [
            _transition("slot-a", SlotState.BOOKED),
            _transition("slot-b", SlotState.BOOKED),
        ]
    )

    def failing() -> Iterator[StateTransition]:
        yield _transition("slot-c", SlotState.BOOKED)
        raise RuntimeError("derivation blew up half way")

    with pytest.raises(RuntimeError, match="blew up"):
        storage.replace_derived_transitions(failing())

    assert [t.slot_uuid for t in storage.list_transitions()] == ["slot-a", "slot-b"]


def test_replace_derived_first_booked_rolls_back_a_failed_backfill(
    storage: SQLiteStorage,
) -> None:
    storage.replace_derived_first_booked(
        [
            _first_booked("slot-a", censored_left=False),
            _first_booked("slot-b", censored_left=True),
        ]
    )

    def failing() -> Iterator[SlotFirstBooked]:
        yield _first_booked("slot-c", censored_left=False)
        raise RuntimeError("derivation blew up half way")

    with pytest.raises(RuntimeError, match="blew up"):
        storage.replace_derived_first_booked(failing())

    assert [row.slot_uuid for row in storage.list_first_booked()] == ["slot-a", "slot-b"]


def test_list_first_booked_can_exclude_left_censored_slots(storage: SQLiteStorage) -> None:
    """Regression: a left-censored slot was already BOOKED at first sight, so
    counting it as a zero-lead booking drags every percentile down."""
    storage.replace_derived_first_booked(
        [
            _first_booked("slot-observed", censored_left=False),
            _first_booked("slot-censored", censored_left=True),
        ]
    )

    assert len(storage.list_first_booked()) == 2
    remaining = storage.list_first_booked(exclude_censored=True)
    assert [row.slot_uuid for row in remaining] == ["slot-observed"]
    assert remaining[0].lead_time_hours == pytest.approx(72.5)
    assert remaining[0].uncertainty_minutes == 30


# --------------------------------------------------------------------------
# query_rows
# --------------------------------------------------------------------------


def test_query_rows_is_parameterized_and_read_only(storage: SQLiteStorage) -> None:
    """Regression: an unparameterized or writable escape hatch here is the one
    way a read path could damage the append-only table."""
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-query",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.BOOKED,
            )
        ]
    )

    rows = storage.query_rows(
        "SELECT slot_uuid FROM slot_observations WHERE state = :state",
        {"state": SlotState.BOOKED.value},
    )
    assert rows == [{"slot_uuid": "slot-query"}]
    assert isinstance(rows[0], dict)

    for statement in (
        "DELETE FROM slot_observations",
        "UPDATE slot_observations SET state = 'OPEN'",
        "DROP TABLE slot_observations",
    ):
        with pytest.raises(ValueError, match="SELECT"):
            storage.query_rows(statement)

    assert storage.query_rows("SELECT COUNT(*) AS n FROM slot_observations")[0]["n"] == 1


# --------------------------------------------------------------------------
# The scripted multi-snapshot history, end to end
# --------------------------------------------------------------------------


def test_scripted_history_lands_intact(synthetic_history_storage: SyntheticHistory) -> None:
    """A ten-snapshot history must survive the round trip unchanged."""
    backend = synthetic_history_storage.storage
    assert backend is not None

    assert len(list(backend.iter_observations())) == len(synthetic_history_storage.observations)

    slot_uuid = synthetic_history_storage.normal_slot_uuid
    trajectory = [o.state for o in backend.observations_for_slots([slot_uuid])]
    assert trajectory == synthetic_history_storage.trajectory(slot_uuid)

    first_booked = next(
        o for o in backend.observations_for_slots([slot_uuid]) if o.state is SlotState.BOOKED
    )
    booking_snapshot = next(
        s for s in synthetic_history_storage.snapshots if s.snapshot_id == first_booked.snapshot_id
    )
    assert booking_snapshot.observed_at == synthetic_history_storage.expected_first_booked_at


def test_scripted_history_normalizes_across_the_two_grids(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """The 60-min and 30-min slots share a wall-clock hour and a business date.
    A slot count makes them equal (1 == 1); court-minutes keep them 60 vs 30."""
    backend = synthetic_history_storage.storage
    assert backend is not None
    sixty, thirty = synthetic_history_storage.normalization_slot_uuids

    rows = backend.query_rows(
        "SELECT venue_uuid, court_minutes, slots FROM v_court_minutes_daily "
        "WHERE business_date = :business_date AND state = 'OPEN' "
        "ORDER BY court_minutes DESC",
        {"business_date": synthetic_history_storage.normalization_business_date.isoformat()},
    )
    minutes = [row["court_minutes"] for row in rows]
    assert minutes == list(synthetic_history_storage.expected_normalization_minutes)
    assert [row["slots"] for row in rows] == [1, 1]
    assert {sixty, thirty} == {
        o.slot_uuid
        for o in backend.iter_observations(
            business_date_from=synthetic_history_storage.normalization_business_date,
            business_date_to=synthetic_history_storage.normalization_business_date,
        )
    }


def test_scripted_history_reports_the_blocked_evening_in_full(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Padel Fort's real 2026-09-13 evening: 14 slots pulled from inventory,
    zero bookings. It must read as blocked minutes, not as an empty evening."""
    backend = synthetic_history_storage.storage
    assert backend is not None

    row = backend.query_rows(
        "SELECT * FROM v_occupancy_daily WHERE business_date = :business_date",
        {"business_date": synthetic_history_storage.blocked_evening_business_date.isoformat()},
    )[0]

    assert row["blocked_minutes"] == synthetic_history_storage.expected_blocked_evening_minutes
    assert row["booked_minutes"] == 0
    assert row["occupancy_strict"] is None
    assert row["blocked_share"] == pytest.approx(1.0)


def test_scripted_history_attributes_a_post_midnight_sale_to_the_previous_day(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """00:30 on Saturday is Friday-night demand. Aggregating on the raw local
    date misattributes it to the wrong trading day and day of week."""
    backend = synthetic_history_storage.storage
    assert backend is not None
    slot_uuid = synthetic_history_storage.post_midnight_slot_uuid

    observation = backend.observations_for_slots([slot_uuid])[0]
    assert observation.business_date == synthetic_history_storage.post_midnight_business_date
    assert observation.slot_start_local.startswith(
        synthetic_history_storage.post_midnight_local_date.isoformat()
    )

    streamed = {
        o.slot_uuid
        for o in backend.iter_observations(
            business_date_from=synthetic_history_storage.post_midnight_business_date,
            business_date_to=synthetic_history_storage.post_midnight_business_date,
        )
    }
    assert slot_uuid in streamed


def test_scripted_history_coverage_shows_the_ninety_minute_hole(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Two scheduled polls never ran. Coverage must say 10 received, not 12."""
    backend = synthetic_history_storage.storage
    assert backend is not None

    rows = backend.query_rows(
        "SELECT facility_uuid, snapshots_received FROM v_coverage_daily ORDER BY facility_uuid"
    )
    assert rows
    for row in rows:
        assert row["snapshots_received"] == synthetic_history_storage.snapshot_count

    window = backend.snapshots_between(
        synthetic_history_storage.gap_start + dt.timedelta(minutes=1),
        synthetic_history_storage.gap_end,
    )
    assert window == []


# --------------------------------------------------------------------------
# One definition of "the row for this slot", in SQL and in Python
# --------------------------------------------------------------------------


def _post_start_history(
    storage: SQLiteStorage, slot_uuid: str, states: list[tuple[SlotState, bool]]
) -> None:
    """Write one slot's trajectory, each entry a (state, is_past) pair."""
    for position, (state, is_past) in enumerate(states):
        snapshot_id = storage.create_snapshot(_poll_key(position), _observed_at(position), 31)
        storage.append_observations(
            [
                _observation(
                    snapshot_id,
                    slot_uuid,
                    start_local=f"{SLOT_DAY} 19:00:00",
                    duration_minutes=30,
                    state=state,
                    is_past=is_past,
                )
            ]
        )


@pytest.mark.parametrize(
    ("states", "expected_state", "expected_strict"),
    [
        # Booked only after it elapsed: the slot never sold while it was
        # sellable, so it settled OPEN.
        ([(SlotState.OPEN, False), (SlotState.OPEN, False), (SlotState.BOOKED, True)], "OPEN", 0.0),
        # Released only after it elapsed: it was sold when it mattered.
        (
            [(SlotState.BOOKED, False), (SlotState.BOOKED, False), (SlotState.OPEN, True)],
            "BOOKED",
            1.0,
        ),
    ],
)
def test_sql_and_python_settle_a_post_start_change_the_same_way(
    storage: SQLiteStorage,
    states: list[tuple[SlotState, bool]],
    expected_state: str,
    expected_strict: float,
) -> None:
    """Regression: two definitions of "the row for this slot", 0% vs 100%.

    Hudle never marks an elapsed slot unavailable and keeps republishing it, so
    a late cancellation, an unblocking or a grid republish rewrites a slot's
    last observation *after* it has already been played. Picking the highest
    snapshot id -- which ``v_slot_settled`` used to do -- then disagrees with
    ``settled_observations``, which picks the last observation taken before the
    slot started. On this three-poll trajectory the two printed 0.0 and 1.0 for
    the same slot out of the same rows, so whether the dashboard read SQL or
    Python decided the headline number.
    """
    slot_uuid = "slot-post-start-change"
    _post_start_history(storage, slot_uuid, states)

    settled = storage.query_rows("SELECT slot_uuid, state, is_past FROM v_slot_settled")
    assert settled == [{"slot_uuid": slot_uuid, "state": expected_state, "is_past": 0}]

    sql_row = storage.query_rows("SELECT * FROM v_occupancy_daily")[0]
    python_rows = occupancy_by_venue_day(storage.iter_observations())
    assert len(python_rows) == 1
    python_row = python_rows[0]

    assert sql_row["occupancy_strict"] == pytest.approx(expected_strict)
    assert python_row.occupancy_strict == pytest.approx(expected_strict)
    assert sql_row["booked_minutes"] == python_row.booked_minutes
    assert sql_row["open_minutes"] == python_row.open_minutes
    assert sql_row["blocked_minutes"] == python_row.blocked_minutes


def test_settled_view_keeps_a_slot_that_was_only_ever_seen_elapsed(
    storage: SQLiteStorage,
) -> None:
    """Regression: dropping slots with no pre-start observation at all.

    A slot that had already started the first time collection reached it has no
    pre-start row, and an inner join on "last pre-start observation" would drop
    it from the view entirely -- silently shrinking the denominator of every
    retrospective day. It falls back to the earliest sighting instead, matching
    ``settled_observations``.
    """
    slot_uuid = "slot-elapsed-only"
    _post_start_history(
        storage,
        slot_uuid,
        [(SlotState.OPEN, True), (SlotState.OPEN, True), (SlotState.BOOKED, True)],
    )

    settled = storage.query_rows("SELECT slot_uuid, state FROM v_slot_settled")
    assert settled == [{"slot_uuid": slot_uuid, "state": "OPEN"}]

    python_rows = occupancy_by_venue_day(storage.iter_observations())
    assert python_rows[0].open_minutes == 30
    assert python_rows[0].booked_minutes == 0


def test_iter_observations_and_the_views_separate_the_two_sports(
    storage: SQLiteStorage,
) -> None:
    """Regression: a venue's pickleball courts inflating its padel numbers.

    Padel Fort polls one padel court and two pickleball courts, all under one
    venue_uuid. Without a sport on the observation its padel occupancy is
    blended with pickleball and its share of listed court-hours inflates by
    half again. The column has to be written at collect time -- it cannot be
    reconstructed later from a dataset that does not carry it.
    """
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-padel",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.BOOKED,
                sport=Sport.PADEL,
            ),
            _observation(
                snapshot_id,
                "slot-pickleball",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
                facility_uuid="pickleball-court-1",
                sport=Sport.PICKLEBALL,
            ),
        ]
    )

    padel = list(storage.iter_observations(sport=Sport.PADEL))
    assert [o.slot_uuid for o in padel] == ["slot-padel"]
    assert len(list(storage.iter_observations())) == 2

    # The Python aggregate keyed on venue_uuid alone would blend them; with the
    # sport filter the padel headline is a padel headline.
    blended = occupancy_by_venue_day(storage.iter_observations())[0]
    padel_only = occupancy_by_venue_day(storage.iter_observations(), sport=Sport.PADEL)[0]
    assert blended.occupancy_strict == pytest.approx(0.5)
    assert padel_only.occupancy_strict == pytest.approx(1.0)

    # And the SQL side can split them too, because sport is a grouping key.
    by_sport = {
        row["sport"]: row["occupancy_strict"]
        for row in storage.query_rows("SELECT sport, occupancy_strict FROM v_occupancy_daily")
    }
    assert by_sport == {"padel": pytest.approx(1.0), "pickleball": pytest.approx(0.0)}


def test_court_minutes_view_exposes_price_per_court_hour(storage: SQLiteStorage) -> None:
    """Regression: the one-line SQL query that inverts the price ranking.

    Play Padel's 1000 per 30-minute slot is 2000 per court-hour, the most
    expensive padel court in the city; Padel Up's 1800 per 60-minute slot is
    1800, joint cheapest. ``slot_price_total / slots`` ranks them exactly
    backwards, so the view has to carry the per-court-hour figure itself.
    """
    snapshot_id = storage.create_snapshot(_poll_key(0), _observed_at(0), 31)
    storage.append_observations(
        [
            _observation(
                snapshot_id,
                "slot-play",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=30,
                state=SlotState.OPEN,
                facility_uuid="play-padel-court",
                price=1000.0,
            ),
            _observation(
                snapshot_id,
                "slot-up",
                start_local=f"{SLOT_DAY} 19:00:00",
                duration_minutes=60,
                state=SlotState.OPEN,
                facility_uuid="padel-up-court",
                price=1800.0,
            ),
        ]
    )

    rows = {
        row["facility_uuid"]: row
        for row in storage.query_rows(
            "SELECT facility_uuid, slots, slot_price_total, price_per_court_hour "
            "FROM v_court_minutes_daily"
        )
    }

    assert rows["play-padel-court"]["price_per_court_hour"] == pytest.approx(2000.0)
    assert rows["padel-up-court"]["price_per_court_hour"] == pytest.approx(1800.0)
    # The trap the column exists to close: per-slot ranking says the opposite.
    per_slot = {uuid: row["slot_price_total"] / row["slots"] for uuid, row in rows.items()}
    assert per_slot["play-padel-court"] < per_slot["padel-up-court"]


def test_initialize_adds_columns_missing_from_an_older_database() -> None:
    """Regression: a column added to the schema never reaching a database that
    already exists, so every deployed collector silently stores NULL forever.

    create_all only creates absent tables. The live database predates the
    upstream timestamp columns; initialize() must add them in place and leave
    the rows that predate them reading None.
    """
    storage = SQLiteStorage("sqlite://")
    storage.initialize()
    with storage._engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE slot_observations DROP COLUMN upstream_updated_at")
        conn.exec_driver_sql("ALTER TABLE slot_observations DROP COLUMN upstream_created_at")
        before = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(slot_observations)")}
    assert "upstream_updated_at" not in before

    storage.initialize()
    storage.initialize()  # and again: the migration must be idempotent

    with storage._engine.connect() as conn:
        after = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(slot_observations)")}
    assert {"upstream_updated_at", "upstream_created_at"} <= after
    storage.close()


def test_key_observations_preserve_every_rule_the_analytics_use(
    synthetic_history_storage: Any,
) -> None:
    """Regression: the reduced read dropping a row an analytics rule can see.

    A slot is re-observed on every poll that still covers it, so the unreduced
    stream is ~1000 identical rows per slot and a page load materialised
    millions of them. iter_key_observations keeps only what the rules can tell
    apart -- this pins that claim to the rules themselves, not to a row count.
    """
    from tracker.analytics.occupancy import occupancy_by_venue_day, settled_observations
    from tracker.analytics.transitions import derive_transitions

    storage = synthetic_history_storage.storage
    assert storage is not None
    full = list(storage.iter_observations())
    key = list(storage.iter_key_observations())

    assert len(key) < len(full), "the reduction must actually drop rows"

    def occupancy(rows: Any) -> Any:
        return {
            (r.venue_uuid, r.business_date): (
                r.occupancy_strict,
                r.booked_court_hours,
                r.blocked_court_hours,
            )
            for r in occupancy_by_venue_day(rows)
        }

    assert occupancy(full) == occupancy(key)

    snapshots = storage.snapshots_between(
        dt.datetime(1970, 1, 1, tzinfo=dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC)
    )

    def transitions(rows: Any) -> Any:
        return sorted(
            (
                t.slot_uuid,
                str(t.from_state),
                str(t.to_state),
                t.first_seen_at,
                t.uncertainty_minutes,
            )
            for t in derive_transitions(rows, snapshots)
        )

    # uncertainty_minutes is the point: keeping only change rows would widen it
    # to the gap between *changes* rather than the true poll gap.
    assert transitions(full) == transitions(key)
    assert [o.slot_uuid for o in settled_observations(full)] == [
        o.slot_uuid for o in settled_observations(key)
    ]


def test_initialize_adds_indexes_missing_from_an_older_database() -> None:
    """Regression: an index added to the schema never reaching a live database.

    create_all builds indexes only beside a table it is creating, so the only
    symptom is a query that scans instead of seeks -- silent until the table is
    large enough to make a page unusable, which is how it was found.
    """
    storage = SQLiteStorage("sqlite://")
    storage.initialize()
    with storage._engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_slot_obs_business_date_slot_snapshot")
        gone = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='slot_observations'"
            )
        }
    assert "ix_slot_obs_business_date_slot_snapshot" not in gone

    storage.initialize()
    storage.initialize()  # and again: creating an index must be idempotent

    with storage._engine.connect() as conn:
        back = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='slot_observations'"
            )
        }
    assert "ix_slot_obs_business_date_slot_snapshot" in back
    storage.close()
