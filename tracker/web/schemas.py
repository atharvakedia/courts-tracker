"""Pydantic response models for the JSON API.

Two rules shape this module.

**Nothing from SQLAlchemy reaches here.** Every ``from_*`` constructor takes a
frozen dataclass out of ``tracker.analytics`` or ``tracker.types`` and copies
named fields across. The web layer never sees a Row, and the analytics layer
never learns that pydantic exists.

**Every metric response states its own denominator.** A chart drawn from this
API is required to print what the number was divided by and which business
dates it covers, so the API supplies both rather than letting the frontend
invent a caption. :class:`MetricMeta` carries the numerator, the denominator,
a prose description of the denominator, the date range and every honesty
caveat -- the ones declared statically in :data:`METRIC_DEFINITIONS` plus the
ones the analytics function attached to its own
:class:`~tracker.analytics.pricing.MetricSpec` at computation time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from tracker.analytics.coverage import CoverageReport, FacilityDayCoverage, GapInterval
from tracker.analytics.leadtime import (
    DAY_NAMES,
    FirstSlotToGo,
    LeadTimeDistribution,
    LeadTimeStats,
    SelloutRecord,
    SelloutReport,
    SlotBooking,
)
from tracker.analytics.market import (
    MarketShareReport,
    MarketShareRow,
    RevenueProxyReport,
    RevenueProxyRow,
    VenueDataQuality,
    VenueRevenueTotal,
)
from tracker.analytics.occupancy import (
    DemandSegment,
    Heatmap,
    HeatmapCell,
    OccupancyTotals,
    VenueDayOccupancy,
    WeekdayWeekendSplit,
)
from tracker.analytics.pricing import (
    CourtPriceProfile,
    DateRange,
    MetricSpec,
    PriceByHourCell,
    PriceByHourTable,
    PriceChange,
    PricePoint,
    PriceTimeline,
    PriceTimelineReport,
    VenuePriceProfile,
)
from tracker.analytics.transitions import BlockedInventoryEvent, CancellationRate
from tracker.config import Config
from tracker.types import FacilityDim, SnapshotRecord, VenueDim
from tracker.web.deps import Filters

# --------------------------------------------------------------------------
# Metric self-description
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """What one endpoint's headline number is, and what it is divided by.

    The analytics layer describes its own denominators on ``MetricSpec``; this
    table covers the endpoints whose numbers the web layer assembles itself
    (occupancy, lead time, coverage) and supplies the numerator, which no
    ``MetricSpec`` carries. Both are merged into :class:`MetricMeta`.
    """

    name: str
    title: str
    unit: str
    numerator: str
    denominator: str
    denominator_description: str
    caveats: tuple[str, ...] = ()


_NORMALIZATION_CAVEAT = (
    "Court-minutes, never slot counts: a 60-minute slot is two 30-minute slots, "
    "so venues on different grids are only comparable in court-hours."
)
_BLOCKED_CAVEAT = (
    "Blocked court-time is excluded from the strict denominator and reported "
    "separately: a venue that withdraws slots to sell them offline would "
    "otherwise read as a venue with no demand."
)
_BUSINESS_DATE_CAVEAT = (
    "Aggregated on business_date, so a 00:30 Saturday slot counts as Friday night's demand."
)
_SETTLED_CAVEAT = (
    "One observation per slot: the last poll taken before the slot started, so "
    "a slot seen open then booked counts once, as booked."
)
_CENSORED_CAVEAT = (
    "Slots already booked in the first poll that ever saw them are excluded, "
    "not counted as zero-lead bookings; excluded_censored reports how many."
)
_UNCERTAINTY_CAVEAT = (
    "A change is only known to have happened between two polls, so every row "
    "carries uncertainty_minutes -- the width of that window."
)

METRIC_DEFINITIONS: Mapping[str, MetricDefinition] = {
    definition.name: definition
    for definition in (
        MetricDefinition(
            name="venues",
            title="Venues and courts",
            unit="rows",
            numerator="configured and observed venues",
            denominator="the frozen venue set in config.yaml",
            denominator_description=(
                "Venues come from the reviewed config, not from whatever the "
                "collector happened to see; observation dates are attached where "
                "the collector has seen the venue."
            ),
        ),
        MetricDefinition(
            name="occupancy_daily",
            title="Daily occupancy",
            unit="ratio and court-hours",
            numerator="booked court-minutes",
            denominator="booked + open court-minutes (strict)",
            denominator_description=(
                "occupancy_strict divides booked court-minutes by sellable "
                "(booked + open) court-minutes. occupancy_gross divides "
                "booked + blocked by all listed court-minutes, which is the "
                "share a walk-up customer could not buy."
            ),
            caveats=(
                _NORMALIZATION_CAVEAT,
                _BLOCKED_CAVEAT,
                _BUSINESS_DATE_CAVEAT,
                _SETTLED_CAVEAT,
            ),
        ),
        MetricDefinition(
            name="occupancy_heatmap",
            title="Occupancy by hour of day and day of week",
            unit="ratio and court-hours",
            numerator="booked court-minutes in the cell",
            denominator="booked + open court-minutes in the cell",
            denominator_description=(
                "Each cell divides by its own sellable court-minutes, and "
                "reports how many business dates fed it. A cell below the sparse "
                "threshold is flagged sparse and must not be read as a rate."
            ),
            caveats=(_NORMALIZATION_CAVEAT, _BUSINESS_DATE_CAVEAT, _SETTLED_CAVEAT),
        ),
        MetricDefinition(
            name="leadtime",
            title="Booking lead time",
            unit="hours before slot start",
            numerator="hours between the booking being first seen and the slot starting",
            denominator="slots with a usable, uncensored first booking (n)",
            denominator_description=(
                "Percentiles are over slots, not over customers: one slot booked "
                "is one sample, whatever its length. n, excluded_censored and "
                "post_start are reported beside every statistic."
            ),
            caveats=(_CENSORED_CAVEAT, _UNCERTAINTY_CAVEAT),
        ),
        MetricDefinition(
            name="pricing_timeline",
            title="Price per court-hour over time",
            unit="INR per court-hour",
            numerator="the modal slot price seen in a poll, normalized to an hour",
            denominator="one court-hour",
            denominator_description=(
                "Always per court-hour, never per slot: Play Padel's 1000 per "
                "30-minute slot is 2000 per court-hour, the most expensive of the "
                "three, while its per-slot price looks the cheapest."
            ),
            caveats=(_UNCERTAINTY_CAVEAT,),
        ),
        MetricDefinition(
            name="pricing_by_hour",
            title="Price by hour of day",
            unit="INR per court-hour",
            numerator="the modal slot price in that hour, normalized to an hour",
            denominator="one court-hour",
            denominator_description=(
                "One cell per court per wall-clock hour, priced per court-hour. "
                "has_any_variation answers the question directly: when it is "
                "false, no venue prices peak hours differently."
            ),
        ),
        MetricDefinition(
            name="market_share",
            title="Demand and supply share",
            unit="ratio",
            numerator="this venue's booked court-hours (demand) or listed court-hours (supply)",
            denominator="all tracked venues' booked (demand) or listed (supply) court-hours",
            denominator_description=(
                "Share of the three tracked Hudle listings only -- not of padel "
                "in Jaipur. Demand share and supply share have different "
                "denominators and are reported side by side on purpose."
            ),
            caveats=(_NORMALIZATION_CAVEAT, _BLOCKED_CAVEAT),
        ),
        MetricDefinition(
            name="revenue_proxy",
            title="Revenue proxy",
            unit="INR per ISO week",
            numerator="booked court-hours x the observed price per court-hour",
            denominator="one ISO week per venue",
            denominator_description=(
                "A proxy, not revenue: it counts observed Hudle bookings at list "
                "price, with no offline sales, discounts, refunds or no-shows. "
                "Blocked court-hours are priced alongside as what was withdrawn."
            ),
            caveats=(_NORMALIZATION_CAVEAT, _BLOCKED_CAVEAT),
        ),
        MetricDefinition(
            name="cancellations",
            title="Cancellation rate",
            unit="ratio per venue-week",
            numerator="observed BOOKED -> OPEN transitions",
            denominator="observed OPEN -> BOOKED transitions in the same venue-week",
            denominator_description=(
                "Slot-events, not customers: one customer booking two adjacent "
                "slots is two bookings. Left-censored slots have no observable "
                "booking event and are counted separately."
            ),
            caveats=(_UNCERTAINTY_CAVEAT,),
        ),
        MetricDefinition(
            name="sellout",
            title="Time to sell out",
            unit="hours from first sighting to first booking",
            numerator="hours between a slot first being observed and first being booked",
            denominator="peak slots booked after a long enough observation window (n)",
            denominator_description=(
                "Only slots watched for at least min_observation_hours before "
                "they sold qualify; a slot we saw twice cannot tell us how long "
                "it took to sell. Every exclusion is counted in the response."
            ),
            caveats=(_CENSORED_CAVEAT, _UNCERTAINTY_CAVEAT),
        ),
        MetricDefinition(
            name="first_slot",
            title="First slot to go each day",
            unit="one slot per business date",
            numerator="the earliest observed booking on that business date",
            denominator="one business date",
            denominator_description=(
                "The winner is the earliest first_booked_at among slots whose "
                "booking we actually watched happen; left-censored slots cannot "
                "win and are counted in excluded_censored."
            ),
            caveats=(_CENSORED_CAVEAT, _BUSINESS_DATE_CAVEAT),
        ),
        MetricDefinition(
            name="blocked_events",
            title="Blocked inventory events",
            unit="court-hours withdrawn",
            numerator="contiguous blocked court-minutes withdrawn in one venue action",
            denominator="one blocking event (a run of adjacent slots blocked in the same poll)",
            denominator_description=(
                "Blocked slots are not sold and not sellable. They are reported "
                "as their own events so a withdrawn evening is never averaged "
                "into occupancy as absent demand."
            ),
            caveats=(_UNCERTAINTY_CAVEAT,),
        ),
        MetricDefinition(
            name="weekday_weekend",
            title="Weekday versus weekend demand",
            unit="court-hours per day",
            numerator="booked court-minutes in the segment",
            denominator=(
                "business dates in the segment (per-day figures) and its "
                "sellable court-minutes (ratios)"
            ),
            denominator_description=(
                "Per-day figures divide by the number of distinct business dates "
                "observed in each segment, so an incomplete weekend does not read "
                "as a quiet one."
            ),
            caveats=(_NORMALIZATION_CAVEAT, _BUSINESS_DATE_CAVEAT),
        ),
        MetricDefinition(
            name="coverage",
            title="Collector coverage",
            unit="ratio of polls received",
            numerator="snapshots actually received on that UTC date",
            denominator="snapshots expected per day at the configured cadence",
            denominator_description=(
                "Counted on the UTC observation date, not on business_date: this "
                "measures the collector, not the courts. Gaps are returned as "
                "explicit intervals so a chart draws a hole instead of a line."
            ),
            caveats=(
                "A partial first or last day reads as low coverage by design; "
                "prorating would let a collector that ran twice report 100%.",
            ),
        ),
    )
}


class DateRangeOut(BaseModel):
    """The business-date span a metric was computed over."""

    start: dt.date | None
    end: dt.date | None
    business_days: int
    label: str

    @classmethod
    def from_range(cls, value: DateRange) -> DateRangeOut:
        return cls(
            start=value.start,
            end=value.end,
            business_days=value.business_days,
            label=value.label(),
        )


class MetricSpecOut(BaseModel):
    """One raw :class:`~tracker.analytics.pricing.MetricSpec`, as declared."""

    name: str
    title: str
    unit: str
    denominator: str
    date_range: DateRangeOut
    caveats: list[str]

    @classmethod
    def from_spec(cls, spec: MetricSpec) -> MetricSpecOut:
        return cls(
            name=spec.name,
            title=spec.title,
            unit=spec.unit,
            denominator=spec.denominator,
            date_range=DateRangeOut.from_range(spec.date_range),
            caveats=list(spec.caveats),
        )


class MetricMeta(BaseModel):
    """What the numbers in this response mean. Render it onto the chart."""

    name: str
    title: str
    unit: str
    numerator: str
    denominator: str
    denominator_description: str
    date_range: DateRangeOut
    caveats: list[str]

    @classmethod
    def build(
        cls,
        name: str,
        date_range: DateRange,
        *,
        specs: Sequence[MetricSpec] = (),
    ) -> MetricMeta:
        """Merge the static definition with whatever the analytics layer declared."""
        definition = METRIC_DEFINITIONS[name]
        caveats: list[str] = list(definition.caveats)
        for spec in specs:
            for caveat in spec.caveats:
                if caveat not in caveats:
                    caveats.append(caveat)
        return cls(
            name=definition.name,
            title=definition.title,
            unit=definition.unit,
            numerator=definition.numerator,
            denominator=definition.denominator,
            denominator_description=definition.denominator_description,
            date_range=DateRangeOut.from_range(date_range),
            caveats=caveats,
        )


class FiltersOut(BaseModel):
    """The filters actually applied, including the ones defaulted for you."""

    start: dt.date | None
    end: dt.date | None
    sport: str
    venue: str | None

    @classmethod
    def from_filters(cls, filters: Filters) -> FiltersOut:
        return cls(
            start=filters.start,
            end=filters.end,
            sport=filters.sport_label,
            venue=filters.venue_uuid,
        )


class Readiness(BaseModel):
    """Whether a metric has enough history behind it to be worth reading.

    A chart drawn from one day of data looks identical to a broken one, and a
    ratio over three bookings looks identical to a ratio over three hundred.
    Every response therefore states what it has, what it needs, and how many
    more days of collection close the gap, so the dashboard can decline to
    draw rather than draw something that cannot be trusted.
    """

    ready: bool
    snapshots: int
    min_snapshots: int
    elapsed_days: int
    min_elapsed_days: int
    #: Whole days of collection still needed; 0 when ready.
    eta_days: int
    note: str


#: Per metric: (min_snapshots, min_elapsed_days, why). Elapsed days are business
#: dates that have fully passed, because a date's occupancy is only settled once
#: its last slot has. Thresholds are deliberately conservative: a metric that
#: unlocks a day late costs nothing, one that unlocks a week early misleads.
_READINESS: Mapping[str, tuple[int, int, str]] = {
    "venues": (0, 0, "Static configuration; always available."),
    "coverage": (1, 0, "One poll is enough to show what the collector caught."),
    "blocked_events": (2, 0, "A withdrawal is a change between two polls."),
    "pricing_timeline": (2, 0, "A price change is a change between two polls."),
    "pricing_by_hour": (1, 0, "The current price grid is visible from one poll."),
    "occupancy_daily": (
        2,
        1,
        "A day's occupancy is settled only after its last slot has elapsed; "
        "the forward window is mostly unbooked and would read as near-zero demand.",
    ),
    "first_slot": (2, 3, "Which hour books first needs several settled days to mean anything."),
    "leadtime": (
        2,
        3,
        "Lead time is inferred from bookings seen to happen between polls, and needs "
        "a few days of evening traffic before a median is more than two points.",
    ),
    "cancellations": (2, 3, "A cancellation rate over a day or two is noise."),
    "occupancy_heatmap": (
        2,
        7,
        "Hour-by-weekday needs at least one settled instance of every weekday, or "
        "six of the seven columns are empty.",
    ),
    "sellout": (2, 7, "Time-to-sellout for evening slots needs a week of evenings."),
    "market_share": (2, 7, "A share of a few days' bookings swings with one group booking."),
    "revenue_proxy": (2, 7, "Weekly revenue needs a settled week."),
    "weekday_weekend": (
        2,
        14,
        "Weekday against weekend needs two of each, or one busy Saturday decides it.",
    ),
}


def readiness_for(name: str, *, snapshots: int, elapsed_days: int) -> Readiness:
    """Readiness of one metric given how much has been collected so far."""
    min_snapshots, min_days, note = _READINESS[name]
    return Readiness(
        ready=snapshots >= min_snapshots and elapsed_days >= min_days,
        snapshots=snapshots,
        min_snapshots=min_snapshots,
        elapsed_days=elapsed_days,
        min_elapsed_days=min_days,
        eta_days=max(0, min_days - elapsed_days),
        note=note,
    )


class MetricEnvelope(BaseModel):
    """Fields every metric response carries, whether or not it has rows."""

    metric: MetricMeta
    analytics_specs: list[MetricSpecOut]
    filters: FiltersOut
    empty: bool
    reason: str | None
    readiness: Readiness
    generated_at: dt.datetime


def envelope(
    name: str,
    *,
    date_range: DateRange,
    filters: Filters,
    now: dt.datetime,
    empty: bool,
    reason: str | None,
    readiness: Readiness,
    specs: Sequence[MetricSpec] = (),
) -> dict[str, Any]:
    """The envelope fields, ready to splat into a response model."""
    return {
        "readiness": readiness,
        "metric": MetricMeta.build(name, date_range, specs=specs),
        "analytics_specs": [MetricSpecOut.from_spec(spec) for spec in specs],
        "filters": FiltersOut.from_filters(filters),
        "empty": empty,
        "reason": reason,
        "generated_at": now,
    }


def venue_names(config: Config, dims: Iterable[VenueDim] = ()) -> dict[str, str]:
    """Display labels by venue uuid, config first and storage as a fallback.

    Matching is by uuid only. Play Padel has already been renamed on Hudle
    once, so a name is a label to print, never an identity.
    """
    names = {venue.uuid: venue.short_name for venue in config.venues}
    for dim in dims:
        names.setdefault(dim.venue_uuid, dim.short_name or dim.name)
    return names


def facility_names(config: Config, dims: Iterable[FacilityDim] = ()) -> dict[str, str]:
    """Display labels by facility uuid, config first and storage as a fallback."""
    names = {
        facility.uuid: f"{venue.short_name} / {facility.name}"
        for venue in config.venues
        for facility in venue.facilities
    }
    for dim in dims:
        names.setdefault(dim.facility_uuid, dim.name)
    return names


def _label(names: Mapping[str, str], uuid: str | None) -> str | None:
    if uuid is None:
        return None
    return names.get(uuid, uuid)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


class SnapshotOut(BaseModel):
    snapshot_id: int
    poll_key: str
    observed_at: dt.datetime
    ok: bool
    error: str | None
    duration_ms: int | None
    horizon_days: int

    @classmethod
    def from_record(cls, record: SnapshotRecord) -> SnapshotOut:
        return cls(
            snapshot_id=record.snapshot_id,
            poll_key=record.poll_key,
            observed_at=record.observed_at,
            ok=record.ok,
            error=record.error,
            duration_ms=record.duration_ms,
            horizon_days=record.horizon_days,
        )


class CircuitOut(BaseModel):
    """The collector's breaker, reconstructed from what it wrote down.

    The web process does not share memory with the collector, so this is not
    the live breaker object: it is the trailing run of failed snapshots in the
    database, compared against ``poll.max_consecutive_failures``. It says what
    the breaker *would* be, and ``source`` says so.
    """

    state: str
    consecutive_failures: int
    max_consecutive_failures: int
    source: str


class HealthResponse(BaseModel):
    ok: bool
    status: str
    reason: str | None
    generated_at: dt.datetime
    cadence_minutes: int
    last_snapshot: SnapshotOut | None
    staleness_minutes: float | None
    stale: bool
    stale_after_minutes: int
    expected_next_at: dt.datetime | None
    snapshots_last_24h: int
    failed_snapshots_last_24h: int
    circuit: CircuitOut


# --------------------------------------------------------------------------
# Venues
# --------------------------------------------------------------------------


class VenueDataQualityOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    flags: list[str]
    note: str
    booked_court_hours: float
    listed_court_hours: float
    blocked_court_hours: float
    blocked_share: float | None

    @classmethod
    def from_quality(
        cls, quality: VenueDataQuality, names: Mapping[str, str]
    ) -> VenueDataQualityOut:
        return cls(
            venue_uuid=quality.venue_uuid,
            venue_name=_label(names, quality.venue_uuid),
            flags=[str(flag) for flag in quality.flags],
            note=quality.note,
            booked_court_hours=quality.booked_court_hours,
            listed_court_hours=quality.listed_court_hours,
            blocked_court_hours=quality.blocked_court_hours,
            blocked_share=quality.blocked_share,
        )


class CourtOut(BaseModel):
    facility_uuid: str
    name: str
    kind: str
    sport: str
    grid_minutes: int | None
    price_per_court_hour: int | None
    price_per_slot: float | None
    active: bool
    first_seen: dt.datetime | None
    last_seen: dt.datetime | None


class VenueOut(BaseModel):
    venue_uuid: str
    name: str
    short_name: str
    slug: str
    numeric_id: str
    tz: str
    active: bool
    in_config: bool
    first_seen: dt.datetime | None
    last_seen: dt.datetime | None
    courts: list[CourtOut]
    data_quality: VenueDataQualityOut | None


class VenuesResponse(MetricEnvelope):
    venues: list[VenueOut]


# --------------------------------------------------------------------------
# Occupancy
# --------------------------------------------------------------------------


class OccupancyTotalsOut(BaseModel):
    """Both denominators, both numerators, always."""

    slots: int
    booked_minutes: int
    open_minutes: int
    blocked_minutes: int
    sellable_minutes: int
    total_minutes: int
    booked_court_hours: float
    open_court_hours: float
    blocked_court_hours: float
    sellable_court_hours: float
    total_court_hours: float
    occupancy_strict: float | None
    occupancy_gross: float | None
    blocked_share: float | None

    @classmethod
    def _totals(cls, totals: OccupancyTotals) -> dict[str, Any]:
        return {
            "slots": totals.slots,
            "booked_minutes": totals.booked_minutes,
            "open_minutes": totals.open_minutes,
            "blocked_minutes": totals.blocked_minutes,
            "sellable_minutes": totals.sellable_minutes,
            "total_minutes": totals.total_minutes,
            "booked_court_hours": totals.booked_court_hours,
            "open_court_hours": totals.open_court_hours,
            "blocked_court_hours": totals.blocked_court_hours,
            "sellable_court_hours": totals.sellable_court_hours,
            "total_court_hours": totals.total_court_hours,
            "occupancy_strict": totals.occupancy_strict,
            "occupancy_gross": totals.occupancy_gross,
            "blocked_share": totals.blocked_share,
        }


class VenueDayOut(OccupancyTotalsOut):
    venue_uuid: str
    venue_name: str | None
    business_date: dt.date

    @classmethod
    def from_row(cls, row: VenueDayOccupancy, names: Mapping[str, str]) -> VenueDayOut:
        return cls(
            venue_uuid=row.venue_uuid,
            venue_name=_label(names, row.venue_uuid),
            business_date=row.business_date,
            **cls._totals(row),
        )


class OccupancyDailyResponse(MetricEnvelope):
    rows: list[VenueDayOut]


class HeatmapCellOut(OccupancyTotalsOut):
    day_of_week: int
    day_name: str
    hour: int
    business_dates: int
    sparse: bool

    @classmethod
    def from_cell(cls, cell: HeatmapCell) -> HeatmapCellOut:
        return cls(
            day_of_week=cell.day_of_week,
            day_name=cell.day_name,
            hour=cell.hour,
            business_dates=cell.business_dates,
            sparse=cell.sparse,
            **cls._totals(cell),
        )


class HeatmapOut(BaseModel):
    venue_uuid: str | None
    venue_name: str | None
    sparse_min_minutes: int
    hours: list[int]
    days_of_week: list[int]
    cells: list[HeatmapCellOut]

    @classmethod
    def from_heatmap(cls, heatmap: Heatmap, names: Mapping[str, str]) -> HeatmapOut:
        return cls(
            venue_uuid=heatmap.venue_uuid,
            venue_name=_label(names, heatmap.venue_uuid),
            sparse_min_minutes=heatmap.sparse_min_minutes,
            hours=list(heatmap.hours),
            days_of_week=list(heatmap.days_of_week),
            cells=[HeatmapCellOut.from_cell(cell) for cell in heatmap.cells],
        )


class OccupancyHeatmapResponse(MetricEnvelope):
    combined: HeatmapOut
    by_venue: list[HeatmapOut]


class DemandSegmentOut(OccupancyTotalsOut):
    label: str
    business_dates: int
    booked_court_hours_per_day: float | None
    sellable_court_hours_per_day: float | None
    blocked_court_hours_per_day: float | None

    @classmethod
    def from_segment(cls, segment: DemandSegment) -> DemandSegmentOut:
        return cls(
            label=segment.label,
            business_dates=segment.business_dates,
            booked_court_hours_per_day=segment.booked_court_hours_per_day,
            sellable_court_hours_per_day=segment.sellable_court_hours_per_day,
            blocked_court_hours_per_day=segment.blocked_court_hours_per_day,
            **cls._totals(segment),
        )


class WeekdayWeekendResponse(MetricEnvelope):
    weekday: DemandSegmentOut
    weekend: DemandSegmentOut
    weekend_uplift: float | None

    @classmethod
    def from_split(cls, split: WeekdayWeekendSplit, **fields: Any) -> WeekdayWeekendResponse:
        return cls(
            weekday=DemandSegmentOut.from_segment(split.weekday),
            weekend=DemandSegmentOut.from_segment(split.weekend),
            weekend_uplift=split.weekend_uplift,
            **fields,
        )


# --------------------------------------------------------------------------
# Lead time and sellout
# --------------------------------------------------------------------------


class LeadTimeStatsOut(BaseModel):
    label: str
    n: int
    excluded_censored: int
    post_start: int
    median_hours: float | None
    p90_hours: float | None
    min_hours: float | None
    max_hours: float | None

    @classmethod
    def from_stats(cls, stats: LeadTimeStats) -> LeadTimeStatsOut:
        return cls(
            label=stats.label,
            n=stats.n,
            excluded_censored=stats.excluded_censored,
            post_start=stats.post_start,
            median_hours=stats.median_hours,
            p90_hours=stats.p90_hours,
            min_hours=stats.min_hours,
            max_hours=stats.max_hours,
        )


class DayOfWeekStatsOut(BaseModel):
    day_of_week: int
    day_name: str
    stats: LeadTimeStatsOut


class PeakStatsOut(BaseModel):
    bucket: str
    stats: LeadTimeStatsOut


class LeadTimeSampleOut(BaseModel):
    """One slot's lead time: the raw distribution the percentiles came from."""

    slot_uuid: str
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    business_date: dt.date
    slot_start_hour: int
    slot_start_utc: dt.datetime
    first_booked_at: dt.datetime | None
    lead_time_hours: float | None
    uncertainty_minutes: int | None
    censored_left: bool
    post_start: bool
    rebooked: bool
    cancelled: bool

    @classmethod
    def from_booking(cls, booking: SlotBooking, names: Mapping[str, str]) -> LeadTimeSampleOut:
        return cls(
            slot_uuid=booking.slot_uuid,
            venue_uuid=booking.venue_uuid,
            venue_name=_label(names, booking.venue_uuid),
            facility_uuid=booking.facility_uuid,
            business_date=booking.business_date,
            slot_start_hour=booking.slot_start_hour,
            slot_start_utc=booking.slot_start_utc,
            first_booked_at=booking.first_booked_at,
            lead_time_hours=booking.lead_time_hours,
            uncertainty_minutes=booking.uncertainty_minutes,
            censored_left=booking.censored_left,
            post_start=booking.post_start,
            rebooked=booking.rebooked,
            cancelled=booking.cancelled,
        )


