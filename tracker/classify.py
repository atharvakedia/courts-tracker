"""Slot-state classification, grid parsing and court-minute normalization.

This module is pure. It performs no I/O, opens no database, makes no HTTP
request and never calls :func:`datetime.now`. Every time-dependent fact is
derived from an ``observed_at`` instant the caller supplies, so the same raw
payload always yields the same observations.

Three ideas are load-bearing here:

**``is_booked`` wins over ``is_available``.** Hudle reports a paid slot as
``is_available: true, is_booked: true``. Reading availability first would
classify a sold slot as OPEN and erase the headline number.

**Pastness is orthogonal to state.** Hudle never marks elapsed slots
unavailable: at 16:21 IST on 2026-09-11 Padel Fort's 07:00 and 16:00 slots from
that same morning still read ``is_available: true, is_booked: false``. An
elapsed unsold slot is therefore genuinely OPEN -- it really was sellable
inventory that went unsold, so it belongs in a retrospective denominator.
Pastness is recorded separately as :attr:`SlotObservation.is_past`, computed by
us from ``slot_start_utc < observed_at``. Any "still winnable",
current-availability or time-to-sellout view must filter on ``is_past`` itself,
never on the state enum.

**Nothing is comparable as a slot count.** Padel Up sells 60-minute slots while
Play Padel and Padel Fort sell 30-minute ones, so one Padel Up slot is two
Padel Fort slots. Every quantity this module produces is in court-minutes,
measured per slot from its own start and end times, never assumed from config.
The same trap applies to price: Play Padel's 1000 per slot is the most
expensive court in the city at 2000 per court-hour.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from tracker.types import (
    DEFAULT_TZ,
    SlotObservation,
    SlotState,
    Sport,
    business_date_for,
    classify_slot_state,
    days_ahead_for,
    duration_minutes_for,
    from_local_text,
    slot_start_utc_for,
)

logger = logging.getLogger("tracker.classify")

MINUTES_PER_DAY = 24 * 60

#: Keys a raw Hudle slot must carry for classification to mean anything.
_REQUIRED_SLOT_KEYS = (
    "id",
    "start_time",
    "end_time",
    "is_available",
    "is_booked",
    "total_count",
    "available_count",
)


class SlotParseError(ValueError):
    """A raw slot could not be turned into an observation.

    Raised loudly rather than skipped: a malformed or misattributed slot that
    is silently dropped becomes a permanent hole in a dataset that cannot be
    backfilled.
    """


@dataclass(frozen=True, slots=True)
class Occupancy:
    """An occupancy ratio that always carries its own arithmetic.

    ``ratio`` is ``None`` -- never ``0.0`` -- when ``denominator_minutes`` is
    zero. A venue that pulled a whole evening from inventory has no sellable
    minutes at all, which is a different fact from selling none of them, and
    conflating the two is how a blocked evening comes to look like empty
    courts. The numerator and denominator travel with the ratio so every chart
    can print the denominator it was computed from.
    """

    ratio: float | None
    numerator_minutes: int
    denominator_minutes: int


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify_slot(raw: Mapping[str, Any]) -> SlotState:
    """Classify one raw Hudle slot into BOOKED, BLOCKED or OPEN.

    ``is_booked`` wins over ``is_available``. A slot that is neither booked nor
    available is BLOCKED: the venue closed it, it is under maintenance, or it
    was sold offline. Pastness is deliberately not consulted -- see the module
    docstring.

    ``available_count`` is *not* used. It happens to be 0 for every booked slot
    at all three venues today because ``total_count`` is always 1, but a
    multi-court facility would break that equivalence, and a blocked slot keeps
    ``available_count: 1``.
    """
    return classify_slot_state(
        is_available=bool(_require(raw, "is_available")),
        is_booked=bool(_require(raw, "is_booked")),
    )


def parse_slot(
    raw: Mapping[str, Any],
    *,
    snapshot_id: int,
    observed_at: dt.datetime,
    venue_uuid: str,
    facility_uuid: str,
    sport: Sport,
    business_day_start_hour: int,
    tz: str = DEFAULT_TZ,
) -> SlotObservation:
    """Turn one raw Hudle slot into an immutable :class:`SlotObservation`.

    ``observed_at`` must be timezone-aware; a naive instant is rejected rather
    than guessed at. The naive local wall-clock strings Hudle returns are kept
    verbatim alongside an explicit ``tz``, and the aware UTC start is derived
    from them, so no naive datetime escapes this function.

    ``sport`` is required and comes from ``config.yaml``, exactly like
    ``FacilityKind``: the slot payload carries no sport, and two of the three
    venues sell pickleball alongside padel, so a stream that cannot be split
    by sport silently compares a venue's three courts against another's one.

    ``duration_minutes`` is measured from this slot's own start and end,
    tolerating the midnight wrap (``23:30 -> 00:00`` is 30 minutes, not
    -1410). ``business_date`` rolls a slot starting before
    ``business_day_start_hour`` back one day so Play Padel's 00:30 Saturday
    sales are attributed to the Friday evening session that produced them; the
    wall-clock fields are never rewritten.

    ``days_ahead`` comes from :func:`tracker.types.days_ahead_for`, which
    compares raw local calendar dates rather than business dates. That is the
    frozen repo-wide definition, so a post-midnight slot observed the previous
    evening reads ``days_ahead == 1`` while its ``business_date`` is that same
    evening. Bucket on ``business_date`` for trading-day questions.
    """
    if observed_at.tzinfo is None:
        raise SlotParseError("observed_at must be timezone-aware")

    for key in _REQUIRED_SLOT_KEYS:
        _require(raw, key)

    raw_facility_uuid = raw.get("facility_uuid")
    if raw_facility_uuid is not None and raw_facility_uuid != facility_uuid:
        raise SlotParseError(
            f"slot {raw['id']} belongs to facility {raw_facility_uuid}, "
            f"not {facility_uuid}; grids must never be mixed"
        )

    slot_start_local = from_local_text(str(raw["start_time"]))
    slot_end_local = from_local_text(str(raw["end_time"]))
    slot_start_utc = slot_start_utc_for(slot_start_local, tz)
    total_count = int(raw["total_count"])
    if total_count != 1:
        # Every one of the 2945 recorded slots reports total_count 1, which is
        # why occupancy is boolean per slot and court_minutes ignores capacity.
        # A multi-court facility would break that silently, so it is logged the
        # first time it happens rather than discovered in a wrong percentage.
        logger.warning(
            "slot_total_count_not_one",
            extra={
                "facility_uuid": facility_uuid,
                "slot_uuid": str(raw["id"]),
                "total_count": total_count,
                "available_count": int(raw["available_count"]),
            },
        )

    return SlotObservation(
        snapshot_id=snapshot_id,
        slot_uuid=str(raw["id"]),
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        slot_start_local=str(raw["start_time"]),
        slot_end_local=str(raw["end_time"]),
        tz=tz,
        slot_start_utc=slot_start_utc,
        duration_minutes=duration_minutes_for(slot_start_local, slot_end_local),
        price=_parse_price(raw.get("price")),
        total_count=total_count,
        available_count=int(raw["available_count"]),
        is_available=bool(raw["is_available"]),
        is_booked=bool(raw["is_booked"]),
        state=classify_slot(raw),
        days_ahead=days_ahead_for(slot_start_local, observed_at, tz),
        business_date=business_date_for(slot_start_local, business_day_start_hour),
        is_past=slot_start_utc < observed_at,
        upstream_created_at=_upstream_stamp(raw.get("created_at"), tz),
        upstream_updated_at=_upstream_stamp(raw.get("updated_at"), tz),
    )


def _upstream_stamp(value: object, tz: str) -> dt.datetime | None:
    """Hudle's naive local row timestamp as an aware UTC datetime, or None.

    The payload documents nothing about these fields, so a missing or
    malformed value is treated as absent rather than as an error: losing one
    timestamp is not a reason to drop the observation that carries it.
    """
    if not value:
        return None
    try:
        return slot_start_utc_for(from_local_text(str(value)), tz)
    except ValueError:
        return None


def parse_slot_grid(
    payload: Mapping[str, Any],
    *,
    snapshot_id: int,
    observed_at: dt.datetime,
    venue_uuid: str,
    facility_uuid: str,
    sport: Sport,
    business_day_start_hour: int,
    tz: str = DEFAULT_TZ,
) -> list[SlotObservation]:
    """Parse a whole ``.../slots?grid=1`` payload into observations.

    Walks ``data.slot_data[]`` and each day's ``slots[]``. A day flagged
    ``is_empty``, a day whose ``slots`` key is missing, and a day with an empty
    ``slots`` list all contribute nothing; each is logged so a venue that
    stopped publishing inventory is visible rather than indistinguishable from
    a venue with no bookings.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        logger.warning(
            "slot_grid_missing_data",
            extra={"facility_uuid": facility_uuid, "snapshot_id": snapshot_id},
        )
        return []

    observations: list[SlotObservation] = []
    for day in data.get("slot_data") or ():
        slots = day.get("slots") or ()
        is_empty = bool(day.get("is_empty"))
        if is_empty and slots:
            logger.warning(
                "slot_grid_day_empty_with_slots",
                extra={
                    "facility_uuid": facility_uuid,
                    "date": day.get("date"),
                    "slot_count": len(slots),
                },
            )
        if is_empty or not slots:
            logger.info(
                "slot_grid_day_skipped",
                extra={
                    "facility_uuid": facility_uuid,
                    "date": day.get("date"),
                    "is_empty": is_empty,
                    "slot_count": len(slots),
                },
            )
            continue
        for raw in slots:
            observations.append(
                parse_slot(
                    raw,
                    snapshot_id=snapshot_id,
                    observed_at=observed_at,
                    venue_uuid=venue_uuid,
                    facility_uuid=facility_uuid,
                    sport=sport,
                    business_day_start_hour=business_day_start_hour,
                    tz=tz,
                )
            )
    return observations


