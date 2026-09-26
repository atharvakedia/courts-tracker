"""Hours a person marked as blocked at a venue, applied on top of what Hudle says.

Some venues take court time off sale by marking it booked (no court left
available) instead of blocking it, so Hudle reports a sale that never
happened. A person who knows the venue's schedule -- a coaching hour every
weekday morning, say -- marks those hours as blocked on the dashboard, and
every figure then treats them as a venue block: left out of both sides of %
booked, like a block Hudle reported itself.

A block is a recurring pattern for one venue: a set of (weekday, hour) cells,
optionally bounded by business dates. The weekday is the business date's and
the hour is the slot's local start hour, the same cells as the busy-hours
grid. Blocks are kept apart from the slots, which the daily pass rewrites
from Hudle; they are applied when rows are read for the views.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tracker.insights import MARKED_BLOCKED, Row


@dataclass(frozen=True, slots=True)
class Block:
    block_id: int
    venue_uuid: str
    cells: frozenset[tuple[int, int]]  # (weekday Monday=0, hour 0-23)
    date_from: dt.date | None  # None: since tracking began
    date_to: dt.date | None  # None: ongoing
    note: str
    created_at: dt.datetime

    def covers(self, r: Row) -> bool:
        day: dt.date = r["business_date"]
        return (
            r["venue_uuid"] == self.venue_uuid
            and (self.date_from is None or day >= self.date_from)
            and (self.date_to is None or day <= self.date_to)
            and (day.weekday(), r["start_local"].hour) in self.cells
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "venue_uuid": self.venue_uuid,
            "cells": sorted([wd, hr] for wd, hr in self.cells),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }


def apply_blocks(rows: Iterable[Row], blocks: Sequence[Block]) -> list[Row]:
    """The rows, with every one a block covers marked blocked. Rows are copied
    only when marked; the rest pass through as they are."""
    if not blocks:
        return list(rows)
    by_venue: dict[str, list[Block]] = {}
    for b in blocks:
        by_venue.setdefault(b.venue_uuid, []).append(b)
    out: list[Row] = []
    for r in rows:
        mine = by_venue.get(r["venue_uuid"])
        if mine and any(b.covers(r) for b in mine):
            out.append({**r, MARKED_BLOCKED: True})
        else:
            out.append(r)
    return out
