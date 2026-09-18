/* pages/system.js — SYSTEM (/system): is anything wrong, and what do I do?

   The page used to be seven cards of raw counters with a paragraph under each,
   and it never said whether the numbers were good. It now answers one question
   in three passes, top to bottom:

     verdict   ALL SYSTEMS NORMAL / 2 THINGS TO LOOK AT / 1 PROBLEM — one line
     pipeline  the machine as a picture: feeds → books → engine → paper, and
               recorder → database, each stage with a light and one figure
     checks    one plain-English line per part, problems first; the selected
               check explains itself on the right — what it does, the numbers
               behind the reading, and what to do when there is something to do

   Every judgement comes from pages/system-model.js (pure, tested in node).
   This module only gathers the inputs and draws: it builds the DOM once and
   updates text in place, like every other page here.

   Keyboard: the check list is this page's list region. ↑↓ select, ⏎ opens the
   page where that check's fix lives (/control, /pairs, /paper, ...). */

import { $, el } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { onMessage } from "../core/ws.js";
import { navigate } from "../core/router.js";
import { SCOPE, chordLabel } from "../core/keys.js";
import { nf, fmtUptime } from "../core/format.js";
import * as M from "./system-model.js";

const TICK_MS = 1000;
const STALE_MS = 5000;
const SPARK_S = 120;
const FONT = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';

const LEVEL_TAG = { ok: "OK", warn: "LOOK", fail: "PROBLEM", off: "OFF", wait: "WAIT" };

let mounted = false;
let tickTimer = 0;
let ctl = null;                 // latest control frame; /api/status is the fallback
let checks = [];                // sorted, as displayed
let selectedId = null;
let userPicked = false;         // until the operator picks, follow the worst check
let lastOrder = "";

onMessage("control", (m) => {
  if (m && m.control) {
    ctl = m.control;
    schedule("system");
  }
});

const E = {};
const rows = new Map();         // check id -> {row, light, tag, name, reading, go}
const stages = new Map();       // stage id -> {node, light, fig, sub}

// ---------- markup ----------

const STAGES = [
  // id, label, grid placement
  ["kalshi", "KALSHI", "s-k"],
  ["polymarket", "POLYMARKET US", "s-p"],
  ["books", "ORDER BOOKS", "s-b"],
  ["engine", "ARB ENGINE", "s-e"],
  ["paper", "PAPER TRADER", "s-t"],
  ["recorder", "RECORDER", "s-r"],
  ["database", "DATABASE", "s-d"],
];

function light(cls) {
  const l = el("span", "sl " + (cls || ""));
  l.setAttribute("aria-hidden", "true");
  return l;
}

