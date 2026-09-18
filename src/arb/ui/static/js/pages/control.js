/* pages/control.js — /control, the one page whose buttons write.

   THE FIVE RULES IT HAS ALWAYS FOLLOWED (unchanged by the redesign):
   1. Nothing about an action is hardcoded: grades, summaries and "asks for
      confirmation" come from GET /api/control's `actions` descriptors.
   2. No optimistic state. A switch never flips until the server says it did;
      truth arrives as a {"t":"control"} frame, so two tabs cannot disagree.
   3. The server owns the confirm copy. The sentence in a confirm box is the
      one written to the audit row, verbatim.
   4. The DOM is built once and updated in place. A field you are in, or have
      edited, is never written to (rebuilding it made the fields uneditable).
   5. A no-op is not a success: "nothing changed" is said, and stays said.

   THE DESIGN, AND WHY EACH CHOICE:
   * Sections are ordered by how often they are used: what you operate (PAPER,
     WATCH SET), what feeds it (the two universes), housekeeping (RECORDER,
     JOBS). Every section has the same anatomy — light · title · STATE, one
     sentence, numbers, controls — so the page is learned once.
   * State is the loudest thing in a section; the button is a VERB. One button
     per switch, labelled with what it will do ("SUSPEND TRADING"): the old
     START+STOP pair always had one dead button, and a slider would imply the
     instant local flip rule 2 forbids.
   * Fields are in the operator's units (¢ per contract, contracts, $), the
     ones /arb and /paper print. Conversion to ticks is exact or refused
     (control-model.js). APPLY is enabled only when something valid changed,
     and the line above it says exactly what: no dead presses (rule 5).
   * The answer appears where the question was asked: a section's confirm box
     and its receipt render INSIDE that section, not in a band at the top of
     the page that expired unseen.
   * A confirm step is a tax, spent only where a slip is expensive: actions
     that REPLACE a set (the watch set, a universe) are previewed first — the
     server prices them without doing them — and G3 jobs arm on the server as
     before. Switches and limits apply at once; they are one value, and the
     inverse is one press away.
   * The universe boxes edit the BASE list only. They used to show base +
     watched-pair legs and write that union back as the base.
   * Explanations are one sentence, in sentence case: they are read, not
     scanned. Labels stay uppercase.
   * The right rail is what happened: a job's output while it runs (DOCTOR's
     output is the whole point of running it), then the audit trail — always
     on screen, never below a fold.
   * No letter keys. Every control writes; a bare letter must never reach one. */

import { $, el } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { nf, fmtAgo } from "../core/format.js";
import { onMessage } from "../core/ws.js";
import { navigate } from "../core/router.js";
import { SCOPE } from "../core/keys.js";
import * as cmd from "../core/cmd.js";
import * as M from "./control-model.js";

const ROOT = "control-page";
const LOG_LIMIT = 40;
const TICK_MS = 1000;
const RESULT_MS = 30000;        // a receipt for a real change fades; a no-op or failure stays
const PREVIEW_TTL_MS = 30000;
const SWITCH_LOCK_MS = 700;     // after a switch flips, so a double-click cannot flip it back

const SWITCHES = new Set(["paper.suspend", "paper.resume", "recording.start", "recording.stop"]);

const GRADE_NOTE = {
  G0: "read-only", G1: "navigation", G2: "changes this run", G3: "writes many rows", G4: "irreversible",
};

const JOBS = [
  ["doctor", "jobs.doctor", "DOCTOR", "Check keys, clock, database, venues and disk."],
  ["propose", "jobs.propose", "PROPOSE PAIRS",
    "Fetch both catalogues and store scored proposals. About 3 minutes; thousands of rows."],
  ["backfill", "jobs.backfill", "BACKFILL LINKS", "Fill in missing event links on older proposals."],
  ["replay", "jobs.replay", "REPLAY A RUN", "Re-run a recorded session through the identical pipeline."],
];

let built = false;
let mounted = false;
let tickTimer = 0;
let ctl = null;
let audit = [];
let armed = null;               // {action, params, token|null, effect, until, section}
const receipts = {};            // section -> {kind, action, message, at}
const busy = new Set();
const lockUntil = {};           // section -> ms timestamp
const jobTails = new Map();     // job_id -> [lines]   (live, from job frames)
const jobLogs = new Map();      // job_id -> [lines]   (full, fetched)
let selectedJob = null;
let jobPicked = false;

const ui = { sec: {} };
const buttons = [];             // {el, action, enabled?: () => bool}
const fields = [];              // {el, read, form}

// ---------- server calls ----------

