/* pages/control.js — the control plane: every runtime toggle and job.

   This is the page that can break things, so it is built to make that
   obvious. Four rules it follows:

   1. NOTHING ABOUT AN ACTION IS HARDCODED HERE. The action list, each one's
      consequence grade and whether it needs confirming all come from
      GET /api/control's `actions` descriptors. A hardcoded copy would drift
      from the server and make a button lie about what it does, which is the
      defect class the last two milestones were spent removing.
   2. NO OPTIMISTIC STATE. A toggle never flips until the server says it did.
      Truth arrives as a {"t":"control"} frame, so two open tabs cannot
      disagree about whether paper trading is on.
   3. THE SERVER OWNS THE CONFIRM COPY. A 428 carries the sentence that will
      be written to the audit row; we render it verbatim. Inventing our own
      wording here would mean the screen and the permanent record disagree.
   4. THE DOM IS BUILT ONCE AND UPDATED IN PLACE. This page repaints on every
      control frame and on a 1 s tick; an earlier version rebuilt its cards
      each time, which destroyed every <input> mid-keystroke and made the
      fields literally uneditable. So: build() creates the nodes, render()
      only writes values, and a field you are editing is never written to.
   5. A NO-OP IS NOT A SUCCESS. Every result carries `changed`, and an action
      that moved zero rows says so — in the toast and in a band that stays up
      until the next action. Pressing RELOAD PAIRS ten times and being told
      ten times that it worked, while the watch set never moved, is the
      specific failure this page is now built to make impossible. */

import { $, el } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { nf, fmtAgo } from "../core/format.js";
import { onMessage } from "../core/ws.js";
import { SCOPE } from "../core/keys.js";
import * as cmd from "../core/cmd.js";

const ROOT = "control-page";
const LOG_LIMIT = 40;
const TICK_MS = 1000;
// How long the receipt for an action that DID change something stays up. A
// no-op has no expiry: it is the message the last milestone was lost for
// want of, so it holds until the next action replaces it.
const RESULT_MS = 30000;

// Consequence grades, from docs/decisions.md. The LABEL is ours; the grade
// itself is the server's and arrives per action.
const GRADE_NOTE = {
  G0: "read-only",
  G1: "navigation",
  G2: "changes this run",
  G3: "writes many rows",
  G4: "irreversible",
};

let built = false;
let mounted = false;
let tickTimer = 0;
let ctl = null;          // last control payload
let audit = [];
let armed = null;        // {action, params, token, effect, until}
let result = null;       // {action, message, changed, at} — the last receipt
const busy = new Set();  // actions with a POST in flight

// Nodes built once and written to in place.
const ui = {};
const buttons = [];      // {el, action}
const fields = [];       // {el, read: () => serverValue}

// ---------- server calls ----------