function build() {
  const page = el("div", "sys-page");

  const head = el("div", "des-head");
  const title = el("span", "des-title");
  title.append(el("span", "des-tag", "SYS"), el("span", null, "HEALTH"));
  E.headStat = el("span", "head-stat sys-headstat", "—");
  head.append(title, E.headStat);

  // verdict
  const v = el("div", "sysv lv-wait");
  E.verdict = v;
  E.vLight = light();
  const vText = el("div", "sysv-text");
  E.vHead = el("div", "sysv-head", "STARTING UP");
  E.vLine = el("div", "sysv-line", "waiting for the first data");
  vText.append(E.vHead, E.vLine);
  const vRate = el("div", "sysv-rate");
  E.spark = el("canvas", "sysv-spark");
  E.spark.setAttribute("aria-hidden", "true");
  E.rate = el("div", "sysv-ratenum", "—");
  vRate.append(E.spark, E.rate);
  vRate.title = "MESSAGES PER SECOND FROM BOTH VENUES, LAST 2 MINUTES";
  v.setAttribute("role", "status");
  v.setAttribute("aria-live", "polite");
  v.append(E.vLight, vText, vRate);

  // pipeline
  const pipe = el("div", "sysp");
  pipe.setAttribute("aria-label", "Pipeline");
  for (const [id, label, cls] of STAGES) {
    const node = el("button", "sysp-node " + cls + " lv-wait");
    node.type = "button";
    const l = light();
    const name = el("span", "sysp-name", label);
    const fig = el("span", "sysp-fig", "—");
    const sub = el("span", "sysp-sub", "");
    node.append(l, name, fig, sub);
    node.addEventListener("click", () => {
      const c = checks.find((x) => x.stage === id) || checks.find((x) => x.id === id);
      if (c) pick(c.id);
    });
    stages.set(id, { node, fig, sub });
    pipe.append(node);
  }

  // checks + detail
  const main = el("div", "sys-main");
  const listWrap = el("div", "sys-listwrap");
  listWrap.append(el("div", "sys-colhead", "CHECKS · PROBLEMS FIRST"));
  E.list = el("div", "sys-checks");
  E.list.id = "sys-checks";
  E.list.tabIndex = 0;
  E.list.dataset.keyregion = "list";
  E.list.setAttribute("role", "listbox");
  E.list.setAttribute("aria-label", "Health checks");
  listWrap.append(E.list);

  const d = el("div", "sys-detail");
  d.setAttribute("aria-live", "off");
  const dHead = el("div", "sysd-head");
  E.dLight = light();
  E.dName = el("span", "sysd-name", "—");
  E.dTag = el("span", "sysd-tag", "");
  dHead.append(E.dLight, E.dName, E.dTag);
  E.dReading = el("div", "sysd-reading", "");
  E.dFixWrap = el("div", "sysd-fix");
  E.dFixWrap.append(el("div", "sysd-label", "WHAT TO DO"), (E.dFix = el("div", "sysd-fixtext", "")));
  E.dWhat = el("div", "sysd-what", "");
  E.dNums = el("div", "sysd-nums");
  E.dRunsWrap = el("div", "sysd-runs");
  E.dRunsWrap.append(el("div", "sysd-label", "RECORDED RUNS · NEWEST FIRST"), (E.dRuns = el("div")));
  E.dGo = el("button", "sysd-go", "");
  E.dGo.type = "button";
  E.dGo.addEventListener("click", openSelected);
  d.append(dHead, E.dReading, E.dFixWrap, el("div", "sysd-label", "WHAT THIS IS"), E.dWhat,
    el("div", "sysd-label", "THE NUMBERS"), E.dNums, E.dRunsWrap, E.dGo);
  E.detail = d;
  main.append(listWrap, d);

  E.foot = el("div", "des-foot", "");
  page.append(head, v, pipe, main, E.foot);
  $("system-page").append(page);
}

function makeRow(c) {
  const row = el("div", "sysc");
  row.id = "sysc-" + c.id;
  row.setAttribute("role", "option");
  const l = light();
  const tag = el("span", "sysc-tag", "");
  const name = el("span", "sysc-name", c.name);
  const reading = el("span", "sysc-reading", "");
  const go = el("span", "sysc-go", "");
  row.append(l, tag, name, reading, go);
  row.addEventListener("click", () => pick(c.id));
  row.addEventListener("dblclick", () => { pick(c.id); openSelected(); });
  const r = { row, tag, name, reading, go };
  rows.set(c.id, r);
  return r;
}

// ---------- inputs ----------

function gather() {
  const control = ctl || (state.status && state.status.control) || null;
  const floor = control && control.paper && control.paper.limits ? control.paper.limits.min_net_ticks : null;
  const now = Date.now();
  return {
    now,
    conn: state.conn,
    statusAt: state.statusAt,
    runId: state.runId || (state.status && state.status.run_id) || null,
    stats: state.stats,
    statsBuf: state.statsBuf,
    status: state.status,
    control,
    books: state.markets.length ? M.summariseBooks(state.markets, state.books, performance.now(), STALE_MS) : null,
    arb: M.summariseArb(state.arb.quotes, floor),
  };
}

// ---------- stage figures: one number a stage is known by ----------

