/* pages/system.js — SYSTEM (/system): the diagnostics page.

   This page is the restructure's missing half. The pre-restructure terminal
   carried SYSTEM and POLYMARKET US as two panels on the one screen; the
   multi-page port dropped their markup and never re-homed `renderSystem` /
   `renderPoly`, so the engine counters, the recorder state, the database
   status and the whole Polymarket REST line went dark. Both renderers are
   ported here verbatim (the run list is the one deliberate edit — it now
   says which run is the live one), against markup this module builds inside
   the empty `#system-page` root.

   Two dirty keys land here, not one: "system" (this page's id, registered by
   the router) and "poly", which the status poll and the 1s stats frame have
   always scheduled and which nothing has drawn since the restructure. */

import { $, el, setVal } from "../core/dom.js";
import { state, schedule, registerRenderer } from "../core/state.js";
import { nf, fmtRate, fmtUptime, fmtAge, fmtAgo, fmtMs } from "../core/format.js";

const SKEW_WARN_MS = 25;        // matches monitor.js and arb doctor's ntp check
const P95_HOT_MS = 250;         // matches monitor.js
const TICK_MS = 1000;           // "checked Ns ago" ticks


let mounted = false;
let tickTimer = 0;

// ---------- markup (module scope: type=module defers, the DOM is parsed) ----------

function kv(label, id, cls) {
  const row = el("div", "kv");
  row.append(el("span", "k", label));
  const v = el("span", "v" + (cls ? " " + cls : ""), "—");
  v.id = id;
  row.append(v);
  return row;
}

