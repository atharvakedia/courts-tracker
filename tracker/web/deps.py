"""Request-scoped dependencies: config, storage, clock and query filters.

Nothing here opens a database or reads a file. The application lifespan puts
the :class:`~tracker.config.Config` and the :class:`~tracker.storage.Storage`
on ``app.state`` once, and these providers hand them to route functions
through ``Annotated[..., Depends(...)]``. Tests construct the app with their
own storage and clock and need no dependency overrides.

Filter parsing lives here too, so route bodies never touch ``Query``: one
:class:`Filters` object carries the validated window, sport and venue, and it
is echoed back in every response so a chart can print what it was actually
filtered to.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from tracker.config import Config
from tracker.storage import Storage
from tracker.types import Sport

#: ``?sport=all`` opts out of the sport filter entirely. Omitting the parameter
#: applies ``dashboard.default_sport`` instead, because two of the three venues
#: publish pickleball courts beside their padel one and an unfiltered venue
#: total silently compares three courts against one.
ALL_SPORTS = "all"


@dataclass(frozen=True, slots=True)
class Filters:
    """The window, sport and venue a request is scoped to.

    ``start`` and ``end`` are inclusive **business** dates, not local dates:
    Play Padel's 00:30 Saturday slots are Friday-night sessions and belong to
    Friday's window.
    """

    start: dt.date | None
    end: dt.date | None
    sport: Sport | None
    sport_label: str
    venue_uuid: str | None


def get_config(request: Request) -> Config:
    """The loaded configuration, put on ``app.state`` by the lifespan."""
    config = getattr(request.app.state, "config", None)
    if config is None:  # pragma: no cover - only reachable if the lifespan is skipped
        raise HTTPException(status_code=503, detail="configuration is not loaded yet")
    assert isinstance(config, Config)
    return config


def get_storage(request: Request) -> Storage:
    """The open storage backend, put on ``app.state`` by the lifespan."""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:  # pragma: no cover - only reachable if the lifespan is skipped
        raise HTTPException(status_code=503, detail="storage is not open yet")
    assert isinstance(storage, Storage)
    return storage


def get_now(request: Request) -> dt.datetime:
    """The current instant, aware UTC, from the app's injected clock.

    A clock is a dependency rather than a call to :func:`datetime.now` so that
    staleness can be tested against a fixed instant.
    """
    clock = getattr(request.app.state, "clock", None)
    if clock is None:  # pragma: no cover - the app always installs one
        return dt.datetime.now(dt.UTC)
    assert callable(clock)
    moment: dt.datetime = clock()
    if moment.tzinfo is None:
        raise HTTPException(status_code=500, detail="clock returned a naive datetime")
    return moment.astimezone(dt.UTC)


ConfigDep = Annotated[Config, Depends(get_config)]
StorageDep = Annotated[Storage, Depends(get_storage)]
NowDep = Annotated[dt.datetime, Depends(get_now)]


def get_filters(
    config: ConfigDep,
    start: Annotated[
        dt.date | None,
        Query(description="First business date to include (inclusive)."),
    ] = None,
    end: Annotated[
        dt.date | None,
        Query(description="Last business date to include (inclusive)."),
    ] = None,
    sport: Annotated[
        str | None,
        Query(description="padel, pickleball, or all. Defaults to dashboard.default_sport."),
    ] = None,
    venue: Annotated[
        str | None,
        Query(description="Restrict to one venue uuid."),
    ] = None,
) -> Filters:
    """Validate and normalize the filter query parameters."""
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=422,
            detail=f"start {start.isoformat()} is after end {end.isoformat()}",
        )

    resolved_sport = _resolve_sport(sport, config)
    label = ALL_SPORTS if resolved_sport is None else str(resolved_sport)

    if venue is not None and config.venue_by_uuid(venue) is None:
        known = ", ".join(v.uuid for v in config.venues)
        raise HTTPException(
            status_code=404,
            detail=f"unknown venue {venue!r}; configured venues are: {known}",
        )

    return Filters(
        start=start,
        end=end,
        sport=resolved_sport,
        sport_label=label,
        venue_uuid=venue,
    )


def _resolve_sport(sport: str | None, config: Config) -> Sport | None:
    if sport is None:
        return config.dashboard.default_sport
    if sport.lower() == ALL_SPORTS:
        return None
    try:
        return Sport(sport.lower())
    except ValueError:
        allowed = ", ".join([*(str(s) for s in Sport), ALL_SPORTS])
        raise HTTPException(
            status_code=422, detail=f"unknown sport {sport!r}; allowed: {allowed}"
        ) from None


FiltersDep = Annotated[Filters, Depends(get_filters)]

Clock = Callable[[], dt.datetime]
