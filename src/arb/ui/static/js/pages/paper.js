/* pages/paper.js — PAPER: the simulated-fill ledger.

   Ported from the pre-multipage app.js, see git history (openPaper/closePaper/movePaper/renderPaper/onPaperKey
   and the 3s /api/paper poll). Three things are new, and all three exist
   because the ported screen printed numbers without telling you where they
   were going:

     - a cumulative expected-net curve over the trade sequence, so the ledger
       has a direction of travel instead of a stack of rows;
     - a per-pair filter driven by the POSITIONS list, so "which pair is this
       coming from" is one click rather than a read of forty rows;
     - limits shown as capacity USED, not as three constants. "MAX NOTIONAL
       $1000.00" says nothing on its own; "$12.40 · 1% OF $1000.00" says
       whether the run is anywhere near its ceiling. That reading only exists
       while the trader is ON: with it off the server sends PaperLimits()
       defaults (nothing enforces them) and totals summed over stored trades
       from EVERY run, so the meters drop the percentage and the screen says
       what the numbers are instead of inventing a ceiling.

   The poll is started in mount() and cleared in unmount(): a 3s fetch that
   outlives navigation is exactly the bug the spec names. The `paper` WS frame
   is handled at module load, so trades pushed while you are on another page
   still land in the ledger. */

import { $, el, setText } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { onMessage } from "../core/ws.js";
import * as cmd from "../core/cmd.js";
import {
  nf, fmtCents, fmtQty, fmtWhen, fmtSignedCents, fmtDollarsFromTicks,
  fmtClockUtc, perContractTicks,
} from "../core/format.js";

const PAPER_POLL_MS = 3000;
const MAX_TRADES = 200;         // matches PaperTrader.payload(max_trades=200)
const CURVE_H = 44;
const CANVAS_FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const WARN_PCT = 70;            // capacity: amber from here
const HOT_PCT = 95;             // capacity: red from here


let mounted = false;
let pollTimer = 0;
let filterPairId = null;        // null = every pair
let curveHover = null;          // hover x in CSS px, or null

// ported from app.js: the ARB page owns the other copy
function dirLabel(direction) {
  return direction === "yes_a_no_b" ? "BUY YES K · BUY NO P" : "BUY YES P · BUY NO K";
}

// ---------- injected chrome (the markup in index.html predates these) ----------

const posRows = $("pos-rows");
const ptrRows = $("ptr-rows");

posRows.setAttribute("role", "listbox");
posRows.setAttribute("tabindex", "0");

// the TRADES band is the element right above the trades column header
const tradesBand = ptrRows.previousElementSibling.previousElementSibling;

const filterChip = el("button", "pf-chip");
filterChip.type = "button";
filterChip.setAttribute("aria-pressed", "false");
filterChip.title = "SELECT A POSITION TO FILTER THESE TRADES";
// the label can be a 60-character pair name, so it clips and the clear glyph
// is a sibling: the way out of a filter must never be the part CSS eats
const filterLabel = el("span", "pf-chip-t", "ALL PAIRS");
const filterX = el("span", "pf-x", "\u2715");
filterChip.append(filterLabel, filterX);
const filterCount = el("span", "pf-count", "");
tradesBand.append(filterChip, filterCount);

filterChip.addEventListener("click", (e) => {
  setFilter(null);
  if (e.detail > 0) filterChip.blur();   // pointer click: hand the keyboard back to ARB>
});

// cumulative expected-net curve, between the totals strip and POSITIONS
const curveWrap = el("div", "paper-curve");
const curveHead = el("div", "pc-head");
const curveLbl = el("span", "pc-lbl", "CUMULATIVE EXPECTED NET");
const curveStat = el("span", "pc-stat", "—");
curveHead.append(curveLbl, curveStat);
const curve = el("canvas", "pc-canvas");
curve.setAttribute("role", "img");
curve.setAttribute("aria-label", "Cumulative expected net over the trade sequence");
const curveTip = el("div", "pc-tip");
curveTip.hidden = true;
curveWrap.append(curveHead, curve, curveTip);
posRows.parentNode.insertBefore(curveWrap, posRows.previousElementSibling.previousElementSibling);

let cctx = null;
let curveW = 0;

curve.addEventListener("mousemove", (e) => {
  const r = curve.getBoundingClientRect();
  curveHover = e.clientX - r.left;
  schedule("papercurve");
});
curve.addEventListener("mouseleave", () => {
  curveHover = null;
  curveTip.hidden = true;
  schedule("papercurve");
});
window.addEventListener("resize", () => schedule("papercurve"));

