"""``python -m tracker <command>`` -- the operator's entry point.

Three commands, one job each:

* ``daily`` is the scheduled job (GitHub Actions, once a day). It seeds the
  padel courts named in ``config.yaml``, optionally (``--discover``) refreshes
  the pickleball venue list from Hudle, then reads every tracked court's grid
  once and records what changed in the store named by ``DATABASE_URL``.
* ``discover`` re-checks the configured venue and facility sets against Hudle
  and prints what drifted. It never edits ``config.yaml``: the venue tree is
  human-reviewed, and a facility silently adopted would be tracked without
  anyone having looked at it.
* ``serve`` runs the dashboard locally: the JSON API plus ``public/``, against
  whatever ``DATABASE_URL`` points at (a SQLite file works).

Exit codes, so a scheduler can tell the three cases apart without parsing
logs: ``0`` clean, ``1`` partial failure (some court failed, drift found),
``2`` the run stopped -- circuit open, or bad configuration.

This module is the composition root. It is the only place that knows an
``HttpConfig`` becomes a :class:`~tracker.hudle.HudleClient` and
``DATABASE_URL`` becomes a :class:`~tracker.store.Store`; everything below it
takes those as arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any

from tracker.config import Config, ConfigError, load_config
from tracker.daily import discover_pickleball, locate_venues, run_daily, seed_configured_courts
from tracker.discover import (
    DiscoveryError,
    DriftReport,
    parse_search_pagination,
    parse_share_url,
    run_discovery,
)
from tracker.hudle import MAX_SEARCH_PAGES, HudleClient, HudleError
from tracker.logging_setup import LogFormat, configure_logging
from tracker.store import Store

logger = logging.getLogger("tracker.cli")

#: Where ``--config`` looks by default, overridable without a flag so a cron
#: line and a container entrypoint can differ without editing either.
CONFIG_ENV_VAR = "PADEL_TRACKER_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: The ASGI app ``serve`` runs. Passed to uvicorn as a string so this module
#: never imports ``tracker.web`` -- the CLI stays usable when the dashboard is
#: broken, half-written or absent.
WEB_APP_PATH = "tracker.web.dev:app"


class ExitCode(IntEnum):
    """Process exit codes, so a scheduler can tell the three cases apart.

    An :class:`~enum.IntEnum` rather than the project's usual ``StrEnum``
    because these values are handed straight to ``sys.exit``.
    """

    OK = 0
    PARTIAL = 1
    STOPPED = 2


def default_config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_NAME)


# --------------------------------------------------------------------------
# Client adapters
# --------------------------------------------------------------------------


class HudleDiscoveryClient:
    """Adapts :class:`~tracker.hudle.HudleClient` to ``discover``'s protocol.

    Two impedance mismatches live here rather than in either module:

    * discovery needs whole ``{code, data, meta}`` page envelopes, because
      ``meta.pagination`` is the only evidence that a search truncated --
      pickleball is 57 venues served 50 at a time. The client's own
      ``search_venues_all`` flattens to a venue list and drops that, so this
      walks the pages itself and keeps each envelope.
    * discovery addresses SSR pages by ``VenueConfig.ssr_path``; the client
      takes ``(slug, numeric_id)``.
    """

    def __init__(self, client: HudleClient) -> None:
        self._client = client

    def search_venues_all(
        self, *, sport_id: int, city_id: int, per_page: int
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_SEARCH_PAGES:
            payload = self._client.search_venues(sport_id, page, per_page, city_id=city_id)
            pages.append(payload)
            pagination = parse_search_pagination(payload)
            if pagination is None or not pagination.has_more:
                break
            page += 1
        else:
            logger.warning(
                "discovery_page_ceiling_hit",
                extra={"sport_id": sport_id, "pages": MAX_SEARCH_PAGES},
            )
        return pages

    def fetch_venue_page(self, ssr_path: str) -> str:
        slug, numeric_id = parse_share_url(ssr_path)
        return self._client.fetch_venue_page_html(slug, numeric_id)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def build_client(config: Config) -> HudleClient:
    """One client per process: its rate-limit floor is per instance."""
    return HudleClient(config.http, config.poll)


def utc_now() -> dt.datetime:
    """The only clock read in this project. Everything below takes ``now``."""
    return dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with build_client(config) as client:
        report = run_discovery(HudleDiscoveryClient(client), config, observed_at=utc_now())
    print_drift(report)
    return int(ExitCode.PARTIAL) if report.should_alert else int(ExitCode.OK)


def print_drift(report: DriftReport) -> None:
    """Print the drift report plus any config block a human should review."""
    print(report.render())
    suggestions = report.suggestion_yaml()
    if suggestions:
        print("\nsuggested config.yaml changes (review by hand, never auto-applied):")
        print(suggestions)
    if report.has_drift and not report.should_alert:
        print(
            "\nunconfigured venues exist for a non-alerting sport, which is the "
            "normal state of the 57-venue pickleball market: exiting 0."
        )


# --------------------------------------------------------------------------
# daily pass (the one scheduled job)
# --------------------------------------------------------------------------


def _open_store() -> Store:
    """The store named by DATABASE_URL (Neon in production, a file locally)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set: point it at the Neon database, or sqlite:///path"
        )
    store = Store(url)
    store.initialize()
    return store