class LeadTimeResponse(MetricEnvelope):
    peak_hours: list[int]
    overall: LeadTimeStatsOut
    by_day_of_week: list[DayOfWeekStatsOut]
    by_peak: list[PeakStatsOut]
    distribution: list[LeadTimeSampleOut]

    @classmethod
    def from_distribution(
        cls,
        distribution: LeadTimeDistribution,
        bookings: Sequence[SlotBooking],
        names: Mapping[str, str],
        **fields: Any,
    ) -> LeadTimeResponse:
        return cls(
            peak_hours=list(distribution.peak_hours),
            overall=LeadTimeStatsOut.from_stats(distribution.overall),
            by_day_of_week=[
                DayOfWeekStatsOut(
                    day_of_week=day,
                    day_name=DAY_NAMES[day],
                    stats=LeadTimeStatsOut.from_stats(stats),
                )
                for day, stats in sorted(distribution.by_day_of_week.items())
            ],
            by_peak=[
                PeakStatsOut(bucket=str(bucket), stats=LeadTimeStatsOut.from_stats(stats))
                for bucket, stats in distribution.by_peak.items()
            ],
            distribution=[LeadTimeSampleOut.from_booking(b, names) for b in bookings],
            **fields,
        )


class SelloutRecordOut(BaseModel):
    slot_uuid: str
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    business_date: dt.date
    slot_start_hour: int
    first_observed_at: dt.datetime
    first_booked_at: dt.datetime
    hours_to_sellout: float
    observation_window_hours: float
    uncertainty_minutes: int | None

    @classmethod
    def from_record(cls, record: SelloutRecord, names: Mapping[str, str]) -> SelloutRecordOut:
        return cls(
            slot_uuid=record.slot_uuid,
            venue_uuid=record.venue_uuid,
            venue_name=_label(names, record.venue_uuid),
            facility_uuid=record.facility_uuid,
            business_date=record.business_date,
            slot_start_hour=record.slot_start_hour,
            first_observed_at=record.first_observed_at,
            first_booked_at=record.first_booked_at,
            hours_to_sellout=record.hours_to_sellout,
            observation_window_hours=record.observation_window_hours,
            uncertainty_minutes=record.uncertainty_minutes,
        )


