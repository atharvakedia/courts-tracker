"""One Hudle slot, read once, as the fact the tracker stores.

A slot is either **booked** or **vacant**. Hudle reports two flags, and a slot
counts as booked when a customer bought it (``is_booked``) *or* the venue made
it unavailable (``is_available`` false) -- venues block slots to record sales
made off-platform, and an hour held every day ahead is sold time too. The raw
flags are kept beside the verdict, because *how* a slot became unavailable is
the evidence for whether a venue actually runs its bookings on Hudle.

**When it was booked** comes from Hudle's ``updated_at``: the last time Hudle
changed the slot, which for a booked slot is the booking. Verified on real data:
booked slots carry it weeks after creation where open ones carry it hours after,
slots bought together share it to the second, every one precedes its slot's
start, and it is Jaipur wall-clock (read as UTC, 89 of 546 would postdate the
poll that saw them booked). It is a last-modified stamp, not a booking field: a
cancel-and-rebook keeps only the final booking.

All Hudle times are naive Asia/Kolkata wall-clock. They become aware UTC here,
at the edge, and nowhere later.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from tracker.types import (
    Sport,
    business_date_for,
    duration_minutes_for,
    from_local_text,
    slot_start_utc_for,
)


@dataclass(frozen=True, slots=True)
class SlotReading:
    """What one fetch said about one slot."""

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    sport: Sport
    business_date: dt.date
    start_local: dt.datetime  # naive Jaipur wall-clock, as Hudle publishes it
    start_utc: dt.datetime
    duration_minutes: int
    price: float | None
    hudle_booked: bool
    hudle_available: bool
    upstream_created_at: dt.datetime | None
    upstream_updated_at: dt.datetime | None

    @property
    def booked(self) -> bool:
        """The one rule: sold through Hudle, or taken off sale by the venue."""
        return self.hudle_booked or not self.hudle_available


def parse_slot(
    raw: Mapping[str, Any],
    *,
    venue_uuid: str,
    facility_uuid: str,
    sport: Sport,
    tz: str,
    business_day_start_hour: int,
) -> SlotReading:
    start_local = from_local_text(str(raw["start_time"]))
    end_local = from_local_text(str(raw["end_time"]))
    return SlotReading(
        slot_uuid=str(raw["id"]),
        venue_uuid=venue_uuid,
        facility_uuid=facility_uuid,
        sport=sport,
        business_date=business_date_for(start_local, business_day_start_hour),
        start_local=start_local,
        start_utc=slot_start_utc_for(start_local, tz),
        duration_minutes=duration_minutes_for(start_local, end_local),
        price=_price(raw.get("price")),
        hudle_booked=bool(raw.get("is_booked")),
        hudle_available=bool(raw.get("is_available")),
        upstream_created_at=_stamp(raw.get("created_at"), tz),
        upstream_updated_at=_stamp(raw.get("updated_at"), tz),
    )


def parse_grid(
    payload: Mapping[str, Any],
    *,
    venue_uuid: str,
    facility_uuid: str,
    sport: Sport,
    tz: str,
    business_day_start_hour: int,
) -> list[SlotReading]:
    """Every slot in a ``/slots`` response that Hudle has created, in published order.

    A slot with no ``id`` is one Hudle shows but has not created yet (its
    ``created_at`` is blank too); seen only on days not yet played. It is left
    out: without Hudle's id it has no stable identity, and a made-up one would
    count the slot twice once Hudle creates it. A day is only counted after it
    is played, and every played day seen so far has its ids.
    """
    days: Iterable[Mapping[str, Any]] = (payload.get("data") or {}).get("slot_data") or []
    return [
        parse_slot(
            raw,
            venue_uuid=venue_uuid,
            facility_uuid=facility_uuid,
            sport=sport,
            tz=tz,
            business_day_start_hour=business_day_start_hour,
        )
        for day in days
        for raw in day.get("slots") or []
        if raw.get("id")
    ]


def _price(value: object) -> float | None:
    try:
        return None if value in (None, "") else float(str(value))
    except ValueError:
        return None


def _stamp(value: object, tz: str) -> dt.datetime | None:
    """A Hudle row timestamp as aware UTC; absent or malformed reads as None."""
    if not value:
        return None
    try:
        return slot_start_utc_for(from_local_text(str(value)), tz)
    except ValueError:
        return None
