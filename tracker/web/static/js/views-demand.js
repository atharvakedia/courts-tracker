/**
 * The top of the page and the demand panels: the hero, the KPI row, occupancy
 * by day, the peak-hour grid, weekday against weekend, and the first slot to
 * sell on a given date.
 *
 * The hero is the one place in the dashboard that does arithmetic. It adds the
 * weekday and weekend partitions the API reports — they cover every business
 * date exactly once — and divides using the API's own `sellable_minutes`. The
 * definition of which minutes are sellable is never restated here.
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
  dayLabel,
  dayWithWeekday,
  esc,
  hourLabel,
  hours as fmtHours,
  int,
  num,
  pct,
  sentence,
  signedPct,
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
  DAY_ORDER,
  colorIndex,
  venueColor,
  denseDates,
  groupBy,
  chart,
  legend,
  statstrip,
  countUp,
  NONE,
} from './ui.js';

/* ==========================================================================
   Hero — the denominator, drawn to scale
   ========================================================================== */

export function renderHero(payload, { windowText, scopeLabel = 'the three venues' }) {
  const host = document.getElementById('hero-content');
  const skeleton = document.getElementById('hero-skeleton');
  if (!host) return null;
  skeleton.hidden = true;
  host.hidden = false;
  host.classList.remove('is-stale');

  if (!payload || !payload.ok) {
    host.innerHTML = `<p class="hero__label">The headline figures could not load.</p>
      <p class="hero__denominator">${esc(sentence(payload && payload.error))}</p>`;
    return null;
  }

  const data = payload.data;
  // weekday and weekend partition every business date exactly once, so their
  // published minute totals add to the window total. The definition of which
  // minutes are sellable is the API's, not this file's.
  const parts = [data.weekday, data.weekend].filter(Boolean);
  const sum = (key) => parts.reduce((acc, p) => acc + (p[key] || 0), 0);
  const booked = sum('booked_minutes');
  const open = sum('open_minutes');
  const blocked = sum('blocked_minutes');
  const sellable = sum('sellable_minutes');
  const total = sum('total_minutes');
  const strict = sellable > 0 ? booked / sellable : null;
  const gross = total > 0 ? (booked + blocked) / total : null;
  const blockedShare = total > 0 ? blocked / total : null;
  const range = (data.metric && data.metric.date_range) || {};

  if (data.empty || total === 0) {
    host.innerHTML = `
      <p class="hero__figure">—<span class="unit">%</span></p>
      <p class="hero__label">No court-time has been observed in this window yet.</p>
      <p class="hero__denominator">${esc(sentence(data.reason || windowText))}</p>`;
    return { strict, booked, blocked, total };
  }

  host.innerHTML = `
    <div class="hero__grid">
      <div class="enter" style="--delay:40ms">
        <p class="hero__figure"><span id="hero-figure">&nbsp;</span><span class="unit">occupied</span></p>
        <p class="hero__label" id="hero-label">of every court-hour ${esc(scopeLabel)} put on sale</p>
      </div>
      <p class="hero__denominator enter" style="--delay:140ms">
        Booked ÷ booked + open court-hours${esc(range.label ? ` · ${range.label}` : '')} ·
        gross <b>${esc(pct(gross, { places: 1 }))}</b>
      </p>
    </div>
    <div class="invbar enter" style="--delay:220ms" role="img"
      aria-label="Every listed court-hour, drawn to scale and split into booked, still open, and withdrawn from sale">
      <div class="invbar__seg" data-kind="booked" style="flex-grow:0"></div>
      <div class="invbar__seg" data-kind="open" style="flex-grow:0"></div>
      <div class="invbar__seg" data-kind="blocked" style="flex-grow:0"></div>
    </div>
    <div class="invbar__key enter" style="--delay:300ms">
      <span class="invbar__item"><span class="invbar__swatch" data-kind="booked"></span>
        <span class="invbar__num">${esc(num(booked / 60))}</span>
        <span class="invbar__cap">court-hours booked</span></span>
      <span class="invbar__item"><span class="invbar__swatch" data-kind="open"></span>
        <span class="invbar__num">${esc(num(open / 60))}</span>
        <span class="invbar__cap">still open</span></span>
      <span class="invbar__item"><span class="invbar__swatch" data-kind="blocked"></span>
        <span class="invbar__num">${esc(num(blocked / 60))}</span>
        <span class="invbar__cap">withdrawn from sale${
          blockedShare ? ` (${esc(pct(blockedShare, { places: 1 }))})` : ''
        }</span></span>
    </div>`;

  const segs = host.querySelectorAll('.invbar__seg');
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      segs[0].style.flexGrow = String(booked || 0.0001);
      segs[1].style.flexGrow = String(open || 0.0001);
      segs[2].style.flexGrow = String(blocked || 0.0001);
    });
  });
  countUp(document.getElementById('hero-figure'), strict, (v) => esc(pct(v, { places: 1 })));

  return { strict, gross, booked, open, blocked, total, blockedShare, range };
}

