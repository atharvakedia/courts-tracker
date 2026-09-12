/**
 * ECharts plumbing: one registry of live instances, one theme switch, one
 * resize observer, and a tooltip rendered from our own CSS classes so the
 * charts look like the rest of the page rather than like a chart library.
 *
 * Charts never compute a metric. They receive court-hours and ratios that
 * Python already derived and only decide how to draw them.
 */

import { prefersReducedMotion, tokens, onThemeChange } from './theme.js';
import { esc } from './format.js';

const instances = new Map();
let observer = null;

function ensureObserver() {
  if (observer || typeof ResizeObserver === 'undefined') return;
  observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const chart = instances.get(entry.target.id);
      if (chart && !chart.isDisposed()) chart.resize();
    }
  });
}

/**
 * Base option shared by every chart: our fonts, our greys, our motion budget.
 *
 * The durations match the page's own: things arrive over 620ms and settle,
 * and a data change morphs over 450ms rather than redrawing, because a filter
 * change is the same chart holding different numbers. A canvas is untouched by
 * the stylesheet's reduced-motion rule, so the flag has to be read here too.
 */
function base() {
  const t = tokens();
  const reduced = prefersReducedMotion();
  return {
    backgroundColor: 'transparent',
    animation: !reduced,
    animationDuration: 620,
    animationEasing: 'cubicOut',
    animationDurationUpdate: 450,
    animationEasingUpdate: 'cubicInOut',
    textStyle: { fontFamily: t.font, color: t.inkSoft, fontSize: 12 },
    grid: { left: 8, right: 12, top: 30, bottom: 6, containLabel: true },
    tooltip: {
      trigger: 'axis',
      confine: true,
      appendTo: 'body',
      backgroundColor: t.surface,
      borderColor: t.line,
      borderWidth: 1,
      padding: [10, 12],
      transitionDuration: 0.18,
      extraCssText:
        'border-radius:10px;box-shadow:0 1px 2px rgb(0 0 0 / .05), 0 14px 30px -16px rgb(0 0 0 / .4);',
      textStyle: { color: t.ink, fontFamily: t.font, fontSize: 12 },
    },
  };
}

function axisStyle(t) {
  return {
    axisLine: { show: true, lineStyle: { color: t.line } },
    axisTick: { show: false },
    axisLabel: { color: t.inkFaint, fontSize: 11, margin: 10 },
    splitLine: { show: true, lineStyle: { color: t.line, type: [3, 4] } },
  };
}

/**
 * Build (or rebuild) a chart in `el`. `builder(tokens, axisStyle)` returns the
 * option; it is called again on every theme change so colours follow the page.
 */
export function mount(el, builder) {
  if (!el || typeof echarts === 'undefined') return null;
  if (!el.id) el.id = `chart-${Math.random().toString(36).slice(2, 9)}`;

  let chart = instances.get(el.id);
  if (chart && chart.isDisposed()) chart = null;
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    instances.set(el.id, chart);
    ensureObserver();
    if (observer) observer.observe(el);
    chart.__builder = builder;
  } else {
    chart.__builder = builder;
  }

  const t = tokens();
  const option = builder(t, axisStyle(t), base());
  chart.setOption(option, { notMerge: true });
  return chart;
}

/** Rebuild an existing chart from scratch — used when its structure changed. */
export function refresh(el) {
  const chart = el && instances.get(el.id);
  if (!chart || chart.isDisposed() || !chart.__builder) return;
  const t = tokens();
  chart.setOption(chart.__builder(t, axisStyle(t), base()), { notMerge: true });
}

/**
 * Re-run a chart's builder and let ECharts tween between the two options.
 *
 * This is what a denominator toggle or a venue tab does: the chart is the same
 * chart, holding a different set of numbers, so the marks move to their new
 * positions instead of the panel being torn down and rebuilt. The builder
 * closes over whichever variable the control changed, so nothing has to be
 * threaded through.
 */
export function morph(el) {
  const chart = el && instances.get(el.id);
  if (!chart || chart.isDisposed() || !chart.__builder) return;
  const t = tokens();
  chart.setOption(chart.__builder(t, axisStyle(t), base()), { notMerge: false });
}

export function disposeIn(root) {
  if (!root) return;
  for (const el of root.querySelectorAll('[id^="chart-"]')) {
    const chart = instances.get(el.id);
    if (chart && !chart.isDisposed()) chart.dispose();
    instances.delete(el.id);
    if (observer) observer.unobserve(el);
  }
}

let pending = false;
onThemeChange(() => {
  // One toggle re-themes every panel; coalesce so a double-tap is one relayout.
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    const t = tokens();
    for (const chart of instances.values()) {
      if (chart.isDisposed() || !chart.__builder) continue;
      chart.setOption(chart.__builder(t, axisStyle(t), base()), { notMerge: true });
    }
  });
});

/* A window resize that matters reaches the chart as an element resize, which
   the ResizeObserver above already handles. A second listener here would make
   every drag of the window edge resize each chart twice. */

/* -------------------------------------------------------------------------
   Tooltip rendering — our markup, our classes, our honesty notes.
   ------------------------------------------------------------------------- */

export function tip({ head, rows, note }) {
  const body = (rows || [])
    .filter(Boolean)
    .map(
      (row) =>
        `<div class="tip__row"><span class="tip__key">${
          row.color
            ? `<span class="tip__ind" style="background:${esc(row.color)}"></span>`
            : ''
        }${esc(row.key)}</span><span class="tip__val">${esc(row.value)}</span></div>`
    )
    .join('');
  return `<div class="tip"><div class="tip__head">${esc(head)}</div>${body}${
    note ? `<div class="tip__note">${esc(note)}</div>` : ''
  }</div>`;
}

/* -------------------------------------------------------------------------
   Shared series helpers.
   ------------------------------------------------------------------------- */

/** A line that stops at a gap instead of drawing through it. */
export function brokenLine(name, data, color, extra = {}) {
  return {
    name,
    type: 'line',
    data,
    connectNulls: false,
    showSymbol: false,
    symbol: 'circle',
    symbolSize: 6,
    smooth: false,
    lineStyle: { width: 2, color },
    itemStyle: { color },
    emphasis: { focus: 'series', lineStyle: { width: 3 } },
    ...extra,
  };
}

export function percentAxis(t, name) {
  return {
    type: 'value',
    name,
    nameLocation: 'end',
    nameGap: 16,
    nameTextStyle: { color: t.inkFaint, fontSize: 11, align: 'left' },
    min: 0,
    max: 1,
    axisLabel: {
      color: t.inkFaint,
      fontSize: 11,
      formatter: (v) => `${Math.round(v * 100)}%`,
    },
    axisLine: { show: false },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: t.line, type: [3, 4] } },
  };
}
