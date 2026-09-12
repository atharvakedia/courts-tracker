/**
 * Composition root for the dashboard.
 *
 * Panels load progressively and independently. Several metric endpoints have
 * to re-read every observation in the window to answer, which on a database a
 * few days old is seconds rather than milliseconds, so nothing here waits for
 * the slowest endpoint before showing the fastest: each panel replaces its own
 * skeleton the moment its own request lands, and a failure costs one panel
 * rather than the page. Requests run a few at a time so sixteen simultaneous
 * full scans do not queue behind each other.
 */

import { filterParams, get, isCurrent, newGeneration } from './api.js';
import { esc, int, minutes as fmtMinutes, sentence, stamp } from './format.js';
import {
  currentFilters,
  describeWindow,
  initFilters,
  populateVenues,
  subscribe,
} from './filters.js';
import { initTheme } from './theme.js';
import { failPanel, markStale, skeletonPanel } from './panels.js';
import { indexFacilities, indexVenues } from './ui.js';
import {
  setLeadTime,
  renderFirstSlot,
  renderHeatmap,
  renderHero,
  renderKpis,
  renderOccupancy,
  renderWeekday,
} from './views-demand.js';
import { renderLeadTime, renderSellout } from './views-timing.js';
import {
  renderPriceByHour,
  renderPricing,
  renderRevenue,
  renderShare,
} from './views-market.js';
import {
  renderBlocked,
  renderCancellations,
  renderCatalog,
  renderCoverage,
  renderVenues,
} from './views-record.js';

/** How many metric requests may be in flight at once. */
const CONCURRENCY = 3;

const TITLES = {
  'panel-occupancy': 'Occupancy by day',
  'panel-heatmap': 'Peak hours',
  'panel-weekday': 'Weekday against weekend',
  'panel-firstslot': 'The first slot to go',
  'panel-leadtime': 'How far ahead people book',
  'panel-sellout': 'How long a peak slot lasts',
  'panel-pricing': 'Price per court-hour',
  'panel-byhour': 'Does price move with the hour?',
  'panel-share': 'Share of the three',
  'panel-revenue': 'Revenue proxy',
  'panel-blocked': 'Inventory withdrawn from sale',
  'panel-cancellations': 'Bookings that went away again',
  'panel-coverage': 'What the collector actually caught',
  'panel-venues': 'The venues and their courts',
  'panel-catalog': 'Every metric and what it divides by',
};

const el = (id) => document.getElementById(id);

let cadence = 30;

/* --------------------------------------------------------------------------
   Health and the banners it drives
   -------------------------------------------------------------------------- */

function renderHealth(payload) {
  const pulse = el('pulse');
  const text = el('pulse-text');
  if (!payload.ok || !payload.data) {
    pulse.dataset.state = 'unknown';
    text.textContent = 'Collector status unavailable';
    return null;
  }
  const health = payload.data;
  cadence = health.cadence_minutes || 30;
  pulse.dataset.state = health.status;

  if (health.status === 'no_data') {
    text.textContent = 'No polls recorded yet';
  } else if (health.last_snapshot) {
    text.textContent = `Last poll ${fmtMinutes(health.staleness_minutes)} ago`;
    pulse.title = `${stamp(health.last_snapshot.observed_at)} · next expected ${stamp(
      health.expected_next_at
    )}`;
  }
  return health;
}

function renderBanners(health, coverage) {
  const host = el('banners');
  const banners = [];

  if (health && health.status === 'open_circuit') {
    banners.push({
      tone: 'alert',
      html: `<b>The collector has stopped.</b> ${esc(sentence(health.reason))} Nothing on this page
        will move until it is running again, and every slot that elapses meanwhile is lost for
        good — this dataset cannot be backfilled.`,
    });
  } else if (health && health.stale) {
    banners.push({
      tone: 'alert',
      html: `<b>This data is stale.</b> ${esc(sentence(health.reason))} The last poll landed
        ${esc(stamp(health.last_snapshot.observed_at))}; the next was due
        ${esc(stamp(health.expected_next_at))}.`,
    });
  }

  if (coverage && coverage.has_gaps && (coverage.poll_gaps || []).length) {
    const gaps = coverage.poll_gaps;
    banners.push({
      tone: 'warn',
      html: `<b>${esc(int(coverage.missed_polls))} poll${
        coverage.missed_polls === 1 ? ' is' : 's are'
      } missing from the record.</b> ${esc(
        gaps.length === 1 ? 'One gap' : `${int(gaps.length)} gaps`
      )}, the longest ${esc(
        fmtMinutes(Math.max(...gaps.map((g) => g.minutes)))
      )} long. Every line on this page is broken across them rather than drawn through them, and
      they are listed in full under <i>What the collector actually caught</i>.`,
    });
  }

  host.innerHTML = banners
    .map(
      (b) =>
        `<div class="banner" data-tone="${b.tone}"><span class="banner__mark" aria-hidden="true"></span><span>${b.html}</span></div>`
    )
    .join('');
}