async function post(action, body) {
  try {
    const r = await fetch("/api/control/" + encodeURIComponent(action), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    let json = null;
    try { json = await r.json(); } catch (err) { json = null; }
    return { ok: r.ok, status: r.status, body: json || {} };
  } catch (err) {
    return { ok: false, status: 0, body: { error: String(err && err.message ? err.message : err) } };
  }
}

function receipt(section, kind, action, message) {
  receipts[section] = { kind, action, message, at: Date.now() };
}

/** The operator pressed a button. Set-replacing actions are priced first. */
async function attempt(action, params, form) {
  const section = M.sectionOf(action);
  if (busy.has(action)) return;
  if (!M.PREVIEW_FIRST.has(action)) return run(action, params, form);
  busy.add(action);
  render();
  const res = await post(action, { params: params || {}, preview: true });
  busy.delete(action);
  if (res.ok && res.body.effect) {
    armed = { action, params: params || null, token: null, effect: res.body.effect,
      until: Date.now() + PREVIEW_TTL_MS, section, form: form || null };
    delete receipts[section];
    cmd.message("CONFIRM TO APPLY", "warn");
  } else {
    receipt(section, "fail", action, String(res.body.error || "HTTP " + res.status));
    cmd.message("FAILED · " + String(res.body.error || res.status).toUpperCase().slice(0, 52), "err");
  }
  render();
}

/** Do it. Handles the server's arm-then-confirm handshake for G3 actions. */
async function run(action, params, form) {
  const section = M.sectionOf(action);
  if (busy.has(action)) return;
  busy.add(action);
  render();
  const token = armed && armed.action === action ? armed.token : null;
  const body = {};
  if (params) body.params = params;
  if (token) body.confirm = token;
  const res = await post(action, body);
  busy.delete(action);
  if (res.status === 428 && res.body.confirm_required) {
    // Armed on the server. The sentence is what the audit row will record.
    armed = { action, params: params || null, token: res.body.confirm_token, effect: res.body.effect,
      until: Date.now() + (res.body.expires_in_s || 0) * 1000, section, form: form || null };
    delete receipts[section];
    cmd.message("CONFIRM REQUIRED", "warn");
  } else if (res.ok) {
    // Only ITS OWN confirmation is spent. Clearing `armed` wholesale meant that
    // running DOCTOR silently dismissed a watch-set confirm in another section.
    if (armed && armed.action === action) armed = null;
    if (form) clearDirty(form);         // only the form that was applied
    if (SWITCHES.has(action)) lockUntil[section] = Date.now() + SWITCH_LOCK_MS;
    // `changed` is the server's answer to "did anything move" — not the same
    // question as "did the request succeed".
    const changed = res.body.changed !== false;
    const said = String(res.body.message || res.body.effect || action);
    receipt(section, changed ? "done" : "noop", action, said);
    cmd.message((changed ? said : "NO CHANGE · " + said).toUpperCase().slice(0, 60), changed ? "ok" : "warn");
    if (res.body.job_id) { selectedJob = res.body.job_id; jobPicked = true; }
    loadAudit();
  } else {
    if (armed && armed.action === action) armed = null;
    receipt(section, "fail", action, String(res.body.error || "HTTP " + res.status));
    cmd.message("FAILED · " + String(res.body.error || res.status).toUpperCase().slice(0, 52), "err");
  }
  render();
}

async function loadControl() {
  try {
    const r = await fetch("/api/control", { cache: "no-store" });
    if (r.ok) ctl = await r.json();
  } catch (err) { /* keep the last known state */ }
  render();
}

async function loadAudit() {
  try {
    const r = await fetch("/api/control/log?limit=" + LOG_LIMIT, { cache: "no-store" });
    if (r.ok) audit = (await r.json()).actions || [];
  } catch (err) { /* the trail is a record, not a requirement */ }
  render();
}

async function loadJobLog(jobId) {
  try {
    const r = await fetch("/api/control/jobs/" + encodeURIComponent(jobId), { cache: "no-store" });
    if (!r.ok) return;
    const j = await r.json();
    if (Array.isArray(j.lines)) jobLogs.set(jobId, j.lines.map(String));
  } catch (err) { /* the live tail still shows */ }
  render();
}

// ---------- descriptors ----------

function spec(action) {
  return ((ctl && ctl.actions) || []).find((a) => a.action === action) || null;
}

function actionTitle(action) {
  const s = spec(action);
  if (!s) return action + " — NOT AVAILABLE IN THIS PROCESS";
  const grade = s.grade + (GRADE_NOTE[s.grade] ? " · " + GRADE_NOTE[s.grade].toUpperCase() : "");
  return [s.summary.toUpperCase(), grade, s.confirm ? "ASKS FOR CONFIRMATION" : null].filter(Boolean).join(" · ");
}

// ---------- payload readers ----------

const paper = () => (ctl && ctl.paper) || {};
const limits = () => paper().limits || {};
const universe = () => (ctl && ctl.universe) || {};
const kalshi = () => universe().kalshi || {};
const polymarket = () => universe().polymarket_us || {};
const pairs = () => (ctl && ctl.pairs) || {};
const poll = () => pairs().poll || {};
const count = (v) => (v == null ? "—" : nf.format(v));   // unknown is not zero

// ---------- element factories (build time only) ----------

function setTxt(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function actionBtn(action, label, paramsFn, opts) {
  const o = opts || {};
  const b = el("button", "cbtn " + (o.cls || ""), label);
  b.type = "button";
  b.dataset.action = action;
  b.addEventListener("click", (e) => {
    const params = paramsFn ? paramsFn() : null;
    if (params !== false) attempt(action, params, o.form);
    if (e.detail > 0) b.blur();          // pointer click: typing goes back to ARB>
  });
  buttons.push({ el: b, action, enabled: o.enabled || null, section: M.sectionOf(action), isSwitch: !!o.isSwitch });
  return b;
}

function plainBtn(label, onClick, cls) {
  const b = el("button", "cbtn " + (cls || ""), label);
  b.type = "button";
  b.addEventListener("click", (e) => {
    onClick();
    if (e.detail > 0) b.blur();
  });
  return b;
}

/** A field whose value is the server's until you touch it. */
function field(id, read, opts) {
  const o = opts || {};
  const tag = o.multiline ? "textarea" : o.select ? "select" : "input";
  const e = el(tag, o.cls || "cin");
  e.id = id;
  if (tag === "input") e.type = "text";
  if (o.rows) e.rows = o.rows;
  if (o.placeholder) e.placeholder = o.placeholder;
  if (o.label) e.setAttribute("aria-label", o.label);
  e.autocomplete = "off";
  e.spellcheck = false;
  if (o.decimal) e.setAttribute("inputmode", "decimal");
  e.addEventListener("input", () => { e.dataset.dirty = "1"; render(); });
  e.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;      // Esc: back to the server's value, focus home
    ev.stopPropagation();
    delete e.dataset.dirty;
    if (read) syncField({ el: e, read }, true);
    e.blur();
    render();
  });
  if (!o.select) fields.push({ el: e, read, form: o.form || null });
  return e;
}

