"""Price analytics, normalized to court-hours.

This module is pure: no I/O, no database handle, no HTTP, and no call to
:func:`datetime.now`. Every time-dependent fact comes from the observations and
snapshots the caller supplies, so the same inputs always produce the same
numbers.

**Never compare per-slot prices.** It is the single easiest way to get this
dataset wrong. Play Padel charges 1000 per slot and Padel Fort charges 900, so
a per-slot ranking calls Play Padel the cheaper court. It is not: Play Padel
sells 30-minute slots at 2000 per court-hour while Padel Fort sells 30-minute
slots at 1800, and Padel Up sells 60-minute slots at 1800. Play Padel is the
*most* expensive padel court in the city and a per-slot chart inverts the
ranking completely. :func:`price_per_court_hour` is the only correct
comparison, and :func:`price_rank` prints both figures side by side so the
inversion is visible rather than merely avoided.

Every aggregate here first reduces the stream with
:func:`tracker.analytics.occupancy.settled_observations`, the project's one
definition of "the row for this slot". Every poll re-observes every slot in the
horizon, so summing straight off the stream inflates each figure roughly
48-fold; and defining a second reduction here -- "the newest poll" rather than
"the state it settled in" -- would let a price panel and an occupancy panel
disagree about which observation they are describing.

**The honest answer about hour-of-day pricing is "there isn't any".** All three
venues were verified flat on 2026-09-11: one price for every hour of every day,
across 2945 slots. :func:`price_by_hour_table` therefore returns an explicit
``is_flat`` flag per court and per venue plus a single ``has_any_variation``
flag, so a dashboard can say "no venue currently varies price by hour" instead
of rendering three identical rows and implying a finding that is not there.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from tracker.analytics.occupancy import settled_observations
from tracker.types import SlotObservation, SnapshotRecord, slot_start_hour

logger = logging.getLogger("analytics.pricing")

T = TypeVar("T")
NumberT = TypeVar("NumberT", int, float)

MINUTES_PER_HOUR = 60

#: Every price Hudle publishes for these venues is in Indian rupees. Carried on
#: each money-valued result so the web layer never guesses a currency symbol.
CURRENCY = "INR"

#: Prices are compared after rounding to this many decimal places. Hudle sends
#: ``"900.00"`` strings which become exact floats, but a 45-minute grid would
#: divide unevenly and float noise must not read as a price change.
PRICE_PRECISION = 2


# --------------------------------------------------------------------------
# Metric self-description
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateRange:
    """The span of business dates a metric was computed over.

    Every metric carries one. A chart that does not print its own date range
    invites the reader to assume the range they had in mind, and this dataset
    is forward-looking and gappy enough that the assumption is usually wrong.
    """

    start: dt.date | None
    end: dt.date | None
    business_days: int

    @property
    def is_empty(self) -> bool:
        return self.start is None or self.end is None

    def label(self) -> str:
        """A human-readable span, for rendering directly onto a chart."""
        if self.start is None or self.end is None:
            return "no data"
        if self.start == self.end:
            return f"{self.start.isoformat()} ({self.business_days} trading day)"
        return (
            f"{self.start.isoformat()} to {self.end.isoformat()} "
            f"({self.business_days} trading days)"
        )


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """What a derived number means, what it is divided by, and where it lies.

    The web layer renders ``denominator`` and ``date_range`` onto the chart
    itself rather than hardcoding a caption, and renders ``caveats`` wherever
    the number could be read as something it is not. A metric with a known
    honesty problem and no caveat here is a bug in this module, not in the
    dashboard.
    """

    name: str
    title: str
    unit: str
    denominator: str
    date_range: DateRange
    caveats: tuple[str, ...] = ()

    @property
    def has_caveats(self) -> bool:
        return bool(self.caveats)


def date_range_of(observations: Iterable[SlotObservation]) -> DateRange:
    """The business-date span covered by ``observations``.

    Business dates, never local dates: Play Padel's 00:30 Saturday slots are
    Friday-night sessions and belong to Friday's span.
    """
    dates = {observation.business_date for observation in observations}
    if not dates:
        return DateRange(start=None, end=None, business_days=0)
    return DateRange(start=min(dates), end=max(dates), business_days=len(dates))


# --------------------------------------------------------------------------
# The one correct price comparison
# --------------------------------------------------------------------------


def price_per_court_hour(observation: SlotObservation) -> float | None:
    """Normalize one slot's price to a per-court-hour rate.

    This is the only correct way to compare price across venues. A 30-minute
    slot at 1000 is 2000 per court-hour; a 60-minute slot at 1800 is 1800.
    Returns ``None`` when the slot carries no price, or a non-positive
    duration, rather than inventing a rate.
    """
    if observation.price is None or observation.duration_minutes <= 0:
        return None
    return round(
        observation.price * MINUTES_PER_HOUR / observation.duration_minutes, PRICE_PRECISION
    )


def price_per_slot(observation: SlotObservation) -> float | None:
    """The raw per-slot price, exposed only so a chart can show the inversion.

    Never rank venues on this. It exists to sit next to
    :func:`price_per_court_hour` in :func:`price_rank`, where the two columns
    together make the trap explicit.
    """
    return observation.price


@dataclass(frozen=True, slots=True)
class PriceRankRow:
    """One court's price, in both the misleading unit and the correct one."""

    venue_uuid: str
    facility_uuid: str
    price_per_court_hour: float
    price_per_slot: float
    grid_minutes: int
    slots_observed: int
    currency: str = CURRENCY


