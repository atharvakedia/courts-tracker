"""Tests for the collect cycle, the derived-table backfill, logging and the CLI.

No network and no wall clock. Every cycle is driven by a fake client serving the
recorded fixtures and an explicit ``now``, so each assertion below is a fixed
number rather than whatever Hudle happens to be doing.

The recorded totals every test measures against:

    Padel Up    589 slots, 60-min grid,  0 booked / 31 blocked /  558 open
    Play Padel 1240 slots, 30-min grid, 21 booked /  0 blocked / 1219 open
    Padel Fort 1116 slots, 30-min grid,  6 booked / 18 blocked / 1092 open
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import logging
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    PADEL_FORT_COURT,
    PADEL_UP_COURT,
    PLAY_PADEL_COURT,
    PROJECT_ROOT,
    SyntheticHistory,
)
from tracker import __main__ as cli
from tracker.collect import (
    ConsecutiveFailureTracker,
    ExitCode,
    FacilityOutcome,
    backfill_derived,
    floor_to_cadence,
    horizon_dates,
    poll_key_for,
    run_collect,
    slot_counts_by_state,
)
from tracker.config import Config
from tracker.hudle import CircuitOpenError, HudleApiError, HudleHttpError
from tracker.logging_setup import (
    ConsoleFormatter,
    JsonFormatter,
    LogFormat,
    configure_logging,
    event_fields,
)
from tracker.storage import Storage
from tracker.types import SlotState, Sport, StateTransition

# 16:15 IST on 2026-09-11, the local day the fixtures start on.
NOW = dt.datetime(2026, 9, 11, 10, 45, tzinfo=dt.UTC)
EXPECTED_POLL_KEY = "2026-09-11T10:30:00Z"

# The three unprobed pickleball courts. They are active in config.yaml, so a
# cycle attempts them; the fake serves them an empty grid.
PLAY_PICKLEBALL_COURT = "e425e5e7-fd5e-4aa5-98a5-8c9d2a5ddda8"
FORT_PICKLEBALL_COURT_1 = "d8452c5f-a340-45a9-9123-995edd038bf4"
FORT_PICKLEBALL_COURT_2 = "2c33a9b6-f9f9-4bf1-82e0-f5bc9f8f65dd"
PICKLEBALL_COURTS = (PLAY_PICKLEBALL_COURT, FORT_PICKLEBALL_COURT_1, FORT_PICKLEBALL_COURT_2)

ACTIVE_COURT_COUNT = 6
TOTAL_SLOTS = 589 + 1240 + 1116
TOTAL_BOOKED = 0 + 21 + 6
TOTAL_BLOCKED = 31 + 0 + 18
TOTAL_OPEN = 558 + 1219 + 1092
# 31 Padel Up slots are 60 minutes and 18 Padel Fort slots are 30, so blocked
# court-minutes equal neither 49*30 nor 49*60. This is the normalization guard.
TOTAL_BLOCKED_MINUTES = 31 * 60 + 18 * 30
TOTAL_BOOKED_MINUTES = 21 * 30 + 6 * 30

EMPTY_GRID: dict[str, Any] = {
    "success": True,
    "code": 200,
    "data": {"slot_timings": [], "slot_data": []},
}


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeCircuit:
    """The breaker surface the collector reads, with a manual trip."""

    def __init__(self, *, is_open: bool = False) -> None:
        self._is_open = is_open
        self.consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    def trip(self) -> None:
        self._is_open = True
        self.consecutive_failures = 5


class FakeSlotsClient:
    """Serves recorded grids per facility, or raises what it was told to raise."""

    def __init__(
        self,
        payloads: Mapping[str, Any],
        *,
        failures: Mapping[str, BaseException] | None = None,
        trips_circuit: Sequence[str] = (),
    ) -> None:
        self._payloads = dict(payloads)
        self._failures = dict(failures or {})
        self._trips_circuit = set(trips_circuit)
        self.circuit = FakeCircuit()
        self.requests: list[tuple[str, str, dt.date, dt.date]] = []

    def fetch_slots(
        self,
        venue_uuid: str,
        facility_uuid: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> dict[str, Any]:
        self.requests.append((venue_uuid, facility_uuid, start_date, end_date))
        if facility_uuid in self._trips_circuit:
            self.circuit.trip()
            raise CircuitOpenError(5, 5)
        failure = self._failures.get(facility_uuid)
        if failure is not None:
            raise failure
        payload = self._payloads.get(facility_uuid, EMPTY_GRID)
        return dict(payload)


class StorageSpy:
    """Delegates to a real Storage, recording every method it is asked for.

    Used to prove ``--dry-run`` writes nothing. Asserting on row counts alone
    would not: a dry run that opened a snapshot row would still leave the
    cadence bucket claimed and make the real poll for that minute a no-op.
    """

    WRITE_METHODS = frozenset(
        {
            "create_snapshot",
            "finalize_snapshot",
            "append_observations",
            "record_facility_fetch",
            "upsert_venue_dim",
            "upsert_facility_dim",
            "append_venue_name_change",
            "append_discovery_log",
            "replace_derived_transitions",
            "replace_derived_first_booked",
            "initialize",
        }
    )

    def __init__(self, inner: Storage) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recorder(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return recorder

    @property
    def writes(self) -> list[str]:
        return [name for name in self.calls if name in self.WRITE_METHODS]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def grids(
    raw_slots_padel_up: Any, raw_slots_play_padel: Any, raw_slots_padel_fort: Any
) -> dict[str, Any]:
    """The three recorded padel grids, keyed by facility uuid."""
    return {
        PADEL_UP_COURT: raw_slots_padel_up,
        PLAY_PADEL_COURT: raw_slots_play_padel,
        PADEL_FORT_COURT: raw_slots_padel_fort,
    }


def row_count(storage: Storage, table: str) -> int:
    return int(storage.query_rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def outcome_for(outcomes: Sequence[FacilityOutcome], facility_uuid: str) -> FacilityOutcome:
    return next(o for o in outcomes if o.facility_uuid == facility_uuid)


def facility_events(records: Sequence[logging.LogRecord]) -> dict[str, logging.LogRecord]:
    return {
        str(record.facility_uuid): record
        for record in records
        if record.getMessage() == "collect_facility_done"
    }


@pytest.fixture()
def isolated_root_logger() -> Iterator[None]:
    """Save and restore the root logger so logging tests do not leak."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


