/**
 * Presentation helpers shared by every panel renderer.
 *
 * Nothing here derives a metric. Colour assignment, dense date axes, table and
 * stat markup and the count-up tween are all about how a figure is shown, not
 * what it means; the meaning arrives precomputed from the API.
 */

import { esc, num, pct } from './format.js';
import { prefersReducedMotion } from './theme.js';

export const DAY_ORDER = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** Stable colour per venue, assigned by the order /api/venues returns them. */
const venueIndex = new Map();
/** Courts inherit their venue's colour, so coverage and occupancy agree. */
const facilityToVenue = new Map();

export function indexVenues(venues) {
  venueIndex.clear();
  venues.forEach((venue, i) => venueIndex.set(venue.venue_uuid, i));
}

export function indexFacilities(venues) {
  facilityToVenue.clear();
  for (const venue of venues) {
    for (const court of venue.courts || []) {
      facilityToVenue.set(court.facility_uuid, venue.venue_uuid);
    }
  }
}

export function colorIndex(uuid) {
  if (venueIndex.has(uuid)) return venueIndex.get(uuid);
  const owner = facilityToVenue.get(uuid);
  if (owner !== undefined && venueIndex.has(owner)) return venueIndex.get(owner);
  return 0;
}

export function venueColor(tokens, uuid) {
  return tokens.venue[colorIndex(uuid) % tokens.venue.length];
}

/** All business dates between the first and last row, so holidays show as holes. */
export function denseDates(rows, key = 'business_date') {
  const all = [...new Set(rows.map((r) => r[key]))].sort();
  if (!all.length) return [];
  const out = [];
  const [y0, m0, d0] = all[0].split('-').map(Number);
  const cursor = new Date(Date.UTC(y0, m0 - 1, d0));
  const last = all[all.length - 1];
  for (let guard = 0; guard < 800; guard += 1) {
    const iso = cursor.toISOString().slice(0, 10);
    out.push(iso);
    if (iso >= last) break;
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return out;
}

export function groupBy(rows, key) {
  const map = new Map();
  for (const row of rows) {
    const k = row[key];
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(row);
  }
  return map;
}

export function chart(cls = 'chart') {
  return `<div class="${cls}" role="img"></div>`;
}

export function legend(items) {
  return `<div class="legend">${items
    .map(
      (item) =>
        `<span class="legend__item"><span class="legend__swatch${
          item.square ? ' legend__swatch--sq' : ''
        }" style="background:${esc(item.color)}"></span>${esc(item.label)}</span>`
    )
    .join('')}</div>`;
}

export function statstrip(stats) {
  return `<div class="statstrip">${stats
    .map(
      (s) => `<div class="stat${s.wide ? ' stat--wide' : ''}">
        <p class="stat__name">${esc(s.name)}</p>
        <p class="stat__value">${s.value}${s.unit ? `<span class="unit">${esc(s.unit)}</span>` : ''}</p>
        ${s.sub ? `<p class="stat__sub">${esc(s.sub)}</p>` : ''}
      </div>`
    )
    .join('')}</div>`;
}

export function table(headers, rows, { align = [] } = {}) {
  return `<div class="table-wrap"><table class="data">
    <thead><tr>${headers
      .map((h, i) => `<th${align[i] === 'left' ? ' style="text-align:left"' : ''}>${esc(h)}</th>`)
      .join('')}</tr></thead>
    <tbody>${rows.map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

/**
 * Count-up, respecting reduced motion and formatting through Intl.
 *
 * The tween interpolates the *figure*, not the pixels, so the digits stay
 * tabular and the card never changes width mid-count. It stops the moment the
 * element leaves the document, which is what happens when a filter change
 * replaces the row underneath it.
 */
export function countUp(el, to, render) {
  if (!el) return;
  if (prefersReducedMotion() || to === null || to === undefined || Number.isNaN(to)) {
    el.innerHTML = render(to);
    return;
  }
  const duration = 650;
  const start = performance.now();
  const step = (now) => {
    if (!el.isConnected) return;
    const p = Math.min(1, (now - start) / duration);
    // cubic-out: fast to the neighbourhood of the answer, then settles
    el.innerHTML = render(to * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
    else el.innerHTML = render(to);
  };
  requestAnimationFrame(step);
}

/**
 * Give every `.segmented` group inside `root` a sliding thumb.
 *
 * The pressed state is still carried by `aria-pressed` / `aria-selected`; the
 * thumb only follows it, and CSS keeps a plain background on the pressed
 * button until the thumb has measured itself, so a group is never unreadable
 * because a frame has not run yet. The thumb moves on transform and width
 * alone, inside a flex row that is already laid out, so switching windows
 * costs no reflow of the page.
 */
export function initSegmented(root = document) {
  for (const group of root.querySelectorAll('.segmented')) {
    if (group.dataset.thumbed === 'true') continue;
    group.dataset.thumbed = 'true';

    const thumb = document.createElement('span');
    thumb.className = 'segmented__thumb';
    thumb.setAttribute('aria-hidden', 'true');
    group.prepend(thumb);

    let frame = 0;
    const sync = () => {
      frame = 0;
      const active = group.querySelector('[aria-pressed="true"], [aria-selected="true"]');
      if (!active || !active.offsetWidth) {
        group.dataset.ready = 'false';
        return;
      }
      thumb.style.setProperty('--thumb-w', `${active.offsetWidth}px`);
      thumb.style.setProperty('--thumb-x', `${active.offsetLeft}px`);
      group.dataset.ready = 'true';
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(sync);
    };

    group.addEventListener('click', schedule);
    new MutationObserver(schedule).observe(group, {
      attributes: true,
      subtree: true,
      attributeFilter: ['aria-pressed', 'aria-selected'],
    });
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(schedule).observe(group);
    }
    schedule();
    // The variable font lands after first paint and changes button widths.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(schedule).catch(() => {});
    }
  }
}

/**
 * Play the arrival animation, but only for something the reader can see.
 *
 * Panels arrive one request at a time and most of them are far below the fold.
 * Animating those would burn frames on motion nobody watches and would have
 * finished long before the panel was scrolled to.
 */
export function enter(el) {
  if (!el || prefersReducedMotion()) return;
  const box = el.getBoundingClientRect();
  const viewport = window.innerHeight || 0;
  if (box.top > viewport * 1.15 || box.bottom < -40) return;
  el.classList.remove('panel-in');
  void el.offsetWidth;
  el.classList.add('panel-in');
}

/** What a KPI shows when the figure does not exist yet. */
export const NONE = '<span class="kpi__none">not yet</span>';
