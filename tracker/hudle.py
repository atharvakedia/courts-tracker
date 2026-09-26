"""Hudle HTTP client -- the only module in this project that touches the network.

Hudle's terms of service prohibit automated scripts, so this client is built to
keep the tracker unambiguously gentle and personal-scale. That is a hard design
goal, not a nicety:

* Every outbound request -- first attempt, retry and pagination page alike --
  passes through one private gate that enforces a monotonic-clock floor of
  ``poll.request_gap_seconds`` between any two requests. No public method can
  reach the network without it, so no caller can opt out of the floor.
* Requests are strictly sequential and synchronous. There is no concurrency and
  no asyncio, and the connection pool is pinned to a single connection.
* Retries are bounded by ``poll.backoff.max_attempts`` per call, so a failing
  facility costs a fixed, small number of requests per collect cycle rather
  than amplifying load.
* A :class:`CircuitBreaker` opens after ``poll.max_consecutive_failures``
  consecutive failures and then refuses every call without touching the
  network, so the scheduler stops instead of hammering.
* A 404 is an answer about one resource (a court the venue took off Hudle),
  not a sign that Hudle is failing: it is not retried and does not count
  toward the breaker, so one gone court cannot stop the pass.

Parsing is deliberately out of scope. These methods return decoded JSON, or raw
HTML text for the server-rendered venue page; turning that into domain objects
belongs to the classification layer.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from enum import StrEnum
from types import TracebackType
from typing import Any

import httpx

from tracker.config import BackoffConfig, HttpConfig, PollConfig
from tracker.types import to_date_text

logger = logging.getLogger("tracker.hudle")

#: Jaipur. ``discovery.city_id`` in ``config.yaml`` carries the same value; it
#: is a default here only so the client stays independent of DiscoveryConfig.
DEFAULT_CITY_ID = 8

#: Hard ceiling on pages walked by :meth:`HudleClient.search_venues_all`, so a
#: malformed pagination block can never turn into an unbounded request loop.
MAX_SEARCH_PAGES = 20

#: How much of a failing response body goes into the exception message. The
#: full body stays on :attr:`HudleHttpError.body`.
BODY_SNIPPET_CHARS = 500

_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class HudleError(Exception):
    """Base class for every failure this client raises."""


class HudleTransportError(HudleError):
    """The request never produced a response: DNS, connect, or read timeout."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class HudleHttpError(HudleError):
    """Hudle answered with a status other than 200."""

    def __init__(self, status: int, body: str, *, path: str = "") -> None:
        super().__init__(f"hudle returned HTTP {status} for {path}: {body[:BODY_SNIPPET_CHARS]}")
        self.status = status
        self.body = body
        self.path = path


class HudleApiError(HudleError):
    """A 200 whose envelope reports failure, or whose body is not JSON.

    Hudle uses two envelope shapes: the slot and venue endpoints carry an
    explicit ``success`` flag, while ``venue-search`` omits it entirely. Only an
    explicit ``success: false`` is an error; a missing key is not.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str = "",
        code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.code = code
        self.payload = payload


class CircuitOpenError(HudleError):
    """The breaker is open, so the call was refused without any network use."""

    def __init__(self, consecutive_failures: int, threshold: int) -> None:
        super().__init__(
            f"hudle circuit is open after {consecutive_failures} consecutive "
            f"failures (threshold {threshold}); refusing to send a request"
        )
        self.consecutive_failures = consecutive_failures
        self.threshold = threshold


# --------------------------------------------------------------------------
# Gentleness primitives
# --------------------------------------------------------------------------


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class CircuitBreaker:
    """Trips after a run of consecutive failures and then refuses every call.

    The point is that the scheduler stops rather than hammering a service that
    is already unhappy. Once open the breaker stays open until :meth:`reset` is
    called explicitly, because nothing can succeed while every call is refused.
    """

    def __init__(self, max_consecutive_failures: int) -> None:
        self._threshold = max(1, max_consecutive_failures)
        self._consecutive_failures = 0
        self._last_error: str | None = None

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def state(self) -> CircuitState:
        return CircuitState.OPEN if self.is_open else CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self._threshold

    def check(self) -> None:
        """Raise :class:`CircuitOpenError` if the breaker has tripped."""
        if self.is_open:
            raise CircuitOpenError(self._consecutive_failures, self._threshold)

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._last_error = None

    def record_failure(self, error: str | None = None) -> None:
        self._consecutive_failures += 1
        self._last_error = error
        if self.is_open:
            logger.warning(
                "hudle_circuit_opened",
                extra={
                    "consecutive_failures": self._consecutive_failures,
                    "threshold": self._threshold,
                    "error": error,
                },
            )

    def reset(self) -> None:
        """Close the breaker by hand, after an operator has looked at it."""
        self._consecutive_failures = 0
        self._last_error = None


class RateLimiter:
    """A monotonic-clock floor between any two outbound requests.

    The floor is measured from the moment the previous request was released,
    not from when it completed, so a slow response never earns the next request
    an earlier slot than the configured gap.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._last_released_at: float | None = None

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    def acquire(self) -> float:
        """Block until the floor has elapsed. Returns the seconds slept."""
        slept = 0.0
        if self._last_released_at is not None:
            remaining = self._min_interval - (time.monotonic() - self._last_released_at)
            if remaining > 0:
                time.sleep(remaining)
                slept = remaining
        self._last_released_at = time.monotonic()
        return slept


