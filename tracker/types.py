"""Domain vocabulary shared by the slot parser, discovery and the daily pass.

Everything here is a plain dataclass, enum or pure function: no SQLAlchemy, no
httpx. Local wall-clock times are carried as the naive strings Hudle returned,
alongside an explicit ``tz`` name, and turned into aware UTC only through the
helpers below.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

#: Canonical text encoding for the naive local wall-clock columns, which is
#: exactly the shape Hudle returns (``2026-09-11 06:00:00``).
LOCAL_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Canonical text encoding for dates, as Hudle's grid endpoint takes them.
DATE_TEXT_FORMAT = "%Y-%m-%d"


class FacilityKind(StrEnum):
    """Whether a Hudle facility is sellable court time or a rental item.

    Frozen in ``config.yaml`` and mirrored into the database; never inferred at
    write time.
    """

    COURT = "court"
    EQUIPMENT = "equipment"


class Sport(StrEnum):
    PADEL = "padel"
    PICKLEBALL = "pickleball"


# --------------------------------------------------------------------------
# Text <-> value conversion at the Hudle edge
# --------------------------------------------------------------------------


def from_local_text(value: str) -> dt.datetime:
    """Decode Hudle's naive local wall-clock text. The result stays naive."""
    return dt.datetime.strptime(value, LOCAL_TEXT_FORMAT)


def to_date_text(value: dt.date) -> str:
    return value.strftime(DATE_TEXT_FORMAT)


# --------------------------------------------------------------------------
# Load-bearing derivations, kept here so every layer computes them identically
# --------------------------------------------------------------------------


def business_date_for(slot_start_local: dt.datetime, business_day_start_hour: int) -> dt.date:
    """The trading day a slot belongs to.

    Play Padel sells 00:00-01:30 and Hudle stamps those slots with the calendar
    date they fall on, so Friday-night demand lands on Saturday. Slots starting
    before ``business_day_start_hour`` roll back one day. The wall-clock columns
    are never mutated; this is an extra fact, not a rewrite.
    """
    local_date = slot_start_local.date()
    if slot_start_local.hour < business_day_start_hour:
        return local_date - dt.timedelta(days=1)
    return local_date


def local_wall_clock(observed_at: dt.datetime, tz: str) -> dt.datetime:
    """Project an aware UTC instant onto naive local wall-clock in ``tz``."""
    return observed_at.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def slot_start_utc_for(slot_start_local: dt.datetime, tz: str) -> dt.datetime:
    """Attach ``tz`` to a naive local wall-clock start and return aware UTC."""
    return slot_start_local.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.UTC)


def duration_minutes_for(slot_start_local: dt.datetime, slot_end_local: dt.datetime) -> int:
    """Slot length in minutes, tolerating a midnight-crossing end time."""
    delta = slot_end_local - slot_start_local
    if delta <= dt.timedelta(0):
        delta += dt.timedelta(days=1)
    return int(delta.total_seconds() // 60)


# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VenueDim:
    venue_uuid: str
    name: str
    short_name: str
    slug: str
    numeric_id: str
    tz: str
    active: bool
    first_seen: dt.datetime
    last_seen: dt.datetime


@dataclass(frozen=True, slots=True)
class DiscoveredFacility:
    """What the SSR page said about one facility, for drift auditing."""

    venue_uuid: str
    facility_uuid: str
    facility_name: str
    activity_id: int | None
    activity_name: str | None
    in_config: bool
    suggested_kind: FacilityKind
