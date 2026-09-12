"""Regression tests for the occupancy and coverage analytics.

Each test names the regression it prevents in its docstring. The expensive
ones run against the recorded 31-day fixtures, so the numbers asserted are the
real published inventory of three real venues; the rest run against the
scripted multi-snapshot history, which is the only way to exercise the
final-state rule on a forward-only dataset.

Nothing here touches the network, the database or the clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from itertools import pairwise
from typing import Any

import pytest

from tests.conftest import (
    BUSINESS_DAY_START_HOUR,
    PADEL_FORT_COURT,
    PADEL_FORT_VENUE,
    PADEL_UP_COURT,
    PADEL_UP_VENUE,
    PLAY_PADEL_COURT,
    PLAY_PADEL_VENUE,
    TZ,
    SyntheticHistory,
)
from tracker.analytics.coverage import (
    coverage_by_facility_day,
    coverage_gaps,
    coverage_report,
    expected_snapshots_per_day,
    facility_coverage_gaps,
    split_on_gaps,
)
from tracker.analytics.occupancy import (
    DEFAULT_SPARSE_MIN_MINUTES,
    VenueDayOccupancy,
    heatmaps_by_venue,
    occupancy_by_venue_day,
    peak_hour_heatmap,
    settled_observations,
    to_court_hours,
    weekday_vs_weekend,
)
from tracker.classify import parse_slot_grid
from tracker.types import (
    FacilityFetch,
    SlotObservation,
    SlotState,
    SnapshotRecord,
    Sport,
    business_date_for,
    duration_minutes_for,
    from_local_text,
    slot_start_utc_for,
)

#: 16:21 IST on 2026-09-11, the instant the fixtures were recorded. Fixed so
#: ``is_past`` is reproducible on any machine at any hour.
FIXTURE_OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 51, tzinfo=dt.UTC)

SYNTHETIC_VENUE = "11111111-1111-1111-1111-111111111111"
SYNTHETIC_COURT = "22222222-2222-2222-2222-222222222222"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def observation(
    *,
    slot_uuid: str,
    start_local: str,
    end_local: str,
    state: SlotState,
    snapshot_id: int = 1,
    venue_uuid: str = SYNTHETIC_VENUE,
    facility_uuid: str = SYNTHETIC_COURT,
    price: float | None = 900.0,
    is_past: bool = False,
    sport: Sport = Sport.PADEL,
) -> SlotObservation:
    """One hand-built observation, with every derivation done the shared way."""
    start = from_local_text(start_local)
    end = from_local_text(end_local)
    return SlotObservation(
        snapshot_id=snapshot_id,
        slot_uuid=slot_uuid,
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        slot_start_local=start_local,
        slot_end_local=end_local,
        tz=TZ,
        slot_start_utc=slot_start_utc_for(start, TZ),
        duration_minutes=duration_minutes_for(start, end),
        price=price,
        total_count=1,
        available_count=0 if state is SlotState.BOOKED else 1,
        is_available=state is not SlotState.BLOCKED,
        is_booked=state is SlotState.BOOKED,
        state=state,
        days_ahead=1,
        business_date=business_date_for(start, BUSINESS_DAY_START_HOUR),
        is_past=is_past,
    )


def parse_fixture(
    payload: Any,
    *,
    venue_uuid: str,
    facility_uuid: str,
    snapshot_id: int = 1,
    sport: Sport = Sport.PADEL,
) -> list[SlotObservation]:
    """Parse a recorded 31-day slot grid at the fixed recording instant."""
    return parse_slot_grid(
        payload,
        snapshot_id=snapshot_id,
        observed_at=FIXTURE_OBSERVED_AT,
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        business_day_start_hour=BUSINESS_DAY_START_HOUR,
    )


def day_row(
    observations: Iterable[SlotObservation], venue_uuid: str, business_date: dt.date
) -> VenueDayOccupancy:
    """The single (venue, business_date) row, asserting it is unique."""
    rows = [
        row
        for row in occupancy_by_venue_day(observations)
        if row.venue_uuid == venue_uuid and row.business_date == business_date
    ]
    assert len(rows) == 1, f"expected exactly one row for {venue_uuid} {business_date}"
    return rows[0]


def snapshots_and_fetches(
    instants: Sequence[dt.datetime],
    *,
    facilities: Sequence[str] = (SYNTHETIC_COURT,),
    failed: Sequence[tuple[int, str]] = (),
) -> tuple[list[SnapshotRecord], list[FacilityFetch]]:
    """A poll history: one snapshot per instant, one fetch per facility.

    ``failed`` names (snapshot_id, facility_uuid) pairs whose fetch did not
    land, so a per-facility hole can be built without a missing snapshot.
    """
    snapshots = [
        SnapshotRecord(
            snapshot_id=index + 1,
            poll_key=f"poll-{index}",
            observed_at=instant,
            ok=True,
            error=None,
            duration_ms=1000,
            horizon_days=21,
        )
        for index, instant in enumerate(instants)
    ]
    fetches = [
        FacilityFetch(
            snapshot_id=snapshot.snapshot_id,
            facility_uuid=facility_uuid,
            ok=(snapshot.snapshot_id, facility_uuid) not in set(failed),
            http_status=200 if (snapshot.snapshot_id, facility_uuid) not in set(failed) else 503,
            error=None,
            duration_ms=300,
            slot_count=36,
            attempts=1,
        )
        for snapshot in snapshots
        for facility_uuid in facilities
    ]
    return snapshots, fetches


# --------------------------------------------------------------------------
# Normalization: the single easiest way to get the dashboard wrong
# --------------------------------------------------------------------------


def test_one_60_minute_booking_equals_two_30_minute_bookings() -> None:
    """THE NORMALIZATION TEST.

    Regression: counting slots instead of summing ``duration_minutes``. One
    booked 60-minute Padel Up slot is exactly as much sold court time as two
    booked 30-minute Padel Fort slots. Anything that counts slots reports
    1 vs 2 and makes every cross-venue chart wrong by 2x.
    """
    sixty = [
        observation(
            slot_uuid="hour-grid",
            start_local="2026-09-14 19:00:00",
            end_local="2026-09-14 20:00:00",
            state=SlotState.BOOKED,
        )
    ]
    halves = [
        observation(
            slot_uuid="half-grid-a",
            start_local="2026-09-15 19:00:00",
            end_local="2026-09-15 19:30:00",
            state=SlotState.BOOKED,
        ),
        observation(
            slot_uuid="half-grid-b",
            start_local="2026-09-15 19:30:00",
            end_local="2026-09-15 20:00:00",
            state=SlotState.BOOKED,
        ),
    ]

    hour_day = day_row(sixty, SYNTHETIC_VENUE, dt.date(2026, 9, 14))
    halves_day = day_row(halves, SYNTHETIC_VENUE, dt.date(2026, 9, 15))

    assert hour_day.booked_minutes == halves_day.booked_minutes == 60
    assert hour_day.booked_court_hours == halves_day.booked_court_hours == 1.0
    # And the slot count is exactly the misleading number this guards against.
    assert hour_day.slots == 1
    assert halves_day.slots == 2


def test_cross_venue_totals_are_only_right_in_court_hours(
    raw_slots_padel_up: Any, raw_slots_padel_fort: Any
) -> None:
    """Regression: ranking venues by slot count.

    Padel Up publishes 589 sixty-minute slots and Padel Fort 1116 thirty-minute
    ones. In slots Fort looks like nearly twice the inventory; in court-hours
    Padel Up is the larger venue (589.0 vs 558.0). A dashboard comparing slots
    gets the ordering backwards.
    """
    up = parse_fixture(raw_slots_padel_up, venue_uuid=PADEL_UP_VENUE, facility_uuid=PADEL_UP_COURT)
    fort = parse_fixture(
        raw_slots_padel_fort, venue_uuid=PADEL_FORT_VENUE, facility_uuid=PADEL_FORT_COURT
    )

    assert len(up) == 589
    assert len(fort) == 1116

    up_hours = sum(row.total_court_hours for row in occupancy_by_venue_day(up))
    fort_hours = sum(row.total_court_hours for row in occupancy_by_venue_day(fort))

    assert up_hours == pytest.approx(589.0)
    assert fort_hours == pytest.approx(558.0)
    assert up_hours > fort_hours
    # The slot count says the opposite, which is the whole point.
    assert len(up) < len(fort)


def test_to_court_hours_is_the_only_conversion() -> None:
    """Regression: a caller hand-dividing by 60 and getting it wrong.

    A 30-minute grid slot is half a court-hour; 2945 court-minutes is not a
    round number of hours and must not be rounded into one here.
    """
    assert to_court_hours(60) == 1.0
    assert to_court_hours(30) == 0.5
    assert to_court_hours(0) == 0.0
    assert to_court_hours(2945) == pytest.approx(49.0833333, abs=1e-6)


# --------------------------------------------------------------------------
# Blocked inventory vs genuine emptiness
# --------------------------------------------------------------------------


def test_all_blocked_day_has_no_strict_occupancy_and_a_full_blocked_share(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: folding blocked minutes into occupancy.

    This exercises the zero-sellable-denominator path with a constructed
    all-blocked day: the scripted history carries only the 14 blocked slots of
    Padel Fort's real 2026-09-13 evening, so the day here has no sellable
    minutes at all and the strict ratio must be ``None`` rather than ``0.0``.

    The real recorded 2026-09-13 is *not* this day -- it has 22 open slots
    beside the 14 blocked ones and reads a genuine strict 0.0 over a 660-minute
    denominator. That case is pinned separately, on the real fixture, by
    :func:`test_real_blocked_evening_day_reads_zero_not_none` below and by
    ``test_blocked_evening_does_not_hide_inside_the_whole_day`` in
    ``tests/test_classify.py``.
    """
    row = day_row(
        synthetic_history.observations,
        PADEL_FORT_VENUE,
        synthetic_history.blocked_evening_business_date,
    )

    assert row.blocked_minutes == synthetic_history.expected_blocked_evening_minutes == 420
    assert row.blocked_court_hours == 7.0
    assert row.sellable_minutes == 0
    assert row.occupancy_strict is None
    assert row.occupancy_gross == 1.0
    assert row.blocked_share == 1.0

    assert row.slots == 14, "the constructed day holds only the blocked run"


