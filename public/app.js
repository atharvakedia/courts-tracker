/* Courts tracker dashboard: one fetch per view (/api/overview), drawn in place.
   A view is sport x window x (optionally) one venue; every chart and headline
   figure answers for the same view, and the URL carries it. */
(function () {
  'use strict';
  // Versioned so a changed default reaches viewers who saved the old one.
  const STATE_KEY = 'ht.state.v2';
  const state = {
    sport: 'padel', window: '7', venue: null, panel: 'venues', data: null, filter: '',
    views: { day: 'rev', hour: 'pct', week: 'grid', metric: 'demand' },
  };
  try {
    const saved = JSON.parse(localStorage.getItem(STATE_KEY) || '{}');
    state.sport = saved.sport || state.sport;
    state.window = saved.window || state.window;
    Object.assign(state.views, saved.views || {});
    if (!['supply', 'demand'].includes(state.views.metric)) state.views.metric = 'demand';
  } catch (_) {}
  // A shared link wins over what this browser last looked at.
  const q = new URLSearchParams(location.search);
  if (['padel', 'pickleball'].includes(q.get('sport'))) state.sport = q.get('sport');
  if (['7', '30', 'all'].includes(q.get('window'))) state.window = q.get('window');
  if (q.get('venue')) state.venue = q.get('venue');
  // Which panel a phone opens on; desktop shows them all.
  if (['venues', 'day', 'hour', 'week', 'map'].includes(q.get('panel'))) state.panel = q.get('panel');

  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const pct = (x, d = 0) => (x == null ? '—' : `${(100 * x).toFixed(d)}%`);
  const num = (x, d = 0) => (x == null ? '—' : x.toLocaleString('en-IN', { maximumFractionDigits: d }));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const LONG_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parseDay = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d)); };
  const fmtDay = (iso) => { const t = parseDay(iso); return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`; };
  const wdOf = (iso) => (parseDay(iso).getUTCDay() + 6) % 7;
  const hh = (h) => `${String(h).padStart(2, '0')}:00`;
  const charts = {};

  // ---------- chart plumbing ----------
  function chart(id) {
    if (!charts[id]) {
      charts[id] = echarts.init($(id), null, { renderer: 'canvas' });
      new ResizeObserver(() => charts[id].resize()).observe($(id));
    }
    return charts[id];
  }

  function theme() {
    return {
      ink: css('--ink'), ink2: css('--ink-2'), ink3: css('--ink-3'), rule: css('--rule'), axis: css('--axis'),
      panel: css('--panel'), accent: css('--accent'), hudle: css('--s-hudle'), block: css('--s-block'),
      vacant: css('--s-vacant'), seq: [0, 1, 2, 3, 4].map((i) => css(`--seq-${i}`)),
      dem: [1, 2, 3, 4].map((i) => css(`--dem-${i}`)),
    };
  }

  function base(t) {
    return {
      animationDuration: 450, animationDurationUpdate: 350,
      aria: { enabled: true },
      textStyle: { fontFamily: css('--font') },
      tooltip: {
        backgroundColor: t.panel, borderColor: t.rule, borderWidth: 1, padding: [8, 10],
        textStyle: { color: t.ink, fontSize: 12 }, extraCssText: 'border-radius:8px;box-shadow:0 4px 16px rgb(0 0 0 / .12);',
        axisPointer: { type: 'shadow', shadowStyle: { color: t.rule, opacity: 0.35 } },
      },
    };
  }

  function xCat(t, data, extra = {}) {
    return {
      type: 'category', data, axisTick: { show: false }, axisLine: { lineStyle: { color: t.axis } },
      axisLabel: { color: t.ink3, fontSize: 11, hideOverlap: true }, splitLine: { show: false }, ...extra,
    };
  }

  function yVal(t, extra = {}) {
    return {
      type: 'value', axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: t.ink3, fontSize: 11 }, splitLine: { lineStyle: { color: t.rule } }, ...extra,
    };
  }

  const yPct = (t, max) => yVal(t, { min: 0, max, axisLabel: { color: t.ink3, fontSize: 11, formatter: '{value}%' } });

  function legend(t, names) {
    return { top: 0, left: 0, data: names, icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 14, textStyle: { color: t.ink2, fontSize: 11 } };
  }

  // A reference line for the view's overall figure: solid hairline, labelled once.
  function avgLine(t, value, label) {
    return {
      silent: true, symbol: 'none', lineStyle: { color: t.ink3, type: 'solid', width: 1 },
      // Outside the plot, right of the line's end: never on top of a bar or its label.
      label: { position: 'end', distance: 4, color: t.ink2, fontSize: 11, formatter: label },
      data: [{ yAxis: value }],
    };
  }

  // A scale that leaves headroom above the tallest bar and ends on a clean step.
  const ceilPct = (vals) => Math.min(100, Math.max(10, Math.ceil((Math.max(0, ...vals) * 1.15) / 10) * 10));

  // Booked and vacant are the court time offered; withBlocked adds the venue's
  // blocked time on top as its own segment, outside the % booked.
  function bookedVacant(t, rows, cats, div = () => 1, withBlocked = false) {
    const s = (name, key, color, top) => ({
      name, type: 'bar', stack: 'h', barMaxWidth: 24, barCategoryGap: '30%',
      itemStyle: { color, borderRadius: top ? [4, 4, 0, 0] : 0, borderColor: t.panel, borderWidth: 1 },
      emphasis: { focus: 'series' },
      data: rows.map((r) => +(r[key] / div(r)).toFixed(1)),
    });
    return [
      s('Booked', 'booked_hours', t.hudle, false),
      s('Vacant', 'vacant_hours', t.vacant, !withBlocked),
      ...(withBlocked ? [s('Venue block', 'blocked_hours', t.block, true)] : []),
    ];
  }

  function stackTip(title, rows, div = () => 1, unit = 'court-h', withBlocked = false) {
    return (ps) => {
      const r = rows[ps[0].dataIndex];
      const d = div(r);
      const line = (k, color, label) => `<div style="display:flex;gap:10px;justify-content:space-between"><span><span class="key" style="background:${color}"></span>${label}</span><b>${num(r[k] / d, 1)}</b></div>`;
      return `<div style="min-width:170px"><b>${title(r)}</b>
        ${line('booked_hours', ps[0].color, 'Booked')}
        ${line('vacant_hours', ps[1].color, 'Vacant')}
        ${withBlocked ? line('blocked_hours', ps[2].color, 'Venue block') : ''}
        <div style="color:${css('--ink-3')};margin-top:4px">${pct(r.occupancy)} booked · ${unit}${r.blocked_hours ? `<br/>venue blocks are not counted as booked` : ''}</div></div>`;
    };
  }

  function setChart(id, option, summary) {
    const c = chart(id);
    c.setOption(option, true);
    $(id).setAttribute('aria-label', summary);
  }

  // ---------- controls ----------
  function persist() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify({ sport: state.sport, window: state.window, views: state.views })); } catch (_) {}
    const p = new URLSearchParams({ sport: state.sport, window: state.window });
    if (state.venue) p.set('venue', state.venue);
    history.replaceState(null, '', `?${p}`);
  }

  function segmented(id, key) {
    const root = $(id);
    const sync = () => root.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === state[key])));
    root.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b || b.dataset.v === state[key]) return;
      state[key] = b.dataset.v;
      if (key === 'sport') { state.venue = null; state.filter = ''; $('search').value = ''; }
      sync();
      load();
    });
    sync();
  }

  function minis() {
    document.querySelectorAll('.mini').forEach((root) => {
      const k = root.dataset.k;
      const sync = () => root.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === state.views[k])));
      root.addEventListener('click', (e) => {
        const b = e.target.closest('button');
        if (!b || b.dataset.v === state.views[k]) return;
        state.views[k] = b.dataset.v;
        sync();
        persist();
        if (state.data) drawPanel(k, state.data);
      });
      sync();
    });
  }

  function showPanel(p) {
    state.panel = p;
    $('tabs').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.p === p)));
    document.querySelectorAll('.panel[data-p]').forEach((el) => el.classList.toggle('on', el.dataset.p === p));
    requestAnimationFrame(() => Object.values(charts).forEach((c) => c.resize()));
  }

  function tabs() {
    $('tabs').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) showPanel(b.dataset.p);
    });
    showPanel(state.panel);
  }

  function selectVenue(uuid) {
    state.venue = uuid === state.venue ? null : uuid;
    // On a phone the list and the charts share one slot: jump to the charts.
    if (state.venue && matchMedia('(max-width: 900px)').matches) showPanel('day');
    load();
  }

  // The views' build time, sent with every view request: a rebuild (the daily
  // pass, or a block saved here) then asks the edge for a new URL instead of
  // an hour-old copy. The newest one this tab has seen wins.
  const REV_KEY = 'ht.rev';
  let rev = null;
  function setRev(r) {
    if (!r || (rev && Date.parse(r) <= Date.parse(rev))) return;
    rev = r;
    try { sessionStorage.setItem(REV_KEY, r); } catch (_) {}
  }
  try { setRev(sessionStorage.getItem(REV_KEY)); } catch (_) {}
  const health = fetch('/api/health').then((r) => r.json());

  async function freshness() {
    const el = $('fresh');
    try {
      const h = await health;
      el.dataset.s = h.status;
      if (!h.last_run) { el.lastElementChild.textContent = 'No data yet'; return; }
      const at = new Date(h.last_run.finished_at || h.last_run.started_at);
      const hrs = Math.round((Date.now() - at) / 36e5);
      el.lastElementChild.textContent = h.status === 'running' ? 'Updating now' : `Updated ${hrs < 1 ? '<1' : hrs}h ago`;
      el.title = `${h.last_run.courts_ok} courts polled, ${h.last_run.courts_failed} failed`;
    } catch (_) { el.dataset.s = 'stale'; el.lastElementChild.textContent = 'Offline'; }
  }

  // Every view fetched is kept for the life of the page (the data changes once
  // a day), keyed by its URL; an in-flight request is shared, not repeated.
  const views = new Map();
  function viewUrl(sport, win, venue) {
    const p = new URLSearchParams({ sport, window: win });
    if (venue) p.set('venue', venue);
    if (rev) p.set('rev', rev);
    return `/api/overview?${p}`;
  }
  function fetchView(url) {
    if (!views.has(url)) {
      const req = fetch(url).then((r) => {
        if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.status = r.status; throw e; }
        return r.json();
      });
      req.catch(() => views.delete(url)); // a failure is retried next time
      views.set(url, req);
    }
    return views.get(url);
  }
  // What the reader is likely to click next: the other windows of this view,
  // and the other sport. Fetched when the browser is idle, one at a time.
  function prefetch() {
    const wins = ['7', '30', 'all'];
    const next = wins.filter((w) => w !== state.window).map((w) => viewUrl(state.sport, w, state.venue));
    const other = state.sport === 'padel' ? 'pickleball' : 'padel';
    next.push(...wins.map((w) => viewUrl(other, w, null)));
    if (state.venue) next.push(...wins.map((w) => viewUrl(state.sport, w, null)));
    const idle = window.requestIdleCallback || ((f) => setTimeout(f, 200));
    const step = () => { const u = next.shift(); if (u) fetchView(u).catch(() => {}).finally(() => idle(step)); };
    idle(step);
  }

  let loading = 0;
  async function load() {
    persist();
    const ticket = ++loading;
    const url = viewUrl(state.sport, state.window, state.venue);
    document.body.style.cursor = 'progress';
    try {
      const data = await fetchView(url);
      if (ticket !== loading) return; // a later click superseded this one
      state.data = data;
      draw(state.data);
      prefetch();
    } catch (err) {
      if (ticket !== loading) return;
      // A venue from a shared link may have no rows in this window: fall back to all.
      if (err.status === 404 && state.venue) { state.venue = null; return load(); }
      const why = err.status === 503 ? 'these charts are not built yet' : err.message;
      $('venue-list').innerHTML = `<p class="empty">Could not load: ${esc(why)}</p>`;
    } finally { if (ticket === loading) document.body.style.cursor = ''; }
  }

  // ---------- drawing ----------
  function scopeName(d) {
    const v = d.venue && d.venues.find((x) => x.venue_uuid === d.venue);
    return v ? v.name : null;
  }

  function range(d) { return `${fmtDay(d.window.start)} – ${fmtDay(d.window.end)}`; }

  function draw(d) {
    const name = scopeName(d);
    $('scope').innerHTML = name
      ? `<button class="chip" id="clear" title="Back to all venues"><span class="lbl">Venue</span><b>${esc(name)}</b><span class="x" aria-hidden="true">✕</span></button>
        <button class="chip act" id="mark" title="Mark hours this venue takes off sale">Blocked hours</button>`
      : '';
    if (name) {
      $('clear').addEventListener('click', () => selectVenue(state.venue));
      $('mark').addEventListener('click', () => openBlocks(d.venue, name));
    }

    const empty = !d.totals.total_hours;
    $('grid').classList.toggle('is-empty', empty);
    $('nodata').hidden = !empty;
    kpis(d);
    venues(d);
    venueDen(d);
    if (empty) { nodata(d, name); return; }
    ['day', 'hour', 'week', 'map'].forEach((k) => drawPanel(k, d));
  }

  function nodata(d, name) {
    const sport = d.sport === 'padel' ? 'padel' : 'pickleball';
    const other = sport === 'padel' ? 'pickleball' : 'padel';
    $('nd-title').textContent = name ? `Nothing counted at ${name}` : `No settled ${sport} days yet`;
    const failing = d.venues.some((v) => v.venue_uuid === d.venue && v.failing);
    $('nd-text').textContent = !name
      ? `No ${sport} court has a fully elapsed day in ${range(d)}. Charts fill in after the first daily pass that follows a played day.`
      : failing ? 'Hudle did not answer for its courts in the last daily pass, so they are left out of every figure until it does.'
        : 'Its courts are blocked by the venue or have almost nothing booked on Hudle, so they are left out of every figure.';
    const acts = [];
    if (name) acts.push(['all', 'All venues']);
    if (d.window.key !== 'all') acts.push(['win', 'Try the whole history']);
    acts.push(['sport', `Show ${other}`]);
    $('nd-actions').innerHTML = acts.map(([k, l]) => `<button data-a="${k}">${l}</button>`).join('');
    $('nd-actions').onclick = (e) => {
      const a = e.target.closest('button')?.dataset.a;
      if (a === 'all') selectVenue(state.venue);
      if (a === 'win') $('window').querySelector('[data-v="all"]').click();
      if (a === 'sport') $('sport').querySelector(`[data-v="${other}"]`).click();
    };
    if (matchMedia('(max-width: 900px)').matches) showPanel(state.panel);
  }

  function sparkSvg(values, color) {
    const pts = values.filter((v) => v != null);
    if (pts.length < 2) return '';
    const max = Math.max(...pts, 0.01), w = 100, h = 30;
    const xy = values.map((v, i) => (v == null ? null : [(i / (values.length - 1)) * w, h - 2 - (v / max) * (h - 6)]));
    const path = xy.filter(Boolean).map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join('');
    const last = xy.filter(Boolean).pop();
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round" opacity=".85"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}" vector-effect="non-scaling-stroke"/></svg>`;
  }

  function kpis(d) {
    const t = d.totals, lead = d.lead_time;
    const T = theme();
    const occSeries = d.days.map((x) => x.occupancy);
    const share = (k) => (t.total_hours ? (100 * t[k]) / t.total_hours : 0);
    const ph = d.peaks.hour, pw = d.peaks.weekday;
    const cards = [
      {
        cls: 'hero', name: `Booked · ${d.venue ? 'this venue' : `${t.courts_counted} courts`} · ${range(d)}`,
        body: `<div class="line"><p class="val">${pct(t.occupancy, 1)}</p><div class="spark" title="% booked per day">${sparkSvg(occSeries, T.accent)}</div></div>`,
        sub: 'of the court time offered to customers',
      },
      {
        name: 'Court-hours booked',
        body: `<p class="val">${t.total_hours ? `${num(t.booked_hours)}<small>of ${num(t.total_hours)} offered</small>` : '—'}</p>
          <div class="meter" aria-hidden="true"><i style="width:${share('booked_hours')}%;background:${T.hudle}"></i><i style="flex:1;background:${T.vacant}"></i></div>`,
        sub: t.blocked_hours ? `+ <b>${num(t.blocked_hours)}</b> h blocked by venues · not counted` : 'no venue blocks in this view',
      },
      {
        name: 'Busiest hour',
        body: `<p class="val">${ph ? hh(ph.hour) : '—'}</p>`,
        sub: ph ? `<b>${pct(ph.occupancy)}</b> booked vs ${pct(t.occupancy)} overall` : 'not enough court time yet',
      },
      {
        name: 'Busiest weekday',
        body: `<p class="val">${pw ? LONG_DAYS[pw.weekday] : '—'}</p>`,
        sub: pw ? `<b>${pct(pw.occupancy)}</b> booked · over ${pw.days} ${LONG_DAYS[pw.weekday]}${pw.days === 1 ? '' : 's'}` : 'not enough court time yet',
      },
      {
        name: 'Booked ahead · median',
        body: `<p class="val">${lead.median_hours == null ? '—' : `${num(lead.median_hours, 1)}<small>h</small>`}</p>`,
        sub: lead.n ? `90% within <b>${num(lead.p90_hours)} h</b> · ${num(lead.n)} bookings` : 'no Hudle bookings with a time',
      },
    ];
    $('kpis').innerHTML = cards.map((c) => `<article class="kpi ${c.cls || ''}"><p class="name">${esc(c.name)}</p>${c.body}<p class="sub">${c.sub}</p></article>`).join('');
  }

  function venues(d) {
    const host = $('venue-list');
    const counted = d.venues.filter((v) => v.verdict !== 'unreliable');
    const listing = d.venues.filter((v) => v.verdict === 'unreliable');
    if (!d.venues.length) { host.innerHTML = '<p class="empty">No venues with settled days in this window.</p>'; return; }
    const f = state.filter.trim().toLowerCase();
    const T = theme();
    const row = (v) => {
      const reasons = v.courts.flatMap((c) => c.reasons.map((r) => `${c.name}: ${r}`)).join('\n');
      const n = v.courts.length;
      const offered = v.total_hours
        ? `${num(v.booked_hours)} of ${num(v.total_hours)} court-h offered`
        : `all ${num(v.blocked_hours)} court-h blocked`;
      const meta = `${offered} · ${n} court${n === 1 ? '' : 's'}${v.price_per_hour ? ` · ₹${num(v.price_per_hour)}/h` : ''}`;
      const tag = v.failing ? '<span class="tag v failing">not updating</span>'
        : v.verdict === 'partial' ? '<span class="tag v partial">low activity</span>'
        : v.verdict !== 'unreliable' ? ''
          : `<span class="tag v unreliable">${v.total_hours ? 'listing only' : 'all blocked'}</span>`;
      const wh = v.total_hours ? (100 * v.booked_hours) / v.total_hours : 0;
      return `<div class="row" role="option" tabindex="0" data-u="${esc(v.venue_uuid)}" data-v="${v.verdict}" aria-selected="${v.venue_uuid === d.venue}" title="${esc(reasons || v.name)}">
        <span class="nm">${esc(v.name)}${v.new ? '<span class="tag new">NEW</span>' : ''}${tag}</span>
        <span class="pct">${pct(v.occupancy)}</span>
        <div class="track"><i style="background:${T.hudle}" data-w="${wh}"></i></div>
        <span class="meta">${esc(meta)}</span></div>`;
    };
    const match = (v) => !f || v.name.toLowerCase().includes(f);
    const a = counted.filter(match), b = listing.filter(match);
    host.innerHTML = (a.map(row).join('') + (b.length ? `<div class="group">Not counted</div>${b.map(row).join('')}` : '')) || '<p class="empty">No venue matches.</p>';
    requestAnimationFrame(() => host.querySelectorAll('.track i').forEach((el) => { el.style.width = `${el.dataset.w}%`; }));
    const sel = host.querySelector('[aria-selected="true"]');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  }

  // ---------- venue map ----------
  // The venues panel's second view: where Jaipur's courts are (supply) against
  // where court time is bought (demand). The heat is the reading: nearby
  // venues add up, so a neighbourhood of small clubs glows like one big one.
  // Dots are markers for hovering and clicking, lightly sized by the figure.
  // Esri's gray canvas needs no key; its labels come as a separate layer on top.
  const TILES = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_{tone}_Gray_{part}/MapServer/tile/{z}/{y}/{x}';
  const ATTRIBUTION = 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors';
  const JAIPUR = [26.9124, 75.7873];
  const mapState = { map: null, tiles: null, tileStyle: null, heat: null, dots: null, fittedFor: null, bounds: null, pending: null };

  function venueDen(d) {
    const counted = d.venues.filter((v) => v.verdict !== 'unreliable');
    const out = d.venues.length - counted.length;
    $('den-venues').textContent = `% of court time offered that was booked · ${counted.length} counted, ${out} not counted · ${range(d)}`;
  }

  function mapDen(d) {
    const placed = d.venues.filter((v) => v.latitude != null).length;
    const missing = d.venues.length - placed;
    const what = state.views.metric === 'supply'
      ? 'Supply: where the courts open to customers are'
      : `Demand: where court-hours are booked, ${range(d)}`;
    $('den-map').textContent = `${what}${missing ? ` · ${missing} venue${missing === 1 ? '' : 's'} not placed yet` : ''}`;
  }

  function setMapBig(on) {
    $('grid').classList.toggle('map-big', on);
    $('mapbig').setAttribute('aria-pressed', String(on));
    $('mapbig').title = on ? 'Back to the charts' : 'Enlarge map';
    $('mapbig').textContent = on ? '⤡' : '⤢';
    // The frame changes size, so the venues are framed again to fill it.
    if (mapState.map) requestAnimationFrame(() => { mapState.map.invalidateSize(); fitVenues(); });
    if (!on) requestAnimationFrame(() => Object.values(charts).forEach((c) => c.resize()));
  }

  function fitVenues() {
    if (mapState.map && mapState.bounds) mapState.map.fitBounds(mapState.bounds, { padding: [36, 36], maxZoom: 14 });
  }

  function drawMap(d) {
    mapDen(d);
    if (typeof L === 'undefined') { $('map').innerHTML = '<p class="empty">The map library did not load.</p>'; return; }
    // A hidden map (a phone showing another tab) has no size to draw into, and
    // the heat layer cannot paint a zero-size canvas: draw once it is shown.
    if (!$('map').clientWidth) { mapState.pending = d; return; }
    mapState.pending = null;
    const t = theme();
    if (!mapState.map) {
      mapState.map = L.map('map', { zoomControl: true, attributionControl: true }).setView(JAIPUR, 12);
      mapState.map.attributionControl.setPrefix(false);
      // Dots live in their own pane above the heat, so the heat canvas never
      // sits between the pointer and a venue.
      mapState.map.createPane('venues').style.zIndex = 450;
      // One heat layer and one dot group for the life of the page: each redraw
      // replaces their contents, so nothing from an earlier view can linger.
      if (typeof L.heatLayer === 'function') mapState.heat = L.heatLayer([], { radius: 45, blur: 35 }).addTo(mapState.map);
      mapState.dots = L.layerGroup().addTo(mapState.map);
      new ResizeObserver(() => mapState.map.invalidateSize()).observe($('map'));
    }
    const map = mapState.map;
    const tone = document.documentElement.dataset.theme === 'dark' ? 'Dark' : 'Light';
    if (mapState.tileStyle !== tone) {
      if (mapState.tiles) mapState.tiles.remove();
      mapState.tiles = L.layerGroup([
        L.tileLayer(TILES, { tone, part: 'Base', maxZoom: 16, attribution: ATTRIBUTION }),
        L.tileLayer(TILES, { tone, part: 'Reference', maxZoom: 16, pane: 'shadowPane' }),
      ]).addTo(map);
      mapState.tileStyle = tone;
    }

    const supply = state.views.metric === 'supply';
    // The map counts exactly what every other figure counts: courts judged
    // reliable or low-activity. A venue left out (all blocked, or a listing
    // with almost nothing booked) is drawn as a grey dot and adds no heat.
    const counted = (c) => c.verdict === 'reliable' || c.verdict === 'partial';
    const weight = (v) => (v.verdict === 'unreliable' ? 0
      : supply ? v.courts.filter(counted).length
        : v.courts.filter(counted).reduce((sum, c) => sum + (c.booked_hours || 0), 0));
    const ramp = supply ? t.seq.slice(1) : t.dem;
    const placed = d.venues.filter((v) => v.latitude != null && v.longitude != null);
    const max = Math.max(1e-9, ...placed.map(weight));
    if (mapState.heat) {
      // Saturates at about two of the largest venues side by side, so one big
      // club alone reads warm and a cluster of them reads hot. A venue that
      // adds nothing (no courts offered, no hours booked) adds no heat.
      mapState.heat.setOptions({
        max: 2 * max, minOpacity: 0.3,
        gradient: { 0.1: ramp[0], 0.4: ramp[1], 0.7: ramp[2], 1: ramp[3] },
      });
      mapState.heat.setLatLngs(placed.filter((v) => weight(v) > 0).map((v) => [v.latitude, v.longitude, weight(v)]));
    }
    mapState.dots.clearLayers();
    // Largest first, so a small venue beside a big one stays clickable on top.
    [...placed].sort((a, b) => weight(b) - weight(a)).forEach((v) => {
      const selected = v.venue_uuid === d.venue;
      const dead = v.verdict === 'unreliable';
      const n = v.courts.length;
      const dot = L.circleMarker([v.latitude, v.longitude], {
        pane: 'venues', radius: 4 + 5 * Math.sqrt(weight(v) / max), className: 'dot',
        color: selected ? t.ink : t.panel, weight: selected ? 3 : 1.5,
        fillColor: dead ? t.ink3 : supply ? ramp[3] : ramp[2], fillOpacity: dead ? 0.5 : 0.9,
      });
      dot.bindTooltip(
        `<b>${esc(v.name)}</b><br><span>${n} court${n === 1 ? '' : 's'} · ${num(v.booked_hours)} court-h booked (${pct(v.occupancy)})</span>`
        + (v.failing ? `<br><span>not updating: ${dead ? 'not counted' : 'a court left out'}</span>`
          : dead ? `<br><span>${v.total_hours ? 'listing only' : 'all court time blocked'} · not counted</span>` : ''),
        { direction: 'top', offset: [0, -6], pane: 'tooltipPane' },
      );
      dot.on('click', () => selectVenue(v.venue_uuid));
      dot.on('mouseover', () => fetchView(viewUrl(state.sport, state.window, v.venue_uuid)).catch(() => {}));
      dot.addTo(mapState.dots);
    });
    const total = placed.reduce((a, v) => a + weight(v), 0);
    $('mapkey').innerHTML = `<b>${supply ? `${num(total)} courts` : `${num(total)} court-h booked`}</b>`
      + `<span class="ramp" style="background:linear-gradient(90deg, ${ramp[0]}, ${ramp[1]}, ${ramp[2]}, ${ramp[3]})"></span>`
      + `<span class="ends"><span>${supply ? 'few courts' : 'little booked'}</span><span>${supply ? 'many' : 'most'}</span></span>`;

    // Frame the venues when the sport changes, not on every redraw: a reader
    // who zoomed into a neighbourhood keeps it while switching windows.
    mapState.bounds = placed.length ? L.latLngBounds(placed.map((v) => [v.latitude, v.longitude])) : null;
    if (placed.length && mapState.fittedFor !== d.sport) {
      map.invalidateSize();
      fitVenues();
      mapState.fittedFor = d.sport;
    }
    $('map').setAttribute('aria-label', `Map of ${placed.length} ${d.sport} venues in Jaipur, shaded by ${supply ? 'number of courts' : 'court-hours booked'}.`);
  }

  function drawPanel(k, d) {
    if (k === 'map' || k === 'metric') { drawMap(d); return; }
    if (!d.totals.total_hours) return;
    ({ day: byDay, hour: byHour, week: byWeek })[k](d, theme());
  }

  function who(d) {
    return d.venue ? scopeName(d) : `${d.totals.courts_counted} counted courts`;
  }

  function byDay(d, t) {
    const rows = d.days;
    const cats = rows.map((r) => `${DAYS[wdOf(r.business_date)]} ${fmtDay(r.business_date)}`);
    const labels = rows.map((r) => fmtDay(r.business_date));
    const title = (r) => `${LONG_DAYS[wdOf(r.business_date)]} ${fmtDay(r.business_date)}`;
    if (state.views.day === 'pct') {
      const vals = rows.map((r) => (r.occupancy == null ? null : +(100 * r.occupancy).toFixed(1)));
      const avg = 100 * d.totals.occupancy;
      $('den-day').textContent = `booked ÷ court-hours offered, each day · ${who(d)} · ${range(d)}`;
      setChart('c-day', {
        ...base(t),
        grid: { left: 40, right: 52, top: 14, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: t.axis } },
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${title(r)}</b><br/>${pct(r.occupancy, 1)} booked<br/><span style="color:${t.ink3}">${num(r.booked_hours, 1)} of ${num(r.total_hours, 1)} court-h</span>`; } },
        xAxis: xCat(t, labels, { boundaryGap: rows.length < 3 }),
        yAxis: yPct(t, ceilPct(vals)),
        series: [{
          name: '% booked', type: 'line', data: vals, smooth: 0.25, showSymbol: rows.length <= 14, symbol: 'circle', symbolSize: 8,
          lineStyle: { width: 2, color: t.accent }, itemStyle: { color: t.accent, borderColor: t.panel, borderWidth: 2 },
          areaStyle: { color: t.accent, opacity: 0.1 },
          markLine: avgLine(t, +avg.toFixed(1), `avg\n${avg.toFixed(0)}%`),
        }],
      }, `Percent booked per day, ${range(d)}; average ${avg.toFixed(0)}%.`);
    } else if (state.views.day === 'rev') {
      const vals = rows.map((r) => r.revenue);
      const total = vals.reduce((a, v) => a + v, 0);
      const avg = rows.length ? total / rows.length : 0;
      const inr = (v) => `₹${num(v)}`;
      const short = (v) => (v >= 1e5 ? `₹${(v / 1e5).toFixed(1)}L` : v >= 1e3 ? `₹${(v / 1e3).toFixed(0)}k` : `₹${v}`);
      $('den-day').textContent = `revenue from customer bookings each day, at Hudle's listed prices (offers and discounts not reflected) · ${inr(total)} in all · ${who(d)} · ${range(d)}`;
      setChart('c-day', {
        ...base(t),
        grid: { left: 48, right: 52, top: 14, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis',
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${title(r)}</b><br/>${inr(r.revenue)} from bookings<br/><span style="color:${t.ink3}">${num(r.booked_hours, 1)} court-h booked · listed prices</span>`; } },
        xAxis: xCat(t, labels),
        yAxis: yVal(t, { axisLabel: { color: t.ink3, fontSize: 11, formatter: short } }),
        series: [{
          name: 'Revenue', type: 'bar', data: vals, barMaxWidth: 24, itemStyle: { color: t.accent, borderRadius: [4, 4, 0, 0] },
          markLine: avgLine(t, Math.round(avg), `avg\n${short(Math.round(avg))}`),
        }],
      }, `Revenue from customer bookings per day at listed prices, ${range(d)}; ${inr(total)} in all.`);
    } else {
      $('den-day').textContent = `court-hours offered each day: booked, vacant · ${who(d)} · ${range(d)}`;
      setChart('c-day', {
        ...base(t),
        legend: legend(t, ['Booked', 'Vacant']),
        grid: { left: 44, right: 12, top: 28, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', formatter: stackTip(title, rows) },
        xAxis: xCat(t, labels),
        yAxis: yVal(t, { axisLabel: { color: t.ink3, fontSize: 11, formatter: (v) => num(v) } }),
        series: bookedVacant(t, rows, cats),
      }, `Court-hours offered per day, booked and vacant, ${range(d)}.`);
    }
  }

  function byHour(d, t) {
    const rows = d.hours;
    const labels = rows.map((r) => String(r.hour));
    const title = (r) => `${hh(r.hour)} – ${hh((r.hour + 1) % 24)}`;
    // An hour with little court time (a dawn or 2am slot) reads 0% or 100% on a
    // handful of slots: drawn faint and called out, not hidden.
    const maxT = Math.max(...rows.map((r) => r.total_hours));
    const thin = (r) => r.total_hours < 0.25 * maxT;
    if (state.views.hour === 'pct') {
      const vals = rows.map((r) => (r.occupancy == null ? null : +(100 * r.occupancy).toFixed(1)));
      const avg = 100 * d.totals.occupancy;
      const anyThin = rows.some(thin);
      const top = ceilPct(rows.filter((r) => !thin(r)).map((r) => 100 * (r.occupancy || 0)));
      $('den-hour').textContent = `booked ÷ court-hours offered at each start hour, all days · ${who(d)} · ${range(d)}${anyThin ? ' · faint = little court time' : ''}`;
      setChart('c-hour', {
        ...base(t),
        grid: { left: 40, right: 52, top: 14, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis',
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${title(r)}</b><br/>${pct(r.occupancy, 1)} booked<br/><span style="color:${t.ink3}">${num(r.booked_hours, 1)} of ${num(r.total_hours, 1)} court-h over ${r.days} days${thin(r) ? '<br/>little court time: read with care' : ''}</span>`; } },
        xAxis: xCat(t, labels, { axisLabel: { color: t.ink3, fontSize: 11, interval: 0, formatter: (v, i) => (i % 2 ? '' : v) } }),
        yAxis: yPct(t, top),
        series: [{
          name: '% booked', type: 'bar', barMaxWidth: 24, barCategoryGap: '25%',
          // The scale fits the well-sold hours; a faint hour above it is cut at
          // the top (its value is in the tooltip) rather than flattening the rest.
          data: rows.map((r, i) => ({
            value: vals[i] == null ? null : Math.min(vals[i], top),
            itemStyle: { color: t.accent, opacity: thin(r) ? 0.28 : 1, borderRadius: [4, 4, 0, 0] },
          })),
          markLine: avgLine(t, +avg.toFixed(1), `avg\n${avg.toFixed(0)}%`),
        }],
      }, `Percent booked by hour of day, ${range(d)}.`);
    } else {
      const div = (r) => r.days || 1;
      $('den-hour').textContent = `court-hours on an average day at each start hour · venue blocks shown, not counted as booked · ${who(d)} · ${range(d)}`;
      setChart('c-hour', {
        ...base(t),
        legend: legend(t, ['Booked', 'Vacant', 'Venue block']),
        grid: { left: 40, right: 12, top: 28, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', formatter: stackTip(title, rows, div, 'court-h on an average day', true) },
        xAxis: xCat(t, labels, { axisLabel: { color: t.ink3, fontSize: 11, interval: 0, formatter: (v, i) => (i % 2 ? '' : v) } }),
        yAxis: yVal(t),
        series: bookedVacant(t, rows, labels, div, true),
      }, `Average court-hours per day by hour: booked, vacant, and blocked by the venue (not counted as booked), ${range(d)}.`);
    }
  }

  function byWeek(d, t) {
    const rows = d.weekdays;
    if (state.views.week === 'grid') {
      // Hours when almost every court is shut (the small hours) are dropped:
      // a percentage of a sliver of court time is noise, not a busy hour.
      const perHour = Object.fromEntries(d.hours.map((r) => [r.hour, r.total_hours]));
      const busiest = Math.max(0, ...Object.values(perHour));
      const hours = d.hours.map((r) => r.hour).filter((h) => perHour[h] >= 0.25 * busiest);
      const cells = d.heatmap.filter((c) => hours.includes(c.hour));
      // The scale spans the values actually present, so a grid that runs 20% to
      // 45% shows its quiet and busy hours apart instead of one shade of blue.
      const values = cells.map((c) => 100 * (c.occupancy || 0));
      const low = Math.floor(Math.min(100, ...values) / 10) * 10;
      const top = Math.max(low + 10, Math.ceil(Math.max(0, ...values) / 10) * 10);
      const wide = $('c-week').clientWidth / Math.max(1, hours.length) >= 18;
      const data = cells.map((c) => ({
        value: [hours.indexOf(c.hour), c.weekday, c.occupancy == null ? null : Math.round(100 * c.occupancy)],
        total: c.total_hours, booked: c.booked_hours,
        // Busy squares are the dark end of the ramp in light mode and the light
        // end in dark mode; either way the panel colour reads on them.
        label: { color: (100 * (c.occupancy || 0) - low) / (top - low) > 0.45 ? t.panel : t.ink },
      }));
      const perCell = Math.max(1, Math.round(d.window.days / 7));
      $('den-week').textContent = `Each square: % of court time booked at that hour on that weekday · ${perCell === 1 ? 'one day each' : `about ${perCell} days each`} · ${who(d)} · ${range(d)}`;
      setChart('c-week', {
        ...base(t),
        grid: { left: 40, right: 8, top: 4, bottom: 44 },
        tooltip: { ...base(t).tooltip, trigger: 'item',
          formatter: (p) => `<b>${LONG_DAYS[p.value[1]]} ${hh(hours[p.value[0]])}</b><br/>${p.value[2]}% of court time booked<br/><span style="color:${t.ink3}">${num(p.data.booked, 1)} of ${num(p.data.total, 1)} court-h</span>` },
        xAxis: xCat(t, hours.map(String), { axisLine: { show: false }, axisLabel: { color: t.ink3, fontSize: 11, interval: 0, formatter: (v, i) => (wide || i % 2 === 0 ? v : '') } }),
        yAxis: { type: 'category', data: DAYS, inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: t.ink3, fontSize: 11 } },
        visualMap: { min: low, max: top, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 140, itemWidth: 8,
          textStyle: { color: t.ink3, fontSize: 10 }, text: [`busier  ${top}%`, `quiet  ${low}%`], inRange: { color: t.seq } },
        series: [{ type: 'heatmap', data, itemStyle: { borderColor: t.panel, borderWidth: 2, borderRadius: 3 },
          label: { show: wide, fontSize: 10, formatter: (p) => (p.value[2] == null ? '' : p.value[2]) },
          emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1 } } }],
      }, `Heatmap of percent of court time booked by weekday and hour, ${range(d)}.`);
    } else {
      const vals = rows.map((r) => (r.occupancy == null ? null : +(100 * r.occupancy).toFixed(1)));
      const avg = 100 * d.totals.occupancy;
      $('den-week').textContent = `booked ÷ court-hours offered on each weekday · ${who(d)} · ${range(d)}`;
      setChart('c-week', {
        ...base(t),
        grid: { left: 40, right: 52, top: 18, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis',
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${LONG_DAYS[r.weekday]}</b><br/>${pct(r.occupancy, 1)} booked<br/><span style="color:${t.ink3}">${num(r.booked_hours)} of ${num(r.total_hours)} court-h over ${r.days} ${DAYS[r.weekday]}${r.days === 1 ? '' : 's'}</span>`; } },
        xAxis: xCat(t, rows.map((r) => DAYS[r.weekday])),
        yAxis: yPct(t, ceilPct(vals)),
        series: [{
          name: '% booked', type: 'bar', barMaxWidth: 24, itemStyle: { color: t.accent, borderRadius: [4, 4, 0, 0] }, data: vals,
          label: { show: true, position: 'top', color: t.ink2, fontSize: 11, formatter: (p) => `${Math.round(p.value)}%` },
          markLine: avgLine(t, +avg.toFixed(1), `avg\n${avg.toFixed(0)}%`),
        }],
      }, `Percent booked by weekday, ${range(d)}.`);
    }
  }

  // ---------- blocked hours ----------
  // A person who knows a venue's schedule marks the hours it takes off sale
  // (coaching, say) where Hudle shows them booked. The server rebuilds every
  // view on a change; the page then asks for the new revision.
  const HOURS = Array.from({ length: 24 }, (_, i) => (i + 4) % 24); // business-day order
  const PASS_KEY = 'ht.admin';
  const bk = { venue: null, cells: new Set(), blocks: [], paint: null };
  const cellKey = (wd, h) => `${wd}:${h}`;

  // "06:00–08:00, 18:00–19:00": runs of consecutive start hours, business-day order.
  function hoursText(hours) {
    const order = hours.map((h) => (h + 20) % 24).sort((a, b) => a - b);
    const runs = [];
    order.forEach((o) => { const r = runs[runs.length - 1]; if (r && o === r[1] + 1) r[1] = o; else runs.push([o, o]); });
    return runs.map(([a, b]) => `${hh((a + 4) % 24)}–${hh((b + 5) % 24)}`).join(', ');
  }
  // "Mon–Fri", "Sat, Sun".
  function daysText(wds) {
    const runs = [];
    wds.forEach((w) => { const r = runs[runs.length - 1]; if (r && w === r[1] + 1) r[1] = w; else runs.push([w, w]); });
    return runs.map(([a, b]) => (b - a >= 2 ? `${DAYS[a]}–${DAYS[b]}` : DAYS.slice(a, b + 1).join(', '))).join(', ');
  }
  // Weekdays with the same hours share a line: "Mon–Fri 06:00–08:00 · Sat 07:00–09:00".
  function describe(cells) {
    const groups = [];
    DAYS.forEach((_, wd) => {
      const txt = hoursText(cells.filter(([w]) => w === wd).map(([, h]) => h));
      if (!txt) return;
      const g = groups.find((x) => x.txt === txt);
      if (g) g.wds.push(wd); else groups.push({ wds: [wd], txt });
    });
    return groups.map((g) => `${daysText(g.wds)} ${g.txt}`).join(' · ');
  }
  function datesText(b) {
    if (!b.date_from && !b.date_to) return 'every day';
    if (!b.date_to) return `from ${fmtDay(b.date_from)}`;
    if (!b.date_from) return `until ${fmtDay(b.date_to)}`;
    return `${fmtDay(b.date_from)} – ${fmtDay(b.date_to)}`;
  }

  function bkStatus(text, err = false) {
    $('bk-status').textContent = text;
    $('bk-status').dataset.s = err ? 'err' : '';
  }

  async function failure(r, what) {
    if (r.status === 401) return 'Wrong password.';
    let detail = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      detail = Array.isArray(j.detail) ? j.detail.map((x) => x.msg).join('; ') : j.detail || detail;
    } catch (_) {}
    return `Could not ${what}: ${detail}`;
  }

  function drawBlocks() {
    $('bk-list').innerHTML = bk.blocks.map((b) => `<div class="bk-rule"><span><b>${esc(describe(b.cells))}</b> · ${esc(datesText(b))}${b.note ? ` <small>· ${esc(b.note)}</small>` : ''}</span>
      <button type="button" data-id="${b.block_id}">Remove</button></div>`).join('');
    const had = new Set(bk.blocks.flatMap((b) => b.cells.map(([wd, h]) => cellKey(wd, h))));
    const head = `<span></span>${HOURS.map((h, i) => `<button type="button" data-col="${h}" title="Every ${hh(h)} slot">${i % 2 ? '' : h}</button>`).join('')}`;
    const rows = DAYS.map((d, wd) => `<button type="button" class="day" data-row="${wd}" title="All of ${LONG_DAYS[wd]}">${d}</button>${HOURS.map((h) => {
      const k = cellKey(wd, h);
      const tip = `${LONG_DAYS[wd]} ${hh(h)}–${hh((h + 1) % 24)}${had.has(k) ? ' · already blocked' : ''}`;
      return `<button type="button" class="cell${had.has(k) ? ' had' : ''}" data-k="${k}" aria-pressed="${bk.cells.has(k)}" aria-label="${tip}" title="${tip}"></button>`;
    }).join('')}`).join('');
    $('bk-grid').innerHTML = head + rows;
    syncSave();
  }

  function syncSave() {
    const n = bk.cells.size;
    $('bk-save').disabled = !n;
    $('bk-save').textContent = n ? `Save block · ${n} hour${n === 1 ? '' : 's'} a week` : 'Save block';
  }

  function setCell(el, on) {
    if (on) bk.cells.add(el.dataset.k); else bk.cells.delete(el.dataset.k);
    el.setAttribute('aria-pressed', String(on));
  }

  async function loadBlocks() {
    const r = await fetch(`/api/blocks?${new URLSearchParams({ venue: bk.venue })}`);
    if (!r.ok) throw new Error(await failure(r, 'load the blocks'));
    bk.blocks = (await r.json()).blocks;
    drawBlocks();
  }

  async function openBlocks(venue, name) {
    bk.venue = venue;
    bk.cells.clear();
    bk.blocks = [];
    $('bk-title').textContent = `Blocked hours · ${name}`;
    ['bk-from', 'bk-to', 'bk-note'].forEach((id) => { $(id).value = ''; });
    try { $('bk-pass').value = sessionStorage.getItem(PASS_KEY) || ''; } catch (_) {}
    bkStatus('');
    drawBlocks();
    $('blocks').showModal();
    try { await loadBlocks(); } catch (err) { bkStatus(err.message, true); }
  }

  // A change is live on the server: forget every view held and ask for the new revision.
  function changed(builtAt) {
    setRev(builtAt);
    views.clear();
    load();
  }

  // A rebuild queued on GitHub lands in a few minutes: ask health (a fresh URL
  // each time, past the edge cache) until the views are newer than the build
  // the server had when the change was saved, then redraw. Gives up after ten
  // minutes; the next load catches up.
  function awaitRebuild(builtAt) {
    const since = builtAt ? Date.parse(builtAt) : 0;
    const until = Date.now() + 10 * 60e3;
    const poll = async () => {
      if (Date.now() > until) return;
      try {
        const h = await (await fetch(`/api/health?t=${Date.now()}`)).json();
        if (h.views_built_at && Date.parse(h.views_built_at) > since) { changed(h.views_built_at); return; }
      } catch (_) {}
      setTimeout(poll, 15e3);
    };
    setTimeout(poll, 30e3);
  }

  // What the page says once a change is saved, by how the rebuild went.
  const REBUILT = {
    done: 'Every chart now reflects it.',
    queued: 'The charts update in a few minutes.',
    failed: 'The charts could not be rebuilt now; they update after the next daily pass.',
  };

  async function change(method, url, body, what) {
    const pass = $('bk-pass').value;
    if (!pass) { bkStatus(`Enter the password to ${what}.`, true); $('bk-pass').focus(); return null; }
    bkStatus('Saving…');
    $('bk-save').disabled = true;
    try {
      const r = await fetch(url, {
        method, headers: { 'Content-Type': 'application/json', 'X-Admin-Password': pass },
        body: body && JSON.stringify(body),
      });
      if (!r.ok) { bkStatus(await failure(r, what), true); return null; }
      try { sessionStorage.setItem(PASS_KEY, pass); } catch (_) {}
      const j = await r.json();
      if (j.rebuild === 'done') changed(j.views_built_at);
      else if (j.rebuild === 'queued') awaitRebuild(j.views_built_at);
      return j;
    } catch (err) {
      bkStatus(`Could not ${what}: ${err.message}`, true);
      return null;
    } finally { syncSave(); }
  }

  $('bk-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const from = $('bk-from').value || null, to = $('bk-to').value || null;
    if (from && to && from > to) { bkStatus('The From date is after the To date.', true); return; }
    const body = {
      venue_uuid: bk.venue, cells: [...bk.cells].map((k) => k.split(':').map(Number)),
      date_from: from, date_to: to, note: $('bk-note').value.trim(),
    };
    const j = await change('POST', '/api/blocks', body, 'save the block');
    if (!j) return;
    bk.cells.clear();
    $('bk-note').value = '';
    await loadBlocks().catch(() => {});
    bkStatus(`Saved. ${REBUILT[j.rebuild]}`);
  });
  $('bk-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-id]');
    if (!b || !confirm('Remove this block? Its hours count as Hudle reports them again.')) return;
    const j = await change('DELETE', `/api/blocks/${b.dataset.id}`, null, 'remove the block');
    if (!j) return;
    await loadBlocks().catch(() => {});
    bkStatus(`Removed. ${REBUILT[j.rebuild]}`);
  });
  $('bk-clear').addEventListener('click', () => { bk.cells.clear(); drawBlocks(); });
  $('bk-close').addEventListener('click', () => $('blocks').close());
  // Drag to tick: the first cell decides whether the stroke ticks or clears.
  // Found by position, not by pointerover, so a finger dragging works too.
  const grid = $('bk-grid');
  grid.addEventListener('pointerdown', (e) => {
    const el = e.target.closest('.cell');
    if (!el) return;
    e.preventDefault();
    bk.paint = !bk.cells.has(el.dataset.k);
    setCell(el, bk.paint);
    syncSave();
  });
  grid.addEventListener('pointermove', (e) => {
    if (bk.paint == null) return;
    const el = document.elementFromPoint(e.clientX, e.clientY)?.closest('.cell');
    if (el && grid.contains(el) && bk.cells.has(el.dataset.k) !== bk.paint) { setCell(el, bk.paint); syncSave(); }
  });
  window.addEventListener('pointerup', () => { bk.paint = null; });
  grid.addEventListener('click', (e) => {
    const el = e.target.closest('button');
    if (!el) return;
    // A pointer already ticked the cell on pointerdown; this is the keyboard's click.
    if (el.classList.contains('cell')) { if (e.detail === 0) { setCell(el, !bk.cells.has(el.dataset.k)); syncSave(); } return; }
    const line = [...grid.querySelectorAll(el.dataset.row != null ? `.cell[data-k^="${el.dataset.row}:"]` : `.cell[data-k$=":${el.dataset.col}"]`)];
    const on = !line.every((c) => bk.cells.has(c.dataset.k));
    line.forEach((c) => setCell(c, on));
    syncSave();
  });

  // ---------- wiring ----------
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ht.theme', next); } catch (_) {}
    if (state.data) draw(state.data);
  });
  new ResizeObserver(() => { if (mapState.pending && $('map').clientWidth) drawMap(mapState.pending); }).observe($('map'));
  $('mapbig').addEventListener('click', () => setMapBig(!$('grid').classList.contains('map-big')));
  $('search').addEventListener('input', (e) => { state.filter = e.target.value; if (state.data) venues(state.data); });
  $('venue-list').addEventListener('click', (e) => { const r = e.target.closest('.row'); if (r) selectVenue(r.dataset.u); });
  // Pointing at a venue starts its fetch, so the click usually finds it ready.
  $('venue-list').addEventListener('pointerover', (e) => {
    const r = e.target.closest('.row');
    if (r && r.dataset.u !== state.venue) fetchView(viewUrl(state.sport, state.window, r.dataset.u)).catch(() => {});
  });
  $('venue-list').addEventListener('keydown', (e) => {
    const r = e.target.closest('.row');
    if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectVenue(r.dataset.u); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.venue && e.target.tagName !== 'INPUT' && !$('blocks').open) selectVenue(state.venue); });

  segmented('sport', 'sport');
  segmented('window', 'window');
  minis();
  tabs();
  freshness();
  // The first view waits for the revision, so it is not fetched twice.
  health.then((h) => setRev(h.views_built_at)).catch(() => {}).finally(load);
})();
