/* pages/control-model.js — the arithmetic and judgement behind /control.

   /control is the one page whose buttons write. What it sends is decided
   here, without a DOM, so tests/js/control-model.test.mjs can pin it: a unit
   slip on this page is a wrong risk limit on a live trader, applied with a
   green receipt.

   Units. The wire speaks ticks ($0.0001) and Qty (1e-4 contracts); an operator
   thinks in cents per contract, contracts and dollars — the units /arb and
   /paper already print. Conversion happens here and only here, and it is
   exact or it is refused: 0.505¢ is not a whole number of ticks, so it is an
   error, never a silent round. */

export const TICKS_PER_CENT = 100;
export const TICKS_PER_DOLLAR = 10000;
export const QTY_PER_CONTRACT = 10000;

// The server's own bounds (control.py _validate_paper_limits). Mirrored so the
// page can say WHICH field is wrong before a round trip; the server still
// re-checks everything.
export const MAX_MIN_NET_TICKS = 10000;        // 100¢
export const MAX_CONTRACTS = 10000000;
export const MAX_NOTIONAL_TICKS = 1e12;

const nf = new Intl.NumberFormat("en-US");

/** "1,000", "$1000", "0.50¢", " 12 " → a finite number, or null. */
export function parseNumber(text) {
  const s = String(text == null ? "" : text).replace(/[$¢,\s]/g, "");
  if (s === "" || !/^-?(\d+\.?\d*|\.\d+)$/.test(s)) return null;
  const v = Number(s);
  return Number.isFinite(v) ? v : null;
}

/** x * scale as an exact integer, or null when it is not one. Multiplying a
    decimal by 100 in floating point gives 56.99999999999999 for 0.57, so the
    test is "within 1e-6 of an integer", then round. */
function exactInt(x, scale) {
  const v = x * scale;
  const r = Math.round(v);
  return Math.abs(v - r) < 1e-6 ? r : null;
}

export function fmtCentsField(ticks) {
  return ticks == null ? "" : (ticks / TICKS_PER_CENT).toFixed(2);
}
export function fmtContractsField(qty) {
  if (qty == null) return "";
  return String(qty / QTY_PER_CONTRACT);
}
export function fmtDollarsField(ticks) {
  if (ticks == null) return "";
  const d = ticks / TICKS_PER_DOLLAR;
  return Number.isInteger(d) ? String(d) : d.toFixed(2);
}
/** Ticks as dollars and cents. Rounded to the cent FIRST, so 19,999 ticks is
    $2.00 and never "$1.100". */
export function usd(ticks) {
  const cents = Math.round(ticks / 100);
  return "$" + nf.format(Math.floor(cents / 100)) + "." + String(cents % 100).padStart(2, "0");
}

/** The three risk limits, as typed, against what the trader has now.

    `current`  {min_net_ticks, max_qty_per_pair, max_notional_ticks}
    `text`     {minEdge, maxPair, maxSpend} — the fields' strings
    `deployed` notional already committed, in ticks

    Returns per-field {error, changed, was}, whether the whole form is valid
    and dirty, the params to POST (wire units), the change list for the preview
    line, and warnings that do not block (a cap below what is deployed is a
    legitimate panic button, but the operator should know what it does). */
export function limitsDraft(current, text, deployed) {
  const cur = current || {};
  const fields = {};
  const changes = [];
  const warnings = [];
  const params = {};

  // min edge: cents per contract → ticks
  {
    const f = { error: null, changed: false, was: fmtCentsField(cur.min_net_ticks) + "¢" };
    const v = parseNumber(text.minEdge);
    let ticks = null;
    if (v == null) f.error = "a number of cents, e.g. 0.50";
    else if (v < 0) f.error = "cannot be negative";
    else if ((ticks = exactInt(v, TICKS_PER_CENT)) == null) f.error = "to the nearest 0.01¢";
    else if (ticks > MAX_MIN_NET_TICKS) f.error = "at most 100¢ — a contract pays $1";
    if (!f.error) {
      params.min_net_ticks = ticks;
      f.changed = ticks !== cur.min_net_ticks;
      if (f.changed) changes.push("min edge " + f.was + " → " + fmtCentsField(ticks) + "¢");
      if (ticks === 0) warnings.push("a 0¢ floor takes every edge, however thin");
    }
    fields.minEdge = f;
  }
  // max per pair: whole contracts → Qty
  {
    const was = cur.max_qty_per_pair == null ? null : cur.max_qty_per_pair / QTY_PER_CONTRACT;
    const f = { error: null, changed: false, was: was == null ? "" : nf.format(was) + " cts" };
    const v = parseNumber(text.maxPair);
    if (v == null) f.error = "a whole number of contracts";
    else if (!Number.isInteger(v)) f.error = "whole contracts only";
    else if (v < 1) f.error = "at least 1 contract";
    else if (v > MAX_CONTRACTS) f.error = "at most " + nf.format(MAX_CONTRACTS);
    if (!f.error) {
      params.max_cts_per_pair = v;
      f.changed = v !== was;
      if (f.changed) changes.push("max per pair " + f.was + " → " + nf.format(v) + " cts");
    }
    fields.maxPair = f;
  }
  // max total spend: dollars → ticks
  {
    const f = { error: null, changed: false, was: cur.max_notional_ticks == null ? "" : usd(cur.max_notional_ticks) };
    const v = parseNumber(text.maxSpend);
    let ticks = null;
    if (v == null) f.error = "a dollar amount";
    else if (v < 0) f.error = "cannot be negative";
    else if ((ticks = exactInt(v, TICKS_PER_DOLLAR)) == null) f.error = "too many decimal places";
    else if (ticks > MAX_NOTIONAL_TICKS) f.error = "too large";
    if (!f.error) {
      params.max_notional_ticks = ticks;
      f.changed = ticks !== cur.max_notional_ticks;
      if (f.changed) changes.push("max spend " + f.was + " → " + usd(ticks));
      if (deployed != null && ticks < deployed) {
        warnings.push("below the " + usd(deployed) + " already deployed: no new trades until it is raised. Nothing is unwound.");
      }
    }
    fields.maxSpend = f;
  }
  const valid = !fields.minEdge.error && !fields.maxPair.error && !fields.maxSpend.error;
  const dirty = fields.minEdge.changed || fields.maxPair.changed || fields.maxSpend.changed
    || !valid;
  return { fields, valid, dirty, canApply: valid && changes.length > 0, params: valid ? params : null, changes, warnings };
}

