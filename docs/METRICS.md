# Metrics

What every module under `tracker/analytics/` computes, what each number is divided by, over
what date range, what it cannot tell you, and — just as importantly — the metrics this
dataset cannot honestly support, which is why they are not here.

Six pure modules, each with its own section below:

| Module | Answers |
| --- | --- |
| `occupancy.py` | How much court time sold, in court-minutes, under both denominators. |
| `leadtime.py` | How far ahead it sold, and how long a slot took to go. |
| `transitions.py` | What changed, when we could first tell, and how wide that window is. |
| `pricing.py` | What it costs per court-hour, and every change to that. |
| `market.py` | How the three venues compare, and where the comparison is unsafe. |
| `coverage.py` | Whether we were actually watching — the denominator under all of the above. |

### The date ranges these figures come from

Two different corpora, and conflating them would misread every number:

- **The recorded fixtures** (`fixtures/raw/`) are a *single* poll of a 31-day grid taken at
  16:21 IST on 2026-09-11: 2945 padel slots over business dates 2026-09-10 to 2026-10-11,
  32 trading days. They contain no state changes at all, because a single snapshot cannot
  contain one. Every occupancy, pricing and market figure quoted below is from these.
- **A scripted synthetic history** (`tests/conftest.py`, `SyntheticHistory`) is ten
  snapshots on a 30-minute cadence with one deliberate 90-minute gap, carrying a normal
  booking, two left-censored slots, a cancellation, a re-booking, an `OPEN → BLOCKED`
  transition and a blocked evening. Everything in `transitions.py`, `leadtime.py` and
  `coverage.py` is verified against it, because those metrics are about *change over time*
  and the fixtures have none.

That second point is the honest status of the lead-time and transition metrics today: the
arithmetic is verified, the real-world sample is not yet collected. The collector began on
2026-09-11 and nothing before it can be backfilled.

The pricing and market reports also declare themselves in code: each carries a `metrics`
tuple of `MetricSpec(name, title, unit, denominator, date_range, caveats)`, and
`market.metric_registry(*reports)` collects them for a page. The web layer renders the
denominator and the caveats **onto** the chart from those specs rather than hardcoding a
caption, because a caption in a template outlives the arithmetic it describes.

## The traps every number here has to survive

**Slot counts are never comparable.** Padel Up sells 60-minute slots; Play Padel and Padel
Fort sell 30-minute ones. One Padel Up slot is two Padel Fort slots. Everything below is
court-minutes (stored) or court-hours (displayed).

**Per-slot prices invert the ranking.** Play Padel's ₹1000 per slot looks cheaper than
Padel Up's ₹1800. Per court-hour Play Padel charges ₹2000 against ₹1800 for both others —
it is the most expensive padel court in the city. `price_per_court_hour()` is the only
correct comparison and `price_rank()` prints both columns so the trap is visible.

**Observations are per-poll, not per-slot.** A 21-day horizon polled every 30 minutes sees
each slot about 48 times. Summing court-minutes off a raw observation stream inflates every
figure ~48×. `settled_observations()` reduces each slot to the state it settled in — the
last observation taken before the slot started — and every aggregate here calls it first;
it is idempotent, so passing already-reduced input is safe.

**There is exactly one definition of "the row for this slot", and it is
`settled_observations()`.** The `v_slot_settled` SQL view implements the same rule, so a
chart reads the same number whether it came from SQL or from Python. Taking the newest
poll instead is a different rule, not a simpler spelling of the same one: Hudle keeps
republishing a slot after it has elapsed, so a late cancellation or a grid republish
rewrites its last observation without changing what it settled as, and the two rules then
print 0% and 100% occupancy for the same slot.

**A venue is not a court, and sport is the second normalization axis.** Play Padel and
Padel Fort publish pickleball courts beside their padel one, so every cross-venue entry
point takes `sport=`. Omitting it compares a three-court venue against a one-court venue
and blends two sports into one occupancy headline.