def grid_minutes_from_payload(payload: Mapping[str, Any]) -> int | None:
    """Infer a facility's slot granularity from ``data.slot_timings``.

    Returns the most common timing length in minutes, or ``None`` when the
    payload carries no usable timings. Two shapes have to survive:

    * a non-contiguous selling window -- Play Padel sells 00:00-01:30 *and*
      06:00-23:30, so the gap between consecutive timings is not the grid and
      differencing start times would report 270 minutes across the hole;
    * the midnight wrap -- the final timing runs ``23:30 -> 00:00``, whose
      naive difference is -1410. A fully degenerate ``00:00 -> 00:00`` entry
      carries no length at all and is discarded rather than reported as 1440.
    """
    data = payload.get("data")
    timings: Iterable[Mapping[str, Any]] = ()
    if isinstance(data, Mapping):
        timings = data.get("slot_timings") or ()

    lengths = Counter(
        length
        for timing in timings
        if (length := _timing_minutes(timing.get("from"), timing.get("to"))) is not None
    )
    if not lengths:
        logger.warning("grid_minutes_undeterminable", extra={"timing_count": len(list(timings))})
        return None

    most_common = max(lengths.values())
    return min(length for length, count in lengths.items() if count == most_common)


# --------------------------------------------------------------------------
# Court-minute normalization and occupancy
# --------------------------------------------------------------------------


