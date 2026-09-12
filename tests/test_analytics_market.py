"""Regression tests for the pricing and market analytics.

Every test below names the specific way the dashboard could lie if the code
regressed. The two that matter most are the price-normalization test (comparing
per-slot prices inverts the ranking of who is expensive) and the market-share
honesty test (Padel Up's zero bookings must annotate, not imply a verdict).

No network, no database, no ``datetime.now()``. The real recorded fixtures are
parsed at a fixed observation instant; everything else is hand-built.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping, Sequence
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
    PRICE_PADEL_FORT_SLOT,
    PRICE_PADEL_UP_SLOT,
    PRICE_PLAY_PADEL_SLOT,
    TZ,
    SyntheticHistory,
)
from tracker.analytics.market import (
    DataQualityFlag,
    MetricReport,
    _hours,
    blocked_inventory,
    demand_by_hour,
    demand_heatmap,
    market_share,
    metric_registry,
    peak_pricing_opportunity,
    revenue_proxy,
    week_start_for,
)
from tracker.analytics.occupancy import (
    occupancy_by_venue_day,
    settled_observations,
    to_court_hours,
)
from tracker.analytics.pricing import (
    price_by_hour_table,
    price_per_court_hour,
    price_rank,
    price_timeline,
)
from tracker.classify import parse_slot_grid
from tracker.types import (
    SlotObservation,
    SlotState,
    SnapshotRecord,
    Sport,
    business_date_for,
    days_ahead_for,
    duration_minutes_for,
    from_local_text,
    local_hour_of,
    slot_start_hour,
    slot_start_utc_for,
)

#: 16:21 IST on 2026-09-11 -- the instant the fixtures were recorded. A literal,
#: so the suite is reproducible at any hour on any machine.
FIXTURE_OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 51, tzinfo=dt.UTC)

MONDAY = dt.date(2026, 9, 14)
FRIDAY = dt.date(2026, 9, 11)
SATURDAY = dt.date(2026, 9, 12)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _observation(
    *,
    slot_uuid: str,
    venue_uuid: str,
    facility_uuid: str,
    start_local: str,
    duration_minutes: int,
    price: float | None,
    state: SlotState,
    snapshot_id: int = 1,
    observed_at: dt.datetime = FIXTURE_OBSERVED_AT,
    sport: Sport = Sport.PADEL,
) -> SlotObservation:
    """One hand-built observation, derived exactly as the collector derives one."""
    start = from_local_text(start_local)
    end = start + dt.timedelta(minutes=duration_minutes)
    end_local = end.strftime("%Y-%m-%d %H:%M:%S")
    slot_start_utc = slot_start_utc_for(start, TZ)
    return SlotObservation(
        snapshot_id=snapshot_id,
        slot_uuid=slot_uuid,
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        slot_start_local=start_local,
        slot_end_local=end_local,
        tz=TZ,
        slot_start_utc=slot_start_utc,
        duration_minutes=duration_minutes_for(start, end),
        price=price,
        total_count=1,
        available_count=0 if state is SlotState.BOOKED else 1,
        is_available=state is not SlotState.BLOCKED,
        is_booked=state is SlotState.BOOKED,
        state=state,
        days_ahead=days_ahead_for(start, observed_at, TZ),
        business_date=business_date_for(start, BUSINESS_DAY_START_HOUR),
        is_past=slot_start_utc < observed_at,
    )


def _fort(
    slot_uuid: str, start_local: str, state: SlotState, *, snapshot_id: int = 1
) -> SlotObservation:
    """A Padel Fort 30-minute slot at the verified 900 per slot (1800 per hour)."""
    return _observation(
        slot_uuid=slot_uuid,
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        start_local=start_local,
        duration_minutes=30,
        price=PRICE_PADEL_FORT_SLOT,
        state=state,
        snapshot_id=snapshot_id,
    )


def _snapshot(snapshot_id: int, observed_at: dt.datetime) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=snapshot_id,
        poll_key=observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        observed_at=observed_at,
        ok=True,
        error=None,
        duration_ms=900,
        horizon_days=21,
    )


@pytest.fixture(scope="module")
def fixture_observations(
    raw_slots_padel_up: Mapping[str, Any],
    raw_slots_play_padel: Mapping[str, Any],
    raw_slots_padel_fort: Mapping[str, Any],
) -> list[SlotObservation]:
    """All 2945 real recorded slots, parsed at the instant they were recorded."""
    observations: list[SlotObservation] = []
    for payload, venue_uuid, facility_uuid in (
        (raw_slots_padel_up, PADEL_UP_VENUE, PADEL_UP_COURT),
        (raw_slots_play_padel, PLAY_PADEL_VENUE, PLAY_PADEL_COURT),
        (raw_slots_padel_fort, PADEL_FORT_VENUE, PADEL_FORT_COURT),
    ):
        observations.extend(
            parse_slot_grid(
                payload,
                snapshot_id=1,
                observed_at=FIXTURE_OBSERVED_AT,
                venue_uuid=venue_uuid,
                facility_uuid=facility_uuid,
                sport=Sport.PADEL,
                business_day_start_hour=BUSINESS_DAY_START_HOUR,
                tz=TZ,
            )
        )
    return observations


# --------------------------------------------------------------------------
# THE PRICE NORMALIZATION TEST
# --------------------------------------------------------------------------


def test_play_padel_is_the_most_expensive_per_court_hour_and_per_slot_inverts_it(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: ranking venues on per-slot price inverts who is expensive.

    Play Padel charges 1000 per 30-minute slot and Padel Fort charges 900, so a
    per-slot ranking calls Play Padel cheaper. Per court-hour Play Padel is
    2000 against 1800 for both others -- the most expensive court in the city.
    A chart that ranks on the raw price column is not slightly off, it is
    exactly backwards.
    """
    ranking = price_rank(fixture_observations)
    by_facility = {row.facility_uuid: row for row in ranking.rows}

    assert by_facility[PLAY_PADEL_COURT].price_per_court_hour == 2000.0
    assert by_facility[PADEL_FORT_COURT].price_per_court_hour == 1800.0
    assert by_facility[PADEL_UP_COURT].price_per_court_hour == 1800.0

    # The correct ranking puts Play Padel first, strictly above both others.
    assert ranking.court_hour_order[0] == PLAY_PADEL_COURT
    assert ranking.most_expensive is not None
    assert ranking.most_expensive.facility_uuid == PLAY_PADEL_COURT

    # The per-slot column, which must never be used for ranking, puts Play
    # Padel *below* Padel Up and would report it as mid-priced.
    assert by_facility[PLAY_PADEL_COURT].price_per_slot == PRICE_PLAY_PADEL_SLOT
    assert by_facility[PADEL_UP_COURT].price_per_slot == PRICE_PADEL_UP_SLOT
    assert by_facility[PLAY_PADEL_COURT].price_per_slot < by_facility[PADEL_UP_COURT].price_per_slot
    assert ranking.per_slot_order[0] == PADEL_UP_COURT
    assert ranking.slot_price_ranking_inverts is True

    # And the cheapest per slot is not the cheapest per hour either.
    assert by_facility[PADEL_FORT_COURT].price_per_slot == PRICE_PADEL_FORT_SLOT
    assert by_facility[PADEL_FORT_COURT].price_per_court_hour == (
        by_facility[PADEL_UP_COURT].price_per_court_hour
    )


