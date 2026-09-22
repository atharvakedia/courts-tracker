"""Tests for the Hudle HTTP client.

Nothing here touches the network: every request is answered by an
``httpx.MockTransport`` handler, and the clock is a fake, so the gentleness
guarantees -- the global inter-request floor, the bounded backoff sequence and
the circuit breaker -- are asserted exactly instead of waited out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.conftest import PADEL_FORT_COURT, PADEL_FORT_VENUE, PADEL_UP_VENUE
from tracker.config import BackoffConfig, Config, HttpConfig, PollConfig
from tracker.hudle import (
    MAX_SEARCH_PAGES,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    HudleApiError,
    HudleClient,
    HudleHttpError,
    HudleTransportError,
    RateLimiter,
    backoff_delays,
)

PADEL_SPORT_ID = 44
PICKLEBALL_SPORT_ID = 56
START_DATE = dt.date(2026, 9, 11)
END_DATE = dt.date(2026, 10, 11)

Handler = Callable[[httpx.Request], httpx.Response]


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that moves only when a test says it should.

    ``sleep`` advances it, the way a real sleep does, so the rate limiter's
    "time already elapsed" arithmetic is exercised rather than bypassed.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    return fake


_BACKOFF_FIELDS = frozenset(f.name for f in dataclasses.fields(BackoffConfig))


def poll_with(base: PollConfig, **overrides: Any) -> PollConfig:
    """A copy of the real poll config with a few knobs moved."""
    backoff_changes = {k: v for k, v in overrides.items() if k in _BACKOFF_FIELDS}
    poll_changes = {k: v for k, v in overrides.items() if k not in _BACKOFF_FIELDS}
    backoff = dataclasses.replace(base.backoff, **backoff_changes)
    return dataclasses.replace(base, backoff=backoff, **poll_changes)


def build_client(handler: Handler, http: HttpConfig, poll: PollConfig) -> HudleClient:
    return HudleClient(http, poll, transport=httpx.MockTransport(handler))


def ok_envelope(data: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "code": 200, "data": data or {}})


# --------------------------------------------------------------------------
# The global inter-request floor
# --------------------------------------------------------------------------


def test_the_inter_request_gap_is_enforced_between_any_two_requests(
    test_config: Config, clock: FakeClock
) -> None:
    """The floor is global: it spans endpoints, not just repeats of one call."""
    poll = poll_with(test_config.poll, request_gap_seconds=2.0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        clock.advance(0.5)  # the request itself takes half a second
        return ok_envelope()

    client = build_client(handler, test_config.http, poll)
    with client:
        client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)
        assert clock.sleeps == []  # nothing to wait for on the very first request
        client.search_venues(PADEL_SPORT_ID)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert clock.sleeps == [pytest.approx(1.5)]


def test_the_inter_request_floor_still_applies_to_every_retry(
    test_config: Config, clock: FakeClock
) -> None:
    """A retry is never cheaper or faster than a fresh request."""
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=2.0,
        max_consecutive_failures=99,
        initial_seconds=0.5,
        multiplier=1.0,
        max_seconds=0.5,
        max_attempts=3,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = build_client(handler, test_config.http, poll)
    with client, pytest.raises(HudleHttpError):
        client.fetch_venue_detail(PADEL_UP_VENUE)

    # backoff 0.5, then the floor tops the gap up to a full 2.0s, twice over.
    assert clock.sleeps == [0.5, 1.5, 0.5, 1.5]


def test_a_rate_limiter_with_no_gap_never_sleeps(clock: FakeClock) -> None:
    limiter = RateLimiter(0.0)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    assert clock.sleeps == []


def test_a_negative_gap_is_clamped_rather_than_inverting_the_floor() -> None:
    assert RateLimiter(-5.0).min_interval_seconds == 0.0


# --------------------------------------------------------------------------
# Bounded backoff: retries must not amplify load
# --------------------------------------------------------------------------


def test_backoff_delays_are_geometric_and_capped(test_config: Config) -> None:
    """The shape of the sequence, pinned independently of how it is tuned.

    ``initial_seconds`` is an operational knob -- it was raised once already,
    when Hudle turned out to refuse a retry that landed inside its burst window
    -- so the geometric-and-capped property is asserted against an explicit
    backoff rather than against whatever ``config.yaml`` currently says. Only
    the length claim reads the real config, because that one is about
    ``max_attempts`` bounding the retries and is true at any tuning.
    """
    backoff = dataclasses.replace(
        test_config.poll.backoff,
        initial_seconds=5.0,
        multiplier=2.0,
        max_seconds=300.0,
        max_attempts=8,
    )
    assert backoff_delays(backoff) == (5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0)
    assert backoff_delays(dataclasses.replace(backoff, max_attempts=1)) == ()
    configured = test_config.poll.backoff
    assert len(backoff_delays(configured)) == configured.max_attempts - 1


def test_retry_sleeps_follow_the_backoff_sequence_exactly(
    test_config: Config, clock: FakeClock
) -> None:
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=0.0,
        max_consecutive_failures=99,
        initial_seconds=5.0,
        multiplier=2.0,
        max_attempts=4,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    client = build_client(handler, test_config.http, poll)
    with client, pytest.raises(HudleHttpError) as excinfo:
        client.fetch_venue_detail(PADEL_UP_VENUE)

    assert excinfo.value.status == 503
    assert excinfo.value.body == "unavailable"
    assert len(calls) == 4  # exactly max_attempts, never one more
    assert clock.sleeps == [5.0, 10.0, 20.0]


def test_max_attempts_is_the_ceiling_for_transport_errors_too(
    test_config: Config, clock: FakeClock
) -> None:
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=0.0,
        max_consecutive_failures=99,
        max_attempts=2,
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("no route to host", request=request)

    client = build_client(handler, test_config.http, poll)
    with client, pytest.raises(HudleTransportError) as excinfo:
        client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)

    assert attempts == 2
    assert "ConnectError" in str(excinfo.value)


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def test_the_circuit_opens_on_the_fifth_consecutive_failure_then_short_circuits(
    test_config: Config, clock: FakeClock
) -> None:
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=0.0,
        max_consecutive_failures=5,
        max_attempts=3,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="boom")

    client = build_client(handler, test_config.http, poll)
    with client:
        with pytest.raises(HudleHttpError):
            client.fetch_venue_detail(PADEL_UP_VENUE)  # attempts 1-3
        assert len(calls) == 3
        assert not client.circuit.is_open
        assert client.circuit.state is CircuitState.CLOSED

        with pytest.raises(CircuitOpenError):
            client.fetch_venue_detail(PADEL_UP_VENUE)  # attempts 4-5, then refused
        assert len(calls) == 5
        assert client.circuit.is_open
        assert client.circuit.state is CircuitState.OPEN
        assert client.circuit.consecutive_failures == 5

        with pytest.raises(CircuitOpenError):
            client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)
        assert len(calls) == 5  # not one further request once open


def test_a_success_resets_the_consecutive_failure_count(
    test_config: Config, clock: FakeClock
) -> None:
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=0.0,
        max_consecutive_failures=5,
        max_attempts=1,
    )
    calls: list[httpx.Request] = []
    failing = True

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="boom") if failing else ok_envelope()

    client = build_client(handler, test_config.http, poll)
    with client:
        for _ in range(4):
            with pytest.raises(HudleHttpError):
                client.fetch_venue_detail(PADEL_UP_VENUE)
        assert client.circuit.consecutive_failures == 4
        assert not client.circuit.is_open

        failing = False
        client.fetch_venue_detail(PADEL_UP_VENUE)
        assert client.circuit.consecutive_failures == 0
        assert client.circuit.state is CircuitState.CLOSED

        failing = True
        for _ in range(4):
            with pytest.raises(HudleHttpError):
                client.fetch_venue_detail(PADEL_UP_VENUE)
        assert not client.circuit.is_open  # the earlier run does not carry over
        assert len(calls) == 9


def test_the_breaker_refuses_calls_until_it_is_reset_by_hand() -> None:
    breaker = CircuitBreaker(2)
    breaker.check()
    breaker.record_failure("first")
    breaker.check()
    breaker.record_failure("second")

    assert breaker.is_open
    assert breaker.last_error == "second"
    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.check()
    assert excinfo.value.consecutive_failures == 2
    assert excinfo.value.threshold == 2

    breaker.reset()
    breaker.check()
    assert breaker.consecutive_failures == 0


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_search_venues_all_walks_every_page(
    test_config: Config, clock: FakeClock, raw_venue_search_pickleball: Any
) -> None:
    """Pickleball is 57 venues across two pages; one call truncates at 50."""
    assert len(raw_venue_search_pickleball["data"]) == 50
    assert raw_venue_search_pickleball["meta"]["pagination"]["total"] == 57

    page_two = {
        "code": 200,
        "data": [{"id": f"synthetic-{i}", "name": f"Synthetic Venue {i}"} for i in range(7)],
        "meta": {
            "pagination": {
                "total": 57,
                "count": 7,
                "per_page": 50,
                "current_page": 2,
                "total_pages": 2,
            }
        },
    }
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        payload = raw_venue_search_pickleball if page == 1 else page_two
        return httpx.Response(200, json=payload)

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        venues = client.search_venues_all(PICKLEBALL_SPORT_ID)

    assert pages == [1, 2]
    assert len(venues) == 57


def test_search_venues_all_stops_when_one_page_holds_everything(
    test_config: Config, clock: FakeClock, raw_venue_search_padel: Any
) -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        return httpx.Response(200, json=raw_venue_search_padel)

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        venues = client.search_venues_all(PADEL_SPORT_ID)

    assert pages == [1]
    assert len(venues) == 3


# --------------------------------------------------------------------------
# Envelope handling
# --------------------------------------------------------------------------


def test_success_false_in_a_200_envelope_raises(test_config: Config, clock: FakeClock) -> None:
    poll = poll_with(
        test_config.poll,
        request_gap_seconds=0.0,
        max_consecutive_failures=5,
        max_attempts=3,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"success": False, "code": 422, "message": "invalid facility"}
        )

    client = build_client(handler, test_config.http, poll)
    with client:
        with pytest.raises(HudleApiError) as excinfo:
            client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)
        assert excinfo.value.code == 422
        assert "invalid facility" in str(excinfo.value)
        assert len(calls) == 1  # a refusal is not a transport problem: no retry
        assert client.circuit.consecutive_failures == 1


def test_a_missing_success_key_is_not_a_failure(
    test_config: Config, clock: FakeClock, raw_venue_search_padel: Any
) -> None:
    """``venue-search`` omits ``success`` entirely; only ``false`` is an error."""
    assert "success" not in raw_venue_search_padel

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw_venue_search_padel)

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        payload = client.search_venues(PADEL_SPORT_ID)
        assert payload["code"] == 200
        assert len(payload["data"]) == 3
        assert client.circuit.consecutive_failures == 0


def test_a_non_json_body_raises_an_api_error(test_config: Config, clock: FakeClock) -> None:
    poll = poll_with(test_config.poll, request_gap_seconds=0.0, max_attempts=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>under maintenance</html>")

    client = build_client(handler, test_config.http, poll)
    with client, pytest.raises(HudleApiError):
        client.fetch_venue_detail(PADEL_UP_VENUE)


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------


def test_the_required_api_headers_are_sent(test_config: Config, clock: FakeClock) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return ok_envelope()

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)

    headers = captured[0].headers
    assert headers["Api-Secret"] == os.environ["HUDLE_API_SECRET"]
    assert headers["x-app-id"] == os.environ["HUDLE_APP_ID"]
    assert headers["x-device-source"] == "3"
    assert headers["Accept"] == "application/json, text/plain, */*"
    user_agent = headers["User-Agent"]
    assert user_agent == test_config.http.user_agent
    assert user_agent.startswith("Mozilla/5.0")
    assert "python-httpx" not in user_agent


def test_fetch_slots_requests_the_documented_grid_url(
    test_config: Config, clock: FakeClock
) -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return ok_envelope()

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        client.fetch_slots(PADEL_FORT_VENUE, PADEL_FORT_COURT, START_DATE, END_DATE)

    url = captured[0]
    assert url.host == "api.hudle.in"
    assert url.path == (
        f"/api/v1/web/venues/{PADEL_FORT_VENUE}/facilities/{PADEL_FORT_COURT}/slots"
    )
    assert url.params["start_date"] == "2026-09-11"
    assert url.params["end_date"] == "2026-10-11"
    assert url.params["grid"] == "1"


def test_search_venues_sends_the_city_and_sport_filters(
    test_config: Config, clock: FakeClock
) -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, json={"code": 200, "data": []})

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        client.search_venues(PICKLEBALL_SPORT_ID, 2, 50, city_id=test_config.discovery.city_id)

    url = captured[0]
    assert url.path == "/api/v1/venue-search"
    assert url.params["page"] == "2"
    assert url.params["per_page"] == "50"
    assert url.params["cityId"] == "8"
    assert url.params["preferred_sports"] == str(PICKLEBALL_SPORT_ID)
    assert url.params["venueName"] == ""


def test_fetch_venue_detail_hits_the_venue_endpoint(test_config: Config, clock: FakeClock) -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return ok_envelope({"activities": []})

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        payload = client.fetch_venue_detail(PADEL_UP_VENUE)

    assert captured[0].path == f"/api/v1/venues/{PADEL_UP_VENUE}"
    assert payload["data"] == {"activities": []}


def test_fetch_venue_page_html_returns_text_with_browser_headers(
    test_config: Config, clock: FakeClock
) -> None:
    """The SSR page is the public website, so no API secret goes with it."""
    html = '<html><script id="__NEXT_DATA__">{"props": {}}</script></html>'
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        result = client.fetch_venue_page_html("padel-up", "772576")

    assert result == html
    request = captured[0]
    assert str(request.url) == "https://hudle.in/venues/padel-up/772576"
    assert request.headers["Accept"].startswith("text/html")
    assert request.headers["User-Agent"] == test_config.http.user_agent
    assert "Api-Secret" not in request.headers


def test_the_ssr_path_in_the_config_matches_the_url_the_client_builds(
    test_config: Config, clock: FakeClock
) -> None:
    venue = test_config.venue_by_uuid(PADEL_UP_VENUE)
    assert venue is not None
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, text="<html></html>")

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        client.fetch_venue_page_html(venue.slug, venue.numeric_id)

    assert captured[0].path == venue.ssr_path


def test_search_venues_all_refuses_to_walk_forever(test_config: Config, clock: FakeClock) -> None:
    """A pagination block that never resolves must not become a request loop."""
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [{"id": f"venue-{i}"} for i in range(50)],
                # Counters that can never be satisfied: total outruns the rows.
                "meta": {"pagination": {"total": 10_000, "per_page": 50}},
            },
        )

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        venues = client.search_venues_all(PICKLEBALL_SPORT_ID)

    assert pages == list(range(1, MAX_SEARCH_PAGES + 1))
    assert len(venues) == 50 * MAX_SEARCH_PAGES


def test_pagination_counters_sent_as_strings_are_still_honoured(
    test_config: Config, clock: FakeClock
) -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [{"id": f"venue-{page}-{i}"} for i in range(2)],
                "meta": {"pagination": {"total": "4", "per_page": "2", "current_page": str(page)}},
            },
        )

    poll = poll_with(test_config.poll, request_gap_seconds=0.0)
    client = build_client(handler, test_config.http, poll)
    with client:
        venues = client.search_venues_all(PADEL_SPORT_ID, per_page=2)

    assert pages == [1, 2]
    assert len(venues) == 4
