/* pages/control.js — the control plane: every runtime toggle and job.

   This is the page that can break things, so it is built to make that
   obvious. Three rules it follows and the others do not:

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
      wording here would mean the screen and the permanent record disagree. */

import { $, el, setText } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { nf, fmtAgo } from "../core/format.js";
import { onMessage } from "../core/ws.js";
import { SCOPE } from "../core/keys.js";
import * as cmd from "../core/cmd.js";

const ROOT = "control-page";
const LOG_LIMIT = 40;
const TICK_MS = 1000;

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
let armed = null;        // {action, token, effect, until}
let busy = new Set();    // actions with a POST in flight
let noteEl = null;
let bodyEl = null;
let headStat = null;

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
    cmd.message((res.body.effect || action).toUpperCase().slice(0, 60), "ok");
    loadAudit();
  } else {
    armed = null;
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

// ---------- building blocks ----------

function spec(action) {
  const list = (ctl && ctl.actions) || [];
  return list.find((a) => a.action === action) || null;
}

function actionTitle(action, extra) {
  const s = spec(action);
  if (!s) return action + " — NOT AVAILABLE IN THIS PROCESS";
  const grade = s.grade + (GRADE_NOTE[s.grade] ? " · " + GRADE_NOTE[s.grade].toUpperCase() : "");
  return [s.summary.toUpperCase(), grade, s.confirm ? "ASKS FOR CONFIRMATION" : null, extra]
    .filter(Boolean)
    .join(" · ");
}

/** A button that runs an action. Disabled when the action does not exist in
    this process, when the plane is read-only, or while a POST is in flight. */
function actionBtn(action, label, paramsFn, cls) {
  const s = spec(action);
  const b = el("button", "cbtn " + (cls || ""), label);
  b.type = "button";
  b.dataset.action = action;
  b.title = actionTitle(action);
  const blocked = !s || (ctl && ctl.read_only && s.mutates) || busy.has(action);
  b.disabled = !!blocked;
  if (s) b.classList.add("g-" + s.grade.toLowerCase());
  b.addEventListener("click", (e) => {
    run(action, paramsFn ? paramsFn() : null);
    if (e.detail > 0) b.blur();   // pointer click: typing goes back to ARB>
  });
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

function numField(id, value, width) {
  const i = el("input", "cnum");
  i.type = "text";
  i.id = id;
  i.value = value == null ? "" : String(value);
  i.autocomplete = "off";
  i.spellcheck = false;
  i.setAttribute("inputmode", "numeric");
  if (width) i.style.width = width;
  return i;
}

function numOf(id, fallback) {
  const e = $(id);
  if (!e) return fallback;
  const n = Number(String(e.value).trim().replace(/,/g, ""));
  return Number.isFinite(n) ? Math.round(n) : fallback;
}

function card(title, ...nodes) {
  const c = el("div", "sys-block cblock");
  c.append(el("div", "sys-title", title));
  c.append(...nodes);
  return c;
}

// ---------- sections ----------

function recordingCard() {
  const on = !!(ctl && ctl.recording);
  const body = [];
  body.push(row("STATE", el("span", on ? "st-live" : "st-dim", on ? "● ON" : "OFF")));
  const bar = el("div", "cbtns");
  bar.append(
    actionBtn("recording.start", "START", null, on ? "" : "primary"),
    actionBtn("recording.stop", "STOP", null, on ? "primary" : ""),
  );
  body.push(bar);
  body.push(el("div", "cnote quiet-line",
    "OFF LEAVES A HOLE A REPLAY READS STRAIGHT ACROSS. THE AUDIT ROW BELOW IS "
    + "THE ONLY RECORD THAT THE GAP WAS DELIBERATE."));
  return card("RECORDER", ...body);
}

function paperCard() {
  const p = (ctl && ctl.paper) || {};
  const lim = p.limits || {};
  const body = [];
  if (!p.attached) {
    body.push(el("div", "cnote quiet-line",
      "NO PAPER TRADER ON THIS RUN"));
  }
  const st = !p.attached ? "NOT ATTACHED" : p.suspended ? "SUSPENDED" : p.enabled ? "● TAKING EDGES" : "IDLE";
  body.push(row("STATE", el("span", p.enabled && !p.suspended ? "st-live" : "st-dim", st)));
  body.push(row("SPENT", el("span", "num", "$" + ((p.notional_ticks || 0) / 10000).toFixed(2))));
  if (p.skipped_suspended) {
    body.push(row("SKIPPED WHILE SUSPENDED", el("span", "num", nf.format(p.skipped_suspended))));
  }
  const bar = el("div", "cbtns");
  bar.append(actionBtn("paper.suspend", "SUSPEND"), actionBtn("paper.resume", "RESUME"));
  body.push(bar);
  body.push(el("div", "cnote quiet-line",
    "SUSPEND KEEPS POSITIONS AND THE SPEND ALREADY COMMITTED — IT IS NOT A RESET."));

  body.push(el("div", "sys-title cs2", "RISK LIMITS"));
  body.push(row("MIN NET / CT (TICKS)", numField("ctl-minnet", lim.min_net_ticks, "7ch")));
  body.push(row("MAX CTS / PAIR",
    numField("ctl-maxcts", Math.round((lim.max_qty_per_pair || 0) / 10000), "7ch")));
  body.push(row("MAX NOTIONAL ($)",
    numField("ctl-maxnot", Math.round((lim.max_notional_ticks || 0) / 10000), "7ch")));
  const apply = el("div", "cbtns");
  apply.append(actionBtn("paper.limits", "APPLY LIMITS", () => ({
    min_net_ticks: numOf("ctl-minnet", lim.min_net_ticks),
    max_cts_per_pair: numOf("ctl-maxcts", Math.round((lim.max_qty_per_pair || 0) / 10000)),
    max_notional_ticks: numOf("ctl-maxnot", Math.round((lim.max_notional_ticks || 0) / 10000)) * 10000,
  }), "primary"));
  body.push(apply);
  return card("PAPER TRADING", ...body);
}

function universeCard() {
  const u = (ctl && ctl.universe) || {};
  const k = u.kalshi || {};
  const pm = u.polymarket_us || {};
  const body = [];
  body.push(row("KALSHI SUBSCRIBED", el("span", "num", nf.format((k.tickers || []).length))));
  body.push(row("POLYMARKET POLLED", el("span", "num", nf.format((pm.slugs || []).length))));
  if (pm.cycle_s != null) {
    body.push(row("POLL CYCLE", el("span", "num", pm.cycle_s.toFixed(1) + "s / BOOK")));
  }
  body.push(row("TRACKED PAIRS", el("span", "num", nf.format((ctl && ctl.tracked_pairs) || 0))));

  body.push(el("div", "sys-title cs2", "TRACKED PAIRS (TOP N BY SCORE)"));
  body.push(row("PAIRS TOP", numField("ctl-pairstop", ctl && ctl.pairs_top, "7ch")));
  const pt = el("div", "cbtns");
  pt.append(actionBtn("pairs.top", "RELOAD PAIRS",
    () => ({ n: numOf("ctl-pairstop", (ctl && ctl.pairs_top) || 0) }), "primary"));
  body.push(pt);
  body.push(el("div", "cnote quiet-line",
    "RE-RESOLVES BOTH VENUES' FEE PARAMETERS — A FEW SECONDS OF REST CALLS."));

  body.push(el("div", "sys-title cs2", "KALSHI SUBSCRIPTION"));
  const kt = el("textarea", "clist");
  kt.id = "ctl-ktickers";
  kt.rows = 3;
  kt.spellcheck = false;
  kt.value = (k.tickers || []).join("\n");
  body.push(kt);
  const kb = el("div", "cbtns");
  kb.append(actionBtn("universe.kalshi", "SUBSCRIBE",
    () => ({ tickers: linesOf("ctl-ktickers") })));
  body.push(kb);
  body.push(el("div", "cnote quiet-line",
    "COSTS A RECONNECT: SECONDS OF GAP AND EVERY BOOK RESNAPSHOTS."));

  body.push(el("div", "sys-title cs2", "POLYMARKET US POLL TARGETS"));
  const pt2 = el("textarea", "clist");
  pt2.id = "ctl-pmslugs";
  pt2.rows = 3;
  pt2.spellcheck = false;
  pt2.value = (pm.slugs || []).join("\n");
  body.push(pt2);
  const pb = el("div", "cbtns");
  pb.append(actionBtn("universe.polymarket", "SET TARGETS",
    () => ({ slugs: linesOf("ctl-pmslugs") })));
  body.push(pb);
  body.push(el("div", "cnote quiet-line",
    "NO RECONNECT. MORE TARGETS = A LONGER CYCLE PER BOOK; STALENESS IS RETUNED."));
  return card("UNIVERSE", ...body);
}

function linesOf(id) {
  const e = $(id);
  if (!e) return [];
  return String(e.value).split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
}

function jobsCard() {
  const body = [];
  const bar = el("div", "cbtns");
  bar.append(
    actionBtn("jobs.doctor", "RUN DOCTOR"),
    actionBtn("jobs.propose", "PROPOSE PAIRS", () => ({
      min_score: undefined,
    })),
    actionBtn("jobs.backfill", "BACKFILL SLUGS"),
  );
  body.push(bar);
  body.push(el("div", "cnote quiet-line",
    "PROPOSE TAKES ABOUT THREE MINUTES AND WRITES THOUSANDS OF ROWS. IT RUNS OFF "
    + "THE INGEST LOOP, SO BOOKS AND THE TAPE KEEP MOVING."));

  body.push(el("div", "sys-title cs2", "REPLAY A RECORDED RUN"));
  const ri = el("input", "cnum crun");
  ri.type = "text";
  ri.id = "ctl-runid";
  ri.placeholder = "RUN ID (BLANK = LATEST)";
  ri.autocomplete = "off";
  ri.spellcheck = false;
  body.push(row("RUN", ri));
  const rb = el("div", "cbtns");
  rb.append(actionBtn("jobs.replay", "REPLAY", () => {
    const id = String((($("ctl-runid") || {}).value) || "").trim();
    return id ? { run_id: id } : {};
  }));
  body.push(rb);
  body.push(el("div", "cnote quiet-line",
    "RUNS AS A SUBPROCESS — ITS PER-ROW LOOP WOULD BLOCK THE INGEST LOOP IN-PROCESS."));
  return card("JOBS", ...body);
}

function jobList() {
  const jobs = (ctl && ctl.jobs) || [];
  const wrap = el("div", "cjobs");
  if (!jobs.length) {
    wrap.append(el("div", "quiet-line cnote", "NO JOBS THIS RUN"));
    return wrap;
  }
  for (const j of jobs) {
    const r = el("div", "cjob kv");
    const running = j.status === "running";
    const head = el("span", "k");
    head.append(el("span", "cj-name", j.name.toUpperCase()));
    head.append(el("span", "cj-st st-" + j.status, j.status.toUpperCase()));
    const v = el("span", "v cj-v");
    const line = j.total
      ? j.phase + " " + nf.format(j.step) + "/" + nf.format(j.total)
      : (j.phase || "");
    v.append(el("span", "cj-phase", (line + " " + (j.message || "")).trim().slice(0, 64)));
    v.append(el("span", "cj-el num", j.elapsed_s.toFixed(1) + "s"));
    if (running) {
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
    wrap.append(r);
  }
  return wrap;
}

function auditList() {
  const wrap = el("div", "caudit");
  if (!audit.length) {
    wrap.append(el("div", "quiet-line cnote",
      ctl && ctl.read_only ? "NOT RECORDED — NO DATABASE" : "NOTHING YET THIS RUN"));
    return wrap;
  }
  for (const a of audit) {
    const r = el("div", "kv caud");
    const when = a.ts_ns ? fmtAgo(Date.now() - a.ts_ns / 1e6) : "—";
    const k = el("span", "k");
    k.append(el("span", "ca-when", when + " AGO"));
    k.append(el("span", "ca-res st-" + (a.result || "ok"), String(a.result || "").toUpperCase()));
    const v = el("span", "v ca-v");
    v.append(el("span", "ca-act", String(a.action || "")));
    // The effect is the sentence the operator was SHOWN at the time. It is
    // the point of the trail: it says what they agreed to, not what we infer.
    v.append(el("span", "ca-eff", String(a.effect || "")));
    if (a.error) v.append(el("span", "ca-err", String(a.error).toUpperCase().slice(0, 50)));
    r.append(k, v);
    wrap.append(r);
  }
  return wrap;
}

function armBanner() {
  if (!armed) return null;
  const left = Math.max(0, Math.round((armed.until - Date.now()) / 1000));
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
  b.append(go, no, el("span", "carm-ttl num", left + "s"));
  return b;
}

function bindBanner() {
  const bind = ctl && ctl.bind;
  if (!bind || bind.loopback) return null;
  const b = el("div", "cbind");
  b.textContent = "BOUND TO " + bind.host + " — NOT LOOPBACK. ANYONE WHO CAN REACH THIS "
    + "PORT CAN DRIVE THIS PROCESS.";
  return b;
}

// ---------- render ----------

function build() {
  const root = $(ROOT);
  if (!root || built) return;
  const head = el("div", "panel-head");
  head.append(el("span", "title", "CONTROL"));
  headStat = el("span", "head-stat", "—");
  head.append(headStat);
  noteEl = el("div", "cbanner");
  bodyEl = el("div", "cbody");
  const foot = el("div", "des-foot",
    "TAB FIELDS · ESC MONITOR · EVERY ACTION IS AUDITED · NO ORDERS ARE EVER PLACED");
  root.append(head, noteEl, bodyEl, foot);
  built = true;
}

function render() {
  if (!built || !mounted) return;
  const ro = !!(ctl && ctl.read_only);
  headStat.textContent = ctl
    ? (ro ? "READ-ONLY · " : "") + nf.format(((ctl.actions || []).length)) + " ACTIONS"
    : "AWAITING CONTROL STATE";

  noteEl.textContent = "";
  const bind = bindBanner();
  if (bind) noteEl.append(bind);
  if (ro) {
    noteEl.append(el("div", "cro",
      "READ-ONLY: THIS SERVER WAS STARTED WITH CONTROLS DISABLED. NOTHING HERE WILL WRITE."));
  }
  const arm = armBanner();
  if (arm) noteEl.append(arm);

  const grid = el("div", "cgrid");
  grid.append(recordingCard(), paperCard(), universeCard(), jobsCard());
  const wide = el("div", "cwide");
  wide.append(card("JOBS THIS RUN", jobList()), card("AUDIT TRAIL", auditList()));
  bodyEl.replaceChildren(grid, wide);
}

function tick() {
  if (!mounted) return;
  if (armed && Date.now() > armed.until) armed = null;
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
  regions: ["ctl-minnet", "ctl-maxcts", "ctl-maxnot", "ctl-pairstop", "ctl-runid"],

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
    armed = null;   // an armed action must not survive leaving the page
  },

  render,

  onKey(e, scope) {
    // No letter bindings: every control here writes something, and a bare
    // letter must never reach one. Buttons and Tab are the way in.
    if (scope !== SCOPE.LIST) return false;
    return false;
  },

  keyHints(scope) {
    if (scope === SCOPE.TEXT) return [];
    return [{ k: "TAB", d: "FIELDS" }];
  },
};
