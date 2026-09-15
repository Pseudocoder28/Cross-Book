/* core/cmd.js — the ARB> command line.

   The command line is the primary input of the terminal: you type anywhere
   and it lands here (core/keys.js). Commands navigate; they no longer open
   overlays. */

import { $ } from "./dom.js";
import { state, select } from "./state.js";
import { navigate } from "./router.js";

const MAX_LEN = 48;

let cmdBuf = "";
let cmdMsgTimer = 0;

/** The current command buffer (already uppercased). */
export function buffer() {
  return cmdBuf;
}

export function setBuffer(s) {
  cmdBuf = String(s || "").toUpperCase().slice(0, MAX_LEN);
  render();
}

/** Append one printable character. */
export function push(ch) {
  if (cmdBuf.length < MAX_LEN) cmdBuf += ch.toUpperCase();
  render();
}

export function backspace() {
  cmdBuf = cmdBuf.slice(0, -1);
  render();
}

export function clear() {
  cmdBuf = "";
  render();
}

export function render() {
  $("cmd-text").textContent = cmdBuf;
}

/** Flash a message at the right of the command line. cls is "ok" or "err". */
export function message(text, cls) {
  const e = $("cmd-msg");
  e.textContent = text;
  e.className = "cmd-msg " + cls;
  clearTimeout(cmdMsgTimer);
  cmdMsgTimer = setTimeout(() => { e.textContent = ""; }, 2500);
}

function desPath(id) {
  return "/market/" + encodeURIComponent(id);
}

/** A command always ends on the ARB> line. Running one is a COMMAND-scope
    act, so the next bare letter has to be typing again even when the command
    was typed with the focus parked in a list (the dirty-buffer rule lets you
    do exactly that). Done before navigating, so the router's mount-focus
    guard sees the keyboard already home and leaves it alone.

    Deliberately not an import from core/keys.js: keys.js imports this module,
    and a two-line getElementById beats a circular import. */
function home() {
  const c = $("cmd");
  if (c && document.activeElement !== c) c.focus({ preventScroll: true });
}

/** Run whatever is in the buffer, then empty it. */
export function exec() {
  const raw = cmdBuf.trim();
  cmdBuf = "";
  render();
  home();
  if (!raw) return;
  let q = raw.replace(/\s*<\s*GO\s*>\s*$/i, "").replace(/\s+GO$/i, "").trim().toUpperCase();
  if (!q) return;
  // "DES" opens the description of the selected market; "<TICKER> DES"
  // selects first. Bloomberg muscle memory, kept deliberately.
  let wantDes = false;
  if (q === "MON" || q === "MONITOR") {
    navigate("/");
    message("MONITOR", "ok");
    return;
  }
  if (q === "PAIRS") {
    navigate("/pairs");
    message("PAIRS", "ok");
    return;
  }
  if (q === "ARB") {
    navigate("/arb");
    message("ARB", "ok");
    return;
  }
  if (q === "PAPER") {
    navigate("/paper");
    message("PAPER", "ok");
    return;
  }
  if (q === "SYS" || q === "SYSTEM") {
    navigate("/system");
    message("SYSTEM", "ok");
    return;
  }
  if (q === "HELP" || q === "?") {
    navigate("/help");
    message("HELP", "ok");
    return;
  }
  if (q === "BACK") {
    history.back();
    message("BACK", "ok");
    return;
  }
  if (q === "DES") {
    if (!state.selectedId) { message("NO MARKET SELECTED", "err"); return; }
    navigate(desPath(state.selectedId));
    message((state.byId.get(state.selectedId) || {}).ticker + " DES", "ok");
    return;
  }
  if (/\s+DES$/.test(q)) { wantDes = true; q = q.replace(/\s+DES$/, "").trim(); }
  const tickers = state.markets;
  const hit =
    tickers.find((m) => (m.ticker || "").toUpperCase() === q) ||
    tickers.find((m) => (m.ticker || "").toUpperCase().startsWith(q)) ||
    tickers.find((m) => (m.ticker || "").toUpperCase().includes(q));
  if (hit) {
    select(hit.market_id);
    if (wantDes) navigate(desPath(hit.market_id));
    message(hit.ticker + (wantDes ? " DES" : " <GO>"), "ok");
  } else {
    message("NO MATCH · " + q, "err");
  }
}