# --------------------------------------------------------------------------
# Cadence bucketing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "cadence", "expected"),
    [
        (dt.datetime(2026, 9, 11, 10, 45, tzinfo=dt.UTC), 30, "2026-09-11T10:30:00Z"),
        (dt.datetime(2026, 9, 11, 10, 30, tzinfo=dt.UTC), 30, "2026-09-11T10:30:00Z"),
        (dt.datetime(2026, 9, 11, 10, 29, 59, tzinfo=dt.UTC), 30, "2026-09-11T10:00:00Z"),
        # A cadence that does not divide an hour still gives stable buckets,
        # which flooring on minute-of-hour would not.
        (dt.datetime(2026, 9, 11, 10, 45, tzinfo=dt.UTC), 90, "2026-09-11T10:30:00Z"),
        (dt.datetime(2026, 9, 11, 11, 59, tzinfo=dt.UTC), 90, "2026-09-11T10:30:00Z"),
    ],
)
def test_poll_key_floors_to_the_cadence_bucket(
    moment: dt.datetime, cadence: int, expected: str
) -> None:
    """Regression: two runs inside one bucket must share an idempotency key."""
    assert poll_key_for(moment, cadence) == expected


def test_poll_key_is_utc_whatever_zone_the_caller_is_in() -> None:
    """Regression: a local-time poll key would collide across DST or zones."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert poll_key_for(NOW.astimezone(ist), 30) == EXPECTED_POLL_KEY


def test_floor_to_cadence_rejects_a_naive_instant() -> None:
    """Regression: a naive `now` would silently bucket by machine local time."""
    with pytest.raises(ValueError, match="timezone-aware"):
        floor_to_cadence(dt.datetime(2026, 9, 11, 10, 45), 30)


def test_horizon_dates_follow_the_local_calendar_day() -> None:
    """Regression: Hudle's date params are local, and 23:00Z is already tomorrow
    in Jaipur -- a UTC date would request a day that has passed there."""
    late = dt.datetime(2026, 9, 11, 23, 0, tzinfo=dt.UTC)  # 04:30 IST on the 12th
    start, end = horizon_dates(late, "Asia/Kolkata", 21)
    assert start == dt.date(2026, 9, 12)
    assert end == dt.date(2026, 10, 2)


# --------------------------------------------------------------------------
# A full cycle
# --------------------------------------------------------------------------


def test_a_full_cycle_writes_every_court_and_one_fetch_row_each(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a cycle must cover all six active courts in one grid request
    each and record every slot exactly once."""
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.ok
    assert result.exit_code is ExitCode.OK
    assert result.poll_key == EXPECTED_POLL_KEY
    assert len(client.requests) == ACTIVE_COURT_COUNT
    assert len(result.facilities) == ACTIVE_COURT_COUNT
    assert row_count(memory_storage, "slot_observations") == TOTAL_SLOTS
    assert row_count(memory_storage, "facility_fetches") == ACTIVE_COURT_COUNT
    assert row_count(memory_storage, "snapshots") == 1

    snapshot = memory_storage.latest_snapshot()
    assert snapshot is not None
    assert snapshot.ok is True
    assert snapshot.error is None
    assert snapshot.horizon_days == test_config.poll.horizon_days


