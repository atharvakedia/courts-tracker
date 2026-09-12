"""Occupancy analytics: the headline numbers, in court-minutes.

Every function here is pure. No database handle, no HTTP, no
:func:`datetime.now`. Input is a sequence of
:class:`~tracker.types.SlotObservation` (or anything iterable of them); output
is a frozen dataclass carrying its own arithmetic. That makes every number
reproducible from the observation stream alone and keeps SQLAlchemy rows out of
the web layer.

Four rules are load-bearing, and each one is a way the dashboard goes silently
wrong if it is skipped.

**Court-minutes, never slot counts.** Padel Up sells 60-minute slots; Play
Padel and Padel Fort sell 30-minute ones. One Padel Up slot is two Padel Fort
slots, so any cross-venue figure computed in slots is wrong by 2x. Every
quantity below is summed from each slot's own ``duration_minutes``, and
:func:`to_court_hours` is the only supported way to turn it into hours.

**One observation per slot: the final settled state.** The same slot is seen by
every poll that covers it -- roughly 48 a day -- so summing a raw multi-snapshot
stream multiplies every court-minute figure by the poll count. Worse, averaging
the states over snapshots would report a slot that sold halfway through the
window as partly booked. :func:`settled_observations` reduces each slot to the
last observation taken *before the slot started*, which is the state it
actually settled in, and every public function applies it.

:func:`settled_observations` is the **one** definition of "the row for this
slot" in the whole project. ``tracker.analytics.pricing`` and
``tracker.analytics.market`` import it rather than defining their own, and the
``v_slot_settled`` SQL view implements the identical rule in SQL. Two rules
would mean two headline occupancies over one dataset, which is exactly what a
plain ``MAX(snapshot_id)`` produces once a slot's state changes after it
elapses -- and Hudle keeps publishing elapsed slots, so it does.

**Sport is a normalization axis too.** Play Padel and Padel Fort publish
pickleball courts alongside their padel one, so a venue total that does not
filter on sport compares a three-court venue against a one-court venue. Every
cross-venue entry point here takes ``sport``, and passing it is how a padel
dashboard stays a padel dashboard.

**Two denominators, always both.** ``occupancy_strict`` is booked over sellable
(booked + open) and is the headline. ``occupancy_gross`` is (booked + blocked)
over the whole published day: the share a walk-up customer could not have
booked, whatever the reason. The gap between them is ``blocked_share``, and it
is the only way a venue that pulls inventory to sell it offline is visible at
all. Padel Fort really did block 2026-09-13 17:00-23:30 entirely.

**Zero denominators are ``None``, never ``0.0``.** A day with no sellable
minutes sold none of nothing, which is a different fact from selling none of a
real inventory. Fold them together and a blocked-out evening reads as empty
courts. Padel Up's genuine 0% -- 558 open court-minutes a day and zero bookings
at 1800 per court-hour -- must stay distinguishable from it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tracker.types import SlotObservation, SlotState, Sport, slot_start_hour

MINUTES_PER_HOUR = 60

#: Day-of-week labels indexed the way :meth:`datetime.date.weekday` numbers
#: them, so index 0 is Monday.
DAY_NAMES: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: ``weekday()`` values that count as the weekend in Jaipur.
WEEKEND_DAYS: frozenset[int] = frozenset({5, 6})

#: A heatmap cell holding fewer sellable court-minutes than this is flagged
#: sparse. Two court-hours is roughly a week of one 30-minute grid slot; below
#: that a single booking swings the cell between 0% and 100%, so the dashboard
#: must grey it rather than print a confident percentage.
DEFAULT_SPARSE_MIN_MINUTES = 120


def to_court_hours(minutes: float) -> float:
    """Convert court-minutes to court-hours.

    The only supported conversion. Hand-dividing by 60 at a call site is how a
    display bug reaches production; routing it through one function also makes
    every caller state, by construction, that it started from minutes.
    """
    return minutes / MINUTES_PER_HOUR


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OccupancyTotals:
    """Court-minutes by state, plus every ratio derived from them.

    The numerator and denominator of each ratio are reachable from the same
    object on purpose: every chart must be able to print the denominator it was
    computed from, and a percentage over four sellable minutes is not the same
    claim as a percentage over four hundred.
    """

    booked_minutes: int
    open_minutes: int
    blocked_minutes: int
    slots: int

    @property
    def total_minutes(self) -> int:
        """Every published court-minute, blocked inventory included."""
        return self.booked_minutes + self.open_minutes + self.blocked_minutes

    @property
    def sellable_minutes(self) -> int:
        """Court-minutes a customer could have bought: booked + open."""
        return self.booked_minutes + self.open_minutes

    @property
    def occupancy_strict(self) -> float | None:
        """Booked over sellable. ``None`` when nothing was sellable."""
        return _ratio(self.booked_minutes, self.sellable_minutes)

    @property
    def occupancy_gross(self) -> float | None:
        """(booked + blocked) over the published day. ``None`` when empty."""
        return _ratio(self.booked_minutes + self.blocked_minutes, self.total_minutes)

    @property
    def blocked_share(self) -> float | None:
        """Blocked over the published day. ``None`` when empty."""
        return _ratio(self.blocked_minutes, self.total_minutes)

    @property
    def booked_court_hours(self) -> float:
        return to_court_hours(self.booked_minutes)

    @property
    def open_court_hours(self) -> float:
        return to_court_hours(self.open_minutes)

    @property
    def blocked_court_hours(self) -> float:
        return to_court_hours(self.blocked_minutes)

    @property
    def sellable_court_hours(self) -> float:
        return to_court_hours(self.sellable_minutes)

    @property
    def total_court_hours(self) -> float:
        return to_court_hours(self.total_minutes)

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-ready mapping, ratios and denominators together."""
        return {
            "booked_minutes": self.booked_minutes,
            "open_minutes": self.open_minutes,
            "blocked_minutes": self.blocked_minutes,
            "sellable_minutes": self.sellable_minutes,
            "total_minutes": self.total_minutes,
            "booked_court_hours": self.booked_court_hours,
            "open_court_hours": self.open_court_hours,
            "blocked_court_hours": self.blocked_court_hours,
            "sellable_court_hours": self.sellable_court_hours,
            "total_court_hours": self.total_court_hours,
            "slots": self.slots,
            "occupancy_strict": self.occupancy_strict,
            "occupancy_gross": self.occupancy_gross,
            "blocked_share": self.blocked_share,
        }