class SelloutResponse(MetricEnvelope):
    peak_hours: list[int]
    min_observation_hours: float
    n: int
    median_hours: float | None
    p90_hours: float | None
    excluded_censored: int
    excluded_short_window: int
    excluded_off_peak: int
    records: list[SelloutRecordOut]

    @classmethod
    def from_report(
        cls, report: SelloutReport, names: Mapping[str, str], **fields: Any
    ) -> SelloutResponse:
        return cls(
            peak_hours=list(report.peak_hours),
            min_observation_hours=report.min_observation_hours,
            n=report.n,
            median_hours=report.median_hours,
            p90_hours=report.p90_hours,
            excluded_censored=report.excluded_censored,
            excluded_short_window=report.excluded_short_window,
            excluded_off_peak=report.excluded_off_peak,
            records=[SelloutRecordOut.from_record(r, names) for r in report.records],
            **fields,
        )


class FirstSlotOut(BaseModel):
    business_date: dt.date
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    slot_uuid: str
    slot_start_hour: int
    first_booked_at: dt.datetime
    lead_time_hours: float | None
    uncertainty_minutes: int | None
    excluded_censored: int

    @classmethod
    def from_row(cls, row: FirstSlotToGo, names: Mapping[str, str]) -> FirstSlotOut:
        return cls(
            business_date=row.business_date,
            venue_uuid=row.venue_uuid,
            venue_name=_label(names, row.venue_uuid),
            facility_uuid=row.facility_uuid,
            slot_uuid=row.slot_uuid,
            slot_start_hour=row.slot_start_hour,
            first_booked_at=row.first_booked_at,
            lead_time_hours=row.lead_time_hours,
            uncertainty_minutes=row.uncertainty_minutes,
            excluded_censored=row.excluded_censored,
        )


