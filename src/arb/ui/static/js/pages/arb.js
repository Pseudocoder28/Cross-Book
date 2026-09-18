/* pages/arb.js — ARB: the cross-venue edge monitor, one row per confirmed
   pair, taker fees included. Ported from the pre-multipage static/app.js, see git history (openArb closeArb
   onArb moveArb renderArb onArbKey dirLabel bboText).

   MEASUREMENT ONLY. This page reads quotes and places nothing.

   Two things the overlay version could not do:
     - the `arb` frame is handled at MODULE LOAD, not in mount(), so the
       snapshot is already current when you navigate here;
     - the table is filtered (minimum net per contract) and sorted client
       side, and the rows that clear the threshold are marked, so "is there
       anything to act on" is one glance, not a scan. The threshold is the
       paper trader's own only when a paper trader is actually running;
       otherwise it is labelled as the (unenforced) default it is.

   Keyboard: the focused region owns every key (.context/keyboard-model.md).
   This page declares `arb-rows` as its list and `arb-min` as its text region,
   and answers only to arrows and ENTER — no letter is a hotkey here, in any
   scope, so typing PAIRS or a ticker always reaches the ARB> line. */

import { $, el, titled, setText } from "../core/dom.js";
import { state, schedule, select } from "../core/state.js";
import { navigate } from "../core/router.js";
import { onMessage } from "../core/ws.js";
import { SCOPE, focusCommand } from "../core/keys.js";
import * as cmd from "../core/cmd.js";
import { nf, fmtCents, fmtQty, fmtSignedCents, fmtDollarsFromTicks } from "../core/format.js";
import * as M from "./arb-model.js";

const VIEW_KEY = "arb.arb.v1";
// src/arb/paper.py PaperLimits.min_net_ticks. Only a fallback: the live value
// comes from /api/paper, because the run may have been started with
// --min-net-ticks and a stale threshold would mark the wrong rows.
const DEFAULT_PAPER_MIN = 50;
// /api/paper serves PaperLimits() defaults even with no trader (enabled:false),
// so the number alone never means the threshold is being enforced. Assume it is
// not until the fetch says otherwise: claiming enforcement that does not exist
// is the one error this screen must not make.
const SORTS = ["net", "size", "pair"];
const DEFAULT_DIR = { net: -1, size: -1, pair: 1 };  // edges read best-first

let mounted = false;
let built = false;
let paperMin = DEFAULT_PAPER_MIN;
let paperOn = false;
let paperMinLoaded = false;
let paperCtl = null;
// false = the cursor follows the top of the current ranking (app.js behaviour);
// true = the operator picked a row and it stays picked.
let pinned = false;
// The age cells of the rows on screen, so the 1s tick rewrites two words per
// row instead of rebuilding the table under the cursor.
let ageCells = [];
let ageTimer = null;

// persisted view: minimum net/contract in ticks (null = no filter), sort, dir
const view = { min: null, sort: "net", dir: -1 };

function loadView() {
  let raw = null;
  try { raw = localStorage.getItem(VIEW_KEY); } catch (err) { return; }
  if (!raw) return;
  let v;
  try { v = JSON.parse(raw); } catch (err) { return; }
  if (!v || typeof v !== "object") return;
  if (v.min === null || (typeof v.min === "number" && isFinite(v.min))) view.min = v.min;
  if (SORTS.indexOf(v.sort) >= 0) view.sort = v.sort;
  if (v.dir === 1 || v.dir === -1) view.dir = v.dir;
}

function saveView() {
  try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (err) { /* private mode */ }
}

// ---------- ported formatting helpers (byte-identical to app.js) ----------

function dirLabel(direction) {
  return direction === "yes_a_no_b" ? "BUY YES K · BUY NO P" : "BUY YES P · BUY NO K";
}

function bboText(leg) {
  if (!leg || !leg.has_book) return "—";
  const b = leg.best_bid ? fmtCents(leg.best_bid[0]) : "—";
  const a = leg.best_ask ? fmtCents(leg.best_ask[0]) : "—";
  return b + "/" + a;
}

// ---------- filter / sort ----------

function labelOf(q) {
  return q.label || (q.kalshi.ticker + " / " + q.polymarket_us.ticker);
}

/** An edge that clears `paperMin` at the current books — what the paper trader
    would actually take when one is running, a display marker when one is not. */
function clearsMin(q) {
  return q.best.qty > 0 && q.best.net_per_contract_ticks >= paperMin;
}