---

## Occupancy — occupancy.py

### `occupancy_by_venue_day` — the headline

**Measures.** Settled court-minutes by state for one venue on one business date, with every
ratio derived from them reachable on the same object.

**Two denominators, and both are always published.**

```
occupancy_strict = booked / (booked + open)          <- the headline
occupancy_gross  = (booked + blocked) / total        <- share unavailable to a walk-up
blocked_share    = blocked / total                   <- the gap between them
```

`occupancy_strict` answers "of the court time that was actually for sale, how much sold?"
`occupancy_gross` answers "if I walked up, how much of this day could I not have had?"
A venue that withdraws its evenings to sell them by phone scores near zero on the first and
high on the second, and **neither number alone is honest about it**. The blocked count is
surfaced beside both and is never folded into either.

**Date range.** Whatever business dates the passed observations cover. Over the fixtures:
2026-09-10 to 2026-10-11, 32 trading days.

**Today, padel only, across the whole fixture window:** 13.5 booked court-hours, 1713.5
open, 40.0 blocked — **0.78% strict, 3.03% gross**. Per venue:

| Venue | Booked | Open | Blocked | Strict | Gross |
| --- | --- | --- | --- | --- | --- |
| Play Padel | 10.5 h | 609.5 h | 0.0 h | 1.69% | 1.69% |
| Padel Fort | 3.0 h | 546.0 h | 9.0 h | 0.55% | 2.15% |
| Padel Up | 0.0 h | 558.0 h | 31.0 h | **0.00%** | 5.26% |

**Caveats, all load-bearing.**

- **The window is mostly future inventory.** A 31-day forward grid polled once is largely
  slots that have not had time to sell. These percentages are a floor on final occupancy,
  not a measurement of it, and they will rise as the window's early dates settle. Do not
  read 0.78% as "padel in Jaipur is empty".
- **Court-minutes, never slot counts.** Summed from each slot's own `duration_minutes`.
  `to_court_hours()` is the only supported conversion.
- **One observation per slot.** See `settled_observations` below; a raw multi-snapshot sum
  inflates everything by the poll count.
- **`sport=` is required** on every cross-venue entry point. Two of the three venues publish
  pickleball courts beside their padel one.
- **Zero denominators return `None`, never `0.0`.** A day with no sellable minutes sold none
  of nothing, which is a different fact from selling none of a real inventory. Padel Up's
  genuine 0.00% — 558 open court-hours and no bookings — must stay distinguishable from it.

**Why it earns its place.** It is the question the project exists to answer, and the single
denominator version of it is misleading in a way that is invisible: Padel Fort's 0.55% and
2.15% describe the same court-time and disagree by a factor of four.

### `settled_observations` — the reduction every other metric depends on

**Measures.** Each slot reduced to the one observation that represents it: the last poll
taken **before the slot started**, which is the state the slot settled in.

**Why it is not "the newest row".** Hudle keeps republishing a slot after it has elapsed, so
a late cancellation or a grid republish rewrites a slot's most recent observation without
changing what it actually settled as. `MAX(snapshot_id)` and "last poll before start" are
two different rules, and they print 0% and 100% occupancy for the same slot.

**There is exactly one definition of it.** `pricing.py` and `market.py` import this function
rather than defining their own, and the `v_slot_settled` SQL view implements the identical
rule, so a chart reads the same number whether it came from SQL or from Python. It is
idempotent — passing already-reduced input is safe.

**Caveat.** A slot never observed before it started has no settled state under this rule and
is excluded. That is correct: we did not watch it sell.

### `peak_hour_heatmap` / `heatmaps_by_venue` / `weekday_vs_weekend`

**Measures.** Court-minutes by state per (weekday × hour-of-day) cell, and the
weekday/weekend split of the same.