// limits -> capacity meters, one under each ceiling the engine enforces
function meter(afterId, noteText) {
  const box = el("div", "pl-meter");
  const track = el("div", "pl-track");
  const fill = el("div", "pl-fill");
  track.append(fill);
  const note = el("div", "pl-note");
  const used = el("span", "pl-used", "—");
  const pctEl = el("span", "pl-pct", noteText);
  note.append(used, pctEl);
  box.append(track, note);
  $(afterId).parentNode.after(box);
  return { box, fill, used, pct: pctEl, track, tag: noteText };
}

// trader off: the three limits below are PaperLimits() defaults the server
// substitutes, not anything configured or enforced. Say so, once, above them.
const limitsCap = el("div", "pl-note pl-solo");
limitsCap.append(el("span", "pl-pct", "DEFAULTS — NOT IN FORCE · TRADER OFF"));
limitsCap.hidden = true;
$("pl-minnet").parentNode.before(limitsCap);

const netNote = el("div", "pl-note pl-solo");
const netUsed = el("span", "pl-used", "—");
const netTag = el("span", "pl-pct", "TIGHTEST TAKEN");
netNote.append(netUsed, netTag);
$("pl-minnet").parentNode.after(netNote);

const qtyMeter = meter("pl-maxqty", "PEAK PAIR");
const notMeter = meter("pl-maxnot", "NOTIONAL USED");

// the footer has to stay true: ENTER now filters, and it did not before
$("paperpage").querySelector(".des-foot").textContent =
  "↑↓ SELECT · ENTER FILTER PAIR · ESC CLOSE · R REFRESH"
  + " · SIMULATED FILLS AT DISPLAYED LIQUIDITY — NO ORDERS";

// ---------- data ----------

function tradesOf(d) {
  return Array.isArray(d.trades) ? d.trades : [];
}

/** The trades the table shows: every one, or one pair's. */
function visibleTrades() {
  const trades = tradesOf(state.paper.data || {});
  if (filterPairId == null) return trades;
  return trades.filter((t) => t.pair_id === filterPairId);
}

function setFilter(pairId) {
  if (filterPairId === pairId) return;
  filterPairId = pairId;
  const p = state.paper;
  // keep the cursor on a row that still exists under the new filter
  const vis = visibleTrades();
  const i = p.selectedId != null ? vis.findIndex((t) => t.id === p.selectedId) : -1;
  if (i >= 0) {
    p.idx = i;
  } else {
    p.idx = 0;
    p.selectedId = vis.length && vis[0].id != null ? vis[0].id : null;
  }
  renderPaper();
}

