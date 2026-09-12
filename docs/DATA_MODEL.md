# Data model

The tables as built, in `tracker/schema.py`.
SQLAlchemy Core, SQLite today, Postgres later. Nothing here leaks past the
`Storage` protocol: analytics and web receive plain dataclasses / dicts.

## Principle

`slot_observations` is append-only and is the only irreplaceable thing in the system.
Everything else — dimensions, derived tables, views — is reconstructible from it.
No row in it is ever updated or deleted.

---

## Dimension tables

### `venues`

| column        | type    | notes                                    |
| ------------- | ------- | ---------------------------------------- |
| `venue_uuid`  | TEXT PK | Hudle venue UUID                         |
| `name`        | TEXT    |                                          |
| `short_name`  | TEXT    | dashboard label                          |
| `slug`        | TEXT    | from `share_url`, for the SSR page        |
| `numeric_id`  | TEXT    | from `share_url`                          |
| `tz`          | TEXT    | `Asia/Kolkata`                            |
| `active`      | BOOL    |                                          |
| `first_seen`  | TEXT    | UTC ISO — when discovery first saw it    |
| `last_seen`   | TEXT    | UTC ISO — updated each discovery run     |

`first_seen` / `last_seen` are what make "a fourth venue appeared" and "one
disappeared" answerable after the fact, not just at alert time.

### `facilities`

| column           | type    | notes                                             |
| ---------------- | ------- | ------------------------------------------------- |
| `facility_uuid`  | TEXT PK |                                                   |
| `venue_uuid`     | TEXT FK |                                                   |
| `name`           | TEXT    |                                                   |
| `kind`           | TEXT    | `court` \| `equipment` — **from config, frozen**  |
| `sport`          | TEXT    | `padel` \| `pickleball`                           |
| `grid_minutes`   | INT     | 30 or 60; observed, cross-checked against config  |
| `active`         | BOOL    |                                                   |
| `first_seen`     | TEXT    |                                                   |
| `last_seen`      | TEXT    |                                                   |

`kind` is mirrored from `config.yaml`, never inferred at write time. Discovery
writes to `facility_discovery_log` instead and warns.

### `facility_discovery_log`

Append-only record of what the SSR page said, so drift is auditable:
`id`, `observed_at` (UTC), `venue_uuid`, `facility_uuid`, `facility_name`,
`activity_id`, `activity_name`, `in_config` (bool), `suggested_kind`.

---

## Observation tables

### `snapshots` — one row per collect run

| column           | type    | notes                                          |
| ---------------- | ------- | ---------------------------------------------- |
| `snapshot_id`    | INT PK  |                                                |
| `poll_key`       | TEXT UQ | **idempotency key**: `observed_at` floored to `cadence_minutes`, e.g. `2026-09-11T14:30Z`. A second run inside the same bucket reuses the row and writes no duplicate observations. |
| `observed_at`    | TEXT    | UTC ISO, actual start                          |
| `ok`             | BOOL    | true only if every active facility fetched ok  |
| `error`          | TEXT    | null unless the run failed as a whole          |
| `duration_ms`    | INT     |                                                |
| `horizon_days`   | INT     | what was asked for, so a config change is visible in history |

### `facility_fetches` — one row per (snapshot, facility)

**Addition to your spec.** One venue failing shouldn't mark the whole poll bad or
imply the other two are missing. This is what lets the dashboard draw an honest
per-venue gap instead of a global one.

`snapshot_id`, `facility_uuid`, `ok`, `http_status`, `error`, `duration_ms`,
`slot_count`, `attempts`. PK `(snapshot_id, facility_uuid)`.

### `slot_observations` — append-only, the whole point