**Denominator.** Each cell carries its own `OccupancyTotals`, so a cell's percentage prints
with the court-minutes it was computed from. A cell over four sellable minutes is not the
same claim as one over four hundred.

**Weekday comes from `business_date`, hour comes from wall-clock.** Play Padel's 00:30
Saturday slots are Friday-night sessions: they stay in hour 0 (wall-clock is never
rewritten) but are attributed to Friday. Getting this backwards files a venue's best night
under the wrong day.

**Caveat.** A 21-day horizon contains three of some weekdays and two of others, so raw
weekday totals rank days partly by how many the window happened to contain. Per-trading-day
averages exist for this reason; see `demand_heatmap`.

---

## Lead time — leadtime.py

Consumes the transition history from `transitions.py`. **Verified against the synthetic
history, not yet against a real one** — see the date-range note above.

### `first_booked` / `lead_time_distribution`

**Measures.** Hours between the moment a booking was first observed and the slot's start,
as a median / P90 distribution, split peak vs off-peak on `dashboard.peak_hours`.

**Denominator.** Bookings whose booking event we actually saw. Every `LeadTimeStats` carries
`n`, the censored count it excluded, and the post-start count it contains.

**Left censoring is the whole problem.** A slot already BOOKED in the first snapshot that
ever saw it was sold before our data starts. Its lead time is not zero and not short — it is
**unknown**. Counting those as "booked the moment we first looked" drags every median and
P90 down, and does so hardest at the busiest venues, which are precisely the ones with
bookings already on the board when collection began. Censored slots get
`lead_time_hours=None`, are excluded from every statistic, and the exclusion count travels
with every statistic computed without them.

Today **all 15 booked slots in the database are left-censored**, because there is one
snapshot. Lead time has no sample yet and says so rather than reporting zero.

**Post-start bookings are real and are kept.** Hudle never marks an elapsed slot
unavailable, so a walk-up paying through the app after play began produces a booking
observed *after* `slot_start_utc` and a negative raw lead time. Those are clamped to zero
and flagged rather than dropped — a dropped row is an invisible bias, and walk-ins are
genuine demand.

**Why it earns its place.** "How far ahead do people book?" is the one operational question
a venue can act on directly, and it is unanswerable from any single snapshot. It is the
clearest payoff of keeping every poll.

### `time_to_sellout` / `first_slot_to_go`

**Measures.** How long a slot stayed open before it went, and which slot on a given day sold
first.

**Denominator.** Only slots observed for at least `DEFAULT_MIN_OBSERVATION_HOURS` (24)
before their start.

**Survivorship, stated on the result.** A slot we started watching three hours before it
began cannot show a three-day sellout no matter how early it really sold. Including it
biases every sellout figure low, so those slots are excluded and the minimum observation
window is reported **on the result object** rather than left implicit in a docstring.

**Caveat.** Both metrics need a slot's full open→booked life inside the observation window.
Until the collector has been running longer than the booking horizon, they are
systematically biased toward short-lead bookings, and that bias shrinks over time rather
than being correctable.

---

## State changes — transitions.py

### `derive_transitions`

**Measures.** The moments each slot changed state, ordered by the snapshot's `observed_at`
rather than by `snapshot_id` — a catch-up poll written out of id order still lands in the
right place.

**Every transition is an interval, never an instant.** The real change happened somewhere
between the last poll that saw the old state and the first that saw the new one. Every
`StateTransition` carries `uncertainty_minutes`, **and a missed poll widens it**: a
transition seen across a 90-minute hole is a 90-minute window, not a confident 30-minute
one. Any presentation that drops `uncertainty_minutes` claims precision the data does not
have.

**First sightings are transitions too**, emitted with `from_state=None` and
`uncertainty_minutes=None`, because we cannot say what a slot was before we looked. That row
is load-bearing twice: it is how `leadtime.py` detects a left-censored booking, and it is how
a block pre-dating our data — Padel Fort's whole 2026-09-13 evening, BLOCKED in every
snapshot we ever took — is still reported instead of vanishing for want of an
`OPEN → BLOCKED` edge.

