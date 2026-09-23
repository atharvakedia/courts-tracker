/* Courts tracker dashboard: one fetch per view (/api/overview), drawn in place.
   A view is sport x window x (optionally) one venue; every chart and headline
   figure answers for the same view, and the URL carries it. */
(function () {
  'use strict';
  const state = {
    sport: 'padel', window: '7', venue: null, panel: 'venues', data: null, filter: '',
    views: { day: 'pct', hour: 'pct', week: 'grid', spread: 'days', venues: 'list', metric: 'demand' },
  };
  try {
    const saved = JSON.parse(localStorage.getItem('ht.state') || '{}');
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
  if (['venues', 'day', 'hour', 'week', 'spread'].includes(q.get('panel'))) state.panel = q.get('panel');

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

  function booked3(t, rows, cats, div = () => 1) {
    const s = (name, key, color, top) => ({
      name, type: 'bar', stack: 'h', barMaxWidth: 24, barCategoryGap: '30%',
      itemStyle: { color, borderRadius: top ? [4, 4, 0, 0] : 0, borderColor: t.panel, borderWidth: 1 },
      emphasis: { focus: 'series' },
      data: rows.map((r) => +(r[key] / div(r)).toFixed(1)),
    });
    return [
      s('On Hudle', 'hudle_booked_hours', t.hudle, false),
      s('Venue block', 'blocked_hours', t.block, false),
      s('Vacant', 'vacant_hours', t.vacant, true),
    ];
  }

  function stackTip(title, rows, div = () => 1, unit = 'court-h') {
    return (ps) => {
      const r = rows[ps[0].dataIndex];
      const d = div(r);
      const line = (k, color, label) => `<div style="display:flex;gap:10px;justify-content:space-between"><span><span class="key" style="background:${color}"></span>${label}</span><b>${num(r[k] / d, 1)}</b></div>`;
      return `<div style="min-width:170px"><b>${title(r)}</b>
        ${line('hudle_booked_hours', ps[0].color, 'On Hudle')}
        ${line('blocked_hours', ps[1].color, 'Venue block')}
        ${line('vacant_hours', ps[2].color, 'Vacant')}
        <div style="color:${css('--ink-3')};margin-top:4px">${pct(r.occupancy)} booked · ${unit}</div></div>`;
    };
  }

  function setChart(id, option, summary) {
    const c = chart(id);
    c.setOption(option, true);
    $(id).setAttribute('aria-label', summary);
  }

  // ---------- controls ----------
  function persist() {
    try { localStorage.setItem('ht.state', JSON.stringify({ sport: state.sport, window: state.window, views: state.views })); } catch (_) {}
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

  // Every view fetched is kept for the life of the page (the data changes once
  // a day), keyed by its URL; an in-flight request is shared, not repeated.
  const views = new Map();
  function viewUrl(sport, win, venue) {
    const p = new URLSearchParams({ sport, window: win });
    if (venue) p.set('venue', venue);
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
      $('venue-list').innerHTML = `<p class="empty">Could not load: ${esc(err.message)}</p>`;
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
      ? `<button class="chip" id="clear" title="Back to all venues"><span class="lbl">Venue</span><b>${esc(name)}</b><span class="x" aria-hidden="true">✕</span></button>`
      : '';
    if (name) $('clear').addEventListener('click', () => selectVenue(state.venue));

    const empty = !d.totals.total_hours;
    $('grid').classList.toggle('is-empty', empty);
    $('nodata').hidden = !empty;
    kpis(d);
    venues(d);
    venueView(d);
    if (empty) { nodata(d, name); return; }
    ['day', 'hour', 'week', 'spread'].forEach((k) => drawPanel(k, d));
  }

  function nodata(d, name) {
    const sport = d.sport === 'padel' ? 'padel' : 'pickleball';
    const other = sport === 'padel' ? 'pickleball' : 'padel';
    $('nd-title').textContent = name ? `Nothing counted at ${name}` : `No settled ${sport} days yet`;
    $('nd-text').textContent = name
      ? 'Its courts are listings with almost nothing booked or blocked, so they are left out of every figure.'
      : `No ${sport} court has a fully elapsed day in ${range(d)}. Charts fill in after the first daily pass that follows a played day.`;
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
    const t = d.totals, prev = d.previous, lead = d.lead_time;
    const T = theme();
    let delta = `<span class="delta flat">no earlier data to compare</span>`;
    if (t.occupancy != null && prev.occupancy != null) {
      const pts = 100 * (t.occupancy - prev.occupancy);
      const cls = Math.abs(pts) < 0.5 ? 'flat' : pts > 0 ? 'up' : 'down';
      const arrow = cls === 'flat' ? '≈' : pts > 0 ? '▲' : '▼';
      delta = `<span class="delta ${cls}">${arrow} ${Math.abs(pts).toFixed(1)} pts</span> vs ${fmtDay(prev.start)} – ${fmtDay(prev.end)}`;
    }
    const occSeries = d.days.map((x) => x.occupancy);
    const share = (k) => (t.total_hours ? (100 * t[k]) / t.total_hours : 0);
    const onHudle = t.booked_hours ? Math.round((100 * t.hudle_booked_hours) / t.booked_hours) : null;
    const ph = d.peaks.hour, pw = d.peaks.weekday;
    const cards = [
      {
        cls: 'hero', name: `Booked · ${d.venue ? 'this venue' : `${t.courts_counted} courts`} · ${range(d)}`,
        body: `<div class="line"><p class="val">${pct(t.occupancy, 1)}</p><div class="spark" title="% booked per day">${sparkSvg(occSeries, T.accent)}</div></div>`,
        sub: delta,
      },
      {
        name: 'Court-hours booked',
        body: `<p class="val">${t.total_hours ? `${num(t.booked_hours)}<small>of ${num(t.total_hours)}</small>` : '—'}</p>
          <div class="meter" aria-hidden="true"><i style="width:${share('hudle_booked_hours')}%;background:${T.hudle}"></i><i style="width:${share('blocked_hours')}%;background:${T.block}"></i><i style="flex:1;background:${T.vacant}"></i></div>`,
        sub: onHudle == null ? 'nothing booked in this view'
          : `<span class="key" style="background:${T.hudle}"></span>${onHudle}% on Hudle · <span class="key" style="background:${T.block}"></span>${100 - onHudle}% venue blocks`,
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
      const meta = `${num(v.booked_hours)} of ${num(v.total_hours)} court-h · ${n} court${n === 1 ? '' : 's'}${v.price_per_hour ? ` · ₹${num(v.price_per_hour)}/h` : ''}`;
      const tag = v.verdict === 'partial' ? '<span class="tag v partial">low activity</span>' : v.verdict === 'unreliable' ? '<span class="tag v unreliable">listing only</span>' : '';
      const wh = v.total_hours ? (100 * v.hudle_booked_hours) / v.total_hours : 0;
      const wb = v.total_hours ? (100 * v.blocked_hours) / v.total_hours : 0;
      return `<div class="row" role="option" tabindex="0" data-u="${esc(v.venue_uuid)}" data-v="${v.verdict}" aria-selected="${v.venue_uuid === d.venue}" title="${esc(reasons || v.name)}">
        <span class="nm">${esc(v.name)}${v.new ? '<span class="tag new">NEW</span>' : ''}${tag}</span>
        <span class="pct">${pct(v.occupancy)}</span>
        <div class="track"><i style="background:${T.hudle}" data-w="${wh}"></i><i style="background:${T.block}" data-w="${wb}"></i></div>
        <span class="meta">${esc(meta)}</span></div>`;
    };
    const match = (v) => !f || v.name.toLowerCase().includes(f);
    const a = counted.filter(match), b = listing.filter(match);
    host.innerHTML = (a.map(row).join('') + (b.length ? `<div class="group">Listing only · not counted</div>${b.map(row).join('')}` : '')) || '<p class="empty">No venue matches.</p>';
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
  const mapState = { map: null, tiles: null, tileStyle: null, layers: null, fittedFor: null, bounds: null };

  function venueView(d) {
    $('venues-panel').dataset.view = state.views.venues;
    if (state.views.venues !== 'map') setMapBig(false);
    if (state.views.venues === 'map') drawMap(d);
    else venueDen(d);
  }

  function venueDen(d) {
    const counted = d.venues.filter((v) => v.verdict !== 'unreliable');
    const listing = d.venues.length - counted.length;
    if (state.views.venues !== 'map') {
      $('den-venues').textContent = `% of listed court-hours booked · ${counted.length} counted, ${listing} listing-only · ${range(d)}`;
      return;
    }
    const placed = d.venues.filter((v) => v.latitude != null).length;
    const missing = d.venues.length - placed;
    const what = state.views.metric === 'supply'
      ? 'Supply: where the courts are, every listed court'
      : `Demand: where court-hours are booked, ${range(d)}`;
    $('den-venues').textContent = `${what}${missing ? ` · ${missing} venue${missing === 1 ? '' : 's'} not placed yet` : ''}`;
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
    venueDen(d);
    if (typeof L === 'undefined') { $('map').innerHTML = '<p class="empty">The map library did not load.</p>'; return; }
    const t = theme();
    if (!mapState.map) {
      mapState.map = L.map('map', { zoomControl: true, attributionControl: true, preferCanvas: true }).setView(JAIPUR, 12);
      mapState.map.attributionControl.setPrefix(false);
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
    if (mapState.layers) mapState.layers.remove();

    const supply = state.views.metric === 'supply';
    const weight = (v) => (supply ? v.courts.length : v.booked_hours || 0);
    const ramp = supply ? t.seq.slice(1) : t.dem;
    const placed = d.venues.filter((v) => v.latitude != null && v.longitude != null);
    const max = Math.max(1e-9, ...placed.map(weight));
    const layers = L.layerGroup();
    if (typeof L.heatLayer === 'function' && placed.length) {
      // Saturates at about two of the largest venues side by side, so one big
      // club alone reads warm and a cluster of them reads hot.
      L.heatLayer(placed.map((v) => [v.latitude, v.longitude, weight(v)]), {
        radius: 45, blur: 35, max: 2 * max, minOpacity: 0.25,
        gradient: { 0.1: ramp[0], 0.4: ramp[1], 0.7: ramp[2], 1: ramp[3] },
      }).addTo(layers);
    }
    // Largest first, so a small venue beside a big one stays clickable on top.
    [...placed].sort((a, b) => weight(b) - weight(a)).forEach((v) => {
      const selected = v.venue_uuid === d.venue;
      const dead = v.verdict === 'unreliable';
      const n = v.courts.length;
      const dot = L.circleMarker([v.latitude, v.longitude], {
        radius: 3 + 5 * Math.sqrt(weight(v) / max), className: 'dot',
        color: selected ? t.ink : t.panel, weight: selected ? 3 : 1.5,
        fillColor: dead ? t.ink3 : supply ? ramp[3] : ramp[2], fillOpacity: dead ? 0.5 : 0.9,
      });
      dot.bindTooltip(
        `<b>${esc(v.name)}</b><br><span>${n} court${n === 1 ? '' : 's'} · ${num(v.booked_hours)} court-h booked (${pct(v.occupancy)})</span>`
        + (dead ? '<br><span>listing only · not counted in demand</span>' : ''),
        { direction: 'top', offset: [0, -6] },
      );
      dot.on('click', () => selectVenue(v.venue_uuid));
      dot.on('mouseover', () => fetchView(viewUrl(state.sport, state.window, v.venue_uuid)).catch(() => {}));
      dot.addTo(layers);
    });
    mapState.layers = layers.addTo(map);
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
    if (k === 'venues' || k === 'metric') { venueView(d); return; }
    if (!d.totals.total_hours) return;
    ({ day: byDay, hour: byHour, week: byWeek, spread })[k](d, theme());
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
      $('den-day').textContent = `booked ÷ listed court-hours, each day · ${who(d)} · ${range(d)}`;
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
    } else {
      $('den-day').textContent = `court-hours each day: booked on Hudle, blocked by the venue, vacant · ${who(d)} · ${range(d)}`;
      setChart('c-day', {
        ...base(t),
        legend: legend(t, ['On Hudle', 'Venue block', 'Vacant']),
        grid: { left: 44, right: 12, top: 28, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', formatter: stackTip(title, rows) },
        xAxis: xCat(t, labels),
        yAxis: yVal(t, { axisLabel: { color: t.ink3, fontSize: 11, formatter: (v) => num(v) } }),
        series: booked3(t, rows, cats),
      }, `Court-hours per day split into booked on Hudle, venue blocks and vacant, ${range(d)}.`);
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
      $('den-hour').textContent = `booked ÷ listed court-hours at each start hour, all days · ${who(d)} · ${range(d)}${anyThin ? ' · faint = little court time' : ''}`;
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
      $('den-hour').textContent = `court-hours on an average day at each start hour · ${who(d)} · ${range(d)}`;
      setChart('c-hour', {
        ...base(t),
        legend: legend(t, ['On Hudle', 'Venue block', 'Vacant']),
        grid: { left: 40, right: 12, top: 28, bottom: 24 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', formatter: stackTip(title, rows, div, 'court-h on an average day') },
        xAxis: xCat(t, labels, { axisLabel: { color: t.ink3, fontSize: 11, interval: 0, formatter: (v, i) => (i % 2 ? '' : v) } }),
        yAxis: yVal(t),
        series: booked3(t, rows, labels, div),
      }, `Average court-hours per day by hour, split into booked on Hudle, venue blocks and vacant, ${range(d)}.`);
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
      const wide = $('c-week').clientWidth / Math.max(1, hours.length) >= 24;
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
      $('den-week').textContent = `booked ÷ listed court-hours on each weekday · ${who(d)} · ${range(d)}`;
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

  function spread(d, t) {
    if (state.views.spread === 'days') {
      const rows = d.spread;
      const labels = rows.map((b, i) => (i === 0 ? 'none' : `${Math.round(100 * b.low)}–${Math.round(100 * b.high)}`));
      // Axis ticks name each bucket by its upper edge, short enough for a phone.
      const ticks = rows.map((b, i) => (i === 0 ? '0' : `${Math.round(100 * b.high)}`));
      const total = rows.reduce((s, b) => s + b.court_days, 0);
      $('den-spread').textContent = `court-days (one court, one day) by % of its hours booked · ${num(total)} court-days · ${who(d)} · ${range(d)}`;
      setChart('c-spread', {
        ...base(t),
        grid: { left: 40, right: 8, top: 18, bottom: 36 },
        tooltip: { ...base(t).tooltip, trigger: 'axis',
          formatter: (ps) => { const i = ps[0].dataIndex, b = rows[i]; return `<b>${i === 0 ? 'Nothing booked' : `${labels[i]}% of hours booked`}</b><br/>${num(b.court_days)} court-days<br/><span style="color:${t.ink3}">${pct(total ? b.court_days / total : null)} of all court-days</span>`; } },
        xAxis: xCat(t, ticks, { name: '% of the day booked (up to)', nameLocation: 'middle', nameGap: 22, nameTextStyle: { color: t.ink3, fontSize: 11 }, axisLabel: { color: t.ink3, fontSize: 10, interval: 0 } }),
        yAxis: yVal(t),
        series: [{
          type: 'bar', barMaxWidth: 24, barCategoryGap: '20%',
          data: rows.map((b, i) => ({ value: b.court_days, itemStyle: { color: i === 0 ? t.axis : t.accent, borderRadius: [4, 4, 0, 0] } })),
          label: { show: true, position: 'top', color: t.ink2, fontSize: 10, formatter: (p) => (total && p.value / total >= 0.08 ? pct(p.value / total) : '') },
        }],
      }, `Histogram of court-days by share of hours booked, ${range(d)}.`);
    } else {
      const labels = d.lead_histogram.map((b) => b.label);
      const counts = d.lead_histogram.map((b) => b.count);
      const n = d.lead_time.n;
      $('den-spread').textContent = `hours between booking and play · ${num(n)} Hudle bookings (venue blocks carry no time) · ${who(d)} · ${range(d)}`;
      if (!n) {
        setChart('c-spread', { ...base(theme()), title: { text: 'No Hudle bookings here: this venue records its sales as blocks, which carry no booking time.', left: 'center', top: 'middle', textStyle: { color: theme().ink3, fontSize: 12, fontWeight: 'normal', width: 260, overflow: 'break' } } }, 'No booking lead times for this view.');
        return;
      }
      setChart('c-spread', {
        ...base(t),
        grid: { left: 40, right: 8, top: 18, bottom: 36 },
        tooltip: { ...base(t).tooltip, trigger: 'axis', formatter: (ps) => `<b>Booked ${labels[ps[0].dataIndex]} ahead</b><br/>${num(ps[0].value)} bookings · ${pct(ps[0].value / n)}` },
        xAxis: xCat(t, labels, { name: 'booked this far ahead', nameLocation: 'middle', nameGap: 22, nameTextStyle: { color: t.ink3, fontSize: 11 }, axisLabel: { color: t.ink3, fontSize: 10, interval: 0 } }),
        yAxis: yVal(t),
        series: [{ type: 'bar', data: counts, barMaxWidth: 24, itemStyle: { color: t.accent, borderRadius: [4, 4, 0, 0] },
          label: { show: true, position: 'top', color: t.ink2, fontSize: 10, formatter: (p) => (p.value / n >= 0.08 ? pct(p.value / n) : '') } }],
      }, `Histogram of booking lead times, ${range(d)}.`);
    }
  }

  // ---------- wiring ----------
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ht.theme', next); } catch (_) {}
    if (state.data) draw(state.data);
  });
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
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.venue && e.target.tagName !== 'INPUT') selectVenue(state.venue); });

  segmented('sport', 'sport');
  segmented('window', 'window');
  minis();
  tabs();
  freshness();
  load();
})();