| column             | type | notes                                                          |
| ------------------ | ---- | -------------------------------------------------------------- |
| `snapshot_id`      | INT  | FK                                                             |
| `slot_uuid`        | TEXT | Hudle slot `id`                                                |
| `venue_uuid`       | TEXT |                                                                |
| `facility_uuid`    | TEXT |                                                                |
| `sport`            | TEXT | `padel` \| `pickleball` — mirrored from `config.yaml` at write time, never inferred |
| `slot_start_local` | TEXT | naive local wall-clock `YYYY-MM-DD HH:MM:SS`                   |
| `slot_end_local`   | TEXT | naive local wall-clock                                         |
| `tz`               | TEXT | `Asia/Kolkata` — explicit, never implied                       |
| `slot_start_utc`   | TEXT | **derived at write time** from local+tz. See note below.       |
| `duration_minutes` | INT  | `end - start`. Denormalized so court-minutes math is one column, not a join + parse. |
| `price`            | NUM  | as returned, per slot (not per hour)                           |
| `total_count`      | INT  |                                                                |
| `available_count`  | INT  |                                                                |
| `is_available`     | BOOL | raw                                                            |
| `is_booked`        | BOOL | raw                                                            |
| `state`            | TEXT | `BOOKED` \| `BLOCKED` \| `OPEN` — classified at write time     |
| `days_ahead`       | INT  | `slot local date - observed local date`, in days                |
| `business_date`    | TEXT | local date, with pre-04:00 slots rolled back one day           |
| `is_past`          | BOOL | `slot_start_utc < observed_at`. **Computed by us, not Hudle.** |

**`is_past` exists because the brief's state table is wrong on one point.** Hudle
does *not* mark elapsed slots unavailable: at 16:21 IST, Padel Fort's 07:00 and
16:00 slots today were both `is_available: true, is_booked: false` — i.e. `OPEN`.
So "the slot is simply in the past" is not a cause of `BLOCKED`. Pastness is
orthogonal to state and is recorded as its own flag rather than folded into the
enum. Retrospective day occupancy is unaffected (an elapsed unsold slot really
was sellable inventory that went unsold). What it does affect is any "still
winnable" or time-to-sellout view, which must filter on `is_past` itself.

**`business_date` exists because Play Padel sells 00:00–01:30**, and Hudle
attributes those to the calendar date they fall on. A Friday-night 00:30 session
is stamped Saturday. Verified live: 2026-09-12 (a Saturday) has bookings at 00:30
and 01:00 that are Friday-night demand. Slots starting before
`business_day_start_hour` roll back one day for aggregation. Wall-clock is never
mutated; this is an extra column, not a rewrite.

PK / unique: `(snapshot_id, slot_uuid)`.

Raw `is_available` / `is_booked` are kept alongside `state` deliberately: if the
classification rule turns out to be wrong, every past observation can be
reclassified without having lost anything.

**On `slot_start_utc`:** local + `tz` is the source of truth; this is a computed
convenience column so lead-time math is a subtraction rather than a per-row tz
conversion in SQL. It is regenerable by `backfill-derived`. Flagging it because
it's the one place I'm storing the same fact twice — say the word and I'll drop
it and convert in Python instead.

---

## Derived

Materialized by `tracker backfill-derived`, fully regenerable, never hand-edited.

### `slot_state_transitions`

One row per observed state change for a slot: `slot_uuid`, `venue_uuid`,
`facility_uuid`, `from_state`, `to_state`, `first_seen_at` (UTC — the snapshot
where the new state first appeared), `prev_seen_at`, `slot_start_utc`,
`days_ahead_at_change`.

Derived with a window function over `slot_observations` ordered by `observed_at`.
Captures all three transitions you named:

- `OPEN → BOOKED` — the booking event; lead time = `slot_start_utc - first_seen_at`
- `BOOKED → OPEN` — cancellation
- `OPEN → BLOCKED` — venue pulled inventory (likely an offline sale)

Because the true change happened *somewhere inside* the 30-min poll gap, every
transition also carries `uncertainty_minutes` = `first_seen_at - prev_seen_at`.
Lead-time percentiles are reported with that resolution stated on the chart.

### `slot_first_booked` (convenience)

`slot_uuid` → first `OPEN → BOOKED` timestamp, `lead_time_hours`, plus flags for
the two cases that must not be silently averaged in:

- `censored_left` — the slot was already `BOOKED` in the very first snapshot that
  ever saw it, so the booking predates our data. Excluded from lead-time stats.
