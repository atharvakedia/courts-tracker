# padel-tracker

A local-first court-occupancy tracker for the three padel venues in Jaipur that list on
[Hudle](https://hudle.in). It polls Hudle's public slot-grid endpoint every 30 minutes,
appends what it saw to SQLite, derives occupancy / lead-time / pricing metrics from that
stream, and serves a dashboard over it.

**The dataset is forward-looking only.** Hudle publishes what a slot looks like *right
now*; it has no history endpoint. Nothing here can be backfilled, so a poll that did not
happen is a hole in the record permanently. That single fact drives most of the design
decisions below — the append-only table, the two denominators, the coverage view, and the
refusal to retry aggressively when the API says no.

---

## The honest position on Hudle's terms of service

Hudle's terms prohibit automated scripts against their site. This project makes requests
anyway, at a deliberately personal scale, and it is worth being straight about that rather
than burying it:

- **One request at a time.** No concurrency anywhere — not across venues, not across
  facilities. The collector walks the configured courts sequentially.
- **A hard global gap between any two requests**, enforced inside the client rather than by
  the caller, so no code path can skip it. See `poll.request_gap_seconds`.
- **About six requests per 30-minute cycle** — one per configured court. That is roughly
  twelve requests an hour, which is less traffic than a single person refreshing the
  booking page while deciding when to play.
- **No amplifying retries.** A failure retries at most `poll.backoff.max_attempts` times
  with exponential backoff, and a 429 is treated as an instruction, not an obstacle. The
  cycle gives up on that court and moves on; the next cycle tries again in 30 minutes.
- **A circuit breaker.** After `poll.max_consecutive_failures` consecutive failures the
  client stops issuing requests entirely rather than continuing to knock. `--loop` shuts
  itself down instead of running all night against an API that is refusing it.
- **Read-only.** Nothing here books, holds, cancels or modifies anything. It reads the same
  public slot grid the venue's own booking page reads, with no login and no cookies.

If you run this, run it at this scale. Turning the cadence down to a minute, adding
concurrency, or removing the request gap converts a personal analytics project into
something that costs a third party real money, and none of the metrics get better for it —
the slot grid does not change fast enough to reward it.

**It rate-limits in practice.** Hudle returns `429 Too Many Requests` for a run of
back-to-back grid requests even at a polite gap; see
[Troubleshooting](#a-429-on-the-last-court-of-every-cycle). The defaults in `config.yaml`
are tuned to stay under that observed limit.

---

## Setup from a clean clone

Requires Python 3.11+ and nothing else — no Docker, no Postgres, no build step for the
dashboard.

```bash
git clone <this repo> padel-tracker
cd padel-tracker

make install            # creates .venv and installs the project with dev extras
cp config.example.yaml config.yaml    # already present in this repo; edit if needed

make check              # ruff --fix + ruff format + mypy + pytest, all from .venv
```

`make check` runs entirely on the committed fixtures in `fixtures/raw/` and makes **no
network calls**. If it passes, the collector, the analytics and the dashboard all work
against real recorded Hudle responses.

Then take your first real observation:

```bash
.venv/bin/python -m tracker collect --dry-run   # fetches and classifies, writes nothing
.venv/bin/python -m tracker collect             # fetches and writes one snapshot
.venv/bin/python -m tracker serve               # dashboard on http://127.0.0.1:8000
```

`smoke_test.py` at the repo root is the one-off script that originally probed the API and
recorded everything in `fixtures/raw/`. It is kept as provenance for the fixtures, not as
part of the running system; the collector never calls it.

---

## The CLI

Everything runs as `python -m tracker <command>`. All four commands accept `--config PATH`
(default: `$PADEL_TRACKER_CONFIG`, else `./config.yaml`), `--log-level`, and
`--log-format {json,console}` — JSON lines by default, because the scheduled runs land in a
log file that is easier to grep structurally than to read.

| Command | What it does |
| --- | --- |
| `collect` | Polls every active court once and appends one snapshot. This is what the scheduler runs. |
| `collect --dry-run` | Fetches and classifies, prints per-facility state counts and court-minutes, and touches no storage method. |
| `collect --loop` | Stays alive and polls on `poll.cadence_minutes` instead of exiting, for an environment with no cron. Stops itself on an open circuit or `poll.max_consecutive_failures` consecutive bad cycles. |
| `discover` | Re-checks the venue and facility sets against Hudle, prints what drifted, and appends what it saw to `facility_discovery_log`. **Never edits `config.yaml`.** |
| `serve` | Runs the dashboard (FastAPI + vanilla HTML + ECharts from CDN). `--host`, `--port`, `--reload`. |
| `backfill-derived` | Recomputes `slot_state_transitions` and `slot_first_booked` from the observation stream. Safe to re-run; the observations are the source of truth. |

Exit codes are meaningful, so a cron wrapper can tell the cases apart without parsing logs:

| Code | Meaning |
| --- | --- |
| `0` | Clean run. |
| `1` | Partial — some court failed, or `discover` found drift worth a human's attention. |
| `2` | Stopped — bad configuration, an open circuit, or the API refusing outright. Nothing was collected. |

---

## Installing the schedule

### macOS (launchd) — the supported path

`deploy/com.atharva.padel-tracker.plist` is a ready launchd agent. It runs
`python -m tracker collect` every 1800 seconds from the project's own venv, with
`WorkingDirectory` set to the repo and both stdout and stderr appended to
`data/collect.log`.

```bash
cp deploy/com.atharva.padel-tracker.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.atharva.padel-tracker.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.atharva.padel-tracker.plist
launchctl list | grep padel          # confirm it is registered
```

`launchctl list` prints `PID  LastExitStatus  Label`. A `-` for PID is correct between
runs — `collect` is a one-shot command, not a daemon. The second column is the exit code of
the last run, and it is the fastest health check you have: `0` clean, `1` partial, `2`
stopped.

To fire one cycle immediately rather than waiting for the interval:

```bash
launchctl start com.atharva.padel-tracker
tail -f /Users/atharvakedia/Dev/padel-tracker/data/collect.log
```

**To reverse the installation**, in full:

```bash
launchctl unload ~/Library/LaunchAgents/com.atharva.padel-tracker.plist
rm ~/Library/LaunchAgents/com.atharva.padel-tracker.plist
```

`unload` alone stops it until the next login; removing the file makes it permanent.

Three things about the plist are deliberate and worth not "fixing":

- **`RunAtLoad` is `false`.** Getting paths right takes a few load/unload cycles, and each
  load would otherwise fire a real request at Hudle. Use `launchctl start` when you want
  one on purpose.
- **There is no `KeepAlive`.** `collect` exits by design. Restarting it on exit turns a
  30-minute cadence into an unbounded request loop against someone else's API.
- **`StartInterval` and `poll.cadence_minutes` are not wired together.** The config value is
  what coverage is measured against; the plist value is what actually fires. If you change
  the cadence, change both — otherwise the dashboard reports coverage against a schedule
  nobody is running. `StartInterval` is in **seconds**: 30 minutes is `1800`.

### cron — the alternative

Same idea, fewer guarantees (cron will not catch up a run missed while the machine slept,
and gives you no exit-status history):

```cron
*/30 * * * * cd /Users/atharvakedia/Dev/padel-tracker && /Users/atharvakedia/Dev/padel-tracker/.venv/bin/python -m tracker collect >> /Users/atharvakedia/Dev/padel-tracker/data/collect.log 2>&1
```

Absolute paths on both the interpreter and the log are not optional: cron's `PATH` does not
include the venv, and its working directory is not the repo.

---

## Adding a fourth venue

The venue tree in `config.yaml` is frozen, human-reviewed source of truth. `discover`
proposes; a person disposes. Nothing auto-adopts, and the reason is specific: a facility
adopted with an unprobed `grid_minutes` writes observations whose `duration_minutes` nobody
verified, into a table that is append-only and cannot be re-collected. A wrong grid is
permanent.

1. **Run discovery.**

   ```bash
   .venv/bin/python -m tracker discover
   ```

   It searches the configured sports in Jaipur, paginates to exhaustion, fetches each
   venue's Next.js SSR page for its facility list, and prints what it found against what is
   configured. Exit code `1` means there is drift worth reading.

2. **Read the suggested config block.** Discovery prints a paste-ready YAML block for
   anything unconfigured. Treat it as a draft, not an answer.

3. **Freeze the court / equipment classification by hand.** This is the step that matters.
   `kind:` is mirrored from config into the `facilities` table and is never inferred at
   write time. The name hints in `discovery.equipment_name_hints` are only a suggestion and
   they have been wrong: "Pickleball Court (Outdoor)" contains the token `ball` and was
   suggested as equipment on the first run. Rackets, racquets and balls are `kind:
   equipment` with `active: false` — they are rentals, they are not courts, and collecting
   them would put non-court rows into every occupancy denominator. Hints and overrides
   match on whole words, so `ball` does not fire inside "Pickleball", and a multi-word
   entry like `padel ball` is matched as a phrase across the space.

4. **Probe the grid and the price before you activate the court.** Set
   `grid_minutes:` and `price_per_court_hour:` only from a real response you have looked
   at, and remember the price is **per court-hour**, not per slot — a venue selling
   30-minute slots at ₹1000 is ₹2000/hour. Leave both `null` and `active: false` until
   probed; an unprobed court is better absent than wrong. (The three pickleball courts in
   `config.yaml` are in exactly that state.)

5. **Paste the block into `config.yaml`** under `venues:`, keyed by UUID. Venue matching is
   by `uuid` only — `name` is a display label. Hudle renames venues: "Play Padel | Clarks
   Amer Hotel" is now "Play Padel | Pickleball | Clarks Amer Hotel" and the UUID never
   moved.

6. **Re-run `discover` and confirm no drift.** A clean second run exiting `0` is the
   confirmation that what you pasted matches what Hudle is serving. If it still reports
   drift, the config and the API disagree and the config is wrong.

7. **Dry-run before the first real collect.**

   ```bash
   .venv/bin/python -m tracker collect --dry-run
   ```

   Check the new court's `grid=` and `slots=` in the output against what you configured.
   This is the last moment a wrong grid is free to fix.

`discover` also appends every run to `facility_discovery_log`, so "when did that facility
appear?" is answerable after the fact rather than only at alert time. Re-run it about
weekly (`discovery.drift_check_days`).

---

## The data model, and why it is append-only

Full column-level detail is in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md). The shape:

| Table | Rows | Role |
| --- | --- | --- |
| `venues`, `facilities` | dimensions | Mirrored from `config.yaml`. `kind` is frozen here, never inferred. |
| `facility_discovery_log` | append-only | What each SSR page said, so drift is auditable. |
| `snapshots` | one per collect run | `poll_key` is the idempotency key. |
| `facility_fetches` | one per (snapshot, court) | Every HTTP attempt, including the failures — so a per-venue gap is honest rather than invisible. |
| `slot_observations` | one per (snapshot, slot) | **The only irreplaceable table.** Append-only: never updated, never deleted. |
| `slot_state_transitions`, `slot_first_booked` | derived | Reconstructible at any time via `backfill-derived`. |

**Why append-only.** Hudle tells us what a slot looks like now and nothing about when it
changed. A booking *time* can only be inferred from the first poll in which the state
flipped — which is possible solely because every poll is kept. Overwrite a slot's row with
its current state and you have a `SELECT` that is marginally simpler and a dataset that can
no longer answer a single interesting question: no lead times, no cancellations, no price
history, no blocked-inventory events. And because none of it can be refetched, that loss is
one-way.

It follows that **every transition is an interval, not an instant**. The change happened
somewhere between the last poll that saw the old state and the first that saw the new one.
Every `StateTransition` carries `uncertainty_minutes` for that reason, and a missed poll
widens it — a transition seen across a 90-minute hole is a 90-minute window, not a
confident 30-minute one. Any chart that drops `uncertainty_minutes` is claiming precision
the data does not have.

Re-running a cycle is safe: `snapshots.poll_key` is the idempotency key, so a second
`collect` inside the same cadence window converges on the same rows instead of duplicating
them. Retry the command freely.

---

## The two occupancy denominators

Every occupancy figure is published twice, and both are load-bearing:

```
occupancy_strict = booked / (booked + open)          <- the headline
occupancy_gross  = (booked + blocked) / total        <- share unavailable to a walk-up
```

They differ because a slot has three states, not two:

| State | Rule | Means |
| --- | --- | --- |
| `BOOKED` | `is_booked == true` | Somebody paid. `is_booked` wins over `is_available`. |
| `BLOCKED` | `not is_available and not is_booked` | Not sellable: venue closed it, maintenance, or a phone booking taken offline. |
| `OPEN` | `is_available and not is_booked` | Bookable. |

`occupancy_strict` answers "of the court time that was actually for sale, how much sold?"
`occupancy_gross` answers "if I walked up, how much of the day could I not have?" A venue
that blocks its evenings to sell them by phone scores near zero on the first and high on
the second, and **neither number alone is honest about it**. So the blocked count is always
surfaced beside them, never folded into either.

This is not hypothetical. Padel Fort's 2026-09-13 was BLOCKED for all fourteen slots from
17:00 to 23:30 with zero bookings — an entire prime evening withdrawn from inventory.
Folded into occupancy it reads as an empty venue. Reported on its own it reads as a
session that left Hudle. `blocked_inventory` in `tracker/analytics/market.py` reports
contiguous run lengths for exactly this reason: a scattered blocked slot is maintenance, a
contiguous blocked evening is a decision.

**Pastness is not a state.** Hudle never marks an elapsed slot unavailable — verified at
16:21 IST on 2026-09-11, Padel Fort's 07:00 slot that same morning still returned
`is_available: true, is_booked: false`. So `is_past` is computed by us as its own boolean
column (`slot_start_utc < snapshot.observed_at`) and is orthogonal to the state enum.
Retrospective day-occupancy is unaffected — an elapsed unsold slot genuinely was sellable
inventory that went unsold, so it belongs in the denominator. But any "still winnable",
current-availability or time-to-sellout view must filter on `is_past` itself.

---

## Court-minutes: why slot counts are never comparable

**Never compare raw slot counts across venues.** Slot duration differs by venue:

| Venue | Grid | Slots/day | Per slot | **Per court-hour** |
| --- | --- | --- | --- | --- |
| Padel Up | 60 min | 19 | ₹1800 | **₹1800** |
| Play Padel | 30 min | 40 | ₹1000 | **₹2000** |
| Padel Fort | 30 min | 36 | ₹900 | **₹1800** |

One Padel Up slot is two Padel Fort slots. "Padel Fort sold 6 slots, Padel Up sold 0" is
not a comparison of anything — it is a comparison of grid sizes. Everything is therefore
normalized to **court-minutes** when stored and **court-hours** when displayed, before any
cross-venue number is computed.

The same trap runs through price, and there it does not merely blur the answer — it inverts
it. Play Padel's ₹1000 is the lowest per-slot price of the three and the *highest* per
court-hour. `price_per_court_hour()` is the only correct comparison, and `price_rank()`
prints both columns so the trap stays visible on the page. Note also that
`price_onwards` from the venue-search endpoint is the venue-wide minimum across all sports
and is useless for padel: Padel Fort reports 450, which is its pickleball price, while its
padel court is ₹900/30min.

**Sport is the second normalization axis.** Two of the three venues list pickleball courts
alongside padel, so every cross-venue entry point takes `sport=`. Omitting it compares a
three-court venue against a one-court venue and blends two sports into one headline.

### Business date

Play Padel sells 00:00–01:30, and Hudle stamps those slots with the calendar date they fall
on — so Friday-night demand lands on Saturday. Verified: 2026-09-12, a Saturday, carries
bookings at 00:30 and 01:00 that are Friday-night sessions.

Every observation therefore also carries `business_date`: the local date, with slots
starting before `business_day_start_hour` (4) rolled back one day. **All day-of-week and
daily aggregation uses `business_date`.** The wall-clock columns are never mutated — a
00:30 slot stays in hour 0 on the hour-of-day chart; only its *day* attribution moves.

---

## Metrics

[`docs/METRICS.md`](docs/METRICS.md) is the reference: every metric with its denominator,
its date range, its caveats and why it earns its place — plus a table of the metrics
deliberately **not** built and the fact each would have required that this API does not
expose. That second table is part of the deliverable, not an omission.

One finding to know before reading any chart:

> **Padel Up shows 0% occupancy, and the collector is fine.** It published 589 slots across
> 31 days at the highest rate of the three (₹1800/court-hour) and recorded zero bookings.
> Its only blocked slot is 05:00, every single day — a standing venue rule, not demand. The
> likeliest reading is that Padel Up does not transact on Hudle at all (the listing is a
> shopfront; sales happen by phone). Its occupancy line will read 0% indefinitely. It is
> flagged `no_bookings_ever_observed` and stays in every chart, because dropping it would be
> its own distortion — its third of the city's listed court-time is a real fact about the
> market.

---

## Troubleshooting

### A stale collector

Symptoms: the dashboard's coverage view shows gaps, or charts stop at a date in the past.

```bash
launchctl list | grep padel                        # registered? what was the last exit code?
tail -50 data/collect.log                          # what did the last run actually say?
sqlite3 data/padel.db \
  "select max(observed_at), count(*) from snapshots;"
```

Read it in that order:

- **Not in `launchctl list`** → the agent is not loaded. Re-run the load command above.
- **Loaded, second column `0`, but no recent snapshot** → it is running and succeeding but
  the machine was asleep. launchd does not backfill missed `StartInterval` runs, and
  neither can we; the gap is permanent. It will show up honestly in the coverage view,
  which is the point of having one.
- **Second column `1`** → partial. Some court failed while others succeeded. Normal and
  self-healing if occasional; see the 429 section if it is every cycle.
- **Second column `2`** → the run stopped and collected nothing. Bad config or an open
  circuit. The log line says which.
- **A traceback in the log** → deliberate. Expected failures (bad config, a refusing API, a
  changed SSR page) are reported as a log event plus an exit code. An unexpected exception
  is left to propagate, because a collector that swallows a bug it does not understand is
  worse than one that stops.

Gaps are visible on purpose: `facility_fetches` records failed attempts too, so a court
that went missing for six hours shows as six failed fetches rather than as six hours that
quietly never happened.

### A 429 on the last court of every cycle

Symptoms: five courts succeed, the sixth returns `429 Too Many Requests`, all three retry
attempts also return 429, and the cycle exits `1`.

This is Hudle's burst limit, not a bug, and it is reproducible: six back-to-back grid
requests (each ~340 KB) trip it at the sixth. The fix is patience, never pressure —
increase `poll.request_gap_seconds` so the cycle spreads over a longer window, and increase
`poll.backoff.initial_seconds` so a retry lands after the window has actually reset rather
than inside it. Both are already tuned in `config.yaml` for the six configured courts; add
courts and you will need to widen the gap again.

What **not** to do: raise `backoff.max_attempts`. A 429 is an instruction. Retrying it
harder is the behaviour the rate limit exists to stop, and it is the difference between a
personal-scale project and a nuisance. The next cycle is thirty minutes away; the slot grid
will not have changed meaningfully.

### An open circuit breaker

Symptoms: `collect_run_done ... circuit_open=true`, exit code `2`, and no requests being
made at all.

The client stops issuing requests after `poll.max_consecutive_failures` consecutive
failures. That is the intended behaviour: the API is refusing us, and more requests are not
the fix. Under `--loop`, the loop shuts itself down rather than running all night.

To clear it, find out what is refusing before you restart anything:

```bash
grep -E "hudle_request_failed|circuit" data/collect.log | tail -20
```

- **All 429** → see above; widen the gap, then start a single cycle by hand and watch it.
- **All 401/403** → the API contract moved. `Api-Secret`, `x-app-id` or the User-Agent in
  `config.yaml` under `http.headers` is no longer accepted.
- **Connection errors** → your network, not theirs.

The breaker is per process, so it resets when the process exits. A scheduled cycle 30
minutes later starts with a closed circuit automatically — there is nothing to reset by
hand, and nothing to "force". If you must test the fix immediately, one
`launchctl start com.atharva.padel-tracker` is a single gentle cycle.

### Detected drift

Symptoms: `discover` exits `1` and prints a drift report.

Drift comes in several kinds and they are not equally urgent:

- **A venue renamed.** Cosmetic. Matching is by UUID, so collection is unaffected. Update
  `name:` when convenient; the old name is kept in `venue_name_history`.
- **A new facility at a configured venue.** The common case. Follow
  [Adding a fourth venue](#adding-a-fourth-venue) from step 2 — classify `kind:` by hand,
  probe the grid and price, then activate.
- **A new *venue* in the padel search.** This is the one worth acting on promptly:
  `discovery.alert_on_venue_set_change: padel` exists because the padel market is three
  venues and a fourth is news. Unconfigured **pickleball** venues are the normal state of a
  57-venue market and exit `0` on purpose.
- **A configured facility that has disappeared.** Do not delete it from `config.yaml` and do
  not delete its observations. Set `active: false`. The history stays valid and stays
  queryable; deleting the dimension row orphans rows that can never be re-collected.
- **The SSR page could not be parsed.** Exit `2`, and the only kind that stops the command.
  Facility UUIDs exist nowhere in the JSON API — they come solely from
  `props.pageProps.venueDetails.activities[].facilities[]` inside the `__NEXT_DATA__`
  script tag. If Hudle changes that page's shape, `tracker/discover.py` needs updating.
  Collection of already-configured courts is unaffected in the meantime, because it runs off
  the UUIDs in `config.yaml`, not off discovery.

`discover` never edits `config.yaml`, so a drift report is never itself destructive. It is
safe to run any time you want to know.
