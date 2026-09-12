"""Booking lead time: how far ahead court time actually sells.

This module is pure. No I/O, no database handle, no HTTP, no
:func:`datetime.now`. It consumes the transition history that
:mod:`tracker.analytics.transitions` derives and turns it into the per-slot
booking record and the distributions the dashboard reads.

Three things decide whether these numbers mean anything.

**Left censoring.** A slot that was already BOOKED in the very first snapshot
that ever saw it was sold before our data starts. Its lead time is not zero and
not short -- it is *unknown*. Counting it as "booked the moment we first looked"
would drag every median and every P90 down, and would do so hardest at the
busiest venues, which are exactly the ones with bookings already on the board
when collection began. So a censored slot gets ``lead_time_hours=None`` and is
excluded from every statistic, and the count of exclusions travels with every
statistic that was computed without them.

**Sample size, always.** A median over four bookings and a median over four
hundred are not the same claim. Every :class:`LeadTimeStats` carries ``n``, the
censored count it excluded, and the number of post-start bookings it contains,
so no figure can be quoted without its own denominators.

**Post-start bookings are real.** Hudle never marks an elapsed slot unavailable,
so a walk-up who pays through the app after play has started produces a booking
observed *after* ``slot_start_utc`` and a negative raw lead time. Those are
clamped to zero and flagged rather than dropped: a dropped row is an invisible
bias, and walk-ins are a genuine demand signal.

Survivorship shows up once more in :func:`time_to_sellout`. A slot we only
started watching a few hours before it started cannot show a long sellout time
no matter how early it really sold, so those slots are excluded and the minimum
observation window is reported on the result rather than left implicit.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from tracker.analytics.transitions import (
    BOOKING,
    CANCELLATION,
    TransitionError,
    slot_static_index,
)
from tracker.types import (
    SlotFirstBooked,
    SlotObservation,
    SlotState,
    StateTransition,
    slot_start_hour,
)

logger = logging.getLogger("tracker.analytics.leadtime")

SECONDS_PER_HOUR = 3600.0

#: How long a slot must have been under observation before its time-to-sellout
#: is trustworthy. A slot first seen three hours before it starts cannot show a
#: three-day sellout, so including it biases every sellout figure low.
DEFAULT_MIN_OBSERVATION_HOURS = 24.0

MEDIAN_QUANTILE = 0.5
P90_QUANTILE = 0.9

#: Monday-first day-of-week labels, matching :meth:`datetime.date.weekday`.
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class PeakBucket(StrEnum):
    """Peak / off-peak split, driven by ``config.dashboard.peak_hours``."""

    PEAK = "peak"
    OFF_PEAK = "off_peak"


# --------------------------------------------------------------------------
# Per-slot booking record
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotBooking:
    """What we could observe about one slot's booking history.

    ``lead_time_hours`` is the storable, clamped figure: ``None`` whenever it is
    unusable (left-censored, or never observed booking), and ``0.0`` for a
    post-start booking. ``raw_lead_time_hours`` keeps the unclamped value,
    negative for a post-start booking, so the clamp is auditable rather than
    silent.

    ``cancelled`` means the booking did not stick: the slot was BOOKED at some
    point and is not BOOKED in the last poll that saw it. ``rebooked`` means two
    or more *observed* booking events. The two are deliberately disjoint in the
    common cases -- a slot booked, released and booked again ends up
    ``rebooked=True, cancelled=False`` -- and ``cancellation_count`` carries the
    raw churn either way.
    """

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    slot_start_utc: dt.datetime
    slot_start_hour: int
    business_date: dt.date
    duration_minutes: int
    first_observed_at: dt.datetime
    first_booked_at: dt.datetime | None
    last_booked_at: dt.datetime | None
    lead_time_hours: float | None
    raw_lead_time_hours: float | None
    uncertainty_minutes: int | None
    observation_window_hours: float
    booking_count: int
    cancellation_count: int
    censored_left: bool
    rebooked: bool
    cancelled: bool
    post_start: bool

    @property
    def usable_lead_time(self) -> bool:
        """Whether this row may enter a lead-time statistic."""
        return self.lead_time_hours is not None

    def as_row(self) -> SlotFirstBooked:
        """The frozen storage row, for ``replace_derived_first_booked``."""
        return SlotFirstBooked(
            slot_uuid=self.slot_uuid,
            venue_uuid=self.venue_uuid,
            facility_uuid=self.facility_uuid,
            slot_start_utc=self.slot_start_utc,
            business_date=self.business_date,
            first_booked_at=self.first_booked_at,
            last_booked_at=self.last_booked_at,
            lead_time_hours=self.lead_time_hours,
            uncertainty_minutes=self.uncertainty_minutes,
            censored_left=self.censored_left,
            rebooked=self.rebooked,
            cancelled=self.cancelled,
        )


@dataclass(frozen=True, slots=True)
class LeadTimeStats:
    """A median and a P90 that cannot be quoted without their denominators.

    ``median_hours`` and ``p90_hours`` are ``None`` -- never ``0.0`` -- when
    ``n`` is zero. ``excluded_censored`` is the number of bookings in this same
    bucket that were dropped because they pre-date the dataset, and
    ``post_start`` how many of the included ones were clamped from a negative
    raw lead time.

    **Percentiles are weighted per booked slot, not per booked court-hour.**
    ``n`` counts slots, so at equal court-hours sold a 30-minute-grid venue
    contributes two data points for every one a 60-minute-grid venue
    contributes, and a pooled median across venues leans toward the finer grid.
    The distortion is zero while Padel Up -- the only 60-minute grid -- records
    no bookings, and a pooled figure must be labelled with ``weighting`` the
    moment it does. Per-venue statistics are unaffected: one venue, one grid.
    """

    label: str
    n: int
    excluded_censored: int
    post_start: int
    median_hours: float | None
    p90_hours: float | None
    min_hours: float | None
    max_hours: float | None


@dataclass(frozen=True, slots=True)
class LeadTimeDistribution:
    """Lead-time statistics overall and broken out two ways.

    ``by_day_of_week`` is keyed on ``business_date.weekday()`` (Monday 0), so
    Play Padel's 00:30 Saturday sales are attributed to the Friday session that
    produced them rather than to Saturday. ``by_peak`` splits on the slot's
    local start hour against ``config.dashboard.peak_hours``.
    """

    overall: LeadTimeStats
    by_day_of_week: Mapping[int, LeadTimeStats]
    by_peak: Mapping[PeakBucket, LeadTimeStats]
    peak_hours: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SelloutRecord:
    """One prime-time slot that sold while we were watching it."""

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    business_date: dt.date
    slot_start_hour: int
    first_observed_at: dt.datetime
    first_booked_at: dt.datetime
    hours_to_sellout: float
    observation_window_hours: float
    uncertainty_minutes: int | None


@dataclass(frozen=True, slots=True)
class SelloutReport:
    """Time-to-sellout for prime slots, with every exclusion stated.

    ``min_observation_hours`` is on the result on purpose: the figure is
    meaningless without it, because a slot first seen an hour before it starts
    can only ever show a one-hour sellout.
    """

    min_observation_hours: float
    peak_hours: tuple[int, ...]
    records: tuple[SelloutRecord, ...]
    n: int
    excluded_censored: int
    excluded_short_window: int
    excluded_off_peak: int
    median_hours: float | None
    p90_hours: float | None


@dataclass(frozen=True, slots=True)
class FirstSlotToGo:
    """Which hour of one trading day sold first.

    Left-censored slots cannot win this race -- they were sold before we looked,
    so the first poll would make every one of them a dead heat at the moment
    collection started. They are excluded and counted in
    ``excluded_censored``, which is how a day whose evening was already gone
    stays distinguishable from a day that genuinely sold its 06:00 first.
    """

    business_date: dt.date
    venue_uuid: str
    facility_uuid: str
    slot_uuid: str
    slot_start_hour: int
    first_booked_at: dt.datetime
    lead_time_hours: float | None
    uncertainty_minutes: int | None
    excluded_censored: int


# --------------------------------------------------------------------------
# Per-slot derivation
# --------------------------------------------------------------------------


def first_booked(
    transitions: Iterable[StateTransition],
    observations: Iterable[SlotObservation],
) -> list[SlotBooking]:
    """Reduce a transition history to one booking record per slot ever booked.

    Only slots observed BOOKED at some point produce a row: a slot nobody
    touched has no booking history to summarise, and its inventory is already
    accounted for in occupancy.

    The first-sighting transition (``from_state is None``) that
    :func:`~tracker.analytics.transitions.derive_transitions` emits for every
    slot is what makes left censoring detectable at all. If that first sighting
    was BOOKED, the booking pre-dates the dataset: ``first_booked_at`` stays
    ``None`` rather than being pinned to the poll that first noticed, and
    ``censored_left`` is set. Nothing downstream can then average it in by
    accident, because the lead time is ``None`` too.

    Rows come back sorted by slot start.
    """
    static = slot_static_index(observations)
    by_slot: dict[str, list[StateTransition]] = defaultdict(list)
    for transition in transitions:
        by_slot[transition.slot_uuid].append(transition)

    bookings: list[SlotBooking] = []
    for slot_uuid, slot_transitions in by_slot.items():
        slot_transitions.sort(key=lambda t: t.first_seen_at)
        booking = _booking_for_slot(slot_uuid, slot_transitions, static)
        if booking is not None:
            bookings.append(booking)

    bookings.sort(key=lambda b: (b.slot_start_utc, b.facility_uuid, b.slot_uuid))
    logger.info(
        "first_booked_derived",
        extra={
            "booked_slots": len(bookings),
            "censored_left": sum(1 for b in bookings if b.censored_left),
            "post_start": sum(1 for b in bookings if b.post_start),
            "rebooked": sum(1 for b in bookings if b.rebooked),
            "cancelled": sum(1 for b in bookings if b.cancelled),
        },
    )
    return bookings


# --------------------------------------------------------------------------
# Distributions
# --------------------------------------------------------------------------


def lead_time_distribution(
    bookings: Iterable[SlotBooking],
    *,
    peak_hours: Sequence[int],
) -> LeadTimeDistribution:
    """Median and P90 lead time, overall and split by weekday and peak.

    ``peak_hours`` comes from ``config.dashboard.peak_hours``; it is never
    hard-coded here, so moving the peak window in config moves every split.

    Left-censored bookings are excluded from every bucket and counted into that
    same bucket's ``excluded_censored``, so a split's exclusions are visible
    beside its own ``n`` rather than only in the total.
    """
    peaks = tuple(sorted(set(peak_hours)))
    peak_set = frozenset(peaks)
    rows = list(bookings)

    overall = _stats("overall", rows)

    by_day: dict[int, list[SlotBooking]] = defaultdict(list)
    for row in rows:
        by_day[row.business_date.weekday()].append(row)
    by_day_of_week = {
        weekday: _stats(DAY_NAMES[weekday], day_rows)
        for weekday, day_rows in sorted(by_day.items())
    }

    by_bucket: dict[PeakBucket, list[SlotBooking]] = {
        PeakBucket.PEAK: [],
        PeakBucket.OFF_PEAK: [],
    }
    for row in rows:
        by_bucket[_bucket(row.slot_start_hour, peak_set)].append(row)
    by_peak = {
        bucket: _stats(str(bucket), bucket_rows) for bucket, bucket_rows in by_bucket.items()
    }

    logger.info(
        "lead_time_distribution_computed",
        extra={
            "n": overall.n,
            "excluded_censored": overall.excluded_censored,
            "post_start": overall.post_start,
            "peak_hours": list(peaks),
        },
    )
    return LeadTimeDistribution(
        overall=overall,
        by_day_of_week=by_day_of_week,
        by_peak=by_peak,
        peak_hours=peaks,
    )


def time_to_sellout(
    bookings: Iterable[SlotBooking],
    *,
    peak_hours: Sequence[int],
    min_observation_hours: float = DEFAULT_MIN_OBSERVATION_HOURS,
) -> SelloutReport:
    """Hours from a prime slot's first observation to the poll that saw it sold.

    Restricted to slots starting in ``peak_hours``: "how fast does prime time
    go" is the question worth asking, and mixing in a 06:00 weekday slot that
    nobody wanted answers a different one.

    A slot qualifies only if we watched it from at least ``min_observation_hours``
    before its start. A slot first seen three hours out can show at most a
    three-hour sellout however early it really sold, so including it drags the
    whole distribution toward zero. Excluded slots are counted, not dropped
    quietly, and the threshold is carried on the result.
    """
    peaks = tuple(sorted(set(peak_hours)))
    peak_set = frozenset(peaks)

    records: list[SelloutRecord] = []
    excluded_censored = 0
    excluded_short_window = 0
    excluded_off_peak = 0

    for booking in bookings:
        if booking.slot_start_hour not in peak_set:
            excluded_off_peak += 1
            continue
        if booking.censored_left or booking.first_booked_at is None:
            excluded_censored += 1
            continue
        if booking.observation_window_hours < min_observation_hours:
            excluded_short_window += 1
            continue
        records.append(
            SelloutRecord(
                slot_uuid=booking.slot_uuid,
                venue_uuid=booking.venue_uuid,
                facility_uuid=booking.facility_uuid,
                business_date=booking.business_date,
                slot_start_hour=booking.slot_start_hour,
                first_observed_at=booking.first_observed_at,
                first_booked_at=booking.first_booked_at,
                hours_to_sellout=_hours(booking.first_booked_at - booking.first_observed_at),
                observation_window_hours=booking.observation_window_hours,
                uncertainty_minutes=booking.uncertainty_minutes,
            )
        )

    records.sort(key=lambda r: (r.business_date, r.slot_start_hour, r.slot_uuid))
    values = [r.hours_to_sellout for r in records]
    logger.info(
        "time_to_sellout_computed",
        extra={
            "n": len(records),
            "excluded_censored": excluded_censored,
            "excluded_short_window": excluded_short_window,
            "min_observation_hours": min_observation_hours,
        },
    )
    return SelloutReport(
        min_observation_hours=min_observation_hours,
        peak_hours=peaks,
        records=tuple(records),
        n=len(records),
        excluded_censored=excluded_censored,
        excluded_short_window=excluded_short_window,
        excluded_off_peak=excluded_off_peak,
        median_hours=_percentile(values, MEDIAN_QUANTILE),
        p90_hours=_percentile(values, P90_QUANTILE),
    )


def first_slot_to_go(bookings: Iterable[SlotBooking]) -> list[FirstSlotToGo]:
    """Per trading day, the hour whose slot was the first one observed sold.

    Keyed on ``business_date``, so a Friday-night 00:30 sale is the Friday's
    first slot to go and not the Saturday's. Days whose only bookings are
    left-censored produce no row at all, with the censored count reported on
    every day that does produce one.
    """
    winners: dict[dt.date, SlotBooking] = {}
    censored: dict[dt.date, int] = defaultdict(int)

    for booking in bookings:
        if booking.censored_left or booking.first_booked_at is None:
            censored[booking.business_date] += 1
            continue
        incumbent = winners.get(booking.business_date)
        if incumbent is None or _earlier(booking, incumbent):
            winners[booking.business_date] = booking

    rows = [
        FirstSlotToGo(
            business_date=business_date,
            venue_uuid=booking.venue_uuid,
            facility_uuid=booking.facility_uuid,
            slot_uuid=booking.slot_uuid,
            slot_start_hour=booking.slot_start_hour,
            first_booked_at=_require_booked_at(booking),
            lead_time_hours=booking.lead_time_hours,
            uncertainty_minutes=booking.uncertainty_minutes,
            excluded_censored=censored.get(business_date, 0),
        )
        for business_date, booking in winners.items()
    ]
    rows.sort(key=lambda r: r.business_date)
    return rows


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _booking_for_slot(
    slot_uuid: str,
    slot_transitions: Sequence[StateTransition],
    static: Mapping[str, SlotObservation],
) -> SlotBooking | None:
    """Summarise one slot, or ``None`` when it was never observed BOOKED."""
    first = slot_transitions[0]
    if first.from_state is not None:
        raise TransitionError(
            f"slot {slot_uuid} has no first-sighting transition; left censoring "
            "cannot be detected without one"
        )

    booking_events = [t for t in slot_transitions if (t.from_state, t.to_state) == BOOKING]
    cancellations = [t for t in slot_transitions if (t.from_state, t.to_state) == CANCELLATION]
    censored_left = first.to_state is SlotState.BOOKED
    ever_booked = censored_left or bool(booking_events)
    if not ever_booked:
        return None

    try:
        observation = static[slot_uuid]
    except KeyError as exc:
        raise TransitionError(
            f"slot {slot_uuid} has transitions but no observation; the two "
            "streams describe different collections"
        ) from exc

    first_event = booking_events[0] if booking_events else None
    last_event = booking_events[-1] if booking_events else None
    first_booked_at = None if censored_left or first_event is None else first_event.first_seen_at
    last_booked_at = None if last_event is None else last_event.first_seen_at

    raw_lead: float | None = None
    if first_booked_at is not None:
        raw_lead = _hours(observation.slot_start_utc - first_booked_at)
    post_start = raw_lead is not None and raw_lead < 0.0
    lead_time = None if raw_lead is None else max(raw_lead, 0.0)

    if post_start:
        logger.info(
            "post_start_booking_clamped",
            extra={
                "slot_uuid": slot_uuid,
                "facility_uuid": observation.facility_uuid,
                "raw_lead_time_hours": raw_lead,
            },
        )

    return SlotBooking(
        slot_uuid=slot_uuid,
        venue_uuid=observation.venue_uuid,
        facility_uuid=observation.facility_uuid,
        slot_start_utc=observation.slot_start_utc,
        slot_start_hour=slot_start_hour(observation),
        business_date=observation.business_date,
        duration_minutes=observation.duration_minutes,
        first_observed_at=first.first_seen_at,
        first_booked_at=first_booked_at,
        last_booked_at=last_booked_at,
        lead_time_hours=lead_time,
        raw_lead_time_hours=raw_lead,
        uncertainty_minutes=None if first_event is None else first_event.uncertainty_minutes,
        observation_window_hours=_hours(observation.slot_start_utc - first.first_seen_at),
        booking_count=len(booking_events),
        cancellation_count=len(cancellations),
        censored_left=censored_left,
        rebooked=len(booking_events) > 1,
        cancelled=slot_transitions[-1].to_state is not SlotState.BOOKED,
        post_start=post_start,
    )


def _stats(label: str, rows: Sequence[SlotBooking]) -> LeadTimeStats:
    """Percentiles over the usable rows, carrying what it left out."""
    usable = [row for row in rows if row.lead_time_hours is not None]
    values = [row.lead_time_hours for row in usable if row.lead_time_hours is not None]
    return LeadTimeStats(
        label=label,
        n=len(values),
        excluded_censored=sum(1 for row in rows if row.censored_left),
        post_start=sum(1 for row in usable if row.post_start),
        median_hours=_percentile(values, MEDIAN_QUANTILE),
        p90_hours=_percentile(values, P90_QUANTILE),
        min_hours=min(values) if values else None,
        max_hours=max(values) if values else None,
    )


def _bucket(hour: int, peak_hours: frozenset[int]) -> PeakBucket:
    return PeakBucket.PEAK if hour in peak_hours else PeakBucket.OFF_PEAK


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    """Linearly interpolated quantile, ``None`` on an empty sample.

    ``None`` rather than ``0.0``: "nobody booked" and "everybody booked at zero
    hours' notice" are different facts and must not render as the same number.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + weight * (ordered[upper] - ordered[lower])


def _hours(delta: dt.timedelta) -> float:
    return delta.total_seconds() / SECONDS_PER_HOUR


def _earlier(candidate: SlotBooking, incumbent: SlotBooking) -> bool:
    """Whether ``candidate`` sold before ``incumbent`` on the same trading day.

    Ties -- two slots first seen booked in the same poll, which is all the
    resolution a 30-minute cadence gives -- are broken by the earlier slot
    start, then by slot uuid, so the answer is deterministic instead of
    dictated by dict ordering.
    """
    candidate_booked = _require_booked_at(candidate)
    incumbent_booked = _require_booked_at(incumbent)
    return (candidate_booked, candidate.slot_start_utc, candidate.slot_uuid) < (
        incumbent_booked,
        incumbent.slot_start_utc,
        incumbent.slot_uuid,
    )


def _require_booked_at(booking: SlotBooking) -> dt.datetime:
    if booking.first_booked_at is None:
        raise TransitionError(
            f"slot {booking.slot_uuid} reached a first-booked ranking without an "
            "observed booking time"
        )
    return booking.first_booked_at
