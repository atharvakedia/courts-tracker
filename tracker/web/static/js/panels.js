/**
 * The panel shell and its three non-data states.
 *
 * Every panel is built the same way on purpose: a title, then a denominator
 * line naming the numerator, the denominator and the exact window, then the
 * figure, then the caveats the analytics layer attached. The denominator line
 * is not decoration — it is the smallest unit of honesty this dashboard has,
 * and putting it in the same place every time is what makes it readable.
 */

import { esc, sentence } from './format.js';
import { disposeIn } from './charts.js';
import { enter, initSegmented } from './ui.js';

/** The denominator line, built from the response envelope's own metric block. */
export function denominatorLine(metric, extra) {
  if (!metric) return '';
  const range = metric.date_range || {};
  const bits = [
    `<b>${esc(metric.numerator)}</b> ÷ <b>${esc(metric.denominator)}</b>`,
    range.label ? `<span class="den-range">${esc(range.label)}</span>` : null,
    extra || null,
  ].filter(Boolean);
  return bits.join('<span class="den-sep">/</span>');
}

/**
 * Draw a panel. `body` is HTML; `mount` runs after it is in the document and
 * is where charts attach, because ECharts measures the element it is given.
 */
export function renderPanel(el, spec) {
  if (!el) return;
  disposeIn(el);
  const titleId = `t-${el.id.replace(/^panel-/, '')}`;
  const caveats = (spec.caveats || []).filter(Boolean);

  el.innerHTML = `
    <div class="panel__head">
      <div class="panel__titlerow">
        <h3 class="panel__title" id="${esc(titleId)}">${esc(spec.title)}</h3>
        ${spec.aside || ''}
      </div>
      ${spec.denominator ? `<p class="panel__den">${spec.denominator}</p>` : ''}
    </div>
    <div class="panel__body${spec.flush ? ' panel__body--flush' : ''}">${spec.body || ''}</div>
    ${
      caveats.length
        ? `<div class="panel__foot">
             <button class="disclose" type="button" aria-expanded="false" data-caveat-toggle>
               ${caveats.length === 1 ? 'What this number does not say' : `What these numbers do not say (${caveats.length})`}
             </button>
             <div class="caveats" hidden>${caveats
               .map((c) => `<p class="caveat">${esc(sentence(c))}</p>`)
               .join('')}</div>
           </div>`
        : ''
    }
  `;
  el.classList.remove('is-stale');
  enter(el);

  const toggle = el.querySelector('[data-caveat-toggle]');
  if (toggle) {
    const list = el.querySelector('.caveats');
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!open));
      list.hidden = open;
      if (!open) {
        list.classList.remove('reveal');
        void list.offsetWidth;
        list.classList.add('reveal');
      }
    });
  }

  initSegmented(el);
  if (typeof spec.mount === 'function') spec.mount(el);
}

export function skeletonPanel(el, title) {
  if (!el) return;
  disposeIn(el);
  el.innerHTML = `
    <div class="panel__head">
      <div class="panel__titlerow"><h3 class="panel__title">${esc(title)}</h3></div>
      <p class="panel__den"><span class="skel" style="display:inline-block;width:min(42ch,80%);height:.8em"></span></p>
    </div>
    <div class="skel-stack" aria-hidden="true">
      <div class="skel" style="height:190px"></div>
      <div class="skel skel-line" style="width:38%"></div>
    </div>
    <p class="visually-hidden">Loading ${esc(title)}…</p>
  `;
}

/**
 * The empty state. It never says "no data" on its own: the API always sends a
 * sentence explaining which of the four empties this is — nothing collected
 * yet, nothing matching the filters, only one snapshot so no change can be
 * seen, or genuinely zero rows — and that sentence is the whole point.
 */
export function emptyState(reason, { title = 'Nothing to plot yet' } = {}) {
  return `
    <div class="state">
      <span class="state__mark" aria-hidden="true">∅</span>
      <p class="state__title">${esc(title)}</p>
      <p class="state__body">${esc(sentence(reason) || 'The API returned no rows and gave no reason.')}</p>
    </div>`;
}

export function errorState(message) {
  return `
    <div class="state state--error">
      <span class="state__mark" aria-hidden="true">!</span>
      <p class="state__title">This panel could not load</p>
      <p class="state__body">${esc(sentence(message))} Every other panel on this page fetched
      separately, so the rest of the dashboard is unaffected.</p>
    </div>`;
}

export function failPanel(el, title, message) {
  renderPanel(el, { title, body: errorState(message) });
}

/**
 * Hold a panel's current figures while the next ones are fetched.
 *
 * A filter change is new data for a page that is already drawn, so the reader
 * keeps the old answer — dimmed, and not clickable, so it cannot be mistaken
 * for the live one — instead of watching fifteen panels blink back to
 * skeletons. Panels that have nothing to hold on to fall back to the skeleton.
 */
export function markStale(el) {
  if (!el) return false;
  if (!el.querySelector('.panel__title')) return false;
  el.classList.add('is-stale');
  return true;
}

/** `metric.caveats` already merges the static text with each spec's own. */
export function caveatsOf(payload) {
  return (payload && payload.metric && payload.metric.caveats) || [];
}
