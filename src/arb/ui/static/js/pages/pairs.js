/* pages/pairs.js — PAIRS: cross-venue pair review.

   A proposal run stores the whole cross product that scored above the cut —
   twelve thousand rows is normal — and a human has to walk it. The screen is
   therefore a review queue, not a table: one keystroke per decision, the
   cursor always landing on the next candidate, and the evidence for the score
   sitting next to the row so the judgement can be made without leaving.

   Ported from the pre-multipage static/app.js, see git history (openPairs/closePairs/movePair/cyclePairFilter/
   renderPairs/onPairsKey and the fetch + decide calls). Three things changed
   and every one of them is written down in the comment above it:
     - the list is fetched ONCE, unfiltered, and filtered in the client;
     - only the first MAX_ROWS rows are put in the DOM;
     - Escape is the shell's now (core/keys.js), so it is not claimed here. */

import { $, el, titled, setText } from "../core/dom.js";
import { state, schedule } from "../core/state.js";
import { nf, fmtWhen } from "../core/format.js";

const PAIR_FILTERS = ["proposed", "confirmed", "rejected", "all"];
// A full universe proposal is ~12k rows. Building them all costs ~100k DOM
// nodes on every keystroke; the list is score-ordered, so the top slice IS the
// review queue and the rest is reachable by deciding or searching. The banner
// under the list says exactly how many are held back — nothing is hidden.
const MAX_ROWS = 500;
// The payload carries both legs' rules text (~17MB for a full run). Re-fetching
// it every time the tab is opened is not free, so a recent load is reused and
// R forces a refresh.
const TTL_MS = 60000;

let mounted = false;
let listDirty = true;       // the row set changed: rebuild the DOM list
let lastSel = -1;           // index currently carrying .sel, -1 after a rebuild
let loadSeq = 0;            // bumped on every load AND on unmount
let ctl = null;             // AbortController for the in-flight load
const rowEls = [];          // rendered rows, index-aligned with p.rows
let armed = null;           // pending bulk decision awaiting a second keypress
let msgTimer = null;        // clears a transient p.msg back to the row count

// A bulk decision is two keystrokes: the first arms and states the damage, the
// second does it. ARM_MS is how long the armed prompt stands.
const ARM_MS = 8000;
// p.msg is feedback about an action, not a readout: it borrows the header stat
// for a few seconds and then the live row count — this page's only "how many am
// I looking at" signal — comes back on its own.
const MSG_MS = 6000;

// This page owns state.pairs; it gains a few fields the overlay never needed.
Object.assign(state.pairs, { all: [], q: "", matchCount: 0, loadedAt: 0, undo: null });

// ---------- the header message ----------

function setMsg(text, ms) {
  // ms = 0 is a failure: it is the only place the error is reported, so it
  // holds until the operator does something that makes it stale.
  if (msgTimer) { clearTimeout(msgTimer); msgTimer = null; }
  state.pairs.msg = text;
  const ttl = ms === undefined ? MSG_MS : ms;
  if (ttl > 0) msgTimer = setTimeout(clearMsg, ttl);
}

function clearMsg() {
  if (msgTimer) { clearTimeout(msgTimer); msgTimer = null; }
  armed = null;
  if (!state.pairs.msg) return;
  state.pairs.msg = "";
  schedule("pairs");
}

// ---------- search index ----------

function haystack(row) {
  const k = row.kalshi || {};
  const q = row.polymarket_us || {};
  return ["#" + row.id, k.ticker, k.outcome, k.event_title, k.event_slug,
    q.ticker, q.outcome, q.event_title, q.event_slug]
    .filter(Boolean).join(" ").toLowerCase();
}

function matchesQuery(row, terms) {
  const hay = row._hay || "";
  for (const t of terms) if (!hay.includes(t)) return false;
  return true;
}

// ---------- filter / counts (client side: the list is loaded whole) ----------

function counts() {
  const c = { proposed: 0, confirmed: 0, rejected: 0, all: 0 };
  for (const row of state.pairs.all) {
    if (c[row.status] != null) c[row.status] += 1;
    c.all += 1;
  }
  return c;
}

