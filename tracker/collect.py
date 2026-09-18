"""The collect cycle: fetch every active court's grid, classify it, store it.

This is the only module that turns a poll into rows. Everything it does is
shaped by one fact: **the dataset is forward-looking and cannot be backfilled.**
A cycle that loses a facility's slots loses them for good, so:

* Facilities are polled **sequentially**, one grid request each, covering the
  whole horizon in a single call. Six courts cost six requests per cycle.
* A failing facility is caught, recorded and stepped over. It never aborts the
  cycle and never discards another facility's observations.
* The snapshot's ``ok`` is true only when every active court succeeded, so a
  partial cycle is visible rather than silently averaged into the coverage
  numbers.
* Writes are idempotent on ``poll_key`` and ``(snapshot_id, slot_uuid)``, so
  re-running a cadence bucket -- a cron overlap, a manual retry -- converges on
  the same rows instead of doubling them.
* ``dry_run`` performs the whole cycle and touches no storage method at all,
  which is what makes it safe to point at a production database.

Two cross-checks run on every cycle because both failures are silent and both
corrupt cross-venue comparisons permanently:

* the observed grid granularity is compared against ``config.yaml`` (a venue
  moving from 60- to 30-minute slots would otherwise halve its apparent
  inventory with no error anywhere);
* every facility logs its counts *and* its court-minutes by state, so a
  plausibility check needs no database access. Padel Up legitimately reads
  ``booked=0`` forever; that has to be distinguishable at a glance from a
  broken collector, and the blocked/open split is what distinguishes it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

from tracker.analytics.leadtime import first_booked
from tracker.analytics.transitions import derive_transitions
from tracker.classify import (
    court_minutes_by_state,
    grid_minutes_from_payload,
    parse_slot_grid,
)
from tracker.config import Config, FacilityConfig, VenueConfig
from tracker.hudle import CircuitOpenError, HudleApiError, HudleError, HudleHttpError
from tracker.storage import Storage
from tracker.types import (
    FacilityDim,
    FacilityFetch,
    SlotFirstBooked,
    SlotObservation,
    SlotState,
    StateTransition,
    VenueDim,
    local_wall_clock,
    to_utc_text,
)

logger = logging.getLogger("tracker.collect")

#: Snapshot id used while classifying in ``dry_run``. No snapshot row exists,
#: so nothing may be written with it; it only satisfies the observation type.
DRY_RUN_SNAPSHOT_ID = 0

#: Slot uuids read per ``observations_for_slots`` call during a backfill.
BACKFILL_SLOT_CHUNK_SIZE = 500

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_FAR_FUTURE = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


class ExitCode(IntEnum):
    """Process exit codes, so a cron wrapper can tell the three cases apart.

    An :class:`~enum.IntEnum` rather than the project's usual ``StrEnum``
    because these values are handed straight to ``sys.exit``.
    """

    OK = 0
    PARTIAL = 1
    STOPPED = 2


# --------------------------------------------------------------------------
# Injected client
# --------------------------------------------------------------------------


class CircuitView(Protocol):
    """The read-only half of the client's breaker the collector consults."""

    @property
    def is_open(self) -> bool: ...

    @property
    def consecutive_failures(self) -> int: ...