@dataclass(frozen=True, slots=True)
class VenueDayOccupancy(OccupancyTotals):
    """One venue's settled occupancy for one business date.

    ``business_date`` -- not the wall-clock date -- is the key: Play Padel's
    00:30 Saturday slots are Friday-night demand and belong to the Friday
    trading day that produced them.
    """

    venue_uuid: str
    business_date: dt.date

    def to_dict(self) -> dict[str, Any]:
        # Named base call rather than ``super()``: ``@dataclass(slots=True)``
        # rebuilds the class, which leaves a zero-argument ``super()`` pointing
        # at the discarded original and raising ``TypeError`` at runtime.
        return {
            "venue_uuid": self.venue_uuid,
            "business_date": self.business_date.isoformat(),
            **OccupancyTotals.to_dict(self),
        }


@dataclass(frozen=True, slots=True)
class HeatmapCell(OccupancyTotals):
    """One (day-of-week, hour-of-day) cell of the demand heatmap.

    ``sparse`` is the honesty flag. A cell built from one or two slots has a
    ratio, but not a meaningful one; the dashboard greys sparse cells instead
    of colouring them like a confident measurement.
    """

    day_of_week: int
    hour: int
    business_dates: int
    sparse: bool

    @property
    def day_name(self) -> str:
        return DAY_NAMES[self.day_of_week]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_of_week": self.day_of_week,
            "day_name": self.day_name,
            "hour": self.hour,
            "business_dates": self.business_dates,
            "sparse": self.sparse,
            **OccupancyTotals.to_dict(self),
        }


