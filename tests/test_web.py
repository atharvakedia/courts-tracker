"""HTTP-level tests for the JSON API.

Every test drives the real application through ``fastapi.testclient`` over an
in-memory SQLite database seeded from the scripted history in
``tests/conftest.py``. Nothing here touches the network, the filesystem or the
clock: the app is built with an injected storage and an injected fixed clock,
so staleness is a deterministic number rather than whatever time it is.

Each test names the regression it catches in its docstring.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    PADEL_FORT_COURT,
    PADEL_FORT_VENUE,
    PADEL_UP_COURT,
    PADEL_UP_VENUE,
    PLAY_PADEL_VENUE,
    SyntheticHistory,
)
from tracker.config import Config
from tracker.storage import Storage
from tracker.storage_sqlite import SQLiteStorage
from tracker.web import create_app
from tracker.web.schemas import (
    METRIC_DEFINITIONS,
    BlockedEventsResponse,
    CancellationsResponse,
    CatalogResponse,
    CoverageResponse,
    FirstSlotResponse,
    HealthResponse,
    LeadTimeResponse,
    MarketShareResponse,
    MetricEnvelope,
    OccupancyDailyResponse,
    OccupancyHeatmapResponse,
    PricingByHourResponse,
    PricingTimelineResponse,
    RevenueProxyResponse,
    SelloutResponse,
    VenuesResponse,
    WeekdayWeekendResponse,
)

#: Every metric endpoint, with the model its body must validate against.
METRIC_ENDPOINTS: dict[str, type[MetricEnvelope]] = {
    "/api/venues": VenuesResponse,
    "/api/occupancy/daily": OccupancyDailyResponse,
    "/api/occupancy/heatmap": OccupancyHeatmapResponse,
    "/api/leadtime": LeadTimeResponse,
    "/api/pricing/timeline": PricingTimelineResponse,
    "/api/pricing/by-hour": PricingByHourResponse,
    "/api/market/share": MarketShareResponse,
    "/api/market/revenue-proxy": RevenueProxyResponse,
    "/api/metrics/cancellations": CancellationsResponse,
    "/api/metrics/sellout": SelloutResponse,
    "/api/metrics/first-slot": FirstSlotResponse,
    "/api/metrics/blocked-events": BlockedEventsResponse,
    "/api/metrics/weekday-weekend": WeekdayWeekendResponse,
    "/api/coverage": CoverageResponse,
}

ALL_ENDPOINTS: tuple[str, ...] = (
    *METRIC_ENDPOINTS,
    "/api/health",
    "/api/metrics/catalog",
)

#: 20 minutes after the last scripted poll: fresh at a 30-minute cadence.
FRESH_OFFSET_MINUTES = 20
#: Well past two cadences, so the collector reads as stale.
STALE_OFFSET_MINUTES = 300


def _client(config: Config, storage: Storage, now: dt.datetime) -> Iterator[TestClient]:
    app = create_app(config=config, storage=storage, clock=lambda: now)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def last_observed_at(synthetic_history: SyntheticHistory) -> dt.datetime:
    return synthetic_history.snapshots[-1].observed_at


@pytest.fixture()
def all_venues_config(test_config: Config) -> Config:
    """``config.yaml`` with every venue visible.

    The shipped config hides Padel Up from the dashboard, which is the right
    default but would silently gut the cross-venue assertions: Padel Up is the
    only 60-minute-grid venue, so hiding it removes the very asymmetry the
    court-minute normalization guards exist to catch. Tests about normalization
    use this; tests about hiding use ``client``.
    """
    return dataclasses.replace(
        test_config,
        venues=tuple(
            dataclasses.replace(venue, show_in_dashboard=True) for venue in test_config.venues
        ),
    )


@pytest.fixture()
def client(
    test_config: Config,
    synthetic_history_storage: SyntheticHistory,
    last_observed_at: dt.datetime,
) -> Iterator[TestClient]:
    """The API over the full scripted history, with a fresh clock."""
    storage = synthetic_history_storage.storage
    assert storage is not None
    yield from _client(
        test_config, storage, last_observed_at + dt.timedelta(minutes=FRESH_OFFSET_MINUTES)
    )


@pytest.fixture()
def full_client(
    all_venues_config: Config,
    synthetic_history_storage: SyntheticHistory,
    last_observed_at: dt.datetime,
) -> Iterator[TestClient]:
    """The API with no venue hidden, for cross-venue assertions."""
    storage = synthetic_history_storage.storage
    assert storage is not None
    yield from _client(
        all_venues_config, storage, last_observed_at + dt.timedelta(minutes=FRESH_OFFSET_MINUTES)
    )


@pytest.fixture()
def empty_client(all_venues_config: Config, last_observed_at: dt.datetime) -> Iterator[TestClient]:
    """The API over a database that has never been collected into.

    Built on the all-visible config so this stays a test about an empty
    database rather than quietly also becoming a test about venue hiding.
    """
    storage = SQLiteStorage("sqlite://")
    storage.initialize()
    try:
        yield from _client(all_venues_config, storage, last_observed_at)
    finally:
        storage.close()


@pytest.fixture()
def one_snapshot_client(
    test_config: Config, synthetic_history: SyntheticHistory, last_observed_at: dt.datetime
) -> Iterator[TestClient]:
    """The API on day one: a single poll, so no state change is visible yet."""
    storage = SQLiteStorage("sqlite://")
    storage.initialize()
    first = synthetic_history.snapshots[0]
    assigned = storage.create_snapshot(first.poll_key, first.observed_at, first.horizon_days)
    storage.append_observations(
        dataclasses.replace(observation, snapshot_id=assigned)
        for observation in synthetic_history.observations
        if observation.snapshot_id == first.snapshot_id
    )
    storage.finalize_snapshot(assigned, True, None, 1200)
    try:
        yield from _client(test_config, storage, first.observed_at + dt.timedelta(minutes=5))
    finally:
        storage.close()


def _get(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    response = client.get(path, params=params)
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------
# Contract: every endpoint answers, and every answer explains itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_ENDPOINTS)
def test_every_endpoint_returns_a_schema_valid_body(client: TestClient, path: str) -> None:
    """Regression: a route that 500s or returns a shape the model rejects.

    The body is re-validated against the declared response model, so a field
    renamed in analytics and not carried through here fails loudly rather than
    silently disappearing from the dashboard.
    """
    body = _get(client, path)
    if path in METRIC_ENDPOINTS:
        METRIC_ENDPOINTS[path].model_validate(body)
    elif path == "/api/health":
        HealthResponse.model_validate(body)
    else:
        CatalogResponse.model_validate(body)


@pytest.mark.parametrize("path", sorted(METRIC_ENDPOINTS))
def test_every_metric_response_carries_its_denominator_and_date_range(
    client: TestClient, path: str
) -> None:
    """Regression: a chart printing a ratio with no denominator or date range.

    The dashboard is required to state what a number was divided by and which
    business dates it covers. If the API does not supply both, the frontend
    has to invent them, and an invented denominator is always the reader's
    assumption rather than the data's.
    """
    metric = _get(client, path)["metric"]
    assert metric["numerator"].strip()
    assert metric["denominator"].strip()
    assert metric["denominator_description"].strip()
    assert metric["date_range"]["label"].strip()
    assert metric["date_range"]["business_days"] >= 1
    assert isinstance(metric["caveats"], list)
    assert metric["name"] in METRIC_DEFINITIONS


def test_the_metric_catalog_covers_every_endpoint(client: TestClient) -> None:
    """Regression: a metric the dashboard draws that the catalog cannot explain."""
    catalog = _get(client, "/api/metrics/catalog")
    catalogued = {entry["name"] for entry in catalog["metrics"]}
    served = {_get(client, path)["metric"]["name"] for path in METRIC_ENDPOINTS}
    assert served <= catalogued
    for entry in catalog["metrics"]:
        assert entry["denominator_description"].strip()


# --------------------------------------------------------------------------
# The empty and nearly-empty database
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_ENDPOINTS)
def test_an_empty_database_answers_200_with_an_explicit_reason(
    empty_client: TestClient, path: str
) -> None:
    """Regression: a fresh install 500ing, or reporting 0% occupancy.

    Before the first poll there is nothing, and this dataset cannot be
    backfilled. "No data yet" and "zero demand" are different answers and the
    API must not conflate them.
    """
    body = _get(empty_client, path)
    assert body["reason"], f"{path} gave an empty answer with no reason"
    assert "no snapshots recorded yet" in body["reason"]
    if path in METRIC_ENDPOINTS:
        # /api/venues is the one endpoint whose rows come from the reviewed
        # config rather than from observations, so it stays non-empty.
        assert body["empty"] is (path != "/api/venues")
        METRIC_ENDPOINTS[path].model_validate(body)


def test_an_empty_database_reports_no_occupancy_rather_than_zero_occupancy(
    empty_client: TestClient,
) -> None:
    """Regression: an empty denominator rendering as 0%, which reads as no demand."""
    body = _get(empty_client, "/api/occupancy/daily")
    assert body["rows"] == []
    assert body["metric"]["date_range"]["start"] is None
    assert body["metric"]["date_range"]["label"] == "no data"


def test_an_empty_database_still_lists_the_configured_venues(empty_client: TestClient) -> None:
    """Regression: a venue list that is empty until the collector has run.

    The venue set is frozen in config and reviewed by a human; it does not
    depend on whether a poll has happened. Only the data-quality flags do.
    """
    body = _get(empty_client, "/api/venues")
    assert [venue["venue_uuid"] for venue in body["venues"]] == [
        PADEL_UP_VENUE,
        PLAY_PADEL_VENUE,
        PADEL_FORT_VENUE,
    ]
    assert all(venue["data_quality"] is None for venue in body["venues"])
    assert "data-quality flags are absent" in body["reason"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/leadtime",
        "/api/metrics/sellout",
        "/api/metrics/first-slot",
        "/api/metrics/cancellations",
    ],
)
def test_a_single_snapshot_says_state_changes_need_a_second_poll(
    one_snapshot_client: TestClient, path: str
) -> None:
    """Regression: day one reading as "nobody books here" instead of "not yet".

    A booking is a state *change*. With one poll there is nothing to compare
    against, which is a different statement from "no bookings happened".
    """
    body = _get(one_snapshot_client, path)
    assert body["empty"] is True
    assert "at least two polls" in body["reason"]


def test_a_single_snapshot_still_reports_occupancy(one_snapshot_client: TestClient) -> None:
    """Regression: hiding the occupancy that one poll genuinely does establish.

    Occupancy is a state, not a change: one snapshot already says how much of
    the published inventory was booked at that moment.
    """
    body = _get(one_snapshot_client, "/api/occupancy/daily")
    assert body["empty"] is False
    assert body["rows"]
    assert any(row["booked_court_hours"] > 0 for row in body["rows"])


# --------------------------------------------------------------------------
# Normalization: the single easiest thing to get wrong
# --------------------------------------------------------------------------


def test_occupancy_daily_is_in_court_hours_and_does_not_under_report_a_60_minute_venue(
    full_client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """THE NORMALIZATION TEST, at the HTTP boundary.

    On 2026-09-16 Padel Up publishes one 60-minute slot and Padel Fort one
    30-minute slot. Counting slots makes them equal (1 == 1) and makes the
    60-minute venue look half the size it is. Only court-hours rank them
    correctly, 1.0 against 0.5, so the API serves court-hours.
    """
    business_date = synthetic_history.normalization_business_date.isoformat()
    body = _get(full_client, "/api/occupancy/daily", start=business_date, end=business_date)
    rows = {row["venue_uuid"]: row for row in body["rows"]}

    assert rows[PADEL_UP_VENUE]["slots"] == rows[PADEL_FORT_VENUE]["slots"] == 1
    assert rows[PADEL_UP_VENUE]["total_court_hours"] == 1.0
    assert rows[PADEL_FORT_VENUE]["total_court_hours"] == 0.5
    assert rows[PADEL_UP_VENUE]["total_minutes"] == 60
    assert rows[PADEL_FORT_VENUE]["total_minutes"] == 30


def test_a_slot_seen_open_then_booked_is_counted_once_as_booked(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: summing raw observations, multiplying every slot by ~48 polls.

    The normal-booking slot is seen OPEN six times and BOOKED four times. It is
    one 30-minute slot, booked: 0.5 booked court-hours and 100% occupancy for
    that date, not 5 hours and not 40%.
    """
    observation = synthetic_history.for_slot(synthetic_history.normal_slot_uuid)[0]
    business_date = observation.business_date.isoformat()
    body = _get(
        client,
        "/api/occupancy/daily",
        start=business_date,
        end=business_date,
        venue=PADEL_FORT_VENUE,
    )
    (row,) = body["rows"]
    assert row["slots"] == 1
    assert row["booked_court_hours"] == 0.5
    assert row["occupancy_strict"] == 1.0


