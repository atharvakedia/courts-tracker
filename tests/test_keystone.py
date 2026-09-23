"""Tests for the keystone layer: config loader, types helpers, recorded fixtures.

Each test names the regression it protects.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from tracker.config import Config, ConfigError, FacilityKind, Sport, load_config
from tracker.types import (
    business_date_for,
    duration_minutes_for,
    local_wall_clock,
    slot_start_utc_for,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Config loader
# --------------------------------------------------------------------------


def test_config_loads_the_frozen_venue_tree(test_config: Config) -> None:
    """Regression: the loader must accept config.yaml exactly as frozen."""
    assert test_config.timezone == "Asia/Kolkata"
    assert test_config.business_day_start_hour == 4
    assert test_config.poll.request_gap_seconds == 15.0
    assert test_config.poll.backoff.max_attempts == 3
    assert test_config.discovery.city_id == 8
    assert test_config.discovery.sport_id(Sport.PADEL) == 44
    assert test_config.discovery.sport_id(Sport.PICKLEBALL) == 56
    assert len(test_config.venues) == 3


def test_equipment_is_configured_but_never_active(test_config: Config) -> None:
    """Regression: rackets and balls are facilities too. Tracking them would
    burn requests and pollute occupancy with non-court inventory."""
    equipment = [
        f for v in test_config.venues for f in v.facilities if f.kind is FacilityKind.EQUIPMENT
    ]
    assert len(equipment) == 5
    assert all(not f.active for f in equipment)


def test_unprobed_pickleball_courts_keep_null_grid_and_price(test_config: Config) -> None:
    """Regression: absent grid_minutes/price keys must load as None, not 0."""
    pickleball = [
        f
        for v in test_config.venues
        for f in v.facilities
        if f.kind is FacilityKind.COURT and f.sport is Sport.PICKLEBALL
    ]
    assert len(pickleball) == 3
    assert all(f.grid_minutes is None for f in pickleball)
    assert all(f.price_per_court_hour is None for f in pickleball)


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


def test_local_wall_clock_projects_utc_onto_jaipur_time() -> None:
    """Regression: at 21:00 IST the UTC date is still the previous day, so a
    window computed from the UTC date is off by one every evening."""
    observed = dt.datetime(2026, 9, 11, 15, 30, tzinfo=dt.UTC)
    assert local_wall_clock(observed, "Asia/Kolkata") == dt.datetime(2026, 9, 11, 21, 0)
    late = dt.datetime(2026, 9, 11, 19, 0, tzinfo=dt.UTC)
    assert local_wall_clock(late, "Asia/Kolkata").date() == dt.date(2026, 9, 12)


# --------------------------------------------------------------------------
# Recorded fixtures
# --------------------------------------------------------------------------


def test_recorded_fixture_state_counts_match_what_was_verified(
    raw_slots_padel_up: dict[str, object],
    raw_slots_play_padel: dict[str, object],
    raw_slots_padel_fort: dict[str, object],
) -> None:
    """Regression: the fixtures are the only stand-in for the live API. If one
    is replaced with a different capture, these counts change and every
    downstream expectation built on them silently shifts."""

    def tally(payload: dict[str, object]) -> tuple[int, int, int, int]:
        """(slots, sold, made unavailable by the venue, open)."""
        sold = unavailable = open_ = 0
        data = payload["data"]
        assert isinstance(data, dict)
        for day in data["slot_data"]:
            for slot in day["slots"]:
                if slot["is_booked"]:
                    sold += 1
                elif not slot["is_available"]:
                    unavailable += 1
                else:
                    open_ += 1
        return sold + unavailable + open_, sold, unavailable, open_

    assert tally(raw_slots_padel_up) == (589, 0, 31, 558)
    assert tally(raw_slots_play_padel) == (1240, 21, 0, 1219)
    assert tally(raw_slots_padel_fort) == (1116, 6, 18, 1092)


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
