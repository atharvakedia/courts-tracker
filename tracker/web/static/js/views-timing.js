/**
 * When a booking arrives: how far ahead of the slot it lands, and how long a
 * peak slot sits on sale before it goes.
 *
 * Both measure a change between two polls, so both are known only to within one
 * polling cadence and both exclude the slots that were already booked the first
 * time the collector saw them. The panels say so rather than rounding it away.
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
  esc,
  hourLabel,
  hours as fmtHours,
  int,
  num,
} from './format.js';
import {
  mount,
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
  chart,
  statstrip,
} from './ui.js';

/* ==========================================================================
   3. Booking lead time
   ========================================================================== */

export function renderLeadTime(el, payload, { cadence }) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Booking lead time', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !data.overall || data.overall.n === 0) {
    return renderPanel(el, {
      title: 'Booking lead time',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const overall = data.overall;
  const byDay = new Map((data.by_day_of_week || []).map((d) => [d.day_name, d.stats]));
  const byPeak = new Map((data.by_peak || []).map((p) => [p.bucket, p.stats]));

  const rowsOrder = [
    ...DAY_ORDER.map((d) => ({ label: d, stats: byDay.get(d), kind: 'day' })),
    { label: '', stats: null, kind: 'spacer' },
    { label: 'Peak', stats: byPeak.get('peak'), kind: 'peak' },
    { label: 'Off-peak', stats: byPeak.get('off_peak') || byPeak.get('off-peak') || byPeak.get('offpeak'), kind: 'peak' },
  ];

  const samples = (data.distribution || [])
    .map((s) => s.lead_time_hours)
    .filter((v) => v !== null && v !== undefined);
  const censored = (data.distribution || []).filter((s) => s.censored_left).length;

  renderPanel(el, {
    title: 'Booking lead time',
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range">peak hours ${(data.peak_hours || []).map(hourLabel).join(', ')}</span>`
    ),
    body:
      statstrip([
        {
          name: 'Median lead time',
          value: esc(fmtHours(overall.median_hours)),
          sub: `half of observed bookings landed inside this`,
        },
        {
          name: '90th percentile',
          value: esc(fmtHours(overall.p90_hours)),
          sub: 'nine in ten landed inside this',
        },
        { name: 'Bookings measured', value: esc(int(overall.n)), sub: `range ${fmtHours(overall.min_hours)} – ${fmtHours(overall.max_hours)}` },
        {
          name: 'Excluded',
          value: esc(int(overall.excluded_censored)),
          sub: 'already booked when first seen, so their lead time is unknowable and they are left out rather than guessed',
        },
        {
          name: 'Precision',
          value: `±${esc(int(cadence))}`,
          unit: 'min',
          wide: true,
          sub: `A booking is only ever seen at the next poll, so every figure here is one polling gap wide. None of them is exact.`,
        },
      ]) +
      chart('chart chart--tall') +
      `<p class="note">Distribution of the
        ${esc(int(samples.length))} measured lead times. The
        ${esc(int(censored))} slots that were already booked the first time the collector saw them
        are not in it.</p>` +
      chart('chart chart--short'),
    caveats: caveatsOf(data),
    mount(root) {
      const [rangeEl, histEl] = root.querySelectorAll('.chart');

      mount(rangeEl, (t, ax, opt) => {
        const cats = rowsOrder.map((r) => r.label);
        const pick = (fn) => rowsOrder.map((r) => (r.stats && r.stats.n ? fn(r.stats) : null));
        return {
          ...opt,
          grid: { left: 4, right: 18, top: 34, bottom: 4, containLabel: true },
          tooltip: {
            ...opt.tooltip,
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter(params) {
              const label = params[0].axisValue;
              const row = rowsOrder.find((r) => r.label === label);
              if (!row || !row.stats || !row.stats.n)
                return tip({ head: label || '—', rows: [{ key: 'bookings measured', value: '0' }] });
              const s = row.stats;
              return tip({
                head: label,
                rows: [
                  { color: t.booked, key: 'median', value: fmtHours(s.median_hours) },
                  { color: t.open, key: '90th percentile', value: fmtHours(s.p90_hours) },
                  { key: 'earliest / latest', value: `${fmtHours(s.min_hours)} – ${fmtHours(s.max_hours)}` },
                  { key: 'n', value: int(s.n) },
                  { key: 'excluded as censored', value: int(s.excluded_censored) },
                ],
                note: `Each figure is known to ±${int(cadence)} minutes.`,
              });
            },
          },
          xAxis: {
            type: 'value',
            name: 'hours before the slot starts',
            nameLocation: 'end',
            nameGap: 14,
            nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'right' },
            ...ax,
            axisLine: { show: false },
          },
          yAxis: {
            type: 'category',
            data: cats,
            // Monday at the top, as in the peak-hour grid: the two panels are
            // read against each other and a flipped week is a real misread.
            inverse: true,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: {
              color: t.inkSoft,
              fontSize: 11,
              fontWeight: 550,
              formatter: (v) => v || '',
            },
          },
          series: [
            {
              name: 'base',
              type: 'bar',
              stack: 'span',
              silent: true,
              itemStyle: { color: 'transparent' },
              data: pick((s) => s.min_hours),
              barMaxWidth: 15,
            },
            {
              name: 'to the median',
              type: 'bar',
              stack: 'span',
              data: pick((s) => Math.max(0, s.median_hours - s.min_hours)),
              itemStyle: { color: t.open, borderRadius: [3, 0, 0, 3] },
              barMaxWidth: 15,
            },
            {
              name: 'median to 90th',
              type: 'bar',
              stack: 'span',
              data: pick((s) => Math.max(0, s.p90_hours - s.median_hours)),
              itemStyle: { color: t.booked },
              barMaxWidth: 15,
            },
            {
              name: 'the last tenth',
              type: 'bar',
              stack: 'span',
              data: pick((s) => Math.max(0, s.max_hours - s.p90_hours)),
              itemStyle: { color: t.open, opacity: 0.45, borderRadius: [0, 3, 3, 0] },
              barMaxWidth: 15,
            },
            {
              name: 'median',
              type: 'scatter',
              symbol: 'diamond',
              symbolSize: 9,
              silent: true,
              z: 6,
              itemStyle: { color: t.surface, borderColor: t.ink, borderWidth: 1.6 },
              data: rowsOrder
                .map((r, i) => (r.stats && r.stats.n ? [r.stats.median_hours, i] : null))
                .filter(Boolean),
            },
          ],
        };
      });

      // A plain histogram of the measured lead times. Binning is presentation;
      // no rate is computed here.
      const maxHours = samples.length ? Math.max(...samples) : 0;
      const binCount = 28;
      const binWidth = maxHours > 0 ? maxHours / binCount : 1;
      const bins = new Array(binCount).fill(0);
      for (const v of samples) {
        const i = Math.min(binCount - 1, Math.floor(v / binWidth));
        bins[i] += 1;
      }

      mount(histEl, (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 10, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter: (params) =>
            tip({
              head: `${fmtHours(params[0].dataIndex * binWidth)} – ${fmtHours((params[0].dataIndex + 1) * binWidth)} ahead`,
              rows: [{ color: t.booked, key: 'bookings', value: int(params[0].value) }],
            }),
        },
        xAxis: {
          type: 'category',
          data: bins.map((_, i) => i),
          ...ax,
          axisLabel: {
            ...ax.axisLabel,
            interval: Math.ceil(binCount / 7),
            formatter: (v) => fmtHours(Number(v) * binWidth),
          },
          splitLine: { show: false },
        },
        yAxis: { type: 'value', name: 'bookings', nameTextStyle: { color: t.inkFaint, fontSize: 11 }, ...ax },
        series: [
          {
            type: 'bar',
            data: bins,
            itemStyle: { color: t.booked, borderRadius: [2, 2, 0, 0] },
            barCategoryGap: '18%',
            animationDelay: (i) => Math.min(i * 12, 340),
          },
        ],
      }));
    },
  });
}

