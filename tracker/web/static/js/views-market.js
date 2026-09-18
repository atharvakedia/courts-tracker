/**
 * Price and comparison: price per court-hour over time, whether price moves
 * with the hour of day, each venue's share of the three, and the revenue proxy.
 *
 * Price is always per court-hour. Per slot, Play Padel is the cheapest of the
 * three and also the most expensive — the ranking inverts — so the per-slot
 * figure appears only beside its per-hour twin, never alone.
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
  esc,
  hourLabel,
  int,
  num,
  pct,
  rupees,
  sentence,
  stamp,
} from './format.js';
import {
  brokenLine,
  morph,
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
  table,
} from './ui.js';

/* ==========================================================================
   5. Price per court-hour over time
   ========================================================================== */

export function renderPricing(el, payload, { gaps, cadence }) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Price per court-hour', body: errorState(payload.error) });
  const data = payload.data;
  const timelines = (data.timelines || []).filter((tl) => tl.points.length);

  if (data.empty || !timelines.length) {
    return renderPanel(el, {
      title: 'Price per court-hour',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const changes = data.changes || [];
  const gapMs = cadence * 60 * 1000;

  renderPanel(el, {
    title: 'Price per court-hour',
    aside: changes.length
      ? `<span class="chip chip--booked">${esc(int(changes.length))} price change${changes.length === 1 ? '' : 's'} seen</span>`
      : `<span class="chip chip--flat">no price change seen yet</span>`,
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range">${esc(data.currency)}, per court-hour — never per slot, which inverts the ranking</span>`
    ),
    body:
      chart() +
      (changes.length
        ? `<div style="margin-top:var(--s4)">${table(
            ['Venue', 'From', 'To', 'Change', 'First seen at this price', 'Known to within'],
            changes.map((c) => [
              `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
                colorIndex(c.venue_uuid) + 1
              })"></span>${esc(c.venue_name || c.venue_uuid)}</span>`,
              `<span class="num">${esc(rupees(c.from_price_per_court_hour))}</span>`,
              `<span class="num">${esc(rupees(c.to_price_per_court_hour))}</span>`,
              `<span class="num" style="color:var(--${c.direction === 'increase' ? 'blocked' : 'booked'})">${
                c.direction === 'increase' ? '+' : '−'
              }${esc(rupees(Math.abs(c.delta_per_court_hour)).slice(1))}</span>`,
              `<span class="dim">${esc(stamp(c.first_seen_at))}</span>`,
              `<span class="dim">±${esc(int(c.uncertainty_minutes))} min</span>`,
            ]),
            { align: ['left'] }
          )}</div>`
        : `<p class="note">Every price observed so far has held
             flat. A change would appear here with the poll window it happened inside — never as an
             exact instant, because a change is only ever seen at the next poll.</p>`),
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 14, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          formatter(params) {
            const first = params.find((p) => p.value && p.value[1] !== null);
            return tip({
              head: first ? stamp(new Date(first.value[0]).toISOString()) : '—',
              rows: params
                .filter((p) => p.value && p.value[1] !== null)
                .map((p) => ({
                  color: p.color,
                  key: p.seriesName,
                  value: `${rupees(p.value[1])} / court-hour`,
                })),
              note: 'A price is only known at the instants the collector polled.',
            });
          },
        },
        xAxis: {
          type: 'time',
          ...ax,
          axisLabel: { ...ax.axisLabel, hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: {
          type: 'value',
          name: `${data.currency} per court-hour`,
          nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'left' },
          scale: true,
          ...ax,
          axisLabel: { ...ax.axisLabel, formatter: (v) => rupees(v) },
        },
        series: timelines.map((tl, idx) => {
          // A hole in the poll record is drawn as a hole. Stepping across it
          // would assert a price nobody observed.
          const points = [];
          let prev = null;
          for (const p of tl.points) {
            const ts = new Date(p.observed_at).getTime();
            if (prev !== null && ts - prev > gapMs * 1.5) {
              points.push([prev + gapMs / 2, null]);
            }
            points.push([ts, p.price_per_court_hour]);
            prev = ts;
          }
          const color = venueColor(t, tl.venue_uuid);
          return {
            name: tl.venue_name || tl.facility_name || tl.venue_uuid,
            type: 'line',
            step: 'end',
            data: points,
            connectNulls: false,
            showSymbol: false,
            lineStyle: { width: 2.2, color },
            itemStyle: { color },
            emphasis: { focus: 'series' },
            markArea:
              idx === 0 && gaps.length
                ? {
                    silent: true,
                    itemStyle: { color: t.blockedSoft, opacity: 0.8 },
                    label: {
                      show: true,
                      position: 'insideTop',
                      color: t.blocked,
                      fontSize: 10,
                      fontWeight: 600,
                      formatter: 'no polls',
                    },
                    data: gaps.map((g) => [
                      { xAxis: new Date(g.start).getTime() },
                      { xAxis: new Date(g.end).getTime() },
                    ]),
                  }
                : undefined,
            markLine: changes.some((c) => c.venue_uuid === tl.venue_uuid)
              ? {
                  silent: true,
                  symbol: 'none',
                  lineStyle: { color, type: 'dotted', width: 1.4 },
                  label: { show: false },
                  data: changes
                    .filter((c) => c.venue_uuid === tl.venue_uuid)
                    .map((c) => ({ xAxis: new Date(c.first_seen_at).getTime() })),
                }
              : undefined,
          };
        }),
      }));
    },
  });
}

/* ==========================================================================
   6. Price by hour of day
   ========================================================================== */

export function renderPriceByHour(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Price by hour of day', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !(data.courts || []).length) {
    return renderPanel(el, {
      title: 'Price by hour of day',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  // When nothing varies, say so once. Three identical rows of the same number
  // would look like a finding; it is the absence of one.
  if (!data.has_any_variation) {
    const ranked = [...(data.venues || [])].sort(
      (a, b) => (b.flat_price_per_court_hour || 0) - (a.flat_price_per_court_hour || 0)
    );
    return renderPanel(el, {
      title: 'Price by hour of day',
      aside: '<span class="chip chip--flat">flat everywhere</span>',
      denominator: denominatorLine(data.metric),
      body:
        `<p class="panel-lede">No venue varies its price by hour of day. A 06:00 court costs what
          a 20:00 court costs, everywhere.</p>
         <p class="note note--lead">${esc(sentence(data.summary))} The table below is the whole of
          the pricing structure: one rate each, charged around the clock.</p>` +
        table(
          ['Venue', 'Per court-hour', 'Hours priced', 'Distinct prices seen'],
          ranked.map((v) => [
            `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
              colorIndex(v.venue_uuid) + 1
            })"></span>${esc(v.venue_name || v.venue_uuid)}</span>`,
            `<span class="num">${esc(rupees(v.flat_price_per_court_hour))}</span>`,
            `<span class="dim">${esc(int(data.hours.length))}</span>`,
            `<span class="dim">${esc(v.distinct_prices_per_court_hour.map((p) => rupees(p)).join(', '))}</span>`,
          ]),
          { align: ['left'] }
        ),
      caveats: caveatsOf(data),
    });
  }

  const courts = data.courts || [];
  const currency = (courts[0] && courts[0].currency) || ((data.cells || [])[0] && data.cells[0].currency) || 'INR';
  const byCourt = new Map();
  for (const cell of data.cells || []) {
    if (!byCourt.has(cell.facility_uuid)) byCourt.set(cell.facility_uuid, new Map());
    byCourt.get(cell.facility_uuid).set(cell.hour, cell);
  }

  renderPanel(el, {
    title: 'Price by hour of day',
    aside: `<span class="chip chip--booked">${esc(sentence(data.summary))}</span>`,
    denominator: denominatorLine(data.metric),
    body:
      chart('chart chart--short') +
      `<div style="margin-top:var(--s4)">${table(
        ['Court', 'Structure', 'Per court-hour', 'Prices seen'],
        courts.map((c) => [
          `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
            colorIndex(c.venue_uuid) + 1
          })"></span>${esc(c.facility_name || c.venue_name || c.facility_uuid)}</span>`,
          c.is_flat
            ? '<span class="chip chip--flat">flat</span>'
            : '<span class="chip chip--blocked">varies by hour</span>',
          `<span class="num">${esc(
            c.is_flat ? rupees(c.flat_price_per_court_hour) : '—'
          )}</span>`,
          `<span class="dim">${esc(
            c.distinct_prices_per_court_hour.map((p) => rupees(p)).join(', ')
          )}</span>`,
        ]),
        { align: ['left'] }
      )}</div>`,
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          formatter: (params) =>
            tip({
              head: `${params[0].axisValue}`,
              rows: params.map((p) => ({
                color: p.color,
                key: p.seriesName,
                value: p.value === null ? 'not sold at this hour' : `${rupees(p.value)} / court-hour`,
              })),
            }),
        },
        xAxis: { type: 'category', data: data.hours.map(hourLabel), ...ax, splitLine: { show: false } },
        yAxis: {
          type: 'value',
          scale: true,
          name: `${currency} per court-hour`,
          nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'left' },
          ...ax,
          axisLabel: { ...ax.axisLabel, formatter: (v) => rupees(v) },
        },
        series: courts.map((c) => {
          const cells = byCourt.get(c.facility_uuid) || new Map();
          const color = venueColor(t, c.venue_uuid);
          return brokenLine(
            c.facility_name || c.venue_name || c.facility_uuid,
            data.hours.map((h) => (cells.has(h) ? cells.get(h).price_per_court_hour : null)),
            color,
            { step: 'middle', lineStyle: { width: 2.2, color, type: c.is_flat ? 'dashed' : 'solid' } }
          );
        }),
      }));
    },
  });
}

/* ==========================================================================
   7. Market share
   ========================================================================== */

let shareBasis = 'demand';

export function renderShare(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Market share', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !(data.rows || []).length) {
    return renderPanel(el, {
      title: 'Market share',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const weeks = [...new Set(data.rows.map((r) => r.week_label))].sort();
  const byVenue = groupBy(data.rows, 'venue_uuid');
  const flagged = (data.venues || []).filter((v) => v.flags && v.flags.length);

  renderPanel(el, {
    title: 'Market share',
    aside: `<div class="segmented" role="group" aria-label="Share basis" id="share-basis">
      <button class="segmented__btn" type="button" data-share="demand" aria-pressed="${shareBasis === 'demand'}">Demand</button>
      <button class="segmented__btn" type="button" data-share="supply" aria-pressed="${shareBasis === 'supply'}">Supply</button>
    </div>`,
    denominator: denominatorLine(data.metric),
    body:
      chart() +
      (flagged.length
        ? `<div class="banner" data-tone="warn" style="margin-top:var(--s4)">
             <span class="banner__mark" aria-hidden="true"></span>
             <span>${flagged
               .map(
                 (v) =>
                   `<b>${esc(v.venue_name || v.venue_uuid)}</b> has never been observed with a
                    booking. Its demand share reads 0% and will keep reading 0%; it lists
                    ${esc(num(v.listed_court_hours))} court-hours in this window, so its supply
                    share is the honest figure for it. It may simply not take bookings through
                    Hudle.`
               )
               .join(' ')}</span>
           </div>`
        : ''),
    caveats: caveatsOf(data),
    mount(root) {
      // Demand and supply are two shares of the same weeks, so the stack
      // re-proportions in place rather than the panel being rebuilt.
      const basisGroup = root.querySelector('#share-basis');
      basisGroup.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-share]');
        if (!btn || btn.dataset.share === shareBasis) return;
        shareBasis = btn.dataset.share;
        for (const other of basisGroup.querySelectorAll('[data-share]')) {
          other.setAttribute('aria-pressed', String(other.dataset.share === shareBasis));
        }
        morph(root.querySelector('.chart'));
      });

      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter(params) {
            const week = params[0].axisValue;
            return tip({
              head: `Week of ${week}`,
              rows: params.map((p) => {
                const row = (byVenue.get(p.seriesId) || []).find((r) => r.week_label === week);
                return {
                  color: p.color,
                  key: p.seriesName,
                  value: row
                    ? `${pct(shareBasis === 'demand' ? row.demand_share : row.supply_share, {
                        places: 1,
                      })} · ${num(
                        shareBasis === 'demand' ? row.booked_court_hours : row.listed_court_hours
                      )} court-h`
                    : '—',
                };
              }),
              note:
                shareBasis === 'demand'
                  ? 'Share of court-hours booked through Hudle across these three listings only — not of padel in Jaipur.'
                  : 'Share of court-hours put on sale. This stays meaningful for a venue with no observed bookings.',
            });
          },
        },
        xAxis: { type: 'category', data: weeks, ...ax, splitLine: { show: false } },
        yAxis: percentAxis(t, shareBasis === 'demand' ? 'share of booked court-hours' : 'share of listed court-hours'),
        series: [...byVenue.keys()].map((uuid) => {
          const rows = byVenue.get(uuid);
          return {
            id: uuid,
            name: rows[0].venue_name || uuid,
            type: 'bar',
            stack: 'share',
            data: weeks.map((w) => {
              const row = rows.find((r) => r.week_label === w);
              if (!row) return null;
              return shareBasis === 'demand' ? row.demand_share : row.supply_share;
            }),
            itemStyle: { color: venueColor(t, uuid) },
            barMaxWidth: 46,
            emphasis: { focus: 'series' },
          };
        }),
      }));
    },
  });
}

/* ==========================================================================
   8. Revenue proxy
   ========================================================================== */

export function renderRevenue(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Revenue proxy', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !(data.venue_totals || []).length) {
    return renderPanel(el, {
      title: 'Revenue proxy',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const weeks = [...new Set((data.rows || []).map((r) => r.week_label))].sort();
  const byVenue = groupBy(data.rows || [], 'venue_uuid');

  renderPanel(el, {
    title: 'Revenue proxy',
    aside: `<span class="chip chip--blocked">a proxy, not revenue</span>`,
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range">${esc(data.label)}</span>`
    ),
    body:
      chart() +
      `<div style="margin-top:var(--s4)">${table(
        ['Venue', 'Booked court-h', 'Booked at list price', 'Withdrawn court-h', 'If that had sold', 'Upper bound'],
        [...data.venue_totals]
          .sort((a, b) => b.booked_revenue_proxy - a.booked_revenue_proxy)
          .map((v) => [
            `<span class="venue-cell"><span class="venue-dot" style="background:var(--v${
              colorIndex(v.venue_uuid) + 1
            })"></span>${esc(v.venue_name || v.venue_uuid)}</span>`,
            `<span class="num">${esc(num(v.booked_court_hours))}</span>`,
            `<span class="num">${esc(rupees(v.booked_revenue_proxy))}</span>`,
            `<span class="num" style="color:var(--blocked)">${esc(num(v.blocked_court_hours))}</span>`,
            `<span class="num dim">${esc(rupees(v.blocked_revenue_if_sold))}</span>`,
            `<span class="num dim">${esc(rupees(v.booked_revenue_proxy + v.blocked_revenue_if_sold))}</span>`,
          ]),
        { align: ['left'] }
      )}</div>`,
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter(params) {
            const week = params[0].axisValue;
            return tip({
              head: `Week of ${week}`,
              rows: params
                .filter((p) => p.value)
                .map((p) => ({ color: p.color, key: p.seriesName, value: rupees(p.value) })),
              note: 'Booked court-hours priced at the list rate observed. Discounts, memberships, offline sales and no-shows are all invisible to this.',
            });
          },
        },
        xAxis: { type: 'category', data: weeks, ...ax, splitLine: { show: false } },
        yAxis: {
          type: 'value',
          name: data.currency || 'INR',
          nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'left' },
          ...ax,
          axisLabel: { ...ax.axisLabel, formatter: (v) => (v >= 1000 ? `₹${v / 1000}k` : `₹${v}`) },
        },
        series: [
          ...[...byVenue.keys()].map((uuid) => {
            const rows = byVenue.get(uuid);
            return {
              name: rows[0].venue_name || uuid,
              type: 'bar',
              stack: 'rev',
              data: weeks.map((w) => {
                const row = rows.find((r) => r.week_label === w);
                return row ? row.booked_revenue_proxy : null;
              }),
              itemStyle: { color: venueColor(t, uuid) },
              barMaxWidth: 46,
              emphasis: { focus: 'series' },
            };
          }),
          {
            name: 'withdrawn, if it had sold',
            type: 'bar',
            stack: 'rev',
            data: weeks.map((w) =>
              (data.rows || [])
                .filter((r) => r.week_label === w)
                .reduce((a, r) => a + r.blocked_revenue_if_sold, 0)
            ),
            itemStyle: {
              color: t.blocked,
              opacity: 0.3,
              borderColor: t.blocked,
              borderWidth: 1,
              borderType: 'dashed',
            },
            barMaxWidth: 46,
          },
        ],
      }));
    },
  });
}