/** POST one action. Returns {ok, status, body}; never throws. */
async function post(action, params, confirm) {
  const payload = {};
  if (params) payload.params = params;
  if (confirm) payload.confirm = confirm;
  try {
    const r = await fetch("/api/control/" + encodeURIComponent(action), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    let body = null;
    try { body = await r.json(); } catch (err) { body = null; }
    return { ok: r.ok, status: r.status, body: body || {} };
  } catch (err) {
    return { ok: false, status: 0, body: { error: String(err && err.message ? err.message : err) } };
  }
}

/** Run an action, handling the arm-then-confirm handshake. */
async function run(action, params) {
  if (busy.has(action)) return;
  busy.add(action);
  render();
  const useToken = armed && armed.action === action ? armed.token : null;
  const res = await post(action, params, useToken);
  busy.delete(action);
  if (res.status === 428 && res.body.confirm_required) {
    // Armed. The sentence is the server's and is what the audit row records.
    armed = {
      action,
      params: params || null,
      token: res.body.confirm_token,
      effect: res.body.effect,
      until: Date.now() + (res.body.expires_in_s || 0) * 1000,
    };
    cmd.message("CONFIRM REQUIRED", "warn");
  } else if (res.ok) {
    armed = null;
    // The edit landed, so the fields may take the server's value again.
    clearDirty();
    // `changed` is the server's answer to "did any row move", and it is NOT
    // the same question as "did the request succeed". A control that ran
    // cleanly and changed nothing is reported as exactly that: the sentence
    // the server wrote, in the no-change colour, in a band that stays up.
    const changed = res.body.changed !== false;
    const said = String(res.body.message || res.body.effect || action);
    result = { action, changed, message: said, at: Date.now() };
    cmd.message(
      (changed ? said : "NO CHANGE · " + said).toUpperCase().slice(0, 60),
      changed ? "ok" : "warn");
    loadAudit();
  } else {
    armed = null;
    result = {
      action,
      changed: false,
      failed: true,
      message: String(res.body.error || "HTTP " + res.status),
      at: Date.now(),
    };
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

// ---------- descriptors ----------

function spec(action) {
  const list = (ctl && ctl.actions) || [];
  return list.find((a) => a.action === action) || null;
}

function actionTitle(action) {
  const s = spec(action);
  if (!s) return action + " — NOT AVAILABLE IN THIS PROCESS";
  const grade = s.grade + (GRADE_NOTE[s.grade] ? " · " + GRADE_NOTE[s.grade].toUpperCase() : "");
  return [s.summary.toUpperCase(), grade, s.confirm ? "ASKS FOR CONFIRMATION" : null]
    .filter(Boolean)
    .join(" · ");
}

// ---------- element factories (build time only) ----------

function actionBtn(action, label, paramsFn, cls) {
  const b = el("button", "cbtn " + (cls || ""), label);
  b.type = "button";
  b.dataset.action = action;
  b.addEventListener("click", (e) => {
    run(action, paramsFn ? paramsFn() : null);
    if (e.detail > 0) b.blur();   // pointer click: typing goes back to ARB>
  });
  buttons.push({ el: b, action });
  return b;
}

function row(label, ...nodes) {
  const r = el("div", "kv");
  r.append(el("span", "k", label));
  const v = el("span", "v");
  v.append(...nodes);
  r.append(v);
  return r;
}

/** A field whose value comes from the server until you touch it.

    `read` pulls the current server value. Once you type, the field is marked
    dirty and render() leaves it alone — otherwise the next control frame
    (they arrive every time anything changes) would overwrite you mid-edit. */
function field(id, read, opts) {
  const o = opts || {};
  const e = el(o.multiline ? "textarea" : "input", o.cls || "cnum");
  e.id = id;
  if (!o.multiline) e.type = "text";
  if (o.rows) e.rows = o.rows;
  if (o.width) e.style.width = o.width;
  if (o.placeholder) e.placeholder = o.placeholder;
  e.autocomplete = "off";
  e.spellcheck = false;
  if (o.numeric) e.setAttribute("inputmode", "numeric");
  e.addEventListener("input", () => { e.dataset.dirty = "1"; });
  // Esc gives the field back to the server's value and hands focus home.
  e.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    ev.stopPropagation();
    delete e.dataset.dirty;
    syncField({ el: e, read });
    e.blur();
  });
  fields.push({ el: e, read });
  return e;
}

function syncField(f) {
  const e = f.el;
  // Never write into a field the operator is in, or one they have edited.
  if (document.activeElement === e || e.dataset.dirty) return;
  const v = f.read();
  const next = v == null ? "" : String(v);
  if (e.value !== next) e.value = next;
}

function clearDirty() {
  for (const f of fields) delete f.el.dataset.dirty;
}

function numOf(id, fallback) {
  const e = $(id);
  if (!e) return fallback;
  const n = Number(String(e.value).trim().replace(/,/g, ""));
  return Number.isFinite(n) ? Math.round(n) : fallback;
}

function linesOf(id) {
  const e = $(id);
  if (!e) return [];
  return String(e.value).split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
}

function card(title, ...nodes) {
  const c = el("div", "sys-block cblock");
  c.append(el("div", "sys-title", title));
  c.append(...nodes);
  return c;
}

function note(text) {
  return el("div", "cnote quiet-line", text);
}

// ---------- reading the payload ----------

const paper = () => (ctl && ctl.paper) || {};
const limits = () => paper().limits || {};
const universe = () => (ctl && ctl.universe) || {};
const kalshi = () => universe().kalshi || {};
const polymarket = () => universe().polymarket_us || {};
const pairs = () => (ctl && ctl.pairs) || {};
const poll = () => pairs().poll || {};

/** A count the server has not read yet is UNKNOWN, not zero.

    `control.pairs.confirmed` is null until the background refresher has
    answered; rendering that as 0 would say "there is nothing to watch", which
    is the opposite of what it means. */
function count(n) {
  return n == null ? "—" : nf.format(n);
}

function secs(n) {
  return n == null ? "—" : Number(n).toFixed(1) + "s";
}

// ---------- build ----------

function build() {
  const root = $(ROOT);
  if (!root || built) return;

  const head = el("div", "panel-head");
  head.append(el("span", "title", "CONTROL"));
  ui.headStat = el("span", "head-stat", "—");
  head.append(ui.headStat);

  ui.note = el("div", "cbanner");
  ui.body = el("div", "cbody");

  // --- recorder ---
  ui.recState = el("span", "st-dim", "—");
  const rec = card("RECORDER",
    row("STATE", ui.recState),
    (ui.recBtns = el("div", "cbtns")),
    note("OFF LEAVES A HOLE A REPLAY READS STRAIGHT ACROSS. THE AUDIT ROW BELOW "
      + "IS THE ONLY RECORD THAT THE GAP WAS DELIBERATE."));
  ui.recBtns.append(actionBtn("recording.start", "START"), actionBtn("recording.stop", "STOP"));

  // --- paper ---
  ui.paperMissing = note("NO PAPER TRADER ON THIS RUN");
  ui.paperState = el("span", "st-dim", "—");
  ui.paperSpent = el("span", "num", "—");
  ui.paperSkipped = row("SKIPPED WHILE SUSPENDED", (ui.paperSkippedVal = el("span", "num", "—")));
  const pbtns = el("div", "cbtns");
  pbtns.append(actionBtn("paper.suspend", "SUSPEND"), actionBtn("paper.resume", "RESUME"));
  const applyBtns = el("div", "cbtns");
  applyBtns.append(actionBtn("paper.limits", "APPLY LIMITS", () => ({
    min_net_ticks: numOf("ctl-minnet", limits().min_net_ticks),
    max_cts_per_pair: numOf("ctl-maxcts", Math.round((limits().max_qty_per_pair || 0) / 10000)),
    max_notional_ticks: numOf("ctl-maxnot",
      Math.round((limits().max_notional_ticks || 0) / 10000)) * 10000,
  }), "primary"));
  const paperCard = card("PAPER TRADING",
    ui.paperMissing,
    row("STATE", ui.paperState),
    row("SPENT", ui.paperSpent),
    ui.paperSkipped,
    pbtns,
    note("SUSPEND KEEPS POSITIONS AND THE SPEND ALREADY COMMITTED — IT IS NOT A RESET."),
    el("div", "sys-title cs2", "RISK LIMITS"),
    row("MIN NET / CT (TICKS)",
      field("ctl-minnet", () => limits().min_net_ticks, { numeric: true, width: "7ch" })),
    row("MAX CTS / PAIR",
      field("ctl-maxcts", () => Math.round((limits().max_qty_per_pair || 0) / 10000),
        { numeric: true, width: "7ch" })),
    row("MAX NOTIONAL ($)",
      field("ctl-maxnot", () => Math.round((limits().max_notional_ticks || 0) / 10000),
        { numeric: true, width: "7ch" })),
    applyBtns);

  // --- the watch set ---
  //
  // Its own card, above the universe, because the gap it shows is what an
  // afternoon was lost to: 46 pairs confirmed, 10 watched, and nothing on
  // screen that named the difference. Three numbers, in this order — how many
  // of the confirmed pairs are watched, how many are actually being quoted,
  // and what the next one costs every other Polymarket book.
  ui.trackGap = el("span", "cbig", "—");
  ui.live = el("span", "num", "—");
  ui.watchCycle = el("span", "num", "—");
  ui.perPair = el("span", "num", "—");
  ui.gapNote = note("");
  const ptBtns = el("div", "cbtns");
  ptBtns.append(
    actionBtn("pairs.top", "SET WATCH SET",
      () => ({ n: numOf("ctl-pairstop", (ctl && ctl.pairs_top) || 0) }), "primary"),
    actionBtn("pairs.top", "WATCH NOTHING", () => ({ n: 0 })));
  const watchCard = card("WATCH SET",
    row("TRACKED", ui.trackGap),
    row("QUOTED RIGHT NOW", ui.live),
    row("POLYMARKET CYCLE", ui.watchCycle),
    row("EACH WATCHED PAIR", ui.perPair),
    ui.gapNote,
    el("div", "sys-title cs2", "BULK SET: THE TOP N CONFIRMED BY SCORE"),
    row("N", field("ctl-pairstop", () => (ctl && ctl.pairs_top),
      { numeric: true, width: "7ch" })),
    ptBtns,
    note("THIS SETS THE TRACKED FLAG ON THE TOP N CONFIRMED PAIRS AND CLEARS IT ON "
      + "EVERY OTHER PAIR. IT IS A SETTER, NOT A FILTER: ANYTHING OUTSIDE THE TOP N "
      + "STOPS BEING WATCHED. N=0 WATCHES NOTHING."),
    note("SCORES TIE — A WHOLE RUN CAN SIT AT 1.000 — SO \"TOP N BY SCORE\" IS OFTEN "
      + "JUST \"THE N OLDEST ROWS\", AND A PAIR CONFIRMED TODAY CAN NEVER WIN THE TIE. "
      + "PICK PAIRS ONE BY ONE ON /PAIRS (T) WHEN THAT MATTERS, WHICH IS USUALLY."));

  // --- universe ---
  ui.kCount = el("span", "num", "—");
  ui.pCount = el("span", "num", "—");
  ui.cycle = el("span", "num", "—");
  const kBtns = el("div", "cbtns");
  kBtns.append(actionBtn("universe.kalshi", "SUBSCRIBE",
    () => ({ tickers: linesOf("ctl-ktickers") })));
  const pBtns = el("div", "cbtns");
  pBtns.append(actionBtn("universe.polymarket", "SET TARGETS",
    () => ({ slugs: linesOf("ctl-pmslugs") })));
  const uniCard = card("UNIVERSE",
    row("KALSHI SUBSCRIBED", ui.kCount),
    row("POLYMARKET POLLED", ui.pCount),
    row("POLL CYCLE", ui.cycle),
    note("THE WATCHED PAIRS' LEGS ARE PART OF BOTH SETS — SEE WATCH SET. THESE TWO "
      + "BOXES ARE THE BASE UNIVERSE THEY ARE ADDED TO."),
    el("div", "sys-title cs2", "KALSHI SUBSCRIPTION"),
    field("ctl-ktickers", () => (kalshi().tickers || []).join("\n"),
      { multiline: true, rows: 3, cls: "clist" }),
    kBtns,
    note("COSTS A RECONNECT: SECONDS OF GAP AND EVERY BOOK RESNAPSHOTS."),
    el("div", "sys-title cs2", "POLYMARKET US POLL TARGETS"),
    field("ctl-pmslugs", () => (polymarket().slugs || []).join("\n"),
      { multiline: true, rows: 3, cls: "clist" }),
    pBtns,
    note("NO RECONNECT. MORE TARGETS = A LONGER CYCLE PER BOOK; STALENESS IS RETUNED."));

  // --- jobs ---
  const jbtns = el("div", "cbtns");
  jbtns.append(
    actionBtn("jobs.doctor", "RUN DOCTOR"),
    actionBtn("jobs.propose", "PROPOSE PAIRS"),
    actionBtn("jobs.backfill", "BACKFILL SLUGS"));
  const rBtns = el("div", "cbtns");
  rBtns.append(actionBtn("jobs.replay", "REPLAY", () => {
    const id = String((($("ctl-runid") || {}).value) || "").trim();
    return id ? { run_id: id } : {};
  }));
  const jobsCard = card("JOBS",
    jbtns,
    note("PROPOSE TAKES ABOUT THREE MINUTES AND WRITES THOUSANDS OF ROWS. IT RUNS "
      + "OFF THE INGEST LOOP, SO BOOKS AND THE TAPE KEEP MOVING."),
    el("div", "sys-title cs2", "REPLAY A RECORDED RUN"),
    row("RUN", field("ctl-runid", () => "",
      { cls: "cnum crun", placeholder: "RUN ID (BLANK = LATEST)" })),
    rBtns,
    note("RUNS AS A SUBPROCESS — ITS PER-ROW LOOP WOULD BLOCK THE INGEST LOOP IN-PROCESS."));

  const grid = el("div", "cgrid");
  grid.append(watchCard, rec, paperCard, uniCard, jobsCard);

  ui.jobs = el("div", "cjobs");
  ui.audit = el("div", "caudit");
  const wide = el("div", "cwide");
  wide.append(card("JOBS THIS RUN", ui.jobs), card("AUDIT TRAIL", ui.audit));

  ui.body.append(grid, wide);
  const foot = el("div", "des-foot",
    "TAB FIELDS · ESC MONITOR · EVERY ACTION IS AUDITED · NO ORDERS ARE EVER PLACED");
  root.append(head, ui.note, ui.body, foot);
  built = true;
}

// ---------- the two lists, which genuinely do change shape ----------

function renderJobs() {
  const jobs = (ctl && ctl.jobs) || [];
  if (!jobs.length) {
    ui.jobs.replaceChildren(el("div", "quiet-line cnote", "NO JOBS THIS RUN"));
    return;
  }
  const out = [];
  for (const j of jobs) {
    const r = el("div", "cjob kv");
    const head = el("span", "k");
    head.append(el("span", "cj-name", String(j.name || "").toUpperCase()));
    head.append(el("span", "cj-st st-" + j.status, String(j.status || "").toUpperCase()));
    const v = el("span", "v cj-v");
    const line = j.total
      ? j.phase + " " + nf.format(j.step) + "/" + nf.format(j.total)
      : (j.phase || "");
    v.append(el("span", "cj-phase", (line + " " + (j.message || "")).trim().slice(0, 64)));
    v.append(el("span", "cj-el num", (j.elapsed_s || 0).toFixed(1) + "s"));
    if (j.status === "running") {
      const c = el("button", "cbtn tiny", "CANCEL");
      c.type = "button";
      c.title = actionTitle("jobs.cancel");
      c.addEventListener("click", (e) => {
        run("jobs.cancel", { job_id: j.job_id });
        if (e.detail > 0) c.blur();
      });
      v.append(c);
    }
    if (j.error) v.append(el("span", "cj-err", String(j.error).toUpperCase().slice(0, 60)));
    r.append(head, v);
    out.push(r);
  }
  ui.jobs.replaceChildren(...out);
}

function renderAudit() {
  if (!audit.length) {
    ui.audit.replaceChildren(el("div", "quiet-line cnote",
      ctl && ctl.read_only ? "NOT RECORDED — NO DATABASE" : "NOTHING YET THIS RUN"));
    return;
  }
  const out = [];
  for (const a of audit) {
    const r = el("div", "kv caud");
    const k = el("span", "k");
    k.append(el("span", "ca-when", (a.ts_ns ? fmtAgo(Date.now() - a.ts_ns / 1e6) : "—") + " AGO"));
    k.append(el("span", "ca-res st-" + (a.result || "ok"), String(a.result || "").toUpperCase()));
    const v = el("span", "v ca-v");
    v.append(el("span", "ca-act", String(a.action || "")));
    // The effect is the sentence the operator was SHOWN at the time — what
    // they agreed to, not something re-derived from the parameters now.
    v.append(el("span", "ca-eff", String(a.effect || "")));
    if (a.error) v.append(el("span", "ca-err", String(a.error).toUpperCase().slice(0, 50)));
    r.append(k, v);
    out.push(r);
  }
  ui.audit.replaceChildren(...out);
}

function renderBanners() {
  const out = [];
  const bind = ctl && ctl.bind;
  if (bind && !bind.loopback) {
    out.push(el("div", "cbind", "BOUND TO " + bind.host + " — NOT LOOPBACK. ANYONE WHO CAN "
      + "REACH THIS PORT CAN DRIVE THIS PROCESS."));
  }
  if (ctl && ctl.read_only) {
    out.push(el("div", "cro", "READ-ONLY: THIS SERVER WAS STARTED WITH CONTROLS DISABLED. "
      + "NOTHING HERE WILL WRITE."));
  }
  if (armed) {
    const b = el("div", "carm");
    b.append(el("span", "carm-tag", "CONFIRM"));
    // Verbatim from the server: this exact sentence goes into the audit row.
    b.append(el("span", "carm-eff", armed.effect));
    const go = el("button", "cbtn primary", "CONFIRM " + armed.action.toUpperCase());
    go.type = "button";
    go.addEventListener("click", (e) => {
      run(armed.action, armed.params);
      if (e.detail > 0) go.blur();
    });
    const no = el("button", "cbtn", "CANCEL");
    no.type = "button";
    no.addEventListener("click", (e) => {
      armed = null;
      render();
      if (e.detail > 0) no.blur();
    });
    b.append(go, no, el("span", "carm-ttl num",
      Math.max(0, Math.round((armed.until - Date.now()) / 1000)) + "s"));
    out.push(b);
  }
  if (result) {
    // The receipt. A success says what moved; a no-op says, in the server's
    // own sentence, that nothing did — and keeps saying it until the next
    // action, because that is the message a toast lost for a whole afternoon.
    const kind = result.failed ? "fail" : result.changed ? "done" : "noop";
    const b = el("div", "cres cres-" + kind);
    b.append(el("span", "cres-tag",
      result.failed ? "FAILED" : result.changed ? "APPLIED" : "NO CHANGE"));
    b.append(el("span", "cres-act", result.action));
    b.append(el("span", "cres-msg", result.message));
    const x = el("button", "cbtn tiny", "DISMISS");
    x.type = "button";
    x.addEventListener("click", (e) => {
      result = null;
      render();
      if (e.detail > 0) x.blur();
    });
    b.append(x);
    out.push(b);
  }
  ui.note.replaceChildren(...out);
}

// ---------- render: writes values, never rebuilds a field ----------

function render() {
  if (!built || !mounted) return;
  const ro = !!(ctl && ctl.read_only);

  ui.headStat.textContent = ctl
    ? (ro ? "READ-ONLY · " : "") + nf.format((ctl.actions || []).length) + " ACTIONS"
    : "AWAITING CONTROL STATE";

  // recorder
  const on = !!(ctl && ctl.recording);
  ui.recState.textContent = on ? "● ON" : "OFF";
  ui.recState.className = on ? "st-live" : "st-dim";

  // paper
  const p = paper();
  ui.paperMissing.hidden = !!p.attached;
  const st = !p.attached ? "NOT ATTACHED"
    : p.suspended ? "SUSPENDED"
    : p.enabled ? "● TAKING EDGES" : "IDLE";
  ui.paperState.textContent = st;
  ui.paperState.className = p.enabled && !p.suspended ? "st-live" : "st-dim";
  ui.paperSpent.textContent = "$" + ((p.notional_ticks || 0) / 10000).toFixed(2);
  ui.paperSkipped.hidden = !p.skipped_suspended;
  ui.paperSkippedVal.textContent = nf.format(p.skipped_suspended || 0);

  // watch set
  const pr = pairs();
  const pl = poll();
  ui.trackGap.textContent = count(pr.tracked) + " OF " + count(pr.confirmed) + " CONFIRMED";
  // Amber while some confirmed pairs are not watched (the normal, deliberate
  // state), red when nothing at all is watched with pairs available to watch —
  // that one is silence where there should be quotes.
  const idle = pr.tracked === 0 && !!pr.confirmed;
  ui.trackGap.classList.toggle("warn", idle);
  // `pairs.live` and the older top-level `tracked_pairs` are the same number
  // (the pairs the monitor is quoting). A process with no control plane
  // publishes only the second, so fall back to it rather than show "—" for a
  // count that is right there.
  const live = pr.live != null ? pr.live : (ctl ? ctl.tracked_pairs : null);
  ui.live.textContent = count(live) + (live === 1 ? " PAIR" : " PAIRS");
  ui.watchCycle.textContent = pl.cycle_s == null
    ? "—"
    : secs(pl.cycle_s) + " / BOOK · " + count(pl.targets) + " TARGETS"
      + (pl.attached ? "" : " (POLLER NOT ATTACHED)");
  ui.perPair.textContent = pl.per_pair_s == null ? "—" : "+" + secs(pl.per_pair_s) + " / BOOK";
  const gap = pr.confirmed == null || pr.tracked == null ? null : pr.confirmed - pr.tracked;
  ui.gapNote.hidden = !gap;
  if (gap) {
    ui.gapNote.textContent = nf.format(gap) + " CONFIRMED "
      + (gap === 1 ? "PAIR IS" : "PAIRS ARE") + " NOT WATCHED. CONFIRMING SAYS THE TWO "
      + "MARKETS RESOLVE THE SAME; WATCHING SPENDS POLL BUDGET ON THEM. CHOOSE THEM ON "
      + "/PAIRS WITH T, OR BULK-SET THE TOP N BELOW.";
  }

  // universe
  ui.kCount.textContent = nf.format((kalshi().tickers || []).length);
  ui.pCount.textContent = nf.format((polymarket().slugs || []).length);
  const cyc = polymarket().cycle_s;
  ui.cycle.textContent = cyc == null ? "—" : cyc.toFixed(1) + "s / BOOK";

  // Fields take the server's value only when you are not editing them.
  for (const f of fields) syncField(f);

  // Buttons: state in place, so a click target never moves under the pointer.
  for (const b of buttons) {
    const s = spec(b.action);
    b.el.disabled = !s || (ro && s.mutates) || busy.has(b.action);
    b.el.title = actionTitle(b.action);
    b.el.classList.toggle("g-g3", !!s && s.grade === "G3");
    b.el.classList.toggle("g-g4", !!s && s.grade === "G4");
  }
  // The primary call-to-action follows the state it would change.
  const start = buttons.find((b) => b.action === "recording.start");
  const stop = buttons.find((b) => b.action === "recording.stop");
  if (start) start.el.classList.toggle("primary", !on);
  if (stop) stop.el.classList.toggle("primary", on);

  renderBanners();
  renderJobs();
  renderAudit();
}

function tick() {
  if (!mounted) return;
  if (armed && Date.now() > armed.until) armed = null;
  // A receipt for something that DID change expires; "nothing changed" does
  // not, because it is the one an operator has to actually read.
  if (result && result.changed && Date.now() - result.at > RESULT_MS) result = null;
  render();
}

// The control frame is the truth; a page that guessed would drift from it.
onMessage("control", (m) => {
  if (m && m.control) {
    ctl = m.control;
    schedule("control");
  }
});
onMessage("job", () => schedule("control"));

registerRenderer("control", render);

export default {
  id: "control",
  path: "/control",
  title: "CONTROL",
  nav: true,
  root: ROOT,
  regions: ["ctl-minnet", "ctl-maxcts", "ctl-maxnot", "ctl-pairstop", "ctl-ktickers",
            "ctl-pmslugs", "ctl-runid"],

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
    armed = null;     // an armed action must not survive leaving the page
    result = null;    // nor the receipt for the last one
    clearDirty();     // nor a half-typed limit
  },

  render,

  onKey(e, scope) {
    // No letter bindings: every control here writes something, and a bare
    // letter must never reach one. Buttons and Tab are the way in.
    if (scope !== SCOPE.LIST) return false;
    return false;
  },

  keyHints(scope) {
    if (scope === SCOPE.TEXT) return [{ k: "ESC", d: "REVERT FIELD" }];
    return [{ k: "TAB", d: "FIELDS" }];
  },
};
