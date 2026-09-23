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

from tracker.types import FacilityKind, Sport

__all__ = [
    "BackoffConfig",
    "Config",
    "ConfigError",
    "DiscoveryConfig",
    "FacilityConfig",
    "FacilityKind",
    "HttpConfig",
    "PollConfig",
    "Sport",
    "VenueConfig",
    "load_config",
]

T = TypeVar("T")


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
class BackoffConfig:
    initial_seconds: float
    multiplier: float
    max_seconds: float
    max_attempts: int


@dataclass(frozen=True, slots=True)
class PollConfig:
    """How gently the Hudle client behaves: pacing, timeouts, retries, breaker."""

    request_gap_seconds: float
    timeout_seconds: float
    #: Failed requests in a row before the client's circuit breaker opens.
    max_consecutive_failures: int
    backoff: BackoffConfig


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    city_id: int
    sports: Mapping[str, int]
    per_page: int
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
class FacilityConfig:
    uuid: str
    name: str
    kind: FacilityKind
    sport: Sport
    grid_minutes: int | None
    price_per_court_hour: int | None
    active: bool


@dataclass(frozen=True, slots=True)
class VenueConfig:
    uuid: str
    name: str
    short_name: str
    slug: str
    numeric_id: str
    active: bool
    facilities: tuple[FacilityConfig, ...]

    @property
    def ssr_path(self) -> str:
        """Path of the Next.js page that carries the facility UUIDs."""
        return f"/venues/{self.slug}/{self.numeric_id}"

    def facility_by_uuid(self, facility_uuid: str) -> FacilityConfig | None:
        return next((f for f in self.facilities if f.uuid == facility_uuid), None)


@dataclass(frozen=True, slots=True)
class Config:
    timezone: str
    poll: PollConfig
    business_day_start_hour: int
    discovery: DiscoveryConfig
    http: HttpConfig
    venues: tuple[VenueConfig, ...]

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
    venues_raw = _sequence(_require(root, "venues", "<root>"), "venues")

    sports_raw = _mapping(_require(discovery_raw, "sports", "discovery"), "discovery.sports")

    config = Config(
        timezone=str(_require(root, "timezone", "<root>")),
        poll=PollConfig(
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
        venues=tuple(
            _load_venue(_mapping(item, f"venues[{i}]"), f"venues[{i}]")
            for i, item in enumerate(venues_raw)
        ),
    )

    if not 0 <= config.business_day_start_hour <= 23:
        raise ConfigError("business_day_start_hour must be between 0 and 23")
    ZoneInfo(config.timezone)
    return config
