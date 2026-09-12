"""Shared test harness.

Nothing in this file touches the network. The live Hudle API is off limits to
the test suite: every fixture is either a recorded response under
``fixtures/raw/`` or deterministically synthesized here.

Every timestamp in this module is a literal. ``datetime.now()`` is never called,
so the suite is reproducible on any machine at any hour.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tracker.config import Config, load_config
from tracker.storage import Storage
from tracker.types import (
    FacilityFetch,
    SlotObservation,
    SlotState,
    SnapshotRecord,
    Sport,
    business_date_for,
    days_ahead_for,
    duration_minutes_for,
    from_local_text,
    slot_start_utc_for,
    to_utc_text,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "fixtures" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

TZ = "Asia/Kolkata"
BUSINESS_DAY_START_HOUR = 4

# Venue / facility identities, straight from the frozen config.
PADEL_UP_VENUE = "e606e880-0b2c-4c69-b1a8-193c8f915328"
PADEL_UP_COURT = "e27518b2-9cee-49a9-aa6f-8685e5c543f3"
PLAY_PADEL_VENUE = "9b288765-eee9-4d8a-b309-a4f09b11abcc"
PLAY_PADEL_COURT = "f40a05d6-e336-43be-bdc6-28405176ed9c"
PADEL_FORT_VENUE = "e161ebf7-78c7-4a45-bad8-49841f38b18a"
PADEL_FORT_COURT = "e03fdd0f-f8d8-4bb4-b707-d64a7244036f"

PRICE_PADEL_UP_SLOT = 1800.0  # 60-min grid -> 1800 per court-hour
PRICE_PLAY_PADEL_SLOT = 1000.0  # 30-min grid -> 2000 per court-hour
PRICE_PADEL_FORT_SLOT = 900.0  # 30-min grid -> 1800 per court-hour


# --------------------------------------------------------------------------
# Recorded API responses
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    """Directory holding the recorded Hudle responses."""
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def load_fixture() -> Callable[[str], Any]:
    """Load a recorded response by file stem or filename."""

    def _load(name: str) -> Any:
        candidate = FIXTURE_DIR / (name if name.endswith(".json") else f"{name}.json")
        if not candidate.is_file():
            available = sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
            raise FileNotFoundError(f"no fixture {name!r}; available: {available}")
        return json.loads(candidate.read_text(encoding="utf-8"))

    return _load


@pytest.fixture(scope="session")
def raw_slots_padel_up(load_fixture: Callable[[str], Any]) -> Any:
    """589 slots, 60-min grid, 0 BOOKED / 31 BLOCKED / 558 OPEN."""
    return load_fixture("slots_padel_up_31d")


@pytest.fixture(scope="session")
def raw_slots_play_padel(load_fixture: Callable[[str], Any]) -> Any:
    """1240 slots, 30-min grid, 21 BOOKED / 0 BLOCKED / 1219 OPEN."""
    return load_fixture("slots_play_padel_31d")


@pytest.fixture(scope="session")
def raw_slots_padel_fort(load_fixture: Callable[[str], Any]) -> Any:
    """1116 slots, 30-min grid, 6 BOOKED / 18 BLOCKED / 1092 OPEN."""
    return load_fixture("slots_padel_fort_31d")


@pytest.fixture(scope="session")
def raw_venue_search_padel(load_fixture: Callable[[str], Any]) -> Any:
    """Exactly 3 venues, flat ``data[]`` plus a ``meta{}`` block."""
    return load_fixture("venue_search_padel")


@pytest.fixture(scope="session")
def raw_venue_search_pickleball(load_fixture: Callable[[str], Any]) -> Any:
    """50 of 57 venues: proves a single call silently truncates."""
    return load_fixture("venue_search_pickleball")


@pytest.fixture(scope="session")
def raw_next_data() -> dict[str, Any]:
    """The SSR ``venueDetails`` objects, already unwrapped, keyed by short name."""
    return {
        key: json.loads(
            (FIXTURE_DIR / f"next_data_venue_details_{key}.json").read_text(encoding="utf-8")
        )
        for key in ("padel_up", "play_padel", "padel_fort")
    }


@pytest.fixture(scope="session")
def test_config() -> Config:
    """The real, frozen ``config.yaml``."""
    return load_config(CONFIG_PATH)


# --------------------------------------------------------------------------
# In-memory storage
# --------------------------------------------------------------------------


@pytest.fixture()
def memory_storage() -> Storage:
    """An initialized in-memory SQLite :class:`Storage`.

    ``tracker.storage_sqlite`` is written concurrently by another agent, so the
    import happens inside the fixture body and the test skips rather than
    breaking collection if the module is not there yet.
    """
    module = pytest.importorskip(
        "tracker.storage_sqlite", reason="tracker.storage_sqlite not implemented yet"
    )
    factory = getattr(module, "SQLiteStorage", None)
    if factory is None:
        pytest.skip("tracker.storage_sqlite does not expose SQLiteStorage")
    storage: Storage = factory("sqlite://")
    storage.initialize()
    return storage


# --------------------------------------------------------------------------
# Synthetic multi-snapshot history
# --------------------------------------------------------------------------
#
# Lead time, cancellation, re-booking and sellout logic can only be tested
# against a *sequence* of polls, and the real dataset is forward-looking: we
# cannot wait days for one. So the trajectories below are scripted.
#
# Snapshot schedule: 12 cadence ticks 30 minutes apart starting at
# BASE_OBSERVED_AT, with ticks 8 and 9 deliberately absent. That leaves 10
# snapshots and one 90-minute hole between tick 7 and tick 10.
#
#   tick   0   1   2   3   4   5   6   7  (8) (9) 10  11
#   pos    0   1   2   3   4   5   6   7           8   9
#
# All ten observation times fall on local date 2026-09-11 (15:30-21:00 IST), so
# days_ahead is stable across the whole history.

BASE_OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)  # 15:30 IST
CADENCE_MINUTES = 30
SCHEDULED_TICKS = tuple(range(12))
MISSING_TICKS = (8, 9)
PRESENT_TICKS = tuple(t for t in SCHEDULED_TICKS if t not in MISSING_TICKS)
SNAPSHOT_COUNT = len(PRESENT_TICKS)  # 10

GAP_START = BASE_OBSERVED_AT + dt.timedelta(minutes=7 * CADENCE_MINUTES)  # 13:30Z
GAP_END = BASE_OBSERVED_AT + dt.timedelta(minutes=10 * CADENCE_MINUTES)  # 15:00Z
GAP_MINUTES = 90

# The real Padel Fort 2026-09-13 evening, pulled from inventory in full:
# 14 consecutive 30-min slots, BLOCKED, zero bookings. 420 blocked court-minutes.
BLOCKED_EVENING_SLOTS: tuple[tuple[str, str], ...] = (
    ("4dee442b-0d69-4490-b7a5-ed65e36212d4", "2026-09-13 17:00:00"),
    ("017a576d-3596-497d-9ed9-a54f5ad5f6e6", "2026-09-13 17:30:00"),
    ("40589c83-8625-479f-be01-60d029b48ed6", "2026-09-13 18:00:00"),
    ("b521418f-2a8a-468b-a80a-2ad126535893", "2026-09-13 18:30:00"),
    ("c0c87f7a-b338-4766-8e11-c2a72def9a7d", "2026-09-13 19:00:00"),
    ("3cf676c9-f914-44ec-821d-551632e0ae23", "2026-09-13 19:30:00"),
    ("c03e8017-755d-4259-9d4b-3ea7d0da2a6c", "2026-09-13 20:00:00"),
    ("aa145d13-adb2-45eb-9b52-2d07610e9b1d", "2026-09-13 20:30:00"),
    ("e2feb356-09a4-496e-8ee4-583d792b4f19", "2026-09-13 21:00:00"),
    ("93a461fd-1275-44fb-9459-c01eef7b1b5c", "2026-09-13 21:30:00"),
    ("7280fa81-6db0-4122-9593-4db894605246", "2026-09-13 22:00:00"),
    ("c7727408-b07d-4d5b-a854-71128fd3d9ff", "2026-09-13 22:30:00"),
    ("ec3b6756-2eff-4a61-9011-ab086fbb97ea", "2026-09-13 23:00:00"),
    ("73a89993-3a5b-483b-b3bd-de6b71d90025", "2026-09-13 23:30:00"),
)

# Slot uuids. Every one is a real Hudle slot id taken from the recorded
# fixtures; only the state trajectory is synthetic.
SLOT_NORMAL_BOOKING = "ab3d448a-050f-4bd3-a804-f4c279c3fac4"  # Fort 2026-09-14 19:00
SLOT_CENSORED_FORT = "4e4f8b6a-2d92-4fb1-98be-a833d3ef0533"  # Fort 2026-09-15 19:00
SLOT_CENSORED_PLAY = "a21fcb42-ea08-4e7f-ada2-8be8f91bea67"  # Play 2026-09-12 01:00
SLOT_CANCELLED = "07e0bf09-486a-47ae-8dea-68397344965d"  # Fort 2026-09-15 20:00
SLOT_REBOOKED = "f9f915c8-fe18-4763-b408-e8ed071a8091"  # Fort 2026-09-15 21:00
SLOT_OPEN_TO_BLOCKED = "65aad407-e2d8-4287-8746-a62a2a83f56e"  # Fort 2026-09-17 19:00
SLOT_NORMALIZE_60 = "6bba96b2-abdd-41fe-9613-45482437e460"  # Up   2026-09-16 19:00
SLOT_NORMALIZE_30 = "cf18e15d-b985-44ec-9b37-0f8476f2184a"  # Fort 2026-09-16 19:00
SLOT_POST_MIDNIGHT = "6641d7bd-1dec-483f-bcbb-da5bf7f5cd15"  # Play 2026-09-12 00:30
SLOT_ELAPSED_OPEN = "32883b96-62ce-40fa-9e8a-163a4f695a97"  # Fort 2026-09-11 07:00

_O, _B, _X = "O", "B", "X"
_CODE_TO_STATE = {_O: SlotState.OPEN, _B: SlotState.BOOKED, _X: SlotState.BLOCKED}


@dataclasses.dataclass(frozen=True, slots=True)
class _Track:
    """One slot's scripted trajectory over the ten present snapshots."""

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    start_local: str
    end_local: str
    price: float
    states: str  # one character per present snapshot, len == SNAPSHOT_COUNT
    regression: str
    sport: Sport = Sport.PADEL


