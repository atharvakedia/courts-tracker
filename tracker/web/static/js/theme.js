/**
 * Theme. The inline script in the document head has already stamped
 * `data-theme` before first paint; this module only handles the toggle, the
 * persistence and the one event every chart listens to.
 */

const KEY = 'padel.theme';
const EVENT = 'padel:themechange';

export function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

export function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (_) {
    return false;
  }
}

/** Design tokens read straight off the root, so CSS stays the single source. */
export function tokens() {
  const css = getComputedStyle(document.documentElement);
  const t = (name) => css.getPropertyValue(name).trim();
  return {
    ink: t('--ink'),
    inkSoft: t('--ink-soft'),
    inkFaint: t('--ink-faint'),
    line: t('--line'),
    lineStrong: t('--line-strong'),
    surface: t('--surface'),
    surfaceSunk: t('--surface-sunk'),
    booked: t('--booked'),
    bookedSoft: t('--booked-soft'),
    open: t('--open'),
    openSoft: t('--open-soft'),
    blocked: t('--blocked'),
    blockedSoft: t('--blocked-soft'),
    alert: t('--alert'),
    rampNull: t('--ramp-null'),
    ramp: [t('--ramp-0'), t('--ramp-1'), t('--ramp-2'), t('--ramp-3'), t('--ramp-4'), t('--ramp-5')],
    venue: [t('--v1'), t('--v2'), t('--v3')],
    font:
      "'Archivo', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
  };
}

function apply(mode, { animate }) {
  const swap = () => {
    document.documentElement.dataset.theme = mode;
    syncToggle();
  };
  if (animate && !prefersReducedMotion() && document.startViewTransition) {
    document.startViewTransition(swap);
  } else {
    swap();
  }
  try {
    localStorage.setItem(KEY, mode);
  } catch (_) {
    /* private mode: the choice lasts for this page view only */
  }
  // Charts re-theme on the next frame, after the new tokens are computable.
  requestAnimationFrame(() => {
    window.dispatchEvent(new CustomEvent(EVENT, { detail: { mode } }));
  });
}

function syncToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  btn.setAttribute('aria-label', `Switch to ${next} theme`);
}

export function onThemeChange(handler) {
  window.addEventListener(EVENT, handler);
}

export function initTheme() {
  syncToggle();
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      apply(currentTheme() === 'dark' ? 'light' : 'dark', { animate: true });
    });
  }

  // Follow the system only while the reader has not made a choice of their own.
  try {
    const stored = localStorage.getItem(KEY);
    if (stored !== 'dark' && stored !== 'light') {
      const query = window.matchMedia('(prefers-color-scheme: dark)');
      const follow = (event) => {
        document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
        syncToggle();
        requestAnimationFrame(() => {
          window.dispatchEvent(
            new CustomEvent(EVENT, { detail: { mode: currentTheme() } })
          );
        });
      };
      if (query.addEventListener) query.addEventListener('change', follow);
    }
  } catch (_) {
    /* no storage, no system follow: the toggle still works */
  }
}

export { EVENT as THEME_EVENT };