**Today:** 3927 transitions in the database, of which 3927 are first sightings and 0 are
state changes. That is the correct output for a one-snapshot dataset, not a bug.

### `cancellation_rate`

**Measures.** `BOOKED → OPEN` events per venue per ISO week.

**Two denominators, both carried, because they answer different questions.** `bookings`
counts observed booking *events*, so a slot booked, released and re-booked contributes two.
`booked_slots` counts distinct slots ever seen BOOKED, including `censored_slots` whose
booking event we never saw. Dividing by the wrong one silently changes the metric.
`rate` is `None` — never `0.0` — when `bookings` is zero, so a week nobody booked is
distinguishable from a week nobody cancelled.

**Caveat: event counts are grid-dependent.** One customer booking and cancelling one hour is
two events at a 30-minute grid and one at a 60-minute grid. The *rate* survives that (both
sides scale together) but a bar chart of `bookings` or `cancellations` across venues does
not — so `booked_court_minutes` and `cancelled_court_minutes` travel beside them and are the
only cross-venue bars to draw.

### `blocked_inventory_events`

**Measures.** Contiguous runs of court time withdrawn from sale, merged when they share a
facility, abut in time, and were first seen blocked in the same poll — the signature of one
decision rather than a coincidence.

**Why runs rather than a blocked count.** Padel Fort blocked 17:00–23:30 on 2026-09-13: 14
consecutive slots, 420 court-minutes. That is *one* decision by one venue, and reporting it
as 14 events makes a single evening look like a fortnight of churn.

**`censored_left`** means every slot in the run was already BLOCKED when we first saw it, so
the block pre-dates our data and `uncertainty_minutes` is unknowable rather than merely wide.

**Caveat.** We cannot distinguish maintenance from a closure from an offline booking. Run
length is the only available signal and it is suggestive, not conclusive.

---

## Coverage — coverage.py

### `coverage_report` / `coverage_by_facility_day` / `coverage_gaps`

**Measures.** Snapshots actually taken per facility per day against snapshots expected, and
the explicit list of gaps.

**Denominator.** `expected_snapshots_per_day(cadence_minutes)` — 48 at the 30-minute
cadence. This is threaded from `poll.cadence_minutes` into storage at construction rather
than defaulted, because defaulting it would quietly report yesterday's coverage against the
wrong denominator after a cadence change.

**Per facility, not per run.** `facility_fetches` records failed attempts too, so a court
that 429'd for six hours shows as failed fetches rather than as six hours that silently
never happened. A run where five courts succeeded and one failed is not "a successful run".

**`split_on_gaps` exists so nothing interpolates across a hole.** A gap is not a low value to
be smoothed over; it is an absence of measurement, and a line drawn through it is a
fabrication.

**Why it earns its place, and why it is not optional.** This is the denominator under every
other metric in this document. The dataset is forward-only: a poll that did not happen is a
permanent hole, and the only honest response is to show it. A dashboard that renders
occupancy without rendering coverage lets a broken collector look like an empty venue —
which, for Padel Up, is exactly the misreading that matters most.

**Caveat.** Coverage measures whether *we* were watching. It says nothing about whether
Hudle's data was correct while we watched.

---

## Pricing and market — pricing.py, market.py

Figures below come from the recorded fixtures (2945 slots, business dates 2026-09-10 to
2026-10-11, 32 trading days) and are reproduced by `tests/test_analytics_market.py`.

### `price_per_court_hour` / `price_rank` — pricing.py

