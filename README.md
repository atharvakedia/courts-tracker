# Courts tracker

Tracks how much court time is booked at padel and pickleball venues in Jaipur that sell
on [Hudle](https://hudle.in), and shows it on a dashboard.

- **Padel:** Play Padel and Padel Fort, named in `config.yaml`. Padel Up is also in there
  but inactive: Hudle has never shown a booking for it.
- **Pickleball:** every Jaipur pickleball venue on Hudle, plus the pickleball courts at
  the two padel venues. The venue list is refreshed from Hudle's search once a week.

## What counts

- **Booked or vacant.** A slot is booked if it was sold on Hudle (`is_booked`) *or* the
  venue made it unavailable (`is_available` false). Venues block slots to record sales
  made off Hudle, so a blocked slot counts as sold time. The raw flags are stored next
  to that verdict.
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
  - *Listing only:* less than 2% of court time booked. The court is shown but left out
    of every figure, because its 0% says nothing about demand.
  - *Low activity:* bookings on less than 30% of days. The court is counted and badged.

## How it runs

```
GitHub Actions  ──daily 23:00 IST──▶  Neon Postgres  ◀──  Vercel: /api/* function
python -m tracker daily                                    + static dashboard (public/)
```

- **Daily pass** (`.github/workflows/daily.yml`): at 23:00 IST, `python -m tracker daily`
  reads each tracked court's grid from yesterday to 14 days ahead and writes to the
  database. On Sundays (Jaipur) it runs with `--discover` first, which refreshes the
  pickleball venues and courts. It can also be started by hand, with or without
  discovery.
- **Store** (`tracker/store.py`): one row per slot, rewritten only when it changes, plus a
  `runs` row for every pass. The same code runs on SQLite and Postgres.
- **Dashboard**: `public/` is served as static files; `api/index.py` serves the FastAPI app
  in `tracker/web/api.py` for `/api/overview` and `/api/health`.
- **Keep-alive** (`.github/workflows/keepalive.yml`): on the 1st of each month it
  re-enables the daily workflow, so GitHub does not disable it after 60 days without
  repository activity.

### Secrets and environment

| Variable           | Needed by                        |
| ------------------ | -------------------------------- |
| `DATABASE_URL`     | the daily pass, Vercel, `serve`  |
| `HUDLE_API_SECRET` | the daily pass, `discover`       |
| `HUDLE_APP_ID`     | the daily pass, `discover`       |

They are GitHub Actions secrets and (for `DATABASE_URL`) a Vercel environment variable.
Locally they go in `.env`, which is untracked; the Makefile loads it. No credential is
ever committed: `config.yaml` refers to the Hudle ones as `${HUDLE_API_SECRET}` and
`${HUDLE_APP_ID}`, and loading fails by name if either is unset.

## Being gentle with Hudle

- Requests are sequential, with at least 15 s between any two (`poll.request_gap_seconds`),
  enforced inside the client.
- A failing call gets at most 3 attempts, with backoff starting at 60 s.
- After 5 failed requests in a row the circuit breaker opens and the pass stops.
- Hudle's gateway sometimes returns a 5xx for a long date range; that range is fetched
  again in three smaller pieces rather than resent whole.
- A venue that refuses past dates (HTTP 403) is re-read from today.
- Read-only: nothing is booked, held or changed.

## Dashboard

- Padel or Pickleball; a 7-day, 30-day or All window (All is the last 90 settled days).
  Each view compares against the window before it.
- Venues as a list or a map. The map shows supply (courts) or demand (hours booked).
  Selecting a venue narrows every chart to it.
- By day, by hour of day, a weekday-by-hour busy-hours grid, and the spread of
  occupancy across court-days.
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
`smoke_test.py` is the script that recorded those fixtures.
