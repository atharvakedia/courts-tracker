"""Coverage analytics: what the collector actually saw, and where it did not.

Pure functions over :class:`~tracker.types.SnapshotRecord` and
:class:`~tracker.types.FacilityFetch`. No database handle, no HTTP, no
:func:`datetime.now`.

The dataset is forward-looking only. A poll that never ran is a hole that can
never be filled, so coverage is not a health metric that can be quietly
smoothed over -- it is part of the data. Two consequences run through this
module:

**Gaps are returned as explicit intervals.** A chart that joins the point
before a gap to the point after it draws a line through time nobody observed,
and that line is indistinguishable from measurement. :func:`coverage_gaps`
hands back the real ``[start, end]`` instants so the dashboard can draw the
hole, and :func:`split_on_gaps` breaks a series into segments so a line chart
physically cannot span one.

**Per-facility, not just per-poll.** A snapshot that ran but whose Padel Fort
fetch failed is complete coverage for the other venues and a hole for Padel
Fort. Failed fetches are counted separately from missing snapshots for exactly
that reason.

``observed_date`` here is the **UTC** date of the observation instant, not a
business date. It answers "did the collector run", which is a question about
our machine, not about a venue's trading day. Occupancy analytics use
``business_date``; the two are deliberately different keys.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, TypeVar

from tracker.types import FacilityFetch, SnapshotRecord, to_utc_text

MINUTES_PER_DAY = 24 * 60

#: How far a poll may slip before the space between two snapshots counts as a
#: gap. The scheduler fires on a cadence but never exactly on it, so a small
#: overrun is jitter, not a missed poll. 1.5 cadences is the midpoint: it
#: cannot be reached without a whole poll having been skipped.
DEFAULT_GAP_FACTOR = 1.5

T = TypeVar("T")


def expected_snapshots_per_day(cadence_minutes: int) -> int:
    """Whole polls a day at ``cadence_minutes``, the coverage denominator."""
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be positive")
    return MINUTES_PER_DAY // cadence_minutes


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapInterval:
    """A stretch of time for which no observation exists.

    ``start`` is the last instant that *was* observed and ``end`` the next one,
    so the unobserved interior is the open interval between them. Drawing the
    band from ``start`` to ``end`` is honest: the truth somewhere inside it is
    unknown and unrecoverable.

    ``facility_uuid`` is ``None`` for a gap in the poll schedule itself -- no
    snapshot ran at all -- and set when the poll ran but this facility's fetch
    did not land.
    """

    facility_uuid: str | None
    start: dt.datetime
    end: dt.datetime
    minutes: int
    missed_polls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "facility_uuid": self.facility_uuid,
            "start": to_utc_text(self.start),
            "end": to_utc_text(self.end),
            "minutes": self.minutes,
            "missed_polls": self.missed_polls,
        }


@dataclass(frozen=True, slots=True)
class FacilityDayCoverage:
    """Expected vs received polls for one facility on one UTC date.

    Matches the ``v_coverage_daily`` SQL view field for field, including its
    definition of ``snapshots_received``: snapshots in which this facility had
    a fetch row at all, successful or not. ``fetches_ok`` and
    ``fetches_failed`` split that count, because a fetch that ran and failed is
    a different operational fact from a poll that never ran.
    """

    facility_uuid: str
    observed_date: dt.date
    snapshots_expected: int
    snapshots_received: int
    fetches_ok: int
    fetches_failed: int
    slot_rows: int
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None

    @property
    def coverage_ratio(self) -> float | None:
        """Received over expected. ``None`` when nothing was expected.

        Can exceed 1.0 if the collector was run manually on top of the cron
        schedule; that is reported rather than clamped, because a number above
        100% is a real signal that the cadence assumption is wrong.
        """
        if self.snapshots_expected == 0:
            return None
        return self.snapshots_received / self.snapshots_expected

    @property
    def snapshots_missing(self) -> int:
        """Polls expected but never seen, floored at zero."""
        return max(self.snapshots_expected - self.snapshots_received, 0)

    @property
    def is_complete(self) -> bool:
        return self.snapshots_missing == 0 and self.fetches_failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "facility_uuid": self.facility_uuid,
            "observed_date": self.observed_date.isoformat(),
            "snapshots_expected": self.snapshots_expected,
            "snapshots_received": self.snapshots_received,
            "snapshots_missing": self.snapshots_missing,
            "fetches_ok": self.fetches_ok,
            "fetches_failed": self.fetches_failed,
            "slot_rows": self.slot_rows,
            "first_observed_at": (
                None if self.first_observed_at is None else to_utc_text(self.first_observed_at)
            ),
            "last_observed_at": (
                None if self.last_observed_at is None else to_utc_text(self.last_observed_at)
            ),
            "coverage_ratio": self.coverage_ratio,
            "is_complete": self.is_complete,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Everything the dashboard needs to draw honest coverage.

    ``poll_gaps`` are holes in the schedule itself; ``facility_gaps`` are holes
    in one facility's series, which includes every poll gap plus the polls
    where that facility's own fetch failed.
    """

    cadence_minutes: int
    snapshots_expected_per_day: int
    days: tuple[FacilityDayCoverage, ...]
    poll_gaps: tuple[GapInterval, ...]
    facility_gaps: dict[str, tuple[GapInterval, ...]]

    @property
    def has_gaps(self) -> bool:
        return bool(self.poll_gaps) or any(gaps for gaps in self.facility_gaps.values())

    @property
    def missed_polls(self) -> int:
        return sum(gap.missed_polls for gap in self.poll_gaps)

    def gaps_for(self, facility_uuid: str) -> tuple[GapInterval, ...]:
        """Every gap in one facility's series, poll gaps included."""
        return self.facility_gaps.get(facility_uuid, ())

    def days_for(self, facility_uuid: str) -> tuple[FacilityDayCoverage, ...]:
        return tuple(day for day in self.days if day.facility_uuid == facility_uuid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cadence_minutes": self.cadence_minutes,
            "snapshots_expected_per_day": self.snapshots_expected_per_day,
            "has_gaps": self.has_gaps,
            "missed_polls": self.missed_polls,
            "days": [day.to_dict() for day in self.days],
            "poll_gaps": [gap.to_dict() for gap in self.poll_gaps],
            "facility_gaps": {
                facility_uuid: [gap.to_dict() for gap in gaps]
                for facility_uuid, gaps in self.facility_gaps.items()
            },
        }


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------