function applyFilter() {
  const p = state.pairs;
  // The visible set is about to change, so an armed bulk decision no longer
  // describes what the operator can see. It dies here rather than firing on a
  // different set of rows than the prompt named.
  armed = null;
  const q = p.q.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const out = [];
  for (const row of p.all) {
    if (p.filter !== "all" && row.status !== p.filter) continue;
    if (terms.length && !matchesQuery(row, terms)) continue;
    out.push(row);
  }
  p.matchCount = out.length;
  p.rows = out.length > MAX_ROWS ? out.slice(0, MAX_ROWS) : out;
  p.idx = Math.min(p.idx, Math.max(0, p.rows.length - 1));
  listDirty = true;
}

// ---------- load ----------

async function loadPairs() {
  const p = state.pairs;
  const seq = ++loadSeq;
  if (ctl) ctl.abort();
  ctl = typeof AbortController === "function" ? new AbortController() : null;
  p.loading = true;
  schedule("pairs");
  try {
    // Unfiltered: one fetch feeds all four filters, so switching one is
    // instant and the chip counts can be honest about the whole universe.
    const r = await fetch("/api/pairs", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (seq !== loadSeq) return;        // superseded, or unmounted mid-flight
    p.all = data.pairs || [];
    for (const row of p.all) row._hay = haystack(row);
    p.loadedAt = Date.now();
    clearMsg();
    applyFilter();
  } catch (err) {
    if (seq !== loadSeq) return;        // a late failure must not paint either
    p.all = [];
    p.rows = [];
    p.matchCount = 0;
    p.loadedAt = 0;
    setMsg("LOAD FAILED · " + String(err.message || err).toUpperCase(), 0);
    listDirty = true;
  }
  p.loading = false;
  schedule("pairs");
}

// ---------- decide ----------

function rememberUndo(rows, label) {
  state.pairs.undo = {
    entries: rows.map((r) => ({ id: r.id, status: r.status })),
    label,
  };
}

function patch(row, updated) {
  // Mutated in place so the row object shared by p.all and p.rows stays one
  // object — app.js replaced it, which only worked because it had one array.
  Object.assign(row, updated);
}

async function decidePair(status) {
  const p = state.pairs;
  const row = p.rows[p.idx];
  if (!row) return;
  try {
    const r = await fetch("/api/pairs/" + row.id + "/decide", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
      cache: "no-store",
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const updated = await r.json();
    rememberUndo([row], "#" + row.id);
    patch(row, updated);
    setMsg("#" + row.id + " " + status.toUpperCase());
    // Under a status filter the decided row no longer belongs; drop it and
    // keep the cursor on the next candidate — review flows top to bottom.
    if (p.filter !== "all" && updated.status !== p.filter) applyFilter();
    else listDirty = true;
  } catch (err) {
    setMsg("DECIDE FAILED · " + String(err.message || err).toUpperCase(), 0);
  }
  schedule("pairs");
}

function eventKey(r) {
  return ((r.kalshi || {}).event_title || "") + "" + ((r.polymarket_us || {}).event_title || "");
}

function eventLabel(row) {
  const t = (row.kalshi || {}).event_title || (row.polymarket_us || {}).event_title || "";
  return (t.length > 32 ? t.slice(0, 31) + "…" : t).toUpperCase() || "—";
}

// How many rows of this event the current status filter admits but the operator
// cannot see — held back by the search box or by the MAX_ROWS cap.
function hiddenInEvent(key, visible) {
  const p = state.pairs;
  let n = 0;
  for (const r of p.all) {
    if (p.filter !== "all" && r.status !== p.filter) continue;
    if (eventKey(r) === key) n += 1;
  }
  return Math.max(0, n - visible);
}

async function decideEventGroup(status) {
  // Shift+Y / Shift+N: every candidate in the selected row's event pairing
  // (same Kalshi event ↔ same Polymarket event) gets the same decision —
  // a 30-team pennant race is one judgement, not thirty.
  const p = state.pairs;
  const row = p.rows[p.idx];
  if (!row) return;
  const key = eventKey(row);
  // The VISIBLE rows only. This used to run over p.all under the status filter,
  // which ignored the search box: searching one candidate's name and pressing
  // Shift+Y wrote all seventeen of its siblings. A bulk write to the dataset
  // that drives trading must never reach a row the operator is not looking at,
  // so when a search hides siblings they are left alone and the message says
  // so; clear the search (or decide the top slice) to reach the rest.
  const group = p.rows.filter((r) => eventKey(r) === key);
  const hidden = hiddenInEvent(key, group.length);
  const label = eventLabel(row);
  const verb = status === "confirmed" ? "CONFIRM" : "REJECT";
  const scope = hidden
    ? nf.format(group.length) + " OF " + nf.format(group.length + hidden) + " IN EVENT (VISIBLE ONLY)"
    : nf.format(group.length) + (group.length === 1 ? " ROW" : " ROWS");
  // Arm first: one keystroke states the exact count and the event, the next one
  // spends it. A single row is the same write as plain Y, so it needs no guard.
  const fresh = armed && armed.status === status && armed.key === key
    && armed.n === group.length && Date.now() < armed.until;
  if (group.length > 1 && !fresh) {
    armed = { status, key, n: group.length, until: Date.now() + ARM_MS };
    setMsg((status === "confirmed" ? "SHIFT+Y" : "SHIFT+N") + " AGAIN → " + verb + " "
      + scope + " · " + label, ARM_MS);
    schedule("pairs");
    return;
  }
  armed = null;
  try {
    const r = await fetch("/api/pairs/decide", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids: group.map((g) => g.id), status }),
      cache: "no-store",
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const res = await r.json();
    setMsg(nf.format(res.updated) + " " + status.toUpperCase() + " · " + label);
    if (res.updated !== group.length) {
      // The server touched a different number of rows than we asked for: our
      // copy is not trustworthy, so re-read rather than guess.
      await loadPairs();
      return;
    }
    rememberUndo(group, nf.format(res.updated) + (res.updated === 1 ? " ROW" : " ROWS"));
    for (const g of group) g.status = status;
    applyFilter();
  } catch (err) {
    setMsg("DECIDE FAILED · " + String(err.message || err).toUpperCase(), 0);
  }
  schedule("pairs");
}

async function undoLast() {
  const p = state.pairs;
  const u = p.undo;
  if (!u || !u.entries.length) return;
  // Re-deciding is how the API undoes; group by the status each row had, so a
  // mixed batch goes back to exactly what it was.
  const byStatus = new Map();
  for (const e of u.entries) {
    if (!byStatus.has(e.status)) byStatus.set(e.status, []);
    byStatus.get(e.status).push(e.id);
  }
  const prev = new Map(u.entries.map((e) => [e.id, e.status]));
  p.undo = null;
  try {
    for (const [status, ids] of byStatus) {
      const r = await fetch("/api/pairs/decide", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids, status }),
        cache: "no-store",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      await r.json();
    }
    for (const row of p.all) {
      const was = prev.get(row.id);
      if (was) row.status = was;
    }
    setMsg("UNDONE · " + u.label);
    applyFilter();
  } catch (err) {
    setMsg("UNDO FAILED · " + String(err.message || err).toUpperCase(), 0);
    await loadPairs();
    return;
  }
  schedule("pairs");
}

