/* pages/monitor.js — the default screen: market monitor, depth ladder, tape
   and the latency panel. One route ("/"), one page module.

   The monitor list is filterable and sortable. Rows are never rebuilt for a
   data tick: cells are updated in place (so flash-on-change survives) and the
   order is re-applied by moving the existing nodes, so a sort by a live price
   cannot cost the selection, the flash state or a drag in progress.

   Keyboard model (see .context/keyboard-model.md): the focused region owns
   every key. This page's only action keys are 1-9 quick-select, and they fire
   ONLY in LIST scope — with the row list focused. At the ARB> line a digit is
   typing, always. The old `cmd.buffer() === ""` guard is gone: the buffer is
   empty exactly when you start typing, so it could never protect the first
   character. Rows stay non-focusable; #mon-rows is the focusable region and
   the selection rides on aria-activedescendant. */

import { $, el, flash, setVal, setText, isReducedMotion } from "../core/dom.js";
import { state, schedule, registerRenderer, select, isSelecting } from "../core/state.js";
import { navigate } from "../core/router.js";
import { SCOPE, focusCommand } from "../core/keys.js";
import {
  nf, fmtCents, fmtMid, fmtQty, fmtSignedQty, fmtMs, fmtAge, fmtTapeTime, percentile,
} from "../core/format.js";

const STALE_MS = 5000;          // project staleness default
const MAX_TAPE_ROWS = 200;
const TAPE_DRAIN_MS = 250;
const TAPE_PER_DRAIN = 3;       // <= 12 rendered rows/s
const MAX_LADDER = 12;          // levels per side
const SPARK_WINDOW = 120;       // seconds shown
const SPARK_W = 340;
const SPARK_H = 48;
const P95_HOT_MS = 250;
const SKEW_WARN_MS = 25;        // |median - rtt/2| beyond this: clocks disagree
const CANVAS_FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const QUICK_KEYS = 9;           // rows 1-9 have a keyboard shortcut
const VIEW_KEY = "arb.monitor.v1";

/* MONITOR's key contract. index.html paints #mon-foot before this module
   runs, but this constant is the source: it is written into the element at
   module scope below, and help.js reads that element back to build the HELP
   table. Every item is live — UP/DOWN focuses the list from ARB> and then
   moves the selection, TAB opens the filter, 1-9 quick-selects inside the
   list, ENTER opens DES and ESC returns to the ARB> line. */
const MON_FOOT = "\u2191\u2193 LIST \u00b7 TAB FILTER \u00b7 1-9 QUICK (IN LIST) \u00b7 \u23ce DES \u00b7 ESC ARB>";

const NUMERIC_SORTS = new Set(["bid", "ask", "mid", "spr"]);

let mounted = false;
let tapeTimer = 0;
let tickTimer = 0;
let sparkHover = null;          // hover x in CSS px, or null

// persisted view: filter text, venue filter, sort column and direction
const view = { q: "", venue: "all", sort: "default", dir: 1 };

function loadView() {
  let raw = null;
  try { raw = localStorage.getItem(VIEW_KEY); } catch (err) { return; }
  if (!raw) return;
  let v;
  try { v = JSON.parse(raw); } catch (err) { return; }
  if (!v || typeof v !== "object") return;
  if (typeof v.q === "string") view.q = v.q;
  if (v.venue === "all" || v.venue === "kalshi" || v.venue === "polymarket_us") view.venue = v.venue;
  if (typeof v.sort === "string") view.sort = v.sort;
  if (v.dir === 1 || v.dir === -1) view.dir = v.dir;
}

function saveView() {
  try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (err) { /* private mode */ }
}

// ---------- monitor rows ----------

const monRefs = new Map();      // market_id -> {row, idx, ven, ticker, bid, ask, mid, spr, prev}
let builtFor = null;            // the state.markets array the rows were built from
const rank = new Map();         // market_id -> hello-order index
let lastOrder = null;           // ids currently attached, in DOM order
let lastSel = null;

function isPm(m) {
  return m.venue === "polymarket_us";
}

