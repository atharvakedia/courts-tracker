"""Domain vocabulary shared by the collector, analytics and web layers.

Everything here is a plain dataclass or enum: no SQLAlchemy, no pydantic, no
httpx. These are the objects that cross the `Storage` boundary in both
directions, so analytics and web never see a database Row.

Timezone discipline: every `datetime` that lives on a dataclass here is
timezone-aware UTC. Local wall-clock times are carried as the naive strings
Hudle returned, alongside an explicit `tz` name, and never as naive datetimes
crossing a function boundary.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

#: Canonical text encoding for UTC instants in the database (TEXT columns).
UTC_TEXT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Canonical text encoding for the naive local wall-clock columns, which is
#: exactly the shape Hudle returns (``2026-09-11 06:00:00``).
LOCAL_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Canonical text encoding for date-only columns (``business_date``).
DATE_TEXT_FORMAT = "%Y-%m-%d"


class SlotState(StrEnum):
    """The three slot states, classified at write time.

    ``is_booked`` wins over ``is_available``: a slot that reports both is
    BOOKED. Pastness is deliberately *not* part of this enum -- Hudle never
    marks elapsed slots unavailable, so an elapsed unsold slot is genuinely
    OPEN. Use :attr:`SlotObservation.is_past` for that.
    """

    BOOKED = "BOOKED"
    BLOCKED = "BLOCKED"
    OPEN = "OPEN"


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
# Text <-> value conversion at the storage edge
# --------------------------------------------------------------------------


def to_utc_text(value: dt.datetime) -> str:
    """Encode an aware datetime as the canonical UTC text form."""
    if value.tzinfo is None:
        raise ValueError("naive datetime cannot be encoded as UTC text")
    return value.astimezone(dt.UTC).strftime(UTC_TEXT_FORMAT)


def from_utc_text(value: str) -> dt.datetime:
    """Decode canonical UTC text back into an aware UTC datetime."""
    return dt.datetime.strptime(value, UTC_TEXT_FORMAT).replace(tzinfo=dt.UTC)


def to_local_text(value: dt.datetime) -> str:
    """Encode a local wall-clock datetime as the naive text Hudle uses."""
    return value.strftime(LOCAL_TEXT_FORMAT)


def from_local_text(value: str) -> dt.datetime:
    """Decode Hudle's naive local wall-clock text. The result stays naive."""
    return dt.datetime.strptime(value, LOCAL_TEXT_FORMAT)


def to_date_text(value: dt.date) -> str:
    return value.strftime(DATE_TEXT_FORMAT)


def from_date_text(value: str) -> dt.date:
    return dt.datetime.strptime(value, DATE_TEXT_FORMAT).date()


# --------------------------------------------------------------------------
# Load-bearing derivations, kept here so every layer computes them identically
# --------------------------------------------------------------------------


def classify_slot_state(*, is_available: bool, is_booked: bool) -> SlotState:
    """Classify a raw Hudle slot. ``is_booked`` wins over ``is_available``."""
    if is_booked:
        return SlotState.BOOKED
    if not is_available:
        return SlotState.BLOCKED
    return SlotState.OPEN


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


def days_ahead_for(slot_start_local: dt.datetime, observed_at: dt.datetime, tz: str) -> int:
    """Whole local calendar days between the observation and the slot."""
    return (slot_start_local.date() - local_wall_clock(observed_at, tz).date()).days


