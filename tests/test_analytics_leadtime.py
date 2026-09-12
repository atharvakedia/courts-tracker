"""Regression tests for state-transition and booking-lead-time analytics.

Everything here runs against the scripted ``synthetic_history`` fixture, whose
trajectories were written for exactly these questions, or against hand-built
samples with an arithmetically known answer. No network, no database, no
``datetime.now()``.

The fixture's schedule matters throughout: twelve 30-minute cadence ticks from
2026-09-11 10:00Z with ticks 8 and 9 deliberately missing, leaving ten snapshots
and one 90-minute hole between 13:30Z and 15:00Z. Positions map to instants as

    position  0     1     2     3     4     5     6     7     8     9
    observed  10:00 10:30 11:00 11:30 12:00 12:30 13:00 13:30 15:00 15:30
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from tests.conftest import (
    BASE_OBSERVED_AT,
    PADEL_FORT_COURT,
    PADEL_FORT_VENUE,
    PLAY_PADEL_VENUE,
    SyntheticHistory,
)
from tracker.analytics.leadtime import (
    DAY_NAMES,
    DEFAULT_MIN_OBSERVATION_HOURS,
    PeakBucket,
    SlotBooking,
    first_booked,
    first_slot_to_go,
    lead_time_distribution,
    time_to_sellout,
)
from tracker.analytics.transitions import (
    BLOCKING,
    BOOKING,
    CANCELLATION,
    UNBLOCKING,
    TransitionError,
    blocked_inventory_events,
    cancellation_rate,
    derive_transitions,
    transitions_between,
)
from tracker.config import Config
from tracker.types import (
    SlotObservation,
    SlotState,
    SnapshotRecord,
    StateTransition,
)

PEAK_HOURS = (18, 19, 20, 21, 22)

_CODE_TO_STATE = {"O": SlotState.OPEN, "B": SlotState.BOOKED, "X": SlotState.BLOCKED}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _at(minutes: int) -> dt.datetime:
    """The instant ``minutes`` after the fixture's first snapshot."""
    return BASE_OBSERVED_AT + dt.timedelta(minutes=minutes)


def _rescript(observations: list[SlotObservation], states: str) -> list[SlotObservation]:
    """Re-script one slot's trajectory, keeping its real times and snapshot ids.

    The fixture's ten snapshot times -- including the 90-minute hole -- are
    preserved; only the state (and the raw flags that must agree with it) is
    replaced. This is how a transition can be placed on a chosen poll without
    inventing a schedule that does not exist.
    """
    assert len(states) == len(observations)
    rescripted: list[SlotObservation] = []
    for observation, code in zip(observations, states, strict=True):
        state = _CODE_TO_STATE[code]
        rescripted.append(
            dataclasses.replace(
                observation,
                state=state,
                is_booked=state is SlotState.BOOKED,
                is_available=state is not SlotState.BLOCKED,
                available_count=0 if state is SlotState.BOOKED else 1,
            )
        )
    return rescripted


def _booking(
    *,
    slot_uuid: str,
    lead_time_hours: float | None,
    slot_start_hour: int = 19,
    business_date: dt.date = dt.date(2026, 9, 14),
    censored_left: bool = False,
    post_start: bool = False,
) -> SlotBooking:
    """A hand-built booking record, for arithmetic with a known answer."""
    slot_start_utc = dt.datetime(2026, 9, 14, 13, 30, tzinfo=dt.UTC)
    first_booked_at = None if lead_time_hours is None else _at(0)
    return SlotBooking(
        slot_uuid=slot_uuid,
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        slot_start_utc=slot_start_utc,
        slot_start_hour=slot_start_hour,
        business_date=business_date,
        duration_minutes=30,
        first_observed_at=_at(0),
        first_booked_at=first_booked_at,
        last_booked_at=first_booked_at,
        lead_time_hours=lead_time_hours,
        raw_lead_time_hours=lead_time_hours,
        uncertainty_minutes=30,
        observation_window_hours=100.0,
        booking_count=0 if censored_left else 1,
        cancellation_count=0,
        censored_left=censored_left,
        rebooked=False,
        cancelled=False,
        post_start=post_start,
    )


@pytest.fixture()
def transitions(synthetic_history: SyntheticHistory) -> list[StateTransition]:
    """The whole scripted history reduced to transitions."""
    return derive_transitions(synthetic_history.observations, synthetic_history.snapshots)