/* ==========================================================================
   4. Time to sell out
   ========================================================================== */

export function renderSellout(el, payload, { cadence }) {
  if (!payload.ok)
    return renderPanel(el, { title: 'Time to sell out', body: errorState(payload.error) });
  const data = payload.data;

  if (data.empty || !data.n) {
    return renderPanel(el, {
      title: 'Time to sell out',
      denominator: denominatorLine(data.metric),
      body: emptyState(data.reason),
      caveats: caveatsOf(data),
    });
  }

  const values = (data.records || []).map((r) => r.hours_to_sellout);

  renderPanel(el, {
    title: 'Time to sell out',
    denominator: denominatorLine(
      data.metric,
      `<span class="den-range">peak hours ${(data.peak_hours || []).map(hourLabel).join(', ')}</span>`
    ),
    body:
      statstrip([
        { name: 'Median time on sale', value: esc(fmtHours(data.median_hours)), sub: 'from first seen open to first seen booked' },
        { name: '90th percentile', value: esc(fmtHours(data.p90_hours)) },
        { name: 'Peak slots measured', value: esc(int(data.n)) },
        {
          name: 'Excluded',
          value: esc(int(data.excluded_censored + data.excluded_short_window + data.excluded_off_peak)),
          wide: true,
          sub: `${int(data.excluded_censored)} already booked when first seen, ${int(
            data.excluded_short_window
          )} watched for less than ${num(data.min_observation_hours, { places: 0 })} hours, ${int(
            data.excluded_off_peak
          )} outside peak hours. Each is known to ±${int(cadence)} minutes.`,
        },
      ]) + chart('chart chart--short'),
    caveats: caveatsOf(data),
    mount(root) {
      const maxHours = values.length ? Math.max(...values) : 1;
      const binCount = 24;
      const binWidth = maxHours / binCount || 1;
      const bins = new Array(binCount).fill(0);
      for (const v of values) bins[Math.min(binCount - 1, Math.floor(v / binWidth))] += 1;

      mount(root.querySelector('.chart'), (t, ax, opt) => ({
        ...opt,
        grid: { left: 4, right: 10, top: 34, bottom: 4, containLabel: true },
        tooltip: {
          ...opt.tooltip,
          axisPointer: { type: 'shadow' },
          formatter: (params) =>
            tip({
              head: `sold after ${fmtHours(params[0].dataIndex * binWidth)} – ${fmtHours(
                (params[0].dataIndex + 1) * binWidth
              )} on sale`,
              rows: [{ color: t.booked, key: 'peak slots', value: int(params[0].value) }],
            }),
        },
        xAxis: {
          type: 'category',
          data: bins.map((_, i) => i),
          ...ax,
          splitLine: { show: false },
          axisLabel: { ...ax.axisLabel, interval: 3, formatter: (v) => fmtHours(Number(v) * binWidth) },
        },
        yAxis: { type: 'value', name: 'peak slots', nameTextStyle: { color: t.inkFaint, fontSize: 11 }, ...ax },
        series: [
          {
            type: 'bar',
            data: bins,
            itemStyle: { color: t.booked, borderRadius: [2, 2, 0, 0] },
            animationDelay: (i) => Math.min(i * 14, 320),
          },
        ],
      }));
    },
  });
}