function stageFigures(x) {
  const out = {};
  const v = (x.status && x.status.venues) || {};
  const s = x.stats;
  out.kalshi = [v.kalshi ? String(v.kalshi.state).toUpperCase() : "—", "WEBSOCKET · PUSHED"];
  const cyc = x.control && x.control.pairs && x.control.pairs.poll ? x.control.pairs.poll.cycle_s : null;
  out.polymarket = [v.polymarket_us ? String(v.polymarket_us.state).toUpperCase() : "—",
    cyc != null ? "EVERY " + Math.round(cyc) + "s PER BOOK" : "REST · POLLED"];
  if (x.books) {
    const bad = Object.values(x.books.invalid).reduce((a, k) => a + k, 0);
    out.books = [(x.books.withBook - bad) + " / " + x.books.markets, bad ? bad + " UNTRUSTED NOW" : "BOOKS GOOD"];
  } else {
    out.books = ["—", ""];
  }
  out.engine = x.arb && x.arb.pairs
    ? [x.arb.pairs + (x.arb.pairs === 1 ? " PAIR" : " PAIRS"), x.arb.overFloor ? x.arb.overFloor + " WITH AN EDGE" : "NO EDGE NOW"]
    : ["IDLE", "NOTHING WATCHED"];
  const p = x.control && x.control.paper;
  out.paper = !p || !p.attached ? ["—", "NOT RUNNING"]
    : [p.suspended ? "SUSPENDED" : (p.trades || 0) + (p.trades === 1 ? " TRADE" : " TRADES"),
      "$" + Math.round(p.notional_ticks / 10000) + " OF $" + Math.round(p.limits.max_notional_ticks / 10000)];
  out.recorder = !s ? ["—", ""] : s.recorder ? ["ON", nf.format(s.recorder.dropped) + " LOST"] : ["OFF", "NOT SAVING"];
  const db = x.status && x.status.database;
  out.database = !db ? ["—", ""] : [db.connected ? "UP" : "DOWN",
    db.raw_messages_total != null ? nf.format(db.raw_messages_total) + " ROWS" : ""];
  return out;
}

// ---------- drawing ----------