def court_minutes(
    observations: Iterable[SlotObservation], *, state: SlotState | None = None
) -> int:
    """Total court-minutes in ``observations``, optionally for one state only.

    Court-minutes, never slot counts: a 60-minute Padel Up slot and a 30-minute
    Padel Fort slot are both "1 slot" and that comparison is always wrong.

    The caller owns de-duplication. These are observations, not slots: the same
    slot appears once per poll that saw it, so summing a multi-snapshot stream
    multiplies every figure by the number of polls. Reduce with
    :func:`tracker.analytics.occupancy.settled_observations` first (the
    ``v_slot_settled`` view applies the same rule in SQL).
    """
    return sum(o.duration_minutes for o in observations if state is None or o.state is state)


def court_minutes_by_state(observations: Iterable[SlotObservation]) -> dict[SlotState, int]:
    """Court-minutes per state, with all three states always present.

    BLOCKED is reported as its own number on purpose. A venue that blocks slots
    to sell them offline otherwise reads as an empty venue, and Padel Fort
    really did pull 2026-09-13 17:00-23:30 -- 420 court-minutes, 14 slots --
    from inventory with zero bookings.
    """
    totals = dict.fromkeys(SlotState, 0)
    for observation in observations:
        totals[observation.state] += observation.duration_minutes
    return totals