class SlotsClient(Protocol):
    """What a collect cycle needs from the HTTP client.

    Structural on purpose: :class:`tracker.hudle.HudleClient` satisfies it, and
    so does a fake serving recorded fixtures, without either importing the
    other.
    """

    @property
    def circuit(self) -> CircuitView: ...

    def fetch_slots(
        self,
        venue_uuid: str,
        facility_uuid: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FacilityOutcome:
    """What one facility's leg of a cycle produced, successful or not."""

    venue_uuid: str
    facility_uuid: str
    label: str
    ok: bool
    http_status: int | None
    error: str | None
    duration_ms: int
    attempts: int
    counts: Mapping[SlotState, int]
    court_minutes: Mapping[SlotState, int]
    observed_grid_minutes: int | None
    configured_grid_minutes: int | None
    written: int
    circuit_open: bool = False

    @property
    def slot_count(self) -> int:
        return sum(self.counts.values())

    @property
    def grid_mismatch(self) -> bool:
        """True when Hudle's grid disagrees with the frozen config.

        ``None`` on either side is not a mismatch: the three pickleball courts
        are configured ``grid_minutes: null`` because nobody has probed them,
        and a facility with no slots at all reports no grid.
        """
        if self.observed_grid_minutes is None or self.configured_grid_minutes is None:
            return False
        return self.observed_grid_minutes != self.configured_grid_minutes

    def as_fetch(self, snapshot_id: int) -> FacilityFetch:
        """This outcome as the row that records it."""
        return FacilityFetch(
            snapshot_id=snapshot_id,
            facility_uuid=self.facility_uuid,
            ok=self.ok,
            http_status=self.http_status,
            error=self.error,
            duration_ms=self.duration_ms,
            slot_count=self.slot_count,
            attempts=self.attempts,
        )

    def log_fields(self) -> dict[str, Any]:
        """The event fields for ``collect_facility_done``.

        Counts *and* court-minutes, because slot counts are not comparable
        across venues: one 60-minute Padel Up slot is two Padel Fort slots.
        """
        return {
            "facility": self.label,
            "facility_uuid": self.facility_uuid,
            "ok": self.ok,
            "booked": self.counts[SlotState.BOOKED],
            "blocked": self.counts[SlotState.BLOCKED],
            "open": self.counts[SlotState.OPEN],
            "court_minutes_booked": self.court_minutes[SlotState.BOOKED],
            "court_minutes_blocked": self.court_minutes[SlotState.BLOCKED],
            "court_minutes_open": self.court_minutes[SlotState.OPEN],
            "grid": self.observed_grid_minutes,
            "slots": self.slot_count,
            "written": self.written,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "http_status": self.http_status,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class CollectResult:
    """The outcome of one whole cycle."""

    poll_key: str
    snapshot_id: int | None
    observed_at: dt.datetime
    start_date: dt.date
    end_date: dt.date
    horizon_days: int
    dry_run: bool
    circuit_open: bool
    duration_ms: int
    courts_planned: int
    error: str | None
    facilities: tuple[FacilityOutcome, ...] = ()

    @property
    def ok(self) -> bool:
        """True only when every planned court was fetched and parsed."""
        return len(self.facilities) == self.courts_planned and all(f.ok for f in self.facilities)

    @property
    def succeeded(self) -> tuple[FacilityOutcome, ...]:
        return tuple(f for f in self.facilities if f.ok)

    @property
    def failed(self) -> tuple[FacilityOutcome, ...]:
        return tuple(f for f in self.facilities if not f.ok)

    @property
    def skipped(self) -> int:
        """Courts never attempted, because the cycle stopped early."""
        return self.courts_planned - len(self.facilities)

    @property
    def written(self) -> int:
        return sum(f.written for f in self.facilities)

    @property
    def counts(self) -> Mapping[SlotState, int]:
        return _merge_by_state(f.counts for f in self.facilities)

    @property
    def court_minutes(self) -> Mapping[SlotState, int]:
        return _merge_by_state(f.court_minutes for f in self.facilities)

    @property
    def grid_mismatches(self) -> tuple[FacilityOutcome, ...]:
        return tuple(f for f in self.facilities if f.grid_mismatch)

    @property
    def exit_code(self) -> ExitCode:
        """0 clean, 1 some facility failed, 2 the cycle stopped or nothing worked."""
        if self.circuit_open or self.skipped:
            return ExitCode.STOPPED
        if not self.succeeded:
            return ExitCode.STOPPED
        if self.failed:
            return ExitCode.PARTIAL
        return ExitCode.OK

    def log_fields(self) -> dict[str, Any]:
        """The event fields for ``collect_run_done``."""
        return {
            "poll_key": self.poll_key,
            "snapshot_id": self.snapshot_id,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "facilities_ok": len(self.succeeded),
            "facilities_failed": len(self.failed),
            "facilities_skipped": self.skipped,
            "booked": self.counts[SlotState.BOOKED],
            "blocked": self.counts[SlotState.BLOCKED],
            "open": self.counts[SlotState.OPEN],
            "court_minutes_booked": self.court_minutes[SlotState.BOOKED],
            "court_minutes_blocked": self.court_minutes[SlotState.BLOCKED],
            "court_minutes_open": self.court_minutes[SlotState.OPEN],
            "slots": sum(self.counts.values()),
            "written": self.written,
            "days": self.horizon_days,
            "circuit_open": self.circuit_open,
            "duration_ms": self.duration_ms,
            "exit_code": int(self.exit_code),
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Cadence bucketing
# --------------------------------------------------------------------------


def floor_to_cadence(moment: dt.datetime, cadence_minutes: int) -> dt.datetime:
    """Floor an aware instant to the start of its cadence bucket, in UTC.

    Flooring is done on whole minutes since the epoch rather than on the
    minute-of-hour field, so a cadence that does not divide an hour (90
    minutes, say) still produces stable, non-overlapping buckets.
    """
    if moment.tzinfo is None:
        raise ValueError("poll instants must be timezone-aware")
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be positive")
    elapsed = int((moment.astimezone(dt.UTC) - _EPOCH).total_seconds() // 60)
    return _EPOCH + dt.timedelta(minutes=(elapsed // cadence_minutes) * cadence_minutes)


def poll_key_for(moment: dt.datetime, cadence_minutes: int) -> str:
    """The idempotency key for the cadence bucket ``moment`` falls in.

    Two runs inside one bucket -- an overlapping cron, an operator retry --
    share a key and therefore share a snapshot, so the second one appends
    nothing.
    """
    return to_utc_text(floor_to_cadence(moment, cadence_minutes))


def horizon_dates(
    moment: dt.datetime, tz: str, horizon_days: int, lookback_days: int = 0
) -> tuple[dt.date, dt.date]:
    """The inclusive local date range one cycle requests.

    Local, not UTC: Hudle's ``start_date``/``end_date`` are local calendar days,
    and at 23:00 UTC it is already tomorrow in Jaipur. The range reaches
    ``lookback_days`` behind today, because a date's final state is only
    knowable after it has fully elapsed and Hudle keeps serving it afterwards.
    """
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if lookback_days < 0:
        raise ValueError("lookback_days cannot be negative")
    today = local_wall_clock(moment, tz).date()
    start = today - dt.timedelta(days=lookback_days)
    return start, today + dt.timedelta(days=horizon_days - 1)


# --------------------------------------------------------------------------
# The cycle
# --------------------------------------------------------------------------


def run_collect(
    config: Config,
    storage: Storage,
    client: SlotsClient,
    *,
    now: dt.datetime,
    dry_run: bool = False,
) -> CollectResult:
    """Run one collect cycle and return what it did.

    ``now`` is required and must be aware: the collector never reads the clock
    itself, so a cycle is reproducible from its inputs.

    With ``dry_run`` the cycle fetches and classifies exactly as usual and calls
    **no** storage method -- not even ``create_snapshot``, which would otherwise
    leave an empty bucket behind and make the real run for that minute a no-op.
    """
    if now.tzinfo is None:
        raise ValueError("run_collect requires an aware `now`")

    started = time.monotonic()
    poll = config.poll
    poll_key = poll_key_for(now, poll.cadence_minutes)
    start_date, end_date = horizon_dates(
        now, config.timezone, poll.horizon_days, poll.lookback_days
    )
    courts = config.active_courts()

    snapshot_id = (
        DRY_RUN_SNAPSHOT_ID
        if dry_run
        else storage.create_snapshot(poll_key, now, poll.horizon_days)
    )
    if not dry_run:
        record_dimensions(config, storage, observed_at=now)
    logger.info(
        "collect_run_started",
        extra={
            "poll_key": poll_key,
            "snapshot_id": None if dry_run else snapshot_id,
            "dry_run": dry_run,
            "courts": len(courts),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days": poll.horizon_days,
        },
    )

    outcomes: list[FacilityOutcome] = []
    circuit_open = False
    for venue, facility in courts:
        if client.circuit.is_open:
            circuit_open = True
            logger.warning(
                "collect_cycle_halted",
                extra={
                    "poll_key": poll_key,
                    "reason": "circuit_open",
                    "next_facility": _label(venue, facility),
                    "consecutive_failures": client.circuit.consecutive_failures,
                },
            )
            break

        outcome, observations = _collect_facility(
            config,
            client,
            venue,
            facility,
            snapshot_id=snapshot_id,
            observed_at=now,
            start_date=start_date,
            end_date=end_date,
        )
        if not dry_run:
            if outcome.ok:
                outcome = dataclasses.replace(
                    outcome, written=storage.append_observations(observations)
                )
            storage.record_facility_fetch(outcome.as_fetch(snapshot_id))
        outcomes.append(outcome)

        if outcome.grid_mismatch:
            logger.warning(
                "collect_grid_mismatch",
                extra={
                    "facility": outcome.label,
                    "facility_uuid": outcome.facility_uuid,
                    "configured_grid_minutes": outcome.configured_grid_minutes,
                    "observed_grid_minutes": outcome.observed_grid_minutes,
                },
            )
        logger.info("collect_facility_done", extra=outcome.log_fields())

        if outcome.circuit_open:
            circuit_open = True
            break

    duration_ms = _elapsed_ms(started)
    result = CollectResult(
        poll_key=poll_key,
        snapshot_id=None if dry_run else snapshot_id,
        observed_at=now,
        start_date=start_date,
        end_date=end_date,
        horizon_days=poll.horizon_days,
        dry_run=dry_run,
        circuit_open=circuit_open,
        duration_ms=duration_ms,
        courts_planned=len(courts),
        error=_run_error(outcomes, circuit_open=circuit_open, planned=len(courts)),
        facilities=tuple(outcomes),
    )

    if not dry_run:
        storage.finalize_snapshot(snapshot_id, result.ok, result.error, duration_ms)

    log = logger.warning if not result.ok else logger.info
    log("collect_run_done", extra=result.log_fields())
    return result


def _collect_facility(
    config: Config,
    client: SlotsClient,
    venue: VenueConfig,
    facility: FacilityConfig,
    *,
    snapshot_id: int,
    observed_at: dt.datetime,
    start_date: dt.date,
    end_date: dt.date,
) -> tuple[FacilityOutcome, tuple[SlotObservation, ...]]:
    """Fetch and classify one facility. Never raises.

    Any failure becomes a recorded outcome so the cycle continues: another
    venue's slots are irreplaceable and must not be lost to this one's error.

    Returns the outcome and the observations it produced. The observations are
    handed back rather than written here so that one function decides whether
    anything is written at all, which is what makes ``dry_run`` verifiable.
    """
    label = _label(venue, facility)
    started = time.monotonic()
    observations: list[SlotObservation] = []
    observed_grid: int | None = None
    try:
        payload = client.fetch_slots(venue.uuid, facility.uuid, start_date, end_date)
        observations = parse_slot_grid(
            payload,
            snapshot_id=snapshot_id,
            observed_at=observed_at,
            venue_uuid=venue.uuid,
            facility_uuid=facility.uuid,
            sport=facility.sport,
            business_day_start_hour=config.business_day_start_hour,
            tz=config.timezone,
        )
        observed_grid = grid_minutes_from_payload(payload)
    except Exception as exc:
        # Deliberately broad: whatever went wrong with this facility, the other
        # five courts' slots are irreplaceable and must still be collected.
        failure = _describe_failure(exc, max_attempts=config.poll.backoff.max_attempts)
        logger.warning(
            "collect_facility_failed",
            extra={
                "facility": label,
                "facility_uuid": facility.uuid,
                "error": failure.error,
                "http_status": failure.http_status,
                "attempts": failure.attempts,
                "circuit_open": failure.circuit_open,
                "duration_ms": _elapsed_ms(started),
            },
        )
        return (
            FacilityOutcome(
                venue_uuid=venue.uuid,
                facility_uuid=facility.uuid,
                label=label,
                ok=False,
                http_status=failure.http_status,
                error=failure.error,
                duration_ms=_elapsed_ms(started),
                attempts=failure.attempts,
                counts=_zeroed(),
                court_minutes=_zeroed(),
                observed_grid_minutes=None,
                configured_grid_minutes=facility.grid_minutes,
                written=0,
                circuit_open=failure.circuit_open,
            ),
            (),
        )

    return (
        FacilityOutcome(
            venue_uuid=venue.uuid,
            facility_uuid=facility.uuid,
            label=label,
            ok=True,
            http_status=200,
            error=None,
            duration_ms=_elapsed_ms(started),
            attempts=1,
            counts=slot_counts_by_state(observations),
            court_minutes=court_minutes_by_state(observations),
            observed_grid_minutes=observed_grid,
            configured_grid_minutes=facility.grid_minutes,
            written=0,
            circuit_open=False,
        ),
        tuple(observations),
    )


def record_dimensions(config: Config, storage: Storage, *, observed_at: dt.datetime) -> None:
    """Mirror the configured venues and facilities into the dimension tables.

    Called every cycle so ``last_seen`` tracks the poll, while the stored
    ``first_seen`` always wins on conflict. That pair is what makes "when did a
    fourth venue appear" answerable from the data months later rather than only
    from a drift alert someone happened to read at the time.

    Every facility is recorded, equipment included: the table then describes
    what we decided about the world, not merely what we polled, so a facility
    reclassified later is still explicable from history.
    """
    for venue in config.venues:
        storage.upsert_venue_dim(
            VenueDim(
                venue_uuid=venue.uuid,
                name=venue.name,
                short_name=venue.short_name,
                slug=venue.slug,
                numeric_id=venue.numeric_id,
                tz=config.timezone,
                active=venue.active,
                first_seen=observed_at,
                last_seen=observed_at,
            )
        )
        for facility in venue.facilities:
            storage.upsert_facility_dim(
                FacilityDim(
                    facility_uuid=facility.uuid,
                    venue_uuid=venue.uuid,
                    name=facility.name,
                    kind=facility.kind,
                    sport=facility.sport,
                    grid_minutes=facility.grid_minutes,
                    active=facility.active,
                    first_seen=observed_at,
                    last_seen=observed_at,
                )
            )


def slot_counts_by_state(observations: Iterable[SlotObservation]) -> dict[SlotState, int]:
    """Slot counts per state, with all three states always present.

    Reported beside court-minutes, never instead of them: counts answer "how
    many rows did this poll see", court-minutes answer every question that
    compares one venue to another.
    """
    counts = dict.fromkeys(SlotState, 0)
    for observation in observations:
        counts[observation.state] += 1
    return counts


# --------------------------------------------------------------------------
# Scheduling support
# --------------------------------------------------------------------------


class ConsecutiveFailureTracker:
    """Counts consecutive failed cycles so ``--loop`` stops instead of hammering.

    A single bad cycle is normal -- Hudle has a bad minute, the laptop's wifi
    drops. A run of them means something is wrong that more requests will not
    fix, and the polite thing is to stop and let a human look.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._consecutive_failures = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def exhausted(self) -> bool:
        return self._consecutive_failures >= self._limit

    def record(self, *, ok: bool) -> None:
        self._consecutive_failures = 0 if ok else self._consecutive_failures + 1


# --------------------------------------------------------------------------
# Derived tables
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """What one ``backfill-derived`` pass recomputed."""

    slots: int
    booked_slots: int
    transitions: int
    first_booked: int


def backfill_derived(
    storage: Storage, *, slot_chunk_size: int = BACKFILL_SLOT_CHUNK_SIZE
) -> BackfillResult:
    """Recompute both derived tables from ``slot_observations``.

    The derivation itself is not here: it is
    :func:`tracker.analytics.transitions.derive_transitions` and
    :func:`tracker.analytics.leadtime.first_booked`, the same pure functions the
    dashboard reads through. A second copy in this module would eventually
    disagree with the dashboard about what "left-censored" means, and the table
    and the chart would quietly tell different stories. This function is only
    the I/O around them: read the observations, hand them over, write the rows.

    Derived tables are disposable; ``slot_observations`` is not. Rebuilding from
    scratch is therefore always correct, and is how a fixed derivation ships.

    Slots are read in chunks so a year of observations never has to fit in
    memory at once. Each table is replaced inside its own transaction by the
    backend, so neither is left half-written; the pair is not one transaction,
    because the ``Storage`` protocol has no multi-table unit of work, so a crash
    between the two leaves two internally consistent tables that disagree until
    the next run. Re-running fixes it, and nothing observed is ever at risk.
    """
    snapshots = storage.snapshots_between(_EPOCH, _FAR_FUTURE)
    slot_uuids = [
        str(row["slot_uuid"])
        for row in storage.query_rows(
            "SELECT DISTINCT slot_uuid FROM slot_observations ORDER BY slot_uuid"
        )
    ]

    transitions: list[StateTransition] = []
    bookings: list[SlotFirstBooked] = []
    for chunk in _chunked(slot_uuids, slot_chunk_size):
        observations = storage.observations_for_slots(chunk)
        chunk_transitions = derive_transitions(observations, snapshots)
        transitions.extend(chunk_transitions)
        bookings.extend(
            booking.as_row() for booking in first_booked(chunk_transitions, observations)
        )

    written_transitions = storage.replace_derived_transitions(transitions)
    written_first_booked = storage.replace_derived_first_booked(bookings)
    result = BackfillResult(
        slots=len(slot_uuids),
        booked_slots=len(bookings),
        transitions=written_transitions,
        first_booked=written_first_booked,
    )
    logger.info(
        "backfill_derived_done",
        extra={
            "slots": result.slots,
            "booked_slots": result.booked_slots,
            "transitions": result.transitions,
            "first_booked": result.first_booked,
        },
    )
    return result


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Failure:
    """How one facility's request failed, mapped onto the fetch row's columns."""

    error: str
    http_status: int | None
    attempts: int
    circuit_open: bool


def _describe_failure(exc: BaseException, *, max_attempts: int) -> _Failure:
    """Classify an exception into the columns ``facility_fetches`` records.

    ``attempts`` is inferred rather than reported: the client retries inside
    one call and exposes no counter. It exhausts its budget before raising a
    transport or HTTP error, never retries an envelope-level API error, and
    sends nothing at all when the breaker is open.
    """
    if isinstance(exc, CircuitOpenError):
        return _Failure(str(exc), None, 0, True)
    if isinstance(exc, HudleHttpError):
        return _Failure(str(exc), exc.status, max_attempts, False)
    if isinstance(exc, HudleApiError):
        return _Failure(str(exc), None, 1, False)
    if isinstance(exc, HudleError):
        return _Failure(str(exc), None, max_attempts, False)
    return _Failure(f"{type(exc).__name__}: {exc}", None, 1, False)


def _run_error(
    outcomes: Sequence[FacilityOutcome], *, circuit_open: bool, planned: int
) -> str | None:
    """A one-line summary of why a cycle was not clean, or ``None`` if it was."""
    failed = [o for o in outcomes if not o.ok]
    skipped = planned - len(outcomes)
    if not failed and not skipped and not circuit_open:
        return None
    parts = [f"{o.label}: {o.error}" for o in failed]
    if circuit_open:
        parts.append("circuit open, cycle halted")
    if skipped:
        parts.append(f"{skipped} facilities not attempted")
    return "; ".join(parts)


def _label(venue: VenueConfig, facility: FacilityConfig) -> str:
    """``"Padel Fort/Padel Court"`` -- readable in a log line at a glance."""
    return f"{venue.short_name}/{facility.name}"


def _zeroed() -> dict[SlotState, int]:
    return dict.fromkeys(SlotState, 0)


def _merge_by_state(
    tallies: Iterable[Mapping[SlotState, int]],
) -> Mapping[SlotState, int]:
    total = _zeroed()
    for tally in tallies:
        for state, value in tally.items():
            total[state] += value
    return total


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