// ---------- cursor ----------

function movePair(d) {
  const p = state.pairs;
  if (!p.rows.length) return;
  p.idx = Math.max(0, Math.min(p.rows.length - 1, p.idx + d));
  schedule("pairs");
}

function cyclePairFilter(d) {
  const p = state.pairs;
  const n = PAIR_FILTERS.length;
  p.filter = PAIR_FILTERS[(PAIR_FILTERS.indexOf(p.filter) + d + n) % n];
  p.idx = 0;
  clearMsg();
  applyFilter();
  schedule("pairs");
}

function setFilter(status) {
  const p = state.pairs;
  if (p.filter === status) return;
  p.filter = status;
  p.idx = 0;
  clearMsg();
  applyFilter();
  schedule("pairs");
}

// ---------- controls (built here; index.html carries the ported markup) ----------

const chipEls = new Map();
let qInput = null;
let undoBtn = null;
let progTxt = null;
let barC = null;
let barR = null;

function buildControls() {
  const left = document.querySelector("#pairs .pairs-left");
  const cols = document.querySelector("#pairs .pair-cols");
  if (!left || !cols) return;

  const ctlRow = el("div", "pair-ctl");
  qInput = el("input", "pair-q");
  qInput.type = "text";
  qInput.id = "pair-q";
  qInput.placeholder = "SEARCH TICKER / SLUG / TITLE";
  qInput.setAttribute("aria-label", "Search pair candidates");
  qInput.autocomplete = "off";
  qInput.spellcheck = false;

  const chips = el("div", "pair-chips");
  chips.setAttribute("role", "group");
  chips.setAttribute("aria-label", "Status filter");
  for (const s of PAIR_FILTERS) {
    const b = el("button", "pchip st-" + s);
    b.type = "button";
    b.dataset.status = s;
    b.append(el("span", "pc-l", s.toUpperCase()), el("span", "pc-n num", "—"));
    b.title = s === "all" ? "SHOW EVERY PAIR (TAB)" : "SHOW " + s.toUpperCase() + " PAIRS (TAB)";
    b.addEventListener("click", (e) => {
      setFilter(s);
      if (e.detail > 0) b.blur();   // pointer click: hand typing back to ARB>
    });
    chipEls.set(s, b);
    chips.append(b);
  }

  undoBtn = el("button", "pchip pundo");
  undoBtn.type = "button";
  undoBtn.disabled = true;
  undoBtn.textContent = "UNDO";
  undoBtn.title = "NO DECISION TO UNDO";
  undoBtn.addEventListener("click", (e) => {
    undoLast();
    if (e.detail > 0) undoBtn.blur();
  });

  ctlRow.append(qInput, chips, undoBtn);

  const prog = el("div", "pair-prog");
  const bar = el("div", "pb");
  bar.setAttribute("aria-hidden", "true");
  barC = el("span", "pb-seg pb-c");
  barR = el("span", "pb-seg pb-r");
  bar.append(barC, barR);
  progTxt = el("span", "pb-txt", "—");
  prog.append(bar, progTxt);

  left.insertBefore(ctlRow, cols);
  left.insertBefore(prog, cols);

  qInput.addEventListener("input", () => {
    const p = state.pairs;
    p.q = qInput.value;
    p.idx = 0;
    clearMsg();
    applyFilter();
    schedule("pairs");
  });
  qInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      if (qInput.value) {
        qInput.value = "";
        state.pairs.q = "";
        state.pairs.idx = 0;
        clearMsg();
        applyFilter();
        schedule("pairs");
      } else qInput.blur();
    } else if (e.key === "Enter") {
      e.preventDefault();
      qInput.blur();              // hand the keyboard back to the ARB> line
    }
  });

  const foot = document.querySelector("#pairs .des-foot");
  if (foot) {
    foot.textContent = "↑↓ SELECT · PGUP/PGDN ±10 · Y / N DECIDE · SHIFT+Y / SHIFT+N TWICE"
      + " = VISIBLE ROWS OF THIS EVENT · U UNDECIDE · TAB FILTER · / SEARCH · R REFRESH · ESC CLOSE";
  }
}