def occupancy_strict(observations: Iterable[SlotObservation]) -> Occupancy:
    """The headline metric: booked / (booked + open) in court-minutes.

    Blocked minutes are excluded from both sides, so this answers "of the court
    time that was actually sellable, how much sold". ``ratio`` is ``None`` when
    nothing was sellable.
    """
    totals = court_minutes_by_state(observations)
    booked = totals[SlotState.BOOKED]
    return _occupancy(booked, booked + totals[SlotState.OPEN])


def occupancy_gross(observations: Iterable[SlotObservation]) -> Occupancy:
    """(booked + blocked) / total in court-minutes.

    The share of the published day a walk-up customer could not have booked,
    whatever the reason. Always reported alongside :func:`occupancy_strict`:
    the gap between the two is the blocked share, and it is the only way an
    offline-selling venue is visible at all.
    """
    totals = court_minutes_by_state(observations)
    unavailable = totals[SlotState.BOOKED] + totals[SlotState.BLOCKED]
    return _occupancy(unavailable, unavailable + totals[SlotState.OPEN])


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _require(raw: Mapping[str, Any], key: str) -> Any:
    """Fetch a required raw-slot key or raise naming it."""
    if key not in raw:
        raise SlotParseError(f"raw slot is missing required key {key!r}")
    value = raw[key]
    if value is None:
        raise SlotParseError(f"raw slot key {key!r} is null")
    return value


def _parse_price(value: Any) -> float | None:
    """Coerce Hudle's string price (``"1800.00"``) to a float.

    Stored per slot, exactly as reported. Per-hour normalization belongs to
    whoever displays it: 1000 per 30-minute slot is 2000 per court-hour.
    """
    if value is None or value == "":
        return None
    return float(value)


def _timing_minutes(start: Any, end: Any) -> int | None:
    """Length of one ``slot_timings`` entry, or ``None`` if it carries none."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    minutes = _clock_minutes(end) - _clock_minutes(start)
    if minutes <= 0:
        minutes += MINUTES_PER_DAY
    if minutes >= MINUTES_PER_DAY:
        return None
    return minutes


def _clock_minutes(value: str) -> int:
    """Minutes since local midnight for an ``HH:MM:SS`` wall-clock string."""
    parts = value.split(":")
    if len(parts) < 2:
        raise SlotParseError(f"unparseable slot timing {value!r}")
    return int(parts[0]) * 60 + int(parts[1])


def _occupancy(numerator: int, denominator: int) -> Occupancy:
    if denominator == 0:
        return Occupancy(ratio=None, numerator_minutes=numerator, denominator_minutes=0)
    return Occupancy(
        ratio=numerator / denominator,
        numerator_minutes=numerator,
        denominator_minutes=denominator,
    )
