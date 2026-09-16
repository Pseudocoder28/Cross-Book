/* ARB TERMINAL — entry point.

   Boots the shell (status bar, clocks, /api/status poll, select-to-copy),
   registers every page module with the router, then opens the ONE WebSocket
   the session uses. Routing is client side so navigation never touches that
   socket: the tape, the latency window and every book survive a page change.

   Page modules are loaded dynamically and self-register. A page whose module
   is not on disk yet gets a placeholder so its route still resolves, its nav
   tab still works and nothing throws. */

import { $, el, isReducedMotion } from "./core/dom.js";
import { state, schedule, registerRenderer, setSelecting, select } from "./core/state.js";
import { fmtClockUtc, nf } from "./core/format.js";
import { connect } from "./core/ws.js";
import { register, start } from "./core/router.js";
import * as keys from "./core/keys.js";
import * as cmd from "./core/cmd.js";

const STATUS_POLL_MS = 10000;

const PAGES = [
  { id: "monitor", path: "/", title: "MONITOR", nav: true, root: "monitor-page", module: "./pages/monitor.js" },
  { id: "arb", path: "/arb", title: "ARB", nav: true, root: "arbpage", module: "./pages/arb.js" },
  { id: "pairs", path: "/pairs", title: "PAIRS", nav: true, root: "pairs", module: "./pages/pairs.js" },
  { id: "paper", path: "/paper", title: "PAPER", nav: true, root: "paperpage", module: "./pages/paper.js" },
  { id: "system", path: "/system", title: "SYSTEM", nav: true, root: "system-page", module: "./pages/system.js" },
  { id: "control", path: "/control", title: "CONTROL", nav: true, root: "control-page", module: "./pages/control.js" },
  { id: "help", path: "/help", title: "HELP", nav: true, root: "help-page", module: "./pages/help.js" },
  { id: "market", path: "/market/:id", title: "DES", nav: false, root: "des", module: "./pages/market.js" },
];

// ---------- status bar ----------

const etFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function setVenue(elm, vv) {
  const st = vv ? String(vv.state || "down") : null;
  elm.textContent = st ? st.toUpperCase() : "—";
  elm.title = vv && vv.detail ? vv.detail : "";
  elm.className = "val vstate " + (st === "live" ? "st-live" : st === "polled" ? "st-polled" : st === "connecting" ? "st-warn" : st ? "st-down" : "st-dim");
}

function renderStatusBar() {
  const runId = state.runId || (state.status && state.status.run_id) || null;
  const runEl = $("run-id");
  // Full value in the DOM: select-to-copy reads textContent, and a run id
  // copied short is a run id that `arb replay` cannot find. CSS clips it.
  runEl.textContent = runId || "—";
  runEl.title = runId || "";
  const v = state.status && state.status.venues;
  setVenue($("v-kalshi"), v && v.kalshi);
  setVenue($("v-poly"), v && v.polymarket_us);
  const recEl = $("rec-state");
  if (state.status == null) {
    recEl.textContent = "—";
    recEl.className = "val st-dim";
  } else if (state.status.recording) {
    recEl.textContent = "● ON";
    recEl.className = "val st-live";
  } else {
    recEl.textContent = "OFF";
    recEl.className = "val st-dim";
  }
  const s = state.conn;
  $("conn").className = "conn conn-" + s;
  $("conn-text").textContent =
    s === "live" ? "LIVE" : s === "reconnecting" ? "RECONNECTING" : s === "connecting" ? "CONNECTING" : "DOWN";
}

function renderClocks() {
  const d = new Date();
  $("clock-utc").textContent = d.toISOString().slice(0, 10) + " " + fmtClockUtc(d.getTime()) + " UTC";
  $("clock-et").textContent = etFmt.format(d) + " ET";
}

// ---------- /api/status poll every 10s (tolerate failures) ----------

async function pollStatus() {
  try {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const to = ctl ? setTimeout(() => ctl.abort(), 5000) : 0;
    const r = await fetch("/api/status", { cache: "no-store", signal: ctl ? ctl.signal : undefined });
    if (to) clearTimeout(to);
    if (!r.ok) return;
    state.status = await r.json();
    state.statusAt = Date.now();
    schedule("status", "system", "poly");
  } catch (err) {
    /* keep last known data */
  }
}

// ---------- select-to-copy ----------
// Anything you select on the terminal lands on the clipboard. Tabular
// regions are rebuilt cell-by-cell as TSV, because the DOM's own
// serialization of a flex grid loses the column boundaries — a ladder
// selection should paste into a spreadsheet with its columns intact.
const ROW_SEL = ".ladder-row, .mon-row, .tape-row, .mid-row, .kv";

let copyToastTimer = 0;
let copyFadeTimer = 0;

function cellsOf(row) {
  return row.children.length ? Array.from(row.children) : [row];
}

function cellText(node) {
  return node.textContent.replace(/\s+/g, " ").trim();
}