**Measures.** Published rate normalized to 60 minutes, per court.
**Denominator.** The slot's own `duration_minutes`, never a config grid — config can be
stale or unprobed, the row cannot.
**Today.** Play Padel ₹2000/hr, Padel Fort ₹1800/hr, Padel Up ₹1800/hr.
`slot_price_ranking_inverts` is `True`.
**Caveats.** Published rack rate only: discounts, packages, memberships and offline rates
are invisible. This is a listed price, never a transacted one.
**Why it earns its place.** It is the one comparison the whole dashboard rests on, and the
naive version of it is not approximately right — it is exactly backwards.

### `price_timeline` — pricing.py

**Measures.** Each court's modal rate per court-hour across every poll, plus every change.
**Denominator.** Modal published rate per court, per poll.
**Change timestamps are intervals.** We never see the moment a price changes; we see the
last poll with the old price and the first poll with the new one. Every `PriceChange`
carries `prev_seen_at`, `first_seen_at` and `uncertainty_minutes`, and the uncertainty is
the *actual* gap between those two polls — 90 minutes across a collector gap, not the
nominal 30-minute cadence.
**Ordering.** Points are ordered on `observed_at`, not `snapshot_id`, so a catch-up poll
written out of id order still lands in the right place.
**Why it earns its place.** Nothing can be backfilled. If a venue raises its price and we
did not record the before-state, that fact is gone permanently. This is the metric that
makes the forward-only collection worth running.

### `price_by_hour_table` — pricing.py

**Measures.** Rate per hour-of-day per court, with `is_flat` per court and per venue and a
single `has_any_variation` flag.
**Denominator.** Modal published rate per court per starting hour.
**Today the answer is "there is nothing to show".** All three venues are flat: one rate for
every hour of every day, verified across 2945 slots. `has_any_variation` is `False` and
`.summary` reads "No venue currently varies price by hour of day: all 3 observed courts
publish a single flat rate."
**Why it earns its place despite having no finding.** A chart that draws three identical
flat lines invites the reader to infer a pattern nobody has observed. Reporting the absence
explicitly is the honest output, and the cells are still returned so the chart works the day
someone introduces peak pricing. Hours 02:00–04:00 are absent because nobody sells then.

### `revenue_proxy` — market.py

**Measures.** Booked court-hours priced at the published rate, per venue per ISO week, with
blocked court-hours and their rack-rate value on the same row.
**Denominator.** Booked court-hours × published rate per court-hour, bucketed by ISO week of
`business_date`.
**Today.** Play Padel ₹21,000; Padel Fort ₹5,400; Padel Up ₹0 — across five ISO weeks.
**Caveats, and they are load-bearing.** This is a *proxy*. `PROXY_LABEL` travels on the
report and in the metric spec, and `is_proxy` is a field. It sees only what sold through
Hudle: phone bookings, walk-ins, memberships and corporate blocks are invisible. Blocked
court-hours sit on the same row because they are the most likely home of those sales.
`blocked_revenue_if_sold` is a ceiling, not an estimate, and it is inflated by standing
venue rules — Padel Up's ₹55,800 figure is its 05:00 daily block, an hour a day nobody was
ever going to buy. Read it against `blocked_inventory` run lengths, never alone.
**Why it earns its place.** A court-hour figure alone does not tell you which venue's hours
are worth more. Normalized price makes revenue comparable; the blocked column keeps the
blind spot the same size on the page as it is in reality.

### `market_share` — market.py

**Measures.** Share of observed booked court-hours per venue per ISO week, **and** share of
observed listed court-hours, each with its absolute court-hours attached.
**Denominators.** Demand: total booked court-hours across all observed venues that week, and
`None` — never `0.0` — when nobody booked anything. Supply: total listed court-hours
(booked + open + blocked) that week.
**Today.** Demand: Play Padel 77.8%, Padel Fort 22.2%, Padel Up 0.0%. Supply: Play Padel
35.1%, Padel Fort 31.6%, **Padel Up 33.3%**.
**The honesty requirement.** Padel Up published 589 slots over 31 days at the highest rate of
the three and recorded zero bookings. A bare demand pie gives the other two 100% of "the
market" and reads as a verdict on Padel Up's business. It is not one — it is a statement
about our instrument. The likelier explanation is that Padel Up does not take bookings
through Hudle at all (a listing as shopfront, sales by phone). So every venue carries a
`VenueDataQuality` with `DataQualityFlag`s and a finished annotation sentence:
`no_bookings_ever_observed`, `no_supply_observed`, `heavily_blocked_inventory` (≥20% of
listed court-time withdrawn). Flagged venues stay in every chart — dropping one would be its
own distortion.
**Why supply share is here.** It is a denominator that remains meaningful for a venue with no
observed demand, and Padel Up's third of the city's listed court-time is a real fact about
the market that the demand chart alone erases.

