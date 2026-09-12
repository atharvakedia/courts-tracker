"""Market-level analytics: revenue proxy, market share, and demand shape.

This module is pure: no I/O, no database handle, no HTTP, and no call to
:func:`datetime.now`. Everything is derived from the observations the caller
supplies.

Three honesty rules are built into the return types rather than left to the
dashboard:

**Revenue is a proxy and is labelled one everywhere.** We can only see slots
that sold through Hudle. Phone and walk-in sales are invisible, and BLOCKED
inventory may well *be* those sales -- a venue that blocks slots to sell them
offline otherwise reads as an empty venue. So :func:`revenue_proxy` reports
booked revenue *and* the blocked court-hours that might represent unseen
revenue, side by side, and never presents either as actual revenue.

**Zero observed bookings is not the same as no business.** Padel Up published
589 slots across 31 days at 1800 per court-hour and recorded zero bookings. A
naive share chart reads "Padel Fort and Play Padel split the market 100%" and
silently implies Padel Up has no customers, when the likelier explanation is
that it does not take bookings through Hudle at all. :func:`market_share`
therefore returns a per-venue ``data_quality`` flag and the observation window
alongside every percentage, plus share of observed *supply* -- a denominator
that stays meaningful for a venue with no observed demand.

**Nothing is comparable as a slot count.** Padel Up sells 60-minute slots and
the other two sell 30-minute ones, so every quantity here is court-minutes
(stored) or court-hours (displayed), never slots. The same normalization
failure has a second axis: Play Padel and Padel Fort publish pickleball courts
beside their padel one, so every entry point below takes ``sport`` and a
cross-venue chart that omits it compares a three-court venue against a
one-court venue.

Every function here reduces the observation stream with
:func:`tracker.analytics.occupancy.settled_observations` -- the project's one
definition of "the row for this slot", shared with the occupancy module and
with the ``v_slot_settled`` SQL view. Nothing in this package may define a
second one: two rules print two headline numbers from one dataset.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tracker.analytics.occupancy import settled_observations, to_court_hours
from tracker.analytics.pricing import (
    CURRENCY,
    DateRange,
    MetricSpec,
    date_range_of,
    price_per_court_hour,
)
from tracker.classify import court_minutes_by_state, occupancy_strict
from tracker.types import SlotObservation, SlotState, Sport, slot_start_hour

logger = logging.getLogger("analytics.market")

#: The phrase that must travel with every revenue figure this module produces.
PROXY_LABEL = "revenue proxy: observed Hudle bookings only, not actual revenue"

#: A venue whose blocked share of listed court-time reaches this is flagged: it
#: is plausibly selling offline, and its occupancy cannot be read at face value.
HEAVY_BLOCK_SHARE = 0.20

#: A single blocked run this long, on a day with no observed bookings, is a
#: venue pulling a session from inventory rather than ordinary maintenance.
EVENING_BLOCK_MINUTES = 240

#: How much higher peak occupancy must run than off-peak before a flat-priced
#: venue is flagged as a peak-pricing candidate.
PEAK_OPPORTUNITY_GAP = 0.15


class DataQualityFlag(StrEnum):
    """Why a venue's numbers may not mean what the chart appears to say.

    These are annotations, not filters. A flagged venue stays in every chart;
    dropping it would be its own distortion.
    """

    #: No booking has ever been observed for this venue. Its demand share will
    #: read 0% indefinitely and that is not evidence of no business.
    NO_BOOKINGS_EVER_OBSERVED = "no_bookings_ever_observed"
    #: The venue published no sellable court-time at all in the window, so even
    #: its supply share is undefined.
    NO_SUPPLY_OBSERVED = "no_supply_observed"
    #: A large share of listed court-time was withdrawn from sale. Occupancy
    #: understates demand and the blocked hours may be offline sales.
    HEAVILY_BLOCKED_INVENTORY = "heavily_blocked_inventory"


# --------------------------------------------------------------------------
# Metric registry
# --------------------------------------------------------------------------


class MetricReport(Protocol):
    """Any result object that can describe its own metrics."""

    @property
    def metrics(self) -> tuple[MetricSpec, ...]: ...


@dataclass(frozen=True, slots=True)
class MetricRegistry:
    """Every metric on a page, with its denominator, date range and caveats.

    The web layer iterates this to render the denominator *on* each chart
    instead of hardcoding captions that drift away from the arithmetic. A
    metric whose caveat lives only in a template is a metric whose caveat will
    eventually be deleted by someone restyling the template.
    """

    specs: tuple[MetricSpec, ...]

    def __iter__(self) -> Iterator[MetricSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def get(self, name: str) -> MetricSpec | None:
        return next((spec for spec in self.specs if spec.name == name), None)

    @property
    def with_caveats(self) -> tuple[MetricSpec, ...]:
        return tuple(spec for spec in self.specs if spec.has_caveats)


def metric_registry(*reports: MetricReport) -> MetricRegistry:
    """Collect the metric specs of every report on a page, de-duplicated by name.

    First declaration wins, so the caller controls which report's date range is
    authoritative when two pages share a metric name.
    """
    seen: dict[str, MetricSpec] = {}
    for report in reports:
        for spec in report.metrics:
            seen.setdefault(spec.name, spec)
    return MetricRegistry(specs=tuple(seen.values()))


# --------------------------------------------------------------------------
# Revenue proxy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevenueProxyRow:
    """One venue's observed booked revenue for one week, plus what we cannot see.

    ``booked_revenue_proxy`` is booked court-hours priced at the published rate.
    ``blocked_revenue_if_sold`` is the same arithmetic applied to withdrawn
    inventory and is explicitly hypothetical: it is the ceiling on what offline
    sales *could* have been worth if every blocked slot was sold at rack rate,
    which is an assumption, not an observation.
    """

    venue_uuid: str
    week_start: dt.date
    booked_court_hours: float
    booked_revenue_proxy: float
    blocked_court_hours: float
    blocked_revenue_if_sold: float
    open_court_hours: float
    listed_court_hours: float
    booked_slots: int
    blocked_slots: int
    unpriced_slots: int
    currency: str = CURRENCY

    @property
    def week_label(self) -> str:
        """ISO week label, e.g. ``2026-W37``."""
        iso = self.week_start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    @property
    def upper_bound_revenue_proxy(self) -> float:
        """Booked plus every blocked hour valued at rack rate. An upper bound only."""
        return round(self.booked_revenue_proxy + self.blocked_revenue_if_sold, 2)


@dataclass(frozen=True, slots=True)
class VenueRevenueTotal:
    """A venue's revenue proxy summed over the whole window."""

    venue_uuid: str
    booked_court_hours: float
    booked_revenue_proxy: float
    blocked_court_hours: float
    blocked_revenue_if_sold: float
    listed_court_hours: float
    weeks: int
    currency: str = CURRENCY


