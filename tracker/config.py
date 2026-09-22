"""Typed loader for ``config.yaml``.

``config.yaml`` is the frozen, human-reviewed source of truth for the venue and
facility tree. Nothing downstream re-parses YAML: everything reads the frozen
dataclasses built here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import yaml

from tracker.types import FacilityKind, SlotState, Sport

__all__ = [
    "BackoffConfig",
    "Config",
    "ConfigError",
    "DashboardConfig",
    "DiscoveryConfig",
    "FacilityConfig",
    "FacilityKind",
    "HttpConfig",
    "PollConfig",
    "SlotState",
    "Sport",
    "StorageConfig",
    "VenueConfig",
    "load_config",
]

T = TypeVar("T")


#: Overrides ``storage.url`` when set. The only config value read from the
#: environment: everything else is the frozen, reviewed file.
STORAGE_URL_ENV_VAR = "PADEL_TRACKER_STORAGE_URL"


class ConfigError(ValueError):
    """Raised when the configuration file is missing or malformed."""


def _require(block: Mapping[str, Any], key: str, where: str) -> Any:
    """Fetch a required key, naming the exact path that is missing."""
    if key not in block or block[key] is None:
        raise ConfigError(f"missing required config key: {where}.{key}")
    return block[key]


def _expand_env(value: str, where: str) -> str:
    """Substitute ``${VAR}`` references from the environment.

    Used for the Hudle client credentials, which stay out of the repo. An
    unset variable fails the load by name rather than sending the literal
    ``${VAR}`` to Hudle and learning about it from a 401.
    """
    expanded = os.path.expandvars(value)
    if "${" in expanded:
        missing = expanded[expanded.index("${") + 2 : expanded.index("}")]
        raise ConfigError(f"{where} needs environment variable {missing}, which is not set")
    return expanded


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"config key {where} must be a mapping, got {type(value).__name__}")
    return value


def _sequence(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ConfigError(f"config key {where} must be a list, got {type(value).__name__}")
    return value


def _enum(enum_cls: type[T], value: Any, where: str) -> T:
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError as exc:
        raise ConfigError(f"config key {where} has unknown value {value!r}") from exc


# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StorageConfig:
    url: str


@dataclass(frozen=True, slots=True)
class BackoffConfig:
    initial_seconds: float
    multiplier: float
    max_seconds: float
    max_attempts: int


@dataclass(frozen=True, slots=True)
class PollConfig:
    cadence_minutes: int
    horizon_days: int
    #: Days before today the grid request starts at. Hudle keeps serving a date
    #: after it elapses, so re-reading yesterday captures each date's settled
    #: state after every booking for it is in -- and survives an outage that
    #: swallowed the last polls before midnight.
    lookback_days: int
    request_gap_seconds: float
    timeout_seconds: float
    max_consecutive_failures: int
    backoff: BackoffConfig

    @property
    def expected_snapshots_per_day(self) -> int:
        """How many polls a full day should contain, for coverage reporting."""
        return (24 * 60) // self.cadence_minutes


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    city_id: int
    sports: Mapping[str, int]
    per_page: int
    drift_check_days: int
    alert_on_venue_set_change: Sport
    equipment_name_hints: tuple[str, ...]
    court_name_override: tuple[str, ...]

    def sport_id(self, sport: Sport) -> int:
        """Hudle's ``preferred_sports`` id for a sport."""
        try:
            return self.sports[str(sport)]
        except KeyError as exc:
            raise ConfigError(f"discovery.sports has no id for sport {sport!r}") from exc


@dataclass(frozen=True, slots=True)
class HttpConfig:
    api_base: str
    web_base: str
    headers: Mapping[str, str]
    user_agent: str

    def request_headers(self) -> dict[str, str]:
        """The full outbound header set, User-Agent included."""
        return {**self.headers, "User-Agent": self.user_agent}


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    default_sport: Sport
    headline_metric: str
    peak_hours: tuple[int, ...]

    def is_peak(self, hour: int) -> bool:
        return hour in self.peak_hours


@dataclass(frozen=True, slots=True)
class FacilityConfig:
    uuid: str
    name: str
    kind: FacilityKind
    sport: Sport
    grid_minutes: int | None
    price_per_court_hour: int | None
    active: bool

    @property
    def is_court(self) -> bool:
        return self.kind is FacilityKind.COURT

    @property
    def price_per_slot(self) -> float | None:
        """Per-slot price implied by the per-court-hour price and the grid.

        Play Padel's 1000 per slot looks cheapest but is 2000 per court-hour;
        the config stores only the per-hour figure so nothing compares per-slot
        prices across venues by accident.
        """
        if self.price_per_court_hour is None or self.grid_minutes is None:
            return None
        return self.price_per_court_hour * self.grid_minutes / 60.0