@dataclass(frozen=True, slots=True)
class PriceRank:
    """Courts ordered most to least expensive per court-hour.

    ``per_slot_order`` is the same set of courts ordered by their per-slot
    price. When the two orders disagree -- and today they do -- a per-slot
    chart is actively lying about who is expensive, and
    :attr:`slot_price_ranking_inverts` says so in one boolean.
    """

    rows: tuple[PriceRankRow, ...]
    per_slot_order: tuple[str, ...]
    metrics: tuple[MetricSpec, ...]

    @property
    def court_hour_order(self) -> tuple[str, ...]:
        return tuple(row.facility_uuid for row in self.rows)

    @property
    def slot_price_ranking_inverts(self) -> bool:
        """True when ranking on per-slot price reorders the courts."""
        return self.court_hour_order != self.per_slot_order

    @property
    def most_expensive(self) -> PriceRankRow | None:
        return self.rows[0] if self.rows else None

    @property
    def cheapest(self) -> PriceRankRow | None:
        return self.rows[-1] if self.rows else None


def price_rank(observations: Iterable[SlotObservation]) -> PriceRank:
    """Rank every observed court by price per court-hour, descending.

    Each court is summarized by its modal per-court-hour rate and the modal
    per-slot price that produced it. Courts that carry no price at all are
    omitted rather than ranked at zero.
    """
    materialized = settled_observations(observations)
    per_court: dict[tuple[str, str], list[SlotObservation]] = defaultdict(list)
    for observation in materialized:
        per_court[(observation.venue_uuid, observation.facility_uuid)].append(observation)

    rows: list[PriceRankRow] = []
    for (venue_uuid, facility_uuid), court_observations in per_court.items():
        rates = [
            rate
            for observation in court_observations
            if (rate := price_per_court_hour(observation)) is not None
        ]
        if not rates:
            logger.info(
                "price_rank_court_has_no_price",
                extra={"venue_uuid": venue_uuid, "facility_uuid": facility_uuid},
            )
            continue
        rate = _modal(rates)
        priced = [
            observation
            for observation in court_observations
            if price_per_court_hour(observation) == rate
        ]
        rows.append(
            PriceRankRow(
                venue_uuid=venue_uuid,
                facility_uuid=facility_uuid,
                price_per_court_hour=rate,
                price_per_slot=_modal([o.price for o in priced if o.price is not None]),
                grid_minutes=_modal([o.duration_minutes for o in priced]),
                slots_observed=len({o.slot_uuid for o in court_observations}),
            )
        )

    rows.sort(key=lambda row: (-row.price_per_court_hour, row.facility_uuid))
    per_slot_order = tuple(
        row.facility_uuid
        for row in sorted(rows, key=lambda row: (-row.price_per_slot, row.facility_uuid))
    )
    window = date_range_of(materialized)
    return PriceRank(
        rows=tuple(rows),
        per_slot_order=per_slot_order,
        metrics=(
            MetricSpec(
                name="price_rank",
                title="Price per court-hour",
                unit=f"{CURRENCY} per court-hour",
                denominator=(
                    "modal published slot price scaled to 60 minutes, per court; "
                    "not a transacted price"
                ),
                date_range=window,
                caveats=(
                    "Published rack rate only. Discounts, packages, memberships and "
                    "offline rates are invisible to this API.",
                    "Ranking on the per-slot price inverts this order: Play Padel's "
                    "1000 per 30 minutes is the most expensive court-hour in the city.",
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Price over time
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PricePoint:
    """What one court was charging as of one snapshot."""

    snapshot_id: int
    observed_at: dt.datetime
    price_per_court_hour: float
    distinct_prices_per_court_hour: tuple[float, ...]
    slots_priced: int

    @property
    def is_uniform(self) -> bool:
        """Whether every slot in this snapshot carried the same rate."""
        return len(self.distinct_prices_per_court_hour) == 1


@dataclass(frozen=True, slots=True)
class PriceChange:
    """A price change, bounded by the poll gap that contains it.

    We never see the moment a price changes: we see the last snapshot with the
    old price and the first snapshot with the new one. The change happened
    somewhere in between, so ``uncertainty_minutes`` is carried on every row and
    must be stated wherever a change timestamp is shown.
    """

    venue_uuid: str
    facility_uuid: str
    from_price_per_court_hour: float
    to_price_per_court_hour: float
    from_prices_per_court_hour: tuple[float, ...]
    to_prices_per_court_hour: tuple[float, ...]
    prev_seen_at: dt.datetime
    first_seen_at: dt.datetime
    uncertainty_minutes: int
    currency: str = CURRENCY

    @property
    def direction(self) -> str:
        if self.to_price_per_court_hour > self.from_price_per_court_hour:
            return "increase"
        if self.to_price_per_court_hour < self.from_price_per_court_hour:
            return "decrease"
        return "restructure"

    @property
    def delta_per_court_hour(self) -> float:
        return round(self.to_price_per_court_hour - self.from_price_per_court_hour, PRICE_PRECISION)


@dataclass(frozen=True, slots=True)
class PriceTimeline:
    """One court's published price across every snapshot that saw it."""

    venue_uuid: str
    facility_uuid: str
    points: tuple[PricePoint, ...]
    changes: tuple[PriceChange, ...]

    @property
    def is_flat(self) -> bool:
        """No change across the whole observation window."""
        return not self.changes

    @property
    def current_price_per_court_hour(self) -> float | None:
        return self.points[-1].price_per_court_hour if self.points else None


@dataclass(frozen=True, slots=True)
class PriceTimelineReport:
    """Price-over-time for every observed court, plus every detected change."""

    timelines: tuple[PriceTimeline, ...]
    changes: tuple[PriceChange, ...]
    metrics: tuple[MetricSpec, ...]

    @property
    def has_any_change(self) -> bool:
        return bool(self.changes)

    def timeline_for(self, facility_uuid: str) -> PriceTimeline | None:
        return next((t for t in self.timelines if t.facility_uuid == facility_uuid), None)


def price_timeline(
    observations: Iterable[SlotObservation],
    snapshots: Iterable[SnapshotRecord],
) -> PriceTimelineReport:
    """Track each court's price per court-hour over time and flag every change.

    ``snapshots`` supplies the ``observed_at`` of each poll --
    :class:`~tracker.types.SlotObservation` carries only a ``snapshot_id`` --
    and also fixes the ordering, so a snapshot written out of id order still
    lands in the right place on the timeline.

    A change is detected when the *set* of distinct per-court-hour rates a court
    is publishing differs from the previous snapshot's. That catches an
    across-the-board rise and the introduction of a second, higher peak rate
    alike. Its timestamp is an interval, never an instant: ``prev_seen_at`` and
    ``first_seen_at`` bracket it and ``uncertainty_minutes`` is their width.
    Snapshots in which a court published no priced slots are skipped rather
    than read as a price of zero.
    """
    observed_at_by_snapshot = {snapshot.snapshot_id: snapshot.observed_at for snapshot in snapshots}
    materialized = list(observations)

    grouped: dict[tuple[str, str], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for observation in materialized:
        rate = price_per_court_hour(observation)
        if rate is None:
            continue
        if observation.snapshot_id not in observed_at_by_snapshot:
            logger.warning(
                "price_timeline_snapshot_missing",
                extra={
                    "snapshot_id": observation.snapshot_id,
                    "facility_uuid": observation.facility_uuid,
                },
            )
            continue
        grouped[(observation.venue_uuid, observation.facility_uuid)][
            observation.snapshot_id
        ].append(rate)

    timelines: list[PriceTimeline] = []
    all_changes: list[PriceChange] = []
    for (venue_uuid, facility_uuid), by_snapshot in grouped.items():
        ordered = sorted(
            by_snapshot.items(), key=lambda item: (observed_at_by_snapshot[item[0]], item[0])
        )
        points = tuple(
            PricePoint(
                snapshot_id=snapshot_id,
                observed_at=observed_at_by_snapshot[snapshot_id],
                price_per_court_hour=_modal(rates),
                distinct_prices_per_court_hour=tuple(sorted(set(rates))),
                slots_priced=len(rates),
            )
            for snapshot_id, rates in ordered
        )
        changes = tuple(
            _change_between(previous, current, venue_uuid, facility_uuid)
            for previous, current in itertools.pairwise(points)
            if previous.distinct_prices_per_court_hour != current.distinct_prices_per_court_hour
        )
        all_changes.extend(changes)
        timelines.append(
            PriceTimeline(
                venue_uuid=venue_uuid,
                facility_uuid=facility_uuid,
                points=points,
                changes=changes,
            )
        )

    timelines.sort(key=lambda timeline: (timeline.venue_uuid, timeline.facility_uuid))
    all_changes.sort(key=lambda change: (change.first_seen_at, change.facility_uuid))
    return PriceTimelineReport(
        timelines=tuple(timelines),
        changes=tuple(all_changes),
        metrics=(
            MetricSpec(
                name="price_timeline",
                title="Published price per court-hour over time",
                unit=f"{CURRENCY} per court-hour",
                denominator="modal published rate per court, per poll",
                date_range=date_range_of(materialized),
                caveats=(
                    "A change is located between two polls, not at an instant; every "
                    "change carries the poll gap that brackets it.",
                    "Published rack rate only: a discount or an offline rate never appears here.",
                ),
            ),
        ),
    )


def _change_between(
    previous: PricePoint, current: PricePoint, venue_uuid: str, facility_uuid: str
) -> PriceChange:
    gap = current.observed_at - previous.observed_at
    return PriceChange(
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        from_price_per_court_hour=previous.price_per_court_hour,
        to_price_per_court_hour=current.price_per_court_hour,
        from_prices_per_court_hour=previous.distinct_prices_per_court_hour,
        to_prices_per_court_hour=current.distinct_prices_per_court_hour,
        prev_seen_at=previous.observed_at,
        first_seen_at=current.observed_at,
        uncertainty_minutes=int(gap.total_seconds() // 60),
    )


# --------------------------------------------------------------------------
# Price by hour of day
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceByHourCell:
    """What one court charges in one hour of the day."""

    venue_uuid: str
    facility_uuid: str
    hour: int
    price_per_court_hour: float
    distinct_prices_per_court_hour: tuple[float, ...]
    slots_priced: int
    currency: str = CURRENCY

    @property
    def is_uniform(self) -> bool:
        return len(self.distinct_prices_per_court_hour) == 1


@dataclass(frozen=True, slots=True)
class CourtPriceProfile:
    """Whether one court varies its price by hour of day, and what it charges."""

    venue_uuid: str
    facility_uuid: str
    is_flat: bool
    flat_price_per_court_hour: float | None
    distinct_prices_per_court_hour: tuple[float, ...]
    hours_covered: tuple[int, ...]
    currency: str = CURRENCY


@dataclass(frozen=True, slots=True)
class VenuePriceProfile:
    """The same, rolled up to a venue across all of its priced courts."""

    venue_uuid: str
    is_flat: bool
    flat_price_per_court_hour: float | None
    distinct_prices_per_court_hour: tuple[float, ...]
    courts: tuple[str, ...]
    currency: str = CURRENCY


@dataclass(frozen=True, slots=True)
class PriceByHourTable:
    """Price per hour-of-day per court, and an honest verdict on whether it varies.

    Today the verdict is "it does not". All three venues publish one rate for
    every hour of every day, so ``has_any_variation`` is ``False`` and the
    dashboard should render the sentence in :attr:`summary`, not three
    identical flat lines that imply a finding nobody has made. The cells are
    still returned so the chart exists the day a venue starts peak pricing.
    """

    cells: tuple[PriceByHourCell, ...]
    courts: tuple[CourtPriceProfile, ...]
    venues: tuple[VenuePriceProfile, ...]
    has_any_variation: bool
    metrics: tuple[MetricSpec, ...]
    hours: tuple[int, ...] = field(default=())

    @property
    def summary(self) -> str:
        """One sentence a dashboard can print in place of an empty chart."""
        if not self.courts:
            return "No priced slots observed, so hour-of-day pricing cannot be assessed."
        if not self.has_any_variation:
            return (
                f"No venue currently varies price by hour of day: all "
                f"{len(self.courts)} observed courts publish a single flat rate."
            )
        varying = [court.facility_uuid for court in self.courts if not court.is_flat]
        return f"{len(varying)} of {len(self.courts)} observed courts vary price by hour of day."

    def court(self, facility_uuid: str) -> CourtPriceProfile | None:
        return next((c for c in self.courts if c.facility_uuid == facility_uuid), None)

    def venue(self, venue_uuid: str) -> VenuePriceProfile | None:
        return next((v for v in self.venues if v.venue_uuid == venue_uuid), None)


def price_by_hour_table(observations: Iterable[SlotObservation]) -> PriceByHourTable:
    """Current price per hour-of-day per court, with explicit flatness flags.

    The hour is the local wall-clock hour the slot starts in, so a 60-minute
    Padel Up slot and the two 30-minute Padel Fort slots that overlap it all
    land in the same column and the rates compare directly.

    ``is_flat`` is per court and per venue, and ``has_any_variation`` is the
    single flag the dashboard should branch on. Reporting the absence of peak
    pricing is the finding; rendering three identical rows and letting the
    reader infer a pattern is not.
    """
    materialized = settled_observations(observations)
    per_cell: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for observation in materialized:
        rate = price_per_court_hour(observation)
        if rate is None:
            continue
        hour = slot_start_hour(observation)
        per_cell[(observation.venue_uuid, observation.facility_uuid, hour)].append(rate)

    cells = tuple(
        PriceByHourCell(
            venue_uuid=venue_uuid,
            facility_uuid=facility_uuid,
            hour=hour,
            price_per_court_hour=_modal(rates),
            distinct_prices_per_court_hour=tuple(sorted(set(rates))),
            slots_priced=len(rates),
        )
        for (venue_uuid, facility_uuid, hour), rates in sorted(per_cell.items())
    )

    courts = tuple(
        _court_profile(facility_cells)
        for facility_cells in _group(cells, key=lambda cell: cell.facility_uuid)
    )
    venues = tuple(
        _venue_profile(venue_courts)
        for venue_courts in _group(courts, key=lambda court: court.venue_uuid)
    )
    window = date_range_of(materialized)
    return PriceByHourTable(
        cells=cells,
        courts=courts,
        venues=venues,
        has_any_variation=any(not court.is_flat for court in courts),
        hours=tuple(sorted({cell.hour for cell in cells})),
        metrics=(
            MetricSpec(
                name="price_by_hour",
                title="Published price by hour of day",
                unit=f"{CURRENCY} per court-hour",
                denominator="modal published rate per court per starting hour",
                date_range=window,
                caveats=(
                    "All three venues were verified flat: one rate for every hour of "
                    "every day. This table has no variation to show yet.",
                    "Published rack rate only, and a rate is attributed to the hour a "
                    "slot starts in.",
                ),
            ),
        ),
    )


def _court_profile(cells: Sequence[PriceByHourCell]) -> CourtPriceProfile:
    distinct = tuple(
        sorted({rate for cell in cells for rate in cell.distinct_prices_per_court_hour})
    )
    is_flat = len(distinct) == 1
    return CourtPriceProfile(
        venue_uuid=cells[0].venue_uuid,
        facility_uuid=cells[0].facility_uuid,
        is_flat=is_flat,
        flat_price_per_court_hour=distinct[0] if is_flat else None,
        distinct_prices_per_court_hour=distinct,
        hours_covered=tuple(sorted(cell.hour for cell in cells)),
    )


def _venue_profile(courts: Sequence[CourtPriceProfile]) -> VenuePriceProfile:
    distinct = tuple(
        sorted({rate for court in courts for rate in court.distinct_prices_per_court_hour})
    )
    is_flat = all(court.is_flat for court in courts) and len(distinct) == 1
    return VenuePriceProfile(
        venue_uuid=courts[0].venue_uuid,
        is_flat=is_flat,
        flat_price_per_court_hour=distinct[0] if is_flat else None,
        distinct_prices_per_court_hour=distinct,
        courts=tuple(sorted(court.facility_uuid for court in courts)),
    )


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def _modal(values: Sequence[NumberT]) -> NumberT:
    """The most common value, breaking ties toward the smallest.

    Deterministic on purpose: a mean would invent a price nobody charges and a
    first-seen value would depend on dict ordering.
    """
    counts = Counter(values)
    highest = max(counts.values())
    return min(value for value, count in counts.items() if count == highest)


def _group(items: Iterable[T], key: Callable[[T], Hashable]) -> list[list[T]]:
    """Group ``items`` by ``key``, preserving first-seen group order."""
    buckets: dict[Hashable, list[T]] = {}
    for item in items:
        buckets.setdefault(key(item), []).append(item)
    return list(buckets.values())


def observed_at_index(snapshots: Iterable[SnapshotRecord]) -> Mapping[int, dt.datetime]:
    """``snapshot_id -> observed_at``, for callers stitching the two together."""
    return {snapshot.snapshot_id: snapshot.observed_at for snapshot in snapshots}