def cmd_daily(args: argparse.Namespace) -> int:
    """Seed the configured courts, discover pickleball if due, then poll everything once."""
    config = load_config(args.config)
    store = _open_store()
    now = utc_now()
    try:
        seed_configured_courts(config, store, now=now)
        with build_client(config) as client:
            if args.discover:
                venues, courts = discover_pickleball(config, store, client, now=now)
                print(f"discovered {venues} pickleball venues, {courts} courts")
                print(f"located {locate_venues(store, client)} more venues")
            result = run_daily(config, store, client, now=now)
    finally:
        store.close()
    print(
        f"daily pass: {result.courts_ok} courts ok, {result.courts_failed} failed, "
        f"{result.slots_seen} slots seen, {result.slots_written} written"
        + (" -- STOPPED EARLY (circuit open)" if result.stopped_early else "")
    )
    if result.stopped_early:
        return int(ExitCode.STOPPED)
    return int(ExitCode.OK if result.ok else ExitCode.PARTIAL)


# --------------------------------------------------------------------------
# serve (local development only)
# --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the dashboard locally. ``tracker.web`` is imported by uvicorn, not by us.

    The store is opened once first, so a missing ``DATABASE_URL`` fails here
    rather than as a 503 on every request, and a fresh SQLite file gets its
    tables before the first page load.
    """
    import uvicorn

    _open_store().close()
    logger.info("serve_starting", extra={"host": args.host, "port": args.port})
    uvicorn.run(WEB_APP_PATH, host=args.host, port=args.port, reload=args.reload)
    return int(ExitCode.OK)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tracker",
        description="Court-occupancy tracker for Jaipur padel and pickleball courts on Hudle.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help=f"path to config.yaml (default: ${CONFIG_ENV_VAR} or ./{DEFAULT_CONFIG_NAME})",
    )
    common.add_argument("--log-level", default="INFO", help="root log level (default: INFO)")
    common.add_argument(
        "--log-format",
        type=LogFormat,
        choices=list(LogFormat),
        default=LogFormat.JSON,
        help="json lines (default) or human-readable console output",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser(
        "discover", parents=[common], help="re-check venues and facilities against Hudle"
    )
    discover.set_defaults(func=cmd_discover)

    daily = commands.add_parser(
        "daily", parents=[common], help="poll every tracked court once (the scheduled job)"
    )
    daily.add_argument(
        "--discover", action="store_true", help="first refresh the pickleball court list"
    )
    daily.set_defaults(func=cmd_daily)

    serve = commands.add_parser(
        "serve", parents=[common], help="run the dashboard locally against DATABASE_URL"
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--reload", action="store_true", help="uvicorn auto-reload")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, configure logging, run one command, return its exit code.

    Expected failures -- bad config, a refusing API, a changed SSR page -- are
    reported as a log event plus exit code 2 rather than a traceback. An
    unexpected exception is deliberately left to propagate: a job that swallows
    a bug it does not understand is worse than one that stops.
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=args.log_level, log_format=args.log_format)
    try:
        code: int = args.func(args)
    except ConfigError as exc:
        logger.error("cli_config_invalid", extra={"error": str(exc)})
        print(f"configuration error: {exc}", file=sys.stderr)
        return int(ExitCode.STOPPED)
    except (HudleError, DiscoveryError) as exc:
        logger.error(
            "cli_command_failed",
            extra={"command": args.command, "error": str(exc), "kind": type(exc).__name__},
        )
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return int(ExitCode.STOPPED)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
