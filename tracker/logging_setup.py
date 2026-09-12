"""Structured logging for the collector and the CLI.

Every log call in this project is an *event*: the message is a snake_case event
name and everything variable travels in ``extra`` as key/value fields. No
f-strings, no interpolation.

    logger.info("collect_facility_done", extra={"facility": name, "booked": 6})

That shape is what makes a 30-minute unattended collector auditable. The
forward-looking dataset cannot be backfilled, so when a poll goes wrong the log
line is the only surviving evidence of what happened, and it has to be
greppable and machine-readable months later.

Two renderings of the same record:

* :class:`JsonFormatter` writes one JSON object per line to stdout -- what a
  container, a cron mail spool or ``jq`` should get.
* :class:`ConsoleFormatter` writes ``ts LEVEL logger event key=value ...`` --
  what a human running ``python -m tracker collect`` should get.

Neither formatter invents fields: the event name, the logger name and the
caller's ``extra`` keys are all that appear.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, TextIO

logger = logging.getLogger("tracker.logging_setup")


class LogFormat(StrEnum):
    """How a log record is rendered."""

    JSON = "json"
    CONSOLE = "console"


DEFAULT_LEVEL = "INFO"

#: Attributes the logging module itself puts on every record. Anything else on
#: a record came from a caller's ``extra`` and is therefore an event field.
_RESERVED_ATTRS: frozenset[str] = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "taskName"}


def event_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The caller-supplied ``extra`` fields on one record, in insertion order."""
    return {key: value for key, value in vars(record).items() if key not in _RESERVED_ATTRS}


def _timestamp(created: float) -> str:
    """UTC ISO-8601 with milliseconds and a ``Z`` suffix."""
    moment = dt.datetime.fromtimestamp(created, dt.UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``event``, fields.

    Field values that json cannot encode are stringified rather than dropped, so
    a stray dataclass in an ``extra`` never costs us the whole line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(event_fields(record))
        if record.exc_info is not None:
            payload["exc_text"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """``ts LEVEL logger event key=value ...`` for a human at a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            _timestamp(record.created),
            f"{record.levelname:<7}",
            record.name,
            record.getMessage(),
        ]
        parts.extend(f"{key}={_render(value)}" for key, value in event_fields(record).items())
        line = " ".join(parts)
        if record.exc_info is not None:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _render(value: Any) -> str:
    """Render one field value so the ``key=value`` pairs stay machine-splittable."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "" or any(character.isspace() for character in text):
        return json.dumps(text)
    return text


def build_formatter(log_format: LogFormat) -> logging.Formatter:
    """The formatter for a rendering choice."""
    if log_format is LogFormat.CONSOLE:
        return ConsoleFormatter()
    return JsonFormatter()


def configure_logging(
    *,
    level: str | int = DEFAULT_LEVEL,
    log_format: LogFormat = LogFormat.JSON,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install a single root handler and return it.

    Idempotent: existing root handlers are removed first, so calling this twice
    (a test, then the CLI) never doubles every line. Logs go to stdout because
    the events are the product of a run, not its error channel; a crash still
    reaches stderr through the traceback.
    """
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(build_formatter(log_format))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler


def describe_fields(fields: Mapping[str, Any]) -> str:
    """Render a field mapping the way :class:`ConsoleFormatter` would.

    Used where a summary has to go to stdout as text rather than through a
    handler, so the two renderings stay identical.
    """
    return " ".join(f"{key}={_render(value)}" for key, value in fields.items())
