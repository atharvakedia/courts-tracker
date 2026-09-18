/**
 * Filter state: the business-date window, the sport and the venue.
 *
 * The windows look *back* by default. A slot can only be observed before it
 * is played, so the forward book is mostly unbooked at any moment and an
 * occupancy figure over "the next 30 days" reads as near-zero demand when it
 * is really a measure of how far ahead people book. Settled days -- business
 * dates that have fully elapsed -- are the ones whose occupancy is final.
 * "Forward book" is offered separately for exactly that question: how much
 * of the coming weeks is already sold.
 *
 * Dates are *business* dates: Play Padel's 00:30 Saturday slot is Friday
 * night's session and the API has already rolled it back.
 */

import { dayLabelFull } from './format.js';
import { initSegmented } from './ui.js';

const TZ = 'Asia/Kolkata';

const listeners = new Set();

const state = {
  window: '7',
  sport: 'padel',
  venue: '',
};

/** Today where the courts are, not where the browser is. */
export function todayInCourtTime() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

function shift(iso, days) {
  const [y, m, d] = iso.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export const WINDOWS = {
  7: { short: 'Last 7 days', tiny: '7d', label: 'the last 7 settled business dates', back: 7, ahead: 0 },
  30: { short: 'Last 30 days', tiny: '30d', label: 'the last 30 settled business dates', back: 30, ahead: 0 },
  ahead: { short: 'Forward book', tiny: 'Ahead', label: 'today and the next 20 business dates', back: 0, ahead: 21 },
  all: { short: 'Everything', tiny: 'All', label: 'every business date observed', back: null, ahead: null },
};

/** The query parameters the API expects, derived from the current state. */
export function currentFilters() {
  const w = WINDOWS[state.window] || WINDOWS.all;
  const base = { sport: state.sport, venue: state.venue || null };
  if (w.back === null) return { start: null, end: null, ...base };
  const today = todayInCourtTime();
  if (w.ahead > 0) return { start: today, end: shift(today, w.ahead - 1), ...base };
  // Settled days only: yesterday back, never today, whose evening is still open.
  return { start: shift(today, -w.back), end: shift(today, -1), ...base };
}

export function describeWindow() {
  const filters = currentFilters();
  if (!filters.start) return 'Every business date on record, past and forward.';
  return `${dayLabelFull(filters.start)} to ${dayLabelFull(filters.end)}, by business date.`;
}

export function getState() {
  return { ...state };
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Tell the page the filters moved.
 *
 * The URL and the controls update on the keystroke; the refetch waits a beat.
 * Several of these endpoints re-read every observation in the window, so a
 * reader stepping 7 -> 30 -> everything would otherwise start three full scans
 * and watch the first two finish into a page that has moved on.
 */
const REFETCH_DELAY_MS = 170;
let pending = 0;

function announce() {
  writeUrl();
  clearTimeout(pending);
  pending = setTimeout(() => {
    for (const fn of listeners) fn(currentFilters(), getState());
  }, REFETCH_DELAY_MS);
}

function writeUrl() {
  const url = new URL(window.location.href);
  const set = (key, value, fallback) => {
    if (value && value !== fallback) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  };
  set('window', state.window, 'all');
  set('sport', state.sport, 'padel');
  set('venue', state.venue, '');
  window.history.replaceState(null, '', url);
}

function readUrl() {
  const params = new URLSearchParams(window.location.search);
  const w = params.get('window');
  if (w && WINDOWS[w]) state.window = w;
  const sport = params.get('sport');
  if (sport === 'padel' || sport === 'pickleball' || sport === 'all') state.sport = sport;
  const venue = params.get('venue');
  if (venue) state.venue = venue;
}

/** Fill the venue picker once /api/venues answers. It is frozen config. */
export function populateVenues(venues) {
  const select = document.getElementById('venue-select');
  if (!select) return;
  const chosen = state.venue;
  select.length = 1;
  for (const venue of venues) {
    const option = document.createElement('option');
    option.value = venue.venue_uuid;
    option.textContent = venue.short_name || venue.name;
    select.append(option);
  }
  if (chosen && [...select.options].some((o) => o.value === chosen)) {
    select.value = chosen;
  } else if (chosen) {
    state.venue = '';
    select.value = '';
  }
}

export function initFilters() {
  readUrl();

  const group = document.getElementById('window-group');
  group.innerHTML = Object.entries(WINDOWS)
    .map(
      ([key, w]) =>
        `<button class="segmented__btn" type="button" data-window="${key}" aria-pressed="false" title="${w.label}"><span class="w-long">${w.short}</span><span class="w-tiny">${w.tiny}</span></button>`
    )
    .join('');
  const syncWindow = () => {
    for (const btn of group.querySelectorAll('[data-window]')) {
      btn.setAttribute('aria-pressed', String(btn.dataset.window === state.window));
    }
  };
  group.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-window]');
    if (!btn || btn.dataset.window === state.window) return;
    state.window = btn.dataset.window;
    syncWindow();
    announce();
  });
  syncWindow();
  initSegmented(group.parentElement || document);

  const sport = document.getElementById('sport-select');
  sport.value = state.sport;
  sport.addEventListener('change', () => {
    state.sport = sport.value;
    announce();
  });

  const venue = document.getElementById('venue-select');
  venue.addEventListener('change', () => {
    state.venue = venue.value;
    announce();
  });

  writeUrl();
}