function focusSearch() {
  if (!qInput) return;
  qInput.focus();
  qInput.select();
}

// ---------- render ----------

function statLine(p) {
  if (p.matchCount > p.rows.length) {
    return nf.format(p.rows.length) + " OF " + nf.format(p.matchCount) + " PAIRS";
  }
  return nf.format(p.rows.length) + " PAIRS";
}

function renderChips(c) {
  const p = state.pairs;
  if (!undoBtn) return;
  for (const [s, b] of chipEls) {
    const on = p.filter === s;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    const n = nf.format(c[s] || 0);
    const cell = b.lastElementChild;
    if (cell.textContent !== n) cell.textContent = n;
  }
  const u = p.undo;
  undoBtn.disabled = !u;
  undoBtn.title = u ? "UNDO " + u.label : "NO DECISION TO UNDO";
}

function renderProgress(c) {
  if (!progTxt) return;
  const total = c.all;
  const decided = c.confirmed + c.rejected;
  const pct = total ? Math.round((decided / total) * 100) : 0;
  const txt = total
    ? nf.format(c.proposed) + " PROPOSED · " + nf.format(c.confirmed) + " CONFIRMED · "
      + nf.format(c.rejected) + " REJECTED · " + pct + "% REVIEWED"
    : "NO PAIRS STORED";
  if (progTxt.textContent !== txt) progTxt.textContent = txt;
  barC.style.width = total ? ((c.confirmed / total) * 100).toFixed(2) + "%" : "0%";
  barR.style.width = total ? ((c.rejected / total) * 100).toFixed(2) + "%" : "0%";
}