function passes(q) {
  if (view.min == null) return true;
  // No size is no edge, whatever the per-contract number rounds to.
  return q.best.qty > 0 && q.best.net_per_contract_ticks >= view.min;
}

function metric(q) {
  return view.sort === "size" ? q.best.qty : q.best.net_per_contract_ticks;
}

function compare(x, y, order) {
  const d = view.dir;
  const rx = order.get(x.pair_id) || 0;
  const ry = order.get(y.pair_id) || 0;
  if (view.sort === "pair") {
    const lx = labelOf(x).toUpperCase();
    const ly = labelOf(y).toUpperCase();
    return lx < ly ? -d : lx > ly ? d : rx - ry;
  }
  const nx = metric(x);
  const ny = metric(y);
  return nx === ny ? rx - ry : (nx - ny) * d;   // server order breaks every tie
}

/** The rows as displayed. Never mutates state.arb.quotes: the head stat and
    the summary still read the server's own best-net-first ranking. */
function visibleQuotes() {
  const qs = state.arb.quotes;
  const order = new Map();
  qs.forEach((q, i) => order.set(q.pair_id, i));
  return qs.filter(passes).sort((x, y) => compare(x, y, order));
}

// ---------- controls (the shell owns index.html; these are built here) ----------

function btn(cls, label, title) {
  const b = el("button", cls, label);
  b.type = "button";
  b.setAttribute("aria-pressed", "false");
  if (title) b.title = title;
  return b;
}

function buildControls() {
  if (built) return;
  const rows = $("arb-rows");
  if (!rows) return;
  built = true;
  const left = rows.parentNode;
  const cols = left.querySelector(".arb-cols");

  const ctl = el("div", "arb-ctl");
  ctl.append(el("span", "arb-ctl-lbl", "MIN NET/CT"));

  const min = el("input", "arb-min");
  min.id = "arb-min";
  min.type = "text";
  min.inputMode = "numeric";
  min.autocomplete = "off";
  min.spellcheck = false;
  min.placeholder = "TICKS";
  min.title = "HIDE EVERY PAIR BELOW THIS NET PER CONTRACT, IN TICKS ($0.0001). BLANK SHOWS ALL.";
  min.setAttribute("aria-label", "Minimum net edge per contract, in ticks");
  ctl.append(min);

  const presets = el("div", "arb-btns");
  presets.id = "arb-presets";
  presets.setAttribute("role", "group");
  presets.setAttribute("aria-label", "Minimum net edge presets");
  const all = btn("abtn", "ALL", "SHOW EVERY TRACKED PAIR");
  all.dataset.preset = "all";
  const edge = btn("abtn", "EDGE", "SHOW ONLY PAIRS WITH A POSITIVE NET EDGE");
  edge.dataset.preset = "edge";
  const paper = btn("abtn", "PAPER", "");
  paper.id = "arb-preset-paper";
  paper.dataset.preset = "paper";
  presets.append(all, edge, paper);
  ctl.append(presets);

  ctl.append(el("span", "arb-ctl-gap"));
  ctl.append(el("span", "arb-ctl-lbl", "SORT"));

  const sorts = el("div", "arb-btns");
  sorts.id = "arb-sorts";
  sorts.setAttribute("role", "group");
  sorts.setAttribute("aria-label", "Sort pair edges");
  for (const s of SORTS) {
    const label = s === "net" ? "NET/CT" : s === "size" ? "SIZE" : "PAIR";
    const b = btn("abtn", label, "SORT BY " + label);
    b.dataset.sort = s;
    b.dataset.label = label;
    sorts.append(b);
  }
  ctl.append(sorts);

  const sum = el("div", "arb-sum", "NO PAIRS TRACKED");
  sum.id = "arb-sum";

  left.insertBefore(ctl, cols);
  left.insertBefore(sum, cols);
  bindControls();
  syncControls();
}

// The keys this page answers to, in one place. help.js reads the footer out of
// the page root to build its table, so this string IS the page's documentation:
// ↑↓ from the ARB> line focuses the list (core/keys.js), ↑↓ inside it moves the
// cursor, and ENTER only opens the Kalshi leg while nothing is typed.
const FOOT = "↑↓ LIST · ⏎ DES (KALSHI LEG) · TAB MIN NET"
  + " · ESC ARB> · MEASUREMENT ONLY — NO ORDERS";

/** One source for the footer: adopt the shell's element if index.html still
    ships one, otherwise create it. Two copies of a key list is how the strip
    and the page drift apart. */