def test_real_blocked_evening_day_reads_zero_not_none(
    raw_slots_padel_fort: Any,
) -> None:
    """Regression: reading the constructed all-blocked day as the real one.

    On the real recorded 2026-09-13 Padel Fort published 36 slots: 22 OPEN
    from 06:00 and the 14 BLOCKED ones from 17:00. That day has a real
    660-minute sellable denominator, so ``occupancy_strict`` is a genuine
    ``0.0`` -- nobody booked -- and ``None`` here would mean "nothing was
    sellable", which is a different and false claim. The two must not converge.
    """
    observations = parse_fixture(
        raw_slots_padel_fort, venue_uuid=PADEL_FORT_VENUE, facility_uuid=PADEL_FORT_COURT
    )
    row = day_row(observations, PADEL_FORT_VENUE, dt.date(2026, 9, 13))

    assert row.slots == 36
    assert row.blocked_minutes == 420
    assert row.open_minutes == 660
    assert row.booked_minutes == 0
    assert row.occupancy_strict == 0.0
    assert row.occupancy_gross == pytest.approx(420 / 1080)


def test_zero_denominator_is_none_not_zero_and_stays_distinguishable(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: reporting ``0.0`` for "nothing was sellable".

    The blocked-out Fort evening and the Padel Up day that published a full
    open hour and sold none of it are different facts. ``None`` vs ``0.0`` is
    the only thing that keeps them apart, and ``None`` must not compare equal
    to zero anywhere in the chain.
    """
    blocked = day_row(synthetic_history.observations, PADEL_FORT_VENUE, dt.date(2026, 9, 13))
    genuinely_empty = day_row(
        synthetic_history.observations,
        PADEL_UP_VENUE,
        synthetic_history.normalization_business_date,
    )

    assert blocked.occupancy_strict is None
    assert genuinely_empty.occupancy_strict == 0.0
    assert genuinely_empty.sellable_minutes == 60
    assert blocked.occupancy_strict != genuinely_empty.occupancy_strict
    assert blocked.to_dict()["occupancy_strict"] is None
    assert genuinely_empty.to_dict()["occupancy_strict"] == 0.0


def test_padel_up_real_fixture_is_a_genuine_zero_not_a_broken_collector(
    raw_slots_padel_up: Any,
) -> None:
    """Regression: a real 0% venue rendered as missing data.

    Padel Up showed zero bookings across 31 days and 589 slots at the highest
    price in the city, with exactly one blocked slot a day (its 05:00 opener).
    Because it has open inventory, ``occupancy_strict`` must be exactly 0.0 --
    a measured zero, not ``None`` -- or the venue looks like a collector fault
    forever.
    """
    observations = parse_fixture(
        raw_slots_padel_up, venue_uuid=PADEL_UP_VENUE, facility_uuid=PADEL_UP_COURT
    )
    rows = occupancy_by_venue_day(observations)

    total_booked = sum(row.booked_minutes for row in rows)
    total_open = sum(row.open_minutes for row in rows)
    total_blocked = sum(row.blocked_minutes for row in rows)

    assert total_booked == 0
    assert total_open == 558 * 60
    assert total_blocked == 31 * 60  # the 05:00 venue rule, every single day
    assert all(row.occupancy_strict == 0.0 for row in rows)
    assert all(row.occupancy_strict is not None for row in rows)
    # One blocked hour out of nineteen published: visible, not folded away.
    assert rows[0].blocked_share == pytest.approx(1 / 19)


def test_blocked_minutes_never_leave_the_strict_numerator_or_denominator() -> None:
    """Regression: treating a blocked slot as sold, or as open inventory.

    A blocked slot is neither. It must raise ``occupancy_gross`` and
    ``blocked_share`` while leaving ``occupancy_strict`` exactly where the
    booked and open minutes put it.
    """
    rows = [
        observation(
            slot_uuid="sold",
            start_local="2026-09-14 19:00:00",
            end_local="2026-09-14 19:30:00",
            state=SlotState.BOOKED,
        ),
        observation(
            slot_uuid="open",
            start_local="2026-09-14 19:30:00",
            end_local="2026-09-14 20:00:00",
            state=SlotState.OPEN,
        ),
        observation(
            slot_uuid="pulled",
            start_local="2026-09-14 20:00:00",
            end_local="2026-09-14 20:30:00",
            state=SlotState.BLOCKED,
        ),
    ]

    row = day_row(rows, SYNTHETIC_VENUE, dt.date(2026, 9, 14))

    assert (row.booked_minutes, row.open_minutes, row.blocked_minutes) == (30, 30, 30)
    assert row.sellable_minutes == 60
    assert row.total_minutes == 90
    assert row.occupancy_strict == 0.5
    assert row.occupancy_gross == pytest.approx(2 / 3)
    assert row.blocked_share == pytest.approx(1 / 3)


# --------------------------------------------------------------------------
# The final-state rule
# --------------------------------------------------------------------------


def test_a_slot_seen_open_then_booked_counts_once_as_booked(
    synthetic_history: SyntheticHistory,
) -> None:
    """THE FINAL-STATE RULE.

    Regression: double-counting a slot once per poll, or averaging its state
    across snapshots. The Fort 2026-09-14 19:00 slot is OPEN in six snapshots
    and BOOKED in four. It must contribute 30 booked court-minutes -- once --
    and no open minutes at all. Summing the raw stream would report 300
    court-minutes on a 30-minute slot; averaging would report 40% booked.
    """
    trajectory = synthetic_history.trajectory(synthetic_history.normal_slot_uuid)
    assert trajectory.count(SlotState.OPEN) == 6
    assert trajectory.count(SlotState.BOOKED) == 4

    settled = [
        o
        for o in settled_observations(synthetic_history.observations)
        if o.slot_uuid == synthetic_history.normal_slot_uuid
    ]
    assert len(settled) == 1
    assert settled[0].state is SlotState.BOOKED

    row = day_row(synthetic_history.observations, PADEL_FORT_VENUE, dt.date(2026, 9, 14))
    assert row.slots == 1
    assert row.booked_minutes == 30
    assert row.open_minutes == 0
    assert row.occupancy_strict == 1.0


def test_a_cancelled_slot_settles_back_to_open(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: keeping the first BOOKED sighting as the day's outcome.

    The Fort 2026-09-15 20:00 slot goes OPEN -> BOOKED -> OPEN. At day end it
    was unsold, and the occupancy row must say so. On the same date a
    re-booked slot settles BOOKED and a left-censored slot stays BOOKED, so
    the day is 60 booked over 90 sellable court-minutes.
    """
    settled = {o.slot_uuid: o.state for o in settled_observations(synthetic_history.observations)}

    assert settled[synthetic_history.cancellation_slot_uuid] is SlotState.OPEN
    assert settled[synthetic_history.rebooked_slot_uuid] is SlotState.BOOKED

    row = day_row(synthetic_history.observations, PADEL_FORT_VENUE, dt.date(2026, 9, 15))
    assert (row.booked_minutes, row.open_minutes, row.blocked_minutes) == (60, 30, 0)
    assert row.occupancy_strict == pytest.approx(2 / 3)


def test_a_slot_pulled_from_inventory_settles_blocked(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: an OPEN -> BLOCKED slot still counted as open inventory.

    The Fort 2026-09-17 19:00 slot is open for four polls and then blocked,
    most likely an offline sale. Its settled state is BLOCKED, so the day has
    no sellable minutes at all and ``occupancy_strict`` is ``None``.
    """
    row = day_row(synthetic_history.observations, PADEL_FORT_VENUE, dt.date(2026, 9, 17))

    assert row.blocked_minutes == 30
    assert row.sellable_minutes == 0
    assert row.occupancy_strict is None
    assert row.blocked_share == 1.0


def test_an_already_elapsed_open_slot_stays_in_the_denominator(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: dropping slots that had already elapsed when first seen.

    The Fort 2026-09-11 07:00 slot is ``is_past`` in all ten snapshots and
    OPEN in all ten -- Hudle never marks elapsed slots unavailable. It really
    was sellable inventory that went unsold, so it belongs in a retrospective
    denominator; dropping it (or reading it as BLOCKED) under-counts the day.
    """
    settled = [
        o
        for o in settled_observations(synthetic_history.observations)
        if o.slot_uuid == synthetic_history.elapsed_open_slot_uuid
    ]
    assert len(settled) == 1
    assert settled[0].state is SlotState.OPEN
    assert settled[0].is_past is True

    row = day_row(synthetic_history.observations, PADEL_FORT_VENUE, dt.date(2026, 9, 11))
    assert row.open_minutes == 30
    assert row.occupancy_strict == 0.0


def test_settled_reduction_is_idempotent(synthetic_history: SyntheticHistory) -> None:
    """Regression: a second reduction pass changing the answer.

    Analytics entry points reduce internally, so a caller who reduces first
    must get identical numbers. Re-applying the reduction to settled rows has
    to be a no-op.
    """
    once = settled_observations(synthetic_history.observations)
    twice = settled_observations(once)

    assert once == twice
    assert len(once) == len({o.slot_uuid for o in synthetic_history.observations})
    assert occupancy_by_venue_day(once) == occupancy_by_venue_day(synthetic_history.observations)


def test_later_polls_of_an_elapsed_slot_do_not_overwrite_its_settled_state() -> None:
    """Regression: letting a poll taken after the slot started win.

    A slot observed BOOKED just before it starts and then reported OPEN by a
    later poll (a post-hoc cancellation, or Hudle re-publishing the row) must
    keep the state it settled in at kickoff. The pre-start observation always
    beats the elapsed one.
    """
    rows = [
        observation(
            slot_uuid="settled",
            start_local="2026-09-14 19:00:00",
            end_local="2026-09-14 19:30:00",
            state=SlotState.BOOKED,
            snapshot_id=1,
            is_past=False,
        ),
        observation(
            slot_uuid="settled",
            start_local="2026-09-14 19:00:00",
            end_local="2026-09-14 19:30:00",
            state=SlotState.OPEN,
            snapshot_id=2,
            is_past=True,
        ),
    ]

    settled = settled_observations(rows)

    assert len(settled) == 1
    assert settled[0].state is SlotState.BOOKED
    assert settled[0].snapshot_id == 1


# --------------------------------------------------------------------------
# Day-of-week attribution and the heatmap
# --------------------------------------------------------------------------


def test_a_post_midnight_saturday_booking_is_friday_demand(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: day-of-week taken from the wall-clock date.

    Play Padel sells 00:00-01:30 and Hudle stamps those slots with the
    calendar date they fall on, so a Friday-night session lands on Saturday.
    The 00:30 slot on Saturday 2026-09-12 must colour the Friday column (
    ``weekday() == 4``) at hour 0, and must leave Saturday hour 0 empty.
    """
    assert synthetic_history.post_midnight_local_date == dt.date(2026, 9, 12)
    assert synthetic_history.post_midnight_business_date == dt.date(2026, 9, 11)

    heatmap = peak_hour_heatmap(synthetic_history.observations, venue_uuid=PLAY_PADEL_VENUE)

    friday_midnight = heatmap.cell(4, 0)
    assert friday_midnight is not None
    assert friday_midnight.day_name == "Fri"
    assert friday_midnight.booked_minutes == 30
    assert friday_midnight.occupancy_strict == 1.0

    assert heatmap.cell(5, 0) is None  # Saturday never sees this demand

    row = day_row(synthetic_history.observations, PLAY_PADEL_VENUE, dt.date(2026, 9, 11))
    assert row.booked_minutes == 60  # the 00:30 and 01:00 sessions together
    assert not [
        r
        for r in occupancy_by_venue_day(synthetic_history.observations)
        if r.venue_uuid == PLAY_PADEL_VENUE and r.business_date == dt.date(2026, 9, 12)
    ]


def test_a_thin_heatmap_cell_is_flagged_sparse_not_reported_confidently() -> None:
    """Regression: colouring a cell built from one slot like a measurement.

    A 19:00 Monday cell with eight weeks of 30-minute inventory behind it is a
    real signal; a 06:00 cell with one slot is not, even though both produce a
    ratio. The thin cell keeps its ratio and denominator but must be flagged
    so the dashboard can grey it.
    """
    mondays = [dt.date(2026, 9, 14) + dt.timedelta(days=7 * week) for week in range(4)]
    rows = [
        observation(
            slot_uuid=f"evening-{index}",
            start_local=f"{monday.isoformat()} 19:00:00",
            end_local=f"{monday.isoformat()} 19:30:00",
            state=SlotState.BOOKED if index % 2 == 0 else SlotState.OPEN,
        )
        for index, monday in enumerate(mondays)
    ] + [
        observation(
            slot_uuid="lone-morning",
            start_local="2026-09-14 06:00:00",
            end_local="2026-09-14 06:30:00",
            state=SlotState.BOOKED,
        )
    ]

    heatmap = peak_hour_heatmap(rows, sparse_min_minutes=60)

    evening = heatmap.cell(0, 19)
    morning = heatmap.cell(0, 6)
    assert evening is not None
    assert morning is not None

    assert evening.sellable_minutes == 120
    assert evening.business_dates == 4
    assert evening.sparse is False
    assert evening.occupancy_strict == 0.5

    assert morning.sellable_minutes == 30
    assert morning.business_dates == 1
    assert morning.sparse is True
    assert morning.occupancy_strict == 1.0  # a confident-looking 100% over 30 minutes
    assert heatmap.dense_cells == (evening,)
    assert DEFAULT_SPARSE_MIN_MINUTES == 120


def test_heatmaps_are_built_per_venue_and_combined(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: one combined grid hiding a venue's own selling window.

    The combined heatmap must total court-minutes across venues, while the
    per-venue grids keep each venue's window separate -- only Play Padel sells
    at 00:30, and only Padel Up publishes a 60-minute slot at 19:00.
    """
    combined = peak_hour_heatmap(synthetic_history.observations)
    per_venue = heatmaps_by_venue(synthetic_history.observations)

    assert combined.venue_uuid is None
    assert set(per_venue) == {PADEL_UP_VENUE, PLAY_PADEL_VENUE, PADEL_FORT_VENUE}

    up_wednesday = per_venue[PADEL_UP_VENUE].cell(2, 19)
    fort_wednesday = per_venue[PADEL_FORT_VENUE].cell(2, 19)
    assert up_wednesday is not None
    assert fort_wednesday is not None
    assert (up_wednesday.open_minutes, fort_wednesday.open_minutes) == (60, 30)

    combined_wednesday = combined.cell(2, 19)
    assert combined_wednesday is not None
    assert combined_wednesday.open_minutes == 90
    assert per_venue[PADEL_UP_VENUE].cell(4, 0) is None
    assert per_venue[PLAY_PADEL_VENUE].cell(4, 0) is not None


def test_heatmap_hours_come_from_the_real_selling_windows(
    raw_slots_play_padel: Any,
) -> None:
    """Regression: assuming a contiguous selling window.

    Play Padel sells 00:00-01:30 *and* 06:00-23:30, with nothing between. The
    heatmap must have cells for hours 0 and 1 and for 6 onwards, and none at
    all for 2-5; inventing empty cells across the hole would show a structural
    closure as unsold demand.
    """
    observations = parse_fixture(
        raw_slots_play_padel, venue_uuid=PLAY_PADEL_VENUE, facility_uuid=PLAY_PADEL_COURT
    )
    heatmap = peak_hour_heatmap(observations)

    assert set(heatmap.hours) == {0, 1, *range(6, 24)}
    assert not {2, 3, 4, 5} & set(heatmap.hours)
    assert set(heatmap.days_of_week) == set(range(7))


# --------------------------------------------------------------------------
# Weekday vs weekend
# --------------------------------------------------------------------------


def test_weekday_vs_weekend_splits_on_business_date_in_court_hours(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: comparing raw totals, or splitting on the wall-clock date.

    The two post-midnight Play Padel bookings are stamped Saturday by Hudle
    but belong to Friday, so they must land on the weekday side. And because
    five weekdays face two weekend days, the comparable figure is court-hours
    per business date, not the raw total.
    """
    split = weekday_vs_weekend(synthetic_history.observations)

    assert split.weekday.label == "weekday"
    assert split.weekend.label == "weekend"

    # Friday 09-11, Monday 09-14, Tuesday 09-15, Wednesday 09-16, Thursday 09-17.
    assert split.weekday.business_dates == 5
    # Sunday 09-13 only: the blocked evening.
    assert split.weekend.business_dates == 1

    assert split.weekday.booked_court_hours == pytest.approx(2.5)
    assert split.weekend.booked_court_hours == 0.0
    assert split.weekend.blocked_minutes == 420
    assert split.weekday.booked_court_hours_per_day == pytest.approx(0.5)
    assert split.weekend.booked_court_hours_per_day == 0.0

    total_booked = split.weekday.booked_minutes + split.weekend.booked_minutes
    assert total_booked == sum(
        o.duration_minutes
        for o in settled_observations(synthetic_history.observations)
        if o.state is SlotState.BOOKED
    )


def test_weekday_vs_weekend_with_no_weekend_days_reports_none_per_day() -> None:
    """Regression: dividing by a zero day count.

    A dataset that has not reached a weekend yet must report ``None`` for the
    weekend per-day rate, never ``0.0``, which would read as "the weekend sold
    nothing".
    """
    rows = [
        observation(
            slot_uuid="monday",
            start_local="2026-09-14 19:00:00",
            end_local="2026-09-14 19:30:00",
            state=SlotState.BOOKED,
        )
    ]

    split = weekday_vs_weekend(rows)

    assert split.weekend.business_dates == 0
    assert split.weekend.booked_court_hours_per_day is None
    assert split.weekend.occupancy_strict is None
    assert split.weekday.booked_court_hours_per_day == 0.5
    assert split.weekend_uplift is None


# --------------------------------------------------------------------------
# Filters and grouping
# --------------------------------------------------------------------------


def test_venue_day_filters_narrow_rows_without_changing_arithmetic(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a date filter applied before the settled reduction.

    Filtering must select whole (venue, business_date) rows and leave each
    row's numbers identical to the unfiltered run. A filter that dropped
    observations before the reduction could promote an earlier poll to
    "settled" and silently change a day's occupancy.
    """
    everything = occupancy_by_venue_day(synthetic_history.observations)
    fort_only = occupancy_by_venue_day(synthetic_history.observations, venue_uuid=PADEL_FORT_VENUE)
    windowed = occupancy_by_venue_day(
        synthetic_history.observations,
        business_date_from=dt.date(2026, 9, 14),
        business_date_to=dt.date(2026, 9, 15),
    )

    assert {row.venue_uuid for row in fort_only} == {PADEL_FORT_VENUE}
    assert fort_only == [row for row in everything if row.venue_uuid == PADEL_FORT_VENUE]
    assert {row.business_date for row in windowed} == {
        dt.date(2026, 9, 14),
        dt.date(2026, 9, 15),
    }
    assert windowed == [
        row
        for row in everything
        if dt.date(2026, 9, 14) <= row.business_date <= dt.date(2026, 9, 15)
    ]
    assert occupancy_by_venue_day([]) == []


def test_venue_days_are_sorted_and_carry_every_denominator(
    raw_slots_padel_fort: Any,
) -> None:
    """Regression: a chart forced to recompute -- or redefine -- a denominator.

    Every row must expose booked, open, blocked, sellable and total minutes
    alongside the three ratios, and the arithmetic must tie out on real data.
    """
    observations = parse_fixture(
        raw_slots_padel_fort, venue_uuid=PADEL_FORT_VENUE, facility_uuid=PADEL_FORT_COURT
    )
    rows = occupancy_by_venue_day(observations)

    assert rows == sorted(rows, key=lambda r: (r.venue_uuid, r.business_date))
    for row in rows:
        assert row.total_minutes == (row.booked_minutes + row.open_minutes + row.blocked_minutes)
        assert row.sellable_minutes == row.booked_minutes + row.open_minutes
        if row.sellable_minutes:
            assert row.occupancy_strict == row.booked_minutes / row.sellable_minutes
        assert row.total_court_hours == to_court_hours(row.total_minutes)

    # The real 2026-09-13 evening: 14 blocked slots, seven court-hours, no sales.
    blocked_day = next(row for row in rows if row.business_date == dt.date(2026, 9, 13))
    assert blocked_day.blocked_minutes == 420
    assert blocked_day.booked_minutes == 0


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def test_coverage_finds_the_ninety_minute_hole_as_an_explicit_interval(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a missed poll smoothed over instead of drawn.

    The scripted history skips ticks 8 and 9, leaving a 90-minute hole between
    13:30Z and 15:00Z at a 30-minute cadence. Coverage must return that hole
    as one interval with its real endpoints and two missed polls, so the
    dashboard can draw the hole rather than interpolate across it.
    """
    gaps = coverage_gaps(
        synthetic_history.snapshots, cadence_minutes=synthetic_history.cadence_minutes
    )

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.start == synthetic_history.gap_start
    assert gap.end == synthetic_history.gap_end
    assert gap.minutes == synthetic_history.gap_minutes == 90
    assert gap.missed_polls == synthetic_history.missing_snapshot_count == 2
    assert gap.facility_uuid is None
    assert gap.to_dict()["start"] == "2026-09-11T13:30:00Z"
    assert gap.to_dict()["end"] == "2026-09-11T15:00:00Z"


def test_a_series_is_split_at_the_gap_and_never_interpolated(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a line chart joining the last point before a gap to the
    first point after it.

    That line asserts observations nobody made, over an interval that can
    never be re-collected. The series must come back as two segments, with no
    single segment spanning 13:30Z to 15:00Z.
    """
    gaps = coverage_gaps(
        synthetic_history.snapshots, cadence_minutes=synthetic_history.cadence_minutes
    )
    points = [
        (snapshot.observed_at, snapshot.snapshot_id)
        for snapshot in reversed(synthetic_history.snapshots)
    ]

    segments = split_on_gaps(points, gaps)

    assert [len(segment) for segment in segments] == [8, 2]
    assert segments[0][-1][0] == synthetic_history.gap_start
    assert segments[1][0][0] == synthetic_history.gap_end
    for segment in segments:
        spans = [
            (later[0] - earlier[0]).total_seconds() / 60 for earlier, later in pairwise(segment)
        ]
        assert all(span == synthetic_history.cadence_minutes for span in spans)
    assert split_on_gaps(points, []) == [sorted(points, key=lambda p: p[0])]
    assert split_on_gaps([], gaps) == []


def test_jitter_within_half_a_cadence_is_not_reported_as_a_gap() -> None:
    """Regression: flagging every late poll as data loss.

    A scheduler that fires at 30.0 and 30.7 minutes has not missed anything.
    Only a space wide enough to contain a whole skipped poll is a gap.
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    jittery = [base, base + dt.timedelta(minutes=31), base + dt.timedelta(minutes=74)]
    snapshots, _ = snapshots_and_fetches(jittery)

    assert coverage_gaps(snapshots, cadence_minutes=30) == []

    dropped = [base, base + dt.timedelta(minutes=90)]
    snapshots, _ = snapshots_and_fetches(dropped)
    assert [gap.missed_polls for gap in coverage_gaps(snapshots, cadence_minutes=30)] == [2]


def test_a_failed_poll_leaves_a_gap_even_though_the_snapshot_row_exists() -> None:
    """Regression: counting a failed collect run as coverage.

    A snapshot row with ``ok=False`` produced no observations, so the time it
    covers is unobserved and must appear as a gap like any missed poll.
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    instants = [base + dt.timedelta(minutes=30 * i) for i in range(4)]
    snapshots, _ = snapshots_and_fetches(instants)
    snapshots[1] = SnapshotRecord(
        snapshot_id=snapshots[1].snapshot_id,
        poll_key=snapshots[1].poll_key,
        observed_at=snapshots[1].observed_at,
        ok=False,
        error="timeout",
        duration_ms=9000,
        horizon_days=21,
    )

    gaps = coverage_gaps(snapshots, cadence_minutes=30)

    assert len(gaps) == 1
    assert gaps[0].start == instants[0]
    assert gaps[0].end == instants[2]
    assert gaps[0].missed_polls == 1


def test_a_failed_facility_fetch_is_a_hole_for_that_facility_only() -> None:
    """Regression: one venue's fetch failure reported as a global outage, or
    as no outage at all.

    The poll ran and the other venue's data landed, so there is no schedule
    gap. The venue whose fetch 503'd still has an hour of unobserved time and
    its own series must be split there.
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    instants = [base + dt.timedelta(minutes=30 * i) for i in range(4)]
    other_court = "33333333-3333-3333-3333-333333333333"
    snapshots, fetches = snapshots_and_fetches(
        instants,
        facilities=(SYNTHETIC_COURT, other_court),
        failed=((2, SYNTHETIC_COURT), (3, SYNTHETIC_COURT)),
    )

    assert coverage_gaps(snapshots, cadence_minutes=30) == []

    per_facility = facility_coverage_gaps(snapshots, fetches, cadence_minutes=30)

    assert per_facility[other_court] == []
    assert len(per_facility[SYNTHETIC_COURT]) == 1
    gap = per_facility[SYNTHETIC_COURT][0]
    assert gap.facility_uuid == SYNTHETIC_COURT
    assert (gap.start, gap.end) == (instants[0], instants[3])
    assert gap.minutes == 90
    assert gap.missed_polls == 2


def test_daily_coverage_counts_expected_against_received_per_facility(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a missing poll invisible in the daily numbers.

    At a 30-minute cadence 48 polls are expected a day. The scripted history
    delivered 10 on 2026-09-11 for each of the three courts, so coverage must
    read 10/48 -- not 100% of what happened to arrive -- and the expectation
    must come from the cadence, not from the data.
    """
    assert expected_snapshots_per_day(30) == 48
    assert expected_snapshots_per_day(15) == 96

    days = coverage_by_facility_day(
        synthetic_history.snapshots,
        synthetic_history.facility_fetches,
        cadence_minutes=synthetic_history.cadence_minutes,
    )

    assert {day.facility_uuid for day in days} == {
        PADEL_UP_COURT,
        PLAY_PADEL_COURT,
        PADEL_FORT_COURT,
    }
    assert {day.observed_date for day in days} == {dt.date(2026, 9, 11)}
    for day in days:
        assert day.snapshots_expected == 48
        assert day.snapshots_received == synthetic_history.snapshot_count == 10
        assert day.fetches_ok == 10
        assert day.fetches_failed == 0
        assert day.coverage_ratio == pytest.approx(10 / 48)
        assert day.snapshots_missing == 38
        assert day.is_complete is False
        assert day.first_observed_at == synthetic_history.base_observed_at
        assert day.last_observed_at == synthetic_history.observed_at(-1)


def test_daily_coverage_separates_failed_fetches_from_missing_polls() -> None:
    """Regression: a fetch that ran and failed counted as coverage, or as a
    missing poll.

    They are different operational facts and the operator needs both: the
    snapshot arrived (so the collector is alive) but the data did not (so the
    venue or the network is at fault).
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    instants = [base + dt.timedelta(minutes=30 * i) for i in range(4)]
    snapshots, fetches = snapshots_and_fetches(instants, failed=((2, SYNTHETIC_COURT),))

    day = coverage_by_facility_day(
        snapshots, fetches, cadence_minutes=30, snapshots_expected_per_day=4
    )[0]

    assert day.snapshots_received == 4
    assert day.fetches_ok == 3
    assert day.fetches_failed == 1
    assert day.coverage_ratio == 1.0
    assert day.snapshots_missing == 0
    assert day.is_complete is False
    assert day.slot_rows == 4 * 36


def test_coverage_report_bundles_days_and_gaps_from_generators(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: the first computation consuming a generator the next needs.

    ``Storage`` hands back iterators, so the report must materialize its
    inputs once. It must also expose the poll gap on every facility's series:
    a poll that never ran is a hole for all of them.
    """
    report = coverage_report(
        iter(synthetic_history.snapshots),
        iter(synthetic_history.facility_fetches),
        cadence_minutes=synthetic_history.cadence_minutes,
    )

    assert report.snapshots_expected_per_day == 48
    assert len(report.days) == 3
    assert report.has_gaps is True
    assert report.missed_polls == 2
    assert len(report.poll_gaps) == 1
    for court in (PADEL_UP_COURT, PLAY_PADEL_COURT, PADEL_FORT_COURT):
        assert [gap.minutes for gap in report.gaps_for(court)] == [90]
        assert len(report.days_for(court)) == 1
    assert report.gaps_for("unknown-facility") == ()
    assert report.to_dict()["poll_gaps"][0]["missed_polls"] == 2


def test_coverage_rejects_a_nonsense_cadence() -> None:
    """Regression: a zero or negative cadence silently producing a division by
    zero, or an infinite expectation.
    """
    with pytest.raises(ValueError):
        expected_snapshots_per_day(0)
    with pytest.raises(ValueError):
        coverage_gaps([], cadence_minutes=0)


def test_coverage_reports_nothing_outside_the_observed_range() -> None:
    """Regression: a collector that started today reported as 99% missing
    history.

    Gaps are only claimed between two real observations. Before the first poll
    and after the last there is no evidence about what should have happened.
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    snapshots, _ = snapshots_and_fetches([base, base + dt.timedelta(minutes=30)])

    assert coverage_gaps(snapshots, cadence_minutes=30) == []
    assert coverage_gaps([], cadence_minutes=30) == []
    assert coverage_gaps(snapshots[:1], cadence_minutes=30) == []


# --------------------------------------------------------------------------
# Real fixture totals
# --------------------------------------------------------------------------


def test_the_three_real_venues_are_only_comparable_in_court_hours(
    raw_slots_padel_up: Any, raw_slots_play_padel: Any, raw_slots_padel_fort: Any
) -> None:
    """Regression: the whole dashboard headline computed in slots.

    All three recorded grids at once, with the verified state counts. Play
    Padel sold 21 thirty-minute slots (10.5 court-hours) and Padel Fort 6
    (3.0); Padel Up sold none. In court-minutes those totals are comparable,
    and the strict occupancy of each venue is a number a chart can print with
    its own denominator beside it.
    """
    observations = [
        *parse_fixture(raw_slots_padel_up, venue_uuid=PADEL_UP_VENUE, facility_uuid=PADEL_UP_COURT),
        *parse_fixture(
            raw_slots_play_padel,
            venue_uuid=PLAY_PADEL_VENUE,
            facility_uuid=PLAY_PADEL_COURT,
        ),
        *parse_fixture(
            raw_slots_padel_fort,
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
        ),
    ]
    rows = occupancy_by_venue_day(observations)

    booked_hours = {
        venue: sum(row.booked_court_hours for row in rows if row.venue_uuid == venue)
        for venue in (PADEL_UP_VENUE, PLAY_PADEL_VENUE, PADEL_FORT_VENUE)
    }
    blocked_hours = {
        venue: sum(row.blocked_court_hours for row in rows if row.venue_uuid == venue)
        for venue in (PADEL_UP_VENUE, PLAY_PADEL_VENUE, PADEL_FORT_VENUE)
    }

    assert booked_hours[PADEL_UP_VENUE] == 0.0
    assert booked_hours[PLAY_PADEL_VENUE] == pytest.approx(10.5)  # 21 x 30 minutes
    assert booked_hours[PADEL_FORT_VENUE] == pytest.approx(3.0)  # 6 x 30 minutes
    assert blocked_hours[PADEL_UP_VENUE] == pytest.approx(31.0)  # the 05:00 rule
    assert blocked_hours[PLAY_PADEL_VENUE] == 0.0
    assert blocked_hours[PADEL_FORT_VENUE] == pytest.approx(9.0)  # 18 x 30 minutes

    # Padel Up sells the fewest court-hours while publishing the most.
    assert booked_hours[PADEL_UP_VENUE] < booked_hours[PADEL_FORT_VENUE]