function syncField(f, force) {
  const e = f.el;
  // Never write into a field the operator is in, or one they have edited.
  if (!force && (document.activeElement === e || e.dataset.dirty)) return;
  const v = f.read();
  const next = v == null ? "" : String(v);
  if (e.value !== next) e.value = next;
}

function clearDirty(form) {
  for (const f of fields) {
    if (form && f.form !== form) continue;
    delete f.el.dataset.dirty;
  }
}

function revert(form) {
  clearDirty(form);
  for (const f of fields) if (f.form === form) syncField(f, true);
  render();
}

function section(id, title, blurb) {
  const root = el("section", "csec lv-wait");
  root.id = "csec-" + id;
  root.setAttribute("aria-label", title);
  const head = el("div", "csec-head");
  const light = el("span", "sl");
  light.setAttribute("aria-hidden", "true");
  const st = el("span", "csec-state", "—");
  head.append(light, el("span", "csec-title", title), st);
  const slot = el("div", "csec-slot");
  slot.hidden = true;
  root.append(head);
  if (blurb) root.append(el("p", "csec-blurb", blurb));
  root.append(slot);
  ui.sec[id] = { root, head, state: st, slot, slotKey: "" };
  return root;
}

function setLevel(node, level) {
  const want = "lv-" + level;
  if (node.classList.contains(want)) return;
  for (const l of ["ok", "warn", "fail", "off", "wait"]) node.classList.remove("lv-" + l);
  node.classList.add(want);
}

function stat(label, valueNode, subNode) {
  const c = el("div", "cstat");
  c.append(el("div", "cstat-k", label), valueNode);
  if (subNode) c.append(subNode);
  return c;
}

function formRow(label, input, unit, hintNode) {
  const r = el("div", "cform-row");
  const l = el("label", "cform-k", label);
  l.htmlFor = input.id;
  const box = el("span", "cform-in");
  box.append(input);
  if (unit) box.append(el("span", "cform-unit", unit));
  r.append(l, box, hintNode);
  return r;
}

// ---------- build ----------