@dataclass(frozen=True, slots=True)
class Heatmap:
    """A full hour-of-day x day-of-week grid for one venue, or for all of them.

    ``venue_uuid`` is ``None`` for the combined grid. Only cells with observed
    inventory exist: an hour a venue never publishes has no cell at all, which
    is a different statement from an hour that published inventory and sold
    none of it.
    """

    venue_uuid: str | None
    sparse_min_minutes: int
    cells: tuple[HeatmapCell, ...]

    @property
    def hours(self) -> tuple[int, ...]:
        """Hours that carry at least one cell, ascending."""
        return tuple(sorted({cell.hour for cell in self.cells}))

    @property
    def days_of_week(self) -> tuple[int, ...]:
        return tuple(sorted({cell.day_of_week for cell in self.cells}))

    @property
    def dense_cells(self) -> tuple[HeatmapCell, ...]:
        """Cells whose denominator clears :attr:`sparse_min_minutes`."""
        return tuple(cell for cell in self.cells if not cell.sparse)

    def cell(self, day_of_week: int, hour: int) -> HeatmapCell | None:
        """The cell for one grid position, or ``None`` if nothing was published."""
        return next(
            (c for c in self.cells if c.day_of_week == day_of_week and c.hour == hour), None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue_uuid": self.venue_uuid,
            "sparse_min_minutes": self.sparse_min_minutes,
            "cells": [cell.to_dict() for cell in self.cells],
        }


@dataclass(frozen=True, slots=True)
class DemandSegment(OccupancyTotals):
    """One side of the weekday/weekend split.

    ``business_dates`` is the day count behind the totals, because there are
    five weekdays for every two weekend days: comparing the raw court-hour
    totals of the two segments compares calendar size, not demand. Use the
    ``*_per_day`` figures for that.
    """

    label: str
    business_dates: int

    @property
    def booked_court_hours_per_day(self) -> float | None:
        return self._per_day(self.booked_court_hours)

    @property
    def sellable_court_hours_per_day(self) -> float | None:
        return self._per_day(self.sellable_court_hours)

    @property
    def blocked_court_hours_per_day(self) -> float | None:
        return self._per_day(self.blocked_court_hours)

    def _per_day(self, court_hours: float) -> float | None:
        if self.business_dates == 0:
            return None
        return court_hours / self.business_dates

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "business_dates": self.business_dates,
            "booked_court_hours_per_day": self.booked_court_hours_per_day,
            "sellable_court_hours_per_day": self.sellable_court_hours_per_day,
            "blocked_court_hours_per_day": self.blocked_court_hours_per_day,
            **OccupancyTotals.to_dict(self),
        }


@dataclass(frozen=True, slots=True)
class WeekdayWeekendSplit:
    """The weekday/weekend demand split, in court-hours."""

    weekday: DemandSegment
    weekend: DemandSegment

    @property
    def weekend_uplift(self) -> float | None:
        """Weekend booked court-hours per day over weekday, minus one.

        ``None`` when either side has no days or the weekday side sold nothing,
        because a ratio against zero demand is not a statement about anything.
        """
        weekday_rate = self.weekday.booked_court_hours_per_day
        weekend_rate = self.weekend.booked_court_hours_per_day
        if not weekday_rate or weekend_rate is None:
            return None
        return weekend_rate / weekday_rate - 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "weekday": self.weekday.to_dict(),
            "weekend": self.weekend.to_dict(),
            "weekend_uplift": self.weekend_uplift,
        }


# --------------------------------------------------------------------------
# Reduction to one settled observation per slot
# --------------------------------------------------------------------------