### `demand_by_hour` — market.py

**Measures.** Booked, open and blocked court-minutes by local starting hour, per venue.
**Denominator.** Court-minutes of slots starting in each local hour, summed over every
`business_date` in the window; each row carries its own `business_dates` and `day_count`, so
a chart prints "over N trading days" instead of implying one.
**Business date, not local date.** Play Padel sells 00:00–01:30 and Hudle stamps those slots
with the calendar date they fall on, so a 00:30 Saturday booking is a Friday-night session.
The row stays in hour 0 — wall-clock is never rewritten — but its `business_dates` says
Friday.
**Why it earns its place.** It is the basis of the peak-pricing read, and it is the metric
where the business-date rule is most visible.

### `demand_heatmap` — market.py *(added by judgment)*

**Measures.** Booked court-minutes per weekday × hour cell, per venue, with a per-trading-day
average alongside the total.
**Denominator.** Court-minutes in each cell ÷ the number of `business_date`s of that weekday
in the window.
**Why the per-day average.** A 21-day window contains three Fridays and two Mondays. A raw
total ranks weekdays by how many of each the window happened to contain, which is an artefact
of the horizon, not a fact about demand.
**Why it earns its place.** Hour-of-day alone cannot separate "Friday evenings sell out" from
"evenings sell out". Weekday is the second axis of the only actionable pattern this dataset
can show, and the weekday comes from `business_date`, so Friday-night sales are not filed
under Saturday.

### `blocked_inventory` — market.py *(added by judgment)*

**Measures.** Blocked court-hours per court per trading day, the longest **contiguous** blocked
run in court-minutes, and `whole_session_withdrawn` (a run ≥ 240 minutes on a day with zero
observed bookings).
**Denominator.** Blocked court-hours ÷ listed court-hours for that court and business date;
`None` when nothing was listed.
**Today.** One withdrawn session in the fixtures: Padel Fort 2026-09-13, all 14 slots from
17:00 to 23:30, 420 contiguous court-minutes, zero bookings, 100% of that day's blocked share.
Padel Up's 31 rows are each one isolated 60-minute block at 05:00 and are correctly **not**
withdrawn sessions.
**Why run length rather than a blocked count.** A scattered blocked slot is maintenance; a
contiguous evening with no bookings is a venue selling or closing that session outside Hudle.
Averaging the two into one "blocked share" destroys the distinction. Runs are chained on UTC
start-equals-previous-end, so they work across a 60-minute grid, a 30-minute grid and midnight
alike.
**Caveat.** We cannot distinguish maintenance, a closure and an offline booking. Run length is
the only signal available and it is suggestive, not conclusive.
**Why it earns its place.** This is the single most misleading thing in the dataset if hidden.
Folded into occupancy, 2026-09-13 reads as an empty venue; reported on its own it reads as a
prime evening that left Hudle.

### `peak_pricing_opportunity` — market.py *(added by judgment)*