/* --------------------------------------------------------------------------
   First run: no snapshots at all. This is the state the dashboard is opened
   in on day one, so it gets a screen of its own rather than fifteen empties.
   -------------------------------------------------------------------------- */

function renderFirstRun(health) {
  el('hero-skeleton').hidden = true;
  const content = el('hero-content');
  content.hidden = false;
  el('hero').classList.add('firstrun');

  const polls = health ? health.snapshots_last_24h : 0;
  content.innerHTML = `
    <p class="firstrun__figure">Nothing has been collected yet.</p>
    <p class="firstrun__body">This dashboard reads an append-only log of court availability at the
      three padel venues in Jaipur listed on Hudle. The log only ever looks forward: a slot can be
      observed before it is played and never afterwards, so the record starts the moment the
      collector first runs, and no part of it can be filled in later.</p>
    <div class="steps">
      <div class="steps__item"><span class="steps__n">1</span>
        <span>Take the first poll. It writes one snapshot covering every court for the next three
        weeks, and occupancy becomes readable immediately.</span></div>
      <div class="steps__item"><span class="steps__n">2</span>
        <span>Leave it running on the ${esc(int(cadence))}-minute cadence. Booking lead time,
        time-to-sellout and cancellations each need at least two polls of the same slot, because
        each of them measures a <em>change</em> rather than a state.</span></div>
      <div class="steps__item"><span class="steps__n">3</span>
        <span>Come back in a week. Day-of-week patterns need one; until then the peak-hour grid
        greys out any cell it has not seen enough court-time in rather than claiming a rate for
        it.</span></div>
    </div>
    <code class="cmd">python -m tracker collect --loop</code>
    <p class="firstrun__body">${
      polls
        ? `${esc(int(polls))} poll${polls === 1 ? '' : 's'} in the last 24 hours.`
        : 'No poll has been recorded in the last 24 hours.'
    } Every panel below says what it is still waiting for.</p>`;
}

/* --------------------------------------------------------------------------
   The load
   -------------------------------------------------------------------------- */

/** What the hero's denominator is scoped to: one venue, or all three. */
function scopeLabel(venues) {
  const chosen = currentFilters().venue;
  if (!chosen) return 'the three venues';
  const match =
    venues.ok && venues.data
      ? (venues.data.venues || []).find((v) => v.venue_uuid === chosen)
      : null;
  return match ? (match.short_name || match.name) : 'this venue';
}

/** Run tasks a few at a time, in order, calling `onDone` as each lands. */
async function pool(tasks, limit) {
  let next = 0;
  const workers = new Array(Math.min(limit, tasks.length)).fill(0).map(async () => {
    while (next < tasks.length) {
      const task = tasks[next];
      next += 1;
      // eslint-disable-next-line no-await-in-loop
      await task();
    }
  });
  await Promise.all(workers);
}