function buildRows() {
  builtFor = state.markets;
  monRefs.clear();
  rank.clear();
  lastOrder = null;
  state.markets.forEach((m, i) => {
    rank.set(m.market_id, i);
    const row = el("div", "mon-row" + (isPm(m) ? " v-pm" : ""));
    row.id = "mon-m" + i;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", "false");
    row.dataset.id = m.market_id;
    row.title = (m.title ? m.title + " · " : "") + m.ticker
      + (m.volume_24h != null ? " · 24h vol " + nf.format(Math.round(m.volume_24h)) : "");
    const idx = el("span", "mon-idx", "");
    const ven = el("span", "mon-ven", isPm(m) ? "PM" : "K");
    ven.title = isPm(m) ? "POLYMARKET US" : "KALSHI";
    const ticker = el("span", "mon-ticker", m.ticker || m.market_id);
    const bid = el("span", "num", "—");
    const ask = el("span", "num", "—");
    const mid = el("span", "num", "—");
    const spr = el("span", "num", "—");
    row.append(idx, ven, ticker, bid, ask, mid, spr);
    row.addEventListener("click", () => select(m.market_id));
    row.addEventListener("dblclick", () => {
      select(m.market_id);
      navigate("/market/" + encodeURIComponent(m.market_id));
    });
    monRefs.set(m.market_id, { row, idx, ven, ticker, bid, ask, mid, spr, prev: {} });
  });
}

function bookMetric(id, key) {
  const b = state.books.get(id);
  if (!b) return null;
  const bb = b.bids.length ? b.bids[0][0] : null;
  const ba = b.asks.length ? b.asks[0][0] : null;
  if (key === "bid") return bb;
  if (key === "ask") return ba;
  if (bb == null || ba == null) return null;
  return key === "mid" ? (bb + ba) / 2 : ba - bb;
}

function matches(m) {
  if (view.venue !== "all") {
    if (view.venue === "polymarket_us" ? !isPm(m) : isPm(m)) return false;
  }
  const q = view.q.trim().toLowerCase();
  if (!q) return true;
  return (m.ticker || "").toLowerCase().includes(q)
    || (m.title || "").toLowerCase().includes(q)
    || (m.market_id || "").toLowerCase().includes(q);
}

function compare(a, b) {
  const d = view.dir;
  const ra = rank.get(a.market_id) || 0;
  const rb = rank.get(b.market_id) || 0;
  if (view.sort === "default") return (ra - rb) * d;
  if (view.sort === "ticker") {
    const ta = (a.ticker || a.market_id || "").toUpperCase();
    const tb = (b.ticker || b.market_id || "").toUpperCase();
    return ta < tb ? -d : ta > tb ? d : ra - rb;
  }
  if (view.sort === "venue") {
    const va = isPm(a) ? 1 : 0;
    const vb = isPm(b) ? 1 : 0;
    return va !== vb ? (va - vb) * d : ra - rb;
  }
  const na = bookMetric(a.market_id, view.sort);
  const nb = bookMetric(b.market_id, view.sort);
  if (na == null && nb == null) return ra - rb;   // no book: always last
  if (na == null) return 1;
  if (nb == null) return -1;
  return na === nb ? ra - rb : (na - nb) * d;
}

function visibleMarkets() {
  return state.markets.filter(matches).sort(compare);
}

function setIdx(refs, i) {
  const txt = String(i + 1);
  if (refs.idx.textContent !== txt) refs.idx.textContent = txt;
  const quick = i < QUICK_KEYS;
  refs.idx.classList.toggle("qs", quick);
  if (quick) refs.idx.title = "QUICK SELECT " + txt + " \u2014 WITH THE LIST FOCUSED";
  else refs.idx.removeAttribute("title");
}

