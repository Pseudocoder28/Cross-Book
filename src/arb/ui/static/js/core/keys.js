/* core/keys.js — scopes, focus and the one key-resolution order.

   THE INVARIANT
   The focused region owns every key. Focus starts and ends at the `ARB>` line
   on every page, so a bare letter is always typing unless you deliberately
   moved focus into a list that visibly says otherwise.

   This exists because the old order called `page.onKey(e)` for EVERY key,
   including bare printables, BEFORE the command line saw them. On /pairs that
   turned the word "RUN" into reload + PROPOSED + REJECTED: two database writes
   from a user who believed they were typing in a search box. The printable
   branch now sits ABOVE the page, so a page can only ever see a letter while
   the focus is inside a `[data-keyregion="list"]` region.

   Scopes are derived from document.activeElement on every keydown:
     TEXT     an <input>/<textarea>/contenteditable — owns every key, always
     LIST     inside [data-keyregion="list"] — owns the page's action keys
     COMMAND  anything else (#cmd, body, a focused button) — owns the buffer */

import { state, selectRelative } from "./state.js";
import { currentPage, navPages, navigate } from "./router.js";
import * as cmd from "./cmd.js";

export const SCOPE = { COMMAND: "command", LIST: "list", TEXT: "text" };

/* ---------- the platform chord: ONE constant drives every label ----------
   Option is the insert-special-character modifier on macOS (Option+1 types
   "¡", Option+[ types a curly quote), so the mac chord is Control. Alt stays
   live as an alias everywhere — it is simply never LABELLED on a Mac. */