@dataclass(frozen=True, slots=True)
class RevenueProxyReport:
    """Booked revenue proxy per venue per week, never labelled actual revenue."""

    rows: tuple[RevenueProxyRow, ...]
    venue_totals: tuple[VenueRevenueTotal, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]
    label: str = PROXY_LABEL
    is_proxy: bool = True

    def total_for(self, venue_uuid: str) -> VenueRevenueTotal | None:
        return next((t for t in self.venue_totals if t.venue_uuid == venue_uuid), None)

    def rows_for(self, venue_uuid: str) -> tuple[RevenueProxyRow, ...]:
        return tuple(row for row in self.rows if row.venue_uuid == venue_uuid)


def revenue_proxy(
    observations: Iterable[SlotObservation], *, sport: Sport | None = None
) -> RevenueProxyReport:
    """Booked court-hours priced at the published rate, per venue per week.

    This is a proxy and nothing more. It sees only what sold through Hudle: a
    phone booking, a walk-in, a membership session and a corporate block are all
    invisible. Blocked court-hours are reported on the same row precisely
    because they are the most likely home of those unseen sales, so the reader
    can see the size of the blind spot next to the number.

    Weeks are ISO weeks of the slot's ``business_date``, so Play Padel's
    00:30 Saturday sessions count toward the Friday that produced them.
    Observations are reduced to the latest poll per slot first, so passing a
    full multi-poll history does not multiply revenue by the poll count.
    """
    reduced = settled_observations(observations, sport=sport)
    buckets: dict[tuple[str, dt.date], list[SlotObservation]] = defaultdict(list)
    for observation in reduced:
        buckets[(observation.venue_uuid, week_start_for(observation.business_date))].append(
            observation
        )

    rows = tuple(
        _revenue_row(venue_uuid, week_start, bucket)
        for (venue_uuid, week_start), bucket in sorted(buckets.items())
    )
    venue_totals = tuple(
        _revenue_total(venue_uuid, [row for row in rows if row.venue_uuid == venue_uuid])
        for venue_uuid in sorted({row.venue_uuid for row in rows})
    )
    return RevenueProxyReport(
        rows=rows,
        venue_totals=venue_totals,
        window=date_range_of(reduced),
        metrics=(
            MetricSpec(
                name="revenue_proxy",
                title="Booked revenue proxy",
                unit=f"{CURRENCY} per ISO week",
                denominator=(
                    "booked court-hours multiplied by the published rate per "
                    "court-hour, summed per venue per ISO week of business_date"
                ),
                date_range=date_range_of(reduced),
                caveats=(
                    PROXY_LABEL,
                    "Offline, phone and walk-in sales are invisible to this API; "
                    "blocked court-hours are reported alongside because they are the "
                    "most likely home of those sales.",
                    "Priced at the published rack rate, so discounts, packages and "
                    "memberships are not reflected.",
                    "blocked_revenue_if_sold assumes every withdrawn slot sold at rack "
                    "rate. It is a ceiling, not an estimate.",
                    "Some blocked time is a standing venue rule and was never sellable "
                    "at all -- Padel Up blocks 05:00 every single day -- so its "
                    "blocked_revenue_if_sold is inflated by an hour a day that nobody "
                    "was ever going to buy. Read it against blocked_inventory's run "
                    "lengths, not on its own.",
                ),
            ),
        ),
    )


