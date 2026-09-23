"""Shared test harness.

Nothing in this file touches the network. The live Hudle API is off limits to
the test suite: every fixture is either a recorded response under
``fixtures/raw/`` or built inside the test that needs it.

Every timestamp in this module is a literal. ``datetime.now()`` is never called,
so the suite is reproducible on any machine at any hour.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tracker.config import Config, load_config

#: Stand-ins for the Hudle client credentials, which config.yaml reads from the
#: environment. Set at import so every config load in the suite resolves; the
#: real values never reach a test, and no test calls Hudle.
TEST_HUDLE_API_SECRET = "test-api-secret"
TEST_HUDLE_APP_ID = "test-app-id"
os.environ.setdefault("HUDLE_API_SECRET", TEST_HUDLE_API_SECRET)
os.environ.setdefault("HUDLE_APP_ID", TEST_HUDLE_APP_ID)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "fixtures" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Venue / facility identities, straight from the frozen config.
PADEL_UP_VENUE = "e606e880-0b2c-4c69-b1a8-193c8f915328"
PADEL_UP_COURT = "e27518b2-9cee-49a9-aa6f-8685e5c543f3"
PLAY_PADEL_VENUE = "9b288765-eee9-4d8a-b309-a4f09b11abcc"
PLAY_PADEL_COURT = "f40a05d6-e336-43be-bdc6-28405176ed9c"
PADEL_FORT_VENUE = "e161ebf7-78c7-4a45-bad8-49841f38b18a"
PADEL_FORT_COURT = "e03fdd0f-f8d8-4bb4-b707-d64a7244036f"


# --------------------------------------------------------------------------
# Recorded API responses
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def load_fixture() -> Callable[[str], Any]:
    """Load a recorded response by file stem or filename."""

    def _load(name: str) -> Any:
        candidate = FIXTURE_DIR / (name if name.endswith(".json") else f"{name}.json")
        if not candidate.is_file():
            available = sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
            raise FileNotFoundError(f"no fixture {name!r}; available: {available}")
        return json.loads(candidate.read_text(encoding="utf-8"))

    return _load


@pytest.fixture(scope="session")
def raw_slots_padel_up(load_fixture: Callable[[str], Any]) -> Any:
    """589 slots, 60-min grid, 0 BOOKED / 31 BLOCKED / 558 OPEN."""
    return load_fixture("slots_padel_up_31d")


@pytest.fixture(scope="session")
def raw_slots_play_padel(load_fixture: Callable[[str], Any]) -> Any:
    """1240 slots, 30-min grid, 21 BOOKED / 0 BLOCKED / 1219 OPEN."""
    return load_fixture("slots_play_padel_31d")


@pytest.fixture(scope="session")
def raw_slots_padel_fort(load_fixture: Callable[[str], Any]) -> Any:
    """1116 slots, 30-min grid, 6 BOOKED / 18 BLOCKED / 1092 OPEN."""
    return load_fixture("slots_padel_fort_31d")


@pytest.fixture(scope="session")
def raw_venue_search_padel(load_fixture: Callable[[str], Any]) -> Any:
    """Exactly 3 venues, flat ``data[]`` plus a ``meta{}`` block."""
    return load_fixture("venue_search_padel")


@pytest.fixture(scope="session")
def raw_venue_search_pickleball(load_fixture: Callable[[str], Any]) -> Any:
    """50 of 57 venues: proves a single call silently truncates."""
    return load_fixture("venue_search_pickleball")


@pytest.fixture(scope="session")
def raw_next_data() -> dict[str, Any]:
    """The SSR ``venueDetails`` objects, already unwrapped, keyed by short name."""
    return {
        key: json.loads(
            (FIXTURE_DIR / f"next_data_venue_details_{key}.json").read_text(encoding="utf-8")
        )
        for key in ("padel_up", "play_padel", "padel_fort")
    }


@pytest.fixture(scope="session")
def test_config() -> Config:
    """The real, frozen ``config.yaml``."""
    return load_config(CONFIG_PATH)
