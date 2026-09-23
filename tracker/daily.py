"""The daily pass, and the discovery that decides what it polls.

Once a day, every tracked court: fetch yesterday through two weeks ahead, record
what changed. Yesterday is in the window because a date's final state is only
known once it has elapsed, and Hudle keeps serving it; the two weeks ahead are
what make the booking-time cross-check possible (a slot vacant in yesterday's
pass and booked in today's must carry an ``updated_at`` between the two).

Requests are sequential and spaced by the client's 15s floor. Hudle's gateway
sometimes times out (HTTP 502) building a long grid, so a failed range is
retried as smaller pieces rather than resent whole.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tracker.config import Config
from tracker.discover import extract_next_data, parse_facilities, search_pickleball_venues
from tracker.hudle import CircuitOpenError, HudleClient, HudleError, HudleHttpError
from tracker.slots import parse_grid
from tracker.store import Court, Store
from tracker.types import FacilityKind, Sport, local_wall_clock

logger = logging.getLogger("tracker.daily")

LOOKBACK_DAYS = 1
AHEAD_DAYS = 14
#: Pieces a failed range is split into before giving up on the court.
SPLIT_PIECES = 3


@dataclass(frozen=True, slots=True)
class DailyResult:
    run_id: int
    courts_ok: int
    courts_failed: int
    slots_seen: int
    slots_written: int
    stopped_early: bool

    @property
    def ok(self) -> bool:
        return self.courts_failed == 0 and not self.stopped_early


def daily_window(now: dt.datetime, tz: str) -> tuple[dt.date, dt.date]:
    """Yesterday through two weeks ahead, in Jaipur dates."""
    today = local_wall_clock(now, tz).date()
    return today - dt.timedelta(days=LOOKBACK_DAYS), today + dt.timedelta(days=AHEAD_DAYS)


def fetch_range(
    client: HudleClient, venue_uuid: str, facility_uuid: str, start: dt.date, end: dt.date
) -> dict[str, Any]:
    """One court's grid for a date range; on a Hudle 5xx, the range in pieces.

    The pieces' day lists are concatenated, so the result has the shape of a
    single response either way.
    """
    try:
        return client.fetch_slots(venue_uuid, facility_uuid, start, end)
    except HudleHttpError as exc:
        if exc.status < 500:
            raise
        logger.warning(
            "daily_range_split", extra={"facility_uuid": facility_uuid, "status": exc.status}
        )
    span = (end - start).days + 1
    step = max(1, -(-span // SPLIT_PIECES))
    merged: dict[str, Any] | None = None
    cursor = start
    while cursor <= end:
        piece_end = min(end, cursor + dt.timedelta(days=step - 1))
        piece = client.fetch_slots(venue_uuid, facility_uuid, cursor, piece_end)
        if merged is None:
            merged = piece
        else:
            merged["data"]["slot_data"].extend((piece.get("data") or {}).get("slot_data") or [])
        cursor = piece_end + dt.timedelta(days=1)
    assert merged is not None
    return merged


def run_daily(
    config: Config,
    store: Store,
    client: HudleClient,
    *,
    now: dt.datetime,
    courts: Sequence[Court] | None = None,
    window: tuple[dt.date, dt.date] | None = None,
) -> DailyResult:
    """Poll every tracked court once and record what changed.

    One court failing is recorded and stepped over; an open circuit breaker
    stops the pass, because it means Hudle is refusing us and more requests
    would only make that worse.
    """
    window = window or daily_window(now, config.timezone)
    courts = list(courts) if courts is not None else store.tracked_courts()
    run_id = store.start_run("daily", started_at=now, window=window)
    ok = failed = seen = written = 0
    stopped = False
    errors: list[str] = []
    for court in courts:
        try:
            payload = fetch_range(client, court.venue_uuid, court.facility_uuid, *window)
        except CircuitOpenError as exc:
            stopped = True
            errors.append(f"circuit open before {court.name}: {exc}")
            logger.error("daily_stopped", extra={"reason": "circuit_open", "court": court.name})
            break
        except HudleError as exc:
            failed += 1
            errors.append(f"{court.facility_uuid}: {exc}")
            logger.warning("daily_court_failed", extra={"court": court.name, "error": str(exc)})
            continue
        readings = parse_grid(
            payload,
            venue_uuid=court.venue_uuid,
            facility_uuid=court.facility_uuid,
            sport=court.sport,
            tz=config.timezone,
            business_day_start_hour=config.business_day_start_hour,
        )
        changed = store.apply(readings, seen_at=now)
        ok += 1
        seen += len(readings)
        written += changed
        logger.info(
            "daily_court_done",
            extra={
                "court": court.name,
                "sport": court.sport.value,
                "slots": len(readings),
                "booked": sum(r.booked for r in readings),
                "written": changed,
            },
        )
    store.finish_run(
        run_id,
        finished_at=dt.datetime.now(dt.UTC),
        courts_ok=ok,
        courts_failed=failed,
        slots_seen=seen,
        slots_written=written,
        error="; ".join(errors)[:4000] or None,
    )
    return DailyResult(run_id, ok, failed, seen, written, stopped)


def seed_configured_courts(config: Config, store: Store, *, now: dt.datetime) -> int:
    """Put the courts named in config.yaml (the padel set) into the store."""
    count = 0
    for venue in config.venues:
        if not venue.active:
            continue
        courts = [f for f in venue.facilities if f.active and f.kind is FacilityKind.COURT]
        if not courts:
            continue
        store.upsert_venue(
            venue_uuid=venue.uuid,
            name=venue.name,
            slug=venue.slug,
            numeric_id=venue.numeric_id,
            seen_at=now,
        )
        for facility in courts:
            store.upsert_court(
                facility_uuid=facility.uuid,
                venue_uuid=venue.uuid,
                name=facility.name,
                sport=facility.sport,
                seen_at=now,
            )
            count += 1
    return count


class _SearchPages:
    """Adapts HudleClient to discovery's page-returning search protocol.

    The client already walks Hudle's pagination to exhaustion; its venues are
    handed over as one page, so discovery parses them without re-paginating.
    """

    def __init__(self, client: HudleClient) -> None:
        self._client = client

    def search_venues_all(
        self, *, sport_id: int, city_id: int, per_page: int
    ) -> list[dict[str, Any]]:
        venues = self._client.search_venues_all(sport_id, per_page=per_page, city_id=city_id)
        return [{"data": venues}]


def discover_pickleball(
    config: Config, store: Store, client: HudleClient, *, now: dt.datetime
) -> tuple[int, int]:
    """Every pickleball venue in the city and its courts, into the store.

    Racket and ball rentals are skipped by name; anything named "court" is kept
    regardless (``Pickleball Court`` contains the token "ball"). A venue whose
    page cannot be read is skipped and retried next week rather than failing
    the pass. Returns (venues seen, courts seen).
    """
    result = search_pickleball_venues(_SearchPages(client), config=config, observed_at=now)
    venues_seen = courts_seen = 0
    for venue in result.venues:
        try:
            details = extract_next_data(client.fetch_venue_page_html(venue.slug, venue.numeric_id))
            facilities = parse_facilities(details, config=config)
        except Exception as exc:  # one unreadable page must not stop discovery
            logger.warning("discover_venue_failed", extra={"venue": venue.name, "error": repr(exc)})
            continue
        picked = [
            f
            for f in facilities
            if "pickle" in str(f.activity_name or "").lower()
            and f.suggested_kind is FacilityKind.COURT
        ]
        if not picked:
            continue
        store.upsert_venue(
            venue_uuid=venue.venue_uuid,
            name=venue.name,
            slug=venue.slug,
            numeric_id=venue.numeric_id,
            seen_at=now,
        )
        venues_seen += 1
        for f in picked:
            store.upsert_court(
                facility_uuid=f.facility_uuid,
                venue_uuid=venue.venue_uuid,
                name=f.facility_name,
                sport=Sport.PICKLEBALL,
                seen_at=now,
            )
            courts_seen += 1
    logger.info(
        "discover_done",
        extra={"venues": venues_seen, "courts": courts_seen, "search_complete": result.complete},
    )
    return venues_seen, courts_seen