def duration_minutes_for(slot_start_local: dt.datetime, slot_end_local: dt.datetime) -> int:
    """Slot length in minutes, tolerating a midnight-crossing end time."""
    delta = slot_end_local - slot_start_local
    if delta <= dt.timedelta(0):
        delta += dt.timedelta(days=1)
    return int(delta.total_seconds() // 60)


def local_hour_of(slot_start_local: str) -> int:
    """The wall-clock hour a slot starts in, from its stored local text.

    The one supported way to read an hour out of ``slot_start_local``. It lives
    here rather than in each analytics module because slicing the string by
    character offset -- ``slot_start_local[11:13]`` -- looks equivalent and is
    not: :func:`from_local_text` accepts a non-zero-padded hour, which
    ``parse_slot`` then stores verbatim, and the slice returns ``"6:"`` for it.
    Parsing keeps every layer agreeing on the answer.
    """
    return from_local_text(slot_start_local).hour


# --------------------------------------------------------------------------
# Observation records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotObservation:
    """One slot as seen in one poll. Append-only; never updated or deleted."""

    snapshot_id: int
    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    #: Which sport this court sells, mirrored from ``config.yaml`` at write
    #: time and never inferred. Two of the three venues publish pickleball
    #: courts as well as padel ones, so a venue-level total that cannot filter
    #: on sport compares a three-court venue against a one-court venue -- the
    #: same normalization failure as counting slots instead of court-minutes,
    #: on a different axis.
    sport: Sport
    slot_start_local: str
    slot_end_local: str
    tz: str
    slot_start_utc: dt.datetime
    duration_minutes: int
    price: float | None
    total_count: int
    available_count: int
    is_available: bool
    is_booked: bool
    state: SlotState
    days_ahead: int
    business_date: dt.date
    is_past: bool
    #: Hudle's own ``created_at`` / ``updated_at`` on the slot row, converted to
    #: UTC. ``updated_at`` moves when a slot is sold: booked slots sit ~30 days
    #: after their creation while open ones sit hours after it, consecutive
    #: slots bought together share it to the second, and every booking's
    #: value precedes its slot start. It is a last-modified stamp, not a
    #: booking field -- a cancel-and-rebook or an admin edit moves it too --
    #: so it is stored raw and interpreted by analytics next to the observed
    #: state, never treated as truth on its own.
    upstream_created_at: dt.datetime | None = None
    upstream_updated_at: dt.datetime | None = None


def slot_start_hour(observation: SlotObservation) -> int:
    """The local wall-clock hour an observed slot starts in.

    The observation-level spelling of :func:`local_hour_of`, and the only one.
    Every hour-bucketed metric -- demand by hour, the weekday heatmap, the
    price-by-hour table, peak / off-peak lead time -- takes its bucket key from
    here, so the four of them cannot drift into disagreeing about which hour a
    slot belongs to. Read from the naive local text rather than from
    ``slot_start_utc``, because "is 19:00 a peak slot" is a question about the
    venue's clock.
    """
    return local_hour_of(observation.slot_start_local)


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """One collect run. ``poll_key`` is the idempotency key."""

    snapshot_id: int
    poll_key: str
    observed_at: dt.datetime
    ok: bool
    error: str | None
    duration_ms: int | None
    horizon_days: int


@dataclass(frozen=True, slots=True)
class FacilityFetch:
    """One (snapshot, facility) HTTP attempt, so a per-venue gap is honest."""

    snapshot_id: int
    facility_uuid: str
    ok: bool
    http_status: int | None
    error: str | None
    duration_ms: int | None
    slot_count: int
    attempts: int


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
class FacilityDim:
    facility_uuid: str
    venue_uuid: str
    name: str
    kind: FacilityKind
    sport: Sport
    grid_minutes: int | None
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


# --------------------------------------------------------------------------
# Derived records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateTransition:
    """One observed state change for a slot.

    The true change happened somewhere inside the poll gap, so
    ``uncertainty_minutes`` (``first_seen_at - prev_seen_at``) is carried on
    every row and must be stated wherever lead-time percentiles are reported.
    """

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    from_state: SlotState | None
    to_state: SlotState
    first_seen_at: dt.datetime
    prev_seen_at: dt.datetime | None
    uncertainty_minutes: int | None
    slot_start_utc: dt.datetime
    days_ahead_at_change: int


@dataclass(frozen=True, slots=True)
class SlotFirstBooked:
    """Per-slot booking summary, with the two cases that must not be averaged in.

    ``censored_left`` means the slot was already BOOKED in the very first
    snapshot that ever saw it, so the booking predates our data: it must be
    excluded from lead-time statistics, never treated as a zero-lead booking.
    """

    slot_uuid: str
    venue_uuid: str
    facility_uuid: str
    slot_start_utc: dt.datetime
    business_date: dt.date
    first_booked_at: dt.datetime | None
    last_booked_at: dt.datetime | None
    lead_time_hours: float | None
    uncertainty_minutes: int | None
    censored_left: bool
    rebooked: bool
    cancelled: bool