function build() {
  const root = $(ROOT);
  if (!root || built) return;

  const head = el("div", "panel-head");
  head.append(el("span", "title", "CONTROL"));
  const chips = el("span", "cchips");
  ui.chipRo = el("span", "cchip cchip-ro", "READ-ONLY");
  ui.chipBind = el("span", "cchip cchip-bind", "");
  ui.headStat = el("span", "head-stat", "—");
  chips.append(ui.chipRo, ui.chipBind, ui.headStat);
  head.append(chips);

  ui.roBand = el("div", "cro", "READ-ONLY — this server was started with controls disabled. Everything "
    + "here shows the live state; nothing here will write.");

  // ---- PAPER TRADING ----
  const sPaper = section("paper", "PAPER TRADING",
    "Takes every edge that clears the floor, at the size the books show. Simulated: no orders are "
    + "ever sent. Suspending is a pause — positions and deployed money are kept.");
  ui.pDeployed = el("div", "cstat-v", "—");
  ui.pMeter = el("div", "cmeter");
  ui.pMeterFill = el("i");
  ui.pMeter.append(ui.pMeterFill);
  ui.pTrades = el("div", "cstat-v", "—");
  ui.pHeld = el("div", "cstat-v", "—");
  ui.pDeclined = el("div", "cstat-v", "—");
  const pStats = el("div", "cstats");
  pStats.append(stat("DEPLOYED", ui.pDeployed, ui.pMeter), stat("TRADES", ui.pTrades),
    stat("PAIRS HELD", ui.pHeld), stat("DECLINED · BOOK UNTRUSTED", ui.pDeclined));
  // The verb sits beside the state it changes, in the section's header.
  ui.pSuspend = actionBtn("paper.suspend", "SUSPEND TRADING", null, { isSwitch: true });
  ui.pResume = actionBtn("paper.resume", "RESUME TRADING", null, { cls: "primary", isSwitch: true });
  ui.sec.paper.head.append(ui.pSuspend, ui.pResume);

  ui.hMin = el("span", "cform-hint", "");
  ui.hCts = el("span", "cform-hint", "");
  ui.hNot = el("span", "cform-hint", "");
  const fMin = field("ctl-minnet", () => M.fmtCentsField(limits().min_net_ticks),
    { form: "limits", decimal: true, label: "Minimum edge to trade, cents per contract" });
  const fCts = field("ctl-maxcts", () => M.fmtContractsField(limits().max_qty_per_pair),
    { form: "limits", decimal: true, label: "Maximum contracts per pair" });
  const fNot = field("ctl-maxnot", () => M.fmtDollarsField(limits().max_notional_ticks),
    { form: "limits", decimal: true, label: "Maximum total spend, dollars" });
  const form = el("div", "cform");
  form.append(el("div", "csub", "RISK LIMITS"),
    formRow("MIN EDGE TO TRADE", fMin, "¢ / contract, after fees", ui.hMin),
    formRow("MAX PER PAIR", fCts, "contracts", ui.hCts),
    formRow("MAX TOTAL SPEND", fNot, "dollars, all pairs", ui.hNot));
  ui.limitsLine = el("div", "cdraft", "");
  const lBtns = el("div", "cbtns");
  ui.limitsRevert = plainBtn("REVERT", () => revert("limits"));
  lBtns.append(ui.limitsRevert,
    actionBtn("paper.limits", "APPLY LIMITS", () => {
      const d = limitsDraft();
      return d.canApply ? d.params : false;
    }, { cls: "primary", form: "limits", enabled: () => limitsDraft().canApply }));
  sPaper.append(pStats, form, ui.limitsLine, lBtns);

  // ---- RECORDER ----
  const sRec = section("recorder", "RECORDER",
    "Saves every raw venue message before it is parsed, so any run can be replayed exactly. "
    + "While it is off, this session has a hole no replay can fill.");
  ui.rSaved = el("div", "cstat-v", "—");
  ui.rLost = el("div", "cstat-v", "—");
  const rStats = el("div", "cstats");
  rStats.append(stat("MESSAGES SAVED THIS RUN", ui.rSaved), stat("LOST", ui.rLost));
  ui.rStop = actionBtn("recording.stop", "STOP RECORDING", null, { isSwitch: true });
  ui.rStart = actionBtn("recording.start", "START RECORDING", null, { cls: "primary", isSwitch: true });
  ui.sec.recorder.head.append(ui.rStop, ui.rStart);
  sRec.append(rStats);

  // ---- WATCH SET ----
  const sWatch = section("watch", "WATCH SET",
    "Watching a confirmed pair subscribes both legs, puts it on ARB and lets paper trade it. "
    + "Each watched pair makes every Polymarket quote a little older.");
  ui.wWatched = el("div", "cstat-v", "—");
  ui.wWatchedSub = el("div", "cstat-sub", "");
  ui.wQuoting = el("div", "cstat-v", "—");
  ui.wCycle = el("div", "cstat-v", "—");
  ui.wCycleSub = el("div", "cstat-sub", "");
  const wStats = el("div", "cstats");
  wStats.append(stat("WATCHED", ui.wWatched, ui.wWatchedSub), stat("QUOTING NOW", ui.wQuoting),
    stat("POLYMARKET REFRESH", ui.wCycle, ui.wCycleSub));
  ui.wSettled = el("div", "cwarnline");
  ui.wSettledText = el("span", "cwarnline-t", "");
  ui.wSettled.append(ui.wSettledText,
    actionBtn("pairs.track", "UNTRACK THEM", () => {
      const ids = (pairs().settled || []).slice(0, 200);
      return ids.length ? { ids, tracked: false } : false;
    }, { enabled: () => (pairs().settled || []).length > 0 }));
  const bulk = el("div", "cbulk");
  const fN = field("ctl-pairstop", () => (ctl && ctl.pairs_top != null ? ctl.pairs_top : ""),
    { form: "watch", decimal: true, cls: "cin cin-n", label: "Number of pairs to watch" });
  ui.wEst = el("span", "cform-hint", "");
  bulk.append(el("span", "cbulk-t", "WATCH"), fN, el("span", "cbulk-t", "PAIRS, ONE PER EVENT FIRST"), ui.wEst);
  const wBtns = el("div", "cbtns");
  wBtns.append(
    actionBtn("pairs.top", "SET WATCH SET", () => {
      const p = M.parseWatchN($("ctl-pairstop").value);
      return p.error ? false : { n: p.n };
    }, { cls: "primary", form: "watch", enabled: () => !M.parseWatchN($("ctl-pairstop").value).error }),
    plainBtn("PICK BY HAND ON PAIRS →", () => navigate("/pairs")),
    el("span", "cbtns-gap"),
    actionBtn("pairs.top", "WATCH NOTHING", () => ({ n: 0 }), { enabled: () => (pairs().tracked || 0) > 0 }));
  sWatch.append(wStats, ui.wSettled, el("div", "csub", "REPLACE THE WHOLE WATCH SET"), bulk, wBtns);

  // ---- UNIVERSE (two venues) ----
  const venue = (id, title, blurb, fieldId, action, paramKey, venueKey, rows) => {
    const s = section(id, title, blurb);
    const countLine = el("div", "ccount", "—");
    const ta = field(fieldId, () => ((venueKey === "kalshi" ? kalshi() : polymarket()).base || []).join("\n"),
      { multiline: true, rows, cls: "clist", form: id, label: title + ", base list, one per line" });
    const draft = el("div", "cdraft", "");
    const btns = el("div", "cbtns");
    btns.append(plainBtn("REVERT", () => revert(id)),
      actionBtn(action, "APPLY", () => {
        const d = venueDraft(id);
        return d.canApply ? { [paramKey]: d.items } : false;
      }, { cls: "primary", form: id, enabled: () => venueDraft(id).canApply }));
    s.append(countLine, ta, draft, btns);
    ui[id] = { countLine, ta, draft, revert: btns.firstChild };
    return s;
  };
  const sK = venue("kalshi", "KALSHI MARKETS",
    "Markets streamed besides the watched pairs' legs. Applying reconnects the socket: a few seconds "
    + "without Kalshi prices while every book re-snapshots.",
    "ctl-ktickers", "universe.kalshi", "tickers", "kalshi", 7);
  const sP = venue("polymarket", "POLYMARKET US MARKETS",
    "Markets polled besides the watched pairs' legs. No reconnect — but every market here makes "
    + "every Polymarket quote older.",
    "ctl-pmslugs", "universe.polymarket", "slugs", "polymarket_us", 7);

  // ---- JOBS ----
  // In the rail, directly above the output pane: you run a job and read what
  // it printed without your eye leaving the column.
  const sJobs = section("jobs", "JOBS", null);
  const jt = el("div", "cjobs");
  ui.jobRows = {};
  for (const [name, action, label, what] of JOBS) {
    const r = el("div", "cjob");
    const nameEl = el("div", "cjob-name", label);
    const whatEl = el("div", "cjob-what", what);
    const status = el("button", "cjob-status", "—");
    status.type = "button";
    status.title = "SHOW THIS JOB'S OUTPUT";
    status.addEventListener("click", (e) => {
      const j = M.latestJobs((ctl && ctl.jobs) || [])[name];
      if (j) selectJob(j.job_id);
      if (e.detail > 0) status.blur();
    });
    const bar = el("div", "cjob-bar");
    const fill = el("i");
    bar.append(fill);
    const act = el("div", "cjob-act");
    let extra = null;
    if (name === "replay") {
      extra = field("ctl-runid", null, { select: true, cls: "cin csel", label: "Run to replay" });
    }
    const runBtn = actionBtn(action, "RUN", () => {
      if (name !== "replay") return null;
      const id = String($("ctl-runid").value || "").trim();
      return id ? { run_id: id } : {};
    });
    const cancel = plainBtn("CANCEL", () => {
      const j = M.latestJobs((ctl && ctl.jobs) || [])[name];
      if (j) run("jobs.cancel", { job_id: j.job_id });
    });
    cancel.hidden = true;
    act.append(runBtn, cancel);
    const info = el("div", "cjob-info");
    info.append(nameEl, whatEl);
    if (extra) info.append(extra);
    const mid = el("div", "cjob-mid");
    mid.append(status, bar);
    r.append(info, mid, act);
    jt.append(r);
    ui.jobRows[name] = { status, bar, fill, runBtn, cancel, select: extra };
  }
  sJobs.append(jt);

  // ---- rail ----
  const rail = el("aside", "crail");
  const out = el("div", "crail-box crail-out");
  ui.outHead = el("div", "crail-head", "JOB OUTPUT");
  ui.outBody = el("div", "cout");
  ui.outBody.tabIndex = -1;
  out.append(ui.outHead, ui.outBody);
  const aud = el("div", "crail-box crail-audit");
  ui.auditHead = el("div", "crail-head", "AUDIT TRAIL");
  ui.audit = el("div", "caudit");
  aud.append(ui.auditHead, ui.audit);
  sJobs.classList.add("crail-jobs");
  rail.append(sJobs, out, aud);

  // Most-used first: what you operate (paper, the watch set), then what
  // feeds it (the two universes), then the recorder as a strip. Jobs live in
  // the rail, with their output and the audit trail: work, and its record.
  const grid = el("div", "cgrid");
  sRec.classList.add("csec-strip");
  grid.append(sPaper, sWatch, sK, sP, sRec);
  const main = el("div", "cmain");
  main.append(grid);
  const layout = el("div", "clayout");
  layout.append(main, rail);

  const foot = el("div", "des-foot cfoot",
    "TAB FIELDS · ESC IN A FIELD REVERTS IT · EVERY ACTION IS AUDITED · NO ORDERS ARE EVER PLACED");
  root.append(head, ui.roBand, layout, foot);
  built = true;
}