@dataclass(frozen=True, slots=True)
class VenueConfig:
    uuid: str
    name: str
    short_name: str
    slug: str
    numeric_id: str
    active: bool
    show_in_dashboard: bool
    facilities: tuple[FacilityConfig, ...]

    @property
    def ssr_path(self) -> str:
        """Path of the Next.js page that carries the facility UUIDs."""
        return f"/venues/{self.slug}/{self.numeric_id}"

    def courts(self, *, active_only: bool = True) -> list[FacilityConfig]:
        return [f for f in self.facilities if f.is_court and (f.active or not active_only)]

    def facility_by_uuid(self, facility_uuid: str) -> FacilityConfig | None:
        return next((f for f in self.facilities if f.uuid == facility_uuid), None)


@dataclass(frozen=True, slots=True)
class Config:
    timezone: str
    storage: StorageConfig
    poll: PollConfig
    business_day_start_hour: int
    discovery: DiscoveryConfig
    http: HttpConfig
    dashboard: DashboardConfig
    venues: tuple[VenueConfig, ...]

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def active_courts(self) -> list[tuple[VenueConfig, FacilityConfig]]:
        """Every (venue, court) pair worth polling: kind==court and active."""
        return [
            (venue, facility)
            for venue in self.venues
            if venue.active
            for facility in venue.facilities
            if facility.is_court and facility.active
        ]

    def courts_for_sport(self, sport: Sport) -> list[tuple[VenueConfig, FacilityConfig]]:
        return [(v, f) for v, f in self.active_courts() if f.sport is sport]

    def dashboard_venues(self) -> list[VenueConfig]:
        """Venues the dashboard presents.

        Deliberately independent of ``active``, which governs *polling*. A venue
        can be collected and not shown: hiding one loses nothing, while dropping
        it from the poll opens a permanent hole in a forward-only dataset. Padel
        Up is the live case -- zero bookings ever observed, so its flat 0% line
        reads as a broken collector rather than as a finding.
        """
        return [venue for venue in self.venues if venue.show_in_dashboard]

    def dashboard_venue_uuids(self) -> frozenset[str]:
        return frozenset(venue.uuid for venue in self.dashboard_venues())

    def hidden_venue_uuids(self) -> frozenset[str]:
        return frozenset(v.uuid for v in self.venues if not v.show_in_dashboard)

    def venue_by_uuid(self, venue_uuid: str) -> VenueConfig | None:
        return next((v for v in self.venues if v.uuid == venue_uuid), None)

    def facility_by_uuid(self, facility_uuid: str) -> tuple[VenueConfig, FacilityConfig] | None:
        for venue in self.venues:
            facility = venue.facility_by_uuid(facility_uuid)
            if facility is not None:
                return venue, facility
        return None


# --------------------------------------------------------------------------


def _load_facility(raw: Mapping[str, Any], where: str) -> FacilityConfig:
    grid = raw.get("grid_minutes")
    price = raw.get("price_per_court_hour")
    if grid is not None and int(grid) <= 0:
        raise ConfigError(f"config key {where}.grid_minutes must be positive")
    return FacilityConfig(
        uuid=str(_require(raw, "uuid", where)),
        name=str(_require(raw, "name", where)),
        kind=_enum(FacilityKind, _require(raw, "kind", where), f"{where}.kind"),
        sport=_enum(Sport, _require(raw, "sport", where), f"{where}.sport"),
        grid_minutes=None if grid is None else int(grid),
        price_per_court_hour=None if price is None else int(price),
        active=bool(_require(raw, "active", where)),
    )


def _load_venue(raw: Mapping[str, Any], where: str) -> VenueConfig:
    facilities_raw = _sequence(_require(raw, "facilities", where), f"{where}.facilities")
    facilities = tuple(
        _load_facility(_mapping(item, f"{where}.facilities[{i}]"), f"{where}.facilities[{i}]")
        for i, item in enumerate(facilities_raw)
    )
    return VenueConfig(
        uuid=str(_require(raw, "uuid", where)),
        name=str(_require(raw, "name", where)),
        short_name=str(_require(raw, "short_name", where)),
        slug=str(_require(raw, "slug", where)),
        numeric_id=str(_require(raw, "numeric_id", where)),
        active=bool(_require(raw, "active", where)),
        show_in_dashboard=bool(raw.get("show_in_dashboard", True)),
        facilities=facilities,
    )


