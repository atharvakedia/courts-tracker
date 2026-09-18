"""``python -m tracker <command>`` -- the operator's entry point.

Four commands, one job each:

* ``collect`` polls every active court once. This is what cron or launchd runs
  every 30 minutes. ``--loop`` instead keeps the process alive on an
  APScheduler interval, for a container with no cron.
* ``discover`` re-checks the venue and facility sets against Hudle and prints
  what drifted. It never edits ``config.yaml``: the venue tree is
  human-reviewed, and a facility silently adopted with an unprobed grid length
  would write observations whose ``duration_minutes`` nobody verified.
* ``serve`` runs the dashboard.
* ``backfill-derived`` recomputes the derived tables from the observations.

Exit codes, so a cron wrapper can tell the three cases apart without parsing
logs: ``0`` clean, ``1`` partial failure (some facility failed, drift found),
``2`` the run stopped -- circuit open, nothing collected, or bad configuration.

This module is the composition root. It is the only place that knows an
``HttpConfig`` becomes an :class:`~tracker.hudle.HudleClient` and a
``storage.url`` becomes a :class:`~tracker.storage_sqlite.SQLiteStorage`;
everything below it takes those as arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from tracker.collect import (
    CollectResult,
    ConsecutiveFailureTracker,
    ExitCode,
    backfill_derived,
    run_collect,
)
from tracker.config import Config, ConfigError, load_config
from tracker.discover import (
    DiscoveryError,
    DriftReport,
    parse_search_pagination,
    parse_share_url,
    run_discovery,
)
from tracker.hudle import MAX_SEARCH_PAGES, HudleClient, HudleError
from tracker.logging_setup import LogFormat, configure_logging, describe_fields
from tracker.storage_sqlite import SQLiteStorage
from tracker.types import SlotState

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
WEB_APP_PATH = "tracker.web:app"


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


def build_storage(config: Config) -> SQLiteStorage:
    """Open and initialize the configured backend.

    ``expected_snapshots_per_day`` is threaded from the poll cadence: the
    coverage view bakes it into SQL, and defaulting it would quietly report
    yesterday's coverage against the wrong denominator after a cadence change.
    """
    if not config.storage.url.startswith("sqlite"):
        raise ConfigError(
            f"storage.url {config.storage.url!r} is not SQLite; "
            "no other Storage backend is implemented yet"
        )
    storage = SQLiteStorage(
        config.storage.url,
        expected_snapshots_per_day=config.poll.expected_snapshots_per_day,
    )
    storage.initialize()
    return storage


def build_client(config: Config) -> HudleClient:
    """One client per process: its rate-limit floor is per instance."""
    return HudleClient(config.http, config.poll)


def utc_now() -> dt.datetime:
    """The only clock read in this project. Everything below takes ``now``."""
    return dt.datetime.now(dt.UTC)


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = build_storage(config)
    try:
        with build_client(config) as client:
            if args.loop:
                return run_loop(config, storage, client, dry_run=args.dry_run)
            result = run_collect(config, storage, client, now=utc_now(), dry_run=args.dry_run)
    finally:
        storage.close()

    if args.dry_run:
        print_dry_run(result)
    return int(result.exit_code)


def print_dry_run(result: CollectResult) -> None:
    """Report what a dry run would have written, per facility and in total.

    Court-minutes sit beside the counts because the counts are not comparable:
    Padel Up's 60-minute slots and Padel Fort's 30-minute slots are both "1".
    """
    print(f"dry run {result.poll_key}  {result.start_date} .. {result.end_date}")
    for outcome in result.facilities:
        status = "ok" if outcome.ok else f"FAILED {outcome.error}"
        print(f"  {outcome.label}: {status}")
        print(f"    {describe_fields(outcome.log_fields())}")
    print(
        "  totals: "
        + describe_fields(
            {
                "booked": result.counts[SlotState.BOOKED],
                "blocked": result.counts[SlotState.BLOCKED],
                "open": result.counts[SlotState.OPEN],
                "court_minutes_booked": result.court_minutes[SlotState.BOOKED],
                "court_minutes_blocked": result.court_minutes[SlotState.BLOCKED],
                "court_minutes_open": result.court_minutes[SlotState.OPEN],
                "slots": sum(result.counts.values()),
                "written": result.written,
            }
        )
    )
    print("  nothing was written: --dry-run touches no storage method")


def run_loop(
    config: Config,
    storage: SQLiteStorage,
    client: HudleClient,
    *,
    dry_run: bool = False,
) -> int:
    """Poll on the configured cadence until told to stop.

    Two stop conditions, both of them about not hammering Hudle: the client's
    circuit breaker opening (which means requests are already being refused),
    and ``poll.max_consecutive_failures`` cycles failing in a row (which means
    more requests will not fix whatever is wrong).
    """
    failures = ConsecutiveFailureTracker(config.poll.max_consecutive_failures)
    scheduler = BlockingScheduler(timezone="UTC")
    outcome = {"exit_code": ExitCode.OK}

    def tick() -> None:
        result = run_collect(config, storage, client, now=utc_now(), dry_run=dry_run)
        failures.record(ok=result.ok)
        stop_reason = None
        if result.circuit_open:
            stop_reason = "circuit_open"
        elif failures.exhausted:
            stop_reason = "consecutive_failures"
        if stop_reason is not None:
            outcome["exit_code"] = ExitCode.STOPPED
            logger.warning(
                "collect_loop_stopping",
                extra={
                    "reason": stop_reason,
                    "consecutive_failures": failures.consecutive_failures,
                    "limit": failures.limit,
                },
            )
            scheduler.shutdown(wait=False)

    scheduler.add_job(
        tick,
        "interval",
        minutes=config.poll.cadence_minutes,
        next_run_time=utc_now(),
        max_instances=1,
        coalesce=True,
        id="collect",
    )
    logger.info(
        "collect_loop_started",
        extra={
            "cadence_minutes": config.poll.cadence_minutes,
            "max_consecutive_failures": failures.limit,
            "dry_run": dry_run,
        },
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("collect_loop_interrupted", extra={})
        scheduler.shutdown(wait=False)
    return int(outcome["exit_code"])


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = build_storage(config)
    observed_at = utc_now()
    try:
        with build_client(config) as client:
            report = run_discovery(HudleDiscoveryClient(client), config, observed_at=observed_at)
        # What the SSR pages said is appended every run, so facility drift is
        # auditable after the fact. `config.yaml` is never touched.
        storage.append_discovery_log(observed_at, report.discovered_facilities)
    finally:
        storage.close()

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
# serve / backfill-derived
# --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the dashboard. ``tracker.web`` is imported by uvicorn, not by us.

    ``--with-collector`` runs the poll loop on a daemon thread in this same
    process, which is what a single always-on container needs: one machine,
    one volume, no scheduler to install. The thread owns its own storage
    handle and HTTP client; the web app opens its own storage in its lifespan,
    and SQLite in WAL mode lets the two share the file. The thread is a daemon
    so the process exits when uvicorn does, and the loop's own stop conditions
    -- an open breaker, too many failed cycles -- still hold.
    """
    import uvicorn

    if args.with_collector:
        config = load_config(args.config)

        def collect_forever() -> None:
            storage = build_storage(config)
            try:
                with build_client(config) as client:
                    code = run_loop(config, storage, client)
            finally:
                storage.close()
            logger.warning("collector_thread_exited", extra={"exit_code": code})

        threading.Thread(target=collect_forever, name="collector", daemon=True).start()

    load_config(args.config)  # fail fast on bad config before binding a port
    logger.info("serve_starting", extra={"host": args.host, "port": args.port})
    uvicorn.run(WEB_APP_PATH, host=args.host, port=args.port, reload=args.reload)
    return int(ExitCode.OK)