function selectionText(sel) {
  const plain = sel.toString();
  let range;
  try {
    range = sel.getRangeAt(0);
  } catch (err) {
    return plain;
  }
  const rows = [];
  for (const row of document.querySelectorAll(ROW_SEL)) {
    if (range.intersectsNode(row)) rows.push(row);
  }
  if (!rows.length) return plain;

  if (rows.length === 1) {
    const hit = cellsOf(rows[0]).filter((c) => range.intersectsNode(c));
    // One cell touched: hand back exactly what was highlighted, so half a
    // number stays half a number. Several: re-join them with tabs.
    if (hit.length <= 1) return plain;
    return hit.map(cellText).filter(Boolean).join("\t");
  }
  const lines = [];
  for (const row of rows) {
    const line = cellsOf(row).map(cellText).filter(Boolean).join("\t");
    if (line) lines.push(line);
  }
  return lines.length ? lines.join("\n") : plain;
}

function showCopyToast(text, ok) {
  const box = $("copy-toast");
  const chars = text.length;
  const lines = text.split("\n").length;
  $("ct-label").textContent = ok ? "COPIED" : "COPY BLOCKED";
  $("ct-count").textContent = ok
    ? nf.format(chars) + (chars === 1 ? " CHAR" : " CHARS") + (lines > 1 ? " · " + lines + " ROWS" : "")
    : "CLIPBOARD DENIED";
  $("ct-preview").textContent = text.replace(/\s+/g, " ").trim().slice(0, 120);
  box.classList.toggle("err", !ok);
  box.classList.remove("fading");
  box.hidden = false;
  clearTimeout(copyToastTimer);
  clearTimeout(copyFadeTimer);
  copyToastTimer = setTimeout(() => {
    if (isReducedMotion()) {
      box.hidden = true;
      return;
    }
    box.classList.add("fading");
    copyFadeTimer = setTimeout(() => {
      box.hidden = true;
      box.classList.remove("fading");
    }, 160);
  }, 1500);
}

async function copySelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
  const text = selectionText(sel).replace(/\u00a0/g, " ");  // nbsp -> plain space
  if (!text.trim()) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      showCopyToast(text, true);
      return;
    }
  } catch (err) {
    /* permission or insecure context — fall back below */
  }
  try {
    // Legacy path copies the live selection as-is (no TSV rebuild).
    if (document.execCommand && document.execCommand("copy")) {
      showCopyToast(sel.toString(), true);
      return;
    }
  } catch (err) {
    /* fall through to the honest failure toast */
  }
  showCopyToast(text, false);
}

let selecting = false;

function endSelecting() {
  if (!selecting) return;
  selecting = false;
  setSelecting(false);
  schedule();  // flush whatever the drag held back
}

function installCopy() {
  // mouseup carries the transient user activation the async clipboard API
  // requires, so the write is done there rather than on selectionchange
  // (which fires mid-drag and without activation).
  document.addEventListener("mousedown", (e) => {
    if (e.button === 0) { selecting = true; setSelecting(true); }
  });
  window.addEventListener("mouseup", (e) => {
    if (e.button !== 0) return;
    endSelecting();
    copySelection();
  });
  // Button released outside the window: the next move over the page reports
  // no buttons held, which is when we un-pause rendering.
  document.addEventListener("mousemove", (e) => {
    if (selecting && e.buttons === 0) endSelecting();
  });
  window.addEventListener("blur", endSelecting);
  // Select-all is a keyboard gesture, and it also carries activation. Inside a
  // text field it means select-this-field, so the filter boxes keep it: the
  // TEXT-scope bail below is load-bearing, not a nicety. It is deliberately
  // the same test core/keys.js scopeOf() uses for SCOPE.TEXT.
  document.addEventListener("keyup", (e) => {
    if (!(e.metaKey || e.ctrlKey) || (e.key !== "a" && e.key !== "A")) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    copySelection();
  });
}

// ---------- page registration ----------

/** Stands in for a page module that is not on disk yet: the route resolves,
    the nav tab works, nothing throws. */
function placeholder(meta) {
  let node = null;
  return {
    id: meta.id,
    path: meta.path,
    title: meta.title,
    nav: meta.nav,
    root: meta.root,
    mount(params) {
      if (params && params.id) select(params.id);
      const root = $(meta.root);
      if (root && root.childElementCount === 0) {
        node = el("div", "page-stub quiet-line", meta.title + " — PAGE MODULE NOT INSTALLED");
        root.append(node);
      }
    },
    unmount() {
      if (node && node.parentNode) node.remove();
      node = null;
    },
    render() {},
    // A stub has no list and no text regions, so core/keys.js must find its
    // optional fields absent-but-answered rather than undefined: arrows fall
    // through to the global selection, Tab is left to the browser and the
    // keys strip shows the global bindings only.
    regions: [],
    listRegion: null,
    keyHints() { return []; },
    onKey() { return false; },
  };
}

async function loadPage(meta) {
  try {
    const mod = await import(meta.module);
    if (mod && mod.default && typeof mod.default.mount === "function") return mod.default;
  } catch (err) {
    // A broken module degrades to a stub rather than taking the terminal down
    // with it — but say so, or a page silently goes missing in production.
    console.error("page module failed to load: " + meta.module, err);
  }
  return placeholder(meta);
}

async function boot() {
  const loaded = await Promise.all(PAGES.map(loadPage));
  for (const page of loaded) register(page);   // registration order == nav order
  start();
  keys.install();
  installCopy();
  connect();
  pollStatus();
  setInterval(pollStatus, STATUS_POLL_MS);
  setInterval(renderClocks, 1000);
}

registerRenderer("status", renderStatusBar);
renderClocks();
cmd.render();
schedule("status");
boot();