async function load({ first = false } = {}) {
  const gen = newGeneration();
  const params = filterParams(currentFilters());

  el('filters-note').textContent = describeWindow();
  setLeadTime(undefined, cadence);

  if (first) {
    for (const [id, title] of Object.entries(TITLES)) skeletonPanel(el(id), title);
  }

  const draw = (id, fn, payload, extra) => {
    if (!isCurrent(gen)) return;
    try {
      fn(el(id), payload, extra);
    } catch (err) {
      failPanel(el(id), TITLES[id] || 'Panel', String((err && err.message) || err));
    }
  };

  // Fast and foundational: health drives the banner, venues fixes the colours
  // and fills the picker, coverage supplies the gaps every time series is
  // broken across, the catalog is static text.
  const [health, coverage, catalog, venues] = await Promise.all([
    get('health'),
    get('coverage', params),
    get('metrics/catalog', params),
    get('venues', params),
  ]);
  if (!isCurrent(gen)) return;

  const healthData = renderHealth(health);
  renderBanners(healthData, coverage.ok ? coverage.data : null);

  if (venues.ok && venues.data) {
    indexVenues(venues.data.venues || []);
    indexFacilities(venues.data.venues || []);
    populateVenues(venues.data.venues || []);
  }

  const gaps = coverage.ok && coverage.data ? coverage.data.poll_gaps || [] : [];
  const noData = healthData && healthData.status === 'no_data';

  draw('panel-coverage', renderCoverage, coverage);
  draw('panel-catalog', renderCatalog, catalog);
  draw('panel-venues', renderVenues, venues);

  if (noData) {
    renderFirstRun(healthData);
    setLeadTime(null, cadence);
    renderKpis({ totals: null, venues: venues.data, health: healthData });
  } else {
    el('hero').classList.remove('firstrun');
  }

  const flaggedVenues =
    venues.ok && venues.data
      ? (venues.data.venues || [])
          .filter((x) => x.data_quality && (x.data_quality.flags || []).includes('no_bookings_ever_observed'))
          .map((x) => x.short_name || x.name)
      : [];
  const ctx = { cadence, gaps, flaggedVenues };

  const step = (path, render, id, extra) => async () => {
    const payload = await get(path, params);
    if (!isCurrent(gen)) return;
    draw(id, render, payload, extra || ctx);
    return payload;
  };

  // The hero and the KPI row come from the weekday/weekend split, which is the
  // one response carrying window totals; it is fetched first so the top of the
  // page fills before the heavier panels arrive.
  const totalsTask = async () => {
    const payload = await get('metrics/weekday-weekend', params);
    if (!isCurrent(gen)) return;
    if (!noData) {
      const summary = renderHero(payload, {
        windowText: describeWindow(),
        scopeLabel: scopeLabel(venues),
      });
      renderKpis({
        totals: summary,
        venues: venues.ok ? venues.data : null,
        health: healthData,
      });
    }
    draw('panel-weekday', renderWeekday, payload);
  };

  const leadTimeTask = async () => {
    const payload = await get('leadtime', params);
    if (!isCurrent(gen)) return;
    draw('panel-leadtime', renderLeadTime, payload, { cadence });
    if (!noData) setLeadTime(payload.ok ? payload.data : false, cadence);
  };

  await pool(
    [
      totalsTask,
      step('occupancy/daily', renderOccupancy, 'panel-occupancy'),
      step('occupancy/heatmap', renderHeatmap, 'panel-heatmap'),
      leadTimeTask,
      step('pricing/timeline', renderPricing, 'panel-pricing', { gaps, cadence }),
      step('pricing/by-hour', renderPriceByHour, 'panel-byhour'),
      step('market/share', renderShare, 'panel-share'),
      step('market/revenue-proxy', renderRevenue, 'panel-revenue'),
      step('metrics/sellout', renderSellout, 'panel-sellout', { cadence }),
      step('metrics/first-slot', renderFirstSlot, 'panel-firstslot'),
      step('metrics/blocked-events', renderBlocked, 'panel-blocked'),
      step('metrics/cancellations', renderCancellations, 'panel-cancellations'),
    ],
    CONCURRENCY
  );

  if (!isCurrent(gen)) return;
  const generated = healthData && healthData.generated_at;
  el('colophon-generated').textContent = generated ? `Read at ${stamp(generated)}.` : '';
}

/* -------------------------------------------------------------------------- */

function boot() {
  initTheme();
  initFilters();
  load({ first: true });

  subscribe(() => {
    // A filter change is new data for the same page, not a new page. Panels
    // that already have figures keep showing them, dimmed and inert, until
    // their replacements land; only panels with nothing to hold on to go back
    // to a skeleton. Fifteen panels blinking to grey and back reads as a
    // reload, and is slower to follow than a cross-fade.
    for (const [id, title] of Object.entries(TITLES)) {
      if (!markStale(el(id))) skeletonPanel(el(id), title);
    }
    for (const id of ['hero-content', 'kpis']) {
      const node = el(id);
      if (node && node.childElementCount) node.classList.add('is-stale');
    }
    load();
  });

  // The masthead only draws its rule once the page has scrolled under it.
  const masthead = document.querySelector('.masthead');
  if (masthead) {
    let ticking = false;
    const sync = () => {
      ticking = false;
      masthead.dataset.scrolled = String(window.scrollY > 4);
    };
    window.addEventListener(
      'scroll',
      () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(sync);
      },
      { passive: true }
    );
    sync();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
