"""Pure analytics over the observation stream.

Every module in this package is side-effect free: no database handle, no HTTP,
no :func:`datetime.now`. Functions take sequences of
:class:`~tracker.types.SlotObservation`, :class:`~tracker.types.SnapshotRecord`
and :class:`~tracker.types.FacilityFetch` and return frozen dataclasses, so the
web layer never sees a SQLAlchemy row and every number is reproducible from the
inputs alone.

This file is a re-export surface only. Modules added alongside these two export
their own names; import them from their own module rather than widening this
one into a place where import order starts to matter.
"""

from tracker.analytics.coverage import (
    DEFAULT_GAP_FACTOR,
    CoverageReport,
    FacilityDayCoverage,
    GapInterval,
    coverage_by_facility_day,
    coverage_gaps,
    coverage_report,
    expected_snapshots_per_day,
    facility_coverage_gaps,
    split_on_gaps,
)
from tracker.analytics.occupancy import (
    DAY_NAMES,
    DEFAULT_SPARSE_MIN_MINUTES,
    WEEKEND_DAYS,
    DemandSegment,
    Heatmap,
    HeatmapCell,
    OccupancyTotals,
    VenueDayOccupancy,
    WeekdayWeekendSplit,
    heatmaps_by_venue,
    occupancy_by_venue_day,
    peak_hour_heatmap,
    settled_observations,
    to_court_hours,
    weekday_vs_weekend,
)

__all__ = [
    "DAY_NAMES",
    "DEFAULT_GAP_FACTOR",
    "DEFAULT_SPARSE_MIN_MINUTES",
    "WEEKEND_DAYS",
    "CoverageReport",
    "DemandSegment",
    "FacilityDayCoverage",
    "GapInterval",
    "Heatmap",
    "HeatmapCell",
    "OccupancyTotals",
    "VenueDayOccupancy",
    "WeekdayWeekendSplit",
    "coverage_by_facility_day",
    "coverage_gaps",
    "coverage_report",
    "expected_snapshots_per_day",
    "facility_coverage_gaps",
    "heatmaps_by_venue",
    "occupancy_by_venue_day",
    "peak_hour_heatmap",
    "settled_observations",
    "split_on_gaps",
    "to_court_hours",
    "weekday_vs_weekend",
]