// ---------- drafts ----------

function limitsDraft() {
  return M.limitsDraft(limits(), {
    minEdge: $("ctl-minnet").value, maxPair: $("ctl-maxcts").value, maxSpend: $("ctl-maxnot").value,
  }, paper().notional_ticks);
}

function venueDraft(id) {
  const isK = id === "kalshi";
  return M.listDraft((isK ? kalshi() : polymarket()).base || [], ui[id].ta.value,
    { venue: isK ? "kalshi" : "polymarket_us", maxItems: isK ? 200 : 100, allowEmpty: !isK });
}

// ---------- the in-section slot: confirm box, or the last receipt ----------

function renderSlot(id) {
  const s = ui.sec[id];
  const a = armed && armed.section === id ? armed : null;
  const r = receipts[id] || null;
  const key = a ? "A|" + a.action + "|" + a.effect : r ? "R|" + r.kind + "|" + r.action + "|" + r.message + "|" + r.at : "";
  if (key !== s.slotKey) {
    s.slotKey = key;
    s.slot.hidden = !key;
    s.ttl = null;
    if (a) {
      const box = el("div", "cconfirm");
      box.setAttribute("role", "alertdialog");
      box.setAttribute("aria-label", "Confirm " + a.action);
      box.append(el("div", "cconfirm-k", a.token ? "CONFIRM · THIS IS WHAT WILL BE RECORDED" : "THIS IS WHAT IT WILL DO"));
      // Verbatim from the server: this sentence is the audit row.
      box.append(el("div", "cconfirm-eff", a.effect));
      const btns = el("div", "cbtns");
      const no = plainBtn("CANCEL", () => { armed = null; render(); });
      const go = plainBtn("CONFIRM", () => run(a.action, a.params, a.form), "primary");
      s.ttl = el("span", "cbtn-note num", "");
      btns.append(no, go, s.ttl);
      box.append(btns);
      s.slot.replaceChildren(box);
      // Keyboard activation lands on the safe button; a pointer press leaves
      // focus at ARB>, as everywhere else on this page.
      if (document.activeElement && document.activeElement !== document.body
        && s.root.contains(document.activeElement)) no.focus();
    } else if (r) {
      const box = el("div", "creceipt cr-" + r.kind);
      box.append(el("span", "creceipt-tag", r.kind === "fail" ? "FAILED" : r.kind === "done" ? "DONE" : "NO CHANGE"),
        el("span", "creceipt-msg", r.message),
        plainBtn("×", () => { delete receipts[id]; render(); }, "tiny"));
      s.slot.replaceChildren(box);
    } else {
      s.slot.replaceChildren();
    }
  }
  if (a && s.ttl) setTxt(s.ttl, "EXPIRES IN " + Math.max(0, Math.round((a.until - Date.now()) / 1000)) + "s");
}

// ---------- jobs + rail ----------

function selectJob(id) {
  selectedJob = id;
  jobPicked = true;
  if (!jobLogs.has(id)) loadJobLog(id);
  render();
}

/** One line of job output. `arb doctor` prints "[ok  ] name    detail" in
    columns padded for an 80-column terminal; in a 420px rail that wraps into
    ragged gaps, so those lines are laid out as real columns instead. Anything
    else is shown exactly as printed. */