def test_price_per_court_hour_scales_by_the_slots_own_duration() -> None:
    """Regression: normalizing with a config grid instead of the slot's own length.

    The slot carries its duration; config can be stale or unprobed. A 30-minute
    slot at 1000 is 2000 per hour and a 60-minute slot at 1800 is 1800, and the
    arithmetic must come from the row, not from a lookup.
    """
    half_hour = _observation(
        slot_uuid="s-30",
        venue_uuid=PLAY_PADEL_VENUE,
        facility_uuid=PLAY_PADEL_COURT,
        start_local="2026-09-14 19:00:00",
        duration_minutes=30,
        price=1000.0,
        state=SlotState.OPEN,
    )
    full_hour = _observation(
        slot_uuid="s-60",
        venue_uuid=PADEL_UP_VENUE,
        facility_uuid=PADEL_UP_COURT,
        start_local="2026-09-14 19:00:00",
        duration_minutes=60,
        price=1800.0,
        state=SlotState.OPEN,
    )
    assert price_per_court_hour(half_hour) == 2000.0
    assert price_per_court_hour(full_hour) == 1800.0
    # The per-slot figures rank the other way round; that is the whole trap.
    assert half_hour.price is not None and full_hour.price is not None
    assert half_hour.price < full_hour.price


def test_price_per_court_hour_is_none_rather_than_zero_when_unpriced() -> None:
    """Regression: an unpriced slot read as free, dragging every average down."""
    unpriced = _observation(
        slot_uuid="s-unpriced",
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        start_local="2026-09-14 19:00:00",
        duration_minutes=30,
        price=None,
        state=SlotState.OPEN,
    )
    assert price_per_court_hour(unpriced) is None


# --------------------------------------------------------------------------
# PRICE BY HOUR -- THE HONEST ABSENCE
# --------------------------------------------------------------------------