def _revenue_row(
    venue_uuid: str, week_start: dt.date, bucket: Sequence[SlotObservation]
) -> RevenueProxyRow:
    minutes = court_minutes_by_state(bucket)
    booked = [o for o in bucket if o.state is SlotState.BOOKED]
    blocked = [o for o in bucket if o.state is SlotState.BLOCKED]
    return RevenueProxyRow(
        venue_uuid=venue_uuid,
        week_start=week_start,
        booked_court_hours=_hours(minutes[SlotState.BOOKED]),
        booked_revenue_proxy=_revenue_of(booked),
        blocked_court_hours=_hours(minutes[SlotState.BLOCKED]),
        blocked_revenue_if_sold=_revenue_of(blocked),
        open_court_hours=_hours(minutes[SlotState.OPEN]),
        listed_court_hours=_hours(sum(minutes.values())),
        booked_slots=len(booked),
        blocked_slots=len(blocked),
        unpriced_slots=sum(1 for o in bucket if o.price is None),
    )


def _revenue_total(venue_uuid: str, rows: Sequence[RevenueProxyRow]) -> VenueRevenueTotal:
    return VenueRevenueTotal(
        venue_uuid=venue_uuid,
        booked_court_hours=round(sum(row.booked_court_hours for row in rows), 4),
        booked_revenue_proxy=round(sum(row.booked_revenue_proxy for row in rows), 2),
        blocked_court_hours=round(sum(row.blocked_court_hours for row in rows), 4),
        blocked_revenue_if_sold=round(sum(row.blocked_revenue_if_sold for row in rows), 2),
        listed_court_hours=round(sum(row.listed_court_hours for row in rows), 4),
        weeks=len(rows),
    )


def _revenue_of(observations: Iterable[SlotObservation]) -> float:
    """Sum published slot prices. Unpriced slots contribute nothing, not a guess."""
    return round(sum(o.price for o in observations if o.price is not None), 2)


# --------------------------------------------------------------------------
# Market share
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketShareRow:
    """One venue's share of one week, in demand and in supply.

    Both shares carry their absolute court-hours, because a percentage of a tiny
    denominator is the most persuasive way to mislead a reader. ``demand_share``
    is ``None`` -- never ``0.0`` -- when nobody booked anything that week: no
    market existed to take a share of.
    """

    venue_uuid: str
    week_start: dt.date
    booked_court_hours: float
    demand_share: float | None
    listed_court_hours: float
    sellable_court_hours: float
    supply_share: float | None
    blocked_court_hours: float

    @property
    def week_label(self) -> str:
        iso = self.week_start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"


@dataclass(frozen=True, slots=True)
class VenueDataQuality:
    """Why a venue's share may not mean what it looks like.

    ``note`` is a finished sentence so the dashboard annotates the series
    rather than inventing its own wording.
    """

    venue_uuid: str
    flags: tuple[DataQualityFlag, ...]
    booked_court_hours: float
    listed_court_hours: float
    blocked_court_hours: float
    blocked_share: float | None
    note: str

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)

    def has(self, flag: DataQualityFlag) -> bool:
        return flag in self.flags


@dataclass(frozen=True, slots=True)
class MarketShareReport:
    """Share of observed booked court-hours, with supply share and honesty flags."""

    rows: tuple[MarketShareRow, ...]
    venues: tuple[VenueDataQuality, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]

    def rows_for(self, venue_uuid: str) -> tuple[MarketShareRow, ...]:
        return tuple(row for row in self.rows if row.venue_uuid == venue_uuid)

    def quality_for(self, venue_uuid: str) -> VenueDataQuality | None:
        return next((v for v in self.venues if v.venue_uuid == venue_uuid), None)

    @property
    def flagged_venues(self) -> tuple[VenueDataQuality, ...]:
        return tuple(venue for venue in self.venues if venue.has_flags)

    @property
    def caveat_summary(self) -> str:
        """One sentence naming the venues whose share needs annotating."""
        flagged = self.flagged_venues
        if not flagged:
            return "No venue carries a data-quality flag in this window."
        return "; ".join(venue.note for venue in flagged)