function buildRow(row, i) {
  const kk = row.kalshi || {};
  const pp = row.polymarket_us || {};
  const r = el("div", "pair-row");
  r.id = "pair-r" + row.id;
  r.setAttribute("role", "option");
  r.setAttribute("aria-selected", "false");
  const k = el("span", "pr-leg"), q = el("span", "pr-leg");
  // Identifiers are clipped by CSS only (textContent stays whole so copy
  // is exact); title exposes the rest on hover, because a half-read slug
  // resolves to nothing on either venue.
  k.append(titled(el("span", "pr-id", kk.ticker || "")), el("span", "pr-out", kk.outcome || ""));
  q.append(titled(el("span", "pr-id", pp.ticker || "")), el("span", "pr-out", pp.outcome || ""));
  r.append(
    el("span", "pr-score num", Number(row.score).toFixed(2)),
    k, q,
    el("span", "pr-status st-" + row.status, String(row.status).toUpperCase()),
  );
  r.addEventListener("click", () => {
    state.pairs.idx = i;
    schedule("pairs");
  });
  return r;
}

function emptyText(p) {
  if (p.q.trim()) return "NO MATCH FOR " + p.q.trim().toUpperCase();
  if (p.all.length) return "NONE " + p.filter.toUpperCase();
  return p.filter === "proposed" ? "NO PROPOSALS — RUN: arb pairs propose" : "NONE";
}

function renderList() {
  listDirty = false;
  lastSel = -1;
  rowEls.length = 0;
  const p = state.pairs;
  const c = $("pair-rows");
  const frag = document.createDocumentFragment();
  if (!p.rows.length && !p.loading) {
    frag.append(el("div", "pair-row quiet-line", emptyText(p)));
  }
  p.rows.forEach((row, i) => {
    const r = buildRow(row, i);
    rowEls.push(r);
    frag.append(r);
  });
  if (p.matchCount > p.rows.length) {
    frag.append(el("div", "pair-row quiet-line",
      "TOP " + nf.format(p.rows.length) + " BY SCORE · " + nf.format(p.matchCount - p.rows.length)
      + " MORE — SEARCH OR DECIDE TO REACH THEM"));
  }
  c.replaceChildren(frag);
}

