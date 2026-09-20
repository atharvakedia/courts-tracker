"""SQLAlchemy Core table definitions. No engine, no connection, no I/O.

``slot_observations`` is append-only and is the only irreplaceable thing in the
system: the dataset is forward-looking, so nothing here can be backfilled.
Dimensions, derived tables and views are all reconstructible from it.

Storage encoding contract (so SQLite and Postgres agree, and so the views can
slice dates out of text):

* UTC instants  -> TEXT, ``tracker.types.UTC_TEXT_FORMAT``  (``2026-09-11T14:30:00Z``)
* local clock   -> TEXT, ``tracker.types.LOCAL_TEXT_FORMAT`` (``2026-09-11 06:00:00``)
* dates         -> TEXT, ``tracker.types.DATE_TEXT_FORMAT``  (``2026-09-11``)
* booleans      -> BOOLEAN (0/1 in SQLite)
"""

from __future__ import annotations

import sqlalchemy as sa

from tracker.types import SlotState, Sport

metadata = sa.MetaData()

_STATE_VALUES = ", ".join(f"'{s.value}'" for s in SlotState)
_SPORT_VALUES = ", ".join(f"'{s.value}'" for s in Sport)

# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------

venues = sa.Table(
    "venues",
    metadata,
    sa.Column("venue_uuid", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("short_name", sa.Text, nullable=False),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("numeric_id", sa.Text, nullable=False),
    sa.Column("tz", sa.Text, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    # first_seen / last_seen are what make "a fourth venue appeared" and "one
    # disappeared" answerable after the fact rather than only at alert time.
    sa.Column("first_seen", sa.Text, nullable=False),
    sa.Column("last_seen", sa.Text, nullable=False),
)

facilities = sa.Table(
    "facilities",
    metadata,
    sa.Column("facility_uuid", sa.Text, primary_key=True),
    sa.Column(
        "venue_uuid",
        sa.Text,
        sa.ForeignKey("venues.venue_uuid"),
        nullable=False,
        index=True,
    ),
    sa.Column("name", sa.Text, nullable=False),
    # kind is mirrored from config.yaml, never inferred at write time.
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("sport", sa.Text, nullable=False),
    sa.Column("grid_minutes", sa.Integer, nullable=True),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("first_seen", sa.Text, nullable=False),
    sa.Column("last_seen", sa.Text, nullable=False),
    sa.CheckConstraint("kind IN ('court', 'equipment')", name="ck_facilities_kind"),
    sa.CheckConstraint(
        "grid_minutes IS NULL OR grid_minutes > 0", name="ck_facilities_grid_minutes"
    ),
)

venue_name_history = sa.Table(
    "venue_name_history",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("venue_uuid", sa.Text, nullable=False, index=True),
    sa.Column("observed_at", sa.Text, nullable=False),
    sa.Column("old_name", sa.Text, nullable=True),
    sa.Column("new_name", sa.Text, nullable=False),
)

facility_discovery_log = sa.Table(
    "facility_discovery_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("observed_at", sa.Text, nullable=False, index=True),
    sa.Column("venue_uuid", sa.Text, nullable=False),
    sa.Column("facility_uuid", sa.Text, nullable=False),
    sa.Column("facility_name", sa.Text, nullable=False),
    sa.Column("activity_id", sa.Integer, nullable=True),
    sa.Column("activity_name", sa.Text, nullable=True),
    sa.Column("in_config", sa.Boolean, nullable=False),
    sa.Column("suggested_kind", sa.Text, nullable=False),
    sa.Index("ix_discovery_log_facility", "facility_uuid", "observed_at"),
)

# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------

snapshots = sa.Table(
    "snapshots",
    metadata,
    sa.Column("snapshot_id", sa.Integer, primary_key=True, autoincrement=True),
    # Idempotency key: observed_at floored to poll.cadence_minutes. A second run
    # inside the same bucket reuses this row and writes no duplicate rows.
    sa.Column("poll_key", sa.Text, nullable=False, unique=True),
    sa.Column("observed_at", sa.Text, nullable=False, index=True),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    # What horizon was asked for, so a config change is visible in history.
    sa.Column("horizon_days", sa.Integer, nullable=False),
)

facility_fetches = sa.Table(
    "facility_fetches",
    metadata,
    sa.Column(
        "snapshot_id",
        sa.Integer,
        sa.ForeignKey("snapshots.snapshot_id"),
        primary_key=True,
    ),
    sa.Column("facility_uuid", sa.Text, primary_key=True),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("http_status", sa.Integer, nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("slot_count", sa.Integer, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
)

slot_observations = sa.Table(
    "slot_observations",
    metadata,
    sa.Column(
        "snapshot_id",
        sa.Integer,
        sa.ForeignKey("snapshots.snapshot_id"),
        nullable=False,
    ),
    sa.Column("slot_uuid", sa.Text, nullable=False),
    sa.Column("venue_uuid", sa.Text, nullable=False),
    sa.Column("facility_uuid", sa.Text, nullable=False),
    # Mirrored from config.yaml at write time, never inferred. Denormalized
    # onto every row because without it a venue-level total silently adds
    # Padel Fort's two pickleball courts to its one padel court.
    sa.Column("sport", sa.Text, nullable=False),
    sa.Column("slot_start_local", sa.Text, nullable=False),
    sa.Column("slot_end_local", sa.Text, nullable=False),
    sa.Column("tz", sa.Text, nullable=False),
    sa.Column("slot_start_utc", sa.Text, nullable=False),
    # Denormalized so court-minute math is one column, not a join plus a parse.
    sa.Column("duration_minutes", sa.Integer, nullable=False),
    sa.Column("price", sa.Numeric, nullable=True),
    sa.Column("total_count", sa.Integer, nullable=False),
    sa.Column("available_count", sa.Integer, nullable=False),
    # Raw flags are kept alongside `state` deliberately: if the classification
    # rule turns out to be wrong, every past observation can be reclassified.
    sa.Column("is_available", sa.Boolean, nullable=False),
    sa.Column("is_booked", sa.Boolean, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("days_ahead", sa.Integer, nullable=False),
    sa.Column("business_date", sa.Text, nullable=False),
    # Computed by us. Hudle never marks elapsed slots unavailable, so pastness
    # is orthogonal to state and must never be inferred from is_available.
    sa.Column("is_past", sa.Boolean, nullable=False),
    # Hudle's own row timestamps, UTC. Nullable: older observations predate the
    # columns, and the upstream payload is not contractually obliged to carry them.
    sa.Column("upstream_created_at", sa.Text, nullable=True),
    sa.Column("upstream_updated_at", sa.Text, nullable=True),
    sa.PrimaryKeyConstraint("snapshot_id", "slot_uuid", name="pk_slot_observations"),
    sa.CheckConstraint(f"state IN ({_STATE_VALUES})", name="ck_slot_observations_state"),
    sa.CheckConstraint(f"sport IN ({_SPORT_VALUES})", name="ck_slot_observations_sport"),
    sa.CheckConstraint("duration_minutes > 0", name="ck_slot_observations_duration"),
    # Deliberate indexing for the real query patterns: daily aggregation per
    # court, per-slot trajectory reconstruction, state-filtered day scans, and
    # the single-sport read every cross-venue chart starts from.
    sa.Index("ix_slot_obs_facility_business_date", "facility_uuid", "business_date"),
    sa.Index("ix_slot_obs_slot_snapshot", "slot_uuid", "snapshot_id"),
    # A window read filters on business_date alone, then partitions by slot and
    # orders by snapshot. Every other index here leads with a different column,
    # so that query was a full table scan plus a sort; this one serves the range
    # seek and the partition order together.
    sa.Index(
        "ix_slot_obs_business_date_slot_snapshot",
        "business_date",
        "slot_uuid",
        "snapshot_id",
    ),
    sa.Index("ix_slot_obs_state_business_date", "state", "business_date"),
    sa.Index("ix_slot_obs_sport_business_date", "sport", "business_date"),
)

# --------------------------------------------------------------------------
# Derived (regenerable from slot_observations; never hand-edited)
# --------------------------------------------------------------------------

slot_state_transitions = sa.Table(
    "slot_state_transitions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("slot_uuid", sa.Text, nullable=False),
    sa.Column("venue_uuid", sa.Text, nullable=False),
    sa.Column("facility_uuid", sa.Text, nullable=False),
    sa.Column("from_state", sa.Text, nullable=True),
    sa.Column("to_state", sa.Text, nullable=False),
    sa.Column("first_seen_at", sa.Text, nullable=False),
    sa.Column("prev_seen_at", sa.Text, nullable=True),
    # first_seen_at - prev_seen_at: the poll gap the true change hid inside.
    sa.Column("uncertainty_minutes", sa.Integer, nullable=True),
    sa.Column("slot_start_utc", sa.Text, nullable=False),
    sa.Column("days_ahead_at_change", sa.Integer, nullable=False),
    sa.CheckConstraint(f"to_state IN ({_STATE_VALUES})", name="ck_transitions_to_state"),
    sa.CheckConstraint(
        f"from_state IS NULL OR from_state IN ({_STATE_VALUES})",
        name="ck_transitions_from_state",
    ),
    sa.Index("ix_transitions_slot", "slot_uuid", "first_seen_at"),
    sa.Index("ix_transitions_facility_change", "facility_uuid", "from_state", "to_state"),
)

slot_first_booked = sa.Table(
    "slot_first_booked",
    metadata,
    sa.Column("slot_uuid", sa.Text, primary_key=True),
    sa.Column("venue_uuid", sa.Text, nullable=False),
    sa.Column("facility_uuid", sa.Text, nullable=False, index=True),
    sa.Column("slot_start_utc", sa.Text, nullable=False),
    sa.Column("business_date", sa.Text, nullable=False, index=True),
    sa.Column("first_booked_at", sa.Text, nullable=True),
    sa.Column("last_booked_at", sa.Text, nullable=True),
    sa.Column("lead_time_hours", sa.Float, nullable=True),
    sa.Column("uncertainty_minutes", sa.Integer, nullable=True),
    # Already BOOKED in the first snapshot that ever saw it: the booking
    # predates our data, so it is excluded from lead-time statistics.
    sa.Column("censored_left", sa.Boolean, nullable=False),
    sa.Column("rebooked", sa.Boolean, nullable=False),
    sa.Column("cancelled", sa.Boolean, nullable=False),
)

# --------------------------------------------------------------------------
# Read views
# --------------------------------------------------------------------------
#
# Four rules hold across all of them, and breaking any of them produces numbers
# that look plausible and are wrong:
#
#   1. Aggregate in COURT-MINUTES (SUM(duration_minutes)), never COUNT(*) of
#      slots. Padel Up sells a 60-minute grid and the other two sell 30, so a
#      slot count is not comparable across venues.
#   2. Group by business_date, never the raw local date. Play Padel's
#      00:00-01:30 sales belong to the previous evening's trading day.
#   3. Reduce to one SETTLED row per slot first, using exactly the definition
#      tracker.analytics.occupancy.settled_observations uses. Two different
#      answers to "which row is this slot" print two different headline
#      occupancies from one dataset.
#   4. Never divide a price total by a slot count. Price is only comparable per
#      court-hour; price_per_court_hour below is the column to read.

#: One row per slot: the state it settled in.
#:
#: The chosen row is the last observation taken BEFORE the slot started. Slots
#: whose every observation is already elapsed -- collection first reached them
#: after they began -- fall back to the earliest sighting, the one nearest the
#: slot's own start. They are kept, not dropped: Hudle never marks an elapsed
#: slot unavailable, so an elapsed unsold slot genuinely was sellable inventory
#: that went unsold.
#:
#: This is the same rule as ``tracker.analytics.occupancy.settled_observations``
#: and the two must stay identical. A plain ``MAX(snapshot_id)`` is NOT the same
#: rule: Hudle keeps publishing a slot after it elapses, so a late cancellation,
#: an unblocking or a grid republish changes the last row without changing what
#: the slot settled as, and the SQL chart and the Python chart would then
#: disagree about the same day.
V_SLOT_SETTLED_SQL = """
CREATE VIEW IF NOT EXISTS v_slot_settled AS
SELECT o.*
FROM slot_observations AS o
JOIN (
    SELECT
        slot_uuid,
        COALESCE(
            MAX(CASE WHEN is_past THEN NULL ELSE snapshot_id END),
            MIN(snapshot_id)
        ) AS snapshot_id
    FROM slot_observations
    GROUP BY slot_uuid
) AS settled
  ON settled.slot_uuid = o.slot_uuid
 AND settled.snapshot_id = o.snapshot_id
"""

#: The normalization layer. Every cross-venue chart reads this, never raw counts.
#:
#: ``sport`` is a grouping key because two venues publish pickleball courts
#: beside their padel one, and a venue-level sum across both compares a
#: three-court venue against a one-court venue.
V_COURT_MINUTES_DAILY_SQL = """
CREATE VIEW IF NOT EXISTS v_court_minutes_daily AS
SELECT
    l.venue_uuid                AS venue_uuid,
    l.facility_uuid             AS facility_uuid,
    l.sport                     AS sport,
    l.business_date             AS business_date,
    l.state                     AS state,
    SUM(l.duration_minutes)     AS court_minutes,
    COUNT(*)                    AS slots,
    SUM(CASE WHEN l.is_past THEN l.duration_minutes ELSE 0 END) AS past_court_minutes,
    SUM(COALESCE(l.price, 0))   AS slot_price_total,
    CASE
        WHEN SUM(l.duration_minutes) = 0 THEN NULL
        ELSE 1.0 * SUM(COALESCE(l.price, 0)) / (SUM(l.duration_minutes) / 60.0)
    END                         AS price_per_court_hour
FROM v_slot_settled AS l
GROUP BY l.venue_uuid, l.facility_uuid, l.sport, l.business_date, l.state
"""

#: occupancy_strict is the headline; occupancy_gross and blocked_share sit
#: beside it so a venue that blocks inventory to sell it offline cannot read as
#: merely empty. Raw numerators and denominators are kept so every chart can
#: print its own denominator.
V_OCCUPANCY_DAILY_SQL = """
CREATE VIEW IF NOT EXISTS v_occupancy_daily AS
SELECT
    venue_uuid,
    facility_uuid,
    sport,
    business_date,
    SUM(CASE WHEN state = 'BOOKED'  THEN court_minutes ELSE 0 END) AS booked_minutes,
    SUM(CASE WHEN state = 'OPEN'    THEN court_minutes ELSE 0 END) AS open_minutes,
    SUM(CASE WHEN state = 'BLOCKED' THEN court_minutes ELSE 0 END) AS blocked_minutes,
    SUM(court_minutes) AS total_minutes,
    SUM(CASE WHEN state IN ('BOOKED', 'OPEN') THEN court_minutes ELSE 0 END)
        AS sellable_minutes,
    CASE
        WHEN SUM(CASE WHEN state IN ('BOOKED', 'OPEN') THEN court_minutes ELSE 0 END) = 0
        THEN NULL
        ELSE 1.0 * SUM(CASE WHEN state = 'BOOKED' THEN court_minutes ELSE 0 END)
             / SUM(CASE WHEN state IN ('BOOKED', 'OPEN') THEN court_minutes ELSE 0 END)
    END AS occupancy_strict,
    CASE
        WHEN SUM(court_minutes) = 0 THEN NULL
        ELSE 1.0 * SUM(CASE WHEN state IN ('BOOKED', 'BLOCKED') THEN court_minutes ELSE 0 END)
             / SUM(court_minutes)
    END AS occupancy_gross,
    CASE
        WHEN SUM(court_minutes) = 0 THEN NULL
        ELSE 1.0 * SUM(CASE WHEN state = 'BLOCKED' THEN court_minutes ELSE 0 END)
             / SUM(court_minutes)
    END AS blocked_share
FROM v_court_minutes_daily
GROUP BY venue_uuid, facility_uuid, sport, business_date
"""

#: Snapshots expected vs received per facility per UTC day. Charts read this to
#: draw gaps instead of interpolating across them.
V_COVERAGE_DAILY_SQL_TEMPLATE = """
CREATE VIEW IF NOT EXISTS v_coverage_daily AS
SELECT
    f.facility_uuid                                       AS facility_uuid,
    substr(s.observed_at, 1, 10)                          AS observed_date,
    {expected}                                            AS snapshots_expected,
    COUNT(DISTINCT f.snapshot_id)                         AS snapshots_received,
    SUM(CASE WHEN f.ok THEN 1 ELSE 0 END)                 AS fetches_ok,
    SUM(CASE WHEN f.ok THEN 0 ELSE 1 END)                 AS fetches_failed,
    SUM(f.slot_count)                                     AS slot_rows,
    MIN(s.observed_at)                                    AS first_observed_at,
    MAX(s.observed_at)                                    AS last_observed_at,
    1.0 * COUNT(DISTINCT f.snapshot_id) / {expected}      AS coverage_ratio
FROM facility_fetches AS f
JOIN snapshots AS s ON s.snapshot_id = f.snapshot_id
GROUP BY f.facility_uuid, substr(s.observed_at, 1, 10)
"""

#: Default expected polls per UTC day at the frozen 30-minute cadence. Pass
#: ``PollConfig.expected_snapshots_per_day`` to :func:`coverage_view_sql` if the
#: cadence ever changes.
DEFAULT_EXPECTED_SNAPSHOTS_PER_DAY = 48

V_COVERAGE_DAILY_SQL = V_COVERAGE_DAILY_SQL_TEMPLATE.format(
    expected=DEFAULT_EXPECTED_SNAPSHOTS_PER_DAY
)


def coverage_view_sql(expected_snapshots_per_day: int) -> str:
    """``v_coverage_daily`` bound to a specific poll cadence."""
    if expected_snapshots_per_day <= 0:
        raise ValueError("expected_snapshots_per_day must be positive")
    return V_COVERAGE_DAILY_SQL_TEMPLATE.format(expected=expected_snapshots_per_day)


#: Views in creation order; later ones read earlier ones.
VIEW_SQL: tuple[str, ...] = (
    V_SLOT_SETTLED_SQL,
    V_COURT_MINUTES_DAILY_SQL,
    V_OCCUPANCY_DAILY_SQL,
    V_COVERAGE_DAILY_SQL,
)

#: View names in drop order (reverse of creation).
VIEW_NAMES: tuple[str, ...] = (
    "v_coverage_daily",
    "v_occupancy_daily",
    "v_court_minutes_daily",
    "v_slot_settled",
)

DROP_VIEW_SQL: tuple[str, ...] = tuple(f"DROP VIEW IF EXISTS {name}" for name in VIEW_NAMES)