/* ==========================================================================
   KPI row
   ========================================================================== */

/**
 * The lead-time figure arrives later than the other three, and may arrive
 * before the row it belongs to exists. Holding it here rather than passing it
 * in means the row and the figure can land in either order without one of them
 * being stuck on a placeholder.
 *
 * Four states, four different sentences: not requested yet, in flight, answered
 * with nothing to say, and failed.
 */
let leadTimeState; // undefined = in flight, false = failed, null = no answer
let leadTimeCadence = 30;

export function setLeadTime(value, cadence) {
  leadTimeState = value;
  if (cadence) leadTimeCadence = cadence;
  const host = document.getElementById('kpis');
  const valueEl = host && host.querySelector('[data-kpi="3"]');
  if (!valueEl) return;
  const card = leadTimeCard(leadTimeState, leadTimeCadence, leadTimeState === undefined);
  if (card.html) valueEl.innerHTML = card.value;
  else valueEl.textContent = card.value;
  const den = host.querySelector('[data-kpi="3"] ~ .kpi__den');
  if (den) den.textContent = card.den;
}

function leadTimeCard(leadtime, uncertainty, pending) {
  const lead = leadtime && leadtime.overall;
  if (leadtime === false) {
    return {
      name: 'Median booking lead time',
      value: NONE,
      html: true,
      raw: null,
      den: 'The lead-time endpoint did not answer. The panel below says what happened.',
    };
  }
  if (pending) {
    return {
      name: 'Median booking lead time',
      value: '<span class="skel" style="display:inline-block;width:4.5ch;height:.72em"></span>',
      html: true,
      raw: null,
      den: 'Reading every observed booking\u2019s first-seen time\u2026',
    };
  }
  return {
    name: 'Median booking lead time',
    value: lead && lead.median_hours !== null ? fmtHours(lead.median_hours) : NONE,
    html: !(lead && lead.median_hours !== null),
    raw: null,
    den:
      lead && lead.n
        ? `n = ${int(lead.n)}, ${int(lead.excluded_censored)} left out as already booked when first seen. Known to \u00b1${int(uncertainty)} min \u2014 the polling gap \u2014 never exactly.`
        : 'Lead time measures a change, so it needs the same slot seen open and then booked. Two polls, at least.',
  };
}