def test_price_by_hour_reports_every_venue_flat_and_no_variation_at_all(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: three identical rows rendered as if they were a finding.

    Verified reality on 2026-09-11: all three venues publish one rate for every
    hour of every day across 2945 slots. The table must say so with explicit
    flags so the dashboard prints "no venue currently varies price by hour"
    instead of drawing three flat lines and implying peak pricing exists.
    """
    table = price_by_hour_table(fixture_observations)

    assert table.has_any_variation is False
    assert len(table.venues) == 3
    for venue_uuid, expected_rate in (
        (PADEL_UP_VENUE, 1800.0),
        (PLAY_PADEL_VENUE, 2000.0),
        (PADEL_FORT_VENUE, 1800.0),
    ):
        profile = table.venue(venue_uuid)
        assert profile is not None, venue_uuid
        assert profile.is_flat is True
        assert profile.flat_price_per_court_hour == expected_rate
        assert profile.distinct_prices_per_court_hour == (expected_rate,)

    for court_uuid in (PADEL_UP_COURT, PLAY_PADEL_COURT, PADEL_FORT_COURT):
        court = table.court(court_uuid)
        assert court is not None, court_uuid
        assert court.is_flat is True

    # Every hour a venue publishes is present, so the chart exists the day a
    # venue does introduce peak pricing. Hours 02-04 are absent because nobody
    # sells then: Play Padel's late window stops at 01:30 and the earliest
    # opening is Padel Up's 05:00.
    assert table.hours == (0, 1, *range(5, 24))
    assert table.summary.startswith("No venue currently varies price by hour of day")


def test_price_by_hour_reports_variation_when_a_venue_actually_has_it() -> None:
    """Regression: a flag hardcoded to True, which would hide real peak pricing."""
    observations = [
        _fort("s-morning", "2026-09-14 07:00:00", SlotState.OPEN),
        _observation(
            slot_uuid="s-evening",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=1200.0,
            state=SlotState.OPEN,
        ),
    ]
    table = price_by_hour_table(observations)

    assert table.has_any_variation is True
    court = table.court(PADEL_FORT_COURT)
    assert court is not None
    assert court.is_flat is False
    assert court.flat_price_per_court_hour is None
    assert court.distinct_prices_per_court_hour == (1800.0, 2400.0)
    assert "vary price by hour of day" in table.summary


# --------------------------------------------------------------------------
# PRICE CHANGE DETECTION, BOUNDED BY THE POLL GAP
# --------------------------------------------------------------------------


def test_price_change_is_detected_and_bracketed_by_the_poll_gap() -> None:
    """Regression: a price change reported as an instant we never observed.

    We see the last poll with the old price and the first poll with the new
    one. The change happened somewhere in between, so the timestamp is an
    interval and ``uncertainty_minutes`` is its width. Here the poll before the
    change is 90 minutes earlier -- a collector gap -- and the uncertainty must
    be 90, not the 30-minute nominal cadence.
    """
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    snapshots = [
        _snapshot(1, base),
        _snapshot(2, base + dt.timedelta(minutes=30)),
        # Two polls missed here: the next snapshot is 90 minutes after the last.
        _snapshot(3, base + dt.timedelta(minutes=120)),
        _snapshot(4, base + dt.timedelta(minutes=150)),
    ]
    observations = [
        _fort("s-1", "2026-09-14 19:00:00", SlotState.OPEN, snapshot_id=1),
        _fort("s-1", "2026-09-14 19:00:00", SlotState.OPEN, snapshot_id=2),
        _observation(
            slot_uuid="s-1",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=1100.0,
            state=SlotState.OPEN,
            snapshot_id=3,
        ),
        _observation(
            slot_uuid="s-1",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=1100.0,
            state=SlotState.OPEN,
            snapshot_id=4,
        ),
    ]

    report = price_timeline(observations, snapshots)

    assert report.has_any_change is True
    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.facility_uuid == PADEL_FORT_COURT
    assert change.from_price_per_court_hour == 1800.0
    assert change.to_price_per_court_hour == 2200.0
    assert change.direction == "increase"
    assert change.delta_per_court_hour == 400.0

    # The interval, not an instant. The change is bounded by the gap.
    assert change.prev_seen_at == base + dt.timedelta(minutes=30)
    assert change.first_seen_at == base + dt.timedelta(minutes=120)
    assert change.uncertainty_minutes == 90
    assert change.first_seen_at - change.prev_seen_at == dt.timedelta(
        minutes=change.uncertainty_minutes
    )

    timeline = report.timeline_for(PADEL_FORT_COURT)
    assert timeline is not None
    assert timeline.is_flat is False
    assert [point.price_per_court_hour for point in timeline.points] == [
        1800.0,
        1800.0,
        2200.0,
        2200.0,
    ]
    assert timeline.current_price_per_court_hour == 2200.0


def test_price_timeline_is_flat_when_nothing_changed(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: float noise or dict ordering inventing a phantom price change."""
    report = price_timeline(synthetic_history.observations, synthetic_history.snapshots)

    assert report.has_any_change is False
    assert report.changes == ()
    assert all(timeline.is_flat for timeline in report.timelines)
    # All three courts in the scripted history appear, each with all ten polls.
    assert len(report.timelines) == 3
    assert all(
        len(timeline.points) == synthetic_history.snapshot_count for timeline in report.timelines
    )


def test_price_timeline_orders_on_observed_at_not_snapshot_id() -> None:
    """Regression: a catch-up poll written out of id order reversing the timeline."""
    base = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)
    # snapshot 2 was written first but observed *later* than snapshot 1.
    snapshots = [
        _snapshot(2, base + dt.timedelta(minutes=30)),
        _snapshot(1, base),
    ]
    observations = [
        _observation(
            slot_uuid="s-1",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=1100.0,
            state=SlotState.OPEN,
            snapshot_id=2,
        ),
        _fort("s-1", "2026-09-14 19:00:00", SlotState.OPEN, snapshot_id=1),
    ]

    timeline = price_timeline(observations, snapshots).timeline_for(PADEL_FORT_COURT)

    assert timeline is not None
    assert [point.observed_at for point in timeline.points] == [
        base,
        base + dt.timedelta(minutes=30),
    ]
    assert [point.price_per_court_hour for point in timeline.points] == [1800.0, 2200.0]


# --------------------------------------------------------------------------
# REVENUE PROXY
# --------------------------------------------------------------------------


def test_revenue_proxy_prices_booked_court_hours_and_reports_blocked_beside_them() -> None:
    """Regression: revenue presented as revenue, with the blind spot omitted.

    Two booked 30-minute slots at 900 are one court-hour and 1800 of proxy
    revenue. The three blocked slots on the same day are 1.5 court-hours we
    cannot see into -- plausibly offline sales, plausibly maintenance -- and
    must be reported on the same row rather than silently dropped or folded
    into revenue.
    """
    observations = [
        _fort("b-1", "2026-09-14 19:00:00", SlotState.BOOKED),
        _fort("b-2", "2026-09-14 19:30:00", SlotState.BOOKED),
        _fort("x-1", "2026-09-14 20:00:00", SlotState.BLOCKED),
        _fort("x-2", "2026-09-14 20:30:00", SlotState.BLOCKED),
        _fort("x-3", "2026-09-14 21:00:00", SlotState.BLOCKED),
        _fort("o-1", "2026-09-14 21:30:00", SlotState.OPEN),
        _observation(
            slot_uuid="up-1",
            venue_uuid=PADEL_UP_VENUE,
            facility_uuid=PADEL_UP_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=60,
            price=PRICE_PADEL_UP_SLOT,
            state=SlotState.BOOKED,
        ),
    ]

    report = revenue_proxy(observations)

    assert report.is_proxy is True
    assert "proxy" in report.label
    fort = report.rows_for(PADEL_FORT_VENUE)
    assert len(fort) == 1
    row = fort[0]
    assert row.week_start == MONDAY == week_start_for(dt.date(2026, 9, 14))
    assert row.week_label == "2026-W38"
    assert row.booked_court_hours == 1.0
    assert row.booked_revenue_proxy == 1800.0
    assert row.blocked_court_hours == 1.5
    assert row.blocked_revenue_if_sold == 2700.0
    assert row.open_court_hours == 0.5
    assert row.listed_court_hours == 3.0
    assert row.booked_slots == 2
    assert row.blocked_slots == 3
    assert row.unpriced_slots == 0
    assert row.upper_bound_revenue_proxy == 4500.0
    assert row.currency == "INR"

    # One 60-minute Padel Up booking is one court-hour, not "one slot" equal to
    # Padel Fort's two half-hours.
    up = report.rows_for(PADEL_UP_VENUE)[0]
    assert up.booked_court_hours == 1.0
    assert up.booked_revenue_proxy == 1800.0
    assert up.booked_slots == 1
    assert up.blocked_court_hours == 0.0

    total = report.total_for(PADEL_FORT_VENUE)
    assert total is not None
    assert total.booked_revenue_proxy == 1800.0
    assert total.blocked_court_hours == 1.5
    assert total.weeks == 1


