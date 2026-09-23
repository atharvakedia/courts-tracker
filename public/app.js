/* Hudle tracker dashboard: one fetch per view (/api/overview), drawn in place. */
(function () {
  'use strict';
  const state = { sport: 'padel', window: '7', panel: 'venues', data: null };
  try {
    const saved = JSON.parse(localStorage.getItem('ht.state') || '{}');
    Object.assign(state, { sport: saved.sport || 'padel', window: saved.window || '7' });
  } catch (_) {}
  // A shared link wins over what this browser last looked at.
  const q = new URLSearchParams(location.search);
  if (['padel', 'pickleball'].includes(q.get('sport'))) state.sport = q.get('sport');
  if (['7', '30', 'all'].includes(q.get('window'))) state.window = q.get('window');

  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const pct = (x, d = 0) => (x == null ? '—' : `${(100 * x).toFixed(d)}%`);
  const num = (x) => (x == null ? '—' : x.toLocaleString('en-IN', { maximumFractionDigits: 1 }));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const charts = {};

  function chart(id) {
    if (!charts[id]) {
      charts[id] = echarts.init($(id), null, { renderer: 'canvas' });
      new ResizeObserver(() => charts[id].resize()).observe($(id));
    }
    return charts[id];
  }

  function axis() {
    const ink = css('--ink-3'), rule = css('--rule');
    return {
      axisLine: { lineStyle: { color: rule } }, axisTick: { show: false },
      axisLabel: { color: ink, fontSize: 11 }, splitLine: { lineStyle: { color: rule } },
    };
  }

  function segmented(id, key) {
    const root = $(id);
    const sync = () => root.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === state[key])));
    root.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b || b.dataset.v === state[key]) return;
      state[key] = b.dataset.v;
      sync();
      try { localStorage.setItem('ht.state', JSON.stringify({ sport: state.sport, window: state.window })); } catch (_) {}
      load();
    });
    sync();
  }

  function tabs() {
    const root = $('tabs');
    const sync = () => {
      root.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.p === state.panel)));
      document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('on', p.dataset.p === state.panel));
      requestAnimationFrame(() => Object.values(charts).forEach((c) => c.resize()));
    };
    root.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) { state.panel = b.dataset.p; sync(); }
    });
    sync();
  }

  async function freshness() {
    const el = $('fresh');
    try {
      const h = await (await fetch('/api/health')).json();
      el.dataset.s = h.status;
      if (!h.last_run) { el.lastElementChild.textContent = 'No data yet'; return; }
      const at = new Date(h.last_run.finished_at || h.last_run.started_at);
      const hrs = Math.round((Date.now() - at) / 36e5);
      el.lastElementChild.textContent = h.status === 'running' ? 'Updating now' : `Updated ${hrs < 1 ? '<1' : hrs}h ago`;
      el.title = `${h.last_run.courts_ok} courts polled, ${h.last_run.courts_failed} failed`;
    } catch (_) { el.dataset.s = 'stale'; el.lastElementChild.textContent = 'Offline'; }
  }

  async function load() {
    document.body.style.cursor = 'progress';
    try {
      const r = await fetch(`/api/overview?sport=${state.sport}&window=${state.window}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.data = await r.json();
      draw(state.data);
    } catch (err) {
      $('venue-list').innerHTML = `<p class="empty">Could not load: ${esc(err.message)}</p>`;
    } finally { document.body.style.cursor = ''; }
  }

  function draw(d) {
    const w = d.window, range = `${w.start} → ${w.end}`;
    const lead = d.lead_time, t = d.totals;
    $('kpis').innerHTML = [
      ['Occupancy', pct(t.occupancy, 1), `booked ÷ all court-hours · venue blocks count as booked`],
      ['Booked court-hours', num(t.booked_hours), `of ${num(t.total_hours)} listed`],
      ['Booked ahead (median)', lead.median_hours == null ? '—' : `${num(lead.median_hours)} h`, lead.n ? `90% within ${num(lead.p90_hours)} h · ${lead.n} bookings` : 'no customer bookings yet'],
      ['Courts counted', `${t.courts_counted} / ${t.courts_tracked}`, 'counted / tracked · listings excluded'],
    ].map(([n, v, s]) => `<article class="kpi"><p class="name">${n}</p><p class="val">${v}</p><p class="sub">${esc(s)}</p></article>`).join('');

    $('den-venues').textContent = `booked ÷ all court-hours · ${range}`;
    $('den-trend').textContent = `counted courts · ${range}`;
    $('den-heat').textContent = `occupancy by Jaipur hour and weekday · counted courts · ${range}`;
    $('den-lead').textContent = `hours between booking and play · customer bookings · ${range}`;

    venues(d.venues);
    trend(d);
    heat(d.heatmap);
    leadHist(d.lead_time_hours);
  }

  function venues(list) {
    const host = $('venue-list');
    if (!list.length) { host.innerHTML = '<p class="empty">No settled days in this window yet.</p>'; return; }
    host.innerHTML = list.map((v) => {
      const reasons = v.courts.flatMap((c) => c.reasons.map((r) => `${c.name}: ${r}`)).join('\n');
      const meta = `${num(v.booked_hours)} of ${num(v.total_hours)} court-h · ${v.courts.length} court${v.courts.length === 1 ? '' : 's'}${v.price_per_hour ? ` · ₹${num(v.price_per_hour)}/h` : ''}`;
      return `<div class="row" data-v="${v.verdict}" title="${esc(reasons)}">
        <span class="nm">${esc(v.name)}${v.new ? '<span class="tag new">NEW</span>' : ''}${v.verdict === 'partial' ? '<span class="tag v">low activity</span>' : v.verdict === 'unreliable' ? '<span class="tag v">listing only</span>' : ''}</span>
        <span class="pct">${pct(v.occupancy)}</span>
        <div class="track"><div class="fill" style="background:var(--${v.verdict === 'no_data' ? 'unreliable' : v.verdict})" data-w="${100 * (v.occupancy || 0)}"></div></div>
        <span class="meta">${esc(meta)}</span></div>`;
    }).join('');
    requestAnimationFrame(() => host.querySelectorAll('.fill').forEach((f) => { f.style.width = `${f.dataset.w}%`; }));
  }

  function trend(d) {
    const byDate = {};
    d.daily.forEach((r) => { (byDate[r.business_date] ||= { b: 0, t: 0 }); byDate[r.business_date].b += r.booked_hours; byDate[r.business_date].t += r.total_hours; });
    const dates = Object.keys(byDate).sort();
    const names = Object.fromEntries(d.venues.map((v) => [v.venue_uuid, v.name]));
    const venues = [...new Set(d.daily.map((r) => r.venue_uuid))];
    const perVenue = d.sport === 'padel' && venues.length <= 4;
    const palette = [css('--accent'), '#7c5cff', '#e36209', '#2f80ed'];
    const series = perVenue
      ? venues.map((u, i) => ({
          name: names[u] || u, type: 'line', smooth: .3, symbolSize: 6, lineStyle: { width: 2 }, itemStyle: { color: palette[i % 4] },
          data: dates.map((dt) => { const r = d.daily.find((x) => x.venue_uuid === u && x.business_date === dt); return r ? +(100 * r.occupancy).toFixed(1) : null; }),
        }))
      : [{ name: 'All counted courts', type: 'line', smooth: .3, symbolSize: 6, lineStyle: { width: 2 }, itemStyle: { color: css('--accent') },
           areaStyle: { color: css('--accent-2'), opacity: .35 },
           data: dates.map((dt) => byDate[dt].t ? +(100 * byDate[dt].b / byDate[dt].t).toFixed(1) : null) }];
    if (!dates.length) { chart('c-trend').clear(); return; }
    chart('c-trend').setOption({
      animationDuration: 500, grid: { left: 36, right: 12, top: perVenue ? 28 : 12, bottom: 22 },
      legend: perVenue ? { top: 0, textStyle: { color: css('--ink-2'), fontSize: 11 }, icon: 'roundRect', itemWidth: 12, itemHeight: 4 } : { show: false },
      tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : `${v}%`) },
      xAxis: { type: 'category', data: dates.map((x) => x.slice(5)), ...axis(), splitLine: { show: false } },
      yAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%', color: css('--ink-3'), fontSize: 11 }, splitLine: { lineStyle: { color: css('--rule') } } },
      series,
    }, true);
  }

  function heat(cells) {
    if (!cells.length) { chart('c-heat').clear(); return; }
    const hours = [...new Set(cells.map((c) => c.hour))].sort((a, b) => a - b);
    const data = cells.map((c) => [hours.indexOf(c.hour), c.weekday, c.occupancy == null ? null : +(100 * c.occupancy).toFixed(0)]);
    chart('c-heat').setOption({
      animationDuration: 400, grid: { left: 36, right: 8, top: 6, bottom: 40 },
      tooltip: { formatter: (p) => `${DAYS[p.value[1]]} ${String(hours[p.value[0]]).padStart(2, '0')}:00 — ${p.value[2]}% booked` },
      xAxis: { type: 'category', data: hours.map((h) => `${h}`), ...axis(), splitLine: { show: false } },
      yAxis: { type: 'category', data: DAYS, inverse: true, ...axis(), splitLine: { show: false } },
      visualMap: { min: 0, max: 100, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 90, itemWidth: 8,
                   textStyle: { color: css('--ink-3'), fontSize: 10 }, inRange: { color: [css('--panel'), css('--accent-2'), css('--accent')] }, text: ['100%', '0%'] },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: css('--panel'), borderWidth: 2, borderRadius: 3 } }],
    }, true);
  }

  function leadHist(hours) {
    if (!hours.length) { chart('c-lead').clear(); return; }
    const edges = [0, 3, 6, 12, 24, 48, 72, 168, Infinity];
    const labels = ['<3h', '3–6h', '6–12h', '12–24h', '1–2d', '2–3d', '3–7d', '7d+'];
    const counts = labels.map((_, i) => hours.filter((h) => h >= edges[i] && h < edges[i + 1]).length);
    chart('c-lead').setOption({
      animationDuration: 500, grid: { left: 36, right: 8, top: 10, bottom: 22 },
      tooltip: { trigger: 'axis', valueFormatter: (v) => `${v} bookings` },
      xAxis: { type: 'category', data: labels, ...axis(), splitLine: { show: false } },
      yAxis: { type: 'value', ...axis() },
      series: [{ type: 'bar', data: counts, barMaxWidth: 28, itemStyle: { color: css('--accent'), borderRadius: [4, 4, 0, 0] } }],
    }, true);
  }

  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ht.theme', next); } catch (_) {}
    if (state.data) draw(state.data);
  });

  segmented('sport', 'sport');
  segmented('window', 'window');
  tabs();
  freshness();
  load();
})();