class FirstSlotResponse(MetricEnvelope):
    rows: list[FirstSlotOut]


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


class CancellationRateOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    iso_year: int
    iso_week: int
    week_start: dt.date
    bookings: int
    cancellations: int
    rate: float | None
    booked_court_hours: float
    cancelled_court_hours: float
    booked_court_minutes: int
    cancelled_court_minutes: int
    booked_slots: int
    censored_slots: int

    @classmethod
    def from_row(cls, row: CancellationRate, names: Mapping[str, str]) -> CancellationRateOut:
        return cls(
            venue_uuid=row.venue_uuid,
            venue_name=_label(names, row.venue_uuid),
            iso_year=row.iso_year,
            iso_week=row.iso_week,
            week_start=row.week_start,
            bookings=row.bookings,
            cancellations=row.cancellations,
            rate=row.rate,
            booked_court_hours=row.booked_court_hours,
            cancelled_court_hours=row.cancelled_court_hours,
            booked_court_minutes=row.booked_court_minutes,
            cancelled_court_minutes=row.cancelled_court_minutes,
            booked_slots=row.booked_slots,
            censored_slots=row.censored_slots,
        )


class CancellationsResponse(MetricEnvelope):
    rows: list[CancellationRateOut]


class BlockedEventOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    business_date: dt.date
    slot_count: int
    court_minutes: int
    court_hours: float
    start_utc: dt.datetime
    end_utc: dt.datetime
    first_seen_at: dt.datetime
    prev_seen_at: dt.datetime | None
    uncertainty_minutes: int | None
    censored_left: bool
    slot_uuids: list[str]

    @classmethod
    def from_event(cls, event: BlockedInventoryEvent, names: Mapping[str, str]) -> BlockedEventOut:
        return cls(
            venue_uuid=event.venue_uuid,
            venue_name=_label(names, event.venue_uuid),
            facility_uuid=event.facility_uuid,
            business_date=event.business_date,
            slot_count=event.slot_count,
            court_minutes=event.court_minutes,
            court_hours=event.court_hours,
            start_utc=event.start_utc,
            end_utc=event.end_utc,
            first_seen_at=event.first_seen_at,
            prev_seen_at=event.prev_seen_at,
            uncertainty_minutes=event.uncertainty_minutes,
            censored_left=event.censored_left,
            slot_uuids=list(event.slot_uuids),
        )