function setTxt(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function setLevel(node, level) {
  const want = "lv-" + level;
  if (node.classList.contains(want)) return;
  for (const l of M.LEVELS) node.classList.remove("lv-" + l);
  node.classList.add(want);
}

function pick(id) {
  selectedId = id;
  userPicked = true;
  schedule("system");
}

function openSelected() {
  const c = checks.find((x) => x.id === selectedId);
  if (c && c.goto) navigate(c.goto.path);
}

function move(d) {
  if (!checks.length) return;
  let i = checks.findIndex((c) => c.id === selectedId);
  i = Math.max(0, Math.min(checks.length - 1, (i < 0 ? 0 : i) + d));
  pick(checks[i].id);
}

function renderList() {
  for (const c of checks) {
    const r = rows.get(c.id) || makeRow(c);
    setLevel(r.row, c.level);
    setTxt(r.tag, LEVEL_TAG[c.level]);
    setTxt(r.reading, c.reading);
    r.reading.title = c.reading;
    setTxt(r.go, c.goto ? "→ " + c.goto.label : "");
    const on = c.id === selectedId;
    if (r.row.classList.contains("sel") !== on) {
      r.row.classList.toggle("sel", on);
      r.row.setAttribute("aria-selected", on ? "true" : "false");
    }
  }
  // Moving existing nodes keeps identity (and the selection) across a re-sort.
  const order = checks.map((c) => c.id).join(",");
  if (order !== lastOrder) {
    lastOrder = order;
    E.list.replaceChildren(...checks.map((c) => rows.get(c.id).row));
  }
  const sel = rows.get(selectedId);
  if (sel) E.list.setAttribute("aria-activedescendant", sel.row.id);
}

let numsKey = "";
let runsKey = "";
function renderDetail() {
  const c = checks.find((x) => x.id === selectedId);
  if (!c) return;
  setLevel(E.detail, c.level);
  setTxt(E.dName, c.name);
  setTxt(E.dTag, LEVEL_TAG[c.level]);
  setTxt(E.dReading, c.reading);
  E.dFixWrap.hidden = !c.fix;
  setTxt(E.dFix, c.fix || "");
  setTxt(E.dWhat, c.what);
  // The kv rows are rebuilt only when the labels change; values update in place.
  const labels = c.id + "|" + c.numbers.map((p) => p[0]).join("|");
  if (labels !== numsKey) {
    numsKey = labels;
    E.dNums.replaceChildren(...c.numbers.map(([k]) => {
      const row = el("div", "kv");
      row.append(el("span", "k", k), el("span", "v", ""));
      return row;
    }));
  }
  c.numbers.forEach(([, val], i) => setTxt(E.dNums.children[i].lastChild, String(val)));
  const runs = c.runs || null;
  E.dRunsWrap.hidden = !runs || !runs.length;
  if (runs && runs.length) {
    const cur = state.runId || (state.status && state.status.run_id);
    const key = runs.slice(0, 8).map((r) => r.run_id + ":" + r.count).join(",") + cur;
    if (key !== runsKey) {
      runsKey = key;
      E.dRuns.replaceChildren(...runs.slice(0, 8).map((r) => {
        const row = el("div", "kv" + (r.run_id === cur ? " run-live" : ""));
        const k = el("span", "k run-id", r.run_id);
        k.title = r.run_id;
        row.append(k, el("span", "v run-tag", r.run_id === cur ? "THIS RUN" : ""), el("span", "v num", nf.format(r.count)));
        return row;
      }));
    }
  }
  E.dGo.hidden = !c.goto;
  if (c.goto) setTxt(E.dGo, "⏎ OPEN " + c.goto.label);
}

function renderSpark() {
  const cv = E.spark;
  const W = cv.clientWidth || 180;
  const H = cv.clientHeight || 28;
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  if (cv.width !== Math.round(W * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const ctx = cv.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const buf = state.statsBuf.slice(-SPARK_S);
  if (!buf.length) return;
  let max = 1;
  for (const s of buf) if (s.msg_rate_1s > max) max = s.msg_rate_1s;
  const bw = W / SPARK_S;
  ctx.fillStyle = "rgba(230,230,230,0.55)";
  buf.forEach((s, i) => {
    const h = Math.max(s.msg_rate_1s > 0 ? 1 : 0, (s.msg_rate_1s / max) * (H - 9));
    ctx.fillRect(W - (buf.length - i) * bw, H - h, Math.max(1, bw - 0.5), h);
  });
  ctx.fillStyle = "#6b7280";
  ctx.font = "9px " + FONT;
  ctx.fillText("PEAK " + Math.round(max) + "/S · 2 MIN", 0, 8);
}

function render() {
  if (!mounted) return;
  const x = gather();
  const raw = M.buildChecks(x);
  checks = M.sortChecks(raw);
  if (!userPicked || !checks.some((c) => c.id === selectedId)) selectedId = checks[0].id;

  const v = M.verdict(raw);
  setLevel(E.verdict, v.level);
  setTxt(E.vHead, v.headline);
  setTxt(E.vLine, v.line);
  const s = x.stats;
  setTxt(E.rate, s && s.msg_rate_1s != null ? s.msg_rate_1s.toFixed(1) + " MSG/S" : "—");
  setTxt(E.headStat, (x.runId ? "RUN " + x.runId + " · " : "") + "UP " + (s ? fmtUptime(s.uptime_s) : "—"));
  E.headStat.title = x.runId || "";

  const figs = stageFigures(x);
  const flowing = Boolean(s && s.msg_rate_1s > 0 && x.conn === "live");
  for (const [id, st] of stages) {
    setLevel(st.node, M.stageLevel(raw, id));
    setTxt(st.fig, figs[id][0]);
    setTxt(st.sub, figs[id][1]);
    st.node.classList.toggle("flow", flowing);
  }
  renderList();
  renderDetail();
  renderSpark();
}

// ---------- page ----------

function footText() {
  return "↑↓ CHECK · ⏎ OPEN WHERE THE FIX LIVES · CLICK A STAGE TO JUMP TO ITS CHECK · "
    + chordLabel() + " PAGE · ESC MONITOR";
}

build();

// ws.js and the status poll have always scheduled "poly" for this page.
registerRenderer("poly", render);

export default {
  id: "system",
  path: "/system",
  title: "SYSTEM",
  nav: true,
  root: "system-page",
  regions: ["sys-checks"],
  listRegion: "sys-checks",

  mount() {
    mounted = true;
    setTxt(E.foot, footText());
    if (!tickTimer) tickTimer = setInterval(() => schedule("system"), TICK_MS);
    schedule("system");
  },

  unmount() {
    mounted = false;
    clearInterval(tickTimer);
    tickTimer = 0;
  },

  render,

  onKey(e, scope) {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      move(e.key === "ArrowUp" ? -1 : 1);
      return true;
    }
    if (scope !== SCOPE.LIST || e.repeat) return false;
    if (e.key === "Enter") {
      openSelected();
      return true;
    }
    return false;
  },

  keyHints(scope) {
    if (scope === SCOPE.LIST) return [{ k: "↑↓", d: "CHECK" }, { k: "⏎", d: "OPEN" }];
    return [];
  },
};