**Measures.** Strict occupancy inside and outside the configured peak hours, per venue, against
whether that venue's price is flat.
**Denominator.** Booked court-hours ÷ (booked + open) court-hours, computed separately for
slots starting in `dashboard.peak_hours` and outside them. Blocked time is excluded from both
sides, so a venue that withdraws its evenings does not appear to have sold them.
**`is_candidate`** requires *both* a flat price and a peak−off-peak gap ≥ 0.15, so a venue that
already prices its peak is never flagged, and neither is a venue with a flat demand curve.
**Today: no candidates.** Play Padel 2.3% peak vs 1.5% off-peak, Padel Fort 2.0% vs 0.0%, Padel
Up 0.0% vs 0.0%. Every venue is flat-priced but none has a large enough gap yet — and the
window is mostly future inventory that has not had time to sell.
**Caveat, stated in the spec.** A question, not a forecast. This dataset holds no price
elasticity, no turned-away demand and no competitor response, so it cannot estimate what a
price change would earn. `occupancy_gap` is `None`, never `0.0`, when either side had no
sellable inventory at all.
**Why it earns its place.** It is the one decision the venue data can genuinely inform, and it
is exactly where an over-confident metric would do the most damage — so the flag is
deliberately conservative and the caveat is part of the return value.

---

## Metrics deliberately NOT built, and why

Skipping these is part of the deliverable. Each would require a fact this API does not expose,
and shipping a plausible-looking version of any of them would be worse than shipping nothing.

| Not built | What it would need | Why the data cannot support it |
| --- | --- | --- |
| **Actual revenue** | Transacted prices, discounts, memberships, offline sales | We see a published rack rate on a slot, not money. Padel Fort's blocked evening may be ₹12,600 of offline revenue or ₹0 of maintenance, and nothing in the payload distinguishes them. `revenue_proxy` is labelled a proxy in its type, its field, its label and its caveats precisely so it is never promoted. |
| **Repeat-booking / retention / cohort analysis** | Customer identity on a booking | The slot grid carries no customer field of any kind. Every booked slot is anonymous and indistinguishable from every other. There is no join key, so there is no cohort. |
| **No-show and cancellation-at-the-door rates** | Attendance, distinct from booking | We see a slot flip BOOKED→OPEN, which is a release before the slot elapsed. Whether anyone turned up for a slot that stayed BOOKED is simply not in the data. |
| **Average booking value / party size / duration preference** | Booking-level rows | Observation is per *slot*, not per booking. Two adjacent booked slots may be one two-hour booking or two independent ones, and nothing distinguishes the cases. Reporting "average booking length" would be reporting the grid size. |
| **Price elasticity / revenue impact of a price change** | Demand at ≥2 price points, plus a control | All three venues are flat-priced and have been for the whole observation window. There is no price variation to regress against, and with three venues in one city there is no control either. `peak_pricing_opportunity` deliberately stops at "worth asking about". |
| **Conversion / funnel (views → bookings)** | Traffic or search-impression data | We poll the slot grid. We have no visibility into how many people looked at a venue and did not book, so every occupancy figure has an unknown denominator upstream of it. |
| **Total market size for padel in Jaipur** | Every venue, every channel | We see three Hudle-listed venues. Courts that do not list on Hudle are invisible, and Padel Up suggests even a listed venue may not transact there. `market_share` is explicitly *share of observed* court-hours and says so in both metric specs. |
| **Occupancy forecasts / "will this slot sell"** | A training history | The dataset is forward-only and began on 2026-09-11. A forecast fitted on weeks of data across three venues would be a confident restatement of the sample. Revisit when there is a year of it; not before. |
| **Pickleball anything** | A probed grid and price | The three pickleball courts in `config.yaml` load with `grid_minutes: null` and `price_per_court_hour: null` because nobody has verified them. Court-minutes computed from an unverified grid would be permanently wrong in an append-only store, and they cannot be recollected. |
| **"Padel Up is failing"** | A reason for zero bookings | The honest reading of 589 slots and zero bookings is "this venue does not appear to transact on Hudle". `no_bookings_ever_observed` says exactly that and no more. Anything stronger is a narrative the data does not carry. |