function sameIds(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function syncOrder(vis) {
  const ids = vis.map((m) => m.market_id);
  if (sameIds(ids, lastOrder)) return;
  lastOrder = ids;
  const c = $("mon-rows");
  if (!ids.length) {
    c.replaceChildren(el("div", "mon-row quiet-line", state.markets.length ? "NO MATCH" : "AWAITING MARKETS"));
    return;
  }
  // replaceChildren MOVES the existing nodes: identity, flash state and the
  // selection class all survive a re-sort.
  c.replaceChildren(...ids.map((id) => monRefs.get(id).row));
  ids.forEach((id, i) => setIdx(monRefs.get(id), i));
}

function syncSelection(force) {
  const id = state.selectedId;
  if (!force && id === lastSel) return;
  lastSel = id;
  for (const [mid, r] of monRefs) {
    const on = mid === id;
    r.row.classList.toggle("sel", on);
    r.row.setAttribute("aria-selected", on ? "true" : "false");
  }
  const rows = $("mon-rows");
  const sel = id ? monRefs.get(id) : null;
  if (sel && sel.row.isConnected) {
    rows.setAttribute("aria-activedescendant", sel.row.id);
    if (mounted) sel.row.scrollIntoView({ block: "nearest" });
  } else {
    rows.removeAttribute("aria-activedescendant");
  }
  for (const row of Array.from($("tape-rows").children)) {
    row.classList.toggle("sel", row.dataset.id === id);
  }
}

function renderHeader(vis) {
  let k = 0;
  let p = 0;
  for (const m of state.markets) {
    if (isPm(m)) p += 1;
    else k += 1;
  }
  // The list holds both venues; the header says so, with live counts.
  const parts = [];
  if (k) parts.push("KALSHI " + k);
  if (p) parts.push("POLYMARKET US " + p);
  const title = $("monitor-title");
  const titleText = "MONITOR — " + (parts.length ? parts.join(" · ") : "AWAITING MARKETS");
  if (title.textContent !== titleText) {
    title.textContent = titleText;
    title.title = titleText;
  }
  const total = state.markets.length;
  let stat = total && vis.length !== total
    ? vis.length + " OF " + total + " MKTS"
    : total + " MKTS";
  if (state.selectedId && !vis.some((m) => m.market_id === state.selectedId)) stat += " · SEL HIDDEN";
  const statEl = $("monitor-stat");
  if (statEl.textContent !== stat) statEl.textContent = stat;
}

function dirOf(oldV, newV) {
  if (oldV == null || newV == null || oldV === newV) return "neutral";
  return newV > oldV ? "up" : "down";
}

function updCell(refs, key, cell, val, text, kind) {
  if (refs.prev[key] === val && cell.textContent === text) return;
  const had = refs.prev[key] != null;
  refs.prev[key] = val;
  if (cell.textContent !== text) {
    cell.textContent = text;
    cell.title = val != null ? val + " ticks" : "";
    if (had) flash(cell, kind);
  }
}

function renderMonitorRows(ids) {
  for (const id of ids) {
    const r = monRefs.get(id);
    const b = state.books.get(id);
    if (!r || !b) continue;
    const bb = b.bids.length ? b.bids[0][0] : null;
    const ba = b.asks.length ? b.asks[0][0] : null;
    const mid = bb != null && ba != null ? (bb + ba) / 2 : null;
    const spr = bb != null && ba != null ? ba - bb : null;
    updCell(r, "bid", r.bid, bb, fmtCents(bb), dirOf(r.prev.bid, bb));
    updCell(r, "ask", r.ask, ba, fmtCents(ba), dirOf(r.prev.ask, ba));
    updCell(r, "mid", r.mid, mid, fmtMid(mid), "neutral");
    updCell(r, "spr", r.spr, spr, fmtCents(spr), "neutral");
  }
}

function renderMonitor() {
  const rebuilt = state.markets !== builtFor;
  if (rebuilt) buildRows();
  if (state.dirtyBooks.size) {
    const ids = Array.from(state.dirtyBooks);
    state.dirtyBooks.clear();
    renderMonitorRows(ids);      // cells update whether or not the row is shown
  }
  const vis = visibleMarkets();
  syncOrder(vis);
  syncSelection(rebuilt);
  renderHeader(vis);
}

// ---------- selection helpers over the VISIBLE list ----------

function moveSel(d) {
  const vis = visibleMarkets();
  if (!vis.length) return;
  let i = vis.findIndex((m) => m.market_id === state.selectedId);
  i = i < 0 ? 0 : Math.max(0, Math.min(vis.length - 1, i + d));
  select(vis[i].market_id);
}

function quickSelect(i) {
  const m = visibleMarkets()[i];
  if (m) select(m.market_id);
}

// ---------- depth ladder ----------

const ladderRefs = { asks: [], bids: [] };
const ladderEl = $("ladder");

function ladderRow(side) {
  const row = el("div", "ladder-row " + side);
  const bq = el("div", "l-qty bid-q");
  const aq = el("div", "l-qty ask-q");
  const yes = el("span", "l-yes");
  const no = el("span", "l-no");
  const q = side === "bid" ? bq : aq;
  const track = el("div", "track");
  const fill = el("div", "fill");
  const qtxt = el("span", "qtxt");
  track.style.visibility = "hidden";
  fill.style.visibility = "hidden";
  q.append(track, fill, qtxt);
  row.append(bq, yes, no, aq);
  return { row, qcell: q, track, fill, qtxt, yes, no, prevP: undefined, prevQ: undefined };
}

function buildLadder() {
  const ac = $("ask-rows");
  const bc = $("bid-rows");
  for (let i = 0; i < MAX_LADDER; i++) {
    const r = ladderRow("ask");
    if (i === MAX_LADDER - 1) r.row.classList.add("best"); // best ask sits just above mid
    ladderRefs.asks.push(r);
    ac.append(r.row);
  }
  for (let i = 0; i < MAX_LADDER; i++) {
    const r = ladderRow("bid");
    if (i === 0) r.row.classList.add("best"); // best bid just below mid
    ladderRefs.bids.push(r);
    bc.append(r.row);
  }
}

function clearLadderRow(r) {
  if (r.yes.textContent !== "") { r.yes.textContent = ""; r.yes.removeAttribute("title"); }
  if (r.no.textContent !== "") { r.no.textContent = ""; r.no.removeAttribute("title"); }
  if (r.qtxt.textContent !== "") r.qtxt.textContent = "";
  r.track.style.visibility = "hidden";
  r.fill.style.visibility = "hidden";
  r.prevP = undefined;
  r.prevQ = undefined;
}

function setLadderRow(r, lvl, side, maxQ) {
  if (!lvl || lvl[0] == null) {
    clearLadderRow(r);
    return;
  }
  const p = lvl[0];
  const q = lvl[1];
  r.track.style.visibility = "";
  r.fill.style.visibility = "";
  const yesTxt = fmtCents(p);
  if (r.yes.textContent !== yesTxt) {
    r.yes.textContent = yesTxt;
    r.yes.title = p + " ticks";
  }
  const noTxt = fmtCents(10000 - p);
  if (r.no.textContent !== noTxt) {
    r.no.textContent = noTxt;
    r.no.title = (10000 - p) + " ticks";
  }
  const changed = r.prevP === p && r.prevQ !== undefined && r.prevQ !== q;
  const qTxt = fmtQty(q);
  if (r.qtxt.textContent !== qTxt) r.qtxt.textContent = qTxt;
  r.fill.style.width = maxQ > 0 ? Math.min(100, (q / maxQ) * 100).toFixed(1) + "%" : "0%";
  if (changed) flash(r.qcell, side);
  r.prevP = p;
  r.prevQ = q;
}

function renderDepth() {
  const id = state.selectedId;
  const mkt = id ? state.byId.get(id) : null;
  $("depth-title").textContent = "DEPTH — " + (mkt ? mkt.ticker : "—");
  const banner = $("depth-banner");
  const stat = $("depth-stat");
  const book = id ? state.books.get(id) : null;

  if (!book) {
    stat.textContent = "—";
    stat.classList.remove("warn");
    banner.textContent = id ? "AWAITING BOOK" : "NO SELECTION";
    banner.classList.add("quiet");
    banner.classList.remove("pulse");
    ladderEl.classList.add("dim");
    for (const r of ladderRefs.asks) clearLadderRow(r);
    for (const r of ladderRefs.bids) clearLadderRow(r);
    $("mid-val").textContent = "—";
    $("spr-val").textContent = "—";
    return;
  }

  const age = book.age_ms + (performance.now() - book.recvAt);
  const quiet = age > STALE_MS;
  // "stale" from the server just means no update inside the trading-engine
  // staleness window — a quiet prediction market, not a broken book. Only
  // structural reasons (seq gap, crossed, bad level) are alarming.
  const structural = book.valid === false && book.reason && book.reason !== "stale";
  stat.textContent = "AGE " + fmtAge(age);
  stat.classList.toggle("warn", Boolean(structural));

  if (structural) {
    banner.classList.remove("quiet");
    banner.textContent = "INVALID · " + String(book.reason).toUpperCase();
  } else if (quiet || book.reason === "stale") {
    banner.classList.add("quiet");
    banner.textContent = "QUIET · LAST UPDATE " + (age / 1000).toFixed(0) + "s AGO";
  } else {
    banner.classList.remove("quiet");
    banner.textContent = "";
  }
  banner.classList.toggle("pulse", Boolean(structural) && !isReducedMotion());
  ladderEl.classList.toggle("dim", Boolean(structural));

  const asks = book.asks.slice(0, MAX_LADDER);
  const bids = book.bids.slice(0, MAX_LADDER);
  let maxQ = 0;
  for (const l of asks) if (l && l[1] > maxQ) maxQ = l[1];
  for (const l of bids) if (l && l[1] > maxQ) maxQ = l[1];
  for (let k = 0; k < MAX_LADDER; k++) {
    // ask level k renders k rows above the mid seam (container bottom row = best ask)
    setLadderRow(ladderRefs.asks[MAX_LADDER - 1 - k], asks[k] || null, "ask", maxQ);
    setLadderRow(ladderRefs.bids[k], bids[k] || null, "bid", maxQ);
  }

  const bb = bids.length ? bids[0][0] : null;
  const ba = asks.length ? asks[0][0] : null;
  const midEl = $("mid-val");
  const sprEl = $("spr-val");
  const midTxt = bb != null && ba != null ? fmtMid((bb + ba) / 2) : "—";
  const sprTxt = bb != null && ba != null ? fmtCents(ba - bb) : "—";
  if (midEl.textContent !== midTxt) midEl.textContent = midTxt;
  if (sprEl.textContent !== sprTxt) sprEl.textContent = sprTxt;
}

// ---------- tape ----------

function buildTapeRow(d) {
  const side = d.side === "bid" ? "b" : "a";
  const row = el("div", "tape-row side-" + side + (isReducedMotion() ? "" : " new-" + side));
  row.dataset.id = d.market_id || "";
  if (d.market_id === state.selectedId) row.classList.add("sel");
  const mkt = d.market_id ? state.byId.get(d.market_id) : null;
  const px = el("span", "t-px", fmtCents(d.price));
  if (d.price != null) px.title = d.price + " ticks";
  row.append(
    el("span", "t-time", d.ts_ms != null ? fmtTapeTime(d.ts_ms) : "—"),
    el("span", "t-side", side === "b" ? "B" : "A"),
    px,
    el("span", "t-dq", fmtSignedQty(d.qty_delta)),
    el("span", "t-lat", fmtMs(d.latency_ms)),
    el("span", "t-tk", mkt ? mkt.ticker : (d.market_id || "")),
  );
  return row;
}

function drainTape() {
  if (isSelecting()) return;  // prepending rows would shift a drag in progress
  if (!state.tape.queue.length) return;
  const take = state.tape.queue.splice(0, TAPE_PER_DRAIN);
  const c = $("tape-rows");
  for (const d of take) c.prepend(buildTapeRow(d)); // oldest of the batch first; newest ends on top
  while (c.childElementCount > MAX_TAPE_ROWS) c.lastElementChild.remove();
  $("tape-stat").textContent = nf.format(state.tape.total) + " MSGS";
}

// ---------- latency sparkline ----------

const spark = $("spark");
let sctx = null;

function setupCanvas() {
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  spark.width = Math.round(SPARK_W * dpr);
  spark.height = Math.round(SPARK_H * dpr);
  spark.style.width = SPARK_W + "px";
  spark.style.height = SPARK_H + "px";
  sctx = spark.getContext("2d");
  if (sctx) sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawSpark() {
  if (!sctx) return;
  const W = SPARK_W;
  const H = SPARK_H;
  const plotW = W - 48; // right gutter for direct labels
  const tip = $("spark-tip");
  sctx.clearRect(0, 0, W, H);

  const pts = state.latPoints.slice(-SPARK_WINDOW);
  const n = pts.length;
  const vals = [];
  for (const p of pts) if (p.v != null) vals.push(p.v);

  if (!vals.length) {
    sctx.fillStyle = "#6b7280";
    sctx.font = "10px " + CANVAS_FONT;
    sctx.textAlign = "center";
    sctx.fillText("AWAITING DATA", W / 2, H / 2 + 3);
    sctx.textAlign = "left";
    tip.hidden = true;
    return;
  }

  const lm = (state.stats && state.stats.latency_ms) || {};
  const nowMs = Date.now();
  if (!state.latLastFit || nowMs - state.latLastFit > 30000) { // y-domain refit every 30s
    const p95 = lm.p95 != null ? lm.p95 : percentile(vals, 0.95);
    const target = p95 != null && p95 > 0 ? p95 : Math.max.apply(null, vals);
    state.latDomainMax = Math.max(10, 1.5 * target); // 0 -> 1.5 x p95
    state.latLastFit = nowMs;
  }
  const dmax = state.latDomainMax;
  const step = plotW / (SPARK_WINDOW - 1);
  const xAt = (i) => plotW - (n - 1 - i) * step;
  // clamps both edges; the raw one-way sample goes negative when the local clock lags the venue's
  const yAt = (v) => Math.min(H - 2, Math.max(2, H - 2 - (v / dmax) * (H - 4)));

  // segments split on null buckets
  const segs = [];
  let cur = null;
  const clampedHi = [];
  const clampedLo = [];
  for (let i = 0; i < n; i++) {
    const v = pts[i].v;
    if (v == null) { cur = null; continue; }
    if (!cur) { cur = []; segs.push(cur); }
    const x = xAt(i);
    cur.push([x, yAt(v)]);
    if (v > dmax) clampedHi.push(x);
    else if (v < 0) clampedLo.push(x);
  }

  // area fill: same-hue 6% fading to 0
  const grad = sctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(86,156,214,0.06)");
  grad.addColorStop(1, "rgba(86,156,214,0)");
  sctx.fillStyle = grad;
  for (const seg of segs) {
    if (seg.length < 2) continue;
    sctx.beginPath();
    sctx.moveTo(seg[0][0], seg[0][1]);
    for (let j = 1; j < seg.length; j++) sctx.lineTo(seg[j][0], seg[j][1]);
    sctx.lineTo(seg[seg.length - 1][0], H);
    sctx.lineTo(seg[0][0], H);
    sctx.closePath();
    sctx.fill();
  }

  // median / p95: 1px dotted rules, right-labeled; p95 goes amber when hot
  const hot = lm.p95 != null && lm.p95 > P95_HOT_MS;
  const labels = [];
  const drawRule = (v, color) => {
    const yy = Math.round(yAt(v)) + 0.5;
    sctx.save();
    sctx.strokeStyle = color;
    sctx.lineWidth = 1;
    sctx.setLineDash([1, 2]);
    sctx.beginPath();
    sctx.moveTo(0, yy);
    sctx.lineTo(plotW, yy);
    sctx.stroke();
    sctx.restore();
    return yy;
  };
  if (lm.median != null) {
    const y = drawRule(lm.median, "#6b7280");
    labels.push({ text: "MED " + Math.round(lm.median), y, color: "#8a9099" });
  }
  if (lm.p95 != null) {
    const y = drawRule(lm.p95, hot ? "#ffb02e" : "#6b7280");
    labels.push({ text: "P95 " + Math.round(lm.p95), y, color: hot ? "#ffb02e" : "#8a9099" });
  }
  // The current value joins the gutter label set so the three direct
  // labels (last / MED / P95) can never overprint each other.
  let last = null;
  for (let i = n - 1; i >= 0; i--) {
    if (pts[i].v != null) {
      last = { y: yAt(pts[i].v), v: pts[i].v };
      break;
    }
  }
  if (last) {
    labels.push({ text: Math.round(last.v) + "ms", y: last.y, color: "#e6e6e6", size: 11 });
  }
  for (const lb of labels) lb.y = Math.max(8, Math.min(H - 2, lb.y + 3));
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < 11) labels[i].y = labels[i - 1].y + 11;
  }
  for (let i = labels.length - 1; i >= 0; i--) {
    const maxY = H - 2 - (labels.length - 1 - i) * 11;
    if (labels[i].y > maxY) labels[i].y = maxY;
  }
  for (const lb of labels) {
    sctx.fillStyle = lb.color;
    sctx.font = (lb.size || 10) + "px " + CANVAS_FONT;
    sctx.fillText(lb.text, plotW + 4, lb.y);
  }

  // line: 1.5px blue, no tweening
  sctx.strokeStyle = "#569cd6";
  sctx.lineWidth = 1.5;
  for (const seg of segs) {
    if (seg.length === 1) {
      sctx.fillStyle = "#569cd6";
      sctx.fillRect(seg[0][0] - 1, seg[0][1] - 1, 2, 2);
      continue;
    }
    sctx.beginPath();
    sctx.moveTo(seg[0][0], seg[0][1]);
    for (let j = 1; j < seg.length; j++) sctx.lineTo(seg[j][0], seg[j][1]);
    sctx.stroke();
  }

  // out-of-domain samples clamp to an edge and say so with a 3px amber tick
  sctx.fillStyle = "#ffb02e";
  for (const x of clampedHi) sctx.fillRect(x - 1, 0, 2, 3);
  for (const x of clampedLo) sctx.fillRect(x - 1, H - 3, 2, 3);

  // hover: 1px crosshair + tooltip
  if (sparkHover != null && sparkHover <= plotW + 4) {
    let i = Math.round(n - 1 - (plotW - sparkHover) / step);
    i = Math.max(0, Math.min(n - 1, i));
    const hx = Math.round(xAt(i)) + 0.5;
    if (hx >= 0) {
      sctx.strokeStyle = "#8a9099";
      sctx.lineWidth = 1;
      sctx.beginPath();
      sctx.moveTo(hx, 0);
      sctx.lineTo(hx, H);
      sctx.stroke();
      const v = pts[i].v;
      tip.textContent = "t−" + (n - 1 - i) + "s · " + (v != null ? Math.round(v) + "ms" : "—");
      tip.hidden = false;
      tip.style.left = (8 + Math.max(0, Math.min(W - 96, hx + 10))) + "px";
      tip.style.top = "4px";
    } else {
      tip.hidden = true;
    }
  } else {
    tip.hidden = true;
  }
}

