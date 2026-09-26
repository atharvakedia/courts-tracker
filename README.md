# Courts tracker

Tracks how much court time is booked at padel and pickleball venues in Jaipur that sell
on [Hudle](https://hudle.in), and shows it on a dashboard.

- **Padel:** Play Padel and Padel Fort, named in `config.yaml`. Padel Up is also in there
  but inactive: Hudle has never shown a booking for it.
- **Pickleball:** every Jaipur pickleball venue on Hudle, plus the pickleball courts at
  the two padel venues. The venue list is refreshed from Hudle's search once a week.

## What counts

- **Booked, vacant or blocked.** A slot is *booked* when a customer bought it on Hudle
  (`is_booked`) and *vacant* when it stayed on sale. A slot the venue made unavailable
  without a booking (`is_available` false) is *blocked*: it was never offered, so it is
  left out of both sides of % booked (booked ÷ court time offered) and reported on its
  own. A court blocked all day is not a full court. The raw flags are stored per slot.
  Hudle occasionally flags a slot booked while still showing a court free
  (`available_count` 1); it is counted as booked, as Hudle's `is_booked` says.
- **Hours marked as blocked.** Some venues take court time off sale by marking it booked
  (no court left available) rather than blocking it, which reads as a sale. On the
  dashboard, select a venue and open **Blocked hours** to tick the weekday × hour cells
  it really blocks (coaching, say), optionally between two dates. Those slots then count
  as venue blocks everywhere, whatever Hudle says (`tracker/blocks.py`). Saving or
  removing a block needs the shared `ADMIN_PASSWORD` and starts the `views` workflow,
  so the charts update within a few minutes.
- **Slots Hudle has not created yet are skipped.** Hudle sometimes shows a slot with no
  `id` (and no `created_at`), only on days not yet played. Without an id the slot has
  no stable identity, so it is left out (`tracker/slots.py`).
- **Booking time.** `booked_at` is Hudle's `updated_at` for a booked slot, read as
  Jaipur (IST) wall-clock. It is a last-modified stamp, so a cancel-and-rebook keeps
  only the final booking.
- **Settled days only.** Occupancy counts only business dates that have fully elapsed
  (yesterday and earlier, Jaipur time). A slot starting before 04:00 belongs to the
  previous evening's business date.
- **Court-hours.** Every figure is summed in court-minutes and shown as court-hours, so
  30-minute and 60-minute grids compare.
- **Reliability**, judged per court from its own settled days (`tracker/insights.py`):
  - *All blocked:* the venue blocked every slot, so nothing was offered and there is no
    occupancy to measure. Shown, left out of every figure.
  - *Listing only:* less than 2% of the court time offered was booked. The court is shown
    but left out of every figure, because its 0% says nothing about demand.
  - *Low activity:* bookings on less than 30% of days. The court is counted and badged.
  - *Not updating:* the latest daily pass did not read the court from Hudle: an error for
    that court alone (such as a 404 for a court the venue removed), or the pass stopped
    before reaching it. Its recent days were never read once settled, so it is left out
    of every figure and its venue is tagged red until a read succeeds.

## How it runs

```
GitHub Actions  ──daily 23:00 IST──▶  Neon Postgres  ◀──  Vercel: /api/* function
python -m tracker daily                                    + static dashboard (public/)
```

- **Daily pass** (`.github/workflows/daily.yml`): at 23:00 IST, `python -m tracker daily`
  reads each tracked court's grid from yesterday to 14 days ahead and writes to the
  database. On Sundays (Jaipur) it runs with `--discover` first, which refreshes the
  pickleball venues and courts. It then builds every dashboard view (see below). It can
  also be started by hand, with or without discovery. Each court's read is recorded on
  the court; a court whose last read was before the window (it was failing, or a pass
  stopped before it) is read from that day, up to 14 days back, so the days it missed
  are recorded once they are settled.
- **Store** (`tracker/store.py`): one row per slot, rewritten only when it changes, plus a
  `runs` row for every pass and a `blocks` row for every block marked on the dashboard.
  The same code runs on SQLite and Postgres.
- **Views** (`tracker/views.py`): the data changes once a day, so every answer the
  dashboard can ask for (each sport and window, for the whole market and for each venue)
  is built once at the end of the daily pass and stored whole in a `views` table.
- **Dashboard**: `public/` is served as static files; `api/index.py` serves the FastAPI app
  in `tracker/web/api.py`. `/api/overview` returns the stored view (one row read), or a
  503 while this version's views are not built yet; responses are cached at Vercel's
  edge. The page asks for each view with the build time `/api/health` reports, so a
  rebuild shows within minutes rather than after the edge cache expires. `/api/blocks`
  lists, adds and removes blocked hours. `/api/health` reports the last pass. The page
  keeps every view it has loaded and
  prefetches the other windows, the other sport and any venue under the pointer, so
  switching is instant. Live at https://courts-tracker.vercel.app.
- **Database transfer.** Neon meters what the database sends out each month (the free
  plan stops at 5 GB), and a views build needs three months of slots, about 100 MB. So
  nothing reads them wholesale. The daily and `views` workflows keep a mirror of the
  slots (`data/mirror.db`, SQLite, named by `SLOT_MIRROR`) in the GitHub Actions cache,
  and each build first pulls only the rows written since the last one (every slot row
  carries `written_at`). The mirror holds only the three months the views read, and reads
  them whole again if its row count disagrees with the database's. The views read only
  the columns they draw, and the API never computes a view. A build then reads a few MB.
- **Keep-alive** (`.github/workflows/keepalive.yml`): on the 1st of each month it
  re-enables the daily workflow, so GitHub does not disable it after 60 days without
  repository activity.

### Secrets and environment

| Variable           | Needed by                        |
| ------------------ | -------------------------------- |
| `DATABASE_URL`     | the daily pass, Vercel, `serve`  |
| `HUDLE_API_SECRET` | the daily pass, `discover`       |
| `HUDLE_APP_ID`     | the daily pass, `discover`       |
| `ADMIN_PASSWORD`   | Vercel: saving blocked hours     |
| `GITHUB_DISPATCH_TOKEN` | Vercel: rebuilding the views after a block change |

They are GitHub Actions secrets and (for `DATABASE_URL`, `ADMIN_PASSWORD` and
`GITHUB_DISPATCH_TOKEN`) Vercel environment variables. Without `ADMIN_PASSWORD` blocks
can be read but not changed. `GITHUB_DISPATCH_TOKEN` is a fine-grained token for this
repository alone with *Actions: read and write*; without it a block change rebuilds the
views inside the function, reading every slot from the database, which is fine locally
and not in production.
Locally they go in `.env`, which is untracked; the Makefile loads it. No credential is
ever committed: `config.yaml` refers to the Hudle ones as `${HUDLE_API_SECRET}` and
`${HUDLE_APP_ID}`, and loading fails by name if either is unset.

## Being gentle with Hudle

- Requests are sequential, with at least 15 s between any two (`poll.request_gap_seconds`),
  enforced inside the client.
- A failing call gets at most 3 attempts, with backoff starting at 60 s.
- After 5 failed requests in a row the circuit breaker opens and the pass stops. A 404 is
  an answer about one court, not a failing Hudle: it is not retried and not counted, so
  a removed court cannot stop the pass.
- Hudle's gateway sometimes returns a 5xx for a long date range; that range is fetched
  again in three smaller pieces rather than resent whole.
- A venue that refuses past dates (HTTP 403) is re-read from today.
- Read-only: nothing is booked, held or changed.

## Dashboard

- Padel or Pickleball; a 7-day, 30-day or All window (All is the last 90 settled days).
- A venue list; selecting a venue narrows every chart to it.
- By day, by hour of day (whose court-hours view also shows venue blocks, not counted
  as booked), and a weekday-by-hour busy-hours grid.
- With a venue selected, **Blocked hours**: mark the hours it takes off sale (see above).
- A map of Jaipur: supply (courts open to customers) or demand (hours booked), as a
  heat layer with a dot per venue; hover a dot for the venue, click it to filter.
- A freshness marker from `/api/health`, which reports when the daily pass last ran.

## Local development

Python 3.11. With `DATABASE_URL`, `HUDLE_API_SECRET` and `HUDLE_APP_ID` in `.env`:

```bash
make install          # .venv with the project and dev extras
make check            # ruff (fixing), ruff format, mypy, pytest
make ci-check         # the same, non-fixing, as CI would run it
make run              # python -m tracker serve --reload: API + public/ on 127.0.0.1:8000
make daily            # python -m tracker daily: one pass (real Hudle requests)
make discover         # python -m tracker discover: config drift report
```

`DATABASE_URL` can be a local SQLite file, for example `sqlite:///data/courts.db`
(`data/` is untracked; create it first). The tables are created on first use. `python -m tracker daily --discover` also refreshes the
pickleball venues. `python -m tracker discover` compares `config.yaml` with what Hudle
lists now and prints suggested changes; it never edits the file. It exits 1 when the
padel venue set, a configured venue's name or its facilities have changed.

Tests run on the recorded Hudle responses in `fixtures/raw/` and make no network calls.

## Changes and deploys

`main` is protected: code reaches it only through a pull request whose `ci` check
(`make ci-check`) passes. Vercel builds a preview deployment for every PR branch, on the
production database (read-only from the site's side); merging deploys production.
Stored views carry a version (`VIEWS_VERSION` in `tracker/views.py`): bump it when a
view's shape or meaning changes. A build replaces its own version's views, keeps the
version just before it and drops older ones, so a PR that bumps the version can build its
preview's views beside production's: run the `views` workflow on the PR branch
(`gh workflow run views.yml --ref <branch>`). Until then the preview says the charts are
not built yet. Do not run it on a branch that keeps production's version: that build
would replace production's views with the branch's. The run on merge builds production's,
and a block saved anywhere, a preview included, rebuilds from main.
`smoke_test.py` is the script that recorded those fixtures.