@pytest.fixture()
def bookings(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> list[SlotBooking]:
    """Per-slot booking records for every slot the fixture ever books."""
    return first_booked(transitions, synthetic_history.observations)


def _by_slot(bookings: list[SlotBooking]) -> dict[str, SlotBooking]:
    return {booking.slot_uuid: booking for booking in bookings}


# --------------------------------------------------------------------------
# derive_transitions
# --------------------------------------------------------------------------


def test_normal_booking_is_anchored_on_the_first_booked_snapshot(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: a booking time read from the wrong end of the poll gap.

    The normal slot is OPEN for six polls then BOOKED. The transition must be
    stamped with the FIRST poll that saw BOOKED (13:00Z), not the last one that
    saw OPEN and not the run start, and it must carry the 30-minute cadence as
    its uncertainty window.
    """
    booked = [
        t
        for t in transitions
        if t.slot_uuid == synthetic_history.normal_slot_uuid and t.from_state is not None
    ]
    assert len(booked) == 1
    transition = booked[0]
    assert (transition.from_state, transition.to_state) == BOOKING
    assert transition.first_seen_at == synthetic_history.expected_first_booked_at
    assert transition.first_seen_at == _at(180)
    assert transition.prev_seen_at == synthetic_history.expected_prev_seen_at
    assert transition.uncertainty_minutes == synthetic_history.expected_uncertainty_minutes
    assert transition.uncertainty_minutes == 30
    assert transition.days_ahead_at_change == 3


def test_every_slot_gets_a_first_sighting_with_no_uncertainty(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: silently inventing a prior state for a slot's first sighting.

    We cannot know what a slot was before collection started. The first sighting
    carries ``from_state=None``, ``prev_seen_at=None`` and
    ``uncertainty_minutes=None`` -- not a fabricated OPEN and not a zero-width
    window -- and there is exactly one per slot.
    """
    first_sightings = [t for t in transitions if t.from_state is None]
    slot_uuids = {o.slot_uuid for o in synthetic_history.observations}
    assert len(first_sightings) == len(slot_uuids)
    assert {t.slot_uuid for t in first_sightings} == slot_uuids
    for sighting in first_sightings:
        assert sighting.prev_seen_at is None
        assert sighting.uncertainty_minutes is None
        assert sighting.first_seen_at == synthetic_history.observed_at(0)


def test_transition_count_matches_the_scripted_trajectories(
    transitions: list[StateTransition],
) -> None:
    """Regression: a state change collapsed away or double-counted.

    The fixture scripts exactly eight real changes: one booking (normal), a
    booking plus a cancellation (cancelled), two bookings plus a cancellation
    (rebooked), one blocking, and one post-midnight booking. Anything that
    re-emits a transition per snapshot, or drops a repeated state change on the
    same slot, breaks this count.
    """
    changes = [t for t in transitions if t.from_state is not None]
    assert len(changes) == 8
    assert len(transitions_between(transitions, *BOOKING)) == 5
    assert len(transitions_between(transitions, *CANCELLATION)) == 2
    assert len(transitions_between(transitions, *BLOCKING)) == 1
    # Nothing in the fixture is ever unblocked, so the reverse edge is empty.
    assert transitions_between(transitions, *UNBLOCKING) == []


def test_a_missed_poll_widens_the_uncertainty_window(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: reporting the nominal cadence across a gap in collection.

    The fixture has a real 90-minute hole (13:30Z to 15:00Z) left by two skipped
    polls. A slot that flips on the far side of it was sold somewhere inside 90
    minutes, not 30. Presenting the nominal cadence there claims a confidence
    the data does not have.
    """
    original = synthetic_history.for_slot(synthetic_history.normalization_slot_uuids[1])
    rescripted = _rescript(original, "OOOOOOOOBB")

    transitions = derive_transitions(rescripted, synthetic_history.snapshots)
    change = next(t for t in transitions if t.from_state is not None)

    assert change.first_seen_at == synthetic_history.gap_end
    assert change.prev_seen_at == synthetic_history.gap_start
    assert change.uncertainty_minutes == synthetic_history.gap_minutes
    assert change.uncertainty_minutes == 90
    assert change.uncertainty_minutes != synthetic_history.cadence_minutes


def test_unknown_snapshot_is_refused_not_guessed(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: ordering a slot's history without knowing when polls ran.

    An observation whose snapshot is absent has no ``observed_at``, so its
    position in the sequence is unknowable. Guessing from ``snapshot_id`` would
    quietly mis-order a catch-up poll; raising keeps the hole visible.
    """
    with pytest.raises(TransitionError, match="unknown snapshot"):
        derive_transitions(synthetic_history.observations, synthetic_history.snapshots[:3])


def test_transitions_are_ordered_by_observed_at_not_snapshot_id(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: trusting autoincrement ids to be chronological.

    A catch-up run for an earlier poll key gets a *higher* snapshot id than the
    later poll already stored. Sequencing on the id would then read the older
    observation as the newer one and invert the transition. Ordering is on
    ``observed_at``, so relabelling the ids changes nothing.
    """
    original = synthetic_history.for_slot(synthetic_history.normal_slot_uuid)
    rescripted = _rescript(original, "OOOOOOBBBB")

    shuffled_snapshots = [
        dataclasses.replace(snapshot, snapshot_id=100 - snapshot.snapshot_id)
        for snapshot in synthetic_history.snapshots
    ]
    shuffled_observations = [
        dataclasses.replace(observation, snapshot_id=100 - observation.snapshot_id)
        for observation in rescripted
    ]

    change = next(
        t
        for t in derive_transitions(shuffled_observations, shuffled_snapshots)
        if t.from_state is not None
    )
    assert change.first_seen_at == _at(180)
    assert change.uncertainty_minutes == 30


def test_naive_snapshot_instant_is_refused(synthetic_history: SyntheticHistory) -> None:
    """Regression: a naive datetime crossing into the analytics layer.

    Every instant inside this layer is aware UTC. A naive ``observed_at`` would
    make every lead time silently wrong by the venue's UTC offset.
    """
    naive = [
        dataclasses.replace(snapshot, observed_at=snapshot.observed_at.replace(tzinfo=None))
        for snapshot in synthetic_history.snapshots
    ]
    with pytest.raises(TransitionError, match="naive observed_at"):
        derive_transitions(synthetic_history.observations, naive)


def test_no_snapshots_is_refused(synthetic_history: SyntheticHistory) -> None:
    """Regression: returning an empty history instead of naming the problem."""
    empty: list[SnapshotRecord] = []
    with pytest.raises(TransitionError, match="no snapshots"):
        derive_transitions(synthetic_history.observations, empty)


# --------------------------------------------------------------------------
# Cancellations
# --------------------------------------------------------------------------


def test_booked_then_open_is_recorded_as_a_cancellation(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: a released booking left reading as booked.

    The cancelled slot runs OPEN -> BOOKED -> OPEN. Both edges must survive as
    separate events: keeping only the latest state loses the revenue that was
    briefly on the books, and keeping only the first loses the release.
    """
    slot = [
        t
        for t in transitions
        if t.slot_uuid == synthetic_history.cancellation_slot_uuid and t.from_state is not None
    ]
    assert [(t.from_state, t.to_state) for t in slot] == [BOOKING, CANCELLATION]
    assert slot[0].first_seen_at == synthetic_history.cancellation_booked_at
    assert slot[1].first_seen_at == synthetic_history.cancellation_released_at
    assert slot[1].uncertainty_minutes == 30


def test_cancellation_rate_carries_both_denominators(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: a cancellation rate quoted without the base it divides by.

    Padel Fort's trading week 2026-09-14 sees four observed bookings (one
    normal, one cancelled, two on the re-booked slot) and two cancellations, so
    the rate is 0.5. The distinct booked-slot count is 4 -- which includes the
    left-censored slot whose booking event we never saw -- and that censored
    count is reported separately so the two denominators can never be confused.
    """
    rows = cancellation_rate(transitions, synthetic_history.observations)
    by_venue = {row.venue_uuid: row for row in rows}
    assert set(by_venue) == {PADEL_FORT_VENUE, PLAY_PADEL_VENUE}

    fort = by_venue[PADEL_FORT_VENUE]
    assert (fort.iso_year, fort.iso_week) == (2026, 38)
    assert fort.week_start == dt.date(2026, 9, 14)
    assert fort.bookings == 4
    assert fort.cancellations == 2
    assert fort.rate == pytest.approx(0.5)
    assert fort.booked_slots == 4
    assert fort.censored_slots == 1

    # Event counts are grid-dependent -- one customer-hour is two events on a
    # 30-minute grid and one on a 60-minute grid -- so the court-minutes behind
    # them travel on the same row and are the only cross-venue quantity here.
    assert fort.booked_court_minutes == 4 * 30
    assert fort.cancelled_court_minutes == 2 * 30
    assert fort.booked_court_hours == pytest.approx(2.0)
    assert fort.cancelled_court_hours == pytest.approx(1.0)


def test_cancellation_rate_buckets_a_post_midnight_slot_on_its_trading_week(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: Friday-night demand billed to the following trading week.

    Play Padel's 00:30 slot on Saturday 2026-09-12 is a Friday-night session.
    Bucketing on the raw local date would put it in ISO week 38; its
    ``business_date`` is 2026-09-11, so it belongs to week 37, starting Monday
    2026-09-07.
    """
    rows = cancellation_rate(transitions, synthetic_history.observations)
    play = next(row for row in rows if row.venue_uuid == PLAY_PADEL_VENUE)

    assert (play.iso_year, play.iso_week) == (2026, 37)
    assert play.week_start == dt.date(2026, 9, 7)
    assert play.bookings == 1
    assert play.cancellations == 0
    assert play.rate == pytest.approx(0.0)
    assert play.censored_slots == 1


def test_cancellation_rate_is_none_when_nothing_was_booked() -> None:
    """Regression: a week with no bookings rendering as a 0% cancellation rate.

    No bookings and no cancellations produces no row at all rather than a
    division by zero or a misleading zero.
    """
    assert cancellation_rate([], []) == []


# --------------------------------------------------------------------------
# Blocked inventory
# --------------------------------------------------------------------------


def test_open_to_blocked_is_its_own_event(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: withdrawn inventory folded into occupancy or read as demand.

    A slot going OPEN -> BLOCKED is the venue pulling court time from sale,
    most often an offline or phone booking. It must surface as its own event
    with its own court-minutes and its own uncertainty window.
    """
    events = blocked_inventory_events(transitions, synthetic_history.observations)
    event = next(event for event in events if event.business_date == dt.date(2026, 9, 17))

    assert event.slot_uuids == (synthetic_history.open_to_blocked_slot_uuid,)
    assert event.slot_count == 1
    assert event.court_minutes == 30
    assert event.censored_left is False
    assert event.first_seen_at == synthetic_history.open_to_blocked_at
    assert event.prev_seen_at == _at(90)
    assert event.uncertainty_minutes == 30


def test_a_whole_blocked_evening_is_one_event_not_fourteen(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: one venue decision reported as fourteen separate events.

    Padel Fort pulled 2026-09-13 17:00-23:30 from inventory in full: 14
    consecutive 30-minute slots, 420 court-minutes, zero bookings. Counting
    slots instead of merging the contiguous run turns a single evening into a
    fortnight of churn, and reporting it in slots rather than court-minutes
    makes it incomparable with Padel Up's 60-minute grid.
    """
    events = blocked_inventory_events(transitions, synthetic_history.observations)
    assert len(events) == 2

    evening = next(
        event
        for event in events
        if event.business_date == synthetic_history.blocked_evening_business_date
    )
    assert evening.slot_count == 14
    assert evening.court_minutes == synthetic_history.expected_blocked_evening_minutes
    assert evening.court_minutes == 420
    assert evening.court_hours == pytest.approx(7.0)
    assert set(evening.slot_uuids) == synthetic_history.blocked_evening_slot_uuids
    # 17:00 IST -> 11:30Z; the 23:30 slot ends at 00:00 IST the next day.
    assert evening.start_utc == dt.datetime(2026, 9, 13, 11, 30, tzinfo=dt.UTC)
    assert evening.end_utc == dt.datetime(2026, 9, 13, 18, 30, tzinfo=dt.UTC)


def test_a_block_that_predates_collection_is_still_reported(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: pre-existing blocks vanishing for want of an OPEN edge.

    The real blocked evening is BLOCKED in every snapshot we ever took, so it
    never produces an ``OPEN -> BLOCKED`` transition. Requiring that edge would
    erase 420 court-minutes of withdrawn inventory -- the single most
    interesting thing in the dataset -- and make the venue look merely quiet.
    The event is reported with ``censored_left`` set and no uncertainty window,
    because the withdrawal happened at an unknowable time before we looked.
    """
    events = blocked_inventory_events(transitions, synthetic_history.observations)
    evening = next(
        event
        for event in events
        if event.business_date == synthetic_history.blocked_evening_business_date
    )
    assert evening.censored_left is True
    assert evening.prev_seen_at is None
    assert evening.uncertainty_minutes is None
    assert evening.first_seen_at == synthetic_history.observed_at(0)


def test_blocked_runs_do_not_merge_across_a_gap_in_court_time(
    synthetic_history: SyntheticHistory, transitions: list[StateTransition]
) -> None:
    """Regression: non-adjacent blocked slots glued into one phantom event.

    The fixture's two blocked episodes are four days apart in the same facility.
    Merging on facility alone, or on the poll that saw them, would report one
    impossible 450-minute event spanning 2026-09-13 to 2026-09-17.
    """
    events = blocked_inventory_events(transitions, synthetic_history.observations)
    assert [event.business_date for event in events] == [
        dt.date(2026, 9, 13),
        dt.date(2026, 9, 17),
    ]
    assert [event.slot_count for event in events] == [14, 1]


# --------------------------------------------------------------------------
# first_booked
# --------------------------------------------------------------------------


def test_first_booked_reports_the_fixture_lead_time(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: a lead time measured from the wrong anchor.

    The normal slot starts 2026-09-14 19:00 IST (13:30Z) and was first seen
    BOOKED at 13:00Z on 2026-09-11: 72.5 hours of lead time, carrying the
    30-minute poll gap it was observed inside.
    """
    booking = _by_slot(bookings)[synthetic_history.normal_slot_uuid]

    assert booking.first_booked_at == synthetic_history.expected_first_booked_at
    assert booking.lead_time_hours == pytest.approx(synthetic_history.expected_lead_time_hours)
    assert booking.lead_time_hours == pytest.approx(72.5)
    assert booking.uncertainty_minutes == 30
    assert booking.censored_left is False
    assert booking.rebooked is False
    assert booking.cancelled is False
    assert booking.post_start is False
    assert booking.slot_start_hour == 19
    assert booking.business_date == dt.date(2026, 9, 14)


def test_only_slots_ever_booked_get_a_booking_record(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: unsold and blocked inventory padding the booking table.

    The fixture books six slots: the normal one, two left-censored ones, the
    cancelled one, the re-booked one and the post-midnight one. The 14 blocked
    evening slots, the normalization pair and the elapsed-but-open slot were
    never booked and must not appear as zero-lead bookings.
    """
    assert len(bookings) == 6
    booked = set(_by_slot(bookings))
    assert synthetic_history.elapsed_open_slot_uuid not in booked
    assert synthetic_history.open_to_blocked_slot_uuid not in booked
    assert not (booked & synthetic_history.blocked_evening_slot_uuids)
    assert not (booked & set(synthetic_history.normalization_slot_uuids))


def test_left_censored_bookings_are_flagged_and_have_no_lead_time(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """THE CENSORING TEST. Regression: a booking that pre-dates the dataset
    counted as a booking made the instant we first looked.

    Both censored slots are BOOKED in the very first snapshot that ever saw
    them, at two different venues and two different grids. Their real booking
    time is unknowable, so ``first_booked_at`` and ``lead_time_hours`` are
    ``None`` -- not the first poll and not zero. Pinning them to first sight
    would invent a lead time for every slot already sold when collection began,
    which is precisely the busiest inventory, and would bias every percentile in
    the project downward.
    """
    by_slot = _by_slot(bookings)
    assert synthetic_history.censored_slot_uuids <= set(by_slot)

    for slot_uuid in synthetic_history.censored_slot_uuids:
        booking = by_slot[slot_uuid]
        assert booking.censored_left is True
        assert booking.first_booked_at is None
        assert booking.last_booked_at is None
        assert booking.lead_time_hours is None
        assert booking.raw_lead_time_hours is None
        assert booking.uncertainty_minutes is None
        assert booking.usable_lead_time is False
        # We still know when we first saw it, which is what bounds the censoring.
        assert booking.first_observed_at == synthetic_history.observed_at(0)


def test_rebooked_slot_keeps_both_the_first_and_the_last_booking(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: churn collapsed to a single booking per slot.

    The re-booked slot runs OPEN -> BOOKED -> OPEN -> BOOKED. Keeping only the
    last booking loses the original lead time; keeping only the first hides that
    the slot turned over. Both survive, the flag is set, and the slot is not
    called cancelled because its last observed state is BOOKED.
    """
    booking = _by_slot(bookings)[synthetic_history.rebooked_slot_uuid]

    assert booking.rebooked is True
    assert booking.booking_count == 2
    assert booking.cancellation_count == 1
    assert booking.first_booked_at == synthetic_history.rebooked_first_booked_at
    assert booking.last_booked_at == synthetic_history.rebooked_last_booked_at
    assert booking.first_booked_at != booking.last_booked_at
    assert booking.cancelled is False
    # Lead time is anchored on the FIRST booking: 2026-09-15 21:00 IST is
    # 15:30Z, and 11:00Z on 2026-09-11 is 100.5 hours earlier.
    assert booking.lead_time_hours == pytest.approx(100.5)


def test_cancelled_slot_is_flagged_as_not_sticking(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: a released booking still counted as sold at day end."""
    booking = _by_slot(bookings)[synthetic_history.cancellation_slot_uuid]

    assert booking.cancelled is True
    assert booking.rebooked is False
    assert booking.cancellation_count == 1
    assert booking.first_booked_at == synthetic_history.cancellation_booked_at
    assert booking.lead_time_hours == pytest.approx(99.0)


def test_post_midnight_booking_lands_on_the_previous_business_date(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: Friday-night demand attributed to Saturday.

    Play Padel's 00:30 slot on Saturday 2026-09-12 is a Friday-night session.
    Its ``business_date`` is 2026-09-11 while its local date stays 2026-09-12,
    and its local start hour is 0, so it is off-peak.
    """
    booking = _by_slot(bookings)[synthetic_history.post_midnight_slot_uuid]

    assert booking.business_date == synthetic_history.post_midnight_business_date
    assert booking.business_date == dt.date(2026, 9, 11)
    assert booking.slot_start_utc.date() == dt.date(2026, 9, 11)
    assert booking.slot_start_hour == 0
    assert booking.first_booked_at == synthetic_history.post_midnight_booked_at
    assert booking.lead_time_hours == pytest.approx(7.0)


def test_post_start_booking_is_clamped_and_flagged_not_dropped(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a walk-in paid through the app silently discarded.

    Hudle never marks an elapsed slot unavailable, so a customer who pays after
    play has started produces a booking observed AFTER ``slot_start_utc`` and a
    negative raw lead time. Dropping it hides real demand and biases the sample
    toward planners; leaving it negative poisons every percentile. It is clamped
    to zero, flagged, and the unclamped value is kept so the clamp is auditable.
    """
    elapsed = synthetic_history.for_slot(synthetic_history.elapsed_open_slot_uuid)
    assert all(observation.is_past for observation in elapsed)

    rescripted = _rescript(elapsed, "OOOOOOBBBB")
    transitions = derive_transitions(rescripted, synthetic_history.snapshots)
    booking = first_booked(transitions, rescripted)[0]

    # 2026-09-11 07:00 IST is 01:30Z; the booking is first seen at 13:00Z.
    assert booking.first_booked_at == _at(180)
    assert booking.post_start is True
    assert booking.raw_lead_time_hours == pytest.approx(-11.5)
    assert booking.lead_time_hours == pytest.approx(0.0)
    assert booking.usable_lead_time is True

    stats = lead_time_distribution([booking], peak_hours=PEAK_HOURS).overall
    assert stats.n == 1
    assert stats.post_start == 1
    assert stats.median_hours == pytest.approx(0.0)


def test_booking_record_round_trips_to_the_storage_row(
    synthetic_history: SyntheticHistory, bookings: list[SlotBooking]
) -> None:
    """Regression: derived rows that cannot be persisted as briefed.

    ``as_row()`` must produce exactly the frozen ``SlotFirstBooked`` that
    ``replace_derived_first_booked`` stores, carrying the clamped lead time and
    both flags.
    """
    booking = _by_slot(bookings)[synthetic_history.normal_slot_uuid]
    row = booking.as_row()

    assert row.slot_uuid == booking.slot_uuid
    assert row.business_date == booking.business_date
    assert row.first_booked_at == booking.first_booked_at
    assert row.lead_time_hours == pytest.approx(72.5)
    assert row.uncertainty_minutes == 30
    assert row.censored_left is False
    assert row.rebooked is False
    assert row.cancelled is False


# --------------------------------------------------------------------------
# lead_time_distribution
# --------------------------------------------------------------------------


def test_median_and_p90_on_a_sample_with_a_known_answer() -> None:
    """Regression: a percentile that is not the percentile it claims to be.

    Lead times 1..10 hours. Linear interpolation puts the median at index
    0.5*(n-1) = 4.5, i.e. 5.5 hours, and P90 at index 8.1, i.e. 9.1 hours. A
    nearest-rank implementation would answer 5 or 6 and 9; a mean would answer
    5.5 and nothing. Every statistic carries its own n.
    """
    sample = [
        _booking(slot_uuid=f"slot-{index}", lead_time_hours=float(index)) for index in range(1, 11)
    ]
    stats = lead_time_distribution(sample, peak_hours=PEAK_HOURS).overall

    assert stats.n == 10
    assert stats.excluded_censored == 0
    assert stats.median_hours == pytest.approx(5.5)
    assert stats.p90_hours == pytest.approx(9.1)
    assert stats.min_hours == pytest.approx(1.0)
    assert stats.max_hours == pytest.approx(10.0)


def test_censored_bookings_are_excluded_from_every_statistic_and_counted(
    bookings: list[SlotBooking],
) -> None:
    """THE CENSORING TEST, at the statistic level. Regression: left-censored
    bookings averaged into the medians.

    Four of the fixture's six bookings have an observed booking time (7.0, 72.5,
    99.0 and 100.5 hours); two are left-censored. The overall sample is
    therefore n=4 with two exclusions reported, giving a median of 85.75 and a
    P90 of 100.05. Including the censored pair -- pinned to the first poll --
    would make n=6, drop the peak median from 99.0 to 99.25 and the off-peak
    sample from one booking to two. The exclusion count travels with every
    statistic so it is never invisible.
    """
    distribution = lead_time_distribution(bookings, peak_hours=PEAK_HOURS)
    overall = distribution.overall

    assert overall.n == 4
    assert overall.excluded_censored == 2
    assert overall.median_hours == pytest.approx(85.75)
    assert overall.p90_hours == pytest.approx(100.05)
    assert overall.min_hours == pytest.approx(7.0)
    assert overall.max_hours == pytest.approx(100.5)
    assert overall.post_start == 0

    # Every bucket reports its own exclusions, not just the total.
    assert sum(stats.excluded_censored for stats in distribution.by_peak.values()) == 2
    assert sum(stats.excluded_censored for stats in distribution.by_day_of_week.values()) == 2
    assert sum(stats.n for stats in distribution.by_peak.values()) == 4


def test_peak_and_off_peak_split_uses_config_peak_hours(
    test_config: Config, bookings: list[SlotBooking]
) -> None:
    """Regression: a hard-coded peak window that config can no longer move.

    ``config.dashboard.peak_hours`` is 18-22. Three booked slots start at 19:00,
    20:00 and 21:00 (peak); the post-midnight 00:30 slot is off-peak. The peak
    median is 99.0 hours over n=3 with one censored exclusion; off-peak is a
    single 7.0-hour booking with one censored exclusion. Narrowing the config
    window must move slots between the buckets.
    """
    peak_hours = test_config.dashboard.peak_hours
    assert peak_hours == PEAK_HOURS

    distribution = lead_time_distribution(bookings, peak_hours=peak_hours)
    assert distribution.peak_hours == PEAK_HOURS

    peak = distribution.by_peak[PeakBucket.PEAK]
    assert peak.n == 3
    assert peak.excluded_censored == 1
    assert peak.median_hours == pytest.approx(99.0)
    assert peak.p90_hours == pytest.approx(100.2)

    off_peak = distribution.by_peak[PeakBucket.OFF_PEAK]
    assert off_peak.n == 1
    assert off_peak.excluded_censored == 1
    assert off_peak.median_hours == pytest.approx(7.0)

    # Config drives the split: drop 19:00 out of peak and the normal slot moves.
    narrowed = lead_time_distribution(bookings, peak_hours=(20, 21, 22))
    assert narrowed.by_peak[PeakBucket.PEAK].n == 2
    assert narrowed.by_peak[PeakBucket.OFF_PEAK].n == 2


def test_day_of_week_split_uses_business_date_not_local_date(
    bookings: list[SlotBooking],
) -> None:
    """Regression: Friday-night sales bucketed as Saturday.

    Business dates present are 2026-09-11 (Friday), 2026-09-14 (Monday) and
    2026-09-15 (Tuesday), so the weekday keys are 4, 0 and 1. The post-midnight
    slot's local date is Saturday 2026-09-12; bucketing on it would create a
    weekday-5 group and empty the Friday one, moving demand to the wrong night
    of the week at every venue that sells past midnight.
    """
    distribution = lead_time_distribution(bookings, peak_hours=PEAK_HOURS)
    by_day = distribution.by_day_of_week

    assert sorted(by_day) == [0, 1, 4]
    assert 5 not in by_day

    friday = by_day[4]
    assert friday.label == DAY_NAMES[4] == "Fri"
    assert friday.n == 1
    assert friday.excluded_censored == 1
    assert friday.median_hours == pytest.approx(7.0)

    monday = by_day[0]
    assert monday.n == 1
    assert monday.median_hours == pytest.approx(72.5)

    tuesday = by_day[1]
    assert tuesday.n == 2
    assert tuesday.excluded_censored == 1
    assert tuesday.median_hours == pytest.approx(99.75)
    assert tuesday.p90_hours == pytest.approx(100.35)


def test_empty_sample_yields_none_not_zero() -> None:
    """Regression: "nobody booked" rendering as "booked at zero hours' notice".

    A ratio or percentile with no sample must be ``None``, so a chart prints an
    empty state rather than an authoritative zero.
    """
    stats = lead_time_distribution([], peak_hours=PEAK_HOURS).overall
    assert stats.n == 0
    assert stats.median_hours is None
    assert stats.p90_hours is None
    assert stats.min_hours is None
    assert stats.max_hours is None


# --------------------------------------------------------------------------
# time_to_sellout
# --------------------------------------------------------------------------


def test_time_to_sellout_covers_prime_slots_watched_long_enough(
    bookings: list[SlotBooking],
) -> None:
    """Regression: sellout times biased low by slots we started watching late.

    Three peak slots sold under observation, 3.0, 1.5 and 1.0 hours after we
    first saw them, all watched from more than 24 hours out. The censored peak
    slot is excluded (it sold before we looked) and the two off-peak slots are
    excluded by design. Every exclusion is counted and the minimum observation
    window is carried on the result, because the number is meaningless without
    it.
    """
    report = time_to_sellout(bookings, peak_hours=PEAK_HOURS)

    assert report.min_observation_hours == DEFAULT_MIN_OBSERVATION_HOURS
    assert report.peak_hours == PEAK_HOURS
    assert report.n == 3
    assert report.excluded_censored == 1
    assert report.excluded_off_peak == 2
    assert report.excluded_short_window == 0
    assert [record.hours_to_sellout for record in report.records] == pytest.approx([3.0, 1.5, 1.0])
    assert report.median_hours == pytest.approx(1.5)
    assert report.p90_hours == pytest.approx(2.7)
    assert all(record.slot_start_hour in PEAK_HOURS for record in report.records)


def test_short_observation_window_is_excluded_not_counted_low(
    bookings: list[SlotBooking],
) -> None:
    """Regression: a slot first seen an hour out reported as a one-hour sellout.

    A slot can only show a sellout as long as we watched it. Raising the minimum
    window above every slot's window must empty the sample and say so, rather
    than quietly returning the same fast numbers computed from a truncated view.
    """
    report = time_to_sellout(bookings, peak_hours=PEAK_HOURS, min_observation_hours=200.0)

    assert report.n == 0
    assert report.records == ()
    assert report.excluded_short_window == 3
    assert report.median_hours is None
    assert report.p90_hours is None
    assert report.min_observation_hours == 200.0


def test_sellout_records_carry_their_uncertainty(bookings: list[SlotBooking]) -> None:
    """Regression: a sellout time presented as exact.

    The booking was observed somewhere inside a poll gap, so every record keeps
    the width of that gap.
    """
    report = time_to_sellout(bookings, peak_hours=PEAK_HOURS)
    assert all(record.uncertainty_minutes == 30 for record in report.records)


# --------------------------------------------------------------------------
# first_slot_to_go
# --------------------------------------------------------------------------


def test_first_slot_to_go_picks_the_earliest_observed_booking_per_day(
    bookings: list[SlotBooking],
) -> None:
    """Regression: the wrong hour named as a trading day's first sale.

    On 2026-09-15 two slots sold: 20:00 at 11:30Z and 21:00 at 11:00Z. The
    winner is 21:00, the earlier booking, not the earlier slot. 2026-09-14's
    winner is 19:00 and 2026-09-11's is the post-midnight 00:30 slot, which
    proves the day key is the business date and not the local one.
    """
    rows = {row.business_date: row for row in first_slot_to_go(bookings)}

    assert sorted(rows) == [dt.date(2026, 9, 11), dt.date(2026, 9, 14), dt.date(2026, 9, 15)]
    assert rows[dt.date(2026, 9, 15)].slot_start_hour == 21
    assert rows[dt.date(2026, 9, 15)].first_booked_at == _at(60)
    assert rows[dt.date(2026, 9, 14)].slot_start_hour == 19
    assert rows[dt.date(2026, 9, 11)].slot_start_hour == 0
    assert rows[dt.date(2026, 9, 11)].venue_uuid == PLAY_PADEL_VENUE


def test_first_slot_to_go_excludes_censored_slots_and_counts_them(
    bookings: list[SlotBooking],
) -> None:
    """Regression: a race whose every censored entrant dead-heats at poll one.

    A slot already booked when we first looked has no observed booking time; if
    it entered the race, the first snapshot would win every day that had one and
    the answer would be "the hour we happened to start collecting". Censored
    slots are excluded and counted on the day they belong to.
    """
    rows = {row.business_date: row for row in first_slot_to_go(bookings)}

    assert rows[dt.date(2026, 9, 15)].excluded_censored == 1
    assert rows[dt.date(2026, 9, 11)].excluded_censored == 1
    assert rows[dt.date(2026, 9, 14)].excluded_censored == 0


def test_first_slot_to_go_has_no_row_for_a_day_with_only_censored_bookings() -> None:
    """Regression: inventing a first-sale hour for a day sold out before we looked."""
    censored = _booking(
        slot_uuid="censored-only",
        lead_time_hours=None,
        censored_left=True,
        business_date=dt.date(2026, 9, 20),
    )
    assert first_slot_to_go([censored]) == []


def test_first_slot_to_go_breaks_ties_deterministically() -> None:
    """Regression: a tie resolved by dict ordering instead of by the data.

    A 30-minute cadence cannot separate two slots first seen booked in the same
    poll. The earlier slot start wins, so the answer does not change when the
    input order does.
    """
    early = _booking(slot_uuid="aaa", lead_time_hours=10.0, slot_start_hour=19)
    late = dataclasses.replace(
        _booking(slot_uuid="zzz", lead_time_hours=10.0, slot_start_hour=21),
        slot_start_utc=dt.datetime(2026, 9, 14, 15, 30, tzinfo=dt.UTC),
    )

    forward = first_slot_to_go([early, late])
    backward = first_slot_to_go([late, early])
    assert forward[0].slot_uuid == backward[0].slot_uuid == "aaa"