def _fort(slot_uuid: str, start: str, end: str, states: str, regression: str) -> _Track:
    return _Track(
        slot_uuid,
        PADEL_FORT_VENUE,
        PADEL_FORT_COURT,
        start,
        end,
        PRICE_PADEL_FORT_SLOT,
        states,
        regression,
    )


_TRACKS: tuple[_Track, ...] = (
    # Normal booking. OPEN for six snapshots, then BOOKED and stays booked.
    # Regression: lead time must be measured from the FIRST snapshot that saw
    # BOOKED (13:00Z), not from the last OPEN one and not from the run start.
    _fort(
        SLOT_NORMAL_BOOKING,
        "2026-09-14 19:00:00",
        "2026-09-14 19:30:00",
        "OOOOOOBBBB",
        "lead time anchored on the first BOOKED snapshot",
    ),
    # Left-censored: BOOKED in the very first snapshot that ever saw it. The
    # real booking predates our data.
    # Regression: must be excluded from lead-time statistics, never counted as
    # a booking made at first sight (which would drag every percentile down).
    _fort(
        SLOT_CENSORED_FORT,
        "2026-09-15 19:00:00",
        "2026-09-15 19:30:00",
        "BBBBBBBBBB",
        "left-censored slot excluded from lead-time stats",
    ),
    # Second left-censored slot, at a different venue and grid, so the censored
    # set is genuinely a set and a single hard-coded uuid will not pass.
    _Track(
        SLOT_CENSORED_PLAY,
        PLAY_PADEL_VENUE,
        PLAY_PADEL_COURT,
        "2026-09-12 01:00:00",
        "2026-09-12 01:30:00",
        PRICE_PLAY_PADEL_SLOT,
        "BBBBBBBBBB",
        "left-censored detection is not venue-specific",
    ),
    # Cancellation: OPEN -> BOOKED -> OPEN.
    # Regression: a BOOKED -> OPEN transition must be recorded as a
    # cancellation, and the slot must not still read as booked at day end.
    _fort(
        SLOT_CANCELLED,
        "2026-09-15 20:00:00",
        "2026-09-15 20:30:00",
        "OOOBBBOOOO",
        "BOOKED -> OPEN recorded as a cancellation",
    ),
    # Re-booked: OPEN -> BOOKED -> OPEN -> BOOKED.
    # Regression: both the first and the last booking must survive. Collapsing
    # to one per slot loses the churn; keeping only the last loses the lead time.
    _fort(
        SLOT_REBOOKED,
        "2026-09-15 21:00:00",
        "2026-09-15 21:30:00",
        "OOBBOOBBBB",
        "first and last booking both retained on a re-booked slot",
    ),
    # Venue pulled inventory: OPEN -> BLOCKED, most likely an offline sale.
    # Regression: must surface as blocked court-minutes, never be folded into
    # occupancy_strict, and never be read as "no demand".
    _fort(
        SLOT_OPEN_TO_BLOCKED,
        "2026-09-17 19:00:00",
        "2026-09-17 19:30:00",
        "OOOOXXXXXX",
        "OPEN -> BLOCKED surfaced separately from occupancy",
    ),
    # Court-minute normalization pair: same wall-clock hour, same business date,
    # different grids (60 vs 30 minutes), both OPEN throughout.
    # Regression: anything that counts slots instead of summing
    # duration_minutes makes these two compare equal (1 == 1) when the truth is
    # 60 court-minutes vs 30.
    _Track(
        SLOT_NORMALIZE_60,
        PADEL_UP_VENUE,
        PADEL_UP_COURT,
        "2026-09-16 19:00:00",
        "2026-09-16 20:00:00",
        PRICE_PADEL_UP_SLOT,
        "OOOOOOOOOO",
        "60-min grid contributes 60 court-minutes, not 1 slot",
    ),
    _fort(
        SLOT_NORMALIZE_30,
        "2026-09-16 19:00:00",
        "2026-09-16 19:30:00",
        "OOOOOOOOOO",
        "30-min grid contributes 30 court-minutes, not 1 slot",
    ),
    # Post-midnight sale: 00:30 on Saturday 2026-09-12 is Friday-night demand,
    # so business_date is 2026-09-11 while the local date stays 2026-09-12.
    # Regression: aggregating on the raw local date misattributes this booking
    # to the wrong trading day and the wrong day of week.
    _Track(
        SLOT_POST_MIDNIGHT,
        PLAY_PADEL_VENUE,
        PLAY_PADEL_COURT,
        "2026-09-12 00:30:00",
        "2026-09-12 01:00:00",
        PRICE_PLAY_PADEL_SLOT,
        "OOOOBBBBBB",
        "pre-04:00 slot rolls back to the previous business_date",
    ),
    # Already elapsed and still OPEN in every snapshot: Hudle never marks past
    # slots unavailable (verified at 16:21 IST on 2026-09-11).
    # Regression: pastness must come from is_past, never from is_available. Any
    # "still winnable" view that filters on state instead of is_past keeps this
    # slot; any day-occupancy denominator that drops it under-counts inventory.
    _fort(
        SLOT_ELAPSED_OPEN,
        "2026-09-11 07:00:00",
        "2026-09-11 07:30:00",
        "OOOOOOOOOO",
        "is_past is orthogonal to state",
    ),
    # The real blocked evening, unchanged across every snapshot.
    *(
        _fort(
            slot_uuid,
            start,
            (from_local_text(start) + dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            _X * SNAPSHOT_COUNT,
            "a whole evening of blocked inventory is reported, not hidden",
        )
        for slot_uuid, start in BLOCKED_EVENING_SLOTS
    ),
)


@dataclasses.dataclass(frozen=True, slots=True)
class SyntheticHistory:
    """A scripted multi-snapshot history plus the answers it should produce.

    ``storage`` is populated only by the ``synthetic_history_storage`` fixture.
    The plain ``synthetic_history`` fixture leaves it ``None`` so pure analytics
    tests can use the data without depending on a storage backend.
    """

    snapshots: tuple[SnapshotRecord, ...]
    observations: tuple[SlotObservation, ...]
    facility_fetches: tuple[FacilityFetch, ...]
    storage: Storage | None

    # -- schedule ------------------------------------------------------
    base_observed_at: dt.datetime = BASE_OBSERVED_AT
    cadence_minutes: int = CADENCE_MINUTES
    snapshot_count: int = SNAPSHOT_COUNT
    #: The 90-minute hole: no snapshot exists in [gap_start, gap_end).
    #: Nothing may interpolate across it.
    gap_start: dt.datetime = GAP_START
    gap_end: dt.datetime = GAP_END
    gap_minutes: int = GAP_MINUTES
    missing_snapshot_count: int = len(MISSING_TICKS)

    # -- normal booking ------------------------------------------------
    normal_slot_uuid: str = SLOT_NORMAL_BOOKING
    expected_first_booked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=180)
    expected_prev_seen_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=150)
    expected_uncertainty_minutes: int = CADENCE_MINUTES
    expected_lead_time_hours: float = 72.5

    # -- left censoring ------------------------------------------------
    censored_slot_uuids: frozenset[str] = frozenset({SLOT_CENSORED_FORT, SLOT_CENSORED_PLAY})

    # -- cancellation and re-booking -----------------------------------
    cancellation_slot_uuid: str = SLOT_CANCELLED
    cancellation_booked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=90)
    cancellation_released_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=180)
    rebooked_slot_uuid: str = SLOT_REBOOKED
    rebooked_first_booked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=60)
    rebooked_last_booked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=180)

    # -- blocking ------------------------------------------------------
    open_to_blocked_slot_uuid: str = SLOT_OPEN_TO_BLOCKED
    open_to_blocked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=120)
    blocked_evening_slot_uuids: frozenset[str] = frozenset(
        uuid for uuid, _ in BLOCKED_EVENING_SLOTS
    )
    blocked_evening_business_date: dt.date = dt.date(2026, 9, 13)
    expected_blocked_evening_minutes: int = 14 * 30

    # -- court-minute normalization ------------------------------------
    normalization_slot_uuids: tuple[str, str] = (SLOT_NORMALIZE_60, SLOT_NORMALIZE_30)
    normalization_business_date: dt.date = dt.date(2026, 9, 16)
    expected_normalization_minutes: tuple[int, int] = (60, 30)

    # -- business date -------------------------------------------------
    post_midnight_slot_uuid: str = SLOT_POST_MIDNIGHT
    post_midnight_local_date: dt.date = dt.date(2026, 9, 12)
    post_midnight_business_date: dt.date = dt.date(2026, 9, 11)
    post_midnight_booked_at: dt.datetime = BASE_OBSERVED_AT + dt.timedelta(minutes=120)

    # -- pastness ------------------------------------------------------
    elapsed_open_slot_uuid: str = SLOT_ELAPSED_OPEN

    def observed_at(self, position: int) -> dt.datetime:
        """The ``observed_at`` of the ``position``-th snapshot that exists."""
        return self.snapshots[position].observed_at

    def for_slot(self, slot_uuid: str) -> list[SlotObservation]:
        """This slot's observations, oldest first."""
        return [o for o in self.observations if o.slot_uuid == slot_uuid]

    def trajectory(self, slot_uuid: str) -> list[SlotState]:
        """This slot's state sequence, oldest first."""
        return [o.state for o in self.for_slot(slot_uuid)]