function linkKv(label, href, text) {
  const row = el("div", "kv");
  row.append(el("span", "k", label));
  const v = el("span", "v");
  const a = el("a", "sys-link", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noreferrer";
  row.append(a);
  return row;
}

function card(area, title, headId) {
  const b = el("div", "sys-block sys-card a-" + area);
  b.setAttribute("aria-label", title);
  const head = el("div", "sys-title sys-head-row");
  head.append(el("span", null, title));
  if (headId) {
    const stat = el("span", "head-stat", "—");
    stat.id = headId;
    head.append(stat);
  }
  b.append(head);
  return b;
}

function note(text) {
  return el("div", "sys-note", text);
}

function build() {
  const page = el("div", "sys-page");

  const head = el("div", "des-head");
  const title = el("span", "des-title");
  title.append(
    el("span", "des-tag", "SYS"),
    el("span", null, "ENGINE · RECORDER · DATABASE · CLOCK · VENUES"),
  );
  const stat = el("span", "head-stat", "—");
  stat.id = "sys-stat";
  head.append(title, stat);

  const body = el("div", "sys-body");
  const banner = el("div", "sys-banner quiet-line", "AWAITING FIRST STATS FRAME");
  banner.id = "sys-banner";

  const cards = el("div", "sys-cards");

  // ENGINE — ported panel, same ids so renderSystem is unchanged.
  const engine = card("engine", "ENGINE");
  engine.append(
    kv("MSG TOTAL", "sys-msg-total", "num"),
    kv("RATE 1S", "sys-rate", "num"),
    kv("PARSE ERR", "sys-parse", "num"),
    kv("SEQ GAPS", "sys-gaps", "num"),
    kv("WS CLIENTS", "sys-clients", "num"),
    kv("UPTIME", "sys-uptime", "num"),
    note("PARSE ERRORS AND SEQ GAPS ARE COUNTED, NEVER FATAL. A GAP FORCES A FRESH SNAPSHOT."),
  );

  // RECORDER — ported panel.
  const rec = card("rec", "RECORDER");
  rec.append(
    kv("STATE", "sys-rec-state"),
    kv("ENQUEUED", "sys-rec-enq", "num"),
    kv("DROPPED", "sys-rec-drop", "num"),
    note("OFF MEANS --no-record: DATA IS DISPLAYED, NOT PERSISTED, AND CANNOT BE REPLAYED."),
  );

  // DATABASE — ported panel plus the current/historical run distinction.
  const db = card("db", "DATABASE");
  const runs = el("div", "runs");
  runs.id = "sys-runs";
  db.append(
    kv("CONN", "sys-db-conn"),
    kv("RAW ROWS", "sys-db-rows", "num"),
    kv("CURRENT RUN", "sys-db-run"),
    el("div", "sys-sub", "RAW ROWS BY RUN · NEWEST FIRST"),
    runs,
    note("arb replay RUN_ID REPLAYS ANY ROW ABOVE THROUGH THE IDENTICAL PIPELINE."),
  );

  // CLOCK & LATENCY — new: the skew-aware latency work, explained.
  const clock = card("clock", "CLOCK & LATENCY");
  const cbanner = el("div", "sys-banner", "AWAITING LATENCY SAMPLES");
  cbanner.id = "sys-lat-banner";
  clock.append(
    cbanner,
    kv("ONE-WAY LAST", "sys-lat-last", "num"),
    kv("ONE-WAY MEDIAN", "sys-lat-med", "num"),
    kv("ONE-WAY P95", "sys-lat-p95", "num"),
    kv("SAMPLES", "sys-lat-n", "num"),
    kv("KEEPALIVE RTT", "sys-lat-rtt", "num"),
    kv("RTT / 2", "sys-lat-half", "num"),
    kv("CLOCK SKEW", "sys-lat-skew", "num"),
    note("ONE-WAY LATENCY IS MEASURED ACROSS TWO MACHINES' CLOCKS "
      + "(VENUE TIMESTAMP → LOCAL RECEIPT), SO IT READS true_transit + (local − venue). "
      + "NEGATIVE MEANS THE LOCAL CLOCK IS BEHIND THE VENUE'S — NOT FASTER-THAN-LIGHT DATA."),
    note("SKEW = MEDIAN − RTT/2. THE KEEPALIVE RTT ONLY EVER TOUCHES THE LOCAL CLOCK, "
      + "SO IT SURVIVES SKEW; WHEN THE BANNER IS AMBER, RTT/2 IS THE NUMBER TO TRUST."),
    note("FIX: uv run arb doctor  (SEE THE ntp clock CHECK), THEN sudo sntp -sS time.apple.com "
      + "ON macOS OR sudo timedatectl set-ntp true ON THE VM HOST."),
  );

  // KALSHI WS — venue health card.
  const kal = card("kalshi", "KALSHI · WEBSOCKET", "sys-k-stat");
  kal.append(
    kv("STATE", "sys-k-state", "vstate"),
    kv("DETAIL", "sys-k-detail"),
    kv("TRANSPORT", "sys-k-tr"),
    kv("STATUS CHECKED", "sys-k-checked"),
  );

  // POLYMARKET US — the ported down-screen, re-laid-out as a health card.
  const poly = card("poly", "POLYMARKET US", "poly-stat");
  const pbody = el("div", "poly-body");
  const pmain = el("div", "poly-main", "DATA UNAVAILABLE");
  pmain.id = "poly-main";
  const psub = el("div", "poly-sub", "AWAITING API CREDENTIALS");
  psub.id = "poly-sub";
  const plive = el("div", "poly-live");
  const pdot = el("span", "mini-dot");
  pdot.id = "poly-dot";
  pdot.setAttribute("aria-hidden", "true");
  const pline = el("span", null, "REST /markets · — · checked —");
  pline.id = "poly-line";
  plive.append(pdot, pline);
  pbody.append(pmain, psub, plive);
  poly.append(
    pbody,
    kv("POLL TARGETS", "sys-pm-targets", "num"),
    kv("CONFIGURED RATE", "sys-pm-rate", "num"),
    kv("REST /markets", "sys-pm-reach"),
    note("NO POLYMARKET US WEBSOCKET CREDENTIALS YET, SO BOOKS ARRIVE BY REST POLL — "
      + "POLLED IS HEALTHY, NOT DEGRADED."),
  );

  // OBSERVABILITY — the same numbers, without the terminal.
  const obs = card("obs", "OBSERVABILITY");
  obs.append(
    linkKv("PROMETHEUS", "/metrics", "/metrics"),
    linkKv("GRAFANA", "http://127.0.0.1:3000", "127.0.0.1:3000"),
    kv("DASHBOARD", "sys-obs-dash"),
    note("GRAFANA'S \"ARB — DATA PLANE\" DASHBOARD READS THE SAME SERIES THIS PAGE DOES: "
      + "arb_ws_one_way_latency_ms, arb_ws_rtt_ms, arb_clock_skew_ms, arb_messages_total."),
  );

  cards.append(engine, rec, db, clock, kal, poly, obs);
  body.append(banner, cards);
  page.append(head, body, el("div", "des-foot",
    "ESC MONITOR · ALT+1-6 PAGE · READ-ONLY DIAGNOSTICS · LIVE FROM THE 1S STATS FRAME AND /api/status"));
  $("system-page").append(page);

  // Static values that never change once the DOM exists.
  $("sys-k-tr").textContent = "WEBSOCKET · orderbook_delta";
  $("sys-obs-dash").textContent = "ARB — DATA PLANE";
}

build();

// ---------- system (ported from app.js renderSystem) ----------

function renderSystem() {
  const s = state.stats;
  if (s) {
    setVal($("sys-msg-total"), s.msg_total != null ? nf.format(s.msg_total) : "—");
    setVal($("sys-rate"), fmtRate(s.msg_rate_1s));
    setVal($("sys-parse"), s.parse_errors != null ? nf.format(s.parse_errors) : "—", (s.parse_errors || 0) > 0);
    setVal($("sys-gaps"), s.seq_gaps != null ? nf.format(s.seq_gaps) : "—", (s.seq_gaps || 0) > 0);
    setVal($("sys-clients"), s.ws_clients != null ? String(s.ws_clients) : "—");
    setVal($("sys-uptime"), fmtUptime(s.uptime_s));
    const rec = s.recorder;
    const recState = $("sys-rec-state");
    setVal(recState, rec ? "ON" : "OFF");
    recState.classList.toggle("on", !!rec);
    setVal($("sys-rec-enq"), rec ? nf.format(rec.enqueued) : "—");
    setVal($("sys-rec-drop"), rec ? nf.format(rec.dropped) : "—", rec ? rec.dropped > 0 : false);
    $("sys-stat").textContent = fmtRate(s.msg_rate_1s);
  }
  const db = state.status && state.status.database;
  if (db) {
    setVal($("sys-db-conn"), db.connected ? "UP" : "DOWN", !db.connected);
    setVal($("sys-db-rows"), db.raw_messages_total != null ? nf.format(db.raw_messages_total) : "—");
    const runs = Array.isArray(db.runs) ? db.runs.slice(0, 10) : [];
    const c = $("sys-runs");
    c.textContent = "";
    // CHANGED from app.js: a run id alone does not say which run is being
    // written right now. The live one is tagged; the tag is its own cell so
    // a copied row of a historical run gains nothing.
    const cur = currentRunId();
    for (const r of runs) {
      const live = cur != null && r.run_id === cur;
      const row = el("div", "kv" + (live ? " run-live" : ""));
      const k = el("span", "k run-id", r.run_id);
      k.title = r.run_id + (live ? " · CURRENT RUN" : " · HISTORICAL RUN");
      row.append(k, el("span", "v run-tag", live ? "LIVE" : ""), el("span", "v num", nf.format(r.count)));
      c.append(row);
    }
    if (!runs.length) c.append(el("div", "kv quiet-line", "NO RUNS"));
  }
}

function currentRunId() {
  return state.runId || (state.status && state.status.run_id) || null;
}

// ---------- polymarket down-screen (ported from app.js renderPoly) ----------

function renderPolyPorted() {
  const v = state.status && state.status.venues && state.status.venues.polymarket_us;
  const dot = $("poly-dot");
  const line = $("poly-line");
  if (!v) {
    $("poly-stat").textContent = "—";
    dot.className = "mini-dot";
    line.textContent = "REST /markets · — · checked —";
    return;
  }
  $("poly-stat").textContent = String(v.state || "down").toUpperCase();
  const polled = v.state === "polled" || v.state === "connecting";
  $("poly-main").textContent = polled ? "REST POLLING" : "DATA UNAVAILABLE";
  if (v.detail) {
    $("poly-sub").textContent = v.detail.toUpperCase();
    $("poly-sub").title = v.detail;
  }
  const ps = state.stats && state.stats.polymarket_us;
  if (polled && ps) {
    const age = ps.last_poll_age_ms;
    const fresh = age != null && age < 30000;
    dot.className = "mini-dot " + (fresh ? "dot-up" : "dot-down");
    line.textContent =
      "polls " + nf.format(ps.polls) + " · 429s " + nf.format(ps.rate_limited)
      + " · errors " + nf.format(ps.errors)
      + " · last book " + (age == null ? "—" : fmtAge(age) + " ago");
    return;
  }
  const ok = !!v.rest_reachable;
  dot.className = "mini-dot " + (ok ? "dot-up" : "dot-down");
  const ago = state.statusAt ? fmtAgo(Date.now() - state.statusAt) + " ago" : "—";
  line.textContent = "REST /markets · " + (ok ? "OK" : "UNREACHABLE") + " · checked " + ago;
}

/** The three figures the ported line never showed: the poll budget and the
    gateway reachability probe, which is what "polled but stale" turns on. */
function renderPolyBudget() {
  const v = state.status && state.status.venues && state.status.venues.polymarket_us;
  const ps = state.stats && state.stats.polymarket_us;
  setVal($("sys-pm-targets"), ps && ps.targets != null ? nf.format(ps.targets) : "—");
  setVal($("sys-pm-rate"), ps && ps.rate_per_s != null ? String(ps.rate_per_s) + " req/s" : "—");
  const reach = v ? !!v.rest_reachable : null;
  setVal($("sys-pm-reach"), v == null ? "—" : reach ? "REACHABLE" : "UNREACHABLE", v != null && !reach);
}

function renderPoly() {
  if (!mounted) return;
  renderPolyPorted();
  renderPolyBudget();
}

// ---------- clock & latency ----------

function renderClock() {
  const s = state.stats;
  const lm = (s && s.latency_ms) || {};
  const rtt = s ? s.rtt_ms : null;
  const skew = s ? s.clock_skew_ms : null;
  // Same condition as the monitor's latency panel: a negative one-way median
  // or a skew estimate past the doctor's threshold means the one-way number
  // is contaminated and RTT/2 is the figure to trust.
  const skewed = (lm.median != null && lm.median < 0) || (skew != null && Math.abs(skew) > SKEW_WARN_MS);
  setVal($("sys-lat-last"), fmtMs(lm.last));
  setVal($("sys-lat-med"), fmtMs(lm.median), lm.median != null && lm.median < 0);
  setVal($("sys-lat-p95"), fmtMs(lm.p95), lm.p95 != null && lm.p95 > P95_HOT_MS);
  setVal($("sys-lat-n"), lm.n != null ? nf.format(lm.n) : "—");
  setVal($("sys-lat-rtt"), fmtMs(rtt));
  setVal($("sys-lat-half"), rtt == null ? "—" : fmtMs(rtt / 2));
  setVal($("sys-lat-skew"), skew == null ? "—" : (skew > 0 ? "+" : "") + Math.round(skew) + "ms", skewed);
  const banner = $("sys-lat-banner");
  // A missing skew estimate is not a clean bill of health: without a
  // keepalive RTT there is nothing skew-immune to compare the median to.
  const unknown = !s || !lm.n;
  const text = unknown
    ? "AWAITING LATENCY SAMPLES"
    : skewed
      ? "CLOCK SKEW · TRUST RTT/2 " + (rtt != null ? fmtMs(rtt / 2) : "—")
      : skew == null
        ? "SKEW UNKNOWN · AWAITING KEEPALIVE RTT"
        : "CLOCKS AGREE · ONE-WAY LATENCY IS SOUND";
  if (banner.textContent !== text) banner.textContent = text;
  banner.classList.toggle("warn", skewed);
  banner.classList.toggle("quiet-line", unknown || (!skewed && skew == null));
}

// ---------- venue health ----------

function venueClass(st) {
  return "v vstate " + (st === "live" ? "st-live"
    : st === "polled" ? "st-polled"
      : st === "connecting" ? "st-warn"
        : st ? "st-down" : "st-dim");
}

function renderVenues() {
  const v = state.status && state.status.venues;
  const k = v && v.kalshi;
  const st = k ? String(k.state || "down") : null;
  const stateEl = $("sys-k-state");
  setVal(stateEl, st ? st.toUpperCase() : "—");
  stateEl.className = venueClass(st);
  $("sys-k-stat").textContent = st ? st.toUpperCase() : "—";
  const detail = k && k.detail ? k.detail : null;
  const detailEl = $("sys-k-detail");
  setVal(detailEl, detail ? detail.toUpperCase() : "—");
  detailEl.title = detail || "";
  // Plain write, not setVal: this one ticks every second and a flash per
  // second on a clock reading is noise, not information.
  const checked = state.statusAt ? fmtAgo(Date.now() - state.statusAt) + " AGO" : "—";
  const checkedEl = $("sys-k-checked");
  if (checkedEl.textContent !== checked) checkedEl.textContent = checked;
}

// ---------- page shell: the awaiting-data banner and the live run id ----------

function renderShell() {
  const run = currentRunId();
  const runEl = $("sys-db-run");
  // The run id stays whole in the DOM — copy must be exact, CSS clips it.
  setVal(runEl, run || "—");
  runEl.title = run || "";
  $("sys-banner").hidden = Boolean(state.stats);
}

// "poly" has been scheduled by the status poll and the stats frame since the
// first commit and has had no renderer since the restructure. "system" is
// this page's id, so the router registers render() under it for us.
registerRenderer("poly", renderPoly);

function tick() {
  // app.js ran this globally so "checked Ns ago" advanced between polls;
  // scoped to the mount here, because a page that leaves a timer running is
  // a bug. Both keys tick: the Kalshi card ages the same way the poly line does.
  if (state.status) schedule("poly", "system");
}

export default {
  id: "system",
  path: "/system",
  title: "SYSTEM",
  nav: true,
  root: "system-page",

  mount() {
    mounted = true;
    if (!tickTimer) tickTimer = setInterval(tick, TICK_MS);
    schedule("system", "poly");
  },

  unmount() {
    mounted = false;
    clearInterval(tickTimer);
    tickTimer = 0;
  },

  render() {
    renderSystem();
    renderClock();
    renderVenues();
    renderShell();
  },

  onKey() {
    return false;
  },
};