function outputLine(ln) {
  const t = String(ln);
  const m = /^\[(\w+)\s*\]\s+(\S.*?)\s{2,}(\S.*)$/.exec(t);
  if (!m) {
    const cls = /error|traceback|failed/i.test(t) ? " bad" : "";
    return el("div", "cout-line" + cls, t);
  }
  const tag = m[1].toLowerCase();
  const row = el("div", "cout-check ck-" + (tag === "ok" ? "ok" : tag === "warn" ? "warn" : "bad"));
  row.append(el("span", "ck-tag", tag.toUpperCase()), el("span", "ck-name", m[2]), el("span", "ck-detail", m[3]));
  return row;
}

function renderJobs() {
  const jobs = (ctl && ctl.jobs) || [];
  const latest = M.latestJobs(jobs);
  let anyRunning = false;
  for (const [name] of JOBS) {
    const row = ui.jobRows[name];
    const j = latest[name] || null;
    const running = !!j && j.status === "running";
    anyRunning = anyRunning || running;
    row.cancel.hidden = !running;
    row.runBtn.hidden = running;
    if (row.select) row.select.hidden = running;
    row.bar.hidden = !running;
    if (!j) {
      setTxt(row.status, "NOT RUN YET");
      row.status.className = "cjob-status st-none";
      row.status.disabled = true;
      continue;
    }
    row.status.disabled = false;
    const p = M.jobProgress(j);
    const when = j.finished_ts_ns ? fmtAgo(Date.now() - j.finished_ts_ns / 1e6) + " AGO" : (j.elapsed_s || 0).toFixed(0) + "s";
    setTxt(row.status, running
      ? "RUNNING · " + (p.where || "starting") + " · " + when
      : String(j.status).toUpperCase() + " · " + when + (j.error ? " · " + String(j.error).slice(0, 60) : ""));
    row.status.className = "cjob-status st-" + j.status + (j.job_id === selectedJob ? " sel" : "");
    row.fill.style.width = p.frac == null ? "100%" : (p.frac * 100).toFixed(1) + "%";
    row.bar.classList.toggle("indet", p.frac == null);
  }
  // replay's run picker: the recorded runs, newest first
  const sel = ui.jobRows.replay.select;
  const runs = (state.status && state.status.database && state.status.database.runs) || [];
  const key = runs.map((r) => r.run_id).join(",");
  if (sel && sel._key !== key && document.activeElement !== sel) {
    sel._key = key;
    const keep = sel.value;
    sel.replaceChildren(new Option("LATEST RUN", ""), ...runs.map((r) =>
      new Option(r.run_id + " · " + nf.format(r.count) + " msgs", r.run_id)));
    sel.value = keep;
  }
  const s = ui.sec.jobs;
  setLevel(s.root, anyRunning ? "ok" : "off");
  setTxt(s.state, anyRunning ? "● RUNNING" : "IDLE");

  // the output pane follows the newest job until the operator picks one
  if (!jobPicked || !jobs.some((j) => j.job_id === selectedJob)) {
    const newest = jobs.slice().sort((a, b) => (b.started_ts_ns || 0) - (a.started_ts_ns || 0))[0];
    selectedJob = newest ? newest.job_id : null;
  }
  const cur = jobs.find((j) => j.job_id === selectedJob) || null;
  if (!cur) {
    setTxt(ui.outHead, "JOB OUTPUT");
    if (ui.outBody._key !== "empty") {
      ui.outBody._key = "empty";
      ui.outBody.replaceChildren(el("div", "cout-empty", "Run a job — DOCTOR is a good first one — and its output appears here."));
    }
    return;
  }
  setTxt(ui.outHead, "JOB OUTPUT · " + String(cur.name).toUpperCase() + " · " + String(cur.status).toUpperCase()
    + " · " + (cur.elapsed_s || 0).toFixed(1) + "s");
  if (cur.status !== "running" && !jobLogs.has(cur.job_id) && !cur._asked) {
    cur._asked = true;
    loadJobLog(cur.job_id);
  }
  const lines = (cur.status === "running" ? jobTails.get(cur.job_id) : null) || jobLogs.get(cur.job_id)
    || jobTails.get(cur.job_id) || [];
  const okey = cur.job_id + "|" + lines.length + "|" + (lines.length ? lines[lines.length - 1] : "") + "|" + cur.status;
  if (ui.outBody._key !== okey) {
    ui.outBody._key = okey;
    const stick = ui.outBody.scrollTop + ui.outBody.clientHeight >= ui.outBody.scrollHeight - 8;
    const nodes = lines.map(outputLine);
    if (!nodes.length) nodes.push(el("div", "cout-empty", cur.status === "running" ? "Running — no output yet." : "No output."));
    if (cur.error) nodes.push(el("div", "cout-line bad", String(cur.error)));
    ui.outBody.replaceChildren(...nodes);
    if (stick) ui.outBody.scrollTop = ui.outBody.scrollHeight;
  }
}

function renderAudit() {
  setTxt(ui.auditHead, "AUDIT TRAIL · LAST " + audit.length);
  const key = audit.map((a) => a.id).join(",") + "|" + Math.floor(Date.now() / 10000);
  if (ui.audit._key === key) return;
  ui.audit._key = key;
  if (!audit.length) {
    ui.audit.replaceChildren(el("div", "cout-empty",
      ctl && ctl.read_only ? "Read-only server: nothing is recorded." : "Nothing yet. Every action taken here is recorded, with the sentence you were shown."));
    return;
  }
  ui.audit.replaceChildren(...audit.map((a) => {
    const r = el("div", "caud ca-" + (a.result || "ok"));
    const top = el("div", "caud-top");
    top.append(el("span", "caud-res", String(a.result || "").toUpperCase()),
      el("span", "caud-act", String(a.action || "")),
      el("span", "caud-when", (a.ts_ns ? fmtAgo(Date.now() - a.ts_ns / 1e6) : "—") + " AGO"));
    r.append(top, el("div", "caud-eff", String(a.effect || "")));
    if (a.error) r.append(el("div", "caud-err", String(a.error)));
    return r;
  }));
}