def backoff_delays(backoff: BackoffConfig) -> tuple[float, ...]:
    """The sleeps between successive attempts: initial, initial*multiplier, ...

    Each term is capped at ``max_seconds`` and the sequence has exactly
    ``max_attempts - 1`` entries, because the last attempt is never followed by
    a wait. This is the whole retry budget for one call.
    """
    attempts = max(1, backoff.max_attempts)
    delays: list[float] = []
    delay = float(backoff.initial_seconds)
    for _ in range(attempts - 1):
        delays.append(min(delay, float(backoff.max_seconds)))
        delay *= float(backoff.multiplier)
    return tuple(delays)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class HudleClient:
    """Synchronous, strictly sequential, rate-limited Hudle client."""

    def __init__(
        self,
        http: HttpConfig,
        poll: PollConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = http
        self._poll = poll
        self._rate_limiter = RateLimiter(poll.request_gap_seconds)
        self._circuit = CircuitBreaker(poll.max_consecutive_failures)
        self._delays = backoff_delays(poll.backoff)
        self._max_attempts = max(1, poll.backoff.max_attempts)
        self._client = httpx.Client(
            timeout=httpx.Timeout(poll.timeout_seconds),
            follow_redirects=True,
            # One connection: the client is sequential by construction, and a
            # pool would only make it easier to accidentally parallelise.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def circuit(self) -> CircuitBreaker:
        """The breaker, so the collector can log its state after a cycle."""
        return self._circuit

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HudleClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- public API --------------------------------------------------------

    def fetch_slots(
        self,
        venue_uuid: str,
        facility_uuid: str,
        start_date: dt.date,
        end_date: dt.date,
    ) -> dict[str, Any]:
        """The slot grid for one facility over an inclusive date range.

        A 31-day range is served in a single call, so the horizon costs one
        request per facility per cycle.
        """
        path = f"/api/v1/web/venues/{venue_uuid}/facilities/{facility_uuid}/slots"
        return self._get_json(
            self._http.api_base + path,
            path=path,
            params={
                "start_date": to_date_text(start_date),
                "end_date": to_date_text(end_date),
                "grid": 1,
            },
            facility_uuid=facility_uuid,
        )

    def search_venues(
        self,
        sport_id: int,
        page: int = 1,
        per_page: int = 50,
        *,
        city_id: int = DEFAULT_CITY_ID,
        venue_name: str = "",
    ) -> dict[str, Any]:
        """One page of venue search results, envelope and all."""
        path = "/api/v1/venue-search"
        return self._get_json(
            self._http.api_base + path,
            path=path,
            params={
                "page": page,
                "per_page": per_page,
                "cityId": city_id,
                "venueName": venue_name,
                "preferred_sports": sport_id,
            },
        )

    def search_venues_all(
        self,
        sport_id: int,
        *,
        per_page: int = 50,
        city_id: int = DEFAULT_CITY_ID,
    ) -> list[dict[str, Any]]:
        """Every venue for a sport, walking ``meta.pagination`` to exhaustion.

        Padel fits on one page; pickleball is 57 venues across two, and a single
        call silently truncates at 50, so exhausting the pagination block is the
        only way to know the real venue set.
        """
        venues: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_SEARCH_PAGES:
            payload = self.search_venues(sport_id, page, per_page, city_id=city_id)
            rows = payload.get("data") or []
            venues.extend(rows)
            pagination = self._pagination(payload)
            total = _as_int(pagination.get("total"))
            total_pages = _as_int(pagination.get("total_pages"))
            if not rows:
                break
            if total is not None and len(venues) >= total:
                break
            if total_pages is not None and page >= total_pages:
                break
            if total is None and total_pages is None:
                # No pagination counters at all: treat the response as complete
                # rather than guessing at a second page.
                break
            page += 1
        else:
            logger.warning(
                "hudle_search_page_ceiling_hit",
                extra={"sport_id": sport_id, "pages": MAX_SEARCH_PAGES, "venues": len(venues)},
            )
        logger.info(
            "hudle_search_venues_all",
            extra={"sport_id": sport_id, "pages": page, "venues": len(venues)},
        )
        return venues

    def fetch_venue_detail(self, venue_uuid: str) -> dict[str, Any]:
        """The venue record. Its ``activities`` carry no facility UUIDs."""
        path = f"/api/v1/venues/{venue_uuid}"
        return self._get_json(self._http.api_base + path, path=path)

    def fetch_venue_page_html(self, slug: str, numeric_id: str) -> str:
        """The server-rendered venue page, the only source of facility UUIDs.

        This is the public website rather than the API, so it is fetched with
        browser headers and without the API secret.
        """
        path = f"/venues/{slug}/{numeric_id}"
        response = self._request(
            self._http.web_base + path,
            path=path,
            headers={"User-Agent": self._http.user_agent, "Accept": _HTML_ACCEPT},
        )
        return response.text

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _pagination(payload: dict[str, Any]) -> dict[str, Any]:
        """The counters, which live under ``meta.pagination``, not ``meta``."""
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            return {}
        pagination = meta.get("pagination")
        return pagination if isinstance(pagination, dict) else {}

    def _get_json(
        self,
        url: str,
        *,
        path: str,
        params: dict[str, Any] | None = None,
        facility_uuid: str | None = None,
    ) -> dict[str, Any]:
        response = self._request(url, path=path, params=params, facility_uuid=facility_uuid)
        try:
            return self._parse_envelope(response, path=path)
        except HudleApiError as exc:
            # A refusal dressed as a 200 is still a failed call: it must be able
            # to trip the breaker, or a bad secret would loop forever.
            self._circuit.record_failure(str(exc))
            raise

    @staticmethod
    def _parse_envelope(response: httpx.Response, *, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HudleApiError(f"hudle returned a non-JSON body for {path}", path=path) from exc
        if not isinstance(payload, dict):
            raise HudleApiError(
                f"hudle returned a {type(payload).__name__}, not an envelope, for {path}",
                path=path,
            )
        if payload.get("success") is False:
            raise HudleApiError(
                f"hudle reported success=false for {path}: {payload.get('message')}",
                path=path,
                code=_as_int(payload.get("code")),
                payload=payload,
            )
        return payload

    def _request(
        self,
        url: str,
        *,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        facility_uuid: str | None = None,
    ) -> httpx.Response:
        """Send one GET, retrying a bounded number of times.

        Every attempt -- the first one included -- waits on the rate limiter
        first and checks the breaker before that, so a retry can never be
        cheaper or faster than a fresh request.
        """
        send_headers = self._http.request_headers() if headers is None else headers
        for attempt in range(1, self._max_attempts + 1):
            self._circuit.check()
            self._rate_limiter.acquire()
            started = time.monotonic()
            try:
                response = self._client.get(url, params=params, headers=send_headers)
            except httpx.HTTPError as exc:
                duration_ms = _elapsed_ms(started)
                message = f"{type(exc).__name__}: {exc}"
                self._circuit.record_failure(message)
                logger.warning(
                    "hudle_request_transport_error",
                    extra={
                        "path": path,
                        "facility_uuid": facility_uuid,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "duration_ms": duration_ms,
                        "error": message,
                        "consecutive_failures": self._circuit.consecutive_failures,
                    },
                )
                if attempt >= self._max_attempts:
                    raise HudleTransportError(message, path=path) from exc
                self._wait_before_retry(attempt)
                continue

            duration_ms = _elapsed_ms(started)
            if response.status_code == 404:
                logger.warning(
                    "hudle_request_not_found",
                    extra={
                        "path": path,
                        "facility_uuid": facility_uuid,
                        "duration_ms": duration_ms,
                    },
                )
                raise HudleHttpError(response.status_code, response.text, path=path)
            if response.status_code != 200:
                self._circuit.record_failure(f"HTTP {response.status_code}")
                logger.warning(
                    "hudle_request_failed",
                    extra={
                        "path": path,
                        "facility_uuid": facility_uuid,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                        "consecutive_failures": self._circuit.consecutive_failures,
                    },
                )
                if attempt >= self._max_attempts:
                    raise HudleHttpError(response.status_code, response.text, path=path)
                self._wait_before_retry(attempt)
                continue

            self._circuit.record_success()
            logger.info(
                "hudle_request_ok",
                extra={
                    "path": path,
                    "facility_uuid": facility_uuid,
                    "attempt": attempt,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "bytes": len(response.content),
                },
            )
            return response

        # Unreachable: the loop either returns or raises on its final attempt.
        raise HudleTransportError(f"no attempt was made for {path}", path=path)

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self._delays[attempt - 1]
        logger.info(
            "hudle_request_backoff",
            extra={"attempt": attempt, "delay_seconds": delay},
        )
        if delay > 0:
            time.sleep(delay)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _as_int(value: Any) -> int | None:
    """Coerce a pagination counter, which Hudle sometimes sends as a string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        return None
