/**
 * What the record itself says: court-time withdrawn from sale, bookings that
 * reverted, the collector's own coverage, the venue set, and the metric
 * catalogue.
 *
 * These panels are about the limits of the data rather than the demand in it,
 * which is why they sit last and why none of them is optional.
 */

/**
 * House rules for every panel in this file, which are correctness rules rather
 * than style ones:
 *
 *  - Every panel prints its denominator and its date range, taken from the
 *    response envelope rather than written here, so the chart and the API can
 *    never disagree about what was divided by what.
 *  - Court-hours, never slot counts, in anything that compares venues: the
 *    grids are 60 and 30 minutes and the slot counts are not commensurable.
 *  - Blocked court-time is drawn beside occupancy, never folded into it.
 *  - A missing poll is a hole in the line, not a segment drawn across it.
 */

import {
  clock,
  dayLabel,
  dayLabelFull,
  dayWithWeekday,
  esc,
  int,
  minutes as fmtMinutes,
  num,
  pct,
  rupees,
  sentence,
  stamp,
} from './format.js';
import {
  mount,
  percentAxis,
  tip,
} from './charts.js';
import {
  caveatsOf,
  denominatorLine,
  emptyState,
  errorState,
  renderPanel,
} from './panels.js';
import {
  colorIndex,
  venueColor,
  groupBy,
  chart,
  statstrip,
  table,
} from './ui.js';

/* ==========================================================================
   9. Blocked events — inventory withdrawn from sale
   ========================================================================== */

export function renderBlocked(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Inventory withdrawn from sale', body: errorState(payload.error) });
  const data = payload.data;
  const rows = data.rows || [];

  if (data.empty || !rows.length) {
    return renderPanel(el, {
      title: 'Inventory withdrawn from sale',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason, { title: 'No court-time has been pulled from sale' }),
      caveats: caveatsOf(data),
    });
  }

  const totalHours = rows.reduce((a, r) => a + r.court_hours, 0);
  const biggest = [...rows].sort((a, b) => b.court_hours - a.court_hours)[0];
  const sorted = [...rows].sort((a, b) => b.court_hours - a.court_hours || a.business_date.localeCompare(b.business_date));

  renderPanel(el, {
    title: 'Inventory withdrawn from sale',
    denominator: denominatorLine(data.metric),
    body:
      statstrip([
        { name: 'Withdrawal events', value: esc(int(rows.length)), sub: 'a run of adjacent slots pulled together counts once' },
        { name: 'Court-hours pulled', value: esc(num(totalHours)), sub: 'never counted as occupancy, in either denominator' },
        {
          name: 'Largest single withdrawal',
          value: esc(num(biggest.court_hours)),
          unit: 'h',
          wide: true,
          sub: `${biggest.venue_name || biggest.venue_uuid}, ${dayLabelFull(biggest.business_date)}, ${clock(
            biggest.start_utc
          )}–${clock(biggest.end_utc)} IST — ${int(biggest.slot_count)} slots at once.`,
        },
      ]) +
      table(
        ['Venue', 'Business date', 'Window', 'Slots', 'Court-h', 'First seen blocked'],
        sorted
          .slice(0, 14)
          .map((r) => [
            `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
              colorIndex(r.venue_uuid) + 1
            })"></span>${esc(r.venue_name || r.venue_uuid)}</span>`,
            `<span class="dim">${esc(dayWithWeekday(r.business_date))}</span>`,
            `<span class="dim">${esc(clock(r.start_utc))}–${esc(clock(r.end_utc))}</span>`,
            `<span class="num">${esc(int(r.slot_count))}</span>`,
            `<span class="num" style="color:var(--blocked)">${esc(num(r.court_hours))}</span>`,
            r.censored_left
              ? '<span class="dim">already blocked when first seen</span>'
              : `<span class="dim">${esc(stamp(r.first_seen_at))} ±${esc(int(r.uncertainty_minutes))} min</span>`,
          ]),
        { align: ['left'] }
      ) +
      (sorted.length > 14
        ? `<p class="note">Showing the 14 largest of
            ${esc(int(sorted.length))} withdrawal events.</p>`
        : ''),
    flush: false,
    caveats: caveatsOf(data),
  });
}

/* ==========================================================================
   10. Cancellations
   ========================================================================== */