function buildFoot() {
  const page = $("arbpage");
  if (!page) return;
  const foot = page.querySelector(".des-foot") || el("div", "des-foot");
  foot.textContent = FOOT;
  if (!foot.parentNode) {
    const right = page.querySelector(".arb-right");
    if (right) right.append(foot);
  }
}

function applyView() {
  saveView();
  schedule("arb");
}

function setMin(v) {
  view.min = v;
  syncControls();
  applyView();
}

function syncControls(skipInput) {
  const min = $("arb-min");
  if (!min) return;
  if (!skipInput) min.value = view.min == null ? "" : String(view.min);
  const paper = $("arb-preset-paper");
  // With no trader running the same number is only this view's own cut-off, and
  // the button has to say so — nothing is enforcing it.
  paper.textContent = (paperOn ? "PAPER " : "DEFAULT ") + paperMin;
  paper.title = paperOn
    ? "PAPER TRADER MINIMUM — " + paperMin + " TICKS/CT (" + fmtSignedCents(paperMin) + ")"
    : "PAPER TRADER DEFAULT MINIMUM — " + paperMin + " TICKS/CT ("
      + fmtSignedCents(paperMin) + ") — NOT IN FORCE: NO PAPER TRADER ON THIS RUN."
      + " FILTERS THIS VIEW ONLY. RESUME THE TRADER ON /control";
  for (const b of document.querySelectorAll("#arb-presets .abtn")) {
    const p = b.dataset.preset;
    const on = p === "all" ? view.min == null
      : p === "edge" ? view.min === 1
        : view.min === paperMin;
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  for (const b of document.querySelectorAll("#arb-sorts .abtn")) {
    const on = b.dataset.sort === view.sort;
    const label = b.dataset.label;
    b.textContent = on ? label + (view.dir > 0 ? "▲" : "▼") : label;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on
      ? "SORTED BY " + label + (view.dir > 0 ? " ASCENDING" : " DESCENDING") + " — CLICK TO REVERSE"
      : "SORT BY " + label;
  }
}

function bindControls() {
  const min = $("arb-min");
  min.addEventListener("input", () => {
    const t = min.value.trim();
    const v = t === "" ? null : parseFloat(t.replace(/[^0-9.+-]/g, ""));
    view.min = v == null || !isFinite(v) ? null : Math.round(v);
    syncControls(true);
    applyView();
  });
  // No keydown handler: core/keys.js owns TEXT scope. Escape clears the box
  // (the synthetic `input` event above re-applies the view) and a second one
  // hands the keyboard back to ARB>; Enter and ↓ step into the list. A local
  // copy of that ladder is how a filter box drifts from every other one.
  for (const b of document.querySelectorAll("#arb-presets .abtn")) {
    b.addEventListener("click", (e) => {
      const p = b.dataset.preset;
      setMin(p === "all" ? null : p === "edge" ? 1 : paperMin);
      if (e.detail > 0) focusCommand();   // pointer click: focus ends at ARB>
    });
  }
  for (const b of document.querySelectorAll("#arb-sorts .abtn")) {
    b.addEventListener("click", (e) => {
      const key = b.dataset.sort;
      if (view.sort === key) view.dir = view.dir > 0 ? -1 : 1;
      else {
        view.sort = key;
        view.dir = DEFAULT_DIR[key];
      }
      syncControls();
      applyView();
      if (e.detail > 0) focusCommand();   // pointer click: focus ends at ARB>
    });
  }
}

// ---------- the paper trader's own threshold ----------

/** One-shot: whether a paper trader is running and what net/contract it would
    require. Failure keeps the documented default, presented as a default. */
async function loadPaperMin() {
  if (paperMinLoaded) return;
  paperMinLoaded = true;
  try {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    paperCtl = ctl;
    const r = await fetch("/api/paper", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
    if (!r.ok) return;
    const d = await r.json();
    const m = d && d.limits ? d.limits.min_net_ticks : null;
    const on = !!(d && d.enabled);
    const min = typeof m === "number" && isFinite(m) ? m : paperMin;
    if (min !== paperMin || on !== paperOn) {
      paperMin = min;
      paperOn = on;
      syncControls();
      schedule("arb");
    }
  } catch (err) {
    paperMinLoaded = false;       // aborted or offline: retry on the next mount
  } finally {
    paperCtl = null;
  }
}

// ---------- render ----------

/** The table is empty for one of two very different reasons; say which. The
    detail pane's copy also says how to get the rows back. */
function emptyText(withHint) {
  const n = state.arb.quotes.length;
  // Confirming is not watching: T on /pairs is what starts a pair quoting.
  if (!n) return "NOTHING IS BEING WATCHED — CONFIRM A PAIR ON /pairs, THEN PRESS T TO WATCH IT";
  return "NO PAIR CLEARS " + view.min + " TICKS/CT — " + nf.format(n) + " TRACKED"
    + (withHint ? " · LOWER MIN NET/CT, OR PRESS ALL" : "");
}

function renderSummary(vis) {
  const qs = state.arb.quotes;
  const sum = $("arb-sum");
  if (!sum) return;
  if (!qs.length) {
    sum.textContent = "NO PAIRS TRACKED";
    sum.classList.remove("has-act");
    return;
  }
  let pos = 0;
  let act = 0;
  for (const q of qs) {
    if (q.best.qty > 0 && q.best.net_per_contract_ticks > 0) pos += 1;
    if (clearsMin(q)) act += 1;
  }
  const best = qs[0].best;        // server ranks best net/contract first
  const parts = [
    "TRACKED " + nf.format(qs.length),
    "POSITIVE " + nf.format(pos),
    paperOn
      ? "ACTIONABLE " + nf.format(act) + " (≥ " + paperMin + " TICKS/CT)"
      : "MARKED " + nf.format(act) + " (≥ " + paperMin + " TICKS/CT, NO PAPER TRADER RUNNING)",
    "BEST " + (best.qty > 0 ? fmtSignedCents(best.net_per_contract_ticks) + "/CT" : "NO EDGE"),
  ];
  if (view.min != null) parts.push("SHOWING " + nf.format(vis.length) + " OF " + nf.format(qs.length));
  sum.textContent = parts.join(" · ");
  sum.classList.toggle("has-act", act > 0);
}

function renderArb() {
  const a = state.arb;
  const vis = visibleQuotes();
  if (!pinned) {
    // Untouched cursor = the top of the current ranking, re-read every frame:
    // the detail pane shows today's best edge, not page-load's.
    a.idx = 0;
    a.selectedPair = vis.length ? vis[0].pair_id : null;
  } else {
    // Picked by hand: keep the cursor on that pair as the ranking reshuffles
    // under it, and only re-pin when a filter or sort hid it.
    if (a.selectedPair != null) {
      const i = vis.findIndex((q) => q.pair_id === a.selectedPair);
      if (i >= 0) a.idx = i;
    }
    a.idx = Math.min(a.idx, Math.max(0, vis.length - 1));
    if (vis.length) a.selectedPair = vis[a.idx].pair_id;
  }
  const best = a.quotes.length ? a.quotes[0].best.net_per_contract_ticks : null;
  setText("arb-stat", a.quotes.length
    ? nf.format(a.quotes.length) + " PAIRS · BEST " + fmtSignedCents(best) + "/CT"
    : "0 PAIRS");
  renderSummary(vis);
  const c = $("arb-rows");
  c.textContent = "";
  ageCells = [];
  const fresh = freshness();
  vis.forEach((q, i) => {
    const b = q.best;
    const has = b.qty > 0;
    const act = clearsMin(q);
    const on = i === a.idx;
    // .mon-row is what main.js's select-to-copy matches on; css/arb.css undoes
    // the one property (the left border) the monitor row brings with it.
    const row = el("div", "arb-row mon-row" + (on ? " sel" : "") + (has ? " pos" : " flat")
      + (act ? " act" : ""));
    row.id = "arb-r" + i;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", on ? "true" : "false");
    if (act) {
      row.title = paperOn
        ? "CLEARS THE PAPER TRADER MINIMUM OF " + paperMin + " TICKS/CT"
        : "AT OR ABOVE " + paperMin + " TICKS/CT — THE PAPER TRADER DEFAULT,"
          + " NOT IN FORCE: NO PAPER TRADER ON THIS RUN";
    }
    // How current each price is, judged the way its venue delivers it: Kalshi
    // is streamed (LIVE, however long since it last changed), Polymarket US is
    // polled (its age in seconds). pages/arb-model.js has the reasoning.
    const kAge = el("span", "aq"), pAge = el("span", "aq num");
    const books = el("span", "ar-books");
    books.append(kAge, el("span", "aq-sep", "·"), pAge);
    const cell = { q, books, kAge, pAge };
    ageCells.push(cell);
    paintAge(cell, fresh);
    row.append(
      el("span", "ar-net num", has ? fmtSignedCents(b.net_per_contract_ticks) : "—"),
      el("span", "ar-size num", has ? nf.format(Math.round(b.contracts)) : "—"),
      el("span", "num", has ? fmtSignedCents(b.gross_per_contract_ticks) : "—"),
      el("span", "num", has ? fmtSignedCents(-b.fee_per_contract_ticks) : "—"),
      titled(el("span", "ar-label", labelOf(q))),
      el("span", "ar-dir", has ? (act ? "▸ " : "") + dirLabel(b.direction) : "NO EDGE"),
      el("span", "num", bboText(q.kalshi)),
      el("span", "num", bboText(q.polymarket_us)),
      books,
    );
    row.addEventListener("click", () => { pinned = true; a.idx = i; a.selectedPair = q.pair_id; schedule("arb"); });
    c.append(row);
  });
  // A placeholder is not data: no .mon-row, so it never copies as a row.
  if (!vis.length) c.append(el("div", "arb-none quiet-line", emptyText()));
  const q = vis[a.idx];
  const empty = $("arb-empty");
  if (!q) empty.textContent = emptyText(true);
  empty.hidden = !!q;
  $("arb-detail").hidden = !q;
  if (!q) {
    c.removeAttribute("aria-activedescendant");
    return;
  }
  c.setAttribute("aria-activedescendant", "arb-r" + a.idx);
  const b = q.best, o = q.other;
  setText("ad-label", q.label || "—");
  setText("ad-dir", b.qty > 0 ? dirLabel(b.direction) : "NO EDGE AT CURRENT BOOKS");
  setText("ad-net", b.qty > 0 ? fmtSignedCents(b.net_per_contract_ticks) : "—");
  setText("ad-gross", b.qty > 0 ? fmtSignedCents(b.gross_per_contract_ticks) : "—");
  setText("ad-fees", b.qty > 0 ? fmtSignedCents(-b.fee_per_contract_ticks) : "—");
  setText("ad-size", b.qty > 0 ? fmtQty(b.qty) + " CTS" : "—");
  setText("ad-total", b.qty > 0 ? fmtDollarsFromTicks(b.net_ticks) : "—");
  const legs = $("ad-legs");
  legs.textContent = "";
  for (const leg of b.legs) {
    const box = el("div", "ad-leg");
    const mk = (k, v) => { const kv = el("div", "kv"); kv.append(el("span", "k", k), el("span", "v num", v)); return kv; };
    box.append(
      mk(leg.venue.toUpperCase().replace("_US", " US"), leg.side.replace("_", " ").toUpperCase()),
      mk("WORST PRICE", b.qty > 0 ? fmtCents(leg.worst_price) + "¢" : "—"),
      mk("FEE", b.qty > 0 ? fmtDollarsFromTicks(leg.fee_ticks) : "—"),
    );
    legs.append(box);
  }
  const fi = q.fee_info || {};
  setText("ad-kfee", (fi.kalshi_fee_type || "—") + " × " + (fi.kalshi_fee_multiplier || "—"));
  setText("ad-pfee", "THETA " + (fi.polymarket_fee_coefficient || "—"));
  paintDetailAges(q, fresh);
  setText("ad-onet", o.qty > 0 ? fmtSignedCents(o.net_per_contract_ticks) : "NONE");
  setText("ad-osize", o.qty > 0 ? fmtQty(o.qty) + " CTS" : "—");
  const selRow = c.children[a.idx];
  if (selRow && selRow.scrollIntoView) selRow.scrollIntoView({ block: "nearest" });
}

// ---------- quote age ----------

/** What the page knows right now about how prices are arriving. */
function freshness() {
  const v = state.status && state.status.venues && state.status.venues.kalshi;
  return {
    now: performance.now(),
    feed: v ? v.state : null,
    cycleMs: M.pollCycleMs(state.stats && state.stats.polymarket_us),
  };
}

function legReadings(q, f) {
  return {
    k: M.kalshiLeg(q.kalshi, M.ageNow(state.books.get(q.kalshi.market_id), f.now), f.feed),
    p: M.polymarketLeg(q.polymarket_us, M.ageNow(state.books.get(q.polymarket_us.market_id), f.now), f.cycleMs),
  };
}

function paintAge(cell, f) {
  const r = legReadings(cell.q, f);
  const put = (node, base, x) => {
    const cls = base + " " + x.level;
    if (node.className !== cls) node.className = cls;
    if (node.textContent !== x.text) node.textContent = x.text;
  };
  put(cell.kAge, "aq", r.k);
  put(cell.pAge, "aq num", r.p);
  const tip = "KALSHI: " + r.k.detail + "\nPOLYMARKET US: " + r.p.detail;
  if (cell.books.title !== tip) cell.books.title = tip;
}

function paintDetailAges(q, f) {
  const r = legReadings(q, f);
  // The prices themselves are in the row and under LEGS; this block is only
  // about how far to trust them, and a line that clips is a line unread.
  const put = (id, x) => {
    const node = $(id);
    if (node.textContent !== x.detail) { node.textContent = x.detail; node.title = x.detail; }
    const cls = "v aq " + x.level;
    if (node.className !== cls) node.className = cls;
  };
  put("ad-kbook", r.k);
  put("ad-pbook", r.p);
}

/** Once a second while the page is up: an age is only a reading if it moves.
    Touches the age words and the detail's two lines, nothing else. */
function tickAges() {
  if (!mounted) return;
  const f = freshness();
  for (const cell of ageCells) paintAge(cell, f);
  const q = visibleQuotes()[state.arb.idx];
  if (q) paintDetailAges(q, f);
}

function moveArb(d) {
  const a = state.arb;
  const vis = visibleQuotes();
  if (!vis.length) return;
  pinned = true;                  // the operator chose: stop following the top
  a.idx = Math.max(0, Math.min(vis.length - 1, a.idx + d));
  a.selectedPair = vis[a.idx].pair_id;
  schedule("arb");                // app.js rendered inline; the rAF batch owns it now
}

// ---------- init (module scope: type=module defers, the DOM is parsed) ----------

loadView();
buildControls();
buildFoot();

// Registered at module load, NOT in mount(): the snapshot has to stay current
// while you are on another page, or arriving here shows a stale table.
onMessage("arb", (m) => {
  state.arb.quotes = Array.isArray(m.quotes) ? m.quotes : [];
  if (mounted) schedule("arb");
});

export default {
  id: "arb",
  path: "/arb",
  title: "ARB",
  nav: true,
  root: "arbpage",

  // Tab from the ARB> line walks these in order; "/" inside the list opens the
  // first text one. The list is where the cursor keys act, so ↑/↓ from the
  // command line focuses it instead of moving anything.
  regions: ["arb-min", "arb-rows"],
  listRegion: "arb-rows",

  mount() {
    mounted = true;
    state.arb.open = true;
    loadPaperMin();
    schedule("arb");
    if (ageTimer == null) ageTimer = setInterval(tickAges, 1000);
  },

  unmount() {
    mounted = false;
    state.arb.open = false;
    if (ageTimer != null) { clearInterval(ageTimer); ageTimer = null; }
    ageCells = [];
    if (paperCtl) {
      try { paperCtl.abort(); } catch (err) { /* already settled */ }
      paperCtl = null;
    }
  },

  render() {
    renderArb();
  },

  /** The keys the list answers to, published for the strip and the ARB> band.
      Only LIST scope has any: from the command line every letter is typing. */
  keyHints(scope) {
    if (scope !== SCOPE.LIST) return [];
    return [
      { k: "↑↓", d: "SELECT" },
      { k: "⏎", d: "DES" },
      { k: "/", d: "MIN NET" },
    ];
  },

  // This page owns no letter keys, so there is nothing here that needs a
  // `scope === SCOPE.LIST` guard: arrows are a view move (G0) and ENTER is
  // navigation (G1), both free in any scope core/keys.js hands them over in.
  // In practice arrows arrive in LIST scope — from COMMAND the core focuses
  // the list first, without moving the cursor, so row 0 is the next candidate.
  onKey(e, scope) {
    const k = e.key;
    if (k === "ArrowUp" || k === "ArrowDown") {
      moveArb(k === "ArrowUp" ? -1 : 1);
      return true;
    }
    // Enter is the command line's whenever something is typed; empty buffer
    // means "open the Kalshi leg", exactly as the footer says. The core only
    // consults a page for Enter once the buffer is empty; this second check is
    // the one that survives if that ever changes.
    if (k === "Enter" && cmd.buffer().trim() === "") {
      const q = visibleQuotes()[state.arb.idx];
      if (!q) return false;
      if (!state.byId.has(q.kalshi.market_id)) {
        cmd.message("NO LIVE BOOK FOR " + q.kalshi.ticker, "err");
        return true;
      }
      select(q.kalshi.market_id);
      navigate("/market/" + encodeURIComponent(q.kalshi.market_id));
      return true;
    }
    return false;
  },
};