def market_share(
    observations: Iterable[SlotObservation],
    *,
    sport: Sport | None = None,
    heavy_block_share: float = HEAVY_BLOCK_SHARE,
) -> MarketShareReport:
    """Share of observed booked court-hours per venue per week, honestly framed.

    Every percentage arrives with its absolute court-hours, and every venue
    arrives with a data-quality flag set. That matters here more than anywhere
    else on the dashboard: Padel Up has recorded zero bookings across the whole
    observed window, so a bare demand-share pie assigns 100% of "the market" to
    the other two and reads as a verdict on Padel Up's business. It is not one.
    The likelier reading is that Padel Up does not sell through Hudle, which is
    a statement about our instrument, not about their customers.

    Share of observed *supply* -- listed court-hours -- is returned alongside
    for exactly that reason: it is a denominator that stays meaningful for a
    venue with no observed demand, and Padel Up's supply share is substantial.
    """
    reduced = settled_observations(observations, sport=sport)
    weeks: dict[dt.date, dict[str, list[SlotObservation]]] = defaultdict(lambda: defaultdict(list))
    for observation in reduced:
        weeks[week_start_for(observation.business_date)][observation.venue_uuid].append(observation)

    rows: list[MarketShareRow] = []
    for week_start, by_venue in sorted(weeks.items()):
        per_venue = {
            venue_uuid: court_minutes_by_state(bucket) for venue_uuid, bucket in by_venue.items()
        }
        booked_total = sum(m[SlotState.BOOKED] for m in per_venue.values())
        listed_total = sum(sum(m.values()) for m in per_venue.values())
        for venue_uuid, minutes in sorted(per_venue.items()):
            booked = minutes[SlotState.BOOKED]
            listed = sum(minutes.values())
            rows.append(
                MarketShareRow(
                    venue_uuid=venue_uuid,
                    week_start=week_start,
                    booked_court_hours=_hours(booked),
                    demand_share=_share(booked, booked_total),
                    listed_court_hours=_hours(listed),
                    sellable_court_hours=_hours(
                        minutes[SlotState.BOOKED] + minutes[SlotState.OPEN]
                    ),
                    supply_share=_share(listed, listed_total),
                    blocked_court_hours=_hours(minutes[SlotState.BLOCKED]),
                )
            )

    venues = tuple(
        _venue_quality(
            venue_uuid,
            [o for o in reduced if o.venue_uuid == venue_uuid],
            heavy_block_share=heavy_block_share,
        )
        for venue_uuid in sorted({o.venue_uuid for o in reduced})
    )
    window = date_range_of(reduced)
    return MarketShareReport(
        rows=tuple(rows),
        venues=venues,
        window=window,
        metrics=(
            MetricSpec(
                name="market_share_demand",
                title="Share of observed booked court-hours",
                unit="share of total booked court-hours",
                denominator=(
                    "total booked court-hours across all observed venues in the same "
                    "ISO week; NULL when no venue booked anything that week"
                ),
                date_range=window,
                caveats=(
                    "Only bookings made through Hudle are observable. A venue with a "
                    "0% share may simply not sell through Hudle -- Padel Up has "
                    "recorded zero bookings at the highest rate of the three.",
                    "Blocked inventory may be offline sales and is excluded from "
                    "booked court-hours; see the blocked column.",
                    "A forward-looking window: a slot still in the future may yet sell.",
                ),
            ),
            MetricSpec(
                name="market_share_supply",
                title="Share of observed listed court-hours",
                unit="share of total listed court-hours",
                denominator=(
                    "total listed court-hours across all observed venues in the same "
                    "ISO week, booked plus open plus blocked"
                ),
                date_range=window,
                caveats=(
                    "Supply, not demand. This stays meaningful for a venue with no "
                    "observed bookings and is the honest denominator for one.",
                    "Court-hours, never slot counts: Padel Up's 60-minute grid makes "
                    "its slot count half its true share.",
                ),
            ),
        ),
    )


def _venue_quality(
    venue_uuid: str,
    observations: Sequence[SlotObservation],
    *,
    heavy_block_share: float,
) -> VenueDataQuality:
    minutes = court_minutes_by_state(observations)
    booked = minutes[SlotState.BOOKED]
    blocked = minutes[SlotState.BLOCKED]
    listed = sum(minutes.values())
    blocked_share = _share(blocked, listed)

    flags: list[DataQualityFlag] = []
    notes: list[str] = []
    if listed == 0:
        flags.append(DataQualityFlag.NO_SUPPLY_OBSERVED)
        notes.append("published no court-time in this window, so no share is defined")
    if booked == 0 and listed > 0:
        flags.append(DataQualityFlag.NO_BOOKINGS_EVER_OBSERVED)
        notes.append(
            "has never been observed with a booking, so its demand share reads 0%; "
            "it may not take bookings through Hudle at all"
        )
    if blocked_share is not None and blocked_share >= heavy_block_share:
        flags.append(DataQualityFlag.HEAVILY_BLOCKED_INVENTORY)
        notes.append(
            f"withdrew {blocked_share:.0%} of its listed court-time from sale, which "
            "may be offline bookings rather than idle courts"
        )

    note = f"{venue_uuid}: " + "; ".join(notes) if notes else f"{venue_uuid}: no flags"
    return VenueDataQuality(
        venue_uuid=venue_uuid,
        flags=tuple(flags),
        booked_court_hours=_hours(booked),
        listed_court_hours=_hours(listed),
        blocked_court_hours=_hours(blocked),
        blocked_share=blocked_share,
        note=note,
    )