async function loadPaper() {
  const p = state.paper;
  if (!mounted) return;
  if (p.ctl) p.ctl.abort();
  const ctl = typeof AbortController === "function" ? new AbortController() : null;
  p.ctl = ctl;
  try {
    const r = await fetch("/api/paper", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (!mounted || p.ctl !== ctl) return;
    p.data = data && typeof data === "object" ? data : {};
    p.error = null;
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (!mounted) return;
    p.error = "LOAD FAILED · " + String(err && err.message || err).toUpperCase();
  }
  if (p.ctl === ctl) p.ctl = null;
  schedule("paper");
}

/** `paper` WS frame: {t:"paper", trades:[...]} in ascending id order. Folded
    into the snapshot so trades taken while another page is mounted are there
    the moment you arrive. Before the first poll there is no snapshot to fold
    into — that poll is authoritative and already contains them. */
function onPaperFrame(m) {
  const p = state.paper;
  const d = p.data;
  if (!d || typeof d !== "object") return;
  const incoming = Array.isArray(m.trades) ? m.trades : [];
  if (!incoming.length) return;
  const trades = tradesOf(d);
  const seen = new Set(trades.map((t) => t.id));
  const fresh = incoming.filter((t) => t && t.id != null && !seen.has(t.id));
  if (!fresh.length) return;

  for (const t of fresh) trades.unshift(t);        // ledger is newest first
  d.trades = trades.slice(0, MAX_TRADES);
  d.enabled = true;                                // only a live trader pushes

  const tot = d.totals && typeof d.totals === "object" ? d.totals : {};
  const add = (k, v) => { tot[k] = (typeof tot[k] === "number" ? tot[k] : 0) + v; };
  const positions = Array.isArray(d.positions) ? d.positions : [];
  for (const t of fresh) {
    add("trades", 1);
    add("qty", t.qty || 0);
    add("cost_ticks", t.cost_ticks || 0);
    add("fee_ticks", t.fee_ticks || 0);
    add("net_ticks", t.net_ticks || 0);
    let pos = positions.find((x) => x.pair_id === t.pair_id);
    if (!pos) {
      pos = { pair_id: t.pair_id, label: t.label, qty: 0, cost_ticks: 0, fee_ticks: 0, net_ticks: 0, trades: 0 };
      positions.push(pos);
    }
    pos.qty += t.qty || 0;
    pos.cost_ticks += t.cost_ticks || 0;
    pos.fee_ticks += t.fee_ticks || 0;
    pos.net_ticks += t.net_ticks || 0;
    pos.trades += 1;
  }
  d.totals = tot;
  d.positions = positions;
  schedule("paper");
}

onMessage("paper", onPaperFrame);

function movePaper(d) {
  const p = state.paper;
  const trades = visibleTrades();
  if (!trades.length) return;
  p.idx = Math.max(0, Math.min(trades.length - 1, p.idx + d));
  p.selectedId = trades[p.idx].id != null ? trades[p.idx].id : null;
  renderPaper();
}

// ---------- cumulative expected-net curve ----------

/** Oldest-first running sum of net_ticks over the visible trades. */
function curvePoints() {
  const vis = visibleTrades();
  const pts = [];
  let cum = 0;
  for (let i = vis.length - 1; i >= 0; i--) {
    const t = vis[i];
    cum += typeof t.net_ticks === "number" ? t.net_ticks : 0;
    pts.push({ cum, t });
  }
  return pts;
}

function sizeCurve() {
  const w = Math.max(120, Math.round(curveWrap.clientWidth - 24));
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const pw = Math.round(w * dpr);
  const ph = Math.round(CURVE_H * dpr);
  if (curve.width !== pw || curve.height !== ph) {
    curve.width = pw;
    curve.height = ph;
    curve.style.width = w + "px";
    curve.style.height = CURVE_H + "px";
    cctx = curve.getContext("2d");
    if (cctx) cctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  } else if (!cctx) {
    cctx = curve.getContext("2d");
    if (cctx) cctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  curveW = w;
}

function drawCurve() {
  sizeCurve();
  if (!cctx) return;
  const W = curveW;
  const H = CURVE_H;
  const plotW = W - 64;                  // right gutter for the direct label
  cctx.clearRect(0, 0, W, H);

  const pts = curvePoints();
  const n = pts.length;
  if (!n) {
    cctx.fillStyle = "#6b7280";
    cctx.font = "10px " + CANVAS_FONT;
    cctx.textAlign = "center";
    cctx.fillText(state.paper.data ? "NO TRADES" : "AWAITING DATA", W / 2, H / 2 + 3);
    cctx.textAlign = "left";
    curveTip.hidden = true;
    curveStat.textContent = "—";
    curveStat.className = "pc-stat";
    return;
  }

  // domain always contains zero: the sign of the curve is the whole point
  let lo = 0;
  let hi = 0;
  for (const p of pts) {
    if (p.cum < lo) lo = p.cum;
    if (p.cum > hi) hi = p.cum;
  }
  const pad = Math.max(1, (hi - lo) * 0.12);
  hi += pad;
  lo -= pad;
  const step = n > 1 ? plotW / (n - 1) : 0;
  const xAt = (i) => (n > 1 ? i * step : plotW);
  const yAt = (v) => Math.min(H - 2, Math.max(2, H - 3 - ((v - lo) / (hi - lo)) * (H - 6)));

  const last = pts[n - 1].cum;
  const color = last > 0 ? "#2fe0a0" : last < 0 ? "#ff4f5e" : "#8a9099";
  const zeroY = Math.round(yAt(0)) + 0.5;

  // area to the zero baseline, 8% of the line hue fading out
  const grad = cctx.createLinearGradient(0, last < 0 ? zeroY : 0, 0, last < 0 ? H : zeroY);
  grad.addColorStop(0, last < 0 ? "rgba(255,79,94,0)" : "rgba(47,224,160,0.10)");
  grad.addColorStop(1, last < 0 ? "rgba(255,79,94,0.10)" : "rgba(47,224,160,0)");
  cctx.fillStyle = grad;
  cctx.beginPath();
  cctx.moveTo(xAt(0), zeroY);
  for (let i = 0; i < n; i++) cctx.lineTo(xAt(i), yAt(pts[i].cum));
  cctx.lineTo(xAt(n - 1), zeroY);
  cctx.closePath();
  cctx.fill();

  // zero rule: 1px dotted, the break-even line
  cctx.save();
  cctx.strokeStyle = "#6b7280";
  cctx.lineWidth = 1;
  cctx.setLineDash([1, 2]);
  cctx.beginPath();
  cctx.moveTo(0, zeroY);
  cctx.lineTo(plotW, zeroY);
  cctx.stroke();
  cctx.restore();

  cctx.strokeStyle = color;
  cctx.fillStyle = color;
  cctx.lineWidth = 1.5;
  if (n === 1) {
    cctx.fillRect(xAt(0) - 1, yAt(pts[0].cum) - 1, 2, 2);
  } else {
    cctx.beginPath();
    cctx.moveTo(xAt(0), yAt(pts[0].cum));
    for (let i = 1; i < n; i++) cctx.lineTo(xAt(i), yAt(pts[i].cum));
    cctx.stroke();
  }
  // the newest point, marked: the curve's head is the number that matters
  cctx.fillRect(xAt(n - 1) - 1.5, yAt(last) - 1.5, 3, 3);

  cctx.font = "11px " + CANVAS_FONT;
  cctx.fillText(fmtDollarsFromTicks(last), plotW + 6, Math.max(9, Math.min(H - 2, yAt(last) + 4)));

  curveStat.textContent = nf.format(n) + (n === 1 ? " TRADE · " : " TRADES · ") + fmtDollarsFromTicks(last);
  curveStat.className = "pc-stat" + (last > 0 ? " pos" : last < 0 ? " neg" : "");

  // hover: 1px crosshair + a tooltip naming the trade under it
  if (curveHover != null && curveHover <= plotW + 6 && n > 1) {
    let i = Math.round(curveHover / step);
    i = Math.max(0, Math.min(n - 1, i));
    const hx = Math.round(xAt(i)) + 0.5;
    cctx.strokeStyle = "#8a9099";
    cctx.lineWidth = 1;
    cctx.beginPath();
    cctx.moveTo(hx, 0);
    cctx.lineTo(hx, H);
    cctx.stroke();
    const t = pts[i].t;
    curveTip.textContent = "#" + (t.id != null ? t.id : "—") + " · " + fmtClockUtc(t.ts_ms)
      + " · " + fmtDollarsFromTicks(t.net_ticks) + " · CUM " + fmtDollarsFromTicks(pts[i].cum);
    curveTip.hidden = false;
    curveTip.style.left = (12 + Math.max(0, Math.min(W - 240, hx + 10))) + "px";
  } else {
    curveTip.hidden = true;
  }
}

// ---------- limits vs. actuals ----------

/** `opts.enforced === false` means no ceiling is in force, so there is no
    percentage to draw: the bar stays empty and `opts.tag` names what the
    number on the left actually is. */
function setMeter(m, used, cap, usedText, capText, opts) {
  const enforced = !opts || opts.enforced !== false;
  const tag = (opts && opts.tag) || m.tag;
  const pct = enforced && cap != null && cap > 0 && used != null ? (used / cap) * 100 : null;
  const shown = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  m.fill.style.width = shown.toFixed(1) + "%";
  m.fill.className = "pl-fill" + (pct == null ? " off" : pct >= HOT_PCT ? " hot" : pct >= WARN_PCT ? " warn" : "");
  m.used.textContent = usedText;
  // no reading yet: keep naming what the bar measures rather than printing a
  // second em dash next to the one already in the value column
  m.pct.textContent = pct == null ? tag : (pct > 0 && pct < 1 ? "<1" : Math.round(pct)) + "% OF " + capText;
  m.box.title = pct == null ? usedText + " — " + tag : usedText + " USED OF " + capText;
}

/** `enabled` is the whole story here. With the trader ON these are the limits
    the engine is enforcing against this run's fills, and the meters read as
    capacity used. With it OFF the server sends PaperLimits() defaults and
    totals summed over stored trades from every run, so there is no ceiling to
    take a percentage against and nothing here belongs to this run. */
function renderLimits(totals, limits, positions, trades, enabled, loaded) {
  setText("pl-minnet", limits.min_net_ticks != null ? fmtSignedCents(limits.min_net_ticks) : "—");
  setText("pl-maxqty", limits.max_qty_per_pair != null ? fmtQty(limits.max_qty_per_pair) + " CTS" : "—");
  setText("pl-maxnot", fmtDollarsFromTicks(limits.max_notional_ticks));

  const stale = loaded && !enabled;
  limitsCap.hidden = !stale;
  const defTitle = stale ? "DEFAULT — THE TRADER IS OFF, NOTHING IS ENFORCING THIS" : "";
  $("pl-minnet").title = defTitle;
  $("pl-maxqty").title = defTitle;
  $("pl-maxnot").title = defTitle;

  // the floor is a filter; the honest actual is the tightest edge it let through
  let tightest = null;
  for (const t of trades) {
    const v = perContractTicks(t.net_ticks, t.qty);
    if (v != null && (tightest == null || v < tightest)) tightest = v;
  }
  netUsed.textContent = tightest != null ? fmtSignedCents(tightest) + "/CT" : "—";
  netUsed.className = "pl-used" + (tightest != null && !stale ? " pos" : "");
  netTag.textContent = stale ? "TIGHTEST IN HISTORY" : "TIGHTEST TAKEN";
  netNote.title = tightest == null
    ? ""
    : stale
      ? "TIGHTEST EDGE IN THE STORED HISTORY, ACROSS RUNS — NOT TAKEN AGAINST THE FLOOR ABOVE"
      : "TIGHTEST EDGE TAKEN, AGAINST A FLOOR OF " + fmtSignedCents(limits.min_net_ticks) + "/CT";

  let peak = null;
  let peakLabel = "";
  for (const pos of positions) {
    if (pos.qty != null && (peak == null || pos.qty > peak)) {
      peak = pos.qty;
      peakLabel = pos.label || (pos.pair_id != null ? "PAIR #" + pos.pair_id : "");
    }
  }
  setMeter(
    qtyMeter, peak, limits.max_qty_per_pair,
    peak != null ? fmtQty(peak) + " CTS" : "—",
    limits.max_qty_per_pair != null ? fmtQty(limits.max_qty_per_pair) : "—",
    stale ? { enforced: false, tag: "NO LIVE POSITIONS" } : undefined,
  );
  if (peakLabel) qtyMeter.box.title = peakLabel + " — " + qtyMeter.box.title;

  setMeter(
    notMeter, totals.cost_ticks, limits.max_notional_ticks,
    fmtDollarsFromTicks(totals.cost_ticks),
    fmtDollarsFromTicks(limits.max_notional_ticks),
    stale ? { enforced: false, tag: "COST · HISTORY, ALL RUNS" } : undefined,
  );
}

// ---------- render ----------

function renderPaper() {
  const p = state.paper;
  const d = p.data || {};
  const enabled = d.enabled === true;
  const totals = d.totals || {};
  const limits = d.limits || {};
  const positions = Array.isArray(d.positions) ? d.positions : [];
  const allTrades = Array.isArray(d.trades) ? d.trades : [];
  // the pair filter can only name a pair that still has trades on the tape
  if (filterPairId != null && !allTrades.some((t) => t.pair_id === filterPairId)) filterPairId = null;
  const trades = visibleTrades();

  // Keep the cursor on the same trade as the tape grows under it (newest first).
  if (p.selectedId != null) {
    const i = trades.findIndex((t) => t.id === p.selectedId);
    if (i >= 0) p.idx = i;
  }
  p.idx = Math.min(p.idx, Math.max(0, trades.length - 1));

  const stat = p.error
    ? p.error
    : !p.data
      ? "LOADING"
      : (enabled ? "ENABLED" : "DISABLED · HISTORY, ALL RUNS") + " · " + nf.format(allTrades.length) + " TRADES · NET " + fmtDollarsFromTicks(totals.net_ticks != null ? totals.net_ticks : 0);
  setText("paper-stat", stat);
  $("paper-stat").classList.toggle("warn", !!p.error);

  $("paper-banner").hidden = !p.data || enabled;

  const st = $("pt-state");
  st.textContent = !p.data ? "—" : enabled ? "ENABLED" : "DISABLED";
  st.className = "v " + (!p.data ? "" : enabled ? "pos" : "off");
  setText("pt-trades", totals.trades != null ? nf.format(totals.trades) : "—");
  setText("pt-qty", totals.qty != null ? fmtQty(totals.qty) : "—");
  setText("pt-cost", fmtDollarsFromTicks(totals.cost_ticks));
  setText("pt-fees", fmtDollarsFromTicks(totals.fee_ticks));
  const netEl = $("pt-net");
  netEl.textContent = fmtDollarsFromTicks(totals.net_ticks);
  netEl.className = "v num" + (totals.net_ticks > 0 ? " pos" : "");

  const pc = $("pos-rows");
  pc.textContent = "";
  if (!positions.length) {
    pc.append(el("div", "pos-row quiet-line", p.data ? "NO POSITIONS" : "—"));
  }
  for (const pos of positions) {
    const on = pos.pair_id != null && pos.pair_id === filterPairId;
    const row = el("div", "pos-row mid-row" + (pos.net_ticks > 0 ? " pos" : "") + (on ? " sel" : ""));
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", on ? "true" : "false");
    const label = el("span", "ps-label", pos.label || (pos.pair_id != null ? "PAIR #" + pos.pair_id : "—"));
    label.title = pos.label || "";
    const perCt = perContractTicks(pos.net_ticks, pos.qty);
    const net = el("span", "ps-net num", fmtDollarsFromTicks(pos.net_ticks));
    if (perCt != null) net.title = fmtSignedCents(perCt) + "/CT";
    row.append(
      label,
      el("span", "num", fmtQty(pos.qty)),
      net,
      el("span", "num", pos.trades != null ? nf.format(pos.trades) : "—"),
    );
    row.title = (pos.label || "") + (on ? " — CLICK TO SHOW EVERY PAIR" : " — CLICK TO FILTER THE TRADES BELOW");
    if (pos.pair_id != null) {
      row.addEventListener("click", () => setFilter(on ? null : pos.pair_id));
    }
    pc.append(row);
  }

  // filter chip: what the trades table is actually showing, and how to undo it
  const on = filterPairId != null;
  const fpos = on ? positions.find((x) => x.pair_id === filterPairId) : null;
  const flabel = on ? ((fpos && fpos.label) || (trades[0] && trades[0].label) || "PAIR #" + filterPairId) : null;
  filterLabel.textContent = on ? flabel : "ALL PAIRS";
  filterX.hidden = !on;
  filterChip.title = on ? "SHOWING ONE PAIR — CLICK TO SHOW EVERY PAIR" : "SELECT A POSITION TO FILTER THESE TRADES";
  filterChip.setAttribute("aria-pressed", on ? "true" : "false");
  filterChip.classList.toggle("on", on);
  filterChip.disabled = !on;
  filterCount.textContent = !allTrades.length
    ? ""
    : on
      ? nf.format(trades.length) + " OF " + nf.format(allTrades.length)
      : nf.format(allTrades.length) + " SHOWN";

  const tc = $("ptr-rows");
  tc.textContent = "";
  if (!trades.length) {
    tc.append(el("div", "ptr-row quiet-line", p.data ? (on ? "NO TRADES ON THIS PAIR" : enabled ? "NO TRADES YET — WAITING FOR AN EDGE ABOVE MIN NET" : "NO TRADES") : "—"));
  }
  trades.forEach((t, i) => {
    const row = el("div", "ptr-row mid-row" + (i === p.idx ? " sel" : "") + (t.net_ticks > 0 ? " pos" : ""));
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", i === p.idx ? "true" : "false");
    const label = el("span", "pt-label", t.label || (t.pair_id != null ? "PAIR #" + t.pair_id : "—"));
    label.title = t.label || "";
    row.append(
      el("span", "pt-time", fmtClockUtc(t.ts_ms)),
      label,
      el("span", "pt-dir", t.direction ? dirLabel(t.direction) : "—"),
      el("span", "num", fmtQty(t.qty)),
      el("span", "num", fmtDollarsFromTicks(t.cost_ticks)),
      el("span", "num", fmtDollarsFromTicks(t.fee_ticks)),
      el("span", "pt-net num", fmtDollarsFromTicks(t.net_ticks)),
    );
    row.addEventListener("click", () => { p.idx = i; p.selectedId = t.id != null ? t.id : null; renderPaper(); });
    tc.append(row);
  });

  const t = trades[p.idx];
  $("paper-empty").hidden = !!t;
  $("paper-detail").hidden = !t;
  if (t) {
    setText("pp-label", t.label || (t.pair_id != null ? "PAIR #" + t.pair_id : "—"));
    setText("pp-dir", t.direction ? dirLabel(t.direction) : "—");
    setText("pp-id", t.id != null ? "#" + t.id : "—");
    setText("pp-time", t.ts_ms != null && isFinite(t.ts_ms) ? fmtWhen(new Date(t.ts_ms).toISOString()) : "—");
    setText("pp-qty", t.qty != null ? fmtQty(t.qty) + " CTS" : "—");
    setText("pp-cost", fmtDollarsFromTicks(t.cost_ticks));
    setText("pp-fees", fmtDollarsFromTicks(t.fee_ticks));
    const ppNet = $("pp-net");
    ppNet.textContent = fmtDollarsFromTicks(t.net_ticks);
    ppNet.className = "v num" + (t.net_ticks > 0 ? " pos" : "");
    const perCt = perContractTicks(t.net_ticks, t.qty);
    setText("pp-netct", perCt != null ? fmtSignedCents(perCt) : "—");
    const legs = $("pp-legs");
    legs.textContent = "";
    const legList = Array.isArray(t.legs) ? t.legs : [];
    if (!legList.length) legs.append(el("div", "quiet-line", "NO LEG DETAIL"));
    for (const leg of legList) {
      const box = el("div", "ad-leg");
      const mk = (k, v) => { const kv = el("div", "kv"); kv.append(el("span", "k", k), el("span", "v num", v)); return kv; };
      const venue = String(leg.venue || "—").toUpperCase().replace("_US", " US");
      const side = String(leg.side || "—").replace("_", " ").toUpperCase();
      box.append(
        mk(venue, side),
        mk("WORST PRICE", leg.worst_price != null ? fmtCents(leg.worst_price) + "¢" : "—"),
        mk("QTY", leg.qty != null ? fmtQty(leg.qty) : "—"),
        mk("FEE", fmtDollarsFromTicks(leg.fee_ticks)),
      );
      legs.append(box);
    }
  }

  // with the trader off these rows are stored trades from every run, not this
  // one: the curve and the totals above it get told so, not left to imply it
  curveLbl.textContent = "CUMULATIVE EXPECTED NET"
    + (p.data && !enabled ? " · HISTORY, ALL RUNS" : "");

  renderLimits(totals, limits, positions, allTrades, enabled, !!p.data);
  drawCurve();

  const selRow = tc.children[p.idx];
  if (t && selRow && selRow.scrollIntoView) selRow.scrollIntoView({ block: "nearest" });
}

registerRenderer("papercurve", () => { if (mounted) drawCurve(); });

export default {
  id: "paper",
  path: "/paper",
  title: "PAPER",
  nav: true,
  root: "paperpage",

  mount() {
    mounted = true;
    state.paper.open = true;
    renderPaper();
    loadPaper();
    clearInterval(pollTimer);
    pollTimer = setInterval(loadPaper, PAPER_POLL_MS);
  },

  // Navigating away stops the poll AND the in-flight fetch: a 3s /api/paper
  // request that outlives the page is the exact bug the spec names.
  unmount() {
    mounted = false;
    state.paper.open = false;
    clearInterval(pollTimer);
    pollTimer = 0;
    const p = state.paper;
    if (p.ctl) { p.ctl.abort(); p.ctl = null; }
    curveHover = null;
    curveTip.hidden = true;
  },

  render() {
    renderPaper();
  },

  // Escape belongs to core/keys.js — claiming it here would break the uniform
  // "ESC CLOSE". Letter keys are taken only with an empty command buffer, so
  // typing PAIRS or ARB on this page still reaches the ARB> line.
  onKey(e) {
    const k = e.key;
    if (k === "ArrowUp" || k === "ArrowDown") { movePaper(k === "ArrowUp" ? -1 : 1); return true; }
    if (k === "Enter" && cmd.buffer() === "") {
      const t = visibleTrades()[state.paper.idx];
      if (t && t.pair_id != null) { setFilter(filterPairId === t.pair_id ? null : t.pair_id); return true; }
      return false;
    }
    const up = k.length === 1 ? k.toUpperCase() : k;
    if (up === "R" && cmd.buffer() === "") { loadPaper(); return true; }
    return false;
  },

  onMessage() {
    // the `paper` frame is handled at module load so it also folds in while
    // another page is mounted; nothing extra to do per frame here
  },
};