/** A universe list as typed, against the BASE list the server holds.

    The editable list is the base universe only. The legs of watched pairs are
    added by the engine on top of it and shown separately: the old page put the
    union in the box and wrote the union back as the base, so one press of
    SUBSCRIBE turned every watched pair's leg into a permanent base market. */
export function listDraft(currentBase, text, opts) {
  const o = opts || {};
  const norm = o.venue === "kalshi" ? (s) => s.toUpperCase() : (s) => s.toLowerCase();
  const raw = String(text || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean).map(norm);
  const seen = new Set();
  const items = [];
  let dupes = 0;
  for (const t of raw) {
    if (seen.has(t)) { dupes += 1; continue; }
    seen.add(t);
    items.push(t);
  }
  const cur = (currentBase || []).map(norm);
  const curSet = new Set(cur);
  const added = items.filter((t) => !curSet.has(t));
  const removed = cur.filter((t) => !seen.has(t));
  const dirty = added.length > 0 || removed.length > 0;
  // Only a CHANGE can be wrong: an untouched list is never scolded, even an
  // empty one (a process with no feed attached has an empty base).
  let error = null;
  if (dirty && !items.length && !o.allowEmpty) error = "at least one market — an empty subscription is refused by the venue";
  else if (dirty && o.maxItems && items.length > o.maxItems) error = "at most " + o.maxItems + " markets";
  return { items, added, removed, dupes, dirty, error, canApply: dirty && !error };
}

/** What watching N pairs costs, estimated as it is typed. An upper bound: a
    pair whose Polymarket leg is already a base target adds nothing. The exact
    figure comes from the server's preview when SET is pressed. */
export function watchEstimate(n, pairs, polymarket) {
  if (n == null || !pairs || !polymarket) return null;
  const interval = pairs.poll && pairs.poll.interval_s;
  const base = (polymarket.base || []).length;
  if (!interval) return null;
  const take = Math.min(n, pairs.confirmed == null ? n : pairs.confirmed);
  const targets = base + take;
  return { pairs: take, targets, cycleS: targets * interval };
}

export function parseWatchN(text, max) {
  const v = parseNumber(text);
  if (v == null || !Number.isInteger(v)) return { n: null, error: "a whole number" };
  if (v < 0) return { n: null, error: "cannot be negative" };
  if (v > (max || 200)) return { n: null, error: "at most " + (max || 200) };
  return { n: v, error: null };
}

/** Which section of the page an action belongs to — where its confirm box and
    its receipt appear, so the answer shows up where the question was asked. */
export function sectionOf(action) {
  if (action === "universe.kalshi") return "kalshi";
  if (action === "universe.polymarket") return "polymarket";
  const head = String(action).split(".")[0];
  return { recording: "recorder", paper: "paper", pairs: "watch", jobs: "jobs" }[head] || "jobs";
}

/** Set-replacing actions are previewed before they run: one press wipes a
    hand-built set or costs a feed gap, so the operator sees the server's own
    sentence first. Single-value changes (a switch, a limit) apply at once —
    a confirm step is a tax, spent only where a slip is expensive. */
export const PREVIEW_FIRST = new Set(["pairs.top", "pairs.track", "universe.kalshi", "universe.polymarket"]);

/** The newest job for each job name, for the JOBS table. */
export function latestJobs(jobs) {
  const out = {};
  for (const j of jobs || []) {
    const cur = out[j.name];
    if (!cur || (j.started_ts_ns || 0) > (cur.started_ts_ns || 0)) out[j.name] = j;
  }
  return out;
}

export function jobProgress(j) {
  if (!j) return null;
  const frac = j.total ? Math.max(0, Math.min(1, j.step / j.total)) : null;
  const where = [j.phase, j.total ? nf.format(j.step) + "/" + nf.format(j.total) : null, j.message]
    .filter(Boolean).join(" · ");
  return { frac, where };
}
