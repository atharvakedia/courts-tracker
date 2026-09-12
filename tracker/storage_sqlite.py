"""SQLite backend for the :class:`tracker.storage.Storage` protocol.

SQLAlchemy Core only -- no ORM, no session. This module is the single place
where SQLAlchemy exists: every method takes and returns the plain dataclasses
from ``tracker.types`` (or plain dicts, for the read views), so analytics and
web never see a ``Row``, an ``Engine`` or a ``Table``.

Three properties this file exists to guarantee:

* **Idempotency.** ``create_snapshot`` is keyed on ``poll_key`` and returns the
  existing id for a bucket already polled; ``append_observations`` inserts with
  ``ON CONFLICT DO NOTHING`` on ``(snapshot_id, slot_uuid)`` and reports only
  the rows it really wrote. Re-running collect for a cadence bucket is a no-op.
* **Append-only observations.** No method here updates or deletes a
  ``slot_observations`` row. The dataset is forward-looking and cannot be
  backfilled, so a lost or overwritten observation is lost for good. Only the
  derived tables are replaced, and only wholesale inside one transaction.
* **Concurrent read while writing.** WAL journaling is enabled on connect so
  the 30-minute collector never blocks the dashboard reader.

Text encoding follows ``tracker.schema``: UTC instants, local wall-clock and
dates are all TEXT, converted at this boundary by the ``tracker.types`` helpers.
A naive datetime never leaves this module.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from tracker import schema
from tracker.types import (
    DiscoveredFacility,
    FacilityDim,
    FacilityFetch,
    FacilityKind,
    SlotFirstBooked,
    SlotObservation,
    SlotState,
    SnapshotRecord,
    Sport,
    StateTransition,
    VenueDim,
    from_date_text,
    from_utc_text,
    to_date_text,
    to_utc_text,
)

logger = logging.getLogger("storage.sqlite")

#: Rows per executemany. A 2300-slot poll becomes three statements, not 2300.
INSERT_CHUNK_SIZE = 1000

#: Values per ``IN (...)`` clause, kept well under SQLite's parameter limit.
IN_CLAUSE_CHUNK_SIZE = 500

#: Rows the streaming reader buffers at a time.
STREAM_CHUNK_SIZE = 500

_T = TypeVar("_T")


# --------------------------------------------------------------------------
# Engine plumbing
# --------------------------------------------------------------------------


def _is_memory_url(url: str) -> bool:
    """True for ``sqlite://`` and ``sqlite:///:memory:``."""
    database = sa.engine.make_url(url).database
    return database is None or database == ":memory:"


def _ensure_parent_directory(url: str) -> None:
    database = sa.engine.make_url(url).database
    if database:
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Per-connection PRAGMAs.

    ``journal_mode=WAL`` is the load-bearing one: without it the collector's
    write transaction blocks every dashboard read. It is a no-op on an
    in-memory database, which reports ``memory`` instead.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _create_engine(url: str) -> sa.Engine:
    if _is_memory_url(url):
        # StaticPool keeps the one connection alive; a fresh connection would
        # be a fresh, empty database.
        engine = sa.create_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        _ensure_parent_directory(url)
        engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    sa.event.listen(engine, "connect", _apply_pragmas)
    return engine


