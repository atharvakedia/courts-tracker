"""State-change analytics: what happened to a slot, and when we could tell.

This module is pure. No I/O, no database handle, no HTTP, no
:func:`datetime.now`. Everything is derived from the observation stream and the
snapshot schedule the caller passes in.

This is what makes the append-only design worth its storage cost. Hudle never
tells us when a slot was sold; it only tells us what a slot looks like right
now. A booking *time* therefore has to be inferred from the first poll in which
the state flipped, which is only possible because every poll is kept.

**Nothing here is an exact time.** The real change happened somewhere inside the
gap between the poll that last saw the old state and the poll that first saw the
new one. Every :class:`~tracker.types.StateTransition` carries
``uncertainty_minutes`` for exactly that reason, and a missed poll widens it: a
transition seen across the 90-minute hole left by two skipped polls is a
90-minute window, not a confident 30-minute one. Any presentation that drops
``uncertainty_minutes`` is claiming a precision the data does not have.

**A slot's first sighting is a transition too.** It is emitted with
``from_state=None`` and ``uncertainty_minutes=None``, because we cannot say what
the slot was before we started looking. That row is load-bearing twice over: it
is how :mod:`tracker.analytics.leadtime` detects a left-censored booking, and it
is how a block that pre-dates our data -- Padel Fort's whole 2026-09-13 evening,
BLOCKED in every snapshot we ever took -- is still reported as blocked inventory
instead of vanishing for want of an ``OPEN -> BLOCKED`` edge.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from tracker.types import (
    SlotObservation,
    SlotState,
    SnapshotRecord,
    StateTransition,
)

logger = logging.getLogger("tracker.analytics.transitions")

MINUTES_PER_HOUR = 60

#: The state changes that are events in their own right, and what each means.
BOOKING = (SlotState.OPEN, SlotState.BOOKED)
CANCELLATION = (SlotState.BOOKED, SlotState.OPEN)
BLOCKING = (SlotState.OPEN, SlotState.BLOCKED)
UNBLOCKING = (SlotState.BLOCKED, SlotState.OPEN)


class TransitionError(ValueError):
    """An observation stream could not be turned into a transition history.

    Raised loudly rather than skipped. A slot whose snapshot is unknown, or a
    transition whose slot never appears in the observation stream, means the two
    inputs describe different collections, and silently dropping the row would
    put a permanent hole in a dataset that cannot be re-collected.
    """


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancellationRate:
    """Cancellation churn for one venue in one ISO week, with its denominators.

    ``rate`` is ``None`` -- never ``0.0`` -- when ``bookings`` is zero, so a week
    nobody booked is distinguishable from a week nobody cancelled.

    Both denominators are carried because they answer different questions.
    ``bookings`` counts *observed* booking events (``OPEN -> BOOKED``), so a slot
    booked, released and re-booked contributes two. ``booked_slots`` counts
    distinct slots ever seen BOOKED, including the ``censored_slots`` that were
    already booked when we first looked and whose booking event we therefore
    never saw. Dividing by the wrong one silently changes the metric.

    ``bookings`` and ``cancellations`` count booked slot-*events*, not customer
    bookings, and a slot-event is grid-dependent: one customer booking and then
    cancelling one hour is two events at Padel Fort's 30-minute grid and one at
    Padel Up's 60-minute grid. The ``rate`` survives that -- both sides scale
    together -- but a chart of ``bookings`` or ``cancellations`` across venues
    does not, so ``booked_court_minutes`` and ``cancelled_court_minutes`` are
    carried beside them and are the only cross-venue bars to draw.
    """

    venue_uuid: str
    iso_year: int
    iso_week: int
    week_start: dt.date
    bookings: int
    cancellations: int
    booked_court_minutes: int
    cancelled_court_minutes: int
    booked_slots: int
    censored_slots: int
    rate: float | None

    @property
    def booked_court_hours(self) -> float:
        return self.booked_court_minutes / MINUTES_PER_HOUR

    @property
    def cancelled_court_hours(self) -> float:
        return self.cancelled_court_minutes / MINUTES_PER_HOUR


@dataclass(frozen=True, slots=True)
class BlockedInventoryEvent:
    """One contiguous run of court time a venue pulled from sale.

    Padel Fort blocked 17:00-23:30 on 2026-09-13 with zero bookings: 14
    consecutive 30-minute slots, 420 court-minutes. That is *one* decision by one
    venue, and reporting it as 14 separate events would make a single evening
    look like a fortnight of churn. Runs are merged when they sit in the same
    facility, abut in time, and were first seen blocked in the same poll -- the
    signature of one action rather than a coincidence.

    ``censored_left`` means every slot in the run was already BLOCKED the first
    time we saw it, so the block pre-dates our data and ``uncertainty_minutes``
    is unknowable rather than merely wide.
    """

    venue_uuid: str
    facility_uuid: str
    business_date: dt.date
    slot_count: int
    court_minutes: int
    start_utc: dt.datetime
    end_utc: dt.datetime
    first_seen_at: dt.datetime
    prev_seen_at: dt.datetime | None
    uncertainty_minutes: int | None
    censored_left: bool
    slot_uuids: tuple[str, ...]

    @property
    def court_hours(self) -> float:
        """Court-minutes as court-hours, the only cross-venue display unit."""
        return self.court_minutes / MINUTES_PER_HOUR


# --------------------------------------------------------------------------
# Transition derivation
# --------------------------------------------------------------------------


def derive_transitions(
    observations: Iterable[SlotObservation],
    snapshots: Iterable[SnapshotRecord],
) -> list[StateTransition]:
    """Reduce an observation stream to the moments each slot changed state.

    ``snapshots`` is required because a :class:`~tracker.types.SlotObservation`
    carries only its ``snapshot_id``: the wall-clock instant of a poll lives on
    the snapshot, and ordering a slot's history by ``snapshot_id`` alone would
    assume ids are handed out in chronological order. Ordering is by the
    snapshot's ``observed_at``.

    Every slot yields a first-sighting row with ``from_state=None``,
    ``prev_seen_at=None`` and ``uncertainty_minutes=None``. Real changes follow,
    each carrying the gap between the last poll that saw the old state and the
    first poll that saw the new one. Callers wanting only genuine changes filter
    on ``from_state is not None``; callers reasoning about what pre-dates the
    dataset need the first-sighting rows.

    The result is sorted by ``first_seen_at``, then by facility and slot start,
    so a run of slots blocked in the same poll arrives in court-time order.
    """
    observed_at_by_snapshot = _observed_at_index(snapshots)

    by_slot: dict[str, list[SlotObservation]] = defaultdict(list)
    for observation in observations:
        if observation.snapshot_id not in observed_at_by_snapshot:
            raise TransitionError(
                f"observation of slot {observation.slot_uuid} references unknown "
                f"snapshot {observation.snapshot_id}"
            )
        by_slot[observation.slot_uuid].append(observation)

    transitions: list[StateTransition] = []
    for slot_observations in by_slot.values():
        slot_observations.sort(
            key=lambda o: (observed_at_by_snapshot[o.snapshot_id], o.snapshot_id)
        )
        previous: SlotObservation | None = None
        for observation in slot_observations:
            if previous is None:
                transitions.append(_transition(observation, None, None, observed_at_by_snapshot))
            elif observation.state is not previous.state:
                transitions.append(
                    _transition(
                        observation,
                        previous.state,
                        observed_at_by_snapshot[previous.snapshot_id],
                        observed_at_by_snapshot,
                    )
                )
            previous = observation

    transitions.sort(
        key=lambda t: (t.first_seen_at, t.facility_uuid, t.slot_start_utc, t.slot_uuid)
    )

    changes = sum(1 for t in transitions if t.from_state is not None)
    logger.info(
        "transitions_derived",
        extra={
            "slot_count": len(by_slot),
            "snapshot_count": len(observed_at_by_snapshot),
            "first_sightings": len(transitions) - changes,
            "state_changes": changes,
        },
    )
    return transitions


def transitions_between(
    transitions: Iterable[StateTransition],
    from_state: SlotState,
    to_state: SlotState,
) -> list[StateTransition]:
    """The transitions matching one specific edge, e.g. ``OPEN -> BOOKED``.

    First-sighting rows (``from_state is None``) never match, which is the point:
    a slot that was already booked when we first looked did not produce an
    observed booking event and must not be counted as one.
    """
    return [t for t in transitions if t.from_state is from_state and t.to_state is to_state]


# --------------------------------------------------------------------------
# Cancellation churn
# --------------------------------------------------------------------------


def cancellation_rate(
    transitions: Iterable[StateTransition],
    observations: Iterable[SlotObservation],
) -> list[CancellationRate]:
    """Cancellations per observed booking, per venue, per ISO week.

    Weeks are keyed on the slot's ``business_date``, not on when the booking
    happened, so a Friday-night session sold on Wednesday counts against the
    trading week whose inventory actually churned. ``business_date`` already
    rolls Play Padel's 00:30 Saturday sales back onto the Friday that produced
    them, so a post-midnight cancellation lands in the right week.

    Rows are returned for every (venue, week) that saw either a booking or a
    cancellation, sorted by venue then week, each carrying its own denominators.

    Event counts come back beside the court-minutes they represent. Plot the
    court-minutes across venues: a 60-minute grid cannot express a half-hour
    cancellation at all, so the event counts are comparable only within a venue.
    """
    static = slot_static_index(observations)
    buckets: dict[tuple[str, int, int], _WeekTally] = {}

    for transition in transitions:
        observation = _lookup(static, transition.slot_uuid)
        iso = observation.business_date.isocalendar()
        tally = buckets.setdefault(
            (transition.venue_uuid, iso.year, iso.week),
            _WeekTally(week_start=observation.business_date - dt.timedelta(days=iso.weekday - 1)),
        )
        edge = (transition.from_state, transition.to_state)
        if edge == BOOKING:
            tally.bookings += 1
            tally.booked_court_minutes += observation.duration_minutes
            tally.booked_slots.add(transition.slot_uuid)
        elif edge == CANCELLATION:
            tally.cancellations += 1
            tally.cancelled_court_minutes += observation.duration_minutes
            tally.booked_slots.add(transition.slot_uuid)
        elif transition.from_state is None and transition.to_state is SlotState.BOOKED:
            tally.booked_slots.add(transition.slot_uuid)
            tally.censored_slots.add(transition.slot_uuid)

    rows = [
        CancellationRate(
            venue_uuid=venue_uuid,
            iso_year=iso_year,
            iso_week=iso_week,
            week_start=tally.week_start,
            bookings=tally.bookings,
            cancellations=tally.cancellations,
            booked_court_minutes=tally.booked_court_minutes,
            cancelled_court_minutes=tally.cancelled_court_minutes,
            booked_slots=len(tally.booked_slots),
            censored_slots=len(tally.censored_slots),
            rate=(tally.cancellations / tally.bookings) if tally.bookings else None,
        )
        for (venue_uuid, iso_year, iso_week), tally in buckets.items()
        if tally.bookings or tally.cancellations
    ]
    rows.sort(key=lambda r: (r.venue_uuid, r.iso_year, r.iso_week))
    return rows


# --------------------------------------------------------------------------
# Blocked inventory
# --------------------------------------------------------------------------


def blocked_inventory_events(
    transitions: Iterable[StateTransition],
    observations: Iterable[SlotObservation],
) -> list[BlockedInventoryEvent]:
    """Episodes of court time pulled from sale, merged into contiguous runs.

    Both routes into BLOCKED count. ``OPEN -> BLOCKED`` is a venue withdrawing
    inventory while we watched -- most often an offline or phone booking. A slot
    that was BLOCKED at its first sighting is the same withdrawal made before we
    started looking, and dropping it would hide Padel Fort's 2026-09-13 evening
    entirely, since we never saw those 14 slots open.

    Adjacent slots merge into one event when they share a facility, were first
    seen blocked in the same poll, and abut in time (one slot's end is the next
    slot's start, measured from each slot's own ``duration_minutes`` so a
    30-minute and a 60-minute grid both work).

    Returned in court-time order, and never folded into occupancy: blocked time
    is neither sold nor sellable, and a venue that blocks to sell offline
    otherwise reads as a venue with no demand.
    """
    static = slot_static_index(observations)

    episodes: dict[str, list[_BlockedEpisode]] = defaultdict(list)
    for transition in transitions:
        if transition.to_state is not SlotState.BLOCKED:
            continue
        if transition.from_state is not None and transition.from_state is not SlotState.OPEN:
            continue
        observation = _lookup(static, transition.slot_uuid)
        episodes[transition.facility_uuid].append(
            _BlockedEpisode(
                slot_uuid=transition.slot_uuid,
                venue_uuid=transition.venue_uuid,
                facility_uuid=transition.facility_uuid,
                business_date=observation.business_date,
                duration_minutes=observation.duration_minutes,
                start_utc=transition.slot_start_utc,
                end_utc=transition.slot_start_utc
                + dt.timedelta(minutes=observation.duration_minutes),
                first_seen_at=transition.first_seen_at,
                prev_seen_at=transition.prev_seen_at,
                uncertainty_minutes=transition.uncertainty_minutes,
                censored_left=transition.from_state is None,
            )
        )

    events: list[BlockedInventoryEvent] = []
    for facility_episodes in episodes.values():
        facility_episodes.sort(key=lambda e: (e.first_seen_at, e.start_utc))
        run: list[_BlockedEpisode] = []
        for episode in facility_episodes:
            if run and _abuts(run[-1], episode):
                run.append(episode)
                continue
            if run:
                events.append(_event(run))
            run = [episode]
        if run:
            events.append(_event(run))

    events.sort(key=lambda e: (e.business_date, e.facility_uuid, e.start_utc))
    logger.info(
        "blocked_inventory_events_derived",
        extra={
            "event_count": len(events),
            "court_minutes": sum(e.court_minutes for e in events),
            "censored_events": sum(1 for e in events if e.censored_left),
        },
    )
    return events


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _WeekTally:
    """Mutable accumulator for one (venue, ISO week) cancellation bucket."""

    week_start: dt.date
    bookings: int = 0
    cancellations: int = 0
    booked_court_minutes: int = 0
    cancelled_court_minutes: int = 0
    booked_slots: set[str] = field(default_factory=set)
    censored_slots: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _BlockedEpisode:
    """One slot's entry into BLOCKED, before neighbouring slots are merged."""

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    business_date: dt.date
    duration_minutes: int
    start_utc: dt.datetime
    end_utc: dt.datetime
    first_seen_at: dt.datetime
    prev_seen_at: dt.datetime | None
    uncertainty_minutes: int | None
    censored_left: bool