def cmd_backfill_derived(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = build_storage(config)
    try:
        result = backfill_derived(storage)
    finally:
        storage.close()
    print(
        "backfill-derived "
        + describe_fields(
            {
                "slots": result.slots,
                "booked_slots": result.booked_slots,
                "transitions": result.transitions,
                "first_booked": result.first_booked,
            }
        )
    )
    return int(ExitCode.OK)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tracker",
        description="Court-occupancy tracker for the Jaipur padel venues on Hudle.",
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

    collect = commands.add_parser("collect", parents=[common], help="poll every active court once")
    collect.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and classify but write nothing; print what would be written",
    )
    collect.add_argument(
        "--loop",
        action="store_true",
        help="keep polling on the configured cadence instead of exiting",
    )
    collect.set_defaults(func=cmd_collect)

    discover = commands.add_parser(
        "discover", parents=[common], help="re-check venues and facilities against Hudle"
    )
    discover.set_defaults(func=cmd_discover)

    serve = commands.add_parser("serve", parents=[common], help="run the dashboard")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--reload", action="store_true", help="uvicorn auto-reload")
    serve.add_argument(
        "--with-collector",
        action="store_true",
        help="also run the poll loop in this process (single-container deployments)",
    )
    serve.set_defaults(func=cmd_serve)

    backfill = commands.add_parser(
        "backfill-derived",
        parents=[common],
        help="recompute slot_state_transitions and slot_first_booked",
    )
    backfill.set_defaults(func=cmd_backfill_derived)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, configure logging, run one command, return its exit code.

    Expected failures -- bad config, a refusing API, a changed SSR page -- are
    reported as a log event plus exit code 2 rather than a traceback. An
    unexpected exception is deliberately left to propagate: a collector that
    swallows a bug it does not understand is worse than one that stops.
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