- `rebooked` — booked, cancelled, booked again. First and last both retained.

**`sport` exists because a venue is not a court.** `run_collect` polls every active
court, and Play Padel and Padel Fort each publish pickleball courts alongside their
padel one. Keyed on `venue_uuid` alone, Padel Fort's three courts are summed against
Padel Up's one: its share of listed court-hours inflates by half again and its padel
occupancy is blended with pickleball. It is the same normalization failure as counting
slots instead of court-minutes, on a different axis. The column is written at collect
time from config, because a dataset that does not carry it cannot be split afterwards.

### Views (cheap, always-live)

- `v_slot_settled` — **one row per slot: the state it settled in.** The last
  observation taken *before* the slot started, falling back to the earliest
  sighting for a slot that was already elapsed the first time we saw it. This is
  the identical rule `tracker.analytics.occupancy.settled_observations` applies in
  Python, and the two must stay identical: `MAX(snapshot_id)` is a *different*
  rule, because Hudle keeps republishing a slot after it elapses, so a late
  cancellation or a grid republish rewrites the last row without changing what the
  slot settled as. Every daily aggregate reduces through this view first.
- `v_court_minutes_daily` — per venue × court × sport × business date × state:
  summed `duration_minutes`. **The normalization layer.** Every cross-venue chart
  reads this, never raw slot counts. It also carries `price_per_court_hour`:
  `slot_price_total / slots` is a per-slot price and ranks the venues backwards.
- `v_occupancy_daily` — `occupancy_strict = booked / (booked + open)`,
  `occupancy_gross = (booked + blocked) / total`, plus `blocked_share`, all in
  court-minutes, with the raw numerator/denominator columns kept so every chart
  can print its own denominator. Grouped by `sport` as well as venue and court.
- `v_coverage_daily` — snapshots expected vs. received per facility per day.
  Charts read this to draw gaps rather than interpolating across them.

---

## Open questions for you

1. **`slot_start_utc`** — store it, or convert in Python every time? (above)
2. **`facility_fetches`** — I added it. Confirm you want per-facility fetch
   granularity rather than one `ok` per poll.
3. **Observation volume.** 3 courts × 21 days × ~36 slots/day ≈ 2.3k rows/poll ×
   48 polls/day ≈ **110k rows/day, ~40M/year** in SQLite. Workable but not free.
   Three options — my recommendation is (b):
   - (a) store every observation forever, unchanged
   - (b) **store every observation, and after N days compress unchanged runs**:
     collapse consecutive identical observations of the same slot into one row
     with `first_seen_at`/`last_seen_at`. Loses nothing analytically — all state
     changes and their timing survive — and cuts volume ~20×
   - (c) keep full fidelity only for `days_ahead <= 7`, thin beyond that
4. **Pickleball courts** — now in config: 1 at Play Padel, 2 at Padel Fort
   (`Court 1`, `Court 2`). `grid_minutes` unprobed. Collecting them takes the
   cycle from 3 to 6 requests. Confirm you want them from day one.

5. **Padel Up reads 0% and always will.** 31 days, 589 slots, **zero bookings
   ever observed**, at the highest price of the three (₹1800/court-hour). Its
   only blocked slot is 05:00, every day — a venue rule, not demand. Either
   Padel Up takes no Hudle bookings at all (listing as shopfront, sales by
   phone) or there is genuinely no demand at that price. A 0% line on the
   occupancy chart will read as a broken collector. Options: (a) plot it anyway
   with an explicit "no bookings observed since <date>" annotation,
   (b) badge it as `offline-only (suspected)` and exclude from market-share
   denominators until a first booking is seen. I lean (a) — it's the honest
   reading, and if a booking ever appears the annotation self-clears.

6. **Venue name history.** "Play Padel | Clarks Amer Hotel" is already
   "Play Padel | Pickleball | Clarks Amer Hotel" one day after your brief. I
   propose a `venue_name_history` append row on change, since a rename is itself
   a market signal (this one leads with pickleball). Cheap. Want it?