export function renderKpis({ totals, venues, health }) {
  const host = document.getElementById('kpis');
  if (!host) return;
  host.classList.remove('is-stale');

  const flagged = ((venues && venues.venues) || []).filter(
    (v) => v.data_quality && v.data_quality.flags && v.data_quality.flags.length
  );

  const uncertainty = health ? health.cadence_minutes : leadTimeCadence;

  const cards = [
    {
      name: 'Occupancy, strict',
      value: totals ? pct(totals.strict, { places: 1 }) : NONE,
      html: !totals,
      raw: totals ? totals.strict : null,
      render: (v) => pct(v, { places: 1 }),
      den: 'Booked ÷ booked + open court-hours. Withdrawn court-time is excluded from both sides.',
    },
    {
      name: 'Booked court-hours',
      value: totals ? num(totals.booked / 60) : NONE,
      html: !totals,
      raw: totals ? totals.booked / 60 : null,
      render: (v) => num(v),
      den: 'Court-minutes normalised to hours, so a 60-minute grid and a 30-minute grid compare.',
    },
    {
      name: 'Withdrawn from sale',
      value: totals ? pct(totals.blockedShare, { places: 1 }) : NONE,
      html: !totals,
      raw: totals ? totals.blockedShare : null,
      render: (v) => pct(v, { places: 1 }),
      den: `Blocked ÷ all listed court-hours. ${
        totals ? `${num(totals.blocked / 60)} court-hours no customer could buy.` : ''
      }`,
    },
    leadTimeCard(leadTimeState, uncertainty, leadTimeState === undefined),
  ];

  // The hero already carries occupancy, booked and withdrawn court-hours to
  // scale; repeating them as tiles said the same number twice. Only the figure
  // the hero cannot show is tiled. The denominator survives as hover text.
  const shown = cards.filter((card) => /lead time/i.test(card.name));
  host.innerHTML = shown
    .map(
      (card, i) => `
      <article class="kpi enter" style="--delay:${60 + i * 70}ms" title="${esc(card.den)}">
        <p class="kpi__name">${esc(card.name)}</p>
        <p class="kpi__value" data-kpi="${i}">${card.html ? card.value : esc(card.value)}</p>
        ${
          i === 0 && flagged.length
            ? `<p class="kpi__flag">${esc(
                flagged
                  .map(
                    (v) =>
                      `${v.short_name || v.name} has never been seen with a booking`
                  )
                  .join('; ')
              )} — its 0% is in this average.</p>`
            : ''
        }
      </article>`
    )
    .join('');

  shown.forEach((card, i) => {
    if (card.raw === null || card.raw === undefined) return;
    countUp(host.querySelector(`[data-kpi="${i}"]`), card.raw, (v) => esc(card.render(v)));
  });
}

/* ==========================================================================
   1. Occupancy by venue by business date — the headline chart
   ========================================================================== */

let occupancyBasis = 'strict';