def test_blocked_court_time_is_reported_beside_occupancy_never_folded_into_it(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: a withdrawn evening reading as a quiet evening.

    Padel Fort's 2026-09-13 evening is blocked in full -- 14 slots, 7 court-
    hours, zero bookings. Strict occupancy has no sellable denominator that
    day, so it must be null rather than 0.0, and the blocked hours must be
    visible in their own column.
    """
    business_date = synthetic_history.blocked_evening_business_date.isoformat()
    body = _get(client, "/api/occupancy/daily", start=business_date, end=business_date)
    (row,) = body["rows"]
    assert row["blocked_minutes"] == synthetic_history.expected_blocked_evening_minutes
    assert row["blocked_court_hours"] == 7.0
    assert row["occupancy_strict"] is None
    assert row["blocked_share"] == 1.0


def test_blocked_events_report_the_whole_evening_as_one_withdrawal(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: 14 separate one-slot events instead of one venue decision."""
    body = _get(client, "/api/metrics/blocked-events")
    evening = [
        row
        for row in body["rows"]
        if row["business_date"] == synthetic_history.blocked_evening_business_date.isoformat()
    ]
    assert len(evening) == 1
    assert evening[0]["slot_count"] == 14
    assert evening[0]["court_hours"] == 7.0
    assert evening[0]["censored_left"] is True


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_pricing_by_hour_states_that_no_venue_varies_price_by_hour(client: TestClient) -> None:
    """Regression: an "unknown" answer where the data gives a definite "no".

    Every court in the scripted history charges one flat price all day, which
    is what the real fixtures show too. The UI must be able to print that
    finding, so the API answers it directly with has_any_variation.
    """
    body = _get(client, "/api/pricing/by-hour")
    assert body["has_any_variation"] is False
    assert body["summary"].strip()
    assert all(court["is_flat"] for court in body["courts"])
    assert all(venue["is_flat"] for venue in body["venues"])


def test_pricing_is_served_per_court_hour_not_per_slot(full_client: TestClient) -> None:
    """Regression: ranking venues on slot price, which inverts the true order.

    Play Padel's 1000 per 30-minute slot is 2000 per court-hour -- the most
    expensive of the three -- while Padel Up's 1800 per 60-minute slot is the
    cheapest per hour and the dearest per slot.
    """
    body = _get(full_client, "/api/pricing/by-hour", sport="all")
    prices = {venue["venue_uuid"]: venue["flat_price_per_court_hour"] for venue in body["venues"]}
    assert prices[PLAY_PADEL_VENUE] == 2000.0
    assert prices[PADEL_UP_VENUE] == 1800.0
    assert prices[PADEL_FORT_VENUE] == 1800.0
    assert all(venue["currency"] == "INR" for venue in body["venues"])


def test_a_flat_price_timeline_says_so_rather_than_inventing_a_change(
    client: TestClient,
) -> None:
    """Regression: noise in a price series that never actually moved."""
    body = _get(client, "/api/pricing/timeline")
    assert body["has_any_change"] is False
    assert body["changes"] == []
    assert all(timeline["is_flat"] for timeline in body["timelines"])
    assert all(timeline["points"] for timeline in body["timelines"])


# --------------------------------------------------------------------------
# Market
# --------------------------------------------------------------------------


def test_market_share_exposes_padel_ups_no_bookings_flag(full_client: TestClient) -> None:
    """Regression: a venue's 0% demand share reading as a broken collector.

    Padel Up has never been observed with a booking. Its share is genuinely
    0.0, and the response must carry the flag that says so, together with the
    supply share that proves the collector is still seeing its inventory.
    """
    body = _get(full_client, "/api/market/share")
    quality = {row["venue_uuid"]: row for row in body["venues"]}
    assert "no_bookings_ever_observed" in quality[PADEL_UP_VENUE]["flags"]
    assert quality[PADEL_UP_VENUE]["booked_court_hours"] == 0.0
    assert quality[PADEL_UP_VENUE]["listed_court_hours"] > 0.0
    assert body["caveat_summary"].strip()

    padel_up_rows = [row for row in body["rows"] if row["venue_uuid"] == PADEL_UP_VENUE]
    assert padel_up_rows, "a venue with no bookings must still appear in the share table"
    assert all(row["supply_share"] is not None for row in padel_up_rows)


def test_venues_carry_the_same_data_quality_flags_as_market_share(full_client: TestClient) -> None:
    """Regression: two surfaces disagreeing about whether a venue is trustworthy."""
    venues = {row["venue_uuid"]: row for row in _get(full_client, "/api/venues")["venues"]}
    share = {row["venue_uuid"]: row for row in _get(full_client, "/api/market/share")["venues"]}
    assert venues[PADEL_UP_VENUE]["data_quality"]["flags"] == share[PADEL_UP_VENUE]["flags"]


def test_revenue_proxy_is_labelled_a_proxy_and_prices_blocked_time_beside_it(
    client: TestClient,
) -> None:
    """Regression: a proxy presented as revenue.

    It counts observed Hudle bookings at list price only. The response says so
    in its own label, and reports withdrawn court-hours alongside so the
    reader can see what was never offered for sale.
    """
    body = _get(client, "/api/market/revenue-proxy")
    assert body["is_proxy"] is True
    assert "proxy" in body["label"]
    assert body["rows"]
    assert any(row["blocked_court_hours"] > 0 for row in body["rows"])
    assert all(row["currency"] == "INR" for row in body["rows"])


# --------------------------------------------------------------------------
# Lead time
# --------------------------------------------------------------------------


def test_leadtime_excludes_left_censored_slots_and_says_how_many(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: booking-lead percentiles dragged down by pre-existing bookings.

    Two slots were already BOOKED in the first poll that ever saw them; their
    real booking pre-dates the dataset. Counting them as zero-lead bookings
    would move every percentile, so they are excluded and counted.
    """
    body = _get(client, "/api/leadtime")
    assert body["overall"]["excluded_censored"] == len(synthetic_history.censored_slot_uuids)
    assert body["overall"]["n"] == 4
    assert body["overall"]["median_hours"] is not None

    censored = [
        sample
        for sample in body["distribution"]
        if sample["slot_uuid"] in synthetic_history.censored_slot_uuids
    ]
    assert len(censored) == 2
    assert all(sample["censored_left"] for sample in censored)
    assert all(sample["lead_time_hours"] is None for sample in censored)


def test_leadtime_carries_the_uncertainty_of_every_sample(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: a lead time quoted to the minute from a 30-minute poll window.

    The booking happened somewhere between the last poll that saw it open and
    the first that saw it booked; the width of that window travels with the
    number.
    """
    body = _get(client, "/api/leadtime")
    sample = next(
        item
        for item in body["distribution"]
        if item["slot_uuid"] == synthetic_history.normal_slot_uuid
    )
    assert sample["lead_time_hours"] == synthetic_history.expected_lead_time_hours
    assert sample["uncertainty_minutes"] == synthetic_history.expected_uncertainty_minutes
    assert sample["first_booked_at"].startswith(
        synthetic_history.expected_first_booked_at.strftime("%Y-%m-%dT%H:%M")
    )


def test_leadtime_splits_by_peak_and_by_day_of_week(client: TestClient) -> None:
    """Regression: a single median hiding that peak slots sell on a different clock."""
    body = _get(client, "/api/leadtime")
    assert body["peak_hours"] == [18, 19, 20, 21, 22]
    assert {bucket["bucket"] for bucket in body["by_peak"]} == {"peak", "off_peak"}
    assert all(entry["day_name"] for entry in body["by_day_of_week"])
    assert all(entry["stats"]["n"] >= 0 for entry in body["by_day_of_week"])


def test_a_post_midnight_booking_is_attributed_to_the_previous_evening(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: Friday-night demand landing on Saturday.

    Play Padel sells 00:30 slots and Hudle stamps them with Saturday's date.
    Aggregation is on business_date, so the booking belongs to Friday.
    """
    body = _get(client, "/api/leadtime", venue=PLAY_PADEL_VENUE)
    sample = next(
        item
        for item in body["distribution"]
        if item["slot_uuid"] == synthetic_history.post_midnight_slot_uuid
    )
    assert sample["business_date"] == synthetic_history.post_midnight_business_date.isoformat()
    assert sample["business_date"] != synthetic_history.post_midnight_local_date.isoformat()


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def test_coverage_returns_the_ninety_minute_hole_as_an_explicit_interval(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: a chart drawing a straight line across a collector outage.

    Two polls are missing from the scripted schedule. The API returns the hole
    as a bounded interval so the dashboard can break the series there instead
    of interpolating demand that was never observed.
    """
    body = _get(client, "/api/coverage")
    assert body["has_gaps"] is True
    gaps = [gap for gap in body["poll_gaps"] if gap["minutes"] == synthetic_history.gap_minutes]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["missed_polls"] == synthetic_history.missing_snapshot_count
    assert gap["start"].startswith(synthetic_history.gap_start.strftime("%Y-%m-%dT%H:%M"))
    assert gap["end"].startswith(synthetic_history.gap_end.strftime("%Y-%m-%dT%H:%M"))
    assert body["missed_polls"] >= synthetic_history.missing_snapshot_count


def test_coverage_reports_each_facility_against_the_configured_cadence(
    client: TestClient, test_config: Config
) -> None:
    """Regression: coverage measured against a hard-coded cadence.

    The expected count comes from the one cadence knob in config, so changing
    the poll interval cannot leave yesterday's coverage divided by the wrong
    number.
    """
    body = _get(client, "/api/coverage")
    assert body["cadence_minutes"] == test_config.poll.cadence_minutes
    assert body["snapshots_expected_per_day"] == test_config.poll.expected_snapshots_per_day
    facilities = {day["facility_uuid"] for day in body["days"]}
    assert {PADEL_UP_COURT, PADEL_FORT_COURT} <= facilities
    assert all(day["snapshots_expected"] > day["snapshots_received"] for day in body["days"])


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_health_reports_staleness_in_minutes_from_the_last_snapshot(
    client: TestClient, last_observed_at: dt.datetime
) -> None:
    """Regression: a dashboard that cannot tell a live collector from a dead one."""
    body = _get(client, "/api/health")
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["stale"] is False
    assert body["staleness_minutes"] == pytest.approx(FRESH_OFFSET_MINUTES)
    assert body["last_snapshot"]["observed_at"].startswith(
        last_observed_at.strftime("%Y-%m-%dT%H:%M")
    )
    assert body["circuit"]["state"] == "closed"


def test_health_flags_a_stale_collector(
    test_config: Config, synthetic_history_storage: SyntheticHistory, last_observed_at: dt.datetime
) -> None:
    """Regression: silent data loss.

    Slots that elapse while the collector is down can never be re-observed, so
    a stalled collector has to be loud rather than merely producing a chart
    that stops.
    """
    storage = synthetic_history_storage.storage
    assert storage is not None
    stale_now = last_observed_at + dt.timedelta(minutes=STALE_OFFSET_MINUTES)
    for stale_client in _client(test_config, storage, stale_now):
        body = _get(stale_client, "/api/health")
        assert body["ok"] is False
        assert body["status"] == "stale"
        assert body["stale"] is True
        assert body["staleness_minutes"] == pytest.approx(STALE_OFFSET_MINUTES)
        assert "can never be re-observed" in body["reason"]


def test_health_reports_the_breaker_as_open_after_consecutive_failures(
    test_config: Config, last_observed_at: dt.datetime
) -> None:
    """Regression: a collector hammering a dead API with nothing saying so.

    The web process cannot see the collector's in-memory breaker, so it
    reconstructs it from the trailing run of failed snapshots and labels the
    answer as reconstructed rather than live.
    """
    storage = SQLiteStorage("sqlite://")
    storage.initialize()
    failures = test_config.poll.max_consecutive_failures
    try:
        for index in range(failures):
            observed_at = last_observed_at + dt.timedelta(minutes=30 * index)
            snapshot_id = storage.create_snapshot(f"key-{index}", observed_at, 21)
            storage.finalize_snapshot(snapshot_id, False, "boom", 10)
        last = last_observed_at + dt.timedelta(minutes=30 * (failures - 1))
        for failing_client in _client(test_config, storage, last + dt.timedelta(minutes=1)):
            body = _get(failing_client, "/api/health")
            assert body["circuit"]["state"] == "open"
            assert body["circuit"]["consecutive_failures"] == failures
            assert body["circuit"]["max_consecutive_failures"] == failures
            assert "reconstructed" in body["circuit"]["source"]
            assert body["status"] == "open_circuit"
            assert body["ok"] is False
    finally:
        storage.close()


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def test_filters_are_echoed_back_including_the_defaulted_sport(client: TestClient) -> None:
    """Regression: a padel chart quietly including pickleball courts.

    Two of the three venues publish pickleball courts beside their padel one.
    The sport filter therefore defaults to the dashboard's sport rather than
    to "everything", and every response states which sport it answered for.
    """
    body = _get(client, "/api/occupancy/daily")
    assert body["filters"]["sport"] == "padel"
    assert body["filters"]["venue"] is None

    everything = _get(client, "/api/occupancy/daily", sport="all")
    assert everything["filters"]["sport"] == "all"


def test_an_unknown_sport_or_venue_is_refused_with_a_usable_message(
    client: TestClient,
) -> None:
    """Regression: a typo'd filter silently returning an empty, plausible chart."""
    bad_sport = client.get("/api/occupancy/daily", params={"sport": "squash"})
    assert bad_sport.status_code == 422
    assert "squash" in bad_sport.json()["detail"]

    bad_venue = client.get("/api/occupancy/daily", params={"venue": "not-a-uuid"})
    assert bad_venue.status_code == 404
    assert PADEL_UP_VENUE in bad_venue.json()["detail"]

    backwards = client.get(
        "/api/occupancy/daily", params={"start": "2026-09-20", "end": "2026-09-10"}
    )
    assert backwards.status_code == 422


def test_narrowing_the_window_does_not_change_a_days_arithmetic(
    client: TestClient, synthetic_history: SyntheticHistory
) -> None:
    """Regression: a filter that changes the denominator it is filtering within."""
    business_date = synthetic_history.normalization_business_date.isoformat()
    narrow = _get(client, "/api/occupancy/daily", start=business_date, end=business_date)
    wide = _get(client, "/api/occupancy/daily")
    wide_rows = {
        (row["venue_uuid"], row["business_date"]): row
        for row in wide["rows"]
        if row["business_date"] == business_date
    }
    for row in narrow["rows"]:
        assert wide_rows[(row["venue_uuid"], row["business_date"])] == row


def test_a_venue_filter_restricts_every_surface(client: TestClient) -> None:
    """Regression: a venue-scoped view leaking another venue's numbers."""
    body = _get(client, "/api/occupancy/daily", venue=PADEL_UP_VENUE)
    assert {row["venue_uuid"] for row in body["rows"]} == {PADEL_UP_VENUE}

    heatmap = _get(client, "/api/occupancy/heatmap", venue=PADEL_UP_VENUE)
    assert heatmap["combined"]["venue_uuid"] == PADEL_UP_VENUE
    assert {entry["venue_uuid"] for entry in heatmap["by_venue"]} == {PADEL_UP_VENUE}


# --------------------------------------------------------------------------
# Heatmap and weekday/weekend
# --------------------------------------------------------------------------


def test_a_thin_heatmap_cell_is_flagged_sparse(client: TestClient) -> None:
    """Regression: a 100% cell built from one observed day read as a rate.

    The scripted history covers a handful of dates, so most cells are thin.
    Each carries its own denominator and a sparse flag so the UI can show the
    cell without implying confidence it has not earned.
    """
    body = _get(client, "/api/occupancy/heatmap")
    cells = body["combined"]["cells"]
    assert cells
    assert any(cell["sparse"] for cell in cells)
    for cell in cells:
        assert cell["business_dates"] >= 1
        assert cell["total_minutes"] > 0
        assert cell["day_name"]


def test_weekday_and_weekend_are_reported_per_day_not_as_raw_totals(
    client: TestClient,
) -> None:
    """Regression: a two-day weekend compared against a five-day week.

    Raw court-hour totals make any weekend look quiet. Both segments carry
    their business-date count and a per-day figure computed from it.
    """
    body = _get(client, "/api/metrics/weekday-weekend")
    assert body["weekday"]["business_dates"] >= 1
    assert body["weekend"]["business_dates"] >= 1
    for segment in (body["weekday"], body["weekend"]):
        expected = segment["booked_court_hours"] / segment["business_dates"]
        assert segment["booked_court_hours_per_day"] == pytest.approx(expected)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_dashboard_is_mounted_at_the_root_without_shadowing_the_api(
    test_config: Config, synthetic_history_storage: SyntheticHistory
) -> None:
    """Regression: mounting the static dashboard at / before the API routes.

    A mount at "/" matches every path, so registering it first would swallow
    every /api request.
    """
    storage = synthetic_history_storage.storage
    assert storage is not None
    app = create_app(config=test_config, storage=storage, clock=lambda: dt.datetime.now(dt.UTC))
    mounts = [route for route in app.routes if getattr(route, "name", None) == "dashboard"]
    assert len(mounts) == 1
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_the_injected_storage_is_not_closed_by_the_lifespan(
    test_config: Config, synthetic_history_storage: SyntheticHistory
) -> None:
    """Regression: shutdown disposing a database the caller still owns."""
    storage = synthetic_history_storage.storage
    assert storage is not None
    app = create_app(config=test_config, storage=storage, clock=lambda: dt.datetime.now(dt.UTC))
    with TestClient(app):
        pass
    assert storage.latest_snapshot() is not None


# --------------------------------------------------------------------------
# Contract: a venue can be collected and still withheld from the dashboard
# --------------------------------------------------------------------------


def test_a_hidden_venue_is_absent_from_every_default_response(client: TestClient) -> None:
    """Regression: Padel Up's flat 0% line reappearing somewhere and reading as a bug.

    ``show_in_dashboard: false`` has to hold across every endpoint at once. A
    venue withheld from the headline chart but still counted in a denominator
    somewhere else would be worse than either showing it or dropping it.
    """
    listed = [row["venue_uuid"] for row in _get(client, "/api/venues")["venues"]]
    assert PADEL_UP_VENUE not in listed
    assert PLAY_PADEL_VENUE in listed and PADEL_FORT_VENUE in listed

    for path in ("/api/occupancy/daily", "/api/market/share", "/api/pricing/by-hour"):
        body = _get(client, path)
        blob = str(body)
        assert PADEL_UP_VENUE not in blob, f"{path} still carries the hidden venue"


def test_a_hidden_venue_is_excluded_from_the_market_share_denominator(
    client: TestClient, full_client: TestClient
) -> None:
    """Regression: hiding a venue visually while still dividing by its court-hours.

    Share must be computed over the venues actually shown, or the percentages
    will not add up to what the chart displays.
    """
    hidden = {row["venue_uuid"] for row in _get(client, "/api/market/share")["venues"]}
    shown = {row["venue_uuid"] for row in _get(full_client, "/api/market/share")["venues"]}
    assert PADEL_UP_VENUE in shown
    assert PADEL_UP_VENUE not in hidden
    assert hidden == shown - {PADEL_UP_VENUE}


def test_naming_a_hidden_venue_explicitly_still_returns_it(client: TestClient) -> None:
    """Regression: hiding a venue making its collected data unreachable.

    Hiding is a dashboard default, not an access control. The data is still
    being collected every cycle and must stay queryable on request, otherwise
    there is no way to check whether Padel Up has started selling.
    """
    listed = [
        row["venue_uuid"] for row in _get(client, "/api/venues", venue=PADEL_UP_VENUE)["venues"]
    ]
    assert listed == [PADEL_UP_VENUE]

    body = _get(client, "/api/occupancy/daily", venue=PADEL_UP_VENUE)
    assert PADEL_UP_VENUE in str(body)
    assert PLAY_PADEL_VENUE not in str(body)


# --------------------------------------------------------------------------
# Contract: every metric says whether it can be trusted yet
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(METRIC_ENDPOINTS))
def test_every_metric_response_declares_its_readiness(client: TestClient, path: str) -> None:
    """Regression: a chart drawn over one day of data looking identical to a
    broken one. The response must say what it has and what it needs."""
    readiness = _get(client, path)["readiness"]
    assert set(readiness) >= {
        "ready",
        "snapshots",
        "min_snapshots",
        "elapsed_days",
        "min_elapsed_days",
        "eta_days",
        "note",
    }
    assert readiness["eta_days"] == max(
        0, readiness["min_elapsed_days"] - readiness["elapsed_days"]
    )
    assert readiness["ready"] == (
        readiness["snapshots"] >= readiness["min_snapshots"]
        and readiness["elapsed_days"] >= readiness["min_elapsed_days"]
    )


def test_history_metrics_are_not_ready_on_day_one(one_snapshot_client: TestClient) -> None:
    """Regression: weekday-vs-weekend and the heatmap unlocking on a single
    poll, drawn from one weekday and read as a pattern."""
    for path in ("/api/leadtime", "/api/occupancy/heatmap", "/api/metrics/weekday-weekend"):
        readiness = _get(one_snapshot_client, path)["readiness"]
        assert readiness["ready"] is False, path
        assert readiness["eta_days"] > 0, path
        assert readiness["note"], path
    # ...while what one poll can honestly show is available at once.
    assert _get(one_snapshot_client, "/api/coverage")["readiness"]["ready"] is True
    assert _get(one_snapshot_client, "/api/venues")["readiness"]["ready"] is True


def test_readiness_is_judged_on_the_whole_dataset_not_the_window(client: TestClient) -> None:
    """Regression: a request for the next seven days has no elapsed days in
    it, and must not make a metric look less ready than the database is."""
    whole = _get(client, "/api/occupancy/daily")["readiness"]
    future = _get(client, "/api/occupancy/daily", start="2030-01-01", end="2030-01-07")["readiness"]
    assert future["elapsed_days"] == whole["elapsed_days"]
    assert future["snapshots"] == whole["snapshots"]