// Case-insensitive on purpose: userAgentData.platform reports "macOS" with a
// lower-case m, and it short-circuits navigator.platform ("MacIntel"), so a
// /Mac/ test silently mislabels every Chromium browser on a Mac.
const MAC = /mac|iphone|ipad|ipod/i.test(
  (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent);
export const NAVMOD = MAC ? "ctrlKey" : "altKey";
export const NAVLABEL = MAC ? "CTRL" : "ALT";

const DIGIT_CODES = [
  "Digit1", "Digit2", "Digit3", "Digit4", "Digit5",
  "Digit6", "Digit7", "Digit8", "Digit9",
];

// ---------- scope ----------

/** The scope that owns a key aimed at `t`. */
export function scopeOf(t) {
  if (!t || t === document.body) return SCOPE.COMMAND;
  if (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable) return SCOPE.TEXT;
  if (t.closest && t.closest('[data-keyregion="list"]')) return SCOPE.LIST;
  return SCOPE.COMMAND;
}

/** The scope the keyboard is in right now. */
export function currentScope() {
  return scopeOf(document.activeElement);
}

const scopeSubs = [];
let lastScope = SCOPE.COMMAND;

/** Subscribe to scope changes: fn(scope, prevScope). Returns an unsubscribe.
    A page uses this to disarm a pending bulk decision when focus leaves. */
export function onScopeChange(fn) {
  scopeSubs.push(fn);
  return () => {
    const i = scopeSubs.indexOf(fn);
    if (i >= 0) scopeSubs.splice(i, 1);
  };
}

function syncScope() {
  const s = currentScope();
  const prev = lastScope;
  lastScope = s;
  renderKeys();
  if (s === prev) return;
  for (const fn of scopeSubs.slice()) {
    try { fn(s, prev); } catch (err) { console.error("scope subscriber failed", err); }
  }
}

// ---------- focus helpers (pages call these; never focus() a row) ----------

/** Put the keyboard back on the `ARB>` line. The home position. */
export function focusCommand() {
  const c = document.getElementById("cmd");
  if (c && document.activeElement !== c) c.focus({ preventScroll: true });
  syncScope();
  return !!c;
}

/** Focus an element by id (a list region, a filter box). False if absent. */
export function focusRegion(id) {
  const e = id ? document.getElementById(id) : null;
  if (!e || e.hidden) return false;
  e.focus({ preventScroll: true });
  syncScope();
  return true;
}

/** Focus the current page's list region (its `listRegion` field). */
export function focusList(page) {
  const p = page || currentPage();
  return !!(p && p.listRegion) && focusRegion(p.listRegion);
}

function regionEls(page) {
  const ids = page && Array.isArray(page.regions) ? page.regions : [];
  const out = [];
  for (const id of ids) {
    const e = document.getElementById(id);
    if (e && !e.hidden) out.push(e);
  }
  return out;
}

/** Focus the first region a page declares — its search box, by convention. */
export function focusFirstRegion(page) {
  const regs = regionEls(page || currentPage());
  if (!regs.length) return false;
  regs[0].focus({ preventScroll: true });
  syncScope();
  return true;
}

function firstTextRegion(page) {
  for (const e of regionEls(page)) {
    if (e.tagName === "INPUT" || e.tagName === "TEXTAREA" || e.isContentEditable) return e;
  }
  return null;
}

// ---------- the nav chord ----------

function cycle(d) {
  const list = navPages();
  if (!list.length) return;
  const cur = currentPage();
  let i = cur ? list.findIndex((p) => p.id === cur.id) : -1;
  if (i < 0) i = d > 0 ? -1 : 0;
  const next = list[(i + d + list.length) % list.length];
  navigate(next.path);
}

/** True when this keydown carries the page-navigation modifier. Cmd is never
    it: ⌘[ / ⌘] are the browser's own history controls on macOS. */
function chordHeld(e) {
  if (e.metaKey || e.shiftKey) return false;
  return e[NAVMOD] === true || e.altKey === true;
}

/** Matched on e.code, so the physical digit works whatever the modifier
    would otherwise have typed. Returns false for anything unbound — a spare
    digit past the nav count belongs to the browser, not to a dead binding. */
function navChord(e) {
  if (!chordHeld(e)) return false;
  const di = DIGIT_CODES.indexOf(e.code);
  if (di >= 0) {
    const page = navPages()[di];
    if (!page) return false;
    navigate(page.path);
    return true;
  }
  if (e.code === "BracketLeft") { cycle(-1); return true; }
  if (e.code === "BracketRight") { cycle(1); return true; }
  // History back/forward is deliberately UNBOUND: ⌘[ / ⌘] on macOS and
  // Alt+← / Alt+→ elsewhere are already native.
  return false;
}

// ---------- the keys strip: it reflects the live scope ----------
//
// A page feeds it by exporting `keyHints(scope)`, returning entries shaped
// {k, d} or the string "K LABEL". Nothing about any individual page is
// hardcoded here.

/** The chord label, sized to the nav that actually exists. NAVLABEL is the
    single source: the nav hint, this strip, every footer and help.js all read
    it, because a hand-edited label is how the docs and the keys drift apart. */
export function chordLabel() {
  const n = navPages().length;
  return n > 1 ? NAVLABEL + "+1-" + Math.min(9, n) : NAVLABEL + "+1";
}

const GLOBAL_HINTS = {
  [SCOPE.COMMAND]: (page) => [
    { k: "↑↓", d: page && page.listRegion ? "LIST" : "SELECT" },
    { k: "⏎", d: "RUN" },
    { k: "⌫", d: "EDIT" },
    { k: "ESC", d: "CLEAR" },
    { k: chordLabel(), d: "PAGE" },
  ],
  [SCOPE.LIST]: () => [{ k: "ESC", d: "ARB>" }],
  [SCOPE.TEXT]: () => [{ k: "⏎", d: "DONE" }, { k: "ESC", d: "ARB>" }],
};

const MODE = {
  [SCOPE.COMMAND]: "ARB> COMMAND",
  [SCOPE.LIST]: "LIST KEYS",
  [SCOPE.TEXT]: "TEXT FIELD",
};

const NOTE = {
  [SCOPE.COMMAND]: "TYPE ANYWHERE TO COMMAND",
  [SCOPE.LIST]: "KEYS ACT ON THE LIST",
  [SCOPE.TEXT]: "TYPING IN A FIELD",
};

function normHint(h) {
  if (!h) return null;
  if (typeof h === "string") {
    const s = h.trim();
    if (!s) return null;
    const cut = s.indexOf(" ");
    return cut < 0 ? { k: s, d: "" } : { k: s.slice(0, cut), d: s.slice(cut + 1).trim() };
  }
  if (!h.k) return null;
  return { k: String(h.k), d: h.d == null ? "" : String(h.d) };
}

/** The hints a page publishes for a scope, normalized. Never throws: a page
    with a broken keyHints must not take the strip down with it. */
function pageHints(page, scope) {
  if (!page || typeof page.keyHints !== "function") return [];
  let raw;
  try { raw = page.keyHints(scope); } catch (err) {
    console.error("keyHints failed for page " + (page.id || "?"), err);
    return [];
  }
  if (!Array.isArray(raw)) return [];
  return raw.map(normHint).filter(Boolean);
}

function hintSpan(h) {
  const s = document.createElement("span");
  if (h.k) {
    const b = document.createElement("b");
    b.textContent = h.k;
    s.append(b);
  }
  if (h.d) s.append(document.createTextNode((h.k ? " " : "") + h.d));
  return s;
}

/** Repaint the keys strip and the ARB> mode band for the live scope. Pages
    call this when their own hints change. */
export function renderKeys() {
  const scope = currentScope();
  const page = currentPage();
  const hints = pageHints(page, scope);
  // The mode note is pinned LEFT on purpose: `.keys` is one clipped nowrap
  // line, so whatever sits leftmost is the last thing to be clipped away.
  const mode = document.getElementById("keys-mode");
  if (mode) {
    mode.className = "keys-mode keys-mode-" + scope;
    mode.textContent = MODE[scope] || MODE[SCOPE.COMMAND];
  }
  const slot = document.getElementById("keys-hints");
  if (slot) {
    const out = [];
    const claimed = new Set();
    for (const h of hints) {
      claimed.add(h.k);
      out.push(hintSpan(h));
    }
    // A page that names a key has the final word on what it does. /help binds
    // the arrows to scrolling, so the global "↑↓ SELECT" would be a lie beside
    // its own "↑↓ SCROLL"; drop the global whenever the page claimed the key.
    for (const h of GLOBAL_HINTS[scope](page)) {
      if (!claimed.has(h.k)) out.push(hintSpan(h));
    }
    slot.replaceChildren(...out);
  }
  const note = document.getElementById("keys-note");
  if (note) note.textContent = NOTE[scope] || "";
  const line = document.getElementById("cmd");
  const band = document.getElementById("cmd-mode");
  const on = scope === SCOPE.LIST;
  if (line) line.classList.toggle("scope-list", on);
  if (band) {
    // The cue belongs on the element whose behaviour changed: in LIST scope
    // the ARB> line is a reversed-video band naming the keys that are live.
    band.textContent = on
      ? "LIST" + hints.map((h) => " · " + h.k + (h.d ? " " + h.d : "")).join("")
      : "";
    band.hidden = !on;
  }
}

// ---------- the Escape ladder ----------

function textValue(t) {
  return t && typeof t.value === "string" ? t.value : (t ? t.textContent || "" : "");
}

function escapeLadder(e, scope) {
  if (scope === SCOPE.TEXT) {
    const t = e.target;
    if (textValue(t) !== "") {
      // 1. a field with something in it: clear it and stay put
      if (typeof t.value === "string") {
        t.value = "";
        t.dispatchEvent(new Event("input", { bubbles: true }));
      }
      return true;
    }
    focusCommand();               // 2. an empty field: back to ARB>
    return true;
  }
  if (scope === SCOPE.LIST) {
    focusCommand();               // 3. leaving the list disarms the page
    return true;
  }
  if (cmd.buffer() !== "") {
    cmd.clear();                  // 4. a half-typed command is the innermost thing open
    return true;
  }
  const cur = currentPage();      // 5. ESC CLOSE, as the strip has always promised
  if (cur && cur.id !== "monitor") navigate("/");
  return true;
}

// ---------- resolution ----------

export function onKey(e) {
  // An IME composition owns its own keystrokes; so does anything a page or a
  // field has already claimed.
  if (e.isComposing || e.key === "Dead" || e.keyCode === 229) return;
  if (e.defaultPrevented) return;

  const scope = scopeOf(e.target);

  // 1. The nav chord, in EVERY scope including TEXT. It is digits and brackets
  //    with a modifier, so it cannot collide with editing a field — and the
  //    old code returned before it whenever a filter box had focus, which left
  //    the page shortcuts dead exactly when you most wanted them.
  if (navChord(e)) {
    e.preventDefault();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  const k = e.key;

  // 2. Escape resolves globally, before any page sees it, so a page can never
  //    leave an armed bulk decision behind.
  if (k === "Escape") {
    if (escapeLadder(e, scope)) e.preventDefault();
    return;
  }

  // 3. A real text field owns everything else it is sent.
  if (scope === SCOPE.TEXT) {
    if (k === "Enter" || k === "ArrowDown") {
      const page = currentPage();
      if (page && page.listRegion && focusList(page)) e.preventDefault();
      else if (k === "Enter") { focusCommand(); e.preventDefault(); }
    }
    return;
  }

  // 4. A focused control keeps its own activation keys.
  const t = e.target;
  if (t && (t.tagName === "BUTTON" || t.tagName === "A" || t.tagName === "SELECT")
      && (k === "Enter" || k === " " || k === "Spacebar")) return;

  const printable = k.length === 1 && /^[\x20-\x7e]$/.test(k);
  const page = currentPage();

  // 5. A typed command always wins, in every scope.
  if (k === "Enter" && cmd.buffer().trim() !== "") {
    e.preventDefault();
    cmd.exec();
    return;
  }

  // 6. THE DIRTY BUFFER RULE. Mid-word, every scope: the characters keep
  //    going where the first one went. This is what survives focus moving
  //    into a list halfway through a ticker.
  if (printable && cmd.buffer() !== "") {
    e.preventDefault();
    cmd.push(k);
    return;
  }

  // 7. THE FIX. A bare printable in COMMAND scope is typing, full stop. The
  //    page is never consulted, so a single-letter action cannot fire from
  //    the home position.
  if (printable && scope === SCOPE.COMMAND) {
    e.preventDefault();
    cmd.push(k);
    return;
  }

  // 8. Arrows from the home position move FOCUS into the list rather than the
  //    cursor: row 0 is the next candidate, and you can see where you are.
  if ((k === "ArrowUp" || k === "ArrowDown") && scope === SCOPE.COMMAND
      && page && page.listRegion && focusList(page)) {
    e.preventDefault();
    return;
  }

  // 9. Belt and braces on the destructive keys. A page's action letters are
  //    G2/G3 writes, and a leaned-on key must not POST once per autorepeat —
  //    pairs.js walks its cursor after each decision, so a held key would
  //    march down the queue. Pages guard this themselves; this makes the
  //    guarantee structural rather than per-page discipline.
  if (printable && e.repeat && scope === SCOPE.LIST) {
    e.preventDefault();
    return;
  }

  // 10. The page. It sees printables ONLY in LIST scope.
  if (page && typeof page.onKey === "function" && page.onKey(e, scope)) {
    e.preventDefault();
    return;
  }

  // 11. "/" opens the page's search box from a list, like every pager.
  if (k === "/" && scope === SCOPE.LIST) {
    const box = firstTextRegion(page);
    if (box) {
      box.focus({ preventScroll: true });
      syncScope();
      e.preventDefault();
      return;
    }
  }

  // 12. Global non-printables.
  if (k === "Tab") {
    if (tabStep(e, page, scope)) e.preventDefault();
    return;
  }
  if (k === "ArrowUp" || k === "ArrowDown") {
    e.preventDefault();
    selectRelative(k === "ArrowUp" ? -1 : 1);
    return;
  }
  if (k === "Enter") {
    e.preventDefault();
    if (state.selectedId) navigate("/market/" + encodeURIComponent(state.selectedId));
    return;
  }
  if (k === "Backspace") {
    e.preventDefault();
    cmd.backspace();
    return;
  }

  // 13. LIST fall-through: an unclaimed printable leaves the list and THEN
  //     types. Typing KXPRES into a focused list gets you KXPRES, not six
  //     silently-swallowed keystrokes.
  if (printable && scope === SCOPE.LIST) {
    e.preventDefault();
    focusCommand();
    cmd.push(k);
  }
}

function tabStep(e, page, scope) {
  const regs = regionEls(page);
  if (!regs.length) return focusList(page);
  const i = regs.indexOf(document.activeElement);
  const from = i < 0 ? (e.shiftKey ? regs.length : -1) : i;
  const next = from + (e.shiftKey ? -1 : 1);
  if (next < 0 || next >= regs.length) return scope === SCOPE.COMMAND ? false : focusCommand();
  regs[next].focus({ preventScroll: true });
  syncScope();
  return true;
}

/** Bind the document listeners. Called once by main.js. */
export function install() {
  document.addEventListener("keydown", onKey);
  document.addEventListener("focusin", syncScope);
  document.addEventListener("focusout", () => setTimeout(syncScope, 0));
  // Clicking a row selects it; it does NOT hand the row the keyboard. Anything
  // that writes has to be reached deliberately, and the mouse route to a
  // decision is a button, not a letter that fires because you clicked nearby.
  document.addEventListener("click", (e) => {
    if (e.detail === 0) return;                       // keyboard-synthesised click
    const a = document.activeElement;
    if (a && a.closest && a.closest('[data-keyregion="list"]')) focusCommand();
  });
  // Every navigation lands on the ARB> line, so the strip has to follow.
  document.addEventListener("arb:navigate", syncScope);
  const hint = document.getElementById("nav-hint");
  if (hint) hint.textContent = chordLabel() + " PAGE · " + NAVLABEL + "+[ ] CYCLE";
  syncScope();
}