function renderLatNums() {
  const s = state.stats;
  if (!s) return;
  const lm = s.latency_ms || {};
  setVal($("lat-last"), fmtMs(lm.last));
  setVal($("lat-med"), fmtMs(lm.median));
  setVal($("lat-p95"), fmtMs(lm.p95), lm.p95 != null && lm.p95 > P95_HOT_MS);
  setVal($("lat-n"), lm.n != null ? nf.format(lm.n) : "—");
  // One-way latency needs synced clocks; the keepalive RTT does not. A
  // negative median or a large estimated skew means the one-way number is
  // contaminated, and RTT/2 is the figure to trust.
  const skew = s.clock_skew_ms;
  const skewed = (lm.median != null && lm.median < 0) || (skew != null && Math.abs(skew) > SKEW_WARN_MS);
  setVal($("lat-rtt"), fmtMs(s.rtt_ms));
  setVal($("lat-skew"), skew == null ? "—" : (skew > 0 ? "+" : "") + Math.round(skew) + "ms", skewed);
  $("lat-stat").textContent = skewed
    ? "CLOCK SKEW · TRUST RTT/2 " + (s.rtt_ms != null ? fmtMs(s.rtt_ms / 2) : "—")
    : fmtMs(lm.last);
  $("lat-stat").classList.toggle("warn", skewed);
}

