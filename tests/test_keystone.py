"""Tests for the keystone layer: config loader, schema, types, test harness.

Each test names the regression it protects. ``tests/test_config.py`` is
reserved for another agent, so the config-loader tests live here.
"""

from __future__ import annotations

import datetime as dt
import os
from itertools import pairwise
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.conftest import SyntheticHistory
from tracker import schema
from tracker.config import Config, ConfigError, FacilityKind, Sport, load_config
from tracker.types import (
    SlotState,
    business_date_for,
    classify_slot_state,
    days_ahead_for,
    duration_minutes_for,
    from_utc_text,
    slot_start_utc_for,
    to_utc_text,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Config loader
# --------------------------------------------------------------------------


def test_config_loads_the_frozen_venue_tree(test_config: Config) -> None:
    """Regression: the loader must accept config.yaml exactly as frozen."""
    assert test_config.timezone == "Asia/Kolkata"
    assert test_config.business_day_start_hour == 4
    assert test_config.poll.cadence_minutes == 30
    assert test_config.poll.horizon_days == 21
    assert test_config.poll.backoff.max_attempts == 3
    assert test_config.discovery.city_id == 8
    assert test_config.discovery.sport_id(Sport.PADEL) == 44
    assert test_config.discovery.sport_id(Sport.PICKLEBALL) == 56
    assert len(test_config.venues) == 3


def test_active_courts_excludes_equipment(test_config: Config) -> None:
    """Regression: rackets and balls are facilities too. Polling them would
    burn requests and pollute occupancy with non-court inventory."""
    pairs = test_config.active_courts()
    # Padel Up is configured but inactive: 2 padel + 3 pickleball courts polled.
    assert len(pairs) == 5
    assert all(f.kind is FacilityKind.COURT for _, f in pairs)
    equipment = [
        f for v in test_config.venues for f in v.facilities if f.kind is FacilityKind.EQUIPMENT
    ]
    assert len(equipment) == 5
    assert all(not f.active for f in equipment)


def test_courts_for_sport_returns_the_two_tracked_padel_courts(test_config: Config) -> None:
    """Regression: the padel set is exactly three courts, one per venue."""
    padel = test_config.courts_for_sport(Sport.PADEL)
    assert len(padel) == 2
    assert {v.short_name for v, _ in padel} == {"Play Padel", "Padel Fort"}


def test_price_per_slot_is_derived_from_the_per_hour_price(test_config: Config) -> None:
    """Regression: Play Padel's 1000 per slot is the MOST expensive per court
    hour (2000) even though it is the smallest per-slot number. Config stores
    only the per-hour figure so nothing compares per-slot prices by accident."""
    by_venue = {v.short_name: f for v, f in test_config.courts_for_sport(Sport.PADEL)}
    assert by_venue["Play Padel"].price_per_court_hour == 2000
    assert by_venue["Play Padel"].price_per_slot == 1000.0
    assert by_venue["Padel Fort"].price_per_court_hour == 1800
    assert by_venue["Padel Fort"].price_per_slot == 900.0


def test_unprobed_pickleball_courts_keep_null_grid_and_price(test_config: Config) -> None:
    """Regression: absent grid_minutes/price keys must load as None, not 0."""
    pickleball = test_config.courts_for_sport(Sport.PICKLEBALL)
    assert len(pickleball) == 3
    assert all(f.grid_minutes is None for _, f in pickleball)
    assert all(f.price_per_slot is None for _, f in pickleball)


def test_facility_by_uuid_finds_the_owning_venue(test_config: Config) -> None:
    """Regression: matching is by UUID only; Play Padel was renamed already."""
    found = test_config.facility_by_uuid("f40a05d6-e336-43be-bdc6-28405176ed9c")
    assert found is not None
    venue, facility = found
    assert venue.short_name == "Play Padel"
    assert facility.grid_minutes == 30
    assert venue.ssr_path == "/venues/pickleball-by-play-padel-clarks-amer-hotel/417565"


def test_request_headers_carry_the_api_secret_and_user_agent(test_config: Config) -> None:
    """Regression: the API rejects requests without Api-Secret."""
    headers = test_config.http.request_headers()
    assert headers["Api-Secret"] == os.environ["HUDLE_API_SECRET"]
    assert headers["User-Agent"].startswith("Mozilla/5.0")


def test_expected_snapshots_per_day_follows_cadence(test_config: Config) -> None:
    """Regression: coverage must be derived from cadence, not hard-coded."""
    assert test_config.poll.expected_snapshots_per_day == 48


def test_missing_key_names_its_path(tmp_path: Path) -> None:
    """Regression: a silently defaulted config key is worse than a crash."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("timezone: Asia/Kolkata\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"missing required config key: <root>\.poll"):
        load_config(broken)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "absent.yaml")


def test_unknown_enum_value_names_its_path(tmp_path: Path) -> None:
    source = (PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")
    broken = tmp_path / "broken.yaml"
    broken.write_text(source.replace("kind: court", "kind: pitch", 1), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"venues\[0\]\.facilities\[0\]\.kind"):
        load_config(broken)


def test_example_config_matches_the_live_config() -> None:
    """Regression: config.example.yaml drifting from config.yaml means a fresh
    clone cannot reproduce the frozen venue tree."""
    live = load_config(PROJECT_ROOT / "config.yaml")
    example = load_config(PROJECT_ROOT / "config.example.yaml")
    assert live == example


# --------------------------------------------------------------------------
# Types: the load-bearing derivations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_available", "is_booked", "expected"),
    [
        (True, False, SlotState.OPEN),
        (False, False, SlotState.BLOCKED),
        (True, True, SlotState.BOOKED),
        # is_booked wins over is_available.
        (False, True, SlotState.BOOKED),
    ],
)
def test_classify_slot_state(is_available: bool, is_booked: bool, expected: SlotState) -> None:
    """Regression: is_booked must win. A slot reporting both is a paid booking."""
    assert classify_slot_state(is_available=is_available, is_booked=is_booked) is expected


def test_business_date_rolls_pre_dawn_slots_back_one_day() -> None:
    """Regression: Play Padel's 00:30 Saturday slot is Friday-night demand."""
    assert business_date_for(dt.datetime(2026, 9, 12, 0, 30), 4) == dt.date(2026, 9, 11)
    assert business_date_for(dt.datetime(2026, 9, 12, 1, 30), 4) == dt.date(2026, 9, 11)
    assert business_date_for(dt.datetime(2026, 9, 12, 4, 0), 4) == dt.date(2026, 9, 12)
    assert business_date_for(dt.datetime(2026, 9, 12, 19, 0), 4) == dt.date(2026, 9, 12)


def test_duration_minutes_handles_a_midnight_crossing_end() -> None:
    """Regression: Padel Fort's 23:30 slot ends at 00:00 the next day; a naive
    subtraction gives a negative or zero duration and breaks the CHECK."""
    assert (
        duration_minutes_for(dt.datetime(2026, 9, 13, 23, 30), dt.datetime(2026, 9, 14, 0, 0)) == 30
    )
    assert (
        duration_minutes_for(dt.datetime(2026, 9, 13, 23, 30), dt.datetime(2026, 9, 13, 0, 0)) == 30
    )
    assert (
        duration_minutes_for(dt.datetime(2026, 9, 16, 19, 0), dt.datetime(2026, 9, 16, 20, 0)) == 60
    )


def test_slot_start_utc_applies_the_ist_offset() -> None:
    """Regression: Hudle times are naive local; reading them as UTC shifts every
    lead time by 5h30m."""
    assert slot_start_utc_for(dt.datetime(2026, 9, 14, 19, 0), "Asia/Kolkata") == dt.datetime(
        2026, 9, 14, 13, 30, tzinfo=dt.UTC
    )


def test_days_ahead_uses_local_dates_not_utc() -> None:
    """Regression: at 21:00 IST the UTC date is still the previous day, so a
    UTC-based days_ahead is off by one every evening."""
    observed = dt.datetime(2026, 9, 11, 15, 30, tzinfo=dt.UTC)  # 21:00 IST
    assert days_ahead_for(dt.datetime(2026, 9, 11, 22, 0), observed, "Asia/Kolkata") == 0
    assert days_ahead_for(dt.datetime(2026, 9, 14, 19, 0), observed, "Asia/Kolkata") == 3


def test_utc_text_round_trips() -> None:
    value = dt.datetime(2026, 9, 11, 14, 30, tzinfo=dt.UTC)
    assert to_utc_text(value) == "2026-09-11T14:30:00Z"
    assert from_utc_text(to_utc_text(value)) == value


def test_to_utc_text_rejects_a_naive_datetime() -> None:
    """Regression: a naive datetime must never cross the storage boundary."""
    with pytest.raises(ValueError, match="naive datetime"):
        to_utc_text(dt.datetime(2026, 9, 11, 14, 30))


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_every_expected_table_is_defined() -> None:
    assert sorted(schema.metadata.tables) == [
        "facilities",
        "facility_discovery_log",
        "facility_fetches",
        "slot_first_booked",
        "slot_observations",
        "slot_state_transitions",
        "snapshots",
        "venue_name_history",
        "venues",
    ]


def test_slot_observations_is_indexed_for_the_real_query_patterns() -> None:
    """Regression: analytics scans tens of millions of rows; a missing index
    here turns a dashboard load into a full table scan.

    The business_date-leading index is the one the reduced read needs: every
    other index here leads with a different column, so a query filtering on
    business_date alone -- which is what the dashboard does -- scanned the
    whole table and then sorted it.
    """
    names = {ix.name for ix in schema.slot_observations.indexes}
    assert names == {
        "ix_slot_obs_business_date_slot_snapshot",
        "ix_slot_obs_facility_business_date",
        "ix_slot_obs_slot_snapshot",
        "ix_slot_obs_state_business_date",
        "ix_slot_obs_sport_business_date",
    }


def test_schema_creates_and_views_aggregate_court_minutes() -> None:
    """Regression: a view that counts slots instead of summing court-minutes
    makes a 60-min grid look the same as a 30-min one."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        schema.metadata.create_all(conn)
        for sql in schema.VIEW_SQL:
            conn.execute(sa.text(sql))

        conn.execute(
            sa.text(
                "INSERT INTO snapshots (snapshot_id, poll_key, observed_at, ok, "
                "horizon_days) VALUES (1, 'k', '2026-09-11T10:00:00Z', 1, 21)"
            )
        )
        rows = [
            # 60-min OPEN at Padel Up, 30-min OPEN at Padel Fort: same hour,
            # same business date, different court-minutes.
            ("up-1", "v-up", "f-up", 60, "OPEN"),
            ("fort-1", "v-fort", "f-fort", 30, "OPEN"),
            ("fort-2", "v-fort", "f-fort", 30, "BOOKED"),
            ("fort-3", "v-fort", "f-fort", 30, "BLOCKED"),
        ]
        for slot_uuid, venue, facility, minutes, state in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO slot_observations (snapshot_id, slot_uuid, venue_uuid, "
                    "facility_uuid, sport, slot_start_local, slot_end_local, tz, "
                    "slot_start_utc, duration_minutes, price, total_count, available_count, "
                    "is_available, is_booked, state, days_ahead, business_date, is_past) VALUES "
                    "(1, :slot, :venue, :facility, 'padel', '2026-09-16 19:00:00', "
                    "'2026-09-16 20:00:00', 'Asia/Kolkata', '2026-09-16T13:30:00Z', "
                    ":minutes, 900, 1, 1, 1, 0, :state, 5, '2026-09-16', 0)"
                ),
                {
                    "slot": slot_uuid,
                    "venue": venue,
                    "facility": facility,
                    "minutes": minutes,
                    "state": state,
                },
            )

        minutes_by_facility = {
            row.facility_uuid: row.court_minutes
            for row in conn.execute(
                sa.text(
                    "SELECT facility_uuid, court_minutes FROM v_court_minutes_daily "
                    "WHERE state = 'OPEN'"
                )
            )
        }
        assert minutes_by_facility == {"f-up": 60, "f-fort": 30}

        fort = conn.execute(
            sa.text("SELECT * FROM v_occupancy_daily WHERE facility_uuid = 'f-fort'")
        ).one()
        assert fort.booked_minutes == 30
        assert fort.open_minutes == 30
        assert fort.blocked_minutes == 30
        assert fort.total_minutes == 90
        # Strict excludes blocked from the denominator; gross counts it as
        # unavailable to a walk-up.
        assert fort.occupancy_strict == pytest.approx(0.5)
        assert fort.occupancy_gross == pytest.approx(2 / 3)
        assert fort.blocked_share == pytest.approx(1 / 3)


def test_state_check_constraint_rejects_an_unknown_state() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        schema.metadata.create_all(conn)
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                schema.slot_observations.insert().values(
                    snapshot_id=1,
                    slot_uuid="s",
                    venue_uuid="v",
                    facility_uuid="f",
                    slot_start_local="2026-09-16 19:00:00",
                    slot_end_local="2026-09-16 19:30:00",
                    tz="Asia/Kolkata",
                    slot_start_utc="2026-09-16T13:30:00Z",
                    duration_minutes=30,
                    price=900,
                    total_count=1,
                    available_count=1,
                    is_available=True,
                    is_booked=False,
                    state="PAST",
                    days_ahead=5,
                    business_date="2026-09-16",
                    is_past=False,
                )
            )


# --------------------------------------------------------------------------
# The synthetic history harness
# --------------------------------------------------------------------------


def test_history_has_ten_snapshots_and_one_ninety_minute_gap(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: coverage logic needs a real hole to detect, and nothing may
    interpolate across it."""
    assert len(synthetic_history.snapshots) == 10
    times = [s.observed_at for s in synthetic_history.snapshots]
    assert times == sorted(times)
    gaps = [int((b - a).total_seconds() // 60) for a, b in pairwise(times)]
    assert gaps == [30, 30, 30, 30, 30, 30, 30, 90, 30]
    assert synthetic_history.gap_start == dt.datetime(2026, 9, 11, 13, 30, tzinfo=dt.UTC)
    assert synthetic_history.gap_end == dt.datetime(2026, 9, 11, 15, 0, tzinfo=dt.UTC)
    assert not any(synthetic_history.gap_start < t < synthetic_history.gap_end for t in times)


def test_poll_keys_are_unique_and_cadence_aligned(
    synthetic_history: SyntheticHistory,
) -> None:
    keys = [s.poll_key for s in synthetic_history.snapshots]
    assert len(set(keys)) == len(keys)
    assert all(k.endswith((":00:00Z", ":30:00Z")) for k in keys)


def test_scripted_trajectories_are_exactly_as_documented(
    synthetic_history: SyntheticHistory,
) -> None:
    h = synthetic_history
    O, B, X = SlotState.OPEN, SlotState.BOOKED, SlotState.BLOCKED  # noqa: E741
    assert h.trajectory(h.normal_slot_uuid) == [O] * 6 + [B] * 4
    assert h.trajectory(h.cancellation_slot_uuid) == [O, O, O, B, B, B, O, O, O, O]
    assert h.trajectory(h.rebooked_slot_uuid) == [O, O, B, B, O, O, B, B, B, B]
    assert h.trajectory(h.open_to_blocked_slot_uuid) == [O] * 4 + [X] * 6
    for slot_uuid in h.censored_slot_uuids:
        assert h.trajectory(slot_uuid) == [B] * 10
    for slot_uuid in h.blocked_evening_slot_uuids:
        assert h.trajectory(slot_uuid) == [X] * 10


def test_normal_booking_lead_time_is_exactly_seventy_two_and_a_half_hours(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: lead time must be slot_start_utc minus the FIRST snapshot
    that saw BOOKED, with the poll gap carried as uncertainty."""
    h = synthetic_history
    booked = [o for o in h.for_slot(h.normal_slot_uuid) if o.state is SlotState.BOOKED]
    first = booked[0]
    first_seen_at = next(s.observed_at for s in h.snapshots if s.snapshot_id == first.snapshot_id)
    assert first_seen_at == h.expected_first_booked_at
    lead = (first.slot_start_utc - first_seen_at).total_seconds() / 3600
    assert lead == pytest.approx(h.expected_lead_time_hours)
    assert h.expected_uncertainty_minutes == 30


def test_censored_slots_are_booked_in_their_first_observation(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a left-censored slot booked before our data began must be
    excluded from lead-time stats, not treated as a zero-lead booking."""
    h = synthetic_history
    assert len(h.censored_slot_uuids) == 2
    for slot_uuid in h.censored_slot_uuids:
        observations = h.for_slot(slot_uuid)
        assert observations[0].state is SlotState.BOOKED
        assert observations[0].snapshot_id == h.snapshots[0].snapshot_id
    # The normal booking slot must NOT be censored: it was seen OPEN first.
    assert h.normal_slot_uuid not in h.censored_slot_uuids


def test_court_minute_normalization_pair_does_not_compare_equal(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: counting slots instead of summing duration_minutes makes a
    60-min grid indistinguishable from a 30-min one."""
    h = synthetic_history
    sixty, thirty = h.normalization_slot_uuids
    a, b = h.for_slot(sixty), h.for_slot(thirty)
    assert len(a) == len(b) == 10  # equal slot counts
    assert a[0].slot_start_local[11:16] == b[0].slot_start_local[11:16] == "19:00"
    assert a[0].business_date == b[0].business_date == h.normalization_business_date
    assert (a[0].duration_minutes, b[0].duration_minutes) == h.expected_normalization_minutes
    assert a[0].duration_minutes != b[0].duration_minutes


def test_post_midnight_slot_rolls_back_to_the_previous_business_date(
    synthetic_history: SyntheticHistory,
) -> None:
    h = synthetic_history
    first = h.for_slot(h.post_midnight_slot_uuid)[0]
    assert first.slot_start_local.startswith("2026-09-12 00:30")
    assert first.business_date == h.post_midnight_business_date == dt.date(2026, 9, 11)
    assert first.business_date != h.post_midnight_local_date


def test_elapsed_slot_stays_open_and_is_only_flagged_by_is_past(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: Hudle never marks elapsed slots unavailable. Inferring
    pastness from is_available invents BLOCKED slots that do not exist."""
    h = synthetic_history
    observations = h.for_slot(h.elapsed_open_slot_uuid)
    assert all(o.state is SlotState.OPEN for o in observations)
    assert all(o.is_available and not o.is_booked for o in observations)
    assert all(o.is_past for o in observations)
    # Future slots in the same history are not flagged.
    assert not any(o.is_past for o in h.for_slot(h.normal_slot_uuid))


def test_blocked_evening_is_four_hundred_and_twenty_court_minutes(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: a whole future evening pulled from inventory with zero
    bookings must never be folded into occupancy as 'empty'."""
    h = synthetic_history
    assert len(h.blocked_evening_slot_uuids) == 14
    latest = {
        o.slot_uuid: o
        for o in h.observations
        if o.slot_uuid in h.blocked_evening_slot_uuids
        and o.snapshot_id == h.snapshots[-1].snapshot_id
    }
    assert len(latest) == 14
    assert all(o.state is SlotState.BLOCKED for o in latest.values())
    assert all(o.business_date == h.blocked_evening_business_date for o in latest.values())
    assert sum(o.duration_minutes for o in latest.values()) == 420
    assert h.expected_blocked_evening_minutes == 420


def test_every_slot_appears_in_every_snapshot(synthetic_history: SyntheticHistory) -> None:
    """Regression: a gappy synthetic history would mask real coverage bugs."""
    h = synthetic_history
    slots = {o.slot_uuid for o in h.observations}
    assert len(h.observations) == len(slots) * len(h.snapshots)
    keys = {(o.snapshot_id, o.slot_uuid) for o in h.observations}
    assert len(keys) == len(h.observations)


def test_raw_flags_agree_with_the_classified_state(
    synthetic_history: SyntheticHistory,
) -> None:
    """Regression: the harness must not produce rows a reclassification pass
    would disagree with."""
    for o in synthetic_history.observations:
        assert classify_slot_state(is_available=o.is_available, is_booked=o.is_booked) is o.state
        assert (o.available_count == 0) is (o.state is SlotState.BOOKED)
        assert o.total_count == 1


def test_recorded_fixture_state_counts_match_what_was_verified(
    raw_slots_padel_up: dict[str, object],
    raw_slots_play_padel: dict[str, object],
    raw_slots_padel_fort: dict[str, object],
) -> None:
    """Regression: the fixtures are the only stand-in for the live API. If one
    is replaced with a different capture, these counts change and every
    downstream expectation built on them silently shifts."""

    def tally(payload: dict[str, object]) -> tuple[int, dict[SlotState, int]]:
        counts = dict.fromkeys(SlotState, 0)
        total = 0
        data = payload["data"]
        assert isinstance(data, dict)
        for day in data["slot_data"]:
            for slot in day["slots"]:
                total += 1
                counts[
                    classify_slot_state(
                        is_available=slot["is_available"], is_booked=slot["is_booked"]
                    )
                ] += 1
        return total, counts

    assert tally(raw_slots_padel_up) == (
        589,
        {SlotState.BOOKED: 0, SlotState.BLOCKED: 31, SlotState.OPEN: 558},
    )
    assert tally(raw_slots_play_padel) == (
        1240,
        {SlotState.BOOKED: 21, SlotState.BLOCKED: 0, SlotState.OPEN: 1219},
    )
    assert tally(raw_slots_padel_fort) == (
        1116,
        {SlotState.BOOKED: 6, SlotState.BLOCKED: 18, SlotState.OPEN: 1092},
    )


def test_venue_search_pickleball_fixture_proves_pagination(
    raw_venue_search_padel: dict[str, object],
    raw_venue_search_pickleball: dict[str, object],
) -> None:
    """Regression: a single pickleball search call returns 50 of 57 and
    silently truncates. Discovery must paginate to exhaustion.

    Note the counters live under ``meta.pagination``, not directly on ``meta``.
    """
    padel_data = raw_venue_search_padel["data"]
    assert isinstance(padel_data, list)
    assert len(padel_data) == 3

    pickleball_data = raw_venue_search_pickleball["data"]
    meta = raw_venue_search_pickleball["meta"]
    assert isinstance(pickleball_data, list)
    assert isinstance(meta, dict)
    assert len(pickleball_data) == 50
    pagination = meta["pagination"]
    assert isinstance(pagination, dict)
    assert pagination["total"] == 57
    assert pagination["count"] == 50
    assert pagination["per_page"] == 50
    assert pagination["current_page"] == 1