# --------------------------------------------------------------------------
# Demand shape
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemandByHourRow:
    """Booked court-minutes in one hour of the day at one venue.

    ``business_dates`` is the set of trading days that contributed, so the
    dashboard can print the real denominator ("over 21 trading days") instead of
    implying a single day. It is also where the post-midnight roll-back is
    visible: a 00:30 Saturday slot appears in hour 0 attributed to Friday's
    business date, because it is Friday-night demand.
    """

    venue_uuid: str
    hour: int
    booked_court_minutes: int
    open_court_minutes: int
    blocked_court_minutes: int
    listed_court_minutes: int
    business_dates: tuple[dt.date, ...]

    @property
    def booked_court_hours(self) -> float:
        return _hours(self.booked_court_minutes)

    @property
    def day_count(self) -> int:
        return len(self.business_dates)

    @property
    def occupancy_strict(self) -> float | None:
        """Booked over sellable court-minutes in this hour. ``None`` if none sellable."""
        return _share(
            self.booked_court_minutes, self.booked_court_minutes + self.open_court_minutes
        )


@dataclass(frozen=True, slots=True)
class DemandByHourReport:
    """Booked court-minutes by hour of day per venue."""

    rows: tuple[DemandByHourRow, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]

    def rows_for(self, venue_uuid: str) -> tuple[DemandByHourRow, ...]:
        return tuple(row for row in self.rows if row.venue_uuid == venue_uuid)

    def row(self, venue_uuid: str, hour: int) -> DemandByHourRow | None:
        return next((r for r in self.rows if r.venue_uuid == venue_uuid and r.hour == hour), None)

    @property
    def busiest_hour(self) -> DemandByHourRow | None:
        booked = [row for row in self.rows if row.booked_court_minutes > 0]
        return max(booked, key=lambda row: row.booked_court_minutes) if booked else None


def demand_by_hour(
    observations: Iterable[SlotObservation], *, sport: Sport | None = None
) -> DemandByHourReport:
    """Booked court-minutes by hour of day per venue: the peak-pricing basis.

    The hour is the local wall-clock hour the slot starts in, taken from the
    stored wall-clock text, so no timezone conversion happens here. Each row
    also carries the trading days that contributed, and those are
    ``business_date`` values: a 00:30 slot belongs to the previous business date
    because it is the previous evening's demand. Aggregating on the raw local
    date would file Friday-night sales under Saturday and move them to the wrong
    day of week.
    """
    reduced = settled_observations(observations, sport=sport)
    buckets: dict[tuple[str, int], list[SlotObservation]] = defaultdict(list)
    for observation in reduced:
        buckets[(observation.venue_uuid, slot_start_hour(observation))].append(observation)

    rows = tuple(
        _demand_row(venue_uuid, hour, bucket)
        for (venue_uuid, hour), bucket in sorted(buckets.items())
    )
    window = date_range_of(reduced)
    return DemandByHourReport(
        rows=rows,
        window=window,
        metrics=(
            MetricSpec(
                name="demand_by_hour",
                title="Booked court-time by hour of day",
                unit="court-minutes",
                denominator=(
                    "court-minutes of slots starting in each local hour, summed over "
                    "every business_date in the window; the row carries its own day count"
                ),
                date_range=window,
                caveats=(
                    "Bucketed on business_date, so a post-midnight slot counts toward "
                    "the previous evening rather than the calendar day it falls on.",
                    "Only Hudle bookings are visible; blocked court-minutes are "
                    "reported separately and may be offline sales.",
                    "A forward-looking window mixes elapsed hours with hours that may "
                    "still sell; filter on is_past for a current-availability read.",
                ),
            ),
        ),
    )


def _demand_row(venue_uuid: str, hour: int, bucket: Sequence[SlotObservation]) -> DemandByHourRow:
    minutes = court_minutes_by_state(bucket)
    return DemandByHourRow(
        venue_uuid=venue_uuid,
        hour=hour,
        booked_court_minutes=minutes[SlotState.BOOKED],
        open_court_minutes=minutes[SlotState.OPEN],
        blocked_court_minutes=minutes[SlotState.BLOCKED],
        listed_court_minutes=sum(minutes.values()),
        business_dates=tuple(sorted({o.business_date for o in bucket})),
    )