def load_config(path: str | Path) -> Config:
    """Parse and validate ``config.yaml`` into a frozen :class:`Config`."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _mapping(parsed, "<root>")

    poll_raw = _mapping(_require(root, "poll", "<root>"), "poll")
    backoff_raw = _mapping(_require(poll_raw, "backoff", "poll"), "poll.backoff")
    discovery_raw = _mapping(_require(root, "discovery", "<root>"), "discovery")
    http_raw = _mapping(_require(root, "http", "<root>"), "http")
    dashboard_raw = _mapping(_require(root, "dashboard", "<root>"), "dashboard")
    storage_raw = _mapping(_require(root, "storage", "<root>"), "storage")
    venues_raw = _sequence(_require(root, "venues", "<root>"), "venues")

    sports_raw = _mapping(_require(discovery_raw, "sports", "discovery"), "discovery.sports")

    config = Config(
        timezone=str(_require(root, "timezone", "<root>")),
        # A container mounts its volume somewhere the checked-in config cannot
        # know about, so the storage URL alone may come from the environment.
        storage=StorageConfig(
            url=os.environ.get(STORAGE_URL_ENV_VAR) or str(_require(storage_raw, "url", "storage"))
        ),
        poll=PollConfig(
            cadence_minutes=int(_require(poll_raw, "cadence_minutes", "poll")),
            horizon_days=int(_require(poll_raw, "horizon_days", "poll")),
            lookback_days=int(poll_raw.get("lookback_days", 1)),
            request_gap_seconds=float(_require(poll_raw, "request_gap_seconds", "poll")),
            timeout_seconds=float(_require(poll_raw, "timeout_seconds", "poll")),
            max_consecutive_failures=int(_require(poll_raw, "max_consecutive_failures", "poll")),
            backoff=BackoffConfig(
                initial_seconds=float(_require(backoff_raw, "initial_seconds", "poll.backoff")),
                multiplier=float(_require(backoff_raw, "multiplier", "poll.backoff")),
                max_seconds=float(_require(backoff_raw, "max_seconds", "poll.backoff")),
                max_attempts=int(_require(backoff_raw, "max_attempts", "poll.backoff")),
            ),
        ),
        business_day_start_hour=int(_require(root, "business_day_start_hour", "<root>")),
        discovery=DiscoveryConfig(
            city_id=int(_require(discovery_raw, "city_id", "discovery")),
            sports={str(k): int(v) for k, v in sports_raw.items()},
            per_page=int(_require(discovery_raw, "per_page", "discovery")),
            drift_check_days=int(_require(discovery_raw, "drift_check_days", "discovery")),
            alert_on_venue_set_change=_enum(
                Sport,
                _require(discovery_raw, "alert_on_venue_set_change", "discovery"),
                "discovery.alert_on_venue_set_change",
            ),
            equipment_name_hints=tuple(
                str(h)
                for h in _sequence(
                    _require(discovery_raw, "equipment_name_hints", "discovery"),
                    "discovery.equipment_name_hints",
                )
            ),
            court_name_override=tuple(
                str(h)
                for h in _sequence(
                    _require(discovery_raw, "court_name_override", "discovery"),
                    "discovery.court_name_override",
                )
            ),
        ),
        http=HttpConfig(
            api_base=str(_require(http_raw, "api_base", "http")).rstrip("/"),
            web_base=str(_require(http_raw, "web_base", "http")).rstrip("/"),
            headers={
                str(k): _expand_env(str(v), f"http.headers.{k}")
                for k, v in _mapping(_require(http_raw, "headers", "http"), "http.headers").items()
            },
            user_agent=str(_require(http_raw, "user_agent", "http")),
        ),
        dashboard=DashboardConfig(
            default_sport=_enum(
                Sport,
                _require(dashboard_raw, "default_sport", "dashboard"),
                "dashboard.default_sport",
            ),
            headline_metric=str(_require(dashboard_raw, "headline_metric", "dashboard")),
            peak_hours=tuple(
                int(h)
                for h in _sequence(
                    _require(dashboard_raw, "peak_hours", "dashboard"), "dashboard.peak_hours"
                )
            ),
        ),
        venues=tuple(
            _load_venue(_mapping(item, f"venues[{i}]"), f"venues[{i}]")
            for i, item in enumerate(venues_raw)
        ),
    )

    if not 0 <= config.business_day_start_hour <= 23:
        raise ConfigError("business_day_start_hour must be between 0 and 23")
    if config.poll.cadence_minutes <= 0:
        raise ConfigError("poll.cadence_minutes must be positive")
    ZoneInfo(config.timezone)
    return config
