"""The FastAPI JSON layer and the static dashboard mount.

Routes are thin on purpose: read the filters, load the rows the filters ask
for, call one pure analytics function, hand the result to a pydantic model.
There is no SQL here, no arithmetic on court-minutes, and no ORM object in any
response. If a number needs deriving, it is derived in ``tracker.analytics``,
where it is pure and testable without a web server.

Two behaviours the dashboard depends on:

*Every metric response answers for itself.* The envelope carries the metric's
numerator, denominator, a prose description of that denominator, the business
dates it covers and its caveats, so a chart never has to invent a caption.

*An empty database is a normal state, not an error.* This dataset is
forward-looking: on the first day there is one snapshot, and before the first
poll there are none. Every endpoint answers 200 with empty rows and an
explicit ``reason`` saying which of those it is, rather than failing or
implying a zero.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from tracker.analytics.coverage import coverage_report
from tracker.analytics.leadtime import (
    SlotBooking,
    first_booked,
    first_slot_to_go,
    lead_time_distribution,
    time_to_sellout,
)
from tracker.analytics.market import (
    blocked_inventory,
    demand_by_hour,
    market_share,
    metric_registry,
    peak_pricing_opportunity,
    revenue_proxy,
)
from tracker.analytics.occupancy import (
    DEFAULT_SPARSE_MIN_MINUTES,
    heatmaps_by_venue,
    occupancy_by_venue_day,
    peak_hour_heatmap,
    weekday_vs_weekend,
)
from tracker.analytics.pricing import (
    CURRENCY,
    DateRange,
    MetricSpec,
    date_range_of,
    price_by_hour_table,
    price_rank,
    price_timeline,
)
from tracker.analytics.transitions import (
    blocked_inventory_events,
    cancellation_rate,
    derive_transitions,
)
from tracker.config import Config, load_config
from tracker.storage import Storage
from tracker.storage_sqlite import SQLiteStorage
from tracker.types import (
    FacilityFetch,
    SlotObservation,
    SnapshotRecord,
    StateTransition,
    local_wall_clock,
)
from tracker.web.deps import Clock, ConfigDep, Filters, FiltersDep, NowDep, StorageDep
from tracker.web.schemas import (
    METRIC_DEFINITIONS,
    BlockedEventOut,
    BlockedEventsResponse,
    CancellationRateOut,
    CancellationsResponse,
    CatalogResponse,
    CircuitOut,
    CourtOut,
    CoverageResponse,
    DateRangeOut,
    FirstSlotOut,
    FirstSlotResponse,
    HealthResponse,
    HeatmapOut,
    LeadTimeResponse,
    MarketShareResponse,
    MetricMeta,
    MetricSpecOut,
    OccupancyDailyResponse,
    OccupancyHeatmapResponse,
    PricingByHourResponse,
    PricingTimelineResponse,
    Readiness,
    RevenueProxyResponse,
    SelloutResponse,
    SnapshotOut,
    VenueDataQualityOut,
    VenueDayOut,
    VenueOut,
    VenuesResponse,
    WeekdayWeekendResponse,
    envelope,
    facility_names,
    readiness_for,
    venue_names,
)

logger = logging.getLogger("tracker.web.app")

#: The vanilla dashboard lives here. Its contents are written separately; this
#: module only guarantees the directory exists so the mount cannot fail on a
#: fresh checkout.
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: A collector is considered stale once it has missed this many cadences.
STALE_CADENCES = 2

#: Bounds for "every snapshot ever", since the protocol's snapshot read is a
#: half-open window rather than a list-all.
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_FAR_FUTURE = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)

_NO_SNAPSHOTS = (
    "no snapshots recorded yet: the collector has not run against this database. "
    "This dataset is forward-looking and cannot be backfilled, so the first poll "
    "is the earliest data that can ever exist."
)

router = APIRouter(prefix="/api", tags=["metrics"])


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def create_app(
    *,
    config: Config | None = None,
    config_path: str | Path | None = None,
    storage: Storage | None = None,
    clock: Clock | None = None,
    cors_origins: Sequence[str] = (),
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the application.

    Passing ``storage`` (and usually ``config``) injects an already-open
    backend, which is how the tests run against in-memory SQLite; ownership
    stays with the caller and the lifespan will not close it. Passing neither
    makes the lifespan load the config and open the configured database, and
    close it again on shutdown.

    CORS is off unless ``cors_origins`` is given. This is a local-first tool
    whose API and dashboard are served from the same origin; a permissive
    default would exist only to support a deployment that does not exist.
    """
    directory = STATIC_DIR if static_dir is None else static_dir
    directory.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="Padel court occupancy tracker",
        description=(
            "Court occupancy, lead time, pricing and coverage for the three "
            "padel venues in Jaipur listed on Hudle. Every metric response "
            "carries its own denominator, date range and caveats."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.config_path = config_path
    app.state.storage = storage
    app.state.owns_storage = False
    app.state.clock = clock or _utc_now

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    app.include_router(router)
    # Mounted last: a mount at "/" matches every path, so the API routes have
    # to be registered before it.
    app.mount("/", StaticFiles(directory=directory, html=True), name="dashboard")
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the configured storage unless one was injected."""
    if app.state.config is None:
        app.state.config = load_config(_config_path(app))
    if app.state.storage is None:
        config: Config = app.state.config
        storage = SQLiteStorage(
            config.storage.url,
            expected_snapshots_per_day=config.poll.expected_snapshots_per_day,
        )
        storage.initialize()
        app.state.storage = storage
        app.state.owns_storage = True
        logger.info("web_storage_opened", extra={"url": config.storage.url})
    try:
        yield
    finally:
        if app.state.owns_storage:
            # Storage is a protocol without close(); only the concrete backend
            # this branch opened has a pool to release.
            app.state.storage.close()
            app.state.owns_storage = False
            logger.info("web_storage_closed", extra={})


def _config_path(app: FastAPI) -> Path:
    """Where to load config from when the caller did not pass one."""
    configured = app.state.config_path
    if configured is not None:
        return Path(configured)
    from_env = os.environ.get("PADEL_TRACKER_CONFIG")
    if from_env:
        return Path(from_env)
    return Path.cwd() / "config.yaml"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------
# Health and dimensions
# --------------------------------------------------------------------------


@router.get("/health", summary="Collector liveness, staleness and breaker state")
def health(config: ConfigDep, storage: StorageDep, now: NowDep) -> HealthResponse:
    latest = storage.latest_snapshot()
    cadence = config.poll.cadence_minutes
    stale_after = cadence * STALE_CADENCES
    recent = storage.snapshots_between(now - dt.timedelta(days=1), _FAR_FUTURE)
    failures = _trailing_failures(recent)
    breaker_open = failures >= config.poll.max_consecutive_failures

    if latest is None:
        return HealthResponse(
            ok=False,
            status="no_data",
            reason=_NO_SNAPSHOTS,
            generated_at=now,
            cadence_minutes=cadence,
            last_snapshot=None,
            staleness_minutes=None,
            stale=False,
            stale_after_minutes=stale_after,
            expected_next_at=None,
            snapshots_last_24h=0,
            failed_snapshots_last_24h=0,
            circuit=_circuit(failures, config, breaker_open),
        )

    staleness = (now - latest.observed_at).total_seconds() / 60.0
    stale = staleness > stale_after
    status = "open_circuit" if breaker_open else ("stale" if stale else "ok")
    reason = None
    if breaker_open:
        reason = (
            f"the last {failures} polls failed, at or above the configured "
            f"limit of {config.poll.max_consecutive_failures}: the collector's "
            "breaker would be open and no new data is arriving"
        )
    elif stale:
        reason = (
            f"the last snapshot is {staleness:.0f} minutes old, more than "
            f"{STALE_CADENCES} polling cadences ({stale_after} minutes). "
            "Slots that elapsed since then can never be re-observed."
        )
    return HealthResponse(
        ok=not stale and not breaker_open,
        status=status,
        reason=reason,
        generated_at=now,
        cadence_minutes=cadence,
        last_snapshot=SnapshotOut.from_record(latest),
        staleness_minutes=round(staleness, 2),
        stale=stale,
        stale_after_minutes=stale_after,
        expected_next_at=latest.observed_at + dt.timedelta(minutes=cadence),
        snapshots_last_24h=len(recent),
        failed_snapshots_last_24h=sum(1 for s in recent if not s.ok),
        circuit=_circuit(failures, config, breaker_open),
    )


@router.get("/venues", summary="Venue and court dimensions with data-quality flags")
def venues(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> VenuesResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    report = market_share(observations, sport=filters.sport)
    quality = {row.venue_uuid: row for row in report.venues}

    dims = {dim.venue_uuid: dim for dim in storage.list_venue_dims()}
    facility_dims = {dim.facility_uuid: dim for dim in storage.list_facility_dims()}
    names = venue_names(config, dims.values())

    rows: list[VenueOut] = []
    for venue in config.venues:
        if venue.uuid not in filters.visible_venue_uuids:
            continue
        dim = dims.get(venue.uuid)
        rows.append(
            VenueOut(
                venue_uuid=venue.uuid,
                name=venue.name,
                short_name=venue.short_name,
                slug=venue.slug,
                numeric_id=venue.numeric_id,
                tz=dim.tz if dim else config.timezone,
                active=venue.active,
                in_config=True,
                first_seen=dim.first_seen if dim else None,
                last_seen=dim.last_seen if dim else None,
                courts=[
                    CourtOut(
                        facility_uuid=facility.uuid,
                        name=facility.name,
                        kind=str(facility.kind),
                        sport=str(facility.sport),
                        grid_minutes=facility.grid_minutes,
                        price_per_court_hour=facility.price_per_court_hour,
                        price_per_slot=facility.price_per_slot,
                        active=facility.active,
                        first_seen=(
                            facility_dims[facility.uuid].first_seen
                            if facility.uuid in facility_dims
                            else None
                        ),
                        last_seen=(
                            facility_dims[facility.uuid].last_seen
                            if facility.uuid in facility_dims
                            else None
                        ),
                    )
                    for facility in venue.facilities
                ],
                data_quality=(
                    VenueDataQualityOut.from_quality(quality[venue.uuid], names)
                    if venue.uuid in quality
                    else None
                ),
            )
        )

    # The venue set is frozen in config, so rows exist even before the first
    # poll; only the data-quality flags depend on observations.
    _, missing = _status(
        count=len(observations),
        observations=observations,
        snapshots=snapshots,
        filters=filters,
    )
    reason = (
        None
        if missing is None
        else f"venue rows come from config; data-quality flags are absent because {missing}"
    )
    empty = not rows
    return VenuesResponse(
        **envelope(
            "venues",
            readiness=_readiness("venues", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
            specs=report.metrics,
        ),
        venues=rows,
    )


# --------------------------------------------------------------------------
# Occupancy
# --------------------------------------------------------------------------


@router.get("/occupancy/daily", summary="Occupancy per venue per business date")
def occupancy_daily(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> OccupancyDailyResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    rows = occupancy_by_venue_day(
        observations,
        venue_uuid=filters.venue_uuid,
        sport=filters.sport,
        business_date_from=filters.start,
        business_date_to=filters.end,
    )
    names = venue_names(config)
    empty, reason = _status(
        count=len(rows), observations=observations, snapshots=snapshots, filters=filters
    )
    return OccupancyDailyResponse(
        **envelope(
            "occupancy_daily",
            readiness=_readiness("occupancy_daily", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
        rows=[VenueDayOut.from_row(row, names) for row in rows],
    )


@router.get("/occupancy/heatmap", summary="Occupancy by hour of day and day of week")
def occupancy_heatmap(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> OccupancyHeatmapResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    combined = peak_hour_heatmap(
        observations,
        venue_uuid=filters.venue_uuid,
        sport=filters.sport,
        sparse_min_minutes=DEFAULT_SPARSE_MIN_MINUTES,
    )
    per_venue = heatmaps_by_venue(
        observations, sport=filters.sport, sparse_min_minutes=DEFAULT_SPARSE_MIN_MINUTES
    )
    names = venue_names(config)
    empty, reason = _status(
        count=len(combined.cells),
        observations=observations,
        snapshots=snapshots,
        filters=filters,
    )
    return OccupancyHeatmapResponse(
        **envelope(
            "occupancy_heatmap",
            readiness=_readiness("occupancy_heatmap", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
        combined=HeatmapOut.from_heatmap(combined, names),
        by_venue=[
            HeatmapOut.from_heatmap(heatmap, names)
            for _, heatmap in sorted(per_venue.items(), key=lambda item: item[0])
        ],
    )


@router.get("/metrics/weekday-weekend", summary="Weekday versus weekend demand")
def weekday_weekend(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> WeekdayWeekendResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    split = weekday_vs_weekend(observations, venue_uuid=filters.venue_uuid, sport=filters.sport)
    empty, reason = _status(
        count=split.weekday.slots + split.weekend.slots,
        observations=observations,
        snapshots=snapshots,
        filters=filters,
    )
    return WeekdayWeekendResponse.from_split(
        split,
        **envelope(
            "weekday_weekend",
            readiness=_readiness("weekday_weekend", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
    )


# --------------------------------------------------------------------------
# Lead time
# --------------------------------------------------------------------------


@router.get("/leadtime", summary="How far ahead slots are booked")
def leadtime(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> LeadTimeResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    bookings = _bookings(observations, snapshots)
    distribution = lead_time_distribution(bookings, peak_hours=config.dashboard.peak_hours)
    empty, reason = _status(
        count=distribution.overall.n,
        observations=observations,
        snapshots=snapshots,
        filters=filters,
        needs_history=True,
    )
    return LeadTimeResponse.from_distribution(
        distribution,
        bookings,
        venue_names(config),
        **envelope(
            "leadtime",
            readiness=_readiness("leadtime", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
    )


@router.get("/metrics/sellout", summary="How long peak slots take to sell")
def sellout(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> SelloutResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    bookings = _bookings(observations, snapshots)
    report = time_to_sellout(bookings, peak_hours=config.dashboard.peak_hours)
    empty, reason = _status(
        count=report.n,
        observations=observations,
        snapshots=snapshots,
        filters=filters,
        needs_history=True,
    )
    return SelloutResponse.from_report(
        report,
        venue_names(config),
        **envelope(
            "sellout",
            readiness=_readiness("sellout", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
    )


@router.get("/metrics/first-slot", summary="The first slot to sell on each business date")
def first_slot(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> FirstSlotResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    rows = first_slot_to_go(_bookings(observations, snapshots))
    names = venue_names(config)
    empty, reason = _status(
        count=len(rows),
        observations=observations,
        snapshots=snapshots,
        filters=filters,
        needs_history=True,
    )
    return FirstSlotResponse(
        **envelope(
            "first_slot",
            readiness=_readiness("first_slot", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
        rows=[FirstSlotOut.from_row(row, names) for row in rows],
    )


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


@router.get("/metrics/cancellations", summary="Cancellations per venue-week")
def cancellations(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> CancellationsResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    transitions = _transitions(observations, snapshots)
    rows = cancellation_rate(transitions, observations)
    names = venue_names(config)
    empty, reason = _status(
        count=len(rows),
        observations=observations,
        snapshots=snapshots,
        filters=filters,
        needs_history=True,
    )
    return CancellationsResponse(
        **envelope(
            "cancellations",
            readiness=_readiness("cancellations", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
        rows=[CancellationRateOut.from_row(row, names) for row in rows],
    )


@router.get("/metrics/blocked-events", summary="Inventory withdrawn from sale")
def blocked_events(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> BlockedEventsResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    transitions = _transitions(observations, snapshots)
    rows = blocked_inventory_events(transitions, observations)
    names = venue_names(config)
    empty, reason = _status(
        count=len(rows), observations=observations, snapshots=snapshots, filters=filters
    )
    return BlockedEventsResponse(
        **envelope(
            "blocked_events",
            readiness=_readiness("blocked_events", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
        rows=[BlockedEventOut.from_event(row, names) for row in rows],
    )


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


@router.get("/pricing/timeline", summary="Price per court-hour over time")
def pricing_timeline(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> PricingTimelineResponse:
    observations = _all_observations(storage, filters)
    snapshots = _all_snapshots(storage)
    report = price_timeline(observations, snapshots)
    empty, reason = _status(
        count=len(report.timelines),
        observations=observations,
        snapshots=snapshots,
        filters=filters,
    )
    return PricingTimelineResponse.from_report(
        report,
        venue_names(config),
        facility_names(config),
        CURRENCY,
        **envelope(
            "pricing_timeline",
            readiness=_readiness("pricing_timeline", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
            specs=report.metrics,
        ),
    )


@router.get("/pricing/by-hour", summary="Price by hour of day, per court-hour")
def pricing_by_hour(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> PricingByHourResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    table = price_by_hour_table(observations)
    empty, reason = _status(
        count=len(table.cells), observations=observations, snapshots=snapshots, filters=filters
    )
    return PricingByHourResponse.from_table(
        table,
        venue_names(config),
        facility_names(config),
        **envelope(
            "pricing_by_hour",
            readiness=_readiness("pricing_by_hour", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
            specs=table.metrics,
        ),
    )


# --------------------------------------------------------------------------
# Market
# --------------------------------------------------------------------------


@router.get("/market/share", summary="Demand share and supply share per venue-week")
def market_share_endpoint(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> MarketShareResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    report = market_share(observations, sport=filters.sport)
    empty, reason = _status(
        count=len(report.rows), observations=observations, snapshots=snapshots, filters=filters
    )
    return MarketShareResponse.from_report(
        report,
        venue_names(config),
        **envelope(
            "market_share",
            readiness=_readiness("market_share", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
            specs=report.metrics,
        ),
    )


@router.get("/market/revenue-proxy", summary="Booked court-hours priced out, as a proxy")
def market_revenue_proxy(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> RevenueProxyResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    report = revenue_proxy(observations, sport=filters.sport)
    empty, reason = _status(
        count=len(report.rows), observations=observations, snapshots=snapshots, filters=filters
    )
    return RevenueProxyResponse.from_report(
        report,
        venue_names(config),
        **envelope(
            "revenue_proxy",
            readiness=_readiness("revenue_proxy", storage, config, now),
            date_range=date_range_of(observations),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
            specs=report.metrics,
        ),
    )


# --------------------------------------------------------------------------
# Coverage and catalog
# --------------------------------------------------------------------------


@router.get("/coverage", summary="Polls received, and the gaps between them")
def coverage(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> CoverageResponse:
    snapshots = _snapshots_in_window(storage, filters)
    fetches = _fetches(storage, snapshots)
    report = coverage_report(
        snapshots,
        fetches,
        cadence_minutes=config.poll.cadence_minutes,
        snapshots_expected_per_day=config.poll.expected_snapshots_per_day,
    )
    empty = not snapshots
    reason: str | None = None
    if not snapshots:
        reason = _NO_SNAPSHOTS
    elif len(snapshots) == 1:
        reason = (
            "one snapshot recorded: coverage needs at least two polls before a "
            "gap between them can exist."
        )
    return CoverageResponse.from_report(
        report,
        facility_names(config, storage.list_facility_dims()),
        **envelope(
            "coverage",
            readiness=_readiness("coverage", storage, config, now),
            date_range=_observed_date_range(snapshots),
            filters=filters,
            now=now,
            empty=empty,
            reason=reason,
        ),
    )


@router.get("/metrics/catalog", summary="Every metric, its denominator and its caveats")
def catalog(
    config: ConfigDep, storage: StorageDep, filters: FiltersDep, now: NowDep
) -> CatalogResponse:
    observations = _observations(storage, filters)
    snapshots = _all_snapshots(storage)
    window = date_range_of(observations)
    specs: list[MetricSpec] = []
    if observations:
        registry = metric_registry(
            price_rank(observations),
            price_timeline(observations, snapshots),
            price_by_hour_table(observations),
            revenue_proxy(observations, sport=filters.sport),
            market_share(observations, sport=filters.sport),
            demand_by_hour(observations, sport=filters.sport),
            blocked_inventory(observations, sport=filters.sport),
            peak_pricing_opportunity(
                observations, peak_hours=config.dashboard.peak_hours, sport=filters.sport
            ),
        )
        specs = list(registry.specs)
    by_name: dict[str, list[MetricSpec]] = {}
    for spec in specs:
        by_name.setdefault(spec.name, []).append(spec)
    return CatalogResponse(
        generated_at=now,
        date_range=DateRangeOut.from_range(window),
        metrics=[
            MetricMeta.build(name, window, specs=by_name.get(name, ()))
            for name in METRIC_DEFINITIONS
        ],
        analytics_specs=[MetricSpecOut.from_spec(spec) for spec in specs],
        reason=_catalog_reason(observations, snapshots, filters),
    )


# --------------------------------------------------------------------------
# Loading and status helpers
# --------------------------------------------------------------------------


def _catalog_reason(
    observations: Sequence[SlotObservation],
    snapshots: Sequence[SnapshotRecord],
    filters: Filters,
) -> str | None:
    """Why the catalog carries only static definitions and no live date range."""
    if observations:
        return None
    if not snapshots:
        return _NO_SNAPSHOTS
    return _no_rows(filters, snapshots=snapshots)


def _all_observations(storage: Storage, filters: Filters) -> list[SlotObservation]:
    """Every observation the filters ask for, unreduced.

    ``price_timeline`` builds one point per (facility, snapshot) from the set
    of rates published in that poll and reports how many slots carried one, so
    it is precisely the row-counting consumer ``iter_key_observations`` is
    lossy for. It reads the full stream; every other endpoint does not.
    """
    rows = storage.iter_observations(
        venue_uuid=filters.venue_uuid,
        sport=filters.sport,
        business_date_from=filters.start,
        business_date_to=filters.end,
    )
    visible = filters.visible_venue_uuids
    return [row for row in rows if row.venue_uuid in visible]


def _observations(storage: Storage, filters: Filters) -> list[SlotObservation]:
    """Every observation the filters ask for, materialized for repeated passes.

    Dashboard-hidden venues are dropped here, in the one place every endpoint
    loads observations through, rather than in each metric. A venue withheld in
    some charts and counted in others would be worse than either choice.
    """
    rows = storage.iter_observations(
        venue_uuid=filters.venue_uuid,
        sport=filters.sport,
        business_date_from=filters.start,
        business_date_to=filters.end,
    )
    visible = filters.visible_venue_uuids
    return [row for row in rows if row.venue_uuid in visible]


def _all_snapshots(storage: Storage) -> list[SnapshotRecord]:
    """Every snapshot, whatever the business-date filter.

    A slot's history is spread across polls whose instants have nothing to do
    with the slot's business date, so narrowing snapshots to the requested
    window would hide the very transitions the window is asking about.
    """
    return storage.snapshots_between(_EPOCH, _FAR_FUTURE)


def _snapshots_in_window(storage: Storage, filters: Filters) -> list[SnapshotRecord]:
    """Snapshots by observation date, which is what coverage measures."""
    start = _EPOCH if filters.start is None else _utc_midnight(filters.start)
    end = _FAR_FUTURE if filters.end is None else _utc_midnight(filters.end) + dt.timedelta(days=1)
    return storage.snapshots_between(start, end)


def _fetches(storage: Storage, snapshots: Sequence[SnapshotRecord]) -> list[FacilityFetch]:
    rows: list[FacilityFetch] = []
    for snapshot in snapshots:
        rows.extend(storage.facility_fetches_for_snapshot(snapshot.snapshot_id))
    return rows


def _transitions(
    observations: Sequence[SlotObservation], snapshots: Sequence[SnapshotRecord]
) -> list[StateTransition]:
    """Derive state changes, or nothing at all when there is no history.

    ``derive_transitions`` refuses an empty snapshot list -- without poll
    instants an observation's position in time is unknowable -- so the empty
    database is handled here rather than by catching its exception.
    """
    if not snapshots or not observations:
        return []
    return derive_transitions(observations, snapshots)


def _bookings(
    observations: Sequence[SlotObservation], snapshots: Sequence[SnapshotRecord]
) -> list[SlotBooking]:
    transitions = _transitions(observations, snapshots)
    if not transitions:
        return []
    return first_booked(transitions, observations)


def _status(
    *,
    count: int,
    observations: Sequence[SlotObservation],
    snapshots: Sequence[SnapshotRecord],
    filters: Filters,
    needs_history: bool = False,
) -> tuple[bool, str | None]:
    """Whether the response is empty, and the honest reason why.

    An empty answer is never returned bare: the caller always learns whether
    the collector has not run, whether the filters excluded everything, or
    whether there is simply not enough history yet for this particular metric.
    """
    if count:
        return False, None
    if not snapshots:
        return True, _NO_SNAPSHOTS
    if not observations:
        return True, (
            f"{len(snapshots)} snapshot(s) recorded, but none of their observations "
            f"match {_filter_summary(filters)}"
        )
    if needs_history and len(snapshots) < 2:
        return True, (
            "only one snapshot recorded: bookings, cancellations and blocks are "
            "state *changes*, and a change needs at least two polls to be visible. "
            "This dataset cannot be backfilled, so the wait is unavoidable."
        )
    return True, _no_rows(filters, observations=observations, snapshots=snapshots)


def _readiness(name: str, storage: Storage, config: Config, now: dt.datetime) -> Readiness:
    """How much of the dataset exists, judged against what this metric needs.

    Measured on the whole database, never on the filtered window: a request
    for the next seven days has zero elapsed days in it, and that says nothing
    about whether the metric itself is trustworthy yet.
    """
    today = local_wall_clock(now, config.timezone).date()
    elapsed = storage.query_rows(
        "SELECT count(DISTINCT business_date) AS n FROM slot_observations "
        "WHERE business_date < :today",
        {"today": today.isoformat()},
    )
    return readiness_for(
        name,
        snapshots=len(_all_snapshots(storage)),
        elapsed_days=int(elapsed[0]["n"]) if elapsed else 0,
    )


def _no_rows(
    filters: Filters,
    *,
    observations: Sequence[SlotObservation] = (),
    snapshots: Sequence[SnapshotRecord] = (),
) -> str:
    return (
        f"{len(observations)} observation(s) over {len(snapshots)} snapshot(s) "
        f"produced no rows for {_filter_summary(filters)}"
    )


def _filter_summary(filters: Filters) -> str:
    window = "all dates"
    if filters.start or filters.end:
        window = f"{filters.start or 'start'} to {filters.end or 'latest'}"
    return (
        f"sport={filters.sport_label}, venue={filters.venue_uuid or 'all'}, business dates={window}"
    )


def _trailing_failures(snapshots: Sequence[SnapshotRecord]) -> int:
    """How many of the most recent consecutive snapshots failed."""
    failures = 0
    for snapshot in sorted(snapshots, key=lambda s: s.observed_at, reverse=True):
        if snapshot.ok:
            break
        failures += 1
    return failures


def _circuit(failures: int, config: Config, breaker_open: bool) -> CircuitOut:
    return CircuitOut(
        state="open" if breaker_open else "closed",
        consecutive_failures=failures,
        max_consecutive_failures=config.poll.max_consecutive_failures,
        source=(
            "reconstructed from the trailing run of failed snapshots; the web "
            "process does not share memory with the collector's breaker"
        ),
    )


def _observed_date_range(snapshots: Iterable[SnapshotRecord]) -> DateRange:
    """The span of UTC observation dates, which is what coverage is indexed on."""
    dates = {snapshot.observed_at.date() for snapshot in snapshots}
    if not dates:
        return DateRange(start=None, end=None, business_days=0)
    return DateRange(start=min(dates), end=max(dates), business_days=len(dates))


def _utc_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)


app = create_app()