// ---------- filter / sort controls ----------

function applyView(refilter) {
  if (refilter) lastOrder = null;
  saveView();
  schedule("monitor");
}

function syncControls() {
  $("mon-q").value = view.q;
  for (const b of document.querySelectorAll("#mon-venue .vbtn")) {
    b.setAttribute("aria-pressed", b.dataset.venue === view.venue ? "true" : "false");
  }
  for (const b of document.querySelectorAll(".mon-cols .mon-h")) {
    const on = b.dataset.sort === view.sort;
    const label = b.dataset.label || b.textContent;
    b.dataset.label = label;
    b.textContent = on ? label + (view.dir > 0 ? "▲" : "▼") : label;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on
      ? "SORTED BY " + label + (view.dir > 0 ? " ASCENDING" : " DESCENDING") + " — CLICK TO REVERSE"
      : "SORT BY " + label;
  }
}

function bindControls() {
  const q = $("mon-q");
  q.addEventListener("input", () => {
    view.q = q.value;
    applyView(true);
  });
  // No keydown handler: a text field owns every key it is sent, and the core
  // resolves the three that leave it. ENTER / ARROWDOWN move focus to the row
  // list (keys.js step 3) and ESCAPE runs the global ladder — clear the value
  // (which fires "input" above, so the view follows), then back to ARB>.
  for (const b of document.querySelectorAll("#mon-venue .vbtn")) {
    b.addEventListener("click", (e) => {
      view.venue = b.dataset.venue;
      syncControls();
      applyView(true);
      if (e.detail > 0) focusCommand();   // a pointer click must not park the keyboard on a button
    });
  }
  for (const b of document.querySelectorAll(".mon-cols .mon-h")) {
    b.addEventListener("click", (e) => {
      const key = b.dataset.sort;
      if (view.sort === key) view.dir = view.dir > 0 ? -1 : 1;
      else {
        view.sort = key;
        view.dir = NUMERIC_SORTS.has(key) ? -1 : 1;  // prices read best high-first
      }
      syncControls();
      applyView(true);
      if (e.detail > 0) focusCommand();
    });
  }
}

