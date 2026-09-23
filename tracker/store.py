"""The tracker's database: one row per slot, written only when it changes.

Four tables. ``venues`` and ``courts`` describe what exists on Hudle. ``slots``
holds one row per slot ever seen -- its current booked/vacant state, when it was
booked, and how often a booking was cancelled -- so storage grows with the
number of slots (~200 a day per court set), not with how often we look.
``runs`` records every collection pass, which is what tells a gap in the data
apart from a quiet day.

The same code runs on SQLite (tests, local) and Postgres (Neon, production):
SQLAlchemy Core, with the upsert built from the connected dialect's own
``INSERT ... ON CONFLICT``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from tracker.slots import SlotReading
from tracker.types import Sport

metadata = sa.MetaData()

UTC_DT = sa.DateTime(timezone=True)

venues = sa.Table(
    "venues",
    metadata,
    sa.Column("venue_uuid", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("numeric_id", sa.Text, nullable=False),
    sa.Column("first_seen_at", UTC_DT, nullable=False),
    sa.Column("last_seen_at", UTC_DT, nullable=False),
    # Where the venue is, from its Hudle page; null until the page is read.
    sa.Column("latitude", sa.Float),
    sa.Column("longitude", sa.Float),
)

courts = sa.Table(
    "courts",
    metadata,
    sa.Column("facility_uuid", sa.Text, primary_key=True),
    sa.Column(
        "venue_uuid", sa.Text, sa.ForeignKey("venues.venue_uuid"), nullable=False, index=True
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("sport", sa.Text, nullable=False),
    # Whether the daily job polls it. Discovery adds courts tracked; a person
    # can switch one off without losing its history.
    sa.Column("tracked", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("first_seen_at", UTC_DT, nullable=False),
    sa.Column("last_seen_at", UTC_DT, nullable=False),
)

slots = sa.Table(
    "slots",
    metadata,
    sa.Column("slot_uuid", sa.Text, primary_key=True),
    sa.Column("venue_uuid", sa.Text, nullable=False),
    sa.Column("facility_uuid", sa.Text, nullable=False),
    sa.Column("sport", sa.Text, nullable=False),
    sa.Column("business_date", sa.Date, nullable=False),
    sa.Column("start_local", sa.DateTime(timezone=False), nullable=False),
    sa.Column("start_utc", UTC_DT, nullable=False),
    sa.Column("duration_minutes", sa.Integer, nullable=False),
    sa.Column("price", sa.Numeric(10, 2)),
    # Bought by a customer on Hudle. A venue block is not a booking: it shows
    # as hudle_booked and hudle_available both false.
    sa.Column("booked", sa.Boolean, nullable=False),
    sa.Column("hudle_booked", sa.Boolean, nullable=False),
    sa.Column("hudle_available", sa.Boolean, nullable=False),
    # When the current booking was made (Hudle's updated_at at the flip).
    sa.Column("booked_at", UTC_DT),
    # The pass that first saw the current booking: a cross-check on booked_at,
    # which must fall between this and the pass before it.
    sa.Column("booked_seen_at", UTC_DT),
    sa.Column("cancel_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_cancelled_at", UTC_DT),
    sa.Column("first_seen_at", UTC_DT, nullable=False),
    sa.Column("upstream_created_at", UTC_DT),
    sa.Column("upstream_updated_at", UTC_DT),
    sa.Index("ix_slots_sport_date", "sport", "business_date"),
    sa.Index("ix_slots_facility_date", "facility_uuid", "business_date"),
)

#: The dashboard's answers, built once a day (see tracker.views). One row per
#: view; the payload is the JSON the API returns.
views = sa.Table(
    "views",
    metadata,
    sa.Column("view_key", sa.Text, primary_key=True),
    sa.Column("as_of", sa.Date, nullable=False),
    sa.Column("built_at", UTC_DT, nullable=False),
    sa.Column("payload", sa.Text, nullable=False),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("job", sa.Text, nullable=False),
    sa.Column("started_at", UTC_DT, nullable=False),
    sa.Column("finished_at", UTC_DT),
    sa.Column("window_start", sa.Date),
    sa.Column("window_end", sa.Date),
    sa.Column("courts_ok", sa.Integer, nullable=False, server_default="0"),
    sa.Column("courts_failed", sa.Integer, nullable=False, server_default="0"),
    sa.Column("slots_seen", sa.Integer, nullable=False, server_default="0"),
    sa.Column("slots_written", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
)


@dataclass(frozen=True, slots=True)
class Court:
    facility_uuid: str
    venue_uuid: str
    name: str
    sport: Sport
    tracked: bool


def _utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes; everything stored here is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


class Store:
    """All reads and writes. Nothing outside this class builds SQL."""

    def __init__(self, url: str) -> None:
        url = _normalise_url(url)
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("postgresql"):
            # Serverless callers open and drop connections per request; a pool
            # held across invocations only leaks. Neon's pooler does the pooling.
            kwargs["poolclass"] = sa.pool.NullPool
        self._engine = sa.create_engine(url, **kwargs)
        self._insert = postgresql.insert if url.startswith("postgresql") else sqlite.insert

    def initialize(self) -> None:
        metadata.create_all(self._engine)
        self._add_missing_columns()
        self._clear_block_bookings()

    def _clear_block_bookings(self) -> None:
        """Hold ``booked`` to its meaning: a customer bought the slot.

        A venue block is not a booking. Any row whose ``booked`` disagrees with
        Hudle's own ``hudle_booked`` flag -- a block stored as booked -- is set
        back, with its booking stamps cleared. Consistent rows are untouched, so
        on a healthy table this is a single scan that changes nothing.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(slots)
                .where(slots.c.booked != slots.c.hudle_booked)
                .values(booked=slots.c.hudle_booked, booked_at=None, booked_seen_at=None)
            )

    def _add_missing_columns(self) -> None:
        """Bring a table created before a nullable column existed up to date.

        ``create_all`` only creates missing tables, so a column added to an
        existing table is added here, once, by name.
        """
        with self._engine.begin() as conn:
            for table in metadata.sorted_tables:
                present = {c["name"] for c in sa.inspect(conn).get_columns(table.name)}
                for column in table.columns:
                    if column.name not in present and column.nullable:
                        kind = column.type.compile(dialect=conn.dialect)
                        conn.execute(
                            sa.text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {kind}")
                        )

    def close(self) -> None:
        self._engine.dispose()

    # -- venues and courts -------------------------------------------------

    def upsert_venue(
        self, *, venue_uuid: str, name: str, slug: str, numeric_id: str, seen_at: dt.datetime
    ) -> None:
        stmt = self._insert(venues).values(
            venue_uuid=venue_uuid,
            name=name,
            slug=slug,
            numeric_id=numeric_id,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["venue_uuid"],
            set_={
                "name": stmt.excluded.name,
                "slug": stmt.excluded.slug,
                "numeric_id": stmt.excluded.numeric_id,
                "last_seen_at": stmt.excluded.last_seen_at,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def set_venue_location(self, venue_uuid: str, *, latitude: float, longitude: float) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(venues)
                .where(venues.c.venue_uuid == venue_uuid)
                .values(latitude=latitude, longitude=longitude)
            )

    def unlocated_venues(self) -> list[dict[str, Any]]:
        """Venues whose coordinates have not been read yet."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(venues.c.venue_uuid, venues.c.name, venues.c.slug, venues.c.numeric_id)
                .where(venues.c.latitude.is_(None))
                .order_by(venues.c.name)
            )
            return [dict(r) for r in rows.mappings()]

    def upsert_court(
        self, *, facility_uuid: str, venue_uuid: str, name: str, sport: Sport, seen_at: dt.datetime
    ) -> None:
        """Add or refresh a court. A court someone switched off stays off."""
        stmt = self._insert(courts).values(
            facility_uuid=facility_uuid,
            venue_uuid=venue_uuid,
            name=name,
            sport=sport.value,
            tracked=True,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["facility_uuid"],
            set_={"name": stmt.excluded.name, "last_seen_at": stmt.excluded.last_seen_at},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def tracked_courts(self, sport: Sport | None = None) -> list[Court]:
        stmt = (
            sa.select(courts)
            .where(courts.c.tracked.is_(True))
            .order_by(courts.c.venue_uuid, courts.c.name)
        )
        if sport is not None:
            stmt = stmt.where(courts.c.sport == sport.value)
        with self._engine.connect() as conn:
            return [
                Court(r.facility_uuid, r.venue_uuid, r.name, Sport(r.sport), r.tracked)
                for r in conn.execute(stmt)
            ]

    def venues(self) -> dict[str, dict[str, Any]]:
        """Every known venue by uuid, with when it first appeared on Hudle."""
        with self._engine.connect() as conn:
            return {
                r["venue_uuid"]: {
                    **dict(r),
                    "first_seen_at": _utc(r["first_seen_at"]),
                    "last_seen_at": _utc(r["last_seen_at"]),
                }
                for r in conn.execute(sa.select(venues)).mappings()
            }

    # -- slots -------------------------------------------------------------

    def apply(self, readings: Sequence[SlotReading], *, seen_at: dt.datetime) -> int:
        """Record what a pass saw. Returns how many rows were inserted or changed.

        One statement per batch. A slot seen for the first time is inserted; a
        known slot is rewritten only if its state, flags or price changed, and
        the change is interpreted on the way in: vacant to booked stamps
        ``booked_at`` from Hudle and ``booked_seen_at`` from this pass; booked
        to vacant clears the booking and counts a cancellation.
        """
        if not readings:
            return 0
        rows = [_row(r, seen_at) for r in readings]
        written = 0
        with self._engine.begin() as conn:
            for chunk in _chunks(rows, 500):
                stmt = self._insert(slots).values(chunk)
                ex, cur = stmt.excluded, slots.c
                became_booked = sa.and_(ex.booked, sa.not_(cur.booked))
                became_vacant = sa.and_(cur.booked, sa.not_(ex.booked))
                stmt = stmt.on_conflict_do_update(
                    index_elements=["slot_uuid"],
                    set_={
                        "booked": ex.booked,
                        "hudle_booked": ex.hudle_booked,
                        "hudle_available": ex.hudle_available,
                        "price": ex.price,
                        "upstream_updated_at": ex.upstream_updated_at,
                        "booked_at": sa.case(
                            (became_booked, ex.upstream_updated_at),
                            (sa.not_(ex.booked), sa.null()),
                            else_=cur.booked_at,
                        ),
                        "booked_seen_at": sa.case(
                            (became_booked, ex.first_seen_at),
                            (sa.not_(ex.booked), sa.null()),
                            else_=cur.booked_seen_at,
                        ),
                        "cancel_count": cur.cancel_count + sa.case((became_vacant, 1), else_=0),
                        "last_cancelled_at": sa.case(
                            (became_vacant, ex.first_seen_at), else_=cur.last_cancelled_at
                        ),
                    },
                    where=sa.or_(
                        cur.booked.is_distinct_from(ex.booked),
                        cur.hudle_booked.is_distinct_from(ex.hudle_booked),
                        cur.hudle_available.is_distinct_from(ex.hudle_available),
                        cur.price.is_distinct_from(ex.price),
                    ),
                )
                # Count returned keys, not rowcount: Postgres reports -1 for a
                # multi-row insert, and a skipped (unchanged) row returns nothing.
                written += len(conn.execute(stmt.returning(slots.c.slot_uuid)).all())
        return written

    def slots_between(
        self,
        *,
        sport: Sport,
        date_from: dt.date,
        date_to: dt.date,
        venue_uuids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        stmt = (
            sa.select(slots)
            .where(slots.c.sport == sport.value)
            .where(slots.c.business_date.between(date_from, date_to))
            .order_by(slots.c.facility_uuid, slots.c.start_utc)
        )
        if venue_uuids is not None:
            stmt = stmt.where(slots.c.venue_uuid.in_(list(venue_uuids)))
        with self._engine.connect() as conn:
            out = []
            for r in conn.execute(stmt).mappings():
                row = dict(r)
                for k in (
                    "start_utc",
                    "booked_at",
                    "booked_seen_at",
                    "last_cancelled_at",
                    "first_seen_at",
                    "upstream_created_at",
                    "upstream_updated_at",
                ):
                    row[k] = _utc(row[k])
                out.append(row)
            return out

    # -- runs --------------------------------------------------------------

    def start_run(
        self, job: str, *, started_at: dt.datetime, window: tuple[dt.date, dt.date] | None = None
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.insert(runs)
                .values(
                    job=job,
                    started_at=started_at,
                    window_start=window[0] if window else None,
                    window_end=window[1] if window else None,
                )
                .returning(runs.c.run_id)
            )
            return int(result.scalar_one())

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: dt.datetime,
        courts_ok: int,
        courts_failed: int,
        slots_seen: int,
        slots_written: int,
        error: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(runs)
                .where(runs.c.run_id == run_id)
                .values(
                    finished_at=finished_at,
                    courts_ok=courts_ok,
                    courts_failed=courts_failed,
                    slots_seen=slots_seen,
                    slots_written=slots_written,
                    error=error,
                )
            )

    # -- views ---------------------------------------------------------------

    def replace_views(
        self, built: Sequence[tuple[str, str]], *, as_of: dt.date, built_at: dt.datetime
    ) -> None:
        """Swap the stored views for a freshly built set, in one transaction, so
        a reader sees either the old set or the new one, never a mix."""
        with self._engine.begin() as conn:
            conn.execute(sa.delete(views))
            if built:
                conn.execute(
                    sa.insert(views),
                    [
                        {"view_key": k, "as_of": as_of, "built_at": built_at, "payload": p}
                        for k, p in built
                    ],
                )

    def view(self, view_key: str) -> str | None:
        """A stored view's JSON, or None if it was never built."""
        with self._engine.connect() as conn:
            return conn.execute(
                sa.select(views.c.payload).where(views.c.view_key == view_key)
            ).scalar_one_or_none()

    def latest_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        stmt = sa.select(runs).order_by(runs.c.started_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            return [
                {
                    **dict(r),
                    "started_at": _utc(r["started_at"]),
                    "finished_at": _utc(r["finished_at"]),
                }
                for r in conn.execute(stmt).mappings()
            ]


def _row(r: SlotReading, seen_at: dt.datetime) -> dict[str, Any]:
    return {
        "slot_uuid": r.slot_uuid,
        "venue_uuid": r.venue_uuid,
        "facility_uuid": r.facility_uuid,
        "sport": r.sport.value,
        "business_date": r.business_date,
        "start_local": r.start_local,
        "start_utc": r.start_utc,
        "duration_minutes": r.duration_minutes,
        "price": r.price,
        "booked": r.booked,
        "hudle_booked": r.hudle_booked,
        "hudle_available": r.hudle_available,
        # First sight of a slot that is already booked: the booking predates us,
        # so Hudle's stamp is the only record of when it happened.
        "booked_at": r.upstream_updated_at if r.booked else None,
        "booked_seen_at": seen_at if r.booked else None,
        "cancel_count": 0,
        "last_cancelled_at": None,
        "first_seen_at": seen_at,
        "upstream_created_at": r.upstream_created_at,
        "upstream_updated_at": r.upstream_updated_at,
    }


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _normalise_url(url: str) -> str:
    """Neon hands out ``postgres://``/``postgresql://``; SQLAlchemy wants a driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url