// ---------- render: writes values, never rebuilds a field ----------

function hint(node, f, idle) {
  const touched = f.error || f.changed;
  setTxt(node, f.error ? f.error : f.changed ? "was " + f.was : idle);
  node.className = "cform-hint" + (f.error ? " bad" : f.changed ? " chg" : "");
  return touched;
}

function render() {
  if (!built || !mounted) return;
  const ro = !!(ctl && ctl.read_only);
  const now = Date.now();

  setTxt(ui.headStat, ctl ? nf.format((ctl.actions || []).length) + " ACTIONS" : "AWAITING CONTROL STATE");
  ui.chipRo.hidden = !ro;
  ui.roBand.hidden = !ro;
  const bind = ctl && ctl.bind;
  ui.chipBind.hidden = !(bind && !bind.loopback);
  if (bind && !bind.loopback) {
    setTxt(ui.chipBind, "BOUND " + bind.host + " · NOT LOOPBACK");
    ui.chipBind.title = "THIS SERVER LISTENS ON " + bind.host + ", NOT 127.0.0.1: ANYONE WHO CAN REACH THE PORT CAN "
      + "DRIVE THIS PAGE. IT WAS STARTED THAT WAY ON PURPOSE (ARB_ALLOW_REMOTE_BIND=1). DOCKER COMPOSE PUBLISHES THE "
      + "PORT ON 127.0.0.1 ONLY.";
  }

  // fields take the server's value unless you are in them or have edited them
  for (const f of fields) syncField(f);

  // ---- paper ----
  const p = paper();
  const sp = ui.sec.paper;
  const trading = !!p.attached && !p.suspended;
  setLevel(sp.root, !p.attached ? "wait" : trading ? "ok" : "off");
  setTxt(sp.state, !ctl ? "—" : !p.attached ? "NOT RUNNING IN THIS PROCESS" : trading ? "● TRADING"
    : "SUSPENDED" + (p.skipped_suspended ? " · " + nf.format(p.skipped_suspended) + " EDGES PASSED UP" : ""));
  const cap = limits().max_notional_ticks || 0;
  const used = cap > 0 ? Math.min(1, (p.notional_ticks || 0) / cap) : 0;
  setTxt(ui.pDeployed, M.usd(p.notional_ticks || 0) + " of " + M.usd(cap));
  ui.pMeterFill.style.width = (used * 100).toFixed(1) + "%";
  ui.pMeter.classList.toggle("full", used >= 0.95);
  setTxt(ui.pTrades, count(p.trades));
  setTxt(ui.pHeld, count(p.positions));
  setTxt(ui.pDeclined, count(p.skipped_invalid));
  ui.pSuspend.hidden = !trading;
  ui.pResume.hidden = trading || !p.attached;
  const d = limitsDraft();
  hint(ui.hMin, d.fields.minEdge, "= " + count(limits().min_net_ticks) + " ticks");
  hint(ui.hCts, d.fields.maxPair, "");
  hint(ui.hNot, d.fields.maxSpend, "");
  // The cap can legitimately sit below what is deployed (it is the panic
  // button). With nothing being edited, that standing fact is what the line says.
  const capped = d.valid && !d.changes.length && (paper().notional_ticks || 0) > (limits().max_notional_ticks || 0);
  setTxt(ui.limitsLine, !d.valid ? "Fix the field marked in red."
    : d.changes.length ? d.changes.join("  ·  ") + (d.warnings.length ? "  —  " + d.warnings.join(" ") : "")
      : capped ? "The spend cap is below what is already deployed, so no new trades are being taken. Raise it to trade again."
        : "No changes. Edit a limit to enable APPLY.");
  ui.limitsLine.className = "cdraft" + (!d.valid ? " bad" : d.changes.length ? " chg" : capped ? " warn" : "");
  ui.limitsRevert.disabled = !fields.some((f) => f.form === "limits" && f.el.dataset.dirty);

  // ---- recorder ----
  const on = !!(ctl && ctl.recording);
  const sr = ui.sec.recorder;
  setLevel(sr.root, !ctl ? "wait" : on ? "ok" : "warn");
  setTxt(sr.state, !ctl ? "—" : on ? "● RECORDING" : "OFF · NOT SAVING");
  const rec = state.stats && state.stats.recorder;
  setTxt(ui.rSaved, rec ? nf.format(rec.enqueued) : on ? "—" : "0");
  setTxt(ui.rLost, rec ? nf.format(rec.dropped) : "—");
  ui.rLost.classList.toggle("bad", !!rec && rec.dropped > 0);
  ui.rStop.hidden = !on;
  ui.rStart.hidden = on;

  // ---- watch set ----
  const pr = pairs();
  const pl = poll();
  const sw = ui.sec.watch;
  const live = pr.live != null ? pr.live : (ctl ? ctl.tracked_pairs : null);
  const settled = (pr.settled || []).length;
  setLevel(sw.root, !ctl ? "wait" : !live ? "off" : settled ? "warn" : "ok");
  setTxt(sw.state, !ctl ? "—" : !live ? "NOTHING WATCHED" : "● WATCHING " + count(live) + (live === 1 ? " PAIR" : " PAIRS"));
  setTxt(ui.wWatched, count(pr.tracked));
  setTxt(ui.wWatchedSub, "of " + count(pr.confirmed) + " confirmed");
  setTxt(ui.wQuoting, count(live));
  setTxt(ui.wCycle, pl.cycle_s == null ? "—" : Math.round(pl.cycle_s) + "s per book");
  setTxt(ui.wCycleSub, pl.cycle_s == null ? "" : count(pl.targets) + " markets polled · +" + Number(pl.per_pair_s).toFixed(1) + "s per extra pair");
  ui.wCycle.classList.toggle("warn", pl.cycle_s != null && pl.cycle_s > 60);
  ui.wSettled.hidden = !settled;
  setTxt(ui.wSettledText, settled + (settled === 1 ? " watched pair is a market" : " watched pairs are markets")
    + " that a venue says " + (settled === 1 ? "has" : "have") + " settled. "
    + (settled === 1 ? "It quotes" : "They quote") + " nothing and only clutter the set.");
  const wn = M.parseWatchN($("ctl-pairstop").value);
  const est = wn.error ? null : M.watchEstimate(wn.n, pr, polymarket());
  setTxt(ui.wEst, $("ctl-pairstop").value === "" ? "" : wn.error ? wn.error
    : est ? "≈ " + Math.round(est.cycleS) + "s per book · " + est.targets + " markets polled" : "");
  ui.wEst.className = "cform-hint" + (wn.error && $("ctl-pairstop").value !== "" ? " bad" : est && est.cycleS > 60 ? " chg" : "");

  // ---- universes ----
  for (const id of ["kalshi", "polymarket"]) {
    const isK = id === "kalshi";
    const v = isK ? kalshi() : polymarket();
    const u = ui[id];
    const s = ui.sec[id];
    const base = (v.base || []).length;
    const legs = (v.pairs || []).length;
    const total = ((isK ? v.tickers : v.slugs) || []).length;
    setLevel(s.root, !ctl ? "wait" : v.attached ? "ok" : "off");
    setTxt(s.state, !ctl ? "—" : !v.attached ? "FEED NOT ATTACHED"
      : (isK ? "● STREAMING " : "● POLLING ") + count(total));
    setTxt(u.countLine, count(base) + " here  +  " + count(legs) + " legs of watched pairs  =  " + count(total)
      + (isK ? " streamed" : " polled"));
    const dv = venueDraft(id);
    const bits = [];
    if (dv.added.length) bits.push("+" + dv.added.length + " added");
    if (dv.removed.length) bits.push("−" + dv.removed.length + " removed");
    if (dv.dupes) bits.push(dv.dupes + " duplicate" + (dv.dupes === 1 ? "" : "s") + " ignored");
    let line = dv.error ? dv.error : dv.dirty ? bits.join("  ·  ") + "  →  " + dv.items.length + " here" : "No changes. One market per line.";
    if (!isK && dv.dirty && !dv.error && pl.interval_s) {
      const after = dv.items.length + legs;
      line += "  ·  ≈ " + Math.round(after * pl.interval_s) + "s per book";
    }
    setTxt(u.draft, line);
    u.draft.className = "cdraft" + (dv.error ? " bad" : dv.dirty ? " chg" : "");
    u.revert.disabled = !u.ta.dataset.dirty;
  }

  // ---- buttons ----
  for (const b of buttons) {
    const s = spec(b.action);
    const locked = b.isSwitch && (lockUntil[b.section] || 0) > now;
    b.el.disabled = !s || (ro && s.mutates) || busy.has(b.action) || locked
      || (armed && armed.section === b.section) || (b.enabled ? !b.enabled() : false);
    b.el.title = actionTitle(b.action);
    b.el.classList.toggle("g-g3", !!s && s.grade === "G3");
    b.el.classList.toggle("busy", busy.has(b.action));
  }

  for (const id of Object.keys(ui.sec)) renderSlot(id);
  renderJobs();
  renderAudit();
}