export function renderCancellations(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Bookings that went away again', body: errorState(payload.error) });
  const data = payload.data;
  const rows = data.rows || [];

  if (data.empty || !rows.length) {
    return renderPanel(el, {
      title: 'Bookings that went away again',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  renderPanel(el, {
    title: 'Bookings that went away again',
    denominator: denominatorLine(data.metric),
    body: table(
      ['Venue', 'Week', 'Bookings seen', 'Reverted', 'Rate', 'Court-h returned', 'Not measurable'],
      [...rows]
        .sort((a, b) => b.week_start.localeCompare(a.week_start) || (b.rate || 0) - (a.rate || 0))
        .map((r) => [
          `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
            colorIndex(r.venue_uuid) + 1
          })"></span>${esc(r.venue_name || r.venue_uuid)}</span>`,
          `<span class="dim">${esc(dayLabel(r.week_start))}</span>`,
          `<span class="num">${esc(int(r.bookings))}</span>`,
          `<span class="num">${esc(int(r.cancellations))}</span>`,
          `<span class="num">${esc(pct(r.rate, { places: 1 }))}</span>`,
          `<span class="num">${esc(num(r.cancelled_court_hours))}</span>`,
          `<span class="dim">${esc(int(r.censored_slots))} censored</span>`,
        ]),
      { align: ['left'] }
    ),
    caveats: caveatsOf(data),
  });
}

/* ==========================================================================
   13. Collector coverage — the holes, drawn as holes
   ========================================================================== */

export function renderCoverage(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'What the collector actually caught', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !(data.days || []).length) {
    return renderPanel(el, {
      title: 'What the collector actually caught',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const gaps = data.poll_gaps || [];
  const dates = [...new Set(data.days.map((d) => d.observed_date))].sort();
  const byFacility = groupBy(data.days, 'facility_uuid');

  renderPanel(el, {
    title: 'What the collector actually caught',
    aside: data.has_gaps
      ? `<span class="chip chip--blocked">${esc(int(data.missed_polls))} missed poll${data.missed_polls === 1 ? '' : 's'}</span>`
      : '<span class="chip chip--booked">unbroken</span>',
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range">${esc(int(data.snapshots_expected_per_day))} polls a day at ${esc(int(data.cadence_minutes))}-minute cadence</span>`
    ),
    body:
      chart('chart chart--short') +
      (gaps.length
        ? `<div style="margin-top:var(--s4)">
            <p class="note note--lead">Each gap below is court-time that can
              never be re-observed. A slot that sold inside one of these windows is recorded as
              having sold at the next poll, not when it actually sold.</p>
            ${table(
              ['Gap began', 'Gap ended', 'Length', 'Polls missed'],
              gaps.map((g) => [
                `<span class="dim">${esc(stamp(g.start))}</span>`,
                `<span class="dim">${esc(stamp(g.end))}</span>`,
                `<span class="num" style="color:var(--blocked)">${esc(fmtMinutes(g.minutes))}</span>`,
                `<span class="num">${esc(int(g.missed_polls))}</span>`,
              ]),
              { align: ['left'] }
            )}</div>`
        : `<p class="note">No gap in the poll record: every
            expected poll in this range arrived.</p>`),
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          formatter(params) {
            const date = params[0].axisValue;
            return tip({
              head: `${dayLabelFull(date)} (UTC observation date)`,
              rows: params.map((p) => {
                const row = (byFacility.get(p.seriesId) || []).find((d) => d.observed_date === date);
                return {
                  color: p.color,
                  key: p.seriesName,
                  value: row
                    ? `${int(row.snapshots_received)} of ${int(row.snapshots_expected)} polls${
                        row.fetches_failed ? ` · ${int(row.fetches_failed)} failed` : ''
                      }`
                    : 'nothing observed',
                };
              }),
              note: 'A partial first or last day reads as low coverage on purpose. Prorating would let a collector that ran twice report 100%.',
            });
          },
        },
        xAxis: {
          type: 'category',
          data: dates,
          ...ax,
          splitLine: { show: false },
          axisLabel: { ...ax.axisLabel, formatter: (v) => dayLabel(v), hideOverlap: true },
        },
        yAxis: percentAxis(t, 'polls received'),
        series: [...byFacility.keys()].map((uuid) => {
          const rows = byFacility.get(uuid);
          const color = venueColor(t, uuid);
          return {
            id: uuid,
            name: rows[0].facility_name || uuid,
            type: 'line',
            step: 'middle',
            connectNulls: false,
            showSymbol: true,
            symbolSize: 5,
            data: dates.map((d) => {
              const row = rows.find((r) => r.observed_date === d);
              return row ? row.coverage_ratio : null;
            }),
            lineStyle: { width: 2, color },
            itemStyle: { color },
            emphasis: { focus: 'series' },
          };
        }),
      }));
    },
  });
}