def _chunked(items: Iterable[_T], size: int) -> Iterator[list[_T]]:
    batch: list[_T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --------------------------------------------------------------------------
# Dataclass <-> row conversion
# --------------------------------------------------------------------------


def _optional_utc_text(value: dt.datetime | None) -> str | None:
    return None if value is None else to_utc_text(value)


def _optional_from_utc_text(value: str | None) -> dt.datetime | None:
    return None if value is None else from_utc_text(value)


def _observation_values(observation: SlotObservation) -> dict[str, Any]:
    return {
        "snapshot_id": observation.snapshot_id,
        "slot_uuid": observation.slot_uuid,
        "venue_uuid": observation.venue_uuid,
        "facility_uuid": observation.facility_uuid,
        "sport": observation.sport.value,
        "slot_start_local": observation.slot_start_local,
        "slot_end_local": observation.slot_end_local,
        "tz": observation.tz,
        "slot_start_utc": to_utc_text(observation.slot_start_utc),
        "duration_minutes": observation.duration_minutes,
        "price": observation.price,
        "total_count": observation.total_count,
        "available_count": observation.available_count,
        "is_available": observation.is_available,
        "is_booked": observation.is_booked,
        "state": observation.state.value,
        "days_ahead": observation.days_ahead,
        "business_date": to_date_text(observation.business_date),
        "is_past": observation.is_past,
    }


def _observation_from_row(row: Mapping[Any, Any]) -> SlotObservation:
    price = row["price"]
    return SlotObservation(
        snapshot_id=int(row["snapshot_id"]),
        slot_uuid=row["slot_uuid"],
        venue_uuid=row["venue_uuid"],
        facility_uuid=row["facility_uuid"],
        sport=Sport(row["sport"]),
        slot_start_local=row["slot_start_local"],
        slot_end_local=row["slot_end_local"],
        tz=row["tz"],
        slot_start_utc=from_utc_text(row["slot_start_utc"]),
        duration_minutes=int(row["duration_minutes"]),
        price=None if price is None else float(price),
        total_count=int(row["total_count"]),
        available_count=int(row["available_count"]),
        is_available=bool(row["is_available"]),
        is_booked=bool(row["is_booked"]),
        state=SlotState(row["state"]),
        days_ahead=int(row["days_ahead"]),
        business_date=from_date_text(row["business_date"]),
        is_past=bool(row["is_past"]),
    )


def _snapshot_from_row(row: Mapping[Any, Any]) -> SnapshotRecord:
    duration_ms = row["duration_ms"]
    return SnapshotRecord(
        snapshot_id=int(row["snapshot_id"]),
        poll_key=row["poll_key"],
        observed_at=from_utc_text(row["observed_at"]),
        ok=bool(row["ok"]),
        error=row["error"],
        duration_ms=None if duration_ms is None else int(duration_ms),
        horizon_days=int(row["horizon_days"]),
    )


def _fetch_values(fetch: FacilityFetch) -> dict[str, Any]:
    return {
        "snapshot_id": fetch.snapshot_id,
        "facility_uuid": fetch.facility_uuid,
        "ok": fetch.ok,
        "http_status": fetch.http_status,
        "error": fetch.error,
        "duration_ms": fetch.duration_ms,
        "slot_count": fetch.slot_count,
        "attempts": fetch.attempts,
    }


def _fetch_from_row(row: Mapping[Any, Any]) -> FacilityFetch:
    http_status = row["http_status"]
    duration_ms = row["duration_ms"]
    return FacilityFetch(
        snapshot_id=int(row["snapshot_id"]),
        facility_uuid=row["facility_uuid"],
        ok=bool(row["ok"]),
        http_status=None if http_status is None else int(http_status),
        error=row["error"],
        duration_ms=None if duration_ms is None else int(duration_ms),
        slot_count=int(row["slot_count"]),
        attempts=int(row["attempts"]),
    )


def _venue_values(venue: VenueDim) -> dict[str, Any]:
    return {
        "venue_uuid": venue.venue_uuid,
        "name": venue.name,
        "short_name": venue.short_name,
        "slug": venue.slug,
        "numeric_id": venue.numeric_id,
        "tz": venue.tz,
        "active": venue.active,
        "first_seen": to_utc_text(venue.first_seen),
        "last_seen": to_utc_text(venue.last_seen),
    }


def _venue_from_row(row: Mapping[Any, Any]) -> VenueDim:
    return VenueDim(
        venue_uuid=row["venue_uuid"],
        name=row["name"],
        short_name=row["short_name"],
        slug=row["slug"],
        numeric_id=row["numeric_id"],
        tz=row["tz"],
        active=bool(row["active"]),
        first_seen=from_utc_text(row["first_seen"]),
        last_seen=from_utc_text(row["last_seen"]),
    )


def _facility_values(facility: FacilityDim) -> dict[str, Any]:
    return {
        "facility_uuid": facility.facility_uuid,
        "venue_uuid": facility.venue_uuid,
        "name": facility.name,
        "kind": facility.kind.value,
        "sport": facility.sport.value,
        "grid_minutes": facility.grid_minutes,
        "active": facility.active,
        "first_seen": to_utc_text(facility.first_seen),
        "last_seen": to_utc_text(facility.last_seen),
    }


def _facility_from_row(row: Mapping[Any, Any]) -> FacilityDim:
    grid_minutes = row["grid_minutes"]
    return FacilityDim(
        facility_uuid=row["facility_uuid"],
        venue_uuid=row["venue_uuid"],
        name=row["name"],
        kind=FacilityKind(row["kind"]),
        sport=Sport(row["sport"]),
        grid_minutes=None if grid_minutes is None else int(grid_minutes),
        active=bool(row["active"]),
        first_seen=from_utc_text(row["first_seen"]),
        last_seen=from_utc_text(row["last_seen"]),
    )


def _transition_values(transition: StateTransition) -> dict[str, Any]:
    return {
        "slot_uuid": transition.slot_uuid,
        "venue_uuid": transition.venue_uuid,
        "facility_uuid": transition.facility_uuid,
        "from_state": None if transition.from_state is None else transition.from_state.value,
        "to_state": transition.to_state.value,
        "first_seen_at": to_utc_text(transition.first_seen_at),
        "prev_seen_at": _optional_utc_text(transition.prev_seen_at),
        "uncertainty_minutes": transition.uncertainty_minutes,
        "slot_start_utc": to_utc_text(transition.slot_start_utc),
        "days_ahead_at_change": transition.days_ahead_at_change,
    }


def _transition_from_row(row: Mapping[Any, Any]) -> StateTransition:
    from_state = row["from_state"]
    uncertainty = row["uncertainty_minutes"]
    return StateTransition(
        slot_uuid=row["slot_uuid"],
        venue_uuid=row["venue_uuid"],
        facility_uuid=row["facility_uuid"],
        from_state=None if from_state is None else SlotState(from_state),
        to_state=SlotState(row["to_state"]),
        first_seen_at=from_utc_text(row["first_seen_at"]),
        prev_seen_at=_optional_from_utc_text(row["prev_seen_at"]),
        uncertainty_minutes=None if uncertainty is None else int(uncertainty),
        slot_start_utc=from_utc_text(row["slot_start_utc"]),
        days_ahead_at_change=int(row["days_ahead_at_change"]),
    )


def _first_booked_values(booked: SlotFirstBooked) -> dict[str, Any]:
    return {
        "slot_uuid": booked.slot_uuid,
        "venue_uuid": booked.venue_uuid,
        "facility_uuid": booked.facility_uuid,
        "slot_start_utc": to_utc_text(booked.slot_start_utc),
        "business_date": to_date_text(booked.business_date),
        "first_booked_at": _optional_utc_text(booked.first_booked_at),
        "last_booked_at": _optional_utc_text(booked.last_booked_at),
        "lead_time_hours": booked.lead_time_hours,
        "uncertainty_minutes": booked.uncertainty_minutes,
        "censored_left": booked.censored_left,
        "rebooked": booked.rebooked,
        "cancelled": booked.cancelled,
    }


def _first_booked_from_row(row: Mapping[Any, Any]) -> SlotFirstBooked:
    lead_time_hours = row["lead_time_hours"]
    uncertainty = row["uncertainty_minutes"]
    return SlotFirstBooked(
        slot_uuid=row["slot_uuid"],
        venue_uuid=row["venue_uuid"],
        facility_uuid=row["facility_uuid"],
        slot_start_utc=from_utc_text(row["slot_start_utc"]),
        business_date=from_date_text(row["business_date"]),
        first_booked_at=_optional_from_utc_text(row["first_booked_at"]),
        last_booked_at=_optional_from_utc_text(row["last_booked_at"]),
        lead_time_hours=None if lead_time_hours is None else float(lead_time_hours),
        uncertainty_minutes=None if uncertainty is None else int(uncertainty),
        censored_left=bool(row["censored_left"]),
        rebooked=bool(row["rebooked"]),
        cancelled=bool(row["cancelled"]),
    )


# --------------------------------------------------------------------------
# The backend
# --------------------------------------------------------------------------


class SQLiteStorage:
    """SQLite implementation of :class:`tracker.storage.Storage`.

    ``url`` is a SQLAlchemy URL: ``sqlite:///data/padel.db`` for a file,
    ``sqlite://`` or ``sqlite:///:memory:`` for an in-memory database whose
    single connection is held open for the life of the object.
    """

    def __init__(
        self,
        url: str,
        *,
        expected_snapshots_per_day: int = schema.DEFAULT_EXPECTED_SNAPSHOTS_PER_DAY,
    ) -> None:
        self._url = url
        self._engine = _create_engine(url)
        self._view_sql: tuple[str, ...] = (
            schema.V_SLOT_SETTLED_SQL,
            schema.V_COURT_MINUTES_DAILY_SQL,
            schema.V_OCCUPANCY_DAILY_SQL,
            schema.coverage_view_sql(expected_snapshots_per_day),
        )

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        """Create tables, indexes and views. Safe to call repeatedly.

        Tables are created only when absent, so existing observations are never
        touched. Views are dropped and recreated: they hold no data, and a
        cadence change must be able to rewrite ``v_coverage_daily``.
        """
        schema.metadata.create_all(self._engine)
        with self._engine.begin() as conn:
            for statement in schema.DROP_VIEW_SQL:
                conn.exec_driver_sql(statement)
            for statement in self._view_sql:
                conn.exec_driver_sql(statement)
        logger.info(
            "storage_initialized",
            extra={"url": self._url, "tables": len(schema.metadata.tables)},
        )

    def close(self) -> None:
        """Dispose of the connection pool."""
        self._engine.dispose()

    # -- snapshots ---------------------------------------------------------

    def get_snapshot_by_poll_key(self, poll_key: str) -> SnapshotRecord | None:
        stmt = sa.select(schema.snapshots).where(schema.snapshots.c.poll_key == poll_key)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return None if row is None else _snapshot_from_row(row)

    def create_snapshot(self, poll_key: str, observed_at: dt.datetime, horizon_days: int) -> int:
        """Open a snapshot, or return the id of the one already in this bucket."""
        insert_stmt = (
            sqlite_insert(schema.snapshots)
            .values(
                poll_key=poll_key,
                observed_at=to_utc_text(observed_at),
                ok=False,
                error=None,
                duration_ms=None,
                horizon_days=horizon_days,
            )
            .on_conflict_do_nothing(index_elements=["poll_key"])
        )
        select_stmt = sa.select(schema.snapshots.c.snapshot_id).where(
            schema.snapshots.c.poll_key == poll_key
        )
        with self._engine.begin() as conn:
            conn.execute(insert_stmt)
            snapshot_id = int(conn.execute(select_stmt).scalar_one())
        return snapshot_id

    def finalize_snapshot(
        self, snapshot_id: int, ok: bool, error: str | None, duration_ms: int
    ) -> None:
        stmt = (
            sa.update(schema.snapshots)
            .where(schema.snapshots.c.snapshot_id == snapshot_id)
            .values(ok=ok, error=error, duration_ms=duration_ms)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def latest_snapshot(self) -> SnapshotRecord | None:
        stmt = sa.select(schema.snapshots).order_by(schema.snapshots.c.observed_at.desc()).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return None if row is None else _snapshot_from_row(row)

    def snapshots_between(self, start: dt.datetime, end: dt.datetime) -> list[SnapshotRecord]:
        stmt = (
            sa.select(schema.snapshots)
            .where(schema.snapshots.c.observed_at >= to_utc_text(start))
            .where(schema.snapshots.c.observed_at < to_utc_text(end))
            .order_by(schema.snapshots.c.observed_at)
        )
        with self._engine.connect() as conn:
            return [_snapshot_from_row(row) for row in conn.execute(stmt).mappings()]

    # -- per-facility fetch outcomes ---------------------------------------

    def record_facility_fetch(self, fetch: FacilityFetch) -> None:
        """Upsert one attempt: a retry's outcome replaces the earlier one."""
        values = _fetch_values(fetch)
        stmt = sqlite_insert(schema.facility_fetches).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["snapshot_id", "facility_uuid"],
            set_={
                "ok": stmt.excluded.ok,
                "http_status": stmt.excluded.http_status,
                "error": stmt.excluded.error,
                "duration_ms": stmt.excluded.duration_ms,
                "slot_count": stmt.excluded.slot_count,
                "attempts": stmt.excluded.attempts,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def facility_fetches_for_snapshot(self, snapshot_id: int) -> list[FacilityFetch]:
        stmt = (
            sa.select(schema.facility_fetches)
            .where(schema.facility_fetches.c.snapshot_id == snapshot_id)
            .order_by(schema.facility_fetches.c.facility_uuid)
        )
        with self._engine.connect() as conn:
            return [_fetch_from_row(row) for row in conn.execute(stmt).mappings()]

    # -- observations ------------------------------------------------------

    def append_observations(self, observations: Iterable[SlotObservation]) -> int:
        """Bulk-append, skipping ``(snapshot_id, slot_uuid)`` already stored.

        One ``executemany`` per chunk of :data:`INSERT_CHUNK_SIZE`. The count is
        read from SQLite's own ``total_changes()`` either side of each chunk, so
        it is the number of rows genuinely written: a replay returns 0.
        """
        stmt = sqlite_insert(schema.slot_observations).on_conflict_do_nothing(
            index_elements=["snapshot_id", "slot_uuid"]
        )
        inserted = 0
        with self._engine.begin() as conn:
            for batch in _chunked(observations, INSERT_CHUNK_SIZE):
                before = int(conn.exec_driver_sql("SELECT total_changes()").scalar_one())
                conn.execute(stmt, [_observation_values(o) for o in batch])
                after = int(conn.exec_driver_sql("SELECT total_changes()").scalar_one())
                inserted += after - before
        logger.info("observations_appended", extra={"inserted": inserted})
        return inserted

    def observations_for_slots(self, slot_uuids: Sequence[str]) -> list[SlotObservation]:
        rows: list[SlotObservation] = []
        with self._engine.connect() as conn:
            for batch in _chunked(slot_uuids, IN_CLAUSE_CHUNK_SIZE):
                stmt = sa.select(schema.slot_observations).where(
                    schema.slot_observations.c.slot_uuid.in_(batch)
                )
                rows.extend(_observation_from_row(row) for row in conn.execute(stmt).mappings())
        rows.sort(key=lambda o: (o.slot_uuid, o.snapshot_id))
        return rows

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
        """Stream matching observations; never materializes the result set."""
        columns = schema.slot_observations.c
        stmt = sa.select(schema.slot_observations)
        if venue_uuid is not None:
            stmt = stmt.where(columns.venue_uuid == venue_uuid)
        if facility_uuid is not None:
            stmt = stmt.where(columns.facility_uuid == facility_uuid)
        if sport is not None:
            stmt = stmt.where(columns.sport == sport.value)
        if snapshot_id is not None:
            stmt = stmt.where(columns.snapshot_id == snapshot_id)
        if business_date_from is not None:
            stmt = stmt.where(columns.business_date >= to_date_text(business_date_from))
        if business_date_to is not None:
            stmt = stmt.where(columns.business_date <= to_date_text(business_date_to))
        if state is not None:
            stmt = stmt.where(columns.state == state.value)
        if not include_past:
            stmt = stmt.where(columns.is_past.is_(False))
        stmt = stmt.order_by(columns.snapshot_id, columns.slot_uuid).execution_options(
            yield_per=STREAM_CHUNK_SIZE
        )
        with self._engine.connect() as conn:
            for row in conn.execute(stmt).mappings():
                yield _observation_from_row(row)

    # -- dimensions --------------------------------------------------------

    def upsert_venue_dim(self, venue: VenueDim) -> None:
        """Insert or refresh a venue. The stored ``first_seen`` always wins."""
        stmt = sqlite_insert(schema.venues).values(**_venue_values(venue))
        stmt = stmt.on_conflict_do_update(
            index_elements=["venue_uuid"],
            set_={
                "name": stmt.excluded.name,
                "short_name": stmt.excluded.short_name,
                "slug": stmt.excluded.slug,
                "numeric_id": stmt.excluded.numeric_id,
                "tz": stmt.excluded.tz,
                "active": stmt.excluded.active,
                "last_seen": stmt.excluded.last_seen,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def upsert_facility_dim(self, facility: FacilityDim) -> None:
        """Insert or refresh a facility. The stored ``first_seen`` always wins."""
        stmt = sqlite_insert(schema.facilities).values(**_facility_values(facility))
        stmt = stmt.on_conflict_do_update(
            index_elements=["facility_uuid"],
            set_={
                "venue_uuid": stmt.excluded.venue_uuid,
                "name": stmt.excluded.name,
                "kind": stmt.excluded.kind,
                "sport": stmt.excluded.sport,
                "grid_minutes": stmt.excluded.grid_minutes,
                "active": stmt.excluded.active,
                "last_seen": stmt.excluded.last_seen,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_venue_dim(self, venue_uuid: str) -> VenueDim | None:
        stmt = sa.select(schema.venues).where(schema.venues.c.venue_uuid == venue_uuid)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return None if row is None else _venue_from_row(row)

    def list_venue_dims(self) -> list[VenueDim]:
        stmt = sa.select(schema.venues).order_by(schema.venues.c.name)
        with self._engine.connect() as conn:
            return [_venue_from_row(row) for row in conn.execute(stmt).mappings()]

    def list_facility_dims(self) -> list[FacilityDim]:
        stmt = sa.select(schema.facilities).order_by(
            schema.facilities.c.venue_uuid, schema.facilities.c.name
        )
        with self._engine.connect() as conn:
            return [_facility_from_row(row) for row in conn.execute(stmt).mappings()]

    def append_venue_name_change(
        self,
        venue_uuid: str,
        observed_at: dt.datetime,
        old_name: str | None,
        new_name: str,
    ) -> None:
        stmt = sa.insert(schema.venue_name_history).values(
            venue_uuid=venue_uuid,
            observed_at=to_utc_text(observed_at),
            old_name=old_name,
            new_name=new_name,
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def append_discovery_log(
        self, observed_at: dt.datetime, entries: Iterable[DiscoveredFacility]
    ) -> int:
        observed_text = to_utc_text(observed_at)
        rows = [
            {
                "observed_at": observed_text,
                "venue_uuid": entry.venue_uuid,
                "facility_uuid": entry.facility_uuid,
                "facility_name": entry.facility_name,
                "activity_id": entry.activity_id,
                "activity_name": entry.activity_name,
                "in_config": entry.in_config,
                "suggested_kind": entry.suggested_kind.value,
            }
            for entry in entries
        ]
        if not rows:
            return 0
        with self._engine.begin() as conn:
            for batch in _chunked(rows, INSERT_CHUNK_SIZE):
                conn.execute(sa.insert(schema.facility_discovery_log), batch)
        return len(rows)

    # -- derived tables ----------------------------------------------------

    def replace_derived_transitions(self, transitions: Iterable[StateTransition]) -> int:
        """Rebuild ``slot_state_transitions`` inside one transaction."""
        return self._replace_derived(
            schema.slot_state_transitions,
            (_transition_values(t) for t in transitions),
        )

    def replace_derived_first_booked(self, rows: Iterable[SlotFirstBooked]) -> int:
        """Rebuild ``slot_first_booked`` inside one transaction."""
        return self._replace_derived(
            schema.slot_first_booked,
            (_first_booked_values(r) for r in rows),
        )

    def _replace_derived(self, table: sa.Table, values: Iterable[dict[str, Any]]) -> int:
        """Delete-then-insert in one transaction.

        The source iterable is consumed *inside* the transaction, so a producer
        that raises half way through leaves the previous contents intact rather
        than an emptied table.
        """
        written = 0
        with self._engine.begin() as conn:
            conn.execute(sa.delete(table))
            for batch in _chunked(values, INSERT_CHUNK_SIZE):
                conn.execute(sa.insert(table), batch)
                written += len(batch)
        logger.info("derived_table_replaced", extra={"table": table.name, "rows": written})
        return written

    def list_transitions(
        self,
        *,
        facility_uuid: str | None = None,
        to_state: SlotState | None = None,
    ) -> list[StateTransition]:
        columns = schema.slot_state_transitions.c
        stmt = sa.select(schema.slot_state_transitions)
        if facility_uuid is not None:
            stmt = stmt.where(columns.facility_uuid == facility_uuid)
        if to_state is not None:
            stmt = stmt.where(columns.to_state == to_state.value)
        stmt = stmt.order_by(columns.slot_uuid, columns.first_seen_at)
        with self._engine.connect() as conn:
            return [_transition_from_row(row) for row in conn.execute(stmt).mappings()]

    def list_first_booked(
        self,
        *,
        facility_uuid: str | None = None,
        exclude_censored: bool = False,
    ) -> list[SlotFirstBooked]:
        columns = schema.slot_first_booked.c
        stmt = sa.select(schema.slot_first_booked)
        if facility_uuid is not None:
            stmt = stmt.where(columns.facility_uuid == facility_uuid)
        if exclude_censored:
            stmt = stmt.where(columns.censored_left.is_(False))
        stmt = stmt.order_by(columns.slot_start_utc, columns.slot_uuid)
        with self._engine.connect() as conn:
            return [_first_booked_from_row(row) for row in conn.execute(stmt).mappings()]

    # -- views -------------------------------------------------------------

    def query_rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read-only, parameterized query and return plain dicts.

        Writes are rejected here: the only sanctioned mutations are the methods
        above, and ``slot_observations`` must stay append-only.
        """
        if not _is_read_only(sql):
            raise ValueError("query_rows accepts SELECT/WITH statements only")
        with self._engine.connect() as conn:
            result = conn.execute(sa.text(sql), params or {})
            return [dict(row) for row in result.mappings()]


def _is_read_only(sql: str) -> bool:
    head = sql.lstrip().lstrip("(").lstrip().upper()
    return head.startswith(("SELECT", "WITH"))
