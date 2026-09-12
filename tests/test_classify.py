"""Tests for the state-classification and parsing layer.

Every assertion here runs against the recorded fixtures in ``fixtures/raw/``,
captured live on 2026-09-11. Nothing calls the network and nothing calls
``datetime.now()``: ``OBSERVED_AT`` below is the literal instant (16:21 IST)
at which the pastness behaviour was verified by hand.

Each test names the regression it prevents in its docstring.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Sequence
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
)
from tracker.classify import (
    Occupancy,
    SlotParseError,
    classify_slot,
    court_minutes,
    court_minutes_by_state,
    grid_minutes_from_payload,
    occupancy_gross,
    occupancy_strict,
    parse_slot,
    parse_slot_grid,
)
from tracker.types import SlotObservation, SlotState, Sport

#: 16:21 IST on 2026-09-11 -- the moment Padel Fort's already-elapsed 07:00 and
#: 16:00 slots were observed still reading ``is_available: true``.
OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 51, tzinfo=dt.UTC)

SEPT_11 = dt.date(2026, 9, 11)
SEPT_13 = dt.date(2026, 9, 13)

# Verified counts, re-derived from the fixtures and pinned here.
EXPECTED_COUNTS = {
    "padel_up": {"total": 589, SlotState.BOOKED: 0, SlotState.BLOCKED: 31, SlotState.OPEN: 558},
    "play_padel": {"total": 1240, SlotState.BOOKED: 21, SlotState.BLOCKED: 0, SlotState.OPEN: 1219},
    "padel_fort": {"total": 1116, SlotState.BOOKED: 6, SlotState.BLOCKED: 18, SlotState.OPEN: 1092},
}

FORT_BOOKED_STARTS = (
    "2026-09-11 19:00:00",
    "2026-09-11 19:30:00",
    "2026-09-11 20:00:00",
    "2026-09-11 20:30:00",
    "2026-09-11 21:00:00",
    "2026-09-11 21:30:00",
)
FORT_BLOCKED_STARTS = (
    "2026-09-11 17:00:00",
    "2026-09-11 17:30:00",
    "2026-09-11 18:00:00",
    "2026-09-11 18:30:00",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parse(
    payload: Any, venue_uuid: str, facility_uuid: str, sport: Sport = Sport.PADEL
) -> list[SlotObservation]:
    return parse_slot_grid(
        payload,
        snapshot_id=1,
        observed_at=OBSERVED_AT,
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        business_day_start_hour=BUSINESS_DAY_START_HOUR,
        tz=TZ,
    )


@pytest.fixture(scope="module")
def fort_observations(raw_slots_padel_fort: Any) -> list[SlotObservation]:
    return _parse(raw_slots_padel_fort, PADEL_FORT_VENUE, PADEL_FORT_COURT)


@pytest.fixture(scope="module")
def up_observations(raw_slots_padel_up: Any) -> list[SlotObservation]:
    return _parse(raw_slots_padel_up, PADEL_UP_VENUE, PADEL_UP_COURT)


@pytest.fixture(scope="module")
def play_observations(raw_slots_play_padel: Any) -> list[SlotObservation]:
    return _parse(raw_slots_play_padel, PLAY_PADEL_VENUE, PLAY_PADEL_COURT)


def _by_start(observations: Sequence[SlotObservation]) -> dict[str, SlotObservation]:
    return {o.slot_start_local: o for o in observations}


def _raw_slot(**overrides: Any) -> dict[str, Any]:
    """A minimal raw slot shaped exactly like a recorded Hudle row."""
    raw: dict[str, Any] = {
        "id": "00000000-0000-4000-8000-000000000000",
        "facility_uuid": PADEL_FORT_COURT,
        "facility_name": "Padel Court",
        "price": "900.00",
        "start_time": "2026-09-20 19:00:00",
        "end_time": "2026-09-20 19:30:00",
        "total_count": 1,
        "available_count": 1,
        "is_available": True,
        "is_booked": False,
    }
    raw.update(overrides)
    return raw


def _parse_one(**overrides: Any) -> SlotObservation:
    return parse_slot(
        _raw_slot(**overrides),
        snapshot_id=1,
        observed_at=OBSERVED_AT,
        venue_uuid=PADEL_FORT_VENUE,
        facility_uuid=PADEL_FORT_COURT,
        sport=Sport.PADEL,
        business_day_start_hour=BUSINESS_DAY_START_HOUR,
        tz=TZ,
    )


# --------------------------------------------------------------------------
# The three states, on real rows
# --------------------------------------------------------------------------


def test_three_states_on_real_padel_fort_rows(fort_observations: list[SlotObservation]) -> None:
    """Regression: the three states must be read off the verified real rows.

    Padel Fort 2026-09-11 carries all three at once: 19:00-21:30 sold,
    17:00-18:30 pulled from inventory, everything else bookable.
    """
    by_start = _by_start([o for o in fort_observations if o.business_date == SEPT_11])

    for start in FORT_BOOKED_STARTS:
        assert by_start[start].state is SlotState.BOOKED, start
    for start in FORT_BLOCKED_STARTS:
        assert by_start[start].state is SlotState.BLOCKED, start

    assert by_start["2026-09-11 06:00:00"].state is SlotState.OPEN
    assert Counter(o.state for o in by_start.values()) == {
        SlotState.OPEN: 26,
        SlotState.BOOKED: 6,
        SlotState.BLOCKED: 4,
    }


def test_is_booked_wins_over_is_available(fort_observations: list[SlotObservation]) -> None:
    """Regression: reading ``is_available`` first classifies sold slots as OPEN.

    Every real booked row reports ``is_available: true`` as well, so a naive
    availability-first branch erases the headline number entirely.
    """
    booked = [o for o in fort_observations if o.state is SlotState.BOOKED]
    assert len(booked) == 6
    assert all(o.is_available for o in booked), "Hudle reports booked slots as available"
    assert classify_slot({"is_available": True, "is_booked": True}) is SlotState.BOOKED


def test_blocked_needs_both_flags_false(fort_observations: list[SlotObservation]) -> None:
    """Regression: BLOCKED must require not-available AND not-booked.

    A blocked slot keeps ``available_count: 1``, so inferring blocking from the
    count instead of the flags would classify it as bookable.
    """
    blocked = [o for o in fort_observations if o.state is SlotState.BLOCKED]
    assert len(blocked) == 18
    assert all(not o.is_available and not o.is_booked for o in blocked)
    assert all(o.available_count == 1 for o in blocked)


# --------------------------------------------------------------------------
# Pastness -- the single most important behaviour in the repo
# --------------------------------------------------------------------------


def test_elapsed_slots_are_open_not_blocked(fort_observations: list[SlotObservation]) -> None:
    """Regression: an elapsed slot must classify OPEN, with is_past its own flag.

    The original brief claimed Hudle marks past slots unavailable. It does not.
    At 16:21 IST on 2026-09-11, Padel Fort's 07:00 and 16:00 slots from that
    same morning still returned ``is_available: true, is_booked: false``.
    Folding pastness into the state enum would invent BLOCKED inventory out of
    every elapsed hour, wrecking both occupancy denominators; deriving pastness
    from ``is_available`` would mark the whole future day as past.
    """
    by_start = _by_start([o for o in fort_observations if o.business_date == SEPT_11])

    for start in ("2026-09-11 07:00:00", "2026-09-11 16:00:00"):
        observation = by_start[start]
        assert observation.state is SlotState.OPEN, start
        assert observation.is_available is True
        assert observation.is_booked is False
        assert observation.is_past is True, "slot elapsed before observed_at"

    future = by_start["2026-09-11 19:00:00"]
    assert future.state is SlotState.BOOKED
    assert future.is_past is False

    boundary = by_start["2026-09-11 16:30:00"]
    assert boundary.is_past is False, "16:30 IST had not started at 16:21 IST"


def test_is_past_is_computed_from_the_observation_instant() -> None:
    """Regression: pastness must move with observed_at, not with the payload.

    The same raw row is past or future depending only on when we looked, so
    ``is_past`` can never be cached on the slot or inferred from its flags.
    """
    raw = _raw_slot(start_time="2026-09-11 07:00:00", end_time="2026-09-11 07:30:00")
    kwargs: dict[str, Any] = {
        "snapshot_id": 1,
        "venue_uuid": PADEL_FORT_VENUE,
        "facility_uuid": PADEL_FORT_COURT,
        "sport": Sport.PADEL,
        "business_day_start_hour": BUSINESS_DAY_START_HOUR,
        "tz": TZ,
    }
    before = parse_slot(raw, observed_at=dt.datetime(2026, 9, 11, 0, 0, tzinfo=dt.UTC), **kwargs)
    after = parse_slot(raw, observed_at=OBSERVED_AT, **kwargs)

    assert before.is_past is False
    assert after.is_past is True
    assert before.state is after.state is SlotState.OPEN


# --------------------------------------------------------------------------
# The blocked evening -- zero denominator must be None, not 0%
# --------------------------------------------------------------------------


def test_fully_blocked_evening_has_no_strict_occupancy(
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: a zero sellable denominator must yield None, not 0.0 or a crash.

    Padel Fort pulled 2026-09-13 17:00-23:30 -- 14 consecutive slots, 420
    court-minutes -- from inventory with zero bookings. Reporting that evening
    as 0% occupancy says "nobody wanted it" about time nobody could buy, and
    dividing anyway raises ZeroDivisionError. The blocked minutes must also be
    reported in their own right, otherwise an offline-selling venue is
    indistinguishable from an empty one.
    """
    evening = [
        o
        for o in fort_observations
        if o.business_date == SEPT_13 and int(o.slot_start_local[11:13]) >= 17
    ]
    assert len(evening) == 14
    assert {o.state for o in evening} == {SlotState.BLOCKED}

    strict = occupancy_strict(evening)
    assert strict == Occupancy(ratio=None, numerator_minutes=0, denominator_minutes=0)

    gross = occupancy_gross(evening)
    assert gross.ratio == 1.0
    assert (gross.numerator_minutes, gross.denominator_minutes) == (420, 420)

    assert court_minutes(evening, state=SlotState.BLOCKED) == 420
    assert court_minutes_by_state(evening)[SlotState.BLOCKED] == 420


