/* core/format.js — pure formatters, no DOM, no state.
   Ported byte-for-byte from the pre-multipage static/app.js (git history). If you need a formatter that is
   not here, put it in YOUR page module, not in this file. */

export const nf = new Intl.NumberFormat("en-US");

const utcFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
});

const etWhenFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false,
});

export function fmtCents(ticks) {
  return ticks == null ? "—" : (ticks / 100).toFixed(2);
}

export function fmtMid(ticks) {
  if (ticks == null) return "—";
  return Number.isInteger(ticks) ? (ticks / 100).toFixed(2) : (ticks / 100).toFixed(3);
}

export function fmtQty(e4) {
  if (e4 == null) return "—";
  const neg = e4 < 0;
  const a = Math.abs(e4);
  const whole = Math.floor(a / 10000);
  const frac = a % 10000;
  let s = nf.format(whole);
  if (frac) s += "." + String(frac).padStart(4, "0").replace(/0+$/, "");
  return (neg ? "-" : "") + s;
}

export function fmtSignedQty(e4) {
  if (e4 == null) return "—";
  return (e4 >= 0 ? "+" : "") + fmtQty(e4);
}

export function fmtMs(v) {
  if (v == null || !isFinite(v)) return "—";
  return (v < 10 ? v.toFixed(1) : String(Math.round(v))) + "ms";
}

export function fmtAge(ms) {
  return ms < 1000 ? Math.round(ms) + "ms" : (ms / 1000).toFixed(1) + "s";
}

/** An elapsed duration, one unit, terse enough for a table column.

    Seconds only up to a minute, then m / h / d. The audit trail is the one
    place in the terminal that shows ages beyond a minute, and seconds-only
    rendered a day-old row as "76523s AGO" — a number nobody reads as 21
    hours. Larger units floor rather than round: "2h" for anything in the
    third hour is honest, "3h" would not be. */
export function fmtAgo(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

export function fmtRate(r) {
  if (r == null || !isFinite(r)) return "—";
  return (r >= 100 ? String(Math.round(r)) : r >= 10 ? r.toFixed(0) : r.toFixed(1)) + "/s";
}

export function fmtUptime(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.floor(s);
  const d = Math.floor(s / 86400);
  const h = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return (d ? d + "d " : "") + h + ":" + m + ":" + ss;
}

export function fmtTapeTime(tsMs) {
  const d = new Date(tsMs);
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return mm + ":" + ss + "." + Math.floor(d.getUTCMilliseconds() / 100);
}

export function percentile(arr, p) {
  if (!arr.length) return null;
  const a = arr.slice().sort((x, y) => x - y);
  return a[Math.min(a.length - 1, Math.round(p * (a.length - 1)))];
}

// Ticks are $0.0001, so cents and implied probability share a number:
// 50 ticks = 0.50¢ = 0.50%.
export function fmtCentsPct(ticks) {
  if (ticks == null) return "—";
  return fmtCents(ticks) + "¢ · " + (ticks / 100).toFixed(2) + "%";
}

export function fmtCount(v) {
  return v == null || !isFinite(v) ? "—" : nf.format(Math.round(v));
}

export function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const utc = d.toISOString().slice(0, 16).replace("T", " ") + "Z";
  return utc + " · " + etWhenFmt.format(d).replace(",", "") + " ET";
}

export function fmtSignedCents(ticks) {
  if (ticks == null) return "—";
  const c = ticks / 100;
  return (c > 0 ? "+" : "") + c.toFixed(2) + "¢";
}

export function fmtDollarsFromTicks(ticks) {
  if (ticks == null) return "—";
  return (ticks < 0 ? "-$" : "$") + (Math.abs(ticks) / 10000).toFixed(2);
}

export function fmtClockUtc(tsMs) {
  if (tsMs == null || !isFinite(tsMs)) return "—";
  const d = new Date(tsMs);
  if (isNaN(d.getTime())) return "—";
  return utcFmt.format(d);
}

// Ticks per contract from a ledger total: net_ticks is a dollar total in
// $0.0001 ticks, qty is in 0.0001-contract units, so the ratio needs the
// 10000 back to land on per-contract ticks. Guarded for qty 0.
export function perContractTicks(netTicks, qty) {
  if (netTicks == null || !qty || qty <= 0) return null;
  return (netTicks * 10000) / qty;
}