def settled_observations(
    observations: Iterable[SlotObservation], *, sport: Sport | None = None
) -> list[SlotObservation]:
    """Reduce an observation stream to one row per slot: its settled state.

    **The project's single definition of "the row for this slot."** Every
    aggregate in ``tracker.analytics`` funnels through it, and the
    ``v_slot_settled`` SQL view implements the same rule, so a chart reads the
    same headline number whether it came from Python or from SQL.

    For each slot the chosen row is the **last observation taken before the
    slot started** (``is_past`` still false), which is the state the slot
    actually settled in. A slot seen OPEN and later BOOKED counts once, as
    BOOKED; a slot booked and then cancelled counts once, as OPEN.

    Slots whose observations are *all* elapsed -- a slot that had already
    started when collection first reached it -- fall back to the earliest
    observation, the one closest to the slot's own start. They are kept, not
    dropped: Hudle never marks an elapsed slot unavailable, so an elapsed
    unsold slot genuinely was sellable inventory that went unsold and belongs
    in the denominator.

    Taking the highest ``snapshot_id`` instead is a *different* rule, not a
    simpler spelling of this one. Hudle keeps republishing a slot after it
    elapses, so a late cancellation or an unblocking rewrites the last row
    without changing what the slot settled as.

    ``sport`` filters before reducing, and is the filter every cross-venue
    figure needs: the sport lives on the observation because Play Padel and
    Padel Fort publish pickleball courts beside their padel one.

    The result is stable under re-application: feeding settled rows back in
    returns them unchanged.
    """
    best: dict[str, SlotObservation] = {}
    for observation in observations:
        if sport is not None and observation.sport is not sport:
            continue
        current = best.get(observation.slot_uuid)
        if current is None or _supersedes(observation, current):
            best[observation.slot_uuid] = observation
    return sorted(best.values(), key=lambda o: (o.slot_start_utc, o.slot_uuid))


def _supersedes(candidate: SlotObservation, current: SlotObservation) -> bool:
    """Whether ``candidate`` is a better settled row than ``current``."""
    if candidate.is_past != current.is_past:
        # A pre-start observation always beats an elapsed one.
        return current.is_past
    if candidate.is_past:
        # Both elapsed: the earliest sighting is nearest the slot's own start.
        return candidate.snapshot_id < current.snapshot_id
    return candidate.snapshot_id > current.snapshot_id


# --------------------------------------------------------------------------
# The headline metric
# --------------------------------------------------------------------------


def occupancy_by_venue_day(
    observations: Iterable[SlotObservation],
    *,
    venue_uuid: str | None = None,
    sport: Sport | None = None,
    business_date_from: dt.date | None = None,
    business_date_to: dt.date | None = None,
) -> list[VenueDayOccupancy]:
    """Settled occupancy per (venue, business date), in court-minutes.

    The headline metric. Each row carries booked, open and blocked
    court-minutes, both denominators and all three ratios, so a caller never
    has to recompute -- or silently redefine -- any of them.

    The optional filters are inclusive at both ends and are applied to the
    settled rows, so narrowing a date range never changes a day's arithmetic.
    Rows come back sorted by venue then business date.
    """
    rows: dict[tuple[str, dt.date], _Accumulator] = {}
    for observation in settled_observations(observations, sport=sport):
        if venue_uuid is not None and observation.venue_uuid != venue_uuid:
            continue
        if business_date_from is not None and observation.business_date < business_date_from:
            continue
        if business_date_to is not None and observation.business_date > business_date_to:
            continue
        key = (observation.venue_uuid, observation.business_date)
        rows.setdefault(key, _Accumulator()).add(observation)

    return [
        accumulator.as_venue_day(venue=key[0], business_date=key[1])
        for key, accumulator in sorted(rows.items())
    ]


def peak_hour_heatmap(
    observations: Iterable[SlotObservation],
    *,
    venue_uuid: str | None = None,
    sport: Sport | None = None,
    sparse_min_minutes: int = DEFAULT_SPARSE_MIN_MINUTES,
) -> Heatmap:
    """Hour-of-day x day-of-week occupancy in court-minutes.

    Pass ``venue_uuid`` for one venue's grid; omit it for the combined grid
    across every venue in ``observations``.

    Day of week comes from ``business_date``, never from the wall-clock date:
    a 00:30 slot sold on Saturday morning is Friday-night demand and must
    colour the Friday column. Hour of day stays wall-clock, because 00:30 is
    genuinely a half-past-midnight session.

    Each cell carries its own denominator and a ``sparse`` flag for cells
    holding less than ``sparse_min_minutes`` of sellable inventory.
    """
    cells: dict[tuple[int, int], _Accumulator] = {}
    for observation in settled_observations(observations, sport=sport):
        if venue_uuid is not None and observation.venue_uuid != venue_uuid:
            continue
        key = (observation.business_date.weekday(), slot_start_hour(observation))
        cells.setdefault(key, _Accumulator()).add(observation)

    return Heatmap(
        venue_uuid=venue_uuid,
        sparse_min_minutes=sparse_min_minutes,
        cells=tuple(
            accumulator.as_heatmap_cell(
                day_of_week=key[0], hour=key[1], sparse_min_minutes=sparse_min_minutes
            )
            for key, accumulator in sorted(cells.items())
        ),
    )