def test_blocked_evening_does_not_hide_inside_the_whole_day(
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: blocked minutes must never be folded into occupancy_strict.

    Across the whole of 2026-09-13 the strict ratio is a genuine 0.0 (22 open
    slots sold nothing) while gross is 0.39 purely from blocking. If the 420
    blocked minutes leaked into the strict numerator or denominator, the day
    would read as partly sold.
    """
    day = [o for o in fort_observations if o.business_date == SEPT_13]
    assert len(day) == 36

    strict = occupancy_strict(day)
    assert strict.ratio == 0.0
    assert (strict.numerator_minutes, strict.denominator_minutes) == (0, 22 * 30)

    gross = occupancy_gross(day)
    assert gross.numerator_minutes == 420
    assert gross.denominator_minutes == 36 * 30
    assert gross.ratio == pytest.approx(420 / 1080)


def test_occupancy_of_nothing_is_none_for_both_denominators() -> None:
    """Regression: an empty day must not crash or report 0% occupancy.

    A failed fetch leaves no observations at all; both ratios have to come back
    None so the dashboard prints "no data" rather than an empty court.
    """
    assert occupancy_strict([]) == Occupancy(None, 0, 0)
    assert occupancy_gross([]) == Occupancy(None, 0, 0)
    assert court_minutes([]) == 0
    assert court_minutes_by_state([]) == dict.fromkeys(SlotState, 0)


# --------------------------------------------------------------------------
# Court-minute normalization
# --------------------------------------------------------------------------


def test_grid_minutes_differ_per_venue(
    raw_slots_padel_up: Any, raw_slots_play_padel: Any, raw_slots_padel_fort: Any
) -> None:
    """Regression: grid length must be derived per facility, never assumed global.

    Padel Up sells hours; the other two sell half-hours. Hard-coding either
    value silently doubles or halves one venue's court time.
    """
    assert grid_minutes_from_payload(raw_slots_padel_up) == 60
    assert grid_minutes_from_payload(raw_slots_play_padel) == 30
    assert grid_minutes_from_payload(raw_slots_padel_fort) == 30


def test_one_padel_up_slot_equals_two_padel_fort_slots(
    up_observations: list[SlotObservation], fort_observations: list[SlotObservation]
) -> None:
    """Regression: cross-venue comparison by slot count is always wrong.

    One 19:00 Padel Up slot and one 19:00 Padel Fort slot are both "1 slot",
    but 60 court-minutes against 30. Counting rows would rank the two venues
    as equal when Padel Up published twice the court time.
    """
    up = _by_start(up_observations)["2026-09-16 19:00:00"]
    fort = _by_start(fort_observations)["2026-09-16 19:00:00"]

    assert up.duration_minutes == 60
    assert fort.duration_minutes == 30
    assert court_minutes([up]) == 2 * court_minutes([fort])
    # Both sides are one slot, which is exactly why a slot count cannot see
    # the difference the court-minute assertion above just measured.


def test_duration_comes_from_the_slot_not_the_config(
    up_observations: list[SlotObservation], fort_observations: list[SlotObservation]
) -> None:
    """Regression: duration must be measured per slot from its own timestamps.

    Every Padel Up slot is 60 minutes and every Padel Fort slot is 30, and both
    facts have to fall out of the payload so a venue that changes its grid
    mid-collection is recorded correctly instead of at the stale config value.
    """
    assert {o.duration_minutes for o in up_observations} == {60}
    assert {o.duration_minutes for o in fort_observations} == {30}
    assert court_minutes(up_observations) == 589 * 60
    assert court_minutes(fort_observations) == 1116 * 30


def test_price_must_be_normalized_per_court_hour(
    up_observations: list[SlotObservation],
    play_observations: list[SlotObservation],
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: per-slot price ranks the venues backwards.

    Play Padel's 1000 per 30-minute slot looks cheaper than Padel Up's 1800,
    yet it is the most expensive court in the city at 2000 per court-hour
    against Padel Up's 1800. Any price comparison that skips the duration
    inverts the ranking.
    """

    def per_hour(observation: SlotObservation) -> float:
        assert observation.price is not None
        return observation.price * 60 / observation.duration_minutes

    up, play, fort = up_observations[0], play_observations[0], fort_observations[0]
    assert (up.price, play.price, fort.price) == (1800.0, 1000.0, 900.0)
    assert play.price is not None and up.price is not None
    assert play.price < up.price, "per slot, Play Padel looks cheaper than Padel Up"
    assert per_hour(play) == 2000.0
    assert per_hour(up) == per_hour(fort) == 1800.0
    assert per_hour(play) > per_hour(up), "per court-hour the ranking flips"
    assert per_hour(fort) < per_hour(play), "Padel Fort is the cheapest court time"


# --------------------------------------------------------------------------
# Play Padel's non-contiguous window and the midnight wrap
# --------------------------------------------------------------------------


def test_play_padel_window_is_non_contiguous(play_observations: list[SlotObservation]) -> None:
    """Regression: a gap in the selling window is not a grid change or a hole.

    Play Padel sells 00:00-01:30 and then 06:00-23:30: 40 slots a day with a
    4.5-hour hole in the middle. Inferring the grid from the distance between
    consecutive slots reports 270 minutes across that gap, and treating the
    gap as missing data invents phantom unsold inventory.
    """
    per_day = Counter(o.slot_start_local[:10] for o in play_observations)
    assert len(per_day) == 31
    assert set(per_day.values()) == {40}

    starts = sorted({o.slot_start_local[11:16] for o in play_observations})
    expected = [f"{h:02d}:{m:02d}" for h in (0, 1) for m in (0, 30)]
    expected += [f"{h:02d}:{m:02d}" for h in range(6, 24) for m in (0, 30)]
    assert starts == sorted(expected)
    assert "02:00" not in starts and "05:30" not in starts


def test_slot_crossing_midnight_is_thirty_minutes(
    play_observations: list[SlotObservation], fort_observations: list[SlotObservation]
) -> None:
    """Regression: naive end-minus-start makes the 23:30 slot -1410 minutes.

    The last slot of the day runs ``23:30 -> 00:00`` on the *next* date. A
    negative duration trips the ``duration_minutes > 0`` CHECK constraint, and
    absolute value or a 1440 wrap would record it as a whole day.
    """
    play = _by_start(play_observations)["2026-09-12 23:30:00"]
    assert play.slot_end_local == "2026-09-13 00:00:00"
    assert play.duration_minutes == 30
    assert play.state is SlotState.BOOKED

    fort = _by_start(fort_observations)["2026-09-13 23:30:00"]
    assert fort.slot_end_local == "2026-09-14 00:00:00"
    assert fort.duration_minutes == 30


def test_grid_minutes_ignores_a_degenerate_wrap_timing() -> None:
    """Regression: a ``00:00 -> 00:00`` timing must not be read as 1440 or -0.

    A facility whose grid straddles midnight can publish a timing whose from
    and to are identical. Wrapping it blindly reports a 24-hour slot, which
    would make every court-minute figure meaningless.
    """
    payload = {
        "data": {
            "slot_timings": [
                {"from": "00:00:00", "to": "00:00:00"},
                {"from": "06:00:00", "to": "06:30:00"},
                {"from": "23:30:00", "to": "00:00:00"},
            ]
        }
    }
    assert grid_minutes_from_payload(payload) == 30
    assert grid_minutes_from_payload({"data": {"slot_timings": []}}) is None
    assert grid_minutes_from_payload({"data": {}}) is None
    assert grid_minutes_from_payload({}) is None


def test_grid_minutes_takes_the_dominant_length() -> None:
    """Regression: one odd timing must not redefine a facility's grid.

    A single 15-minute maintenance timing among half-hours is an exception, not
    the grid. Taking the minimum or the first entry would halve every
    normalized figure for the venue.
    """
    payload = {
        "data": {
            "slot_timings": [
                {"from": "06:00:00", "to": "06:15:00"},
                {"from": "07:00:00", "to": "07:30:00"},
                {"from": "07:30:00", "to": "08:00:00"},
                {"from": "08:00:00", "to": "08:30:00"},
            ]
        }
    }
    assert grid_minutes_from_payload(payload) == 30


# --------------------------------------------------------------------------
# Business date
# --------------------------------------------------------------------------


def test_post_midnight_slot_rolls_back_to_the_previous_business_date(
    play_observations: list[SlotObservation],
) -> None:
    """Regression: Friday-night demand must not be attributed to Saturday.

    Hudle stamps Play Padel's 00:30 slot with the calendar date it falls on, so
    the two real bookings at 00:30 and 01:00 on Saturday 2026-09-12 are Friday
    evening sessions. Aggregating on the raw local date moves them to the wrong
    trading day and the wrong day of week. The wall-clock columns must stay
    untouched.
    """
    by_start = _by_start(play_observations)
    midnight = by_start["2026-09-12 00:30:00"]

    assert midnight.slot_start_local == "2026-09-12 00:30:00", "wall clock not rewritten"
    assert midnight.business_date == dt.date(2026, 9, 11)
    assert midnight.state is SlotState.BOOKED
    assert by_start["2026-09-12 01:00:00"].business_date == dt.date(2026, 9, 11)

    morning = by_start["2026-09-12 06:00:00"]
    assert morning.business_date == dt.date(2026, 9, 12), "06:00 stays on its own date"

    rolled = {o.slot_start_local for o in play_observations if o.business_date == SEPT_11}
    assert "2026-09-12 00:00:00" in rolled
    assert "2026-09-12 01:30:00" in rolled
    assert "2026-09-12 06:00:00" not in rolled


def test_days_ahead_uses_the_local_calendar_date(
    play_observations: list[SlotObservation],
) -> None:
    """Regression: days_ahead must keep the one repo-wide definition.

    ``tracker.types.days_ahead_for`` differences raw local calendar dates, so a
    00:30 slot seen the previous evening is one day ahead even though its
    business_date is that same evening. Redefining it here on business_date
    would silently disagree with every other producer of the column.
    """
    midnight = _by_start(play_observations)["2026-09-12 00:30:00"]
    assert midnight.business_date == SEPT_11
    assert midnight.days_ahead == 1
    assert _by_start(play_observations)["2026-09-11 19:00:00"].days_ahead == 0


# --------------------------------------------------------------------------
# Timezone discipline
# --------------------------------------------------------------------------


def test_no_naive_datetime_escapes_parse_slot(fort_observations: list[SlotObservation]) -> None:
    """Regression: a naive datetime crossing this boundary corrupts every UTC column.

    Local wall-clock stays a string next to an explicit tz; the only datetime
    on the record is aware UTC. 19:00 IST is 13:30 UTC, so a missing conversion
    is a 5.5-hour error in every lead time.
    """
    observation = _by_start(fort_observations)["2026-09-11 19:00:00"]

    assert observation.tz == TZ
    assert observation.slot_start_utc.tzinfo is not None
    assert observation.slot_start_utc == dt.datetime(2026, 9, 11, 13, 30, tzinfo=dt.UTC)
    assert isinstance(observation.slot_start_local, str)
    assert isinstance(observation.slot_end_local, str)


def test_naive_observed_at_is_rejected() -> None:
    """Regression: a naive observed_at would make is_past a coin flip.

    Comparing a naive local instant against an aware UTC slot start raises at
    best and is off by the UTC offset at worst, so it is refused at the edge.
    """
    with pytest.raises(SlotParseError, match="timezone-aware"):
        parse_slot(
            _raw_slot(),
            snapshot_id=1,
            observed_at=dt.datetime(2026, 9, 11, 16, 21),
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            sport=Sport.PADEL,
            business_day_start_hour=BUSINESS_DAY_START_HOUR,
            tz=TZ,
        )


# --------------------------------------------------------------------------
# total_count, and a facility with more than one court
# --------------------------------------------------------------------------


def test_total_count_is_one_everywhere_in_the_fixtures(
    up_observations: list[SlotObservation],
    play_observations: list[SlotObservation],
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: guards the assumption that occupancy is boolean per slot.

    Across all 2945 recorded slots ``total_count`` is 1, which is the only
    reason ``available_count == 0`` currently coincides with BOOKED. If a
    multi-court facility ever appears this test fails first, before the
    occupancy maths starts lying.
    """
    everything = [*up_observations, *play_observations, *fort_observations]
    assert len(everything) == 2945
    assert {o.total_count for o in everything} == {1}
    assert {o.available_count for o in everything if o.state is SlotState.BOOKED} == {0}
    assert {o.available_count for o in everything if o.state is not SlotState.BOOKED} == {1}


def test_multi_court_row_keeps_its_raw_counts_and_flag_based_state() -> None:
    """Regression: state must come from the flags, never from available_count.

    A facility with two courts and one sold reports ``total_count: 2,
    available_count: 1`` while still being bookable, and a partially sold slot
    would read ``available_count: 0`` only when fully sold. Deriving BOOKED
    from ``available_count == 0`` breaks the moment such a facility appears,
    and the raw counts must survive so the breakage is visible.
    """
    partly_sold = _parse_one(total_count=2, available_count=1)
    assert partly_sold.state is SlotState.OPEN
    assert (partly_sold.total_count, partly_sold.available_count) == (2, 1)

    counted_out = _parse_one(total_count=2, available_count=0)
    assert counted_out.state is SlotState.OPEN, "flags say bookable; the count does not decide"

    genuinely_booked = _parse_one(total_count=2, available_count=0, is_booked=True)
    assert genuinely_booked.state is SlotState.BOOKED
    assert genuinely_booked.total_count == 2


def test_a_multi_court_facility_announces_itself_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: the total_count == 1 assumption breaking silently.

    Occupancy is boolean per slot and ``court_minutes`` ignores capacity, both
    of which are only correct because every one of the 2945 recorded slots
    reports ``total_count: 1``. A fixture test cannot catch a live payload that
    breaks that, so a slot with any other capacity has to say so at parse time
    -- otherwise a two-court facility silently halves its reported inventory
    and nothing anywhere reports a number that looks wrong.
    """
    with caplog.at_level(logging.WARNING, logger="tracker.classify"):
        _parse_one(total_count=2, available_count=1)
    assert [record.message for record in caplog.records] == ["slot_total_count_not_one"]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tracker.classify"):
        _parse_one()
    assert caplog.records == []


# --------------------------------------------------------------------------
# Padel Up: a real 0% venue that must not look broken
# --------------------------------------------------------------------------


def test_padel_up_zero_occupancy_is_zero_not_none(up_observations: list[SlotObservation]) -> None:
    """Regression: a genuinely unsold venue must be distinguishable from no data.

    Padel Up took zero bookings across 31 days at the highest price of the
    three, and blocks exactly one slot per day (05:00, a venue rule, not
    demand). Its strict occupancy is an exact 0.0 over a real 33480-minute
    denominator -- reporting None there would look like a broken collector, and
    reporting the 31 blocked slots as demand signal would be wrong.
    """
    counts = Counter(o.state for o in up_observations)
    assert counts[SlotState.BOOKED] == 0
    assert counts[SlotState.BLOCKED] == 31

    blocked = [o for o in up_observations if o.state is SlotState.BLOCKED]
    blocked_starts = {o.slot_start_local[11:] for o in blocked}
    assert blocked_starts == {"05:00:00"}, "one venue-rule slot per day, not demand"

    strict = occupancy_strict(up_observations)
    assert strict.ratio == 0.0
    assert strict.ratio is not None
    assert (strict.numerator_minutes, strict.denominator_minutes) == (0, 558 * 60)

    gross = occupancy_gross(up_observations)
    assert gross.numerator_minutes == 31 * 60
    assert gross.ratio == pytest.approx(31 / 589)


# --------------------------------------------------------------------------
# Whole-fixture state tallies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("venue", ["padel_up", "play_padel", "padel_fort"])
def test_state_counts_match_the_verified_numbers(
    venue: str,
    up_observations: list[SlotObservation],
    play_observations: list[SlotObservation],
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: pins the classifier against hand-verified fixture tallies.

    Any change to the state rules -- folding pastness in, preferring
    availability, inferring from counts -- moves at least one of these nine
    numbers. Padel Up 589 (0/31/558), Play Padel 1240 (21/0/1219), Padel Fort
    1116 (6/18/1092).
    """
    observations = {
        "padel_up": up_observations,
        "play_padel": play_observations,
        "padel_fort": fort_observations,
    }[venue]
    expected = EXPECTED_COUNTS[venue]
    counts = Counter(o.state for o in observations)

    assert len(observations) == expected["total"]
    for state in SlotState:
        assert counts[state] == expected[state], f"{venue} {state}"


# --------------------------------------------------------------------------
# Grid walking
# --------------------------------------------------------------------------


def test_parse_slot_grid_skips_empty_and_slotless_days() -> None:
    """Regression: an empty day must contribute nothing and must not raise.

    Hudle returns ``is_empty: true`` for a day with no published inventory, and
    a day can arrive without a ``slots`` key at all. Either one must yield no
    observations rather than a KeyError that loses the whole poll, since a
    dropped poll can never be recovered.
    """
    payload = {
        "data": {
            "slot_data": [
                {"date": "2026-09-20", "is_empty": True, "slots": []},
                {"date": "2026-09-21", "is_empty": False},
                {"date": "2026-09-22", "is_empty": False, "slots": None},
                {
                    "date": "2026-09-23",
                    "is_empty": False,
                    "slots": [
                        _raw_slot(
                            start_time="2026-09-23 19:00:00",
                            end_time="2026-09-23 19:30:00",
                        )
                    ],
                },
            ]
        }
    }
    observations = _parse(payload, PADEL_FORT_VENUE, PADEL_FORT_COURT)
    assert len(observations) == 1
    assert observations[0].slot_start_local == "2026-09-23 19:00:00"

    assert _parse({"success": False, "data": None}, PADEL_FORT_VENUE, PADEL_FORT_COURT) == []
    assert _parse({}, PADEL_FORT_VENUE, PADEL_FORT_COURT) == []


def test_parse_slot_grid_covers_every_day_of_the_horizon(
    fort_observations: list[SlotObservation],
) -> None:
    """Regression: the walker must not drop a day or a slot of a 31-day range.

    A 31-day request returns 31 nested day objects; an off-by-one in the walk
    quietly loses a day of irreplaceable inventory.
    """
    per_day = Counter(o.slot_start_local[:10] for o in fort_observations)
    assert len(per_day) == 31
    assert set(per_day.values()) == {36}
    assert min(per_day) == "2026-09-11"
    assert max(per_day) == "2026-10-11"


def test_slot_from_another_facility_is_rejected() -> None:
    """Regression: mixing two facilities' grids corrupts normalization silently.

    Hudle echoes ``facility_uuid`` on every slot. If a linked-facility row ever
    arrives in another court's grid, attributing it to the requested court
    would mix a 60-minute grid into a 30-minute one and no later check could
    detect it.
    """
    with pytest.raises(SlotParseError, match="not"):
        parse_slot(
            _raw_slot(facility_uuid=PADEL_UP_COURT),
            snapshot_id=1,
            observed_at=OBSERVED_AT,
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            sport=Sport.PADEL,
            business_day_start_hour=BUSINESS_DAY_START_HOUR,
            tz=TZ,
        )


def test_missing_required_key_names_itself() -> None:
    """Regression: a malformed slot must fail loudly, naming the key.

    Skipping unparseable rows would leave a silent hole in a forward-only
    dataset; the error has to say which key was absent so the payload change
    can be diagnosed from the log alone.
    """
    raw = _raw_slot()
    del raw["is_booked"]
    with pytest.raises(SlotParseError, match="is_booked"):
        parse_slot(
            raw,
            snapshot_id=1,
            observed_at=OBSERVED_AT,
            venue_uuid=PADEL_FORT_VENUE,
            facility_uuid=PADEL_FORT_COURT,
            sport=Sport.PADEL,
            business_day_start_hour=BUSINESS_DAY_START_HOUR,
            tz=TZ,
        )


def test_price_is_parsed_from_its_string_form_and_may_be_absent() -> None:
    """Regression: Hudle sends price as the string "900.00", not a number.

    Storing the string makes every per-court-hour computation a TypeError, and
    a missing price must be None rather than 0.0, which would read as a free
    court.
    """
    assert _parse_one(price="900.00").price == 900.0
    assert _parse_one(price=None).price is None
    assert _parse_one(price="").price is None
