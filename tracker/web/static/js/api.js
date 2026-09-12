/**
 * The API client. Same origin, GET only, and it never throws at the caller:
 * every call resolves to `{ok, data, error}` so one failing endpoint leaves
 * one panel in an error state instead of blanking the page.
 */

const BASE = 'api';

/** Requests in flight, so a filter change can abandon the previous round. */
let generation = 0;

export function newGeneration() {
  generation += 1;
  return generation;
}

export function isCurrent(gen) {
  return gen === generation;
}

function buildUrl(path, params = {}) {
  const url = new URL(`${BASE}/${path}`.replace(/\/+/g, '/'), window.location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

export async function get(path, params = {}, { signal } = {}) {
  const url = buildUrl(path, params);
  try {
    const response = await fetch(url, {
      signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }
    if (!response.ok) {
      const detail =
        body && typeof body.detail === 'string'
          ? body.detail
          : `the server answered ${response.status}`;
      return { ok: false, data: null, error: detail, status: response.status };
    }
    return { ok: true, data: body, error: null, status: response.status };
  } catch (err) {
    if (err && err.name === 'AbortError') {
      return { ok: false, data: null, error: null, aborted: true };
    }
    return {
      ok: false,
      data: null,
      error: 'the dashboard could not reach its own API — is the server still running?',
      status: 0,
    };
  }
}

/** The filter set every metric endpoint accepts, as query parameters. */
export function filterParams(filters) {
  return {
    start: filters.start,
    end: filters.end,
    sport: filters.sport,
    venue: filters.venue,
  };
}