class BlockedEventsResponse(MetricEnvelope):
    rows: list[BlockedEventOut]


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


class PricePointOut(BaseModel):
    snapshot_id: int
    observed_at: dt.datetime
    price_per_court_hour: float
    distinct_prices_per_court_hour: list[float]
    slots_priced: int
    is_uniform: bool

    @classmethod
    def from_point(cls, point: PricePoint) -> PricePointOut:
        return cls(
            snapshot_id=point.snapshot_id,
            observed_at=point.observed_at,
            price_per_court_hour=point.price_per_court_hour,
            distinct_prices_per_court_hour=list(point.distinct_prices_per_court_hour),
            slots_priced=point.slots_priced,
            is_uniform=point.is_uniform,
        )


class PriceChangeOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    from_price_per_court_hour: float
    to_price_per_court_hour: float
    delta_per_court_hour: float
    direction: str
    prev_seen_at: dt.datetime
    first_seen_at: dt.datetime
    uncertainty_minutes: int
    currency: str

    @classmethod
    def from_change(cls, change: PriceChange, names: Mapping[str, str]) -> PriceChangeOut:
        return cls(
            venue_uuid=change.venue_uuid,
            venue_name=_label(names, change.venue_uuid),
            facility_uuid=change.facility_uuid,
            from_price_per_court_hour=change.from_price_per_court_hour,
            to_price_per_court_hour=change.to_price_per_court_hour,
            delta_per_court_hour=change.delta_per_court_hour,
            direction=change.direction,
            prev_seen_at=change.prev_seen_at,
            first_seen_at=change.first_seen_at,
            uncertainty_minutes=change.uncertainty_minutes,
            currency=change.currency,
        )


class PriceTimelineOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    facility_name: str | None
    is_flat: bool
    current_price_per_court_hour: float | None
    points: list[PricePointOut]
    changes: list[PriceChangeOut]

    @classmethod
    def from_timeline(
        cls,
        timeline: PriceTimeline,
        venues: Mapping[str, str],
        facilities: Mapping[str, str],
    ) -> PriceTimelineOut:
        return cls(
            venue_uuid=timeline.venue_uuid,
            venue_name=_label(venues, timeline.venue_uuid),
            facility_uuid=timeline.facility_uuid,
            facility_name=_label(facilities, timeline.facility_uuid),
            is_flat=timeline.is_flat,
            current_price_per_court_hour=timeline.current_price_per_court_hour,
            points=[PricePointOut.from_point(p) for p in timeline.points],
            changes=[PriceChangeOut.from_change(c, venues) for c in timeline.changes],
        )


class PricingTimelineResponse(MetricEnvelope):
    currency: str
    has_any_change: bool
    timelines: list[PriceTimelineOut]
    changes: list[PriceChangeOut]

    @classmethod
    def from_report(
        cls,
        report: PriceTimelineReport,
        venues: Mapping[str, str],
        facilities: Mapping[str, str],
        currency: str,
        **fields: Any,
    ) -> PricingTimelineResponse:
        return cls(
            currency=currency,
            has_any_change=report.has_any_change,
            timelines=[
                PriceTimelineOut.from_timeline(t, venues, facilities) for t in report.timelines
            ],
            changes=[PriceChangeOut.from_change(c, venues) for c in report.changes],
            **fields,
        )


class PriceByHourCellOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    hour: int
    price_per_court_hour: float
    distinct_prices_per_court_hour: list[float]
    slots_priced: int
    is_uniform: bool
    currency: str

    @classmethod
    def from_cell(cls, cell: PriceByHourCell, names: Mapping[str, str]) -> PriceByHourCellOut:
        return cls(
            venue_uuid=cell.venue_uuid,
            venue_name=_label(names, cell.venue_uuid),
            facility_uuid=cell.facility_uuid,
            hour=cell.hour,
            price_per_court_hour=cell.price_per_court_hour,
            distinct_prices_per_court_hour=list(cell.distinct_prices_per_court_hour),
            slots_priced=cell.slots_priced,
            is_uniform=cell.is_uniform,
            currency=cell.currency,
        )


class CourtPriceProfileOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    facility_uuid: str
    facility_name: str | None
    is_flat: bool
    flat_price_per_court_hour: float | None
    distinct_prices_per_court_hour: list[float]
    hours_covered: list[int]
    currency: str

    @classmethod
    def from_profile(
        cls,
        profile: CourtPriceProfile,
        venues: Mapping[str, str],
        facilities: Mapping[str, str],
    ) -> CourtPriceProfileOut:
        return cls(
            venue_uuid=profile.venue_uuid,
            venue_name=_label(venues, profile.venue_uuid),
            facility_uuid=profile.facility_uuid,
            facility_name=_label(facilities, profile.facility_uuid),
            is_flat=profile.is_flat,
            flat_price_per_court_hour=profile.flat_price_per_court_hour,
            distinct_prices_per_court_hour=list(profile.distinct_prices_per_court_hour),
            hours_covered=list(profile.hours_covered),
            currency=profile.currency,
        )


class VenuePriceProfileOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    is_flat: bool
    flat_price_per_court_hour: float | None
    distinct_prices_per_court_hour: list[float]
    courts: list[str]
    currency: str

    @classmethod
    def from_profile(
        cls, profile: VenuePriceProfile, names: Mapping[str, str]
    ) -> VenuePriceProfileOut:
        return cls(
            venue_uuid=profile.venue_uuid,
            venue_name=_label(names, profile.venue_uuid),
            is_flat=profile.is_flat,
            flat_price_per_court_hour=profile.flat_price_per_court_hour,
            distinct_prices_per_court_hour=list(profile.distinct_prices_per_court_hour),
            courts=list(profile.courts),
            currency=profile.currency,
        )


class PricingByHourResponse(MetricEnvelope):
    has_any_variation: bool
    summary: str
    hours: list[int]
    cells: list[PriceByHourCellOut]
    courts: list[CourtPriceProfileOut]
    venues: list[VenuePriceProfileOut]

    @classmethod
    def from_table(
        cls,
        table: PriceByHourTable,
        venues: Mapping[str, str],
        facilities: Mapping[str, str],
        **fields: Any,
    ) -> PricingByHourResponse:
        return cls(
            has_any_variation=table.has_any_variation,
            summary=table.summary,
            hours=list(table.hours),
            cells=[PriceByHourCellOut.from_cell(c, venues) for c in table.cells],
            courts=[CourtPriceProfileOut.from_profile(c, venues, facilities) for c in table.courts],
            venues=[VenuePriceProfileOut.from_profile(v, venues) for v in table.venues],
            **fields,
        )


# --------------------------------------------------------------------------
# Market
# --------------------------------------------------------------------------


class MarketShareRowOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    week_start: dt.date
    week_label: str
    booked_court_hours: float
    demand_share: float | None
    listed_court_hours: float
    sellable_court_hours: float
    supply_share: float | None
    blocked_court_hours: float

    @classmethod
    def from_row(cls, row: MarketShareRow, names: Mapping[str, str]) -> MarketShareRowOut:
        return cls(
            venue_uuid=row.venue_uuid,
            venue_name=_label(names, row.venue_uuid),
            week_start=row.week_start,
            week_label=row.week_label,
            booked_court_hours=row.booked_court_hours,
            demand_share=row.demand_share,
            listed_court_hours=row.listed_court_hours,
            sellable_court_hours=row.sellable_court_hours,
            supply_share=row.supply_share,
            blocked_court_hours=row.blocked_court_hours,
        )


class MarketShareResponse(MetricEnvelope):
    rows: list[MarketShareRowOut]
    venues: list[VenueDataQualityOut]
    caveat_summary: str

    @classmethod
    def from_report(
        cls, report: MarketShareReport, names: Mapping[str, str], **fields: Any
    ) -> MarketShareResponse:
        return cls(
            rows=[MarketShareRowOut.from_row(r, names) for r in report.rows],
            venues=[VenueDataQualityOut.from_quality(v, names) for v in report.venues],
            caveat_summary=report.caveat_summary,
            **fields,
        )


class RevenueProxyRowOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    week_start: dt.date
    week_label: str
    booked_court_hours: float
    booked_revenue_proxy: float
    blocked_court_hours: float
    blocked_revenue_if_sold: float
    upper_bound_revenue_proxy: float
    open_court_hours: float
    listed_court_hours: float
    booked_slots: int
    blocked_slots: int
    unpriced_slots: int
    currency: str

    @classmethod
    def from_row(cls, row: RevenueProxyRow, names: Mapping[str, str]) -> RevenueProxyRowOut:
        return cls(
            venue_uuid=row.venue_uuid,
            venue_name=_label(names, row.venue_uuid),
            week_start=row.week_start,
            week_label=row.week_label,
            booked_court_hours=row.booked_court_hours,
            booked_revenue_proxy=row.booked_revenue_proxy,
            blocked_court_hours=row.blocked_court_hours,
            blocked_revenue_if_sold=row.blocked_revenue_if_sold,
            upper_bound_revenue_proxy=row.upper_bound_revenue_proxy,
            open_court_hours=row.open_court_hours,
            listed_court_hours=row.listed_court_hours,
            booked_slots=row.booked_slots,
            blocked_slots=row.blocked_slots,
            unpriced_slots=row.unpriced_slots,
            currency=row.currency,
        )


class VenueRevenueTotalOut(BaseModel):
    venue_uuid: str
    venue_name: str | None
    booked_court_hours: float
    booked_revenue_proxy: float
    blocked_court_hours: float
    blocked_revenue_if_sold: float
    listed_court_hours: float
    weeks: int
    currency: str

    @classmethod
    def from_total(cls, total: VenueRevenueTotal, names: Mapping[str, str]) -> VenueRevenueTotalOut:
        return cls(
            venue_uuid=total.venue_uuid,
            venue_name=_label(names, total.venue_uuid),
            booked_court_hours=total.booked_court_hours,
            booked_revenue_proxy=total.booked_revenue_proxy,
            blocked_court_hours=total.blocked_court_hours,
            blocked_revenue_if_sold=total.blocked_revenue_if_sold,
            listed_court_hours=total.listed_court_hours,
            weeks=total.weeks,
            currency=total.currency,
        )


class RevenueProxyResponse(MetricEnvelope):
    is_proxy: bool
    label: str
    rows: list[RevenueProxyRowOut]
    venue_totals: list[VenueRevenueTotalOut]

    @classmethod
    def from_report(
        cls, report: RevenueProxyReport, names: Mapping[str, str], **fields: Any
    ) -> RevenueProxyResponse:
        return cls(
            is_proxy=report.is_proxy,
            label=report.label,
            rows=[RevenueProxyRowOut.from_row(r, names) for r in report.rows],
            venue_totals=[VenueRevenueTotalOut.from_total(t, names) for t in report.venue_totals],
            **fields,
        )


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


class GapIntervalOut(BaseModel):
    """A hole in the record. The UI draws a break here, never a line."""

    facility_uuid: str | None
    facility_name: str | None
    start: dt.datetime
    end: dt.datetime
    minutes: int
    missed_polls: int

    @classmethod
    def from_gap(cls, gap: GapInterval, facilities: Mapping[str, str]) -> GapIntervalOut:
        return cls(
            facility_uuid=gap.facility_uuid,
            facility_name=_label(facilities, gap.facility_uuid),
            start=gap.start,
            end=gap.end,
            minutes=gap.minutes,
            missed_polls=gap.missed_polls,
        )


class FacilityDayCoverageOut(BaseModel):
    facility_uuid: str
    facility_name: str | None
    observed_date: dt.date
    snapshots_expected: int
    snapshots_received: int
    snapshots_missing: int
    coverage_ratio: float | None
    fetches_ok: int
    fetches_failed: int
    slot_rows: int
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    is_complete: bool

    @classmethod
    def from_day(
        cls, day: FacilityDayCoverage, facilities: Mapping[str, str]
    ) -> FacilityDayCoverageOut:
        return cls(
            facility_uuid=day.facility_uuid,
            facility_name=_label(facilities, day.facility_uuid),
            observed_date=day.observed_date,
            snapshots_expected=day.snapshots_expected,
            snapshots_received=day.snapshots_received,
            snapshots_missing=day.snapshots_missing,
            coverage_ratio=day.coverage_ratio,
            fetches_ok=day.fetches_ok,
            fetches_failed=day.fetches_failed,
            slot_rows=day.slot_rows,
            first_observed_at=day.first_observed_at,
            last_observed_at=day.last_observed_at,
            is_complete=day.is_complete,
        )


class FacilityGapsOut(BaseModel):
    facility_uuid: str
    facility_name: str | None
    gaps: list[GapIntervalOut]


class CoverageResponse(MetricEnvelope):
    cadence_minutes: int
    snapshots_expected_per_day: int
    has_gaps: bool
    missed_polls: int
    poll_gaps: list[GapIntervalOut]
    facility_gaps: list[FacilityGapsOut]
    days: list[FacilityDayCoverageOut]

    @classmethod
    def from_report(
        cls, report: CoverageReport, facilities: Mapping[str, str], **fields: Any
    ) -> CoverageResponse:
        return cls(
            cadence_minutes=report.cadence_minutes,
            snapshots_expected_per_day=report.snapshots_expected_per_day,
            has_gaps=report.has_gaps,
            missed_polls=report.missed_polls,
            poll_gaps=[GapIntervalOut.from_gap(g, facilities) for g in report.poll_gaps],
            facility_gaps=[
                FacilityGapsOut(
                    facility_uuid=facility_uuid,
                    facility_name=_label(facilities, facility_uuid),
                    gaps=[GapIntervalOut.from_gap(g, facilities) for g in gaps],
                )
                for facility_uuid, gaps in sorted(report.facility_gaps.items())
            ],
            days=[FacilityDayCoverageOut.from_day(d, facilities) for d in report.days],
            **fields,
        )


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


class CatalogResponse(BaseModel):
    """Every metric the dashboard can draw, with its denominator and caveats."""

    generated_at: dt.datetime
    date_range: DateRangeOut
    metrics: list[MetricMeta]
    analytics_specs: list[MetricSpecOut]
    reason: str | None