export function renderOccupancy(el, payload, ctx) {
  if (!payload.ok) return renderPanel(el, { title: 'Occupancy by day', body: errorState(payload.error) });
  const data = payload.data;
  const rows = data.rows || [];

  const aside = `<div class="segmented" role="group" aria-label="Occupancy denominator" id="occ-basis">
      <button class="segmented__btn" type="button" data-basis="strict" aria-pressed="${occupancyBasis === 'strict'}">Strict</button>
      <button class="segmented__btn" type="button" data-basis="gross" aria-pressed="${occupancyBasis === 'gross'}">Gross</button>
    </div>`;

  if (data.empty || !rows.length) {
    return renderPanel(el, {
      title: 'Occupancy by day',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const dates = denseDates(rows);
  const byVenue = groupBy(rows, 'venue_uuid');
  const blockedTotals = dates.map((d) =>
    rows.filter((r) => r.business_date === d).reduce((a, r) => a + r.blocked_court_hours, 0)
  );
  // Withdrawn court-time as a share of everything listed that day, so it sits
  // on the same 0-100% axis as occupancy instead of on a second scale.
  const listedTotals = dates.map((d) =>
    rows
      .filter((r) => r.business_date === d)
      .reduce((a, r) => a + r.sellable_court_hours + r.blocked_court_hours, 0)
  );
  const blockedShares = blockedTotals.map((b, i) => (listedTotals[i] ? b / listedTotals[i] : null));
  const lookup = new Map(rows.map((r) => [`${r.venue_uuid}|${r.business_date}`, r]));

  renderPanel(el, {
    title: 'Occupancy by day',
    aside,
    denominator: denominatorLine(data.metric),
    body:
      legend([
        ...[...byVenue.keys()].map((uuid) => ({
          color: 'var(--ink-faint)',
          label: (byVenue.get(uuid)[0].venue_name || uuid).toString(),
        })),
        { color: 'var(--blocked)', label: 'withdrawn from sale, share of listed court-time', square: true },
      ]).replace('<div class="legend">', '<div class="legend" id="occ-legend">') +
      chart('chart'),
    caveats: caveatsOf(data),
    mount(root) {
      // Real legend swatches, coloured from the live theme.
      const legendEl = root.querySelector('#occ-legend');
      const swatches = legendEl.querySelectorAll('.legend__swatch');
      [...byVenue.keys()].forEach((uuid, i) => {
        if (swatches[i]) swatches[i].style.background = `var(--v${colorIndex(uuid) + 1})`;
      });

      // Strict and gross are two denominators over the same rows, so the lines
      // travel between them rather than the panel being rebuilt around them.
      const basisGroup = root.querySelector('#occ-basis');
      basisGroup.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-basis]');
        if (!btn || btn.dataset.basis === occupancyBasis) return;
        occupancyBasis = btn.dataset.basis;
        for (const other of basisGroup.querySelectorAll('[data-basis]')) {
          other.setAttribute('aria-pressed', String(other.dataset.basis === occupancyBasis));
        }
        morph(root.querySelector('.chart'));
      });

      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 6, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          formatter(params) {
            const date = params[0].axisValue;
            const rowsHere = [...byVenue.keys()]
              .map((uuid) => lookup.get(`${uuid}|${date}`))
              .filter(Boolean);
            const blocked = blockedTotals[dates.indexOf(date)];
            return tip({
              head: dayWithWeekday(date),
              rows: [
                ...rowsHere.map((r) => ({
                  color: venueColor(t, r.venue_uuid),
                  key: r.venue_name || r.venue_uuid,
                  value: `${pct(r.occupancy_strict, { places: 1 })} strict · ${pct(
                    r.occupancy_gross,
                    { places: 1 }
                  )} gross · ${num(r.booked_court_hours)} of ${num(r.sellable_court_hours)} h`,
                })),
                blocked
                  ? {
                      color: t.blocked,
                      key: 'withdrawn from sale',
                      value: `${pct(blockedShares[dates.indexOf(date)], { places: 0 })} of listed court-time (${num(blocked)} h)`,
                    }
                  : null,
              ],
              note: rowsHere.length
                ? null
                : 'No slot for this business date was observed — the line is broken rather than drawn through it.',
            });
          },
        },
        xAxis: {
          type: 'category',
          data: dates,
          boundaryGap: false,
          ...ax,
          axisLabel: {
            ...ax.axisLabel,
            formatter: (v) => dayLabel(v),
            hideOverlap: true,
          },
        },
        yAxis: percentAxis(t, occupancyBasis === 'strict' ? 'occupancy (strict)' : 'occupancy (gross)'),
        series: [
          {
            name: 'Withdrawn from sale',
            type: 'bar',
            data: blockedShares.map((v) => (v > 0 ? v : null)),
            barMaxWidth: 22,
            z: 1,
            silent: true,
            itemStyle: { color: t.blocked, opacity: 0.22, borderRadius: [3, 3, 0, 0] },
            animationDelay: (i) => Math.min(i * 6, 260),
          },
          ...[...byVenue.keys()].map((uuid) => {
            const name = byVenue.get(uuid)[0].venue_name || uuid;
            const data = dates.map((d) => {
              const row = lookup.get(`${uuid}|${d}`);
              if (!row) return null;
              return occupancyBasis === 'strict' ? row.occupancy_strict : row.occupancy_gross;
            });
            return brokenLine(name, data, venueColor(t, uuid), { z: 3 });
          }),
        ],
      }));
    },
  });
}

/* ==========================================================================
   2. Peak-hour heatmap
   ========================================================================== */

let heatmapTab = 'combined';

/**
 * The darkest cell on the grid, named.
 *
 * The ramp runs the full 0-100% so two venues can be compared cell for cell,
 * which means a grid where nothing ever passes a third reads as uniformly
 * pale. That is the finding. Saying where the top of the observed range
 * actually sits keeps the scale comparable and the picture readable at once.
 */