def _poll_key(observed_at: dt.datetime) -> str:
    floored = observed_at.replace(
        minute=(observed_at.minute // CADENCE_MINUTES) * CADENCE_MINUTES,
        second=0,
        microsecond=0,
    )
    return to_utc_text(floored)


def _build_history() -> SyntheticHistory:
    for track in _TRACKS:
        if len(track.states) != SNAPSHOT_COUNT:
            raise AssertionError(
                f"track {track.slot_uuid} has {len(track.states)} states, expected {SNAPSHOT_COUNT}"
            )

    snapshots: list[SnapshotRecord] = []
    observations: list[SlotObservation] = []
    fetches: list[FacilityFetch] = []

    for position, tick in enumerate(PRESENT_TICKS):
        snapshot_id = position + 1
        observed_at = BASE_OBSERVED_AT + dt.timedelta(minutes=tick * CADENCE_MINUTES)
        snapshots.append(
            SnapshotRecord(
                snapshot_id=snapshot_id,
                poll_key=_poll_key(observed_at),
                observed_at=observed_at,
                ok=True,
                error=None,
                duration_ms=1200,
                horizon_days=21,
            )
        )

        per_facility: dict[str, int] = {}
        for track in _TRACKS:
            start_local = from_local_text(track.start_local)
            end_local = from_local_text(track.end_local)
            state = _CODE_TO_STATE[track.states[position]]
            slot_start_utc = slot_start_utc_for(start_local, TZ)
            observations.append(
                SlotObservation(
                    snapshot_id=snapshot_id,
                    slot_uuid=track.slot_uuid,
                    venue_uuid=track.venue_uuid,
                    facility_uuid=track.facility_uuid,
                    sport=track.sport,
                    slot_start_local=track.start_local,
                    slot_end_local=track.end_local,
                    tz=TZ,
                    slot_start_utc=slot_start_utc,
                    duration_minutes=duration_minutes_for(start_local, end_local),
                    price=track.price,
                    total_count=1,
                    available_count=0 if state is SlotState.BOOKED else 1,
                    is_available=state is not SlotState.BLOCKED,
                    is_booked=state is SlotState.BOOKED,
                    state=state,
                    days_ahead=days_ahead_for(start_local, observed_at, TZ),
                    business_date=business_date_for(start_local, BUSINESS_DAY_START_HOUR),
                    is_past=slot_start_utc < observed_at,
                )
            )
            per_facility[track.facility_uuid] = per_facility.get(track.facility_uuid, 0) + 1

        for facility_uuid, slot_count in sorted(per_facility.items()):
            fetches.append(
                FacilityFetch(
                    snapshot_id=snapshot_id,
                    facility_uuid=facility_uuid,
                    ok=True,
                    http_status=200,
                    error=None,
                    duration_ms=350,
                    slot_count=slot_count,
                    attempts=1,
                )
            )

    return SyntheticHistory(
        snapshots=tuple(snapshots),
        observations=tuple(observations),
        facility_fetches=tuple(fetches),
        storage=None,
    )


@pytest.fixture(scope="session")
def synthetic_history() -> SyntheticHistory:
    """Scripted observation history with no storage attached.

    Use this for pure analytics: it is the observation list plus the expected
    answers, and it never needs a database.
    """
    return _build_history()


@pytest.fixture()
def synthetic_history_storage(
    memory_storage: Storage, synthetic_history: SyntheticHistory
) -> SyntheticHistory:
    """The same scripted history, loaded into an in-memory :class:`Storage`.

    Snapshot ids are remapped to whatever the backend assigned, so the returned
    observations always match what is actually in the database.
    """
    id_map: dict[int, int] = {}
    for snapshot in synthetic_history.snapshots:
        assigned = memory_storage.create_snapshot(
            snapshot.poll_key, snapshot.observed_at, snapshot.horizon_days
        )
        id_map[snapshot.snapshot_id] = assigned

    observations = tuple(
        dataclasses.replace(o, snapshot_id=id_map[o.snapshot_id])
        for o in synthetic_history.observations
    )
    memory_storage.append_observations(observations)

    fetches = tuple(
        dataclasses.replace(f, snapshot_id=id_map[f.snapshot_id])
        for f in synthetic_history.facility_fetches
    )
    for fetch in fetches:
        memory_storage.record_facility_fetch(fetch)

    snapshots = tuple(
        dataclasses.replace(s, snapshot_id=id_map[s.snapshot_id])
        for s in synthetic_history.snapshots
    )
    for snapshot in snapshots:
        memory_storage.finalize_snapshot(snapshot.snapshot_id, True, None, 1200)

    return dataclasses.replace(
        synthetic_history,
        snapshots=snapshots,
        observations=observations,
        facility_fetches=fetches,
        storage=memory_storage,
    )


@pytest.fixture()
def synthetic_observations(synthetic_history: SyntheticHistory) -> Iterator[SlotObservation]:
    """Just the observation stream, for analytics that take an iterable.

    Function-scoped on purpose: an iterator is consumed by whoever reads it.
    """
    return iter(synthetic_history.observations)