function syncSelection() {
  const p = state.pairs;
  if (lastSel === p.idx) return;
  const prev = rowEls[lastSel];
  if (prev) {
    prev.classList.remove("sel");
    prev.setAttribute("aria-selected", "false");
  }
  lastSel = p.idx;
  const rows = $("pair-rows");
  const cur = rowEls[p.idx];
  if (!cur) {
    rows.removeAttribute("aria-activedescendant");
    return;
  }
  cur.classList.add("sel");
  cur.setAttribute("aria-selected", "true");
  rows.setAttribute("aria-activedescendant", cur.id);
  if (mounted && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
}

function renderDetail() {
  const p = state.pairs;
  const row = p.rows[p.idx];
  $("pair-empty").hidden = !!row;
  $("pair-detail").hidden = !row;
  if (!row) return;
  const kk = row.kalshi || {}, pp = row.polymarket_us || {}, f = row.features || {};
  setText("pd-k-event", kk.event_title || "—");
  setText("pd-k-outcome", kk.outcome || "—");
  setText("pd-k-ticker", kk.ticker || "—");
  setText("pd-k-close", fmtWhen(kk.close_time));
  setText("pd-k-rules", kk.rules || "—");
  setText("pd-p-event", pp.event_title || "—");
  setText("pd-p-outcome", pp.outcome || "—");
  setText("pd-p-ticker", pp.ticker || "—");
  setText("pd-p-close", fmtWhen(pp.close_time));
  setText("pd-p-rules", pp.rules || "—");
  setText("pd-score", Number(row.score).toFixed(3));
  setText("pd-title", f.title_similarity != null ? Number(f.title_similarity).toFixed(2) : "—");
  setText("pd-outcome", f.outcome_similarity != null ? Number(f.outcome_similarity).toFixed(2) : "—");
  setText("pd-overlap", f.outcome_overlap != null ? Number(f.outcome_overlap).toFixed(2) : "—");
  setText("pd-days", f.days_apart != null ? String(f.days_apart) : "—");
  const st = $("pd-status");
  st.textContent = String(row.status).toUpperCase();
  st.className = "v " + (row.status === "confirmed" ? "st-live" : row.status === "rejected" ? "st-off" : "");
}

function renderPairs() {
  const p = state.pairs;
  if (!p.open) return;
  setText("pairs-filter", p.filter.toUpperCase());
  setText("pairs-stat", p.loading ? "LOADING" : (p.msg || statLine(p)));
  const c = counts();
  renderChips(c);
  renderProgress(c);
  if (listDirty) renderList();
  syncSelection();
  renderDetail();
}

// ---------- keys ----------

function onPairsKey(e) {
  const k = e.key;
  const up = k.length === 1 ? k.toUpperCase() : k;
  // Only a repeat of the same bulk key keeps the armed prompt alive: moving the
  // cursor or touching anything else must not leave a loaded Shift+Y behind.
  if (!(e.shiftKey && (up === "Y" || up === "N"))) armed = null;
  // Escape belongs to core/keys.js now ("ESC CLOSE" = back to the monitor);
  // claiming it here would make this page the one that behaves differently.
  if (k === "ArrowUp" || k === "ArrowDown") { movePair(k === "ArrowUp" ? -1 : 1); return true; }
  if (k === "PageUp" || k === "PageDown") { movePair(k === "PageUp" ? -10 : 10); return true; }
  if (k === "Home") { movePair(-state.pairs.rows.length); return true; }
  if (k === "End") { movePair(state.pairs.rows.length); return true; }
  if (k === "Tab") { cyclePairFilter(e.shiftKey ? -1 : 1); return true; }
  if (k === "/") { focusSearch(); return true; }
  if (up === "Y") { (e.shiftKey ? decideEventGroup : decidePair)("confirmed"); return true; }
  if (up === "N") { (e.shiftKey ? decideEventGroup : decidePair)("rejected"); return true; }
  if (up === "U") { decidePair("proposed"); return true; }
  if (up === "R") { loadPairs(); return true; }
  return false;
}

// ---------- init (module scope: the DOM is parsed, type=module defers) ----------


buildControls();

export default {
  id: "pairs",
  path: "/pairs",
  title: "PAIRS",
  nav: true,
  root: "pairs",

  mount() {
    mounted = true;
    const p = state.pairs;
    p.open = true;
    if (qInput) qInput.value = p.q;
    listDirty = true;
    lastSel = -1;
    if (!p.all.length || Date.now() - p.loadedAt > TTL_MS) loadPairs();
    else schedule("pairs");
  },

  unmount() {
    mounted = false;
    state.pairs.open = false;
    // Feedback about an action does not survive leaving the page; coming back
    // must show the live count, not the last decision.
    clearMsg();
    // Any in-flight load resolves into a dead sequence number and paints
    // nothing; the abort is the courtesy, the counter is the guarantee.
    loadSeq += 1;
    if (ctl) { ctl.abort(); ctl = null; }
    state.pairs.loading = false;
  },

  render() {
    renderPairs();
  },

  onKey(e) {
    return onPairsKey(e);
  },
};