/* ==========================================================================
   14. Venues and courts
   ========================================================================== */

export function renderVenues(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'The venues and their courts', body: errorState(payload.error) });
  const data = payload.data;
  const venues = data.venues || [];

  renderPanel(el, {
    title: 'The venues and their courts',
    denominator: denominatorLine(data.metric),
    body:
      (data.reason
        ? `<p class="note note--lead">${esc(sentence(data.reason))}</p>`
        : '') +
      venues
        .map((venue) => {
          const quality = venue.data_quality;
          const flagged = quality && quality.flags && quality.flags.length;
          return `
          <div class="venue-block">
            <p class="deflist__term"><span class="venue-cell"><span class="venue-dot"
              style="background:var(--v${colorIndex(venue.venue_uuid) + 1})"></span>${esc(
                venue.name
              )}</span></p>
            <p class="deflist__def">Tracked since ${esc(
              venue.first_seen ? stamp(venue.first_seen) : 'the first poll'
            )} · last seen ${esc(venue.last_seen ? stamp(venue.last_seen) : '—')} · ${esc(venue.tz)}</p>
            ${table(
              ['Court', 'Sport', 'Grid', 'Per court-hour', 'Per slot'],
              venue.courts
                .filter((c) => c.kind === 'court')
                .map((c) => [
                  `<span style="font-weight:550">${esc(c.name)}</span>${
                    c.active ? '' : ' <span class="chip chip--flat">inactive</span>'
                  }`,
                  `<span class="dim">${esc(c.sport)}</span>`,
                  `<span class="dim">${c.grid_minutes ? `${esc(int(c.grid_minutes))} min` : 'unprobed'}</span>`,
                  `<span class="num">${esc(
                    c.price_per_court_hour ? rupees(c.price_per_court_hour) : '—'
                  )}</span>`,
                  `<span class="num dim">${esc(c.price_per_slot ? rupees(c.price_per_slot) : '—')}</span>`,
                ]),
              { align: ['left'] }
            )}
            ${
              flagged
                ? `<div class="banner" data-tone="warn" style="margin-top:var(--s3)">
                     <span class="banner__mark" aria-hidden="true"></span>
                     <span>${esc(
                       sentence(
                         `${venue.short_name || venue.name} ${quality.note.replace(
                           `${venue.venue_uuid}: `,
                           ''
                         )}`
                       )
                     )} It has listed ${esc(num(quality.listed_court_hours))} court-hours in this
                     window and ${esc(num(quality.blocked_court_hours))} of them were withdrawn from
                     sale.</span>
                   </div>`
                : ''
            }
          </div>`;
        })
        .join(''),
    caveats: caveatsOf(data),
  });
}

/* ==========================================================================
   15. The metric catalog
   ========================================================================== */

export function renderCatalog(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Every metric and what it divides by', body: errorState(payload.error) });
  const data = payload.data;
  const metrics = data.metrics || [];

  renderPanel(el, {
    title: 'Every metric and what it divides by',
    aside: `<button class="disclose" type="button" aria-expanded="false" id="catalog-toggle">${esc(
      int(metrics.length)
    )} definitions</button>`,
    denominator: `<span class="den-range">The same definitions the API sends with every response, listed once. If a chart above disagrees with one of these, the chart is wrong.</span>`,
    body: `<div class="deflist" id="catalog-list" hidden>${metrics
      .map(
        (m) => `<div class="deflist__row">
          <p class="deflist__term">${esc(m.title)}</p>
          <p class="deflist__def"><b>${esc(m.numerator)}</b> ÷ <b>${esc(m.denominator)}</b> — ${esc(
            m.denominator_description
          )}</p>
        </div>`
      )
      .join('')}</div>`,
    mount(root) {
      const toggle = root.querySelector('#catalog-toggle');
      const list = root.querySelector('#catalog-list');
      toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', String(!open));
        list.hidden = open;
      });
    },
  });
}
