/**
 * Formatting only. Nothing here derives a metric.
 *
 * Occupancy ratios, court-minute normalisation and the business-date rollback
 * are computed once, in Python, and arrive precomputed. If this file ever
 * divides one field by another the dashboard has a second implementation of
 * the crux logic, and the two will eventually disagree.
 */

const TZ = 'Asia/Kolkata';

const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const pct0 = new Intl.NumberFormat('en-IN', { style: 'percent', maximumFractionDigits: 0 });
const pct1 = new Intl.NumberFormat('en-IN', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
const money = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });

const dayFmt = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
const dayYearFmt = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
const weekdayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short' });
const stampFmt = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: TZ,
});
const clockFmt = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: TZ,
});

/** A plain `YYYY-MM-DD` business date, as a local Date with no zone shift. */
export function parseDay(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

export function dayLabel(iso) {
  const d = parseDay(iso);
  return d ? dayFmt.format(d) : '—';
}

export function dayLabelFull(iso) {
  const d = parseDay(iso);
  return d ? dayYearFmt.format(d) : '—';
}

export function dayWithWeekday(iso) {
  const d = parseDay(iso);
  return d ? `${weekdayFmt.format(d)} ${dayFmt.format(d)}` : '—';
}

/** An instant, shown in the courts' own wall-clock zone rather than the viewer's. */
export function stamp(iso) {
  if (!iso) return '—';
  return `${stampFmt.format(new Date(iso))} IST`;
}

export function clock(iso) {
  if (!iso) return '—';
  return clockFmt.format(new Date(iso));
}

export function hourLabel(h) {
  return `${String(h).padStart(2, '0')}:00`;
}

export function pct(value, { places = 0, dash = '—' } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return (places === 0 ? pct0 : pct1).format(value);
}

export function num(value, { places = 1, dash = '—' } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return (places === 0 ? nf0 : nf1).format(value);
}

export function int(value, { dash = '—' } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return nf0.format(value);
}

export function rupees(value, { dash = '—' } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  return `₹${money.format(value)}`;
}

/** Hours as the eye reads them: 4.2 h, 1 d 6 h, 38 min. */
export function hours(value, { dash = '—' } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return dash;
  if (value < 1) return `${Math.round(value * 60)} min`;
  if (value < 48) return `${nf1.format(value)} h`;
  const d = Math.floor(value / 24);
  const h = Math.round(value - d * 24);
  return h ? `${d} d ${h} h` : `${d} d`;
}

export function minutes(value, { dash = '—' } = {}) {
  if (value === null || value === undefined) return dash;
  if (value < 90) return `${Math.round(value)} min`;
  return hours(value / 60);
}

export function signedPct(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const s = pct1.format(Math.abs(value));
  if (Math.abs(value) < 0.0005) return 'no difference';
  return value > 0 ? `+${s}` : `−${s}`;
}

export function plural(n, one, many) {
  return `${int(n, { dash: '0' })} ${n === 1 ? one : many ?? `${one}s`}`;
}

/** Escape before any string from the API reaches innerHTML. */
export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** A sentence that starts with a capital and ends with a stop. */
export function sentence(text) {
  if (!text) return '';
  const t = String(text).trim();
  const capped = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(capped) ? capped : `${capped}.`;
}

export { TZ };