def coverage_gaps(
    snapshots: Iterable[SnapshotRecord],
    *,
    cadence_minutes: int,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    facility_uuid: str | None = None,
) -> list[GapInterval]:
    """Holes in a poll schedule, as explicit intervals.

    Two consecutive snapshots more than ``cadence_minutes * gap_factor`` apart
    bracket a gap. The returned interval runs from the earlier snapshot's
    ``observed_at`` to the later one's, and ``missed_polls`` is how many
    scheduled polls fit inside it.

    Only snapshots that succeeded (``ok``) count as observations: a poll that
    ran and failed produced no data, so the hole it left is real.

    Gaps before the first snapshot and after the last are not reported. There
    is no evidence about what the cadence was supposed to be outside the
    observed range, and inventing a gap there would make a collector that
    started yesterday look broken.
    """
    return _gaps_between(
        (s.observed_at for s in snapshots if s.ok),
        cadence_minutes=cadence_minutes,
        gap_factor=gap_factor,
        facility_uuid=facility_uuid,
    )


def facility_coverage_gaps(
    snapshots: Iterable[SnapshotRecord],
    fetches: Iterable[FacilityFetch],
    *,
    cadence_minutes: int,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> dict[str, list[GapInterval]]:
    """Per-facility holes: missed polls *and* failed fetches, together.

    A facility's series contains a point only where a snapshot ran and that
    facility's fetch succeeded. So a venue whose fetch 500s for an hour has an
    hour-long hole even though the collector itself never missed a beat, and
    the dashboard must not join across it.
    """
    snapshot_times = {s.snapshot_id: s.observed_at for s in snapshots if s.ok}

    per_facility: dict[str, list[dt.datetime]] = {}
    for fetch in fetches:
        observed_at = snapshot_times.get(fetch.snapshot_id)
        if observed_at is None or not fetch.ok:
            continue
        per_facility.setdefault(fetch.facility_uuid, []).append(observed_at)

    return {
        facility_uuid: _gaps_between(
            instants,
            cadence_minutes=cadence_minutes,
            gap_factor=gap_factor,
            facility_uuid=facility_uuid,
        )
        for facility_uuid, instants in sorted(per_facility.items())
    }


def split_on_gaps(
    points: Sequence[tuple[dt.datetime, T]], gaps: Iterable[GapInterval]
) -> list[list[tuple[dt.datetime, T]]]:
    """Break a time series into segments so no line spans a gap.

    Each returned segment is a run of consecutive points with no gap between
    them; plotting each segment as its own series leaves the hole visibly
    empty. Interpolating across a gap would draw observations that were never
    made, over an interval that can never be re-collected.

    Points are sorted by instant first, so a caller cannot defeat this by
    handing over an unordered series.
    """
    ordered = sorted(points, key=lambda point: point[0])
    boundaries = sorted((gap.start, gap.end) for gap in gaps)
    if not ordered:
        return []
    if not boundaries:
        return [list(ordered)]

    segments: list[list[tuple[dt.datetime, T]]] = [[ordered[0]]]
    for previous, current in pairwise(ordered):
        if any(previous[0] <= start and end <= current[0] for start, end in boundaries):
            segments.append([])
        segments[-1].append(current)
    return segments


# --------------------------------------------------------------------------
# Daily coverage
# --------------------------------------------------------------------------


def coverage_by_facility_day(
    snapshots: Iterable[SnapshotRecord],
    fetches: Iterable[FacilityFetch],
    *,
    cadence_minutes: int,
    snapshots_expected_per_day: int | None = None,
) -> list[FacilityDayCoverage]:
    """Expected vs received polls per (facility, UTC date).

    ``snapshots_expected_per_day`` defaults to the whole-day figure implied by
    the cadence. A partial first or last day therefore reports low coverage,
    which is honest: those polls genuinely did not happen, and the alternative
    -- prorating the expectation to the observed window -- would make a
    collector that ran twice and stopped report 100%.

    Every snapshot is counted, failed ones included, because a facility fetch
    row exists for attempts that failed and ``fetches_failed`` is the column
    that tells the operator which kind of hole they have.
    """
    expected = (
        expected_snapshots_per_day(cadence_minutes)
        if snapshots_expected_per_day is None
        else snapshots_expected_per_day
    )
    if expected <= 0:
        raise ValueError("snapshots_expected_per_day must be positive")

    snapshot_times = {s.snapshot_id: s.observed_at for s in snapshots}

    rows: dict[tuple[str, dt.date], _DayAccumulator] = {}
    for fetch in fetches:
        observed_at = snapshot_times.get(fetch.snapshot_id)
        if observed_at is None:
            continue
        key = (fetch.facility_uuid, observed_at.date())
        rows.setdefault(key, _DayAccumulator()).add(fetch, observed_at)

    return [
        accumulator.as_coverage(
            facility_uuid=key[0], observed_date=key[1], snapshots_expected=expected
        )
        for key, accumulator in sorted(rows.items())
    ]


def coverage_report(
    snapshots: Iterable[SnapshotRecord],
    fetches: Iterable[FacilityFetch],
    *,
    cadence_minutes: int,
    snapshots_expected_per_day: int | None = None,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> CoverageReport:
    """Daily coverage and gap intervals in one pass-friendly result.

    The iterables are materialized once here, so a caller may pass generators
    from ``Storage`` without them being consumed by the first of the three
    computations.
    """
    snapshot_list = list(snapshots)
    fetch_list = list(fetches)
    expected = (
        expected_snapshots_per_day(cadence_minutes)
        if snapshots_expected_per_day is None
        else snapshots_expected_per_day
    )

    return CoverageReport(
        cadence_minutes=cadence_minutes,
        snapshots_expected_per_day=expected,
        days=tuple(
            coverage_by_facility_day(
                snapshot_list,
                fetch_list,
                cadence_minutes=cadence_minutes,
                snapshots_expected_per_day=expected,
            )
        ),
        poll_gaps=tuple(
            coverage_gaps(snapshot_list, cadence_minutes=cadence_minutes, gap_factor=gap_factor)
        ),
        facility_gaps={
            facility_uuid: tuple(gaps)
            for facility_uuid, gaps in facility_coverage_gaps(
                snapshot_list, fetch_list, cadence_minutes=cadence_minutes, gap_factor=gap_factor
            ).items()
        },
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _gaps_between(
    instants: Iterable[dt.datetime],
    *,
    cadence_minutes: int,
    gap_factor: float,
    facility_uuid: str | None,
) -> list[GapInterval]:
    """Gap intervals between consecutive observation instants."""
    if cadence_minutes <= 0:
        raise ValueError("cadence_minutes must be positive")

    ordered = sorted(instants)
    threshold = dt.timedelta(minutes=cadence_minutes * gap_factor)
    cadence = dt.timedelta(minutes=cadence_minutes)

    gaps: list[GapInterval] = []
    for start, end in pairwise(ordered):
        delta = end - start
        if delta <= threshold:
            continue
        gaps.append(
            GapInterval(
                facility_uuid=facility_uuid,
                start=start,
                end=end,
                minutes=int(delta.total_seconds() // 60),
                missed_polls=int(delta // cadence) - 1,
            )
        )
    return gaps


class _DayAccumulator:
    """Mutable per-(facility, date) tally behind :class:`FacilityDayCoverage`."""

    __slots__ = ("_fetches_failed", "_fetches_ok", "_first", "_last", "_slot_rows", "_snapshots")

    def __init__(self) -> None:
        self._snapshots: set[int] = set()
        self._fetches_ok = 0
        self._fetches_failed = 0
        self._slot_rows = 0
        self._first: dt.datetime | None = None
        self._last: dt.datetime | None = None

    def add(self, fetch: FacilityFetch, observed_at: dt.datetime) -> None:
        self._snapshots.add(fetch.snapshot_id)
        if fetch.ok:
            self._fetches_ok += 1
        else:
            self._fetches_failed += 1
        self._slot_rows += fetch.slot_count
        if self._first is None or observed_at < self._first:
            self._first = observed_at
        if self._last is None or observed_at > self._last:
            self._last = observed_at

    def as_coverage(
        self, *, facility_uuid: str, observed_date: dt.date, snapshots_expected: int
    ) -> FacilityDayCoverage:
        return FacilityDayCoverage(
            facility_uuid=facility_uuid,
            observed_date=observed_date,
            snapshots_expected=snapshots_expected,
            snapshots_received=len(self._snapshots),
            fetches_ok=self._fetches_ok,
            fetches_failed=self._fetches_failed,
            slot_rows=self._slot_rows,
            first_observed_at=self._first,
            last_observed_at=self._last,
        )