def test_revenue_proxy_does_not_multiply_revenue_by_the_poll_count(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: summing a multi-poll stream, inflating revenue ~48x.

    Every slot is re-observed on every poll. The scripted history holds ten
    polls of the same slots, so a report that does not reduce to the latest
    observation per slot reports ten times the revenue and ten times the
    court-hours.
    """
    reduced = revenue_proxy(settled_observations(synthetic_history.observations))
    raw = revenue_proxy(synthetic_history.observations)

    for venue_uuid in (PADEL_FORT_VENUE, PLAY_PADEL_VENUE, PADEL_UP_VENUE):
        reduced_total = reduced.total_for(venue_uuid)
        raw_total = raw.total_for(venue_uuid)
        assert reduced_total is not None and raw_total is not None
        assert raw_total.booked_revenue_proxy == reduced_total.booked_revenue_proxy
        assert raw_total.listed_court_hours == reduced_total.listed_court_hours

    # And the absolute figure is the single-poll truth, not ten polls of it.
    fort = raw.total_for(PADEL_FORT_VENUE)
    assert fort is not None
    assert fort.blocked_court_hours == pytest.approx(
        synthetic_history.expected_blocked_evening_minutes / 60 + 0.5,  # + the OPEN->BLOCKED slot
        abs=1e-9,
    )


def test_revenue_proxy_ignores_unpriced_slots_instead_of_pricing_them_at_zero() -> None:
    """Regression: an unpriced pickleball court dragging the proxy toward zero."""
    observations = [
        _fort("b-1", "2026-09-14 19:00:00", SlotState.BOOKED),
        _observation(
            slot_uuid="b-2",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid="d8452c5f-a340-45a9-9123-995edd038bf4",
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=None,
            state=SlotState.BOOKED,
        ),
    ]
    row = revenue_proxy(observations).rows_for(PADEL_FORT_VENUE)[0]

    assert row.booked_revenue_proxy == 900.0
    assert row.booked_court_hours == 1.0  # court-time is still counted
    assert row.unpriced_slots == 1


# --------------------------------------------------------------------------
# THE MARKET-SHARE HONESTY TEST
# --------------------------------------------------------------------------


def test_market_share_flags_padel_ups_zero_bookings_and_keeps_its_supply_share(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: a share pie implying Padel Up has no business.

    Padel Up published 589 slots over 31 days at the highest rate of the three
    and recorded zero bookings. A bare demand-share chart gives Padel Fort and
    Play Padel 100% of "the market" and reads as a verdict on Padel Up. The
    likelier reading is that Padel Up does not sell through Hudle at all -- a
    statement about our instrument, not their customers -- so the venue must
    carry an explicit flag, and its share of observed *supply* must stay
    non-zero because supply is a denominator that still means something.
    """
    report = market_share(fixture_observations)

    quality = report.quality_for(PADEL_UP_VENUE)
    assert quality is not None
    assert quality.has(DataQualityFlag.NO_BOOKINGS_EVER_OBSERVED)
    assert quality.booked_court_hours == 0.0
    assert quality.listed_court_hours == pytest.approx(589 * 60 / 60)
    assert "may not take bookings through Hudle" in quality.note

    # The other two really do have bookings, so the flag is discriminating.
    for venue_uuid in (PLAY_PADEL_VENUE, PADEL_FORT_VENUE):
        other = report.quality_for(venue_uuid)
        assert other is not None
        assert not other.has(DataQualityFlag.NO_BOOKINGS_EVER_OBSERVED)
        assert other.booked_court_hours > 0.0

    up_rows = report.rows_for(PADEL_UP_VENUE)
    assert up_rows, "Padel Up must stay in the chart, not be filtered out"
    for row in up_rows:
        assert row.booked_court_hours == 0.0
        assert row.demand_share in (None, 0.0)
        # Supply share is the honest denominator for a venue with no demand.
        assert row.supply_share is not None
        assert row.supply_share > 0.0
        assert row.listed_court_hours > 0.0

    # At least one week where others booked: demand share is a real 0.0 there,
    # and supply share is roughly a third of the market.
    weeks_with_demand = [row for row in up_rows if row.demand_share == 0.0]
    assert weeks_with_demand
    assert max(row.supply_share or 0.0 for row in up_rows) > 0.25

    assert report.flagged_venues
    assert PADEL_UP_VENUE in report.caveat_summary
    assert report.window.start is not None and report.window.end is not None


def test_market_share_demand_share_is_none_not_zero_when_no_one_booked() -> None:
    """Regression: a week with no market at all rendered as three zeroes.

    "Nobody booked anything" and "this venue booked none of a real market" are
    different facts. A 0.0 that means both is how an empty week looks like a
    competitive loss.
    """
    observations = [
        _fort("o-1", "2026-09-14 19:00:00", SlotState.OPEN),
        _observation(
            slot_uuid="o-2",
            venue_uuid=PADEL_UP_VENUE,
            facility_uuid=PADEL_UP_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=60,
            price=PRICE_PADEL_UP_SLOT,
            state=SlotState.OPEN,
        ),
    ]
    report = market_share(observations)

    assert all(row.demand_share is None for row in report.rows)
    assert all(row.supply_share is not None for row in report.rows)
    # Court-hours, not slots: one 60-minute slot outweighs one 30-minute slot.
    shares = {row.venue_uuid: row.supply_share for row in report.rows}
    assert shares[PADEL_UP_VENUE] == pytest.approx(2 / 3)
    assert shares[PADEL_FORT_VENUE] == pytest.approx(1 / 3)


def test_market_share_flags_a_venue_that_withdraws_most_of_its_inventory() -> None:
    """Regression: heavy blocking read as low demand rather than as a blind spot."""
    observations = [
        _fort("b-1", "2026-09-14 19:00:00", SlotState.BOOKED),
        *(
            _fort(f"x-{index}", f"2026-09-14 {19 + index}:30:00", SlotState.BLOCKED)
            for index in range(4)
        ),
    ]
    quality = market_share(observations).quality_for(PADEL_FORT_VENUE)

    assert quality is not None
    assert quality.has(DataQualityFlag.HEAVILY_BLOCKED_INVENTORY)
    assert quality.blocked_share == pytest.approx(0.8)
    assert "withdrew 80%" in quality.note


# --------------------------------------------------------------------------
# EVERY METRIC DECLARES ITS DENOMINATOR AND DATE RANGE
# --------------------------------------------------------------------------


def test_every_returned_metric_carries_a_denominator_a_date_range_and_caveats(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: a chart whose caption lives in a template and drifts.

    Every report describes its own metrics so the web layer renders the
    denominator onto the chart instead of hardcoding a caption that outlives
    the arithmetic it describes. A metric with no denominator, no date range or
    no caveat is a metric the dashboard cannot render honestly.
    """
    snapshots = [_snapshot(1, FIXTURE_OBSERVED_AT)]
    reports: list[MetricReport] = [
        price_rank(fixture_observations),
        price_by_hour_table(fixture_observations),
        price_timeline(fixture_observations, snapshots),
        revenue_proxy(fixture_observations),
        market_share(fixture_observations),
        demand_by_hour(fixture_observations),
        demand_heatmap(fixture_observations),
        blocked_inventory(fixture_observations),
        peak_pricing_opportunity(fixture_observations, peak_hours=(18, 19, 20, 21, 22)),
    ]

    registry = metric_registry(*reports)

    assert len(registry) == 10  # market_share declares two: demand and supply
    assert "market_share_demand" in registry.names
    assert "market_share_supply" in registry.names
    assert len(set(registry.names)) == len(registry.names)

    for spec in registry:
        assert spec.name and spec.title and spec.unit, spec
        assert spec.denominator.strip(), f"{spec.name} has no denominator"
        # The window is business dates, not calendar dates, which is why it
        # opens a day before the first calendar date in the fixtures: Play
        # Padel's 00:00-01:30 slots on 2026-09-11 are Thursday-night sessions
        # and roll back to 2026-09-10.
        assert spec.date_range.start == dt.date(2026, 9, 10), spec.name
        assert spec.date_range.end == dt.date(2026, 10, 11), spec.name
        assert spec.date_range.business_days == 32, spec.name
        assert not spec.date_range.is_empty
        assert "2026-09-10" in spec.date_range.label()
        assert "32 trading days" in spec.date_range.label()
        assert spec.caveats, f"{spec.name} declares no caveat"
        assert spec.has_caveats

    assert registry.with_caveats == registry.specs
    assert registry.get("revenue_proxy") is not None
    assert registry.get("not_a_metric") is None
    # The revenue metric must say the word out loud.
    revenue_spec = registry.get("revenue_proxy")
    assert revenue_spec is not None
    assert any("proxy" in caveat for caveat in revenue_spec.caveats)


# --------------------------------------------------------------------------
# DEMAND BY HOUR AND THE BUSINESS DATE
# --------------------------------------------------------------------------


def test_demand_by_hour_attributes_a_post_midnight_slot_to_the_previous_business_date() -> None:
    """Regression: Friday-night demand filed under Saturday.

    Play Padel sells 00:00-01:30 and Hudle stamps those slots with the calendar
    date they fall on, so a 00:30 Saturday booking is a Friday-night session.
    Aggregating on the raw local date moves it to the wrong trading day and the
    wrong day of week; ``business_date`` rolls it back.
    """
    booking = _observation(
        slot_uuid="pm-1",
        venue_uuid=PLAY_PADEL_VENUE,
        facility_uuid=PLAY_PADEL_COURT,
        start_local="2026-09-12 00:30:00",
        duration_minutes=30,
        price=PRICE_PLAY_PADEL_SLOT,
        state=SlotState.BOOKED,
    )
    evening = _observation(
        slot_uuid="pm-0",
        venue_uuid=PLAY_PADEL_VENUE,
        facility_uuid=PLAY_PADEL_COURT,
        start_local="2026-09-11 22:00:00",
        duration_minutes=30,
        price=PRICE_PLAY_PADEL_SLOT,
        state=SlotState.BOOKED,
    )

    assert booking.slot_start_local.startswith(str(SATURDAY))
    assert booking.business_date == FRIDAY

    report = demand_by_hour([booking, evening])
    midnight_row = report.row(PLAY_PADEL_VENUE, 0)
    assert midnight_row is not None

    # The slot still lives in hour 0 -- wall-clock is never rewritten ...
    assert midnight_row.hour == 0
    assert midnight_row.booked_court_minutes == 30
    assert midnight_row.booked_court_hours == 0.5
    # ... but the trading day it counts toward is Friday, not Saturday.
    assert midnight_row.business_dates == (FRIDAY,)
    assert SATURDAY not in midnight_row.business_dates
    assert midnight_row.day_count == 1

    # Both bookings therefore belong to the same trading day and the same week.
    assert {date for row in report.rows for date in row.business_dates} == {FRIDAY}
    assert report.window.start == report.window.end == FRIDAY


def test_demand_heatmap_puts_a_post_midnight_saturday_slot_on_friday() -> None:
    """Regression: a weekday heatmap built on the local date shifts a whole night.

    The 00:30 slot falls on Saturday 2026-09-12 by wall clock, but it is Friday
    evening's demand. A heatmap keyed on the local date invents Saturday
    midnight demand and erases the Friday demand that produced it.
    """
    booking = _observation(
        slot_uuid="pm-1",
        venue_uuid=PLAY_PADEL_VENUE,
        facility_uuid=PLAY_PADEL_COURT,
        start_local="2026-09-12 00:30:00",
        duration_minutes=30,
        price=PRICE_PLAY_PADEL_SLOT,
        state=SlotState.BOOKED,
    )
    report = demand_heatmap([booking])

    friday, saturday = FRIDAY.weekday(), SATURDAY.weekday()
    friday_cell = report.cell(PLAY_PADEL_VENUE, friday, 0)
    assert friday_cell is not None
    assert friday_cell.weekday_name == "Friday"
    assert friday_cell.booked_court_minutes == 30
    assert friday_cell.booked_court_minutes_per_day == 30.0
    assert report.cell(PLAY_PADEL_VENUE, saturday, 0) is None


def test_demand_by_hour_reports_open_and_blocked_beside_booked(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: an hour with withdrawn inventory reading as an hour with no demand."""
    report = demand_by_hour(fixture_observations)
    fort_19 = report.row(PADEL_FORT_VENUE, 19)
    assert fort_19 is not None

    assert fort_19.listed_court_minutes == (
        fort_19.booked_court_minutes + fort_19.open_court_minutes + fort_19.blocked_court_minutes
    )
    assert fort_19.blocked_court_minutes > 0
    assert fort_19.occupancy_strict is not None
    # Strict occupancy excludes the blocked minutes from both sides.
    assert fort_19.occupancy_strict == pytest.approx(
        fort_19.booked_court_minutes / (fort_19.booked_court_minutes + fort_19.open_court_minutes)
    )
    assert fort_19.day_count == len(set(fort_19.business_dates))

    # Padel Up's 60-minute grid gives it 60 court-minutes per day per hour while
    # Padel Fort's 30-minute grid gives 60 across two slots: equal court-time,
    # unequal slot counts.
    up_19 = report.row(PADEL_UP_VENUE, 19)
    assert up_19 is not None
    assert up_19.listed_court_minutes == 60 * up_19.day_count


# --------------------------------------------------------------------------
# BLOCKED INVENTORY
# --------------------------------------------------------------------------


def test_blocked_inventory_surfaces_the_whole_evening_padel_fort_withdrew(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: 420 court-minutes of prime evening inventory folded into occupancy.

    Padel Fort pulled all 14 slots from 17:00 to 23:30 on 2026-09-13 with zero
    bookings. Averaged into a daily occupancy figure that day looks like an
    empty venue. Reported as a contiguous run with no bookings it looks like
    what it probably is: a session sold or closed outside Hudle.
    """
    report = blocked_inventory(fixture_observations)
    rows = {
        (row.facility_uuid, row.business_date): row for row in report.rows_for(PADEL_FORT_VENUE)
    }
    row = rows[(PADEL_FORT_COURT, dt.date(2026, 9, 13))]

    assert row.blocked_slots == 14
    assert row.blocked_court_hours == 7.0
    assert row.booked_court_hours == 0.0
    assert row.longest_blocked_run_minutes == 420
    assert row.longest_blocked_run_start_local == "2026-09-13 17:00:00"
    assert row.whole_session_withdrawn is True
    assert row.blocked_share == pytest.approx(420 / (36 * 30))

    assert row in report.withdrawn_sessions

    # Padel Up blocks 05:00 every single day: a standing venue rule, one slot
    # at a time, and it must never be reported as a withdrawn session.
    up_rows = report.rows_for(PADEL_UP_VENUE)
    assert len(up_rows) == 31
    assert all(up_row.blocked_slots == 1 for up_row in up_rows)
    assert all(up_row.longest_blocked_run_minutes == 60 for up_row in up_rows)
    assert all(not up_row.whole_session_withdrawn for up_row in up_rows)


def test_blocked_inventory_run_length_chains_slots_not_counts_them(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a run measured in slots, which mis-sizes a 60-minute grid.

    The scripted history holds the same 14-slot blocked evening in all ten
    polls. Run length must be 420 court-minutes after reducing to the latest
    observation per slot -- not 4200, and not "14".
    """
    report = blocked_inventory(synthetic_history.observations)
    row = next(
        r
        for r in report.rows_for(PADEL_FORT_VENUE)
        if r.business_date == synthetic_history.blocked_evening_business_date
    )

    assert row.blocked_slots == len(synthetic_history.blocked_evening_slot_uuids) == 14
    assert row.longest_blocked_run_minutes == synthetic_history.expected_blocked_evening_minutes
    assert row.blocked_court_hours == 7.0
    assert row.whole_session_withdrawn is True
    # The whole evening was withdrawn and nothing was sellable, so a strict
    # occupancy ratio for that day is undefined rather than 0%.
    assert row.blocked_share == 1.0


def test_blocked_inventory_does_not_call_scattered_maintenance_a_withdrawn_session() -> None:
    """Regression: four isolated blocked slots reported as a pulled evening."""
    observations = [
        _fort(f"x-{hour}", f"2026-09-14 {hour}:00:00", SlotState.BLOCKED)
        for hour in (7, 10, 14, 18)
    ]
    row = blocked_inventory(observations).rows_for(PADEL_FORT_VENUE)[0]

    assert row.blocked_slots == 4
    assert row.blocked_court_hours == 2.0
    assert row.longest_blocked_run_minutes == 30
    assert row.whole_session_withdrawn is False
    assert blocked_inventory(observations).withdrawn_sessions == ()


# --------------------------------------------------------------------------
# PEAK-PRICING OPPORTUNITY
# --------------------------------------------------------------------------


def test_peak_pricing_opportunity_needs_a_flat_price_and_a_real_occupancy_gap() -> None:
    """Regression: a flat-price venue with lopsided demand shown as fully priced.

    Padel Fort here sells every evening slot and no morning slot while charging
    one rate all day. That is a pricing question worth asking. The flag must
    require both halves -- a flat price and a real gap -- so a venue that
    already prices its peak is not flagged.
    """
    observations = [
        _fort("pk-1", "2026-09-14 19:00:00", SlotState.BOOKED),
        _fort("pk-2", "2026-09-14 19:30:00", SlotState.BOOKED),
        _fort("op-1", "2026-09-14 08:00:00", SlotState.OPEN),
        _fort("op-2", "2026-09-14 08:30:00", SlotState.OPEN),
    ]
    row = peak_pricing_opportunity(observations, peak_hours=(18, 19, 20, 21, 22)).row(
        PADEL_FORT_VENUE
    )

    assert row is not None
    assert row.peak_occupancy_strict == 1.0
    assert row.offpeak_occupancy_strict == 0.0
    assert row.occupancy_gap == 1.0
    assert row.peak_booked_court_hours == 1.0
    assert row.offpeak_sellable_court_hours == 1.0
    assert row.price_is_flat is True
    assert row.price_per_court_hour == 1800.0
    assert row.is_candidate is True


def test_peak_pricing_opportunity_is_not_a_candidate_when_the_price_already_varies() -> None:
    """Regression: recommending peak pricing to a venue that already has it."""
    observations = [
        _observation(
            slot_uuid="pk-1",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=1500.0,
            state=SlotState.BOOKED,
        ),
        _fort("op-1", "2026-09-14 08:00:00", SlotState.OPEN),
    ]
    row = peak_pricing_opportunity(observations, peak_hours=(19,)).row(PADEL_FORT_VENUE)

    assert row is not None
    assert row.price_is_flat is False
    assert row.price_per_court_hour is None
    assert row.is_candidate is False
    assert peak_pricing_opportunity(observations, peak_hours=(19,)).candidates == ()


def test_peak_pricing_opportunity_gap_is_none_when_a_side_had_no_inventory() -> None:
    """Regression: an undefined comparison rendered as a zero gap.

    A venue that withdrew its whole evening has no sellable peak court-time at
    all, which is a different fact from selling none of it. Strict occupancy is
    ``None`` there, and the gap must stay ``None`` rather than collapse to 0.
    """
    observations = [
        _fort("x-1", "2026-09-14 19:00:00", SlotState.BLOCKED),
        _fort("op-1", "2026-09-14 08:00:00", SlotState.OPEN),
    ]
    row = peak_pricing_opportunity(observations, peak_hours=(19,)).row(PADEL_FORT_VENUE)

    assert row is not None
    assert row.peak_occupancy_strict is None
    assert row.peak_sellable_court_hours == 0.0
    assert row.offpeak_occupancy_strict == 0.0
    assert row.occupancy_gap is None
    assert row.is_candidate is False


def test_padel_up_is_not_a_peak_pricing_candidate_because_it_sells_nothing_at_all(
    fixture_observations: list[SlotObservation],
) -> None:
    """Regression: a zero-everywhere venue flagged for a peak-pricing tweak.

    Padel Up's occupancy is 0% in every hour. There is no peak/off-peak gap to
    price against, and its problem -- whatever it is -- is not the shape of its
    demand curve.
    """
    report = peak_pricing_opportunity(fixture_observations, peak_hours=(18, 19, 20, 21, 22))
    row = report.row(PADEL_UP_VENUE)

    assert row is not None
    assert row.peak_occupancy_strict == 0.0
    assert row.offpeak_occupancy_strict == 0.0
    assert row.occupancy_gap == 0.0
    assert row.price_is_flat is True
    assert row.price_per_court_hour == 1800.0
    assert row.is_candidate is False


# --------------------------------------------------------------------------
# The de-duplication guard
# --------------------------------------------------------------------------


def test_market_reduces_with_the_same_settled_rule_as_occupancy(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: the ~48x court-minute inflation from summing every poll.

    The reducer must keep exactly one row per slot and applying it twice must
    change nothing, so an aggregate that calls it internally is safe to hand
    already-reduced input. This module must use the *same* reducer occupancy
    uses -- ``settled_observations`` -- or the two print different headline
    numbers from one stream.
    """
    once = settled_observations(synthetic_history.observations)
    twice = settled_observations(once)

    slot_uuids = {o.slot_uuid for o in synthetic_history.observations}
    assert len(once) == len(slot_uuids)
    assert len(twice) == len(once)

    # The cancelled slot was BOOKED mid-history and OPEN by the last pre-start
    # poll; the reducer must report the state it settled in, not its most
    # interesting one.
    cancelled = next(o for o in once if o.slot_uuid == synthetic_history.cancellation_slot_uuid)
    assert cancelled.state is SlotState.OPEN
    assert synthetic_history.trajectory(synthetic_history.cancellation_slot_uuid)[3] is (
        SlotState.BOOKED
    )

    # And market's own aggregates agree with occupancy's, court-hour for
    # court-hour, because they now start from the same rows.
    market_hours = {
        row.venue_uuid: row.listed_court_hours
        for row in revenue_proxy(synthetic_history.observations).venue_totals
    }
    occupancy_hours: dict[str, float] = {}
    for row in occupancy_by_venue_day(synthetic_history.observations):
        occupancy_hours[row.venue_uuid] = (
            occupancy_hours.get(row.venue_uuid, 0.0) + row.total_court_hours
        )
    assert market_hours == occupancy_hours


def test_normalization_pair_compares_as_court_minutes_not_slot_counts(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a 60-minute slot and a 30-minute slot compared as 1 == 1."""
    sixty, thirty = synthetic_history.normalization_slot_uuids
    observations = [o for o in synthetic_history.observations if o.slot_uuid in {sixty, thirty}]
    report = demand_by_hour(observations)

    up_19 = report.row(PADEL_UP_VENUE, 19)
    fort_19 = report.row(PADEL_FORT_VENUE, 19)
    assert up_19 is not None and fort_19 is not None
    assert (up_19.listed_court_minutes, fort_19.listed_court_minutes) == (
        synthetic_history.expected_normalization_minutes
    )
    assert up_19.listed_court_minutes == 2 * fort_19.listed_court_minutes


# --------------------------------------------------------------------------
# One settled-row definition, shared with the occupancy module
# --------------------------------------------------------------------------

#: 2026-09-14 19:00 IST is 13:30Z, so 12:00Z is before the slot and 15:00Z
#: after it. Both are literals: the suite never reads a clock.
_PRE_START = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.UTC)
_POST_START = dt.datetime(2026, 9, 14, 15, 0, tzinfo=dt.UTC)


def _trajectory(states: Sequence[tuple[SlotState, dt.datetime]]) -> list[SlotObservation]:
    """One Padel Fort slot observed repeatedly, each poll at its own instant."""
    return [
        _observation(
            slot_uuid="slot-post-start-change",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=30,
            price=PRICE_PADEL_FORT_SLOT,
            state=state,
            snapshot_id=position + 1,
            observed_at=observed_at,
        )
        for position, (state, observed_at) in enumerate(states)
    ]


def test_market_and_occupancy_agree_on_a_slot_that_changed_after_it_started() -> None:
    """Regression: market.py and occupancy.py printing 100% and 0% for one slot.

    Hudle keeps republishing a slot after it elapses, so a late cancellation or
    a grid republish rewrites the last observation of a slot that has already
    been played. While this module reduced on the highest snapshot id and
    occupancy reduced on the last pre-start observation, the same three rows
    gave occupancy 0.0 booked court-hours and market Rs 450 of revenue. Both
    now start from ``settled_observations``, so the answer is one answer.
    """
    observations = _trajectory(
        [
            (SlotState.OPEN, _PRE_START),
            (SlotState.OPEN, _PRE_START),
            (SlotState.BOOKED, _POST_START),
        ]
    )
    assert [o.is_past for o in observations] == [False, False, True]

    day = occupancy_by_venue_day(observations)[0]
    total = revenue_proxy(observations).total_for(PADEL_FORT_VENUE)
    share = market_share(observations).rows_for(PADEL_FORT_VENUE)[0]

    assert total is not None
    assert day.booked_court_hours == 0.0
    assert total.booked_court_hours == 0.0
    assert total.booked_revenue_proxy == 0.0
    assert share.booked_court_hours == 0.0
    assert day.occupancy_strict == 0.0

    # And the mirror image: sold before it started, released only afterwards.
    released = _trajectory(
        [
            (SlotState.BOOKED, _PRE_START),
            (SlotState.BOOKED, _PRE_START),
            (SlotState.OPEN, _POST_START),
        ]
    )
    released_day = occupancy_by_venue_day(released)[0]
    released_total = revenue_proxy(released).total_for(PADEL_FORT_VENUE)

    assert released_total is not None
    assert released_day.booked_court_hours == 0.5
    assert released_total.booked_court_hours == 0.5
    assert released_total.booked_revenue_proxy == pytest.approx(PRICE_PADEL_FORT_SLOT)


# --------------------------------------------------------------------------
# The sport axis of the same normalization problem
# --------------------------------------------------------------------------


def test_pickleball_courts_do_not_inflate_a_venue_padel_numbers() -> None:
    """Regression: one venue_uuid covering three courts and two sports.

    ``run_collect`` polls every active court, and Padel Fort has one padel
    court and two pickleball ones. Summed under one venue_uuid its listed
    court-hours read three courts' worth and its occupancy blends two sports,
    so its supply share inflates by half again while a single-court venue's
    halves. ``sport`` is carried on the observation for exactly this filter.
    """
    padel = [
        _fort("fort-padel-1", "2026-09-14 19:00:00", SlotState.BOOKED),
        _fort("fort-padel-2", "2026-09-14 19:30:00", SlotState.OPEN),
    ]
    pickleball = [
        _observation(
            slot_uuid=f"fort-pickle-{index}",
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid="pickleball-court-1",
            start_local=start_local,
            duration_minutes=30,
            price=PRICE_PADEL_FORT_SLOT,
            state=SlotState.OPEN,
            sport=Sport.PICKLEBALL,
        )
        for index, start_local in enumerate(("2026-09-14 19:00:00", "2026-09-14 19:30:00"))
    ]
    up = [
        _observation(
            slot_uuid="up-1",
            venue_uuid=PADEL_UP_VENUE,
            facility_uuid=PADEL_UP_COURT,
            start_local="2026-09-14 19:00:00",
            duration_minutes=60,
            price=PRICE_PADEL_UP_SLOT,
            state=SlotState.OPEN,
        )
    ]
    observations = [*padel, *pickleball, *up]

    blended = {row.venue_uuid: row.listed_court_hours for row in market_share(observations).rows}
    padel_only = {
        row.venue_uuid: row.listed_court_hours
        for row in market_share(observations, sport=Sport.PADEL).rows
    }

    # Unfiltered, Padel Fort looks twice the size it is on the padel axis.
    assert blended[PADEL_FORT_VENUE] == 2.0
    assert padel_only[PADEL_FORT_VENUE] == 1.0
    assert blended[PADEL_UP_VENUE] == padel_only[PADEL_UP_VENUE] == 1.0

    # The headline occupancy is blended too: the pickleball court is all OPEN.
    fort_blended = occupancy_by_venue_day(observations, venue_uuid=PADEL_FORT_VENUE)[0]
    fort_padel = occupancy_by_venue_day(
        observations, venue_uuid=PADEL_FORT_VENUE, sport=Sport.PADEL
    )[0]
    assert fort_blended.occupancy_strict == pytest.approx(0.25)
    assert fort_padel.occupancy_strict == pytest.approx(0.5)


def test_court_hour_conversion_cross_foots_between_the_two_modules() -> None:
    """Regression: market rounding court-hours to 4dp while occupancy did not.

    An occupancy panel and a market panel showing the same venue's court-hours
    have to agree to the last digit, and 589 court-minutes is 9.816666... hours,
    not 9.8167.
    """
    assert _hours(589) == to_court_hours(589)
    assert _hours(589) != round(to_court_hours(589), 4)


def test_local_hour_agrees_across_modules_on_a_non_padded_hour() -> None:
    """Regression: competing spellings of "the hour this slot starts in".

    ``parse_slot`` stores Hudle's ``start_time`` verbatim and
    ``from_local_text`` accepts a non-zero-padded hour, so ``6:00:00`` is a
    storable value. Slicing ``slot_start_local[11:13]`` reads ``"6:"`` and
    raises, taking down every hour-bucketed chart for the whole window, while
    the parsing spelling returns 6. Every hour-bucketed metric -- demand by
    hour, the weekday heatmap, the price-by-hour table, peak / off-peak lead
    time -- now routes through the one ``slot_start_hour`` in ``tracker.types``
    rather than each module keeping its own copy.
    """
    padded = _fort("padded", "2026-09-14 06:00:00", SlotState.OPEN)
    unpadded = dataclasses.replace(padded, slot_start_local="2026-09-14 6:00:00")

    assert slot_start_hour(padded) == 6
    assert slot_start_hour(unpadded) == local_hour_of(unpadded.slot_start_local) == 6
