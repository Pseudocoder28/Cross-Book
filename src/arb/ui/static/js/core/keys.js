/* core/keys.js — global key handling.

   Order: a focused text field wins outright, then the Alt page shortcuts, then
   the active page's onKey(e), then the global bindings. A page returns true
   from onKey to claim a key. The text field comes first so the filter boxes
   neither steal "type anywhere to command" nor lose Option+arrow, which is
   word-navigation inside a field on macOS. */

import { state, selectRelative } from "./state.js";
import { currentPage, navPages, navigate } from "./router.js";
import * as cmd from "./cmd.js";

const ALT_DIGITS = ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7"];

function cycle(d) {
  const list = navPages();
  if (!list.length) return;
  const cur = currentPage();
  let i = cur ? list.findIndex((p) => p.id === cur.id) : -1;
  if (i < 0) i = d > 0 ? -1 : 0;
  const next = list[(i + d + list.length) % list.length];
  navigate(next.path);
}

function altBinding(e) {
  const di = ALT_DIGITS.indexOf(e.code);
  if (di >= 0) {
    const page = navPages()[di];
    // More digits than nav pages: leave the spare ones to the browser rather
    // than swallowing them into a binding that does nothing.
    if (!page) return false;
    navigate(page.path);
    return true;
  }
  if (e.code === "BracketLeft") { cycle(-1); return true; }
  if (e.code === "BracketRight") { cycle(1); return true; }
  if (e.code === "ArrowLeft") { history.back(); return true; }
  if (e.code === "ArrowRight") { history.forward(); return true; }
  return false;
}

export function onKey(e) {
  const t = e.target;
  // A real text field owns every key: the filter box must not be a command,
  // and Option+arrow is word-navigation inside it, not history back/forward.
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  if (e.altKey && !e.ctrlKey && !e.metaKey) {
    if (altBinding(e)) e.preventDefault();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // A focused control still owns its own activation keys.
  if (t && (t.tagName === "BUTTON" || t.tagName === "A" || t.tagName === "SELECT")
      && (e.key === "Enter" || e.key === " " || e.key === "Spacebar")) return;

  const page = currentPage();
  if (page && page.onKey(e)) {
    e.preventDefault();
    return;
  }

  const k = e.key;
  if (k === "ArrowUp" || k === "ArrowDown") {
    e.preventDefault();
    selectRelative(k === "ArrowUp" ? -1 : 1);
    return;
  }
  if (k === "Enter") {
    e.preventDefault();
    if (cmd.buffer().trim() === "") {
      // Enter on a market = DES, exactly as before; it is a route now.
      if (state.selectedId) navigate("/market/" + encodeURIComponent(state.selectedId));
    } else {
      cmd.exec();
    }
    return;
  }
  if (k === "Escape") {
    // "ESC CLOSE", as the keys strip has always promised. A half-typed command
    // is the innermost thing open, so it goes first; otherwise leave the page.
    if (cmd.buffer() !== "") {
      cmd.clear();
      return;
    }
    const cur = currentPage();
    if (cur && cur.id !== "monitor") navigate("/");
    return;
  }
  if (k === "Backspace") {
    e.preventDefault();
    cmd.backspace();
    return;
  }
  if (k.length === 1) {
    if (/^[\x20-\x7e]$/.test(k)) {
      e.preventDefault();
      cmd.push(k);
    }
  }
}

/** Bind the document-level keydown listener. Called once by main.js. */
export function install() {
  document.addEventListener("keydown", onKey);
}