function tick() {
  if (!mounted) return;
  const now = Date.now();
  if (armed && now > armed.until) armed = null;
  for (const [id, r] of Object.entries(receipts)) {
    if (r.kind === "done" && now - r.at > RESULT_MS) delete receipts[id];
  }
  render();
}

// The control frame is the truth; a page that guessed would drift from it.
onMessage("control", (m) => {
  if (m && m.control) {
    ctl = m.control;
    schedule("control");
  }
});
onMessage("job", (m) => {
  if (m && m.job && m.job.job_id) {
    if (Array.isArray(m.tail)) jobTails.set(m.job.job_id, m.tail.map(String));
    // Progress between control frames: fold the frame into the list we hold.
    if (ctl && Array.isArray(ctl.jobs)) {
      const i = ctl.jobs.findIndex((j) => j.job_id === m.job.job_id);
      if (i >= 0) ctl.jobs[i] = m.job; else ctl.jobs.unshift(m.job);
    }
    if (m.job.status !== "running") jobLogs.delete(m.job.job_id);   // refetch the full log once
    while (jobTails.size > 20) jobTails.delete(jobTails.keys().next().value);
  }
  schedule("control");
});

registerRenderer("control", render);

export default {
  id: "control",
  path: "/control",
  title: "CONTROL",
  nav: true,
  root: ROOT,
  regions: ["ctl-minnet", "ctl-maxcts", "ctl-maxnot", "ctl-pairstop", "ctl-ktickers", "ctl-pmslugs", "ctl-runid"],

  mount() {
    mounted = true;
    build();
    if (state.control) ctl = state.control;
    loadControl();
    loadAudit();
    if (!tickTimer) tickTimer = setInterval(tick, TICK_MS);
    render();
  },

  unmount() {
    mounted = false;
    clearInterval(tickTimer);
    tickTimer = 0;
    armed = null;                          // an armed action must not survive leaving the page
    for (const k of Object.keys(receipts)) delete receipts[k];
    clearDirty();                          // nor a half-typed limit
  },

  render,

  onKey(e, scope) {
    // No letter bindings: every control here writes something, and a bare
    // letter must never reach one. Buttons and Tab are the way in.
    void e;
    void scope;
    return false;
  },

  keyHints(scope) {
    if (scope === SCOPE.TEXT) return [{ k: "ESC", d: "REVERT FIELD" }];
    return [{ k: "TAB", d: "FIELDS" }];
  },
};