def _observed_at_index(snapshots: Iterable[SnapshotRecord]) -> dict[int, dt.datetime]:
    """Map ``snapshot_id`` to the aware UTC instant the poll ran."""
    index: dict[int, dt.datetime] = {}
    for snapshot in snapshots:
        if snapshot.observed_at.tzinfo is None:
            raise TransitionError(
                f"snapshot {snapshot.snapshot_id} has a naive observed_at; "
                "snapshot instants must be timezone-aware UTC"
            )
        index[snapshot.snapshot_id] = snapshot.observed_at
    if not index:
        raise TransitionError("no snapshots supplied; observation times are unknowable")
    return index


def slot_static_index(observations: Iterable[SlotObservation]) -> dict[str, SlotObservation]:
    """One representative observation per slot, for its unchanging fields.

    ``duration_minutes``, ``business_date``, ``slot_start_local`` and the venue
    and facility identities are properties of the slot, not of the poll, so any
    observation of it answers for all of them.

    Public because :mod:`tracker.analytics.leadtime` needs the same reduction
    and needs it to mean the same thing: it reads a booking's slot metadata out
    of this index while the transition rows it joins against were built from
    that index here, and two independent "pick a representative" rules would be
    free to pick different observations of the same slot.
    """
    index: dict[str, SlotObservation] = {}
    for observation in observations:
        index.setdefault(observation.slot_uuid, observation)
    return index


