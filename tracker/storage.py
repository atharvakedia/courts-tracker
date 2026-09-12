"""The persistence boundary.

Only the :class:`Storage` protocol lives here: no SQLite, no engine, no SQL.
Concrete backends implement it (``tracker.storage_sqlite.SQLiteStorage`` today,
a Postgres class later) and every caller depends on this protocol instead.

Everything crossing this boundary is a plain dataclass from ``tracker.types``,
so analytics and web never see a database Row.

Idempotency contract
--------------------
Polling is retried, replayed and rescheduled; duplicates must converge on one
final state rather than on duplicate rows.

* ``create_snapshot`` is keyed on ``poll_key`` (``observed_at`` floored to
  ``poll.cadence_minutes``). Calling it twice with the same ``poll_key``
  returns the **same** ``snapshot_id`` and creates no second row. The
  ``observed_at`` and ``horizon_days`` of the first call win.
* ``append_observations`` must not duplicate rows: ``(snapshot_id, slot_uuid)``
  is unique, and re-appending an already-written observation is a no-op rather
  than an error. It returns the number of rows actually inserted, so a replay
  legitimately returns 0.
* ``record_facility_fetch`` is keyed on ``(snapshot_id, facility_uuid)`` and
  overwrites: a retry's outcome replaces the earlier attempt's.
* ``finalize_snapshot`` is last-write-wins on the snapshot row only. It never
  touches observations.
* Existing ``slot_observations`` rows are never updated or deleted by any
  method on this protocol. Only the derived tables are replaced wholesale, by
  ``replace_derived_transitions`` / ``replace_derived_first_booked``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

from tracker.types import (
    DiscoveredFacility,
    FacilityDim,
    FacilityFetch,
    SlotFirstBooked,
    SlotObservation,
    SlotState,
    SnapshotRecord,
    Sport,
    StateTransition,
    VenueDim,
)

__all__ = ["Storage"]


@runtime_checkable
class Storage(Protocol):
    """Append-only observation store plus its dimensions and derived tables."""

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Create tables, indexes and views if absent. Safe to call repeatedly."""
        raise NotImplementedError

    # -- snapshots ---------------------------------------------------------

    def get_snapshot_by_poll_key(self, poll_key: str) -> SnapshotRecord | None:
        """The snapshot for this cadence bucket, or None if this bucket is new."""
        raise NotImplementedError

    def create_snapshot(self, poll_key: str, observed_at: dt.datetime, horizon_days: int) -> int:
        """Open a snapshot and return its id.

        Idempotent on ``poll_key``: a second call in the same cadence bucket
        returns the existing ``snapshot_id`` unchanged.
        """
        raise NotImplementedError

    def finalize_snapshot(
        self, snapshot_id: int, ok: bool, error: str | None, duration_ms: int
    ) -> None:
        """Record the run's outcome. Last write wins; observations untouched."""
        raise NotImplementedError

    def latest_snapshot(self) -> SnapshotRecord | None:
        """The snapshot with the greatest ``observed_at``, or None if empty."""
        raise NotImplementedError

    def snapshots_between(self, start: dt.datetime, end: dt.datetime) -> list[SnapshotRecord]:
        """Snapshots with ``start <= observed_at < end``, ascending."""
        raise NotImplementedError

    # -- per-facility fetch outcomes ---------------------------------------

    def record_facility_fetch(self, fetch: FacilityFetch) -> None:
        """Upsert one (snapshot, facility) fetch outcome."""
        raise NotImplementedError

    def facility_fetches_for_snapshot(self, snapshot_id: int) -> list[FacilityFetch]:
        """Every recorded fetch for one snapshot."""
        raise NotImplementedError

    # -- observations ------------------------------------------------------

    def append_observations(self, observations: Iterable[SlotObservation]) -> int:
        """Append observations, skipping any ``(snapshot_id, slot_uuid)`` already
        stored. Returns the number of rows actually inserted."""
        raise NotImplementedError

    def observations_for_slots(self, slot_uuids: Sequence[str]) -> list[SlotObservation]:
        """Every observation of the given slots, ordered by slot then snapshot.

        This is the per-slot trajectory read that transition, lead-time and
        cancellation derivation are built on.
        """
        raise NotImplementedError

    def iter_observations(
        self,
        *,
        venue_uuid: str | None = None,
        facility_uuid: str | None = None,
        sport: Sport | None = None,
        snapshot_id: int | None = None,
        business_date_from: dt.date | None = None,
        business_date_to: dt.date | None = None,
        state: SlotState | None = None,
        include_past: bool = True,
    ) -> Iterator[SlotObservation]:
        """Stream observations matching the filters, ordered by
        ``(snapshot_id, slot_uuid)``.

        ``business_date_from`` / ``business_date_to`` are inclusive. Filters
        are on ``business_date``, not the raw local date. ``include_past=False``
        drops rows whose ``is_past`` is true, which is what any
        "still winnable" or current-availability read needs.

        ``sport`` is the filter every cross-venue read needs first: Play Padel
        and Padel Fort publish pickleball courts beside their padel one, and a
        venue total that mixes them compares three courts against one.
        """
        raise NotImplementedError

    # -- dimensions --------------------------------------------------------

    def upsert_venue_dim(self, venue: VenueDim) -> None:
        """Insert or update a venue, advancing ``last_seen`` and preserving the
        stored ``first_seen`` when the row already exists."""
        raise NotImplementedError

    def upsert_facility_dim(self, facility: FacilityDim) -> None:
        """Insert or update a facility, preserving the stored ``first_seen``."""
        raise NotImplementedError

    def get_venue_dim(self, venue_uuid: str) -> VenueDim | None:
        raise NotImplementedError

    def list_venue_dims(self) -> list[VenueDim]:
        raise NotImplementedError

    def list_facility_dims(self) -> list[FacilityDim]:
        raise NotImplementedError

    def append_venue_name_change(
        self,
        venue_uuid: str,
        observed_at: dt.datetime,
        old_name: str | None,
        new_name: str,
    ) -> None:
        """Append a rename. A rename is itself a market signal, so it is kept."""
        raise NotImplementedError

    def append_discovery_log(
        self, observed_at: dt.datetime, entries: Iterable[DiscoveredFacility]
    ) -> int:
        """Append what the SSR page said, so facility drift is auditable.
        Returns the number of rows written. Never mutates ``facilities``."""
        raise NotImplementedError

    # -- derived tables ----------------------------------------------------

    def replace_derived_transitions(self, transitions: Iterable[StateTransition]) -> int:
        """Replace ``slot_state_transitions`` wholesale. Returns rows written."""
        raise NotImplementedError

    def replace_derived_first_booked(self, rows: Iterable[SlotFirstBooked]) -> int:
        """Replace ``slot_first_booked`` wholesale. Returns rows written."""
        raise NotImplementedError

    def list_transitions(
        self,
        *,
        facility_uuid: str | None = None,
        to_state: SlotState | None = None,
    ) -> list[StateTransition]:
        raise NotImplementedError

    def list_first_booked(
        self,
        *,
        facility_uuid: str | None = None,
        exclude_censored: bool = False,
    ) -> list[SlotFirstBooked]:
        """Per-slot booking summaries. ``exclude_censored=True`` drops
        left-censored slots, which is mandatory for lead-time statistics."""
        raise NotImplementedError

    # -- views -------------------------------------------------------------

    def query_rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read-only query against the views and return plain dicts.

        The only escape hatch from the dataclass boundary, and it is for the
        ``v_*`` read views. Callers get dicts, never Rows.
        """
        raise NotImplementedError