@dataclass(frozen=True, slots=True)
class DemandHeatmapCell:
    """Booked court-minutes for one weekday-by-hour cell at one venue."""

    venue_uuid: str
    weekday: int
    hour: int
    booked_court_minutes: int
    listed_court_minutes: int
    business_dates: tuple[dt.date, ...]

    @property
    def weekday_name(self) -> str:
        return _WEEKDAY_NAMES[self.weekday]

    @property
    def day_count(self) -> int:
        return len(self.business_dates)

    @property
    def booked_court_minutes_per_day(self) -> float | None:
        """Per-trading-day average, so unequal day counts do not distort the map."""
        if not self.business_dates:
            return None
        return round(self.booked_court_minutes / len(self.business_dates), 4)


@dataclass(frozen=True, slots=True)
class DemandHeatmapReport:
    """Weekday-by-hour demand, averaged per trading day."""

    cells: tuple[DemandHeatmapCell, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]

    def cell(self, venue_uuid: str, weekday: int, hour: int) -> DemandHeatmapCell | None:
        return next(
            (
                c
                for c in self.cells
                if c.venue_uuid == venue_uuid and c.weekday == weekday and c.hour == hour
            ),
            None,
        )


def demand_heatmap(
    observations: Iterable[SlotObservation], *, sport: Sport | None = None
) -> DemandHeatmapReport:
    """Booked court-minutes per weekday-by-hour cell, per trading day.

    The weekday comes from ``business_date``, which is the whole point: Play
    Padel's 00:30 Saturday slots are Friday-night sessions, and a heatmap built
    on the raw local date would show Saturday demand at midnight that does not
    exist and hide the Friday demand that does.

    Cells carry a per-trading-day average as well as a total, because a 21-day
    window holds three Fridays and two Mondays and a raw total would rank
    weekdays by how many of them the window happened to contain.
    """
    reduced = settled_observations(observations, sport=sport)
    buckets: dict[tuple[str, int, int], list[SlotObservation]] = defaultdict(list)
    for observation in reduced:
        key = (
            observation.venue_uuid,
            observation.business_date.weekday(),
            slot_start_hour(observation),
        )
        buckets[key].append(observation)

    cells = tuple(
        DemandHeatmapCell(
            venue_uuid=venue_uuid,
            weekday=weekday,
            hour=hour,
            booked_court_minutes=court_minutes_by_state(bucket)[SlotState.BOOKED],
            listed_court_minutes=sum(court_minutes_by_state(bucket).values()),
            business_dates=tuple(sorted({o.business_date for o in bucket})),
        )
        for (venue_uuid, weekday, hour), bucket in sorted(buckets.items())
    )
    window = date_range_of(reduced)
    return DemandHeatmapReport(
        cells=cells,
        window=window,
        metrics=(
            MetricSpec(
                name="demand_heatmap",
                title="Booked court-time by weekday and hour",
                unit="court-minutes per trading day",
                denominator=(
                    "court-minutes booked in each weekday-by-hour cell divided by the "
                    "number of business_dates of that weekday in the window"
                ),
                date_range=window,
                caveats=(
                    "The weekday is the business_date's weekday, so post-midnight "
                    "slots count toward the previous evening.",
                    "A short window contains unequal counts of each weekday; use the "
                    "per-day average, not the total.",
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Blocked inventory
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedDayRow:
    """One court's withdrawn inventory on one trading day.

    Blocked court-time is the dataset's biggest blind spot and its most
    interesting signal at once. Padel Fort withdrew all 14 slots from 17:00 to
    23:30 on 2026-09-13 with zero bookings recorded: 420 court-minutes of prime
    evening inventory. Folded into occupancy that day looks like an empty venue;
    reported as its own number it looks like what it probably is, an evening
    sold or closed outside Hudle.
    """

    venue_uuid: str
    facility_uuid: str
    business_date: dt.date
    blocked_court_hours: float
    booked_court_hours: float
    listed_court_hours: float
    blocked_share: float | None
    blocked_slots: int
    longest_blocked_run_minutes: int
    longest_blocked_run_start_local: str | None
    whole_session_withdrawn: bool


@dataclass(frozen=True, slots=True)
class BlockedInventoryReport:
    """Withdrawn inventory per court per trading day, never folded into occupancy."""

    rows: tuple[BlockedDayRow, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]

    @property
    def withdrawn_sessions(self) -> tuple[BlockedDayRow, ...]:
        """Days where a whole session was withdrawn and nothing was booked."""
        return tuple(row for row in self.rows if row.whole_session_withdrawn)

    def rows_for(self, venue_uuid: str) -> tuple[BlockedDayRow, ...]:
        return tuple(row for row in self.rows if row.venue_uuid == venue_uuid)


def blocked_inventory(
    observations: Iterable[SlotObservation],
    *,
    sport: Sport | None = None,
    session_minutes: int = EVENING_BLOCK_MINUTES,
) -> BlockedInventoryReport:
    """Blocked court-time per court per trading day, with run-length detection.

    A scattered blocked slot is maintenance. A contiguous run covering a whole
    evening on a day with no observed bookings is a venue selling or closing
    that session outside Hudle, and the two must not average together into a
    single "blocked share" number. ``longest_blocked_run_minutes`` measures the
    run in court-minutes by chaining slots whose UTC start matches the previous
    slot's end, so it works across a 60-minute grid and a 30-minute one alike,
    and across midnight.
    """
    reduced = settled_observations(observations, sport=sport)
    buckets: dict[tuple[str, str, dt.date], list[SlotObservation]] = defaultdict(list)
    for observation in reduced:
        buckets[
            (observation.venue_uuid, observation.facility_uuid, observation.business_date)
        ].append(observation)

    rows: list[BlockedDayRow] = []
    for (venue_uuid, facility_uuid, business_date), bucket in sorted(buckets.items()):
        minutes = court_minutes_by_state(bucket)
        blocked = [o for o in bucket if o.state is SlotState.BLOCKED]
        if not blocked:
            continue
        run_minutes, run_start = _longest_contiguous_run(blocked)
        listed = sum(minutes.values())
        rows.append(
            BlockedDayRow(
                venue_uuid=venue_uuid,
                facility_uuid=facility_uuid,
                business_date=business_date,
                blocked_court_hours=_hours(minutes[SlotState.BLOCKED]),
                booked_court_hours=_hours(minutes[SlotState.BOOKED]),
                listed_court_hours=_hours(listed),
                blocked_share=_share(minutes[SlotState.BLOCKED], listed),
                blocked_slots=len(blocked),
                longest_blocked_run_minutes=run_minutes,
                longest_blocked_run_start_local=run_start,
                whole_session_withdrawn=(
                    run_minutes >= session_minutes and minutes[SlotState.BOOKED] == 0
                ),
            )
        )

    window = date_range_of(reduced)
    return BlockedInventoryReport(
        rows=tuple(rows),
        window=window,
        metrics=(
            MetricSpec(
                name="blocked_inventory",
                title="Withdrawn inventory",
                unit="court-hours",
                denominator=(
                    "blocked court-hours over listed court-hours, per court per "
                    "business_date; NULL when nothing was listed"
                ),
                date_range=window,
                caveats=(
                    "A blocked slot is not sellable and is excluded from "
                    "occupancy_strict on purpose; it is never evidence of no demand.",
                    "We cannot distinguish maintenance, a venue closure and an offline "
                    "booking. Run length is the only signal we have and it is "
                    "suggestive, not conclusive.",
                    "Padel Up blocks 05:00 every single day; that is a standing venue "
                    "rule, not withdrawn demand.",
                ),
            ),
        ),
    )


def _longest_contiguous_run(blocked: Sequence[SlotObservation]) -> tuple[int, str | None]:
    """Longest chain of blocked slots where each starts exactly as the last ends."""
    ordered = sorted(blocked, key=lambda o: o.slot_start_utc)
    best_minutes = 0
    best_start: str | None = None
    run_minutes = 0
    run_start: str | None = None
    run_end: dt.datetime | None = None
    for observation in ordered:
        if run_end is not None and observation.slot_start_utc == run_end:
            run_minutes += observation.duration_minutes
        else:
            run_minutes = observation.duration_minutes
            run_start = observation.slot_start_local
        run_end = observation.slot_start_utc + dt.timedelta(minutes=observation.duration_minutes)
        if run_minutes > best_minutes:
            best_minutes = run_minutes
            best_start = run_start
    return best_minutes, best_start


# --------------------------------------------------------------------------
# Peak-pricing opportunity
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeakOpportunityRow:
    """Peak versus off-peak occupancy at one venue, against a flat price.

    A hypothesis generator, not a revenue forecast. If a venue sells a far
    higher share of its evening inventory than its daytime inventory while
    charging one flat rate all day, that is a pricing question worth asking.
    It is not an estimate of what differential pricing would earn: this dataset
    contains no price elasticity, no lost demand and no competitor response.
    """

    venue_uuid: str
    peak_hours: tuple[int, ...]
    peak_occupancy_strict: float | None
    offpeak_occupancy_strict: float | None
    peak_booked_court_hours: float
    peak_sellable_court_hours: float
    offpeak_booked_court_hours: float
    offpeak_sellable_court_hours: float
    price_is_flat: bool
    price_per_court_hour: float | None
    currency: str = CURRENCY

    @property
    def occupancy_gap(self) -> float | None:
        """Peak minus off-peak occupancy. ``None`` if either side had no inventory."""
        if self.peak_occupancy_strict is None or self.offpeak_occupancy_strict is None:
            return None
        return round(self.peak_occupancy_strict - self.offpeak_occupancy_strict, 6)

    @property
    def is_candidate(self) -> bool:
        """Whether this venue is worth asking a peak-pricing question about."""
        gap = self.occupancy_gap
        return self.price_is_flat and gap is not None and gap >= PEAK_OPPORTUNITY_GAP


@dataclass(frozen=True, slots=True)
class PeakOpportunityReport:
    """Where a flat price meets an uneven demand curve."""

    rows: tuple[PeakOpportunityRow, ...]
    window: DateRange
    metrics: tuple[MetricSpec, ...]

    @property
    def candidates(self) -> tuple[PeakOpportunityRow, ...]:
        return tuple(row for row in self.rows if row.is_candidate)

    def row(self, venue_uuid: str) -> PeakOpportunityRow | None:
        return next((r for r in self.rows if r.venue_uuid == venue_uuid), None)


def peak_pricing_opportunity(
    observations: Iterable[SlotObservation],
    *,
    peak_hours: Sequence[int],
    sport: Sport | None = None,
) -> PeakOpportunityReport:
    """Compare peak and off-peak occupancy at venues that charge one flat rate.

    ``peak_hours`` is the dashboard's configured evening window, passed in
    rather than assumed, so changing the split is a config edit and not a code
    edit. Occupancy is strict on both sides -- blocked court-time is excluded
    from numerator and denominator alike -- so a venue that withdraws its
    evenings does not appear to have sold them.
    """
    reduced = settled_observations(observations, sport=sport)
    peaks = tuple(sorted(set(peak_hours)))
    by_venue: dict[str, list[SlotObservation]] = defaultdict(list)
    for observation in reduced:
        by_venue[observation.venue_uuid].append(observation)

    rows: list[PeakOpportunityRow] = []
    for venue_uuid, bucket in sorted(by_venue.items()):
        peak = [o for o in bucket if slot_start_hour(o) in peaks]
        offpeak = [o for o in bucket if slot_start_hour(o) not in peaks]
        peak_occupancy = occupancy_strict(peak)
        offpeak_occupancy = occupancy_strict(offpeak)
        rates = {rate for o in bucket if (rate := price_per_court_hour(o)) is not None}
        rows.append(
            PeakOpportunityRow(
                venue_uuid=venue_uuid,
                peak_hours=peaks,
                peak_occupancy_strict=peak_occupancy.ratio,
                offpeak_occupancy_strict=offpeak_occupancy.ratio,
                peak_booked_court_hours=_hours(peak_occupancy.numerator_minutes),
                peak_sellable_court_hours=_hours(peak_occupancy.denominator_minutes),
                offpeak_booked_court_hours=_hours(offpeak_occupancy.numerator_minutes),
                offpeak_sellable_court_hours=_hours(offpeak_occupancy.denominator_minutes),
                price_is_flat=len(rates) == 1,
                price_per_court_hour=next(iter(rates)) if len(rates) == 1 else None,
            )
        )

    window = date_range_of(reduced)
    return PeakOpportunityReport(
        rows=tuple(rows),
        window=window,
        metrics=(
            MetricSpec(
                name="peak_pricing_opportunity",
                title="Peak versus off-peak occupancy against a flat price",
                unit="occupancy_strict, court-hours",
                denominator=(
                    "booked court-hours over booked plus open court-hours, computed "
                    f"separately for slots starting in {list(peaks)} and outside them"
                ),
                date_range=window,
                caveats=(
                    "A question, not a forecast. This dataset holds no price "
                    "elasticity, no turned-away demand and no competitor response, so "
                    "it cannot estimate the revenue of a price change.",
                    "Blocked court-time is excluded from both sides; a venue that "
                    "withdraws its evenings will not look sold out.",
                    "A forward-looking window: future off-peak slots may still sell.",
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def week_start_for(business_date: dt.date) -> dt.date:
    """The Monday of the ISO week containing ``business_date``."""
    return business_date - dt.timedelta(days=business_date.weekday())


def _hours(minutes: float) -> float:
    """Court-minutes to court-hours, via the one supported conversion.

    Not rounded: an occupancy panel and a market panel showing the same venue's
    court-hours have to cross-foot to the last digit, and rounding here made
    them disagree by ~1e-4. Rounding belongs at the display edge.
    """
    return to_court_hours(minutes)


def _share(numerator: float, denominator: float) -> float | None:
    """A ratio that is ``None`` -- never ``0.0`` -- when nothing was measurable.

    "Nobody booked anything this week" and "this venue booked none of what was
    available" are different facts, and a zero that means both is how a blocked
    evening comes to look like an empty one.
    """
    if denominator <= 0:
        return None
    return numerator / denominator