function busiestLabel(grid) {
  const rates = grid.cells
    .filter((c) => !c.sparse && c.occupancy_strict !== null)
    .map((c) => c.occupancy_strict);
  if (!rates.length) return 'no cell has enough court-time to claim a rate';
  return `busiest cell ${pct(Math.max(...rates), { places: 0 })}`;
}

export function renderHeatmap(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Peak hours', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !data.combined || !data.combined.cells.length) {
    return renderPanel(el, {
      title: 'Peak hours',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const views = [{ key: 'combined', label: 'All three', map: data.combined }].concat(
    (data.by_venue || []).map((h) => ({
      key: h.venue_uuid,
      label: h.venue_name || h.venue_uuid,
      map: h,
    }))
  );
  if (!views.some((v) => v.key === heatmapTab)) heatmapTab = 'combined';
  /** The grid currently on screen. Read at draw time so a tab can morph. */
  const current = () => views.find((v) => v.key === heatmapTab).map;
  const map = current();

  const aside = `<div class="segmented" role="tablist" aria-label="Heatmap scope" id="heat-tabs">
    ${views
      .map(
        (v) =>
          `<button class="segmented__btn" type="button" role="tab" data-heat="${esc(v.key)}"
            aria-selected="${v.key === heatmapTab}">${esc(v.label)}</button>`
      )
      .join('')}</div>`;

  renderPanel(el, {
    title: 'Peak hours',
    aside,
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range" id="heat-sparse">grey = fewer than ${int(
        map.sparse_min_minutes
      )} court-minutes seen in that cell</span>`
    ),
    body:
      `<div class="legend" id="heat-legend">
        <span class="ramp"><span>0%</span><span class="ramp__bar">
          <i style="background:var(--ramp-0)"></i><i style="background:var(--ramp-1)"></i>
          <i style="background:var(--ramp-2)"></i><i style="background:var(--ramp-3)"></i>
          <i style="background:var(--ramp-4)"></i><i style="background:var(--ramp-5)"></i>
        </span><span>100% booked</span></span>
        <span class="legend__item" id="heat-busiest">${esc(busiestLabel(map))}</span>
        <span class="legend__item"><span class="legend__swatch legend__swatch--sq"
          style="background:var(--ramp-null)"></span>too little observed to say</span>
        <span class="legend__item"><span class="legend__swatch legend__swatch--sq"
          style="background:transparent;box-shadow:inset 0 0 0 2px var(--blocked)"></span>some court-time withdrawn</span>
      </div>` +
      `<p class="scroll-hint">The grid is wider than this screen — scroll it sideways for the rest of the day.</p>
       <div class="scroll-x" tabindex="0" aria-label="Occupancy by hour of day and day of week, scrollable">${chart(
         'chart chart--tall'
       )}</div>`,
    caveats: caveatsOf(data),
    mount(root) {
      const chartEl = root.querySelector('.chart');
      const sparseEl = root.querySelector('#heat-sparse');
      const tabs = root.querySelector('#heat-tabs');

      /** Split one grid's cells into the cells that carry a rate and the ones
          that do not. The greyed set is a separate series so the colour ramp
          never has to pretend a thinly observed cell is a low one. */
      const split = (grid) => {
        const xi = new Map(grid.hours.map((h, i) => [h, i]));
        const yi = new Map(grid.days_of_week.map((d, i) => [d, i]));
        const solid = [];
        const greyed = [];
        for (const cell of grid.cells) {
          const x = xi.get(cell.hour);
          const y = yi.get(cell.day_of_week);
          if (x === undefined || y === undefined) continue;
          const unknown =
            cell.sparse || cell.sellable_minutes === 0 || cell.occupancy_strict === null;
          const item = { value: [x, y, unknown ? 0 : cell.occupancy_strict], cell };
          (unknown ? greyed : solid).push(item);
        }
        return { solid, greyed };
      };

      const fitWidth = (grid) => {
        chartEl.style.minWidth = `${Math.max(720, grid.hours.length * 38)}px`;
      };
      fitWidth(map);

      // Switching venue is the same grid holding a different venue's numbers,
      // so the cells recolour in place instead of the panel being rebuilt.
      tabs.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-heat]');
        if (!btn || btn.dataset.heat === heatmapTab) return;
        heatmapTab = btn.dataset.heat;
        for (const other of tabs.querySelectorAll('[data-heat]')) {
          other.setAttribute('aria-selected', String(other.dataset.heat === heatmapTab));
        }
        const grid = current();
        fitWidth(grid);
        if (sparseEl) {
          sparseEl.textContent = `grey = fewer than ${int(
            grid.sparse_min_minutes
          )} court-minutes seen in that cell`;
        }
        const busiestEl = root.querySelector('#heat-busiest');
        if (busiestEl) busiestEl.textContent = busiestLabel(grid);
        morph(chartEl);
      });

      mount(chartEl, (t, ax, opt) => {
      const grid = current();
      const days = grid.days_of_week.map((d) => DAY_ORDER[d] || String(d));
      const { solid, greyed } = split(grid);
      return ({
        ...opt,
        grid: { left: 6, right: 12, top: 8, bottom: 26, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          trigger: 'item',
          formatter(params) {
            const cell = params.data.cell;
            const unknown = cell.sparse || cell.sellable_minutes === 0 || cell.occupancy_strict === null;
            return tip({
              head: `${DAY_ORDER[cell.day_of_week]} ${hourLabel(cell.hour)}`,
              rows: [
                {
                  color: unknown ? t.rampNull : t.booked,
                  key: 'occupancy, strict',
                  value: unknown ? 'not enough observed' : pct(cell.occupancy_strict, { places: 1 }),
                },
                { key: 'booked', value: `${num(cell.booked_court_hours)} court-h` },
                { key: 'open', value: `${num(cell.open_court_hours)} court-h` },
                {
                  color: cell.blocked_court_hours > 0 ? t.blocked : null,
                  key: 'withdrawn',
                  value: `${num(cell.blocked_court_hours)} court-h`,
                },
                { key: 'business dates', value: int(cell.business_dates) },
              ],
              note: unknown
                ? `Fewer than ${int(
                    grid.sparse_min_minutes
                  )} court-minutes have been observed in this cell, so no rate is claimed for it.`
                : null,
            });
          },
        },
        xAxis: {
          type: 'category',
          data: grid.hours.map(hourLabel),
          splitArea: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: t.inkFaint, fontSize: 10, interval: 0 },
        },
        yAxis: {
          type: 'category',
          data: days,
          inverse: true,
          splitArea: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: t.inkSoft, fontSize: 11, fontWeight: 550 },
        },
        visualMap: {
          type: 'continuous',
          seriesIndex: 0,
          min: 0,
          max: 1,
          calculable: true,
          orient: 'horizontal',
          left: 'center',
          bottom: 0,
          itemWidth: 11,
          itemHeight: 90,
          show: false,
          inRange: { color: t.ramp },
        },
        series: [
          {
            name: 'occupancy',
            type: 'heatmap',
            data: solid.map((item) =>
              item.cell.blocked_minutes > 0
                ? { ...item, itemStyle: { borderColor: t.blocked, borderWidth: 2 } }
                : item
            ),
            itemStyle: { borderColor: t.surface, borderWidth: 1.5, borderRadius: 2 },
            emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 2 } },
            progressive: 0,
            // The grid fills in reading order and is done inside 400ms; the
            // cascade is short enough to read as one gesture, not a wipe.
            animationDelay: (i) => Math.min(i * 1.4, 360),
          },
          {
            name: 'too little observed',
            type: 'heatmap',
            data: greyed.map((item) =>
              item.cell.blocked_minutes > 0
                ? { ...item, itemStyle: { color: t.rampNull, borderColor: t.blocked, borderWidth: 2 } }
                : item
            ),
            itemStyle: { color: t.rampNull, borderColor: t.surface, borderWidth: 1.5, borderRadius: 2 },
            emphasis: { itemStyle: { borderColor: t.inkFaint, borderWidth: 2 } },
            progressive: 0,
          },
        ],
      });
      });
    },
  });
}

/* ==========================================================================
   11. Weekday vs weekend
   ========================================================================== */

export function renderWeekday(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Weekday vs weekend', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !data.weekday) {
    return renderPanel(el, {
      title: 'Weekday vs weekend',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const segments = [data.weekday, data.weekend];

  renderPanel(el, {
    title: 'Weekday vs weekend',
    denominator: denominatorLine(data.metric),
    body:
      statstrip([
        {
          name: 'Weekend against weekday',
          value: esc(signedPct(data.weekend_uplift)),
          wide: true,
          sub: 'difference in booked court-hours per trading day, not in total — the two segments have different numbers of days in them',
        },
      ]) + chart('chart chart--short'),
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter(params) {
            const seg = segments[params[0].dataIndex];
            return tip({
              head: params[0].axisValue,
              rows: [
                { color: t.booked, key: 'booked per day', value: `${num(seg.booked_court_hours_per_day)} court-h` },
                { color: t.open, key: 'on sale per day', value: `${num(seg.sellable_court_hours_per_day)} court-h` },
                { color: t.blocked, key: 'withdrawn per day', value: `${num(seg.blocked_court_hours_per_day)} court-h` },
                { key: 'occupancy, strict', value: pct(seg.occupancy_strict, { places: 1 }) },
                { key: 'business dates', value: int(seg.business_dates) },
              ],
            });
          },
        },
        xAxis: { type: 'category', data: ['Weekday', 'Weekend'], ...ax, splitLine: { show: false } },
        yAxis: {
          type: 'value',
          name: 'court-hours per trading day',
          nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'left' },
          ...ax,
        },
        series: [
          {
            name: 'booked',
            type: 'bar',
            data: segments.map((s) => s.booked_court_hours_per_day),
            itemStyle: { color: t.booked, borderRadius: [3, 3, 0, 0] },
            barMaxWidth: 54,
          },
          {
            name: 'withdrawn',
            type: 'bar',
            data: segments.map((s) => s.blocked_court_hours_per_day),
            itemStyle: { color: t.blocked, opacity: 0.65, borderRadius: [3, 3, 0, 0] },
            barMaxWidth: 54,
          },
        ],
      }));
    },
  });
}

/* ==========================================================================
   12. First slot to go each day
   ========================================================================== */

export function renderFirstSlot(el, payload) {
  if (!payload.ok)
    return renderPanel(el, { title: 'First slot to go', body: errorState(payload.error) });
  const data = payload.data;
  const rows = data.rows || [];

  if (data.empty || !rows.length) {
    return renderPanel(el, {
      title: 'First slot to go',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const counts = new Map();
  for (const row of rows) counts.set(row.slot_start_hour, (counts.get(row.slot_start_hour) || 0) + 1);
  const hoursSorted = [...counts.keys()].sort((a, b) => a - b);

  renderPanel(el, {
    title: 'First slot to go',
    denominator: denominatorLine(data.metric),
    body:
      `<p class="note note--lead">Which hour sells first on a given business
        date, counted across ${esc(int(rows.length))} dates that had any booking at all.</p>` +
      chart('chart chart--short'),
    caveats: caveatsOf(data),
    mount(root) {
      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 12, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter: (params) =>
            tip({
              head: `${params[0].axisValue} was first to sell`,
              rows: [{ color: t.booked, key: 'business dates', value: int(params[0].value) }],
            }),
        },
        xAxis: { type: 'category', data: hoursSorted.map(hourLabel), ...ax, splitLine: { show: false } },
        yAxis: { type: 'value', name: 'dates', nameTextStyle: { color: t.inkFaint, fontSize: 11 }, ...ax, minInterval: 1 },
        series: [
          {
            type: 'bar',
            data: hoursSorted.map((h) => counts.get(h)),
            itemStyle: { color: t.booked, borderRadius: [3, 3, 0, 0] },
            animationDelay: (i) => Math.min(i * 26, 320),
          },
        ],
      }));
    },
  });
}
