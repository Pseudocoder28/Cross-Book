/* pages/arb-model.js — how much one leg's price is worth right now.

   An edge on /arb is two prices subtracted, and the two venues deliver prices
   in different ways, so "is this price current" has two different answers:

     Kalshi is STREAMED. Every change to the book is pushed as it happens, so
     a book with no gap on a live socket is exact however long ago it last
     changed. Seven minutes without an update means nobody touched the market,
     not that the price is seven minutes old. The only thing that makes a
     Kalshi price untrustworthy is the socket being down or the book being
     structurally broken.

     Polymarket US is POLLED, one market at a time, round-robin. Its price is
     a photograph: exactly as old as the last poll, and with N markets on the
     rota that is anywhere from 0 to N x 2.2s. Here the age IS the freshness.

   The old column judged both legs by one rule — "updated within the staleness
   limit" — with a 5s limit for Kalshi and three poll cycles (minutes) for
   Polymarket. So it printed K:QUIET over the exact price and P:OK over the
   one that might be a minute old: true, and pointed at the wrong leg.

   No DOM here; tests/js/arb-model.test.mjs pins it. */

// Kalshi: how long without a change before the detail line mentions it. The
// same figure the MONITOR depth panel calls QUIET.
export const QUIET_AFTER_MS = 5000;
// Polymarket: a poll is OVERDUE once the quote is older than this many cycles.
// 1.0 would flap on ordinary jitter (a slow response, one rate-limit retry).
export const OVERDUE_CYCLES = 1.5;

/** "34s", "6m 48s", "2h 05m" — two units, because on this page the difference
    between 1m and 1m 50s is the difference between one poll cycle and two. */
export function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
}

/** The table cell is narrow: one unit. */
function fmtShort(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 100) return s + "s";
  if (s < 6000) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h";
}

function structural(leg) {
  if (!leg || !leg.has_book) return { text: "NONE", level: "bad", detail: "NO BOOK YET" };
  if (!leg.valid && leg.reason && leg.reason !== "stale") {
    const r = String(leg.reason).toUpperCase().replace(/_/g, " ");
    return { text: r.slice(0, 7), level: "bad", detail: "NOT USABLE · " + r + " · RESYNCING" };
  }
  return null;
}

/** The Kalshi leg. `feed` is the venue state from /api/status ("live",
    "connecting", "down"…) or null before the first status frame. */
export function kalshiLeg(leg, ageMs, feed) {
  const bad = structural(leg);
  if (bad) return bad;
  const since = ageMs == null ? "" : fmtElapsed(ageMs);
  if (feed == null) return { text: "—", level: "wait", detail: "WAITING FOR THE FEED STATUS" };
  if (feed !== "live") {
    // The book is whatever it was when the socket dropped. Never LIVE.
    return { text: "FROZEN", level: "bad",
      detail: "FEED " + String(feed).toUpperCase() + " · PRICE FROZEN" + (since ? " " + since + " AGO" : "") };
  }
  const quiet = ageMs != null && ageMs > QUIET_AFTER_MS;
  return { text: "LIVE", level: "ok",
    detail: "LIVE · STREAMED" + (quiet ? " · NO CHANGE FOR " + since : "") };
}

/** The Polymarket US leg. `cycleMs` is one full round-robin (targets / rate),
    or null when it is not known yet. */
export function polymarketLeg(leg, ageMs, cycleMs) {
  const bad = structural(leg);
  if (bad) return bad;
  if (ageMs == null) return { text: "—", level: "wait", detail: "POLLED · AGE NOT KNOWN YET" };
  const every = cycleMs ? " · RE-READ EVERY " + fmtElapsed(cycleMs) : "";
  const overdue = leg.reason === "stale" || (cycleMs != null && cycleMs > 0 && ageMs > cycleMs * OVERDUE_CYCLES);
  if (overdue) {
    return { text: fmtShort(ageMs), level: "bad",
      detail: "OVERDUE · POLLED " + fmtElapsed(ageMs) + " AGO" + (cycleMs ? " · DUE EVERY " + fmtElapsed(cycleMs) : "") };
  }
  return { text: fmtShort(ageMs), level: "ok", detail: "POLLED " + fmtElapsed(ageMs) + " AGO" + every };
}

/** One round-robin in ms from the stats frame's polymarket_us block. */
export function pollCycleMs(pm) {
  if (!pm || !pm.targets || !pm.rate_per_s) return null;
  return (pm.targets / pm.rate_per_s) * 1000;
}

/** A book frame's age now: what the server measured plus the time since. */
export function ageNow(book, now) {
  if (!book || typeof book.age_ms !== "number") return null;
  return book.age_ms + Math.max(0, now - book.recvAt);
}