def heatmaps_by_venue(
    observations: Iterable[SlotObservation],
    *,
    sport: Sport | None = None,
    sparse_min_minutes: int = DEFAULT_SPARSE_MIN_MINUTES,
) -> dict[str, Heatmap]:
    """One :func:`peak_hour_heatmap` per venue present in ``observations``.

    Venues are never merged into one grid here: Padel Up publishes 60-minute
    slots and the other two publish 30-minute ones, and while court-minutes
    make the totals comparable, the venues' selling windows differ (Play Padel
    sells 00:00-01:30 and nobody else does), so a shared grid would show
    structural holes as if they were missing demand.
    """
    settled = settled_observations(observations, sport=sport)
    venues = sorted({observation.venue_uuid for observation in settled})
    return {
        venue: peak_hour_heatmap(settled, venue_uuid=venue, sparse_min_minutes=sparse_min_minutes)
        for venue in venues
    }


def weekday_vs_weekend(
    observations: Iterable[SlotObservation],
    *,
    venue_uuid: str | None = None,
    sport: Sport | None = None,
) -> WeekdayWeekendSplit:
    """Split settled demand into weekday and weekend, in court-hours.

    Saturday and Sunday are the weekend, decided on ``business_date`` so a
    Friday-night session sold at 00:30 on Saturday stays a weekday session.

    Both segments carry their business-date count, because five weekdays
    against two weekend days makes the raw totals a comparison of calendar
    size. ``booked_court_hours_per_day`` is the figure to plot.
    """
    segments: dict[bool, _Accumulator] = {False: _Accumulator(), True: _Accumulator()}
    for observation in settled_observations(observations, sport=sport):
        if venue_uuid is not None and observation.venue_uuid != venue_uuid:
            continue
        is_weekend = observation.business_date.weekday() in WEEKEND_DAYS
        segments[is_weekend].add(observation)

    return WeekdayWeekendSplit(
        weekday=segments[False].as_segment(label="weekday"),
        weekend=segments[True].as_segment(label="weekend"),
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


class _Accumulator:
    """Mutable court-minute tally behind every public result type.

    Private on purpose: the frozen dataclasses are the contract, and this
    exists only so the four groupings above share one definition of "add an
    observation" rather than three near-copies of the same three sums.
    """

    __slots__ = ("_by_state", "_dates", "_slots")

    def __init__(self) -> None:
        self._by_state: dict[SlotState, int] = dict.fromkeys(SlotState, 0)
        self._dates: set[dt.date] = set()
        self._slots = 0

    def add(self, observation: SlotObservation) -> None:
        self._by_state[observation.state] += observation.duration_minutes
        self._dates.add(observation.business_date)
        self._slots += 1

    def _fields(self) -> dict[str, int]:
        return {
            "booked_minutes": self._by_state[SlotState.BOOKED],
            "open_minutes": self._by_state[SlotState.OPEN],
            "blocked_minutes": self._by_state[SlotState.BLOCKED],
            "slots": self._slots,
        }

    def as_venue_day(self, *, venue: str, business_date: dt.date) -> VenueDayOccupancy:
        return VenueDayOccupancy(**self._fields(), venue_uuid=venue, business_date=business_date)

    def as_heatmap_cell(
        self, *, day_of_week: int, hour: int, sparse_min_minutes: int
    ) -> HeatmapCell:
        fields = self._fields()
        sellable = fields["booked_minutes"] + fields["open_minutes"]
        return HeatmapCell(
            **fields,
            day_of_week=day_of_week,
            hour=hour,
            business_dates=len(self._dates),
            sparse=sellable < sparse_min_minutes,
        )

    def as_segment(self, *, label: str) -> DemandSegment:
        return DemandSegment(**self._fields(), label=label, business_dates=len(self._dates))


def _ratio(numerator: int, denominator: int) -> float | None:
    """A ratio that is ``None`` -- never ``0.0`` -- on a zero denominator."""
    if denominator == 0:
        return None
    return numerator / denominator