// ---------- init (module scope: the DOM is parsed, type=module defers) ----------

buildLadder();
setupCanvas();
loadView();
bindControls();
syncControls();
// At module scope, not in mount(): help.js reads this footer straight out of
// the DOM and builds its key table once, so the text has to be right even on
// a deep link to /help where MONITOR never mounted.
setText("mon-foot", MON_FOOT);

spark.addEventListener("mousemove", (e) => {
  const r = spark.getBoundingClientRect();
  sparkHover = e.clientX - r.left;
  schedule("spark");
});
spark.addEventListener("mouseleave", () => {
  sparkHover = null;
  $("spark-tip").hidden = true;
  schedule("spark");
});
window.addEventListener("resize", () => {
  setupCanvas();
  schedule("spark");
});

registerRenderer("depth", () => { if (mounted) renderDepth(); });
registerRenderer("latnums", () => { if (mounted) renderLatNums(); });
registerRenderer("spark", () => { if (mounted) drawSpark(); });

function tick() {
  if (state.selectedId) schedule("depth"); // client-side book aging -> QUIET banner
}

export default {
  id: "monitor",
  path: "/",
  title: "MONITOR",
  nav: true,
  root: "monitor-page",
  regions: ["mon-q", "mon-rows"],         // TAB order from ARB>; [0] is also the "/" target
  listRegion: "mon-rows",                 // UP/DOWN from ARB> focuses this

  mount() {
    mounted = true;
    lastOrder = null;                     // re-attach rows after any absence
    if (!tapeTimer) tapeTimer = setInterval(drainTape, TAPE_DRAIN_MS);
    if (!tickTimer) tickTimer = setInterval(tick, 1000);
    schedule("monitor", "depth", "latnums", "spark");
  },

  unmount() {
    mounted = false;
    clearInterval(tapeTimer);
    tapeTimer = 0;
    clearInterval(tickTimer);
    tickTimer = 0;
  },

  render() {
    renderMonitor();
  },

  onKey(e, scope) {
    const k = e.key;
    // Moving the selection is a G0 view action: free in whatever scope the
    // core hands us. In practice that is LIST — from ARB> an arrow focuses
    // the list first (keys.js step 8) without moving the cursor, so row 0 is
    // the next candidate. This walks the VISIBLE order, which is why the page
    // claims arrows at all rather than leaving them to selectRelative().
    if (k === "ArrowUp" || k === "ArrowDown") {
      moveSel(k === "ArrowUp" ? -1 : 1);
      return true;
    }
    // 1-9 quick-select: LIST scope only. At the ARB> line a digit is typing
    // (the core never even asks us), and a held key must not walk the list.
    if (scope !== SCOPE.LIST) return false;
    if (e.repeat) return false;
    if (/^[1-9]$/.test(k)) {
      quickSelect(Number(k) - 1);
      return true;
    }
    return false;
  },

  /** What the keys strip and the reversed-video ARB> band say, per scope.
      Same source as the footer's list-scope items, so they cannot disagree. */
  keyHints(scope) {
    if (scope === SCOPE.LIST) {
      return [
        { k: "\u2191\u2193", d: "SELECT" },
        { k: "1-9", d: "QUICK" },
        { k: "\u23ce", d: "DES" },
        { k: "/", d: "FILTER" },
      ];
    }
    // In the filter box the core already says "ENTER DONE"; the useful extra
    // is that DOWN drops straight into the rows.
    if (scope === SCOPE.TEXT) return [{ k: "\u2193", d: "LIST" }];
    return [];
  },
};