def test_a_cycle_requests_one_range_per_facility_not_one_per_day(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: the range parameter is the whole point. One request per day
    would be 132 requests a cycle instead of 6.

    The range starts one day behind today: a date's settled state is only
    knowable after it elapses, and Hudle keeps serving it, so the lookback is
    what turns "the last poll before midnight" into "the final answer"."""
    client = FakeSlotsClient(grids)

    run_collect(test_config, memory_storage, client, now=NOW)

    start, end = dt.date(2026, 9, 10), dt.date(2026, 10, 1)  # 1 day back + 21 ahead
    assert [(r[2], r[3]) for r in client.requests] == [(start, end)] * ACTIVE_COURT_COUNT


def test_a_cycle_normalizes_to_court_minutes_not_slot_counts(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: 49 blocked slots span 2400 court-minutes, because 31 of them
    are Padel Up's 60-minute slots. Any slot-count total is wrong."""
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.counts[SlotState.BOOKED] == TOTAL_BOOKED
    assert result.counts[SlotState.BLOCKED] == TOTAL_BLOCKED
    assert result.counts[SlotState.OPEN] == TOTAL_OPEN
    assert result.court_minutes[SlotState.BLOCKED] == TOTAL_BLOCKED_MINUTES
    assert result.court_minutes[SlotState.BLOCKED] not in (
        TOTAL_BLOCKED * 30,
        TOTAL_BLOCKED * 60,
    )


def test_padel_up_reports_zero_bookings_without_looking_broken(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: Padel Up really sells nothing on Hudle. It must come back
    ok=True with a full slot count, not as a failed fetch."""
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW)
    padel_up = outcome_for(result.facilities, PADEL_UP_COURT)

    assert padel_up.ok is True
    assert padel_up.error is None
    assert padel_up.counts == {SlotState.BOOKED: 0, SlotState.BLOCKED: 31, SlotState.OPEN: 558}
    assert padel_up.written == 589


def test_unprobed_pickleball_courts_are_polled_and_recorded_as_empty(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: the three pickleball courts are active in config, so they
    must be attempted and get a fetch row even when they return no slots."""
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW)

    for facility_uuid in PICKLEBALL_COURTS:
        outcome = outcome_for(result.facilities, facility_uuid)
        assert outcome.ok is True
        assert outcome.slot_count == 0
        assert outcome.observed_grid_minutes is None
        assert outcome.grid_mismatch is False


def test_every_observation_is_stamped_with_its_configured_sport(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a venue's pickleball slots landing in its padel numbers.

    Six courts are polled and three of them are pickleball. The payload carries
    no sport, so it has to come from config at write time -- a dataset that did
    not record it could never be split afterwards, and this one cannot be
    backfilled. Here Padel Fort's first pickleball court is served a real grid,
    so the venue holds both sports and the filter has something to do.
    """
    pickleball_grid = json.loads(json.dumps(grids[PADEL_FORT_COURT]))
    for day in pickleball_grid["data"]["slot_data"]:
        for slot in day.get("slots") or ():
            slot["facility_uuid"] = FORT_PICKLEBALL_COURT_1
            # Distinct slot ids: the two courts publish separate inventory, and
            # (snapshot_id, slot_uuid) is the append-only uniqueness key.
            slot["id"] = f"pickle-{slot['id']}"
    client = FakeSlotsClient({**grids, FORT_PICKLEBALL_COURT_1: pickleball_grid})

    run_collect(test_config, memory_storage, client, now=NOW)

    sports_by_facility = {(o.facility_uuid, o.sport) for o in memory_storage.iter_observations()}
    assert (PADEL_FORT_COURT, Sport.PADEL) in sports_by_facility
    assert (FORT_PICKLEBALL_COURT_1, Sport.PICKLEBALL) in sports_by_facility

    padel_only = list(memory_storage.iter_observations(sport=Sport.PADEL))
    assert len(padel_only) == TOTAL_SLOTS
    assert {o.facility_uuid for o in padel_only} == {
        PADEL_UP_COURT,
        PLAY_PADEL_COURT,
        PADEL_FORT_COURT,
    }
    assert len(list(memory_storage.iter_observations())) == TOTAL_SLOTS + 1116


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_rerunning_the_same_minute_changes_no_row_counts(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """THE IDEMPOTENCY TEST. Regression: an overlapping cron or a manual retry
    inside one cadence bucket must converge, never double-write."""
    client = FakeSlotsClient(grids)

    first = run_collect(test_config, memory_storage, client, now=NOW)
    counts_after_first = {
        table: row_count(memory_storage, table)
        for table in ("snapshots", "slot_observations", "facility_fetches")
    }

    # A different instant inside the same 30-minute bucket.
    second = run_collect(test_config, memory_storage, client, now=NOW + dt.timedelta(minutes=3))

    assert second.poll_key == first.poll_key
    assert second.snapshot_id == first.snapshot_id
    assert first.written == TOTAL_SLOTS
    assert second.written == 0
    assert second.ok is True
    assert {
        table: row_count(memory_storage, table)
        for table in ("snapshots", "slot_observations", "facility_fetches")
    } == counts_after_first


def test_the_next_cadence_bucket_appends_a_second_observation_per_slot(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: idempotency must be per bucket, not per slot. The next poll
    is a new observation of the same slots -- that history is the dataset."""
    client = FakeSlotsClient(grids)

    run_collect(test_config, memory_storage, client, now=NOW)
    second = run_collect(test_config, memory_storage, client, now=NOW + dt.timedelta(minutes=30))

    assert second.snapshot_id != 1 or row_count(memory_storage, "snapshots") == 2
    assert row_count(memory_storage, "snapshots") == 2
    assert row_count(memory_storage, "slot_observations") == TOTAL_SLOTS * 2


# --------------------------------------------------------------------------
# Partial failure
# --------------------------------------------------------------------------


def test_one_failing_facility_does_not_lose_the_others(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: the forward-only dataset makes a lost facility permanent. A
    503 at one venue must not discard the other five courts' slots."""
    client = FakeSlotsClient(
        grids, failures={PADEL_FORT_COURT: HudleHttpError(503, "upstream busy", path="/slots")}
    )

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.ok is False
    assert result.exit_code is ExitCode.PARTIAL
    assert len(result.facilities) == ACTIVE_COURT_COUNT  # every court was attempted
    assert result.skipped == 0

    # The other venues' data is all there; only Padel Fort's is missing.
    assert row_count(memory_storage, "slot_observations") == 589 + 1240
    assert not list(memory_storage.iter_observations(facility_uuid=PADEL_FORT_COURT))
    assert len(list(memory_storage.iter_observations(facility_uuid=PLAY_PADEL_COURT))) == 1240

    snapshot = memory_storage.latest_snapshot()
    assert snapshot is not None
    assert snapshot.ok is False
    assert snapshot.error is not None
    assert "Padel Fort/Padel Court" in snapshot.error

    fetches = {f.facility_uuid: f for f in memory_storage.facility_fetches_for_snapshot(1)}
    assert len(fetches) == ACTIVE_COURT_COUNT
    failed = fetches[PADEL_FORT_COURT]
    assert failed.ok is False
    assert failed.http_status == 503
    assert failed.slot_count == 0
    assert failed.error is not None and "503" in failed.error
    # The client exhausts its retry budget before raising an HTTP error.
    assert failed.attempts == test_config.poll.backoff.max_attempts
    assert fetches[PLAY_PADEL_COURT].ok is True
    assert fetches[PLAY_PADEL_COURT].attempts == 1


def test_an_envelope_level_refusal_is_recorded_without_a_retry_count(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a 200 carrying success=false is not retried by the client, so
    recording it as max_attempts would misreport the request cost."""
    client = FakeSlotsClient(
        grids, failures={PADEL_UP_COURT: HudleApiError("success=false", path="/slots")}
    )

    result = run_collect(test_config, memory_storage, client, now=NOW)
    outcome = outcome_for(result.facilities, PADEL_UP_COURT)

    assert outcome.ok is False
    assert outcome.attempts == 1
    assert outcome.http_status is None


def test_a_malformed_slot_fails_only_its_own_facility(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a parse error is still one facility's problem. It must be
    caught and recorded, not raised through the cycle."""
    broken = {
        "success": True,
        "data": {
            "slot_timings": [{"from": "19:00:00", "to": "19:30:00"}],
            "slot_data": [{"date": "2026-09-11", "is_empty": False, "slots": [{"id": "x"}]}],
        },
    }
    client = FakeSlotsClient({**grids, FORT_PICKLEBALL_COURT_1: broken})

    result = run_collect(test_config, memory_storage, client, now=NOW)
    outcome = outcome_for(result.facilities, FORT_PICKLEBALL_COURT_1)

    assert outcome.ok is False
    assert outcome.error is not None
    assert row_count(memory_storage, "slot_observations") == TOTAL_SLOTS
    assert result.exit_code is ExitCode.PARTIAL


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_dry_run_calls_no_storage_write_method(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: --dry-run must be safe against a production database. Even
    create_snapshot would claim the cadence bucket and neuter the real poll."""
    spy = StorageSpy(memory_storage)
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, spy, client, now=NOW, dry_run=True)  # type: ignore[arg-type]

    assert spy.writes == []
    assert spy.calls == []
    assert row_count(memory_storage, "snapshots") == 0
    assert row_count(memory_storage, "slot_observations") == 0
    assert result.snapshot_id is None
    assert result.dry_run is True


def test_dry_run_still_reports_the_classified_counts(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a dry run that reported nothing would be useless for the
    plausibility check it exists to serve."""
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW, dry_run=True)

    assert len(client.requests) == ACTIVE_COURT_COUNT
    assert sum(result.counts.values()) == TOTAL_SLOTS
    assert result.counts[SlotState.BOOKED] == TOTAL_BOOKED
    assert result.counts[SlotState.BLOCKED] == TOTAL_BLOCKED
    assert result.court_minutes[SlotState.BOOKED] == TOTAL_BOOKED_MINUTES
    assert result.written == 0
    assert result.ok is True


def test_dry_run_summary_names_every_facility_and_the_totals(
    test_config: Config,
    memory_storage: Storage,
    grids: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: the printed summary is what an operator eyeballs; it must
    carry the per-state counts, not just a row total."""
    client = FakeSlotsClient(grids)
    result = run_collect(test_config, memory_storage, client, now=NOW, dry_run=True)

    cli.print_dry_run(result)
    out = capsys.readouterr().out

    assert "Padel Fort/Padel Court" in out
    assert "booked=6" in out
    assert f"court_minutes_blocked={TOTAL_BLOCKED_MINUTES}" in out
    assert "nothing was written" in out


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def test_an_open_circuit_stops_the_cycle_mid_way_and_exits_two(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: once Hudle is refusing us the polite thing is to stop. The
    remaining courts must be left unattempted and the caller told to stop."""
    client = FakeSlotsClient(grids, trips_circuit={PLAY_PADEL_COURT})

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.circuit_open is True
    assert result.exit_code is ExitCode.STOPPED
    assert result.ok is False
    assert result.skipped > 0
    # Padel Up succeeded before the breaker tripped; its slots are kept.
    assert row_count(memory_storage, "slot_observations") == 589
    assert len(client.requests) < ACTIVE_COURT_COUNT

    snapshot = memory_storage.latest_snapshot()
    assert snapshot is not None
    assert snapshot.ok is False
    assert snapshot.error is not None and "circuit open" in snapshot.error


def test_a_circuit_already_open_sends_no_request_at_all(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: a scheduler that keeps calling collect must not keep probing
    a service that has already refused us."""
    client = FakeSlotsClient(grids)
    client.circuit.trip()

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert client.requests == []
    assert result.facilities == ()
    assert result.circuit_open is True
    assert result.exit_code is ExitCode.STOPPED
    assert row_count(memory_storage, "slot_observations") == 0


def test_a_total_failure_exits_two_not_one(test_config: Config, memory_storage: Storage) -> None:
    """Regression: zero facilities collected is not a partial success, and cron
    must be able to tell "some data" from "no data"."""
    failures = {
        uuid: HudleHttpError(500, "boom", path="/slots")
        for uuid, _ in (
            (PADEL_UP_COURT, None),
            (PLAY_PADEL_COURT, None),
            (PADEL_FORT_COURT, None),
            *[(u, None) for u in PICKLEBALL_COURTS],
        )
    }
    client = FakeSlotsClient({}, failures=failures)

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.succeeded == ()
    assert result.exit_code is ExitCode.STOPPED
    assert row_count(memory_storage, "slot_observations") == 0
    assert row_count(memory_storage, "facility_fetches") == ACTIVE_COURT_COUNT


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def test_per_facility_state_counts_reach_the_log(
    test_config: Config,
    memory_storage: Storage,
    grids: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: the explicit requirement. Every run must log counts by state
    per facility so plausibility is checkable without a database."""
    caplog.set_level(logging.INFO, logger="tracker.collect")
    client = FakeSlotsClient(grids)

    run_collect(test_config, memory_storage, client, now=NOW)

    events = facility_events(caplog.records)
    assert set(events) == {PADEL_UP_COURT, PLAY_PADEL_COURT, PADEL_FORT_COURT, *PICKLEBALL_COURTS}

    fort = events[PADEL_FORT_COURT]
    assert fort.facility == "Padel Fort/Padel Court"
    assert (fort.booked, fort.blocked, fort.open) == (6, 18, 1092)
    assert fort.court_minutes_booked == 180
    assert fort.grid == 30
    assert fort.written == 1116

    up = events[PADEL_UP_COURT]
    assert (up.booked, up.blocked, up.open) == (0, 31, 558)
    # 60-minute grid: the same slot count means twice the court time.
    assert up.court_minutes_blocked == 31 * 60
    assert up.grid == 60


def test_the_run_summary_logs_the_totals(
    test_config: Config,
    memory_storage: Storage,
    grids: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a per-facility line is not enough to spot a half-collected
    cycle; the run needs one line carrying its own totals."""
    caplog.set_level(logging.INFO, logger="tracker.collect")
    client = FakeSlotsClient(grids)

    run_collect(test_config, memory_storage, client, now=NOW)

    done = [r for r in caplog.records if r.getMessage() == "collect_run_done"]
    assert len(done) == 1
    record = done[0]
    assert record.slots == TOTAL_SLOTS
    assert record.booked == TOTAL_BOOKED
    assert record.court_minutes_blocked == TOTAL_BLOCKED_MINUTES
    assert record.facilities_ok == ACTIVE_COURT_COUNT
    assert record.exit_code == int(ExitCode.OK)
    assert record.poll_key == EXPECTED_POLL_KEY


def test_a_grid_mismatch_against_config_warns(
    test_config: Config,
    memory_storage: Storage,
    grids: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a venue moving from 60- to 30-minute slots would silently
    halve its apparent inventory and corrupt every cross-venue comparison."""
    caplog.set_level(logging.INFO, logger="tracker.collect")
    # Padel Up is configured at 60 minutes; serve it a 30-minute grid.
    regridded = {
        "success": True,
        "data": {
            "slot_timings": [
                {"from": "19:00:00", "to": "19:30:00"},
                {"from": "19:30:00", "to": "20:00:00"},
            ],
            "slot_data": [
                {
                    "date": "2026-09-11",
                    "is_empty": False,
                    "slots": [
                        {
                            "id": "22222222-2222-4222-8222-222222222222",
                            "facility_uuid": PADEL_UP_COURT,
                            "price": "900.00",
                            "start_time": "2026-09-11 19:00:00",
                            "end_time": "2026-09-11 19:30:00",
                            "total_count": 1,
                            "available_count": 1,
                            "is_available": True,
                            "is_booked": False,
                        }
                    ],
                }
            ],
        },
    }
    client = FakeSlotsClient({**grids, PADEL_UP_COURT: regridded})

    result = run_collect(test_config, memory_storage, client, now=NOW)
    outcome = outcome_for(result.facilities, PADEL_UP_COURT)

    assert outcome.observed_grid_minutes == 30
    assert outcome.configured_grid_minutes == 60
    assert outcome.grid_mismatch is True
    warnings = [r for r in caplog.records if r.getMessage() == "collect_grid_mismatch"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert (warnings[0].configured_grid_minutes, warnings[0].observed_grid_minutes) == (60, 30)
    # The mismatch is a warning, not a failure: the slots are still collected.
    assert outcome.ok is True


def test_a_matching_grid_produces_no_warning(
    test_config: Config,
    memory_storage: Storage,
    grids: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a mismatch warning that fires on the normal case would be
    ignored within a week."""
    caplog.set_level(logging.INFO, logger="tracker.collect")
    client = FakeSlotsClient(grids)

    result = run_collect(test_config, memory_storage, client, now=NOW)

    assert result.grid_mismatches == ()
    assert [r for r in caplog.records if r.getMessage() == "collect_grid_mismatch"] == []


def test_slot_counts_by_state_always_names_all_three_states() -> None:
    """Regression: a missing BLOCKED key is how a blocked evening disappears
    from a report."""
    assert slot_counts_by_state([]) == {
        SlotState.BOOKED: 0,
        SlotState.BLOCKED: 0,
        SlotState.OPEN: 0,
    }


# --------------------------------------------------------------------------
# Derived tables
# --------------------------------------------------------------------------


def test_backfill_derives_the_normal_booking_lead_time(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: lead time is measured from the FIRST snapshot that saw BOOKED,
    and carries the poll gap it hid inside."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)

    rows = {r.slot_uuid: r for r in storage.list_first_booked()}
    row = rows[synthetic_history_storage.normal_slot_uuid]
    assert row.first_booked_at == synthetic_history_storage.expected_first_booked_at
    assert row.lead_time_hours == pytest.approx(synthetic_history_storage.expected_lead_time_hours)
    assert row.uncertainty_minutes == synthetic_history_storage.expected_uncertainty_minutes
    assert row.censored_left is False
    assert row.rebooked is False
    assert row.cancelled is False


def test_backfill_excludes_left_censored_slots_from_lead_time(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: a slot already BOOKED in the first snapshot was sold before
    our data starts. Counting it as a zero-lead booking drags every percentile
    down; it must carry no lead time at all."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)

    censored = {r.slot_uuid: r for r in storage.list_first_booked() if r.censored_left}
    assert set(censored) == set(synthetic_history_storage.censored_slot_uuids)
    for row in censored.values():
        assert row.lead_time_hours is None
        assert row.uncertainty_minutes is None

    uncensored = storage.list_first_booked(exclude_censored=True)
    assert not ({r.slot_uuid for r in uncensored} & synthetic_history_storage.censored_slot_uuids)


def test_backfill_records_cancellation_and_rebooking(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: collapsing a slot to one booking loses the churn. A cancelled
    slot must not read as booked, and a re-booked one must keep both sales."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)
    rows = {r.slot_uuid: r for r in storage.list_first_booked()}

    cancelled = rows[synthetic_history_storage.cancellation_slot_uuid]
    assert cancelled.cancelled is True
    assert cancelled.rebooked is False
    assert cancelled.first_booked_at == synthetic_history_storage.cancellation_booked_at

    rebooked = rows[synthetic_history_storage.rebooked_slot_uuid]
    assert rebooked.rebooked is True
    assert rebooked.first_booked_at == synthetic_history_storage.rebooked_first_booked_at
    assert rebooked.last_booked_at == synthetic_history_storage.rebooked_last_booked_at

    released = [
        t
        for t in storage.list_transitions(facility_uuid=cancelled.facility_uuid)
        if t.slot_uuid == cancelled.slot_uuid and t.from_state is SlotState.BOOKED
    ]
    assert [t.first_seen_at for t in released] == [
        synthetic_history_storage.cancellation_released_at
    ]


def test_backfill_records_the_open_to_blocked_transition(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: a venue pulling a slot from inventory is a market signal, and
    it is not a booking. It must appear as a transition, never in first_booked."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)

    slot_uuid = synthetic_history_storage.open_to_blocked_slot_uuid
    blocked = [t for t in storage.list_transitions(to_state=SlotState.BLOCKED)]
    matching = [t for t in blocked if t.slot_uuid == slot_uuid]
    assert len(matching) == 1
    assert matching[0].from_state is SlotState.OPEN
    assert matching[0].first_seen_at == synthetic_history_storage.open_to_blocked_at
    assert {r.slot_uuid for r in storage.list_first_booked()}.isdisjoint({slot_uuid})


def test_backfill_writes_no_row_for_a_slot_that_was_never_booked(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: the whole blocked evening and the elapsed-but-open slot were
    never sold. They belong in the occupancy denominator, not in booking stats."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)
    booked_slots = {r.slot_uuid for r in storage.list_first_booked()}

    assert booked_slots.isdisjoint(synthetic_history_storage.blocked_evening_slot_uuids)
    assert synthetic_history_storage.elapsed_open_slot_uuid not in booked_slots


def test_backfill_records_one_first_sighting_plus_the_real_changes(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: the first-sighting row (``from_state IS NULL``) is what makes
    a booking that pre-dates the dataset detectable at all, so it must be
    written; a slot that never changed must contribute nothing beyond it."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)
    transitions = storage.list_transitions()

    steady = {
        *synthetic_history_storage.blocked_evening_slot_uuids,
        synthetic_history_storage.elapsed_open_slot_uuid,
        *synthetic_history_storage.normalization_slot_uuids,
        *synthetic_history_storage.censored_slot_uuids,
    }
    for slot_uuid in steady:
        rows = [t for t in transitions if t.slot_uuid == slot_uuid]
        assert len(rows) == 1
        assert rows[0].from_state is None
        assert rows[0].prev_seen_at is None
        assert rows[0].uncertainty_minutes is None

    changes = [t for t in transitions if t.from_state is not None]
    assert {t.slot_uuid for t in changes}.isdisjoint(steady)
    # Every change carries the poll gap it hid inside; the cadence is 30 minutes.
    assert all(t.uncertainty_minutes is not None for t in changes)


def test_backfill_is_repeatable_and_replaces_rather_than_appends(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: the derived tables are rebuilt, not accumulated. A second run
    that doubled them would double every lead-time percentile's weight."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    first = backfill_derived(storage)
    second = backfill_derived(storage)

    assert first == second
    assert first.transitions == len(storage.list_transitions())
    assert first.first_booked == len(storage.list_first_booked())
    assert first.slots > first.booked_slots > 0


def test_backfill_business_date_follows_the_trading_day(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: Play Padel's 00:30 Saturday slot is Friday-night demand. A
    booking summary keyed on the local date reports the wrong trading day."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)
    rows = {r.slot_uuid: r for r in storage.list_first_booked()}
    row = rows[synthetic_history_storage.post_midnight_slot_uuid]

    assert row.business_date == synthetic_history_storage.post_midnight_business_date
    assert row.business_date != synthetic_history_storage.post_midnight_local_date
    assert row.first_booked_at == synthetic_history_storage.post_midnight_booked_at


def test_backfill_dates_a_change_to_the_poll_that_first_saw_it(
    synthetic_history_storage: SyntheticHistory,
) -> None:
    """Regression: how far ahead a slot was when it sold is the whole lead-time
    story, and the change belongs to the poll that first saw it -- not to the
    last poll of the run and not to the end of the history."""
    storage = synthetic_history_storage.storage
    assert storage is not None

    backfill_derived(storage)

    slot_uuid = synthetic_history_storage.normal_slot_uuid
    booked = [
        t
        for t in storage.list_transitions(to_state=SlotState.BOOKED)
        if t.slot_uuid == slot_uuid and t.from_state is SlotState.OPEN
    ]
    assert len(booked) == 1
    assert booked[0].first_seen_at == synthetic_history_storage.expected_first_booked_at
    assert booked[0].prev_seen_at == synthetic_history_storage.expected_prev_seen_at
    assert booked[0].uncertainty_minutes == synthetic_history_storage.expected_uncertainty_minutes

    observation = next(
        o for o in synthetic_history_storage.for_slot(slot_uuid) if o.state is SlotState.BOOKED
    )
    assert booked[0].days_ahead_at_change == observation.days_ahead
    assert isinstance(booked[0], StateTransition)


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------


def test_json_formatter_emits_one_object_per_line_with_the_event_fields() -> None:
    """Regression: the log is the only record of an unattended poll. It has to
    be machine-readable months later."""
    record = logging.LogRecord(
        "tracker.collect", logging.INFO, __file__, 1, "collect_facility_done", None, None
    )
    record.booked = 6
    record.facility = "Padel Fort/Padel Court"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "collect_facility_done"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tracker.collect"
    assert payload["booked"] == 6
    assert payload["facility"] == "Padel Fort/Padel Court"
    assert payload["ts"].endswith("Z")


def test_console_formatter_renders_key_value_pairs() -> None:
    """Regression: the human form must stay greppable, so values with spaces are
    quoted rather than running together."""
    record = logging.LogRecord(
        "tracker.collect", logging.WARNING, __file__, 1, "collect_grid_mismatch", None, None
    )
    record.facility = "Padel Up/Padel Court"
    record.observed_grid_minutes = 30
    record.error = None

    line = ConsoleFormatter().format(record)

    assert "collect_grid_mismatch" in line
    assert 'facility="Padel Up/Padel Court"' in line
    assert "observed_grid_minutes=30" in line
    assert "error=-" in line


def test_event_fields_ignores_the_logging_modules_own_attributes() -> None:
    """Regression: leaking `lineno` or `msg` into every event would bury the
    fields that matter."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "an_event", None, None)
    record.booked = 1
    assert event_fields(record) == {"booked": 1}


def test_configure_logging_installs_exactly_one_handler(isolated_root_logger: None) -> None:
    """Regression: calling setup twice must not double every line."""
    stream = io.StringIO()
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)
    configure_logging(level="INFO", log_format=LogFormat.JSON, stream=stream)

    logging.getLogger("tracker.collect").info("collect_run_done", extra={"booked": 27})

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["booked"] == 27
    assert len(logging.getLogger().handlers) == 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_collect_defaults_to_a_single_shot_json_run() -> None:
    """Regression: the default form is what cron calls. It must not loop."""
    args = cli.build_parser().parse_args(["collect"])
    assert args.func is cli.cmd_collect
    assert args.dry_run is False
    assert args.loop is False
    assert args.log_format is LogFormat.JSON


def test_cli_collect_accepts_dry_run_loop_and_config() -> None:
    args = cli.build_parser().parse_args(
        ["collect", "--dry-run", "--loop", "--config", "/tmp/other.yaml"]
    )
    assert (args.dry_run, args.loop, args.config) == (True, True, Path("/tmp/other.yaml"))


def test_cli_exposes_every_documented_command() -> None:
    """Regression: the Makefile and any cron line depend on these names."""
    parser = cli.build_parser()
    for command, expected in (
        ("collect", cli.cmd_collect),
        ("discover", cli.cmd_discover),
        ("serve", cli.cmd_serve),
        ("backfill-derived", cli.cmd_backfill_derived),
    ):
        assert parser.parse_args([command]).func is expected


def test_cli_reports_bad_configuration_as_exit_two(
    tmp_path: Path, isolated_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: a missing config is a stop, not a partial success, and it must
    not surface as a traceback in a cron mail."""
    missing = tmp_path / "nope.yaml"
    code = cli.main(["collect", "--config", str(missing)])
    assert code == int(ExitCode.STOPPED)
    assert "configuration error" in capsys.readouterr().err


def test_cli_rejects_a_non_sqlite_storage_url_rather_than_guessing(test_config: Config) -> None:
    """Regression: silently falling back to SQLite for a postgres:// url would
    write the irreplaceable observations to the wrong database."""
    from tracker.config import ConfigError, StorageConfig

    postgres = dataclasses.replace(
        test_config, storage=StorageConfig(url="postgresql://localhost/padel")
    )
    with pytest.raises(ConfigError, match="not SQLite"):
        cli.build_storage(postgres)


def test_cli_storage_threads_the_poll_cadence_into_the_coverage_view(
    test_config: Config, tmp_path: Path
) -> None:
    """Regression: v_coverage_daily bakes the expected polls per day into SQL.
    Defaulting it would report coverage against the wrong denominator."""
    from tracker.config import StorageConfig

    config = dataclasses.replace(
        test_config, storage=StorageConfig(url=f"sqlite:///{tmp_path / 'padel.db'}")
    )
    storage = cli.build_storage(config)
    try:
        rows = storage.query_rows("SELECT snapshots_expected FROM v_coverage_daily LIMIT 1")
        assert rows == []  # no data yet, but the view exists and is queryable
        assert config.poll.expected_snapshots_per_day == 48
    finally:
        storage.close()


class FakeSearchClient:
    """A HudleClient stand-in for the discovery adapter."""

    def __init__(self, pages: Sequence[Mapping[str, Any]], html: str = "") -> None:
        self._pages = list(pages)
        self.html = html
        self.search_calls: list[tuple[int, int, int, int]] = []
        self.page_calls: list[tuple[str, str]] = []

    def search_venues(
        self,
        sport_id: int,
        page: int = 1,
        per_page: int = 50,
        *,
        city_id: int = 8,
        venue_name: str = "",
    ) -> dict[str, Any]:
        self.search_calls.append((sport_id, page, per_page, city_id))
        return dict(self._pages[page - 1])

    def fetch_venue_page_html(self, slug: str, numeric_id: str) -> str:
        self.page_calls.append((slug, numeric_id))
        return self.html


def test_discovery_adapter_returns_whole_page_envelopes(raw_venue_search_padel: Any) -> None:
    """Regression: discovery reads meta.pagination to know whether a search
    truncated. An adapter that flattened to a venue list would destroy the only
    evidence that the 57-venue pickleball market came back as 50."""
    client = FakeSearchClient([raw_venue_search_padel])

    pages = cli.HudleDiscoveryClient(client).search_venues_all(  # type: ignore[arg-type]
        sport_id=44, city_id=8, per_page=50
    )

    assert len(pages) == 1
    assert "meta" in pages[0]
    assert pages[0]["meta"]["pagination"]["total"] == 3


def test_discovery_adapter_walks_every_page(raw_venue_search_pickleball: Any) -> None:
    """Regression: pickleball is 57 venues at per_page=50. One call truncates."""
    page_two = {
        "code": 200,
        "data": [],
        "meta": {
            "pagination": {
                "total": 57,
                "count": 7,
                "per_page": 50,
                "current_page": 2,
                "total_pages": 2,
            }
        },
    }
    client = FakeSearchClient([raw_venue_search_pickleball, page_two])

    pages = cli.HudleDiscoveryClient(client).search_venues_all(  # type: ignore[arg-type]
        sport_id=56, city_id=8, per_page=50
    )

    assert len(pages) == 2
    assert [call[1] for call in client.search_calls] == [1, 2]


def test_discovery_adapter_maps_an_ssr_path_to_slug_and_numeric_id() -> None:
    """Regression: discovery addresses SSR pages by VenueConfig.ssr_path while
    the client takes (slug, numeric_id). A wrong split fetches the wrong venue."""
    client = FakeSearchClient([], html="<html></html>")

    html = cli.HudleDiscoveryClient(client).fetch_venue_page(  # type: ignore[arg-type]
        "/venues/padel-up/772576"
    )

    assert html == "<html></html>"
    assert client.page_calls == [("padel-up", "772576")]


def test_consecutive_failure_tracker_stops_the_loop_after_the_limit() -> None:
    """Regression: --loop must stop rather than hammer Hudle for hours. One bad
    cycle is normal; a run of them is not fixable by more requests."""
    failures = ConsecutiveFailureTracker(3)
    for _ in range(2):
        failures.record(ok=False)
    assert failures.exhausted is False

    failures.record(ok=True)
    assert failures.consecutive_failures == 0

    for _ in range(3):
        failures.record(ok=False)
    assert failures.exhausted is True
    assert failures.limit == 3


def test_cli_serve_hands_the_app_to_uvicorn_as_a_string() -> None:
    """Regression: the CLI must stay usable while tracker.web is half-written,
    so the app is named, never imported here.

    The "not imported" half runs in a subprocess on purpose. Asserting against
    this process's ``sys.modules`` would pass alone and fail in a full session,
    because ``tests/test_web.py`` imports ``tracker.web`` for its own fixtures
    and module imports are global. A fresh interpreter is the only place the
    question "does importing the CLI drag in the dashboard?" has an answer.
    """
    args = cli.build_parser().parse_args(["serve", "--port", "9001"])
    assert args.port == 9001
    assert cli.WEB_APP_PATH == "tracker.web:app"

    probe = subprocess.run(
        [sys.executable, "-c", "import tracker.__main__, sys; print('tracker.web' in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
    )
    assert probe.stdout.strip() == "False"


def test_a_cycle_records_the_venue_and_facility_dimensions(
    test_config: Config, memory_storage: Storage, grids: dict[str, Any]
) -> None:
    """Regression: dimension tables left empty, so first_seen/last_seen never exist.

    ``first_seen``/``last_seen`` are what make "when did a fourth venue appear"
    answerable from the data months later rather than only from a drift alert
    someone happened to read at the time. The upserts existed but nothing
    called them, so both tables stayed at zero rows through a real collect.
    """
    run_collect(test_config, memory_storage, FakeSlotsClient(grids), now=NOW)

    venues = {dim.venue_uuid: dim for dim in memory_storage.list_venue_dims()}
    facilities = {dim.facility_uuid: dim for dim in memory_storage.list_facility_dims()}
    assert set(venues) == {venue.uuid for venue in test_config.venues}
    assert set(facilities) == {
        facility.uuid for venue in test_config.venues for facility in venue.facilities
    }, "equipment facilities belong here too: the table records what we decided, not what we polled"
    assert all(dim.first_seen == NOW for dim in venues.values())


def test_the_window_reaches_lookback_days_behind_today() -> None:
    """Regression: lookback silently ignored, so an outage over midnight loses
    every date's final state for good."""
    at = dt.datetime(2026, 9, 12, 3, 0, tzinfo=dt.UTC)  # 08:30 IST on the 12th
    assert horizon_dates(at, "Asia/Kolkata", 21) == (dt.date(2026, 9, 12), dt.date(2026, 10, 2))
    assert horizon_dates(at, "Asia/Kolkata", 21, 1) == (dt.date(2026, 9, 11), dt.date(2026, 10, 2))
    with pytest.raises(ValueError):
        horizon_dates(at, "Asia/Kolkata", 21, -1)