def _lookup(index: Mapping[str, SlotObservation], slot_uuid: str) -> SlotObservation:
    try:
        return index[slot_uuid]
    except KeyError as exc:
        raise TransitionError(
            f"slot {slot_uuid} has a transition but no observation; "
            "the transition and observation streams describe different collections"
        ) from exc


def _transition(
    observation: SlotObservation,
    from_state: SlotState | None,
    prev_seen_at: dt.datetime | None,
    observed_at_by_snapshot: Mapping[int, dt.datetime],
) -> StateTransition:
    first_seen_at = observed_at_by_snapshot[observation.snapshot_id]
    return StateTransition(
        slot_uuid=observation.slot_uuid,
        venue_uuid=observation.venue_uuid,
        facility_uuid=observation.facility_uuid,
        from_state=from_state,
        to_state=observation.state,
        first_seen_at=first_seen_at,
        prev_seen_at=prev_seen_at,
        uncertainty_minutes=_uncertainty_minutes(first_seen_at, prev_seen_at),
        slot_start_utc=observation.slot_start_utc,
        days_ahead_at_change=observation.days_ahead,
    )


def _uncertainty_minutes(
    first_seen_at: dt.datetime, prev_seen_at: dt.datetime | None
) -> int | None:
    """Width of the poll gap the true change happened inside.

    ``None`` for a first sighting: there is no earlier poll, so the change could
    have happened at any time before collection started. A missed poll widens
    this -- two skipped polls at a 30-minute cadence make it 90, and reporting
    the nominal 30 would claim a confidence the gap does not support.
    """
    if prev_seen_at is None:
        return None
    return int((first_seen_at - prev_seen_at).total_seconds() // 60)


def _abuts(previous: _BlockedEpisode, current: _BlockedEpisode) -> bool:
    """Whether ``current`` continues ``previous`` as one venue action."""
    return (
        previous.first_seen_at == current.first_seen_at
        and previous.censored_left == current.censored_left
        and previous.end_utc == current.start_utc
    )


def _event(run: Sequence[_BlockedEpisode]) -> BlockedInventoryEvent:
    """Collapse one contiguous run of blocked slots into a single event."""
    uncertainties = [e.uncertainty_minutes for e in run if e.uncertainty_minutes is not None]
    prev_seen = [e.prev_seen_at for e in run if e.prev_seen_at is not None]
    return BlockedInventoryEvent(
        venue_uuid=run[0].venue_uuid,
        facility_uuid=run[0].facility_uuid,
        business_date=run[0].business_date,
        slot_count=len(run),
        court_minutes=sum(e.duration_minutes for e in run),
        start_utc=run[0].start_utc,
        end_utc=run[-1].end_utc,
        first_seen_at=run[0].first_seen_at,
        prev_seen_at=min(prev_seen) if prev_seen else None,
        uncertainty_minutes=max(uncertainties) if uncertainties else None,
        censored_left=all(e.censored_left for e in run),
        slot_uuids=tuple(e.slot_uuid for e in run),
    )
