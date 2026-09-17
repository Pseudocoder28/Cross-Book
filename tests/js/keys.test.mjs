/* tests/js/keys.test.mjs — core/keys.js, the one key-resolution order.

   THE BUG THIS SUITE EXISTS FOR
   On /pairs, typing the word "RUN" with nothing focused used to reload the
   list (R) and then write TWO pair decisions to Postgres (PROPOSED, REJECTED),
   because the old order called `page.onKey(e)` for EVERY key before the ARB>
   command line saw it. The fix put the printable branch above the page. That
   is pure resolution-order logic — no layout, no CSS — so it is exactly what a
   unit test can hold down, and `pages must never see a bare printable in
   COMMAND scope` is asserted here from several directions.

   WHAT IS REAL AND WHAT IS A STUB
   Real: core/keys.js, core/cmd.js, core/router.js, core/state.js — the whole
   import graph, unmocked, with the real command buffer and the real router.
   Stub: the DOM (see tests/js/shim.mjs). Focus here is "whatever last called
   focus()", which is enough for scope resolution and nothing more. Anything
   that depends on the browser TAKING focus away — a node being hidden or
   replaced under it — is out of scope for this layer by construction.

   `install()` is never called: the shim does not propagate events, so every
   test drives `onKey(e)` directly, which is also the seam the bug lived in. */

import test from "node:test";
import assert from "node:assert/strict";
import { jsUrl, mkEl, keydown, setNavigator, documentStub, locationStub } from "./shim.mjs";

const cmd = await import(jsUrl("core/cmd.js"));
const router = await import(jsUrl("core/router.js"));
const { state } = await import(jsUrl("core/state.js"));

/* keys.js reads `navigator` ONCE, at module evaluation, to pick the nav
   modifier. A query string gives each platform its own module instance; its
   imports (cmd, router, state) still resolve to the single cached copy above,
   so the instances share one command buffer and one router. */
let plat = 0;
async function loadKeys(nav) {
  setNavigator(nav);
  plat += 1;
  return import(jsUrl("core/keys.js") + "?platform=" + plat);
}

const keys = await loadKeys({ platform: "Win32", userAgent: "Mozilla/5.0 (X11; Linux x86_64)" });
const keysMac = await loadKeys({ userAgentData: { platform: "macOS" }, platform: "MacIntel" });

// ---------- the shell core/keys.js and core/cmd.js write into ----------

const cmdLine = mkEl("div", { id: "cmd", attrs: { "data-keyregion": "command", tabindex: "0" } });
mkEl("span", { id: "cmd-text", parent: cmdLine });
const cmdMode = mkEl("span", { id: "cmd-mode", parent: cmdLine, hidden: true });
mkEl("span", { id: "cmd-msg", parent: cmdLine });
mkEl("span", { id: "keys-mode" });
const keysHints = mkEl("span", { id: "keys-hints" });
mkEl("span", { id: "keys-note" });

// ---------- pages ----------

const pairRows = mkEl("div", { id: "pair-rows", attrs: { "data-keyregion": "list" } });
const pairRow = mkEl("div", { parent: pairRows, attrs: { "data-row": "0" } }); // a row inside the list
const pairQ = mkEl("input", { id: "pair-q" });
const plainDiv = mkEl("div", { id: "just-a-div" });
const someButton = mkEl("button", { id: "a-button" });

function makePage(id, path, opts = {}) {
  const p = {
    id,
    path,
    root: id + "-page",
    title: id.toUpperCase(),
    nav: opts.nav !== false,
    listRegion: opts.listRegion,
    regions: opts.regions,
    seen: [],
    claim: opts.claim || (() => false), // which keys this page says it handled
    mount() {},
    unmount() {},
    render() {},
    keyHints: opts.keyHints,
    onKey(e, scope) {
      p.seen.push({ key: e.key, scope });
      return p.claim(e, scope);
    },
  };
  mkEl("section", { id: p.root });
  return p;
}

const monitor = makePage("monitor", "/");
const arb = makePage("arb", "/arb");
const pairs = makePage("pairs", "/pairs", {
  listRegion: "pair-rows",
  regions: ["pair-q", "pair-rows"],
  // /pairs really does bind bare letters: R reloads, P proposes, X rejects.
  claim: (e, scope) => scope === "list" && "RPX".includes(e.key.toUpperCase()),
  keyHints: () => [{ k: "P", d: "PROPOSE" }, "X REJECT"],
});
const paper = makePage("paper", "/paper");
const market = makePage("market", "/market/:id", { nav: false });
const bare = makePage("bare", "/bare", { nav: false }); // no listRegion, no regions

for (const p of [monitor, arb, pairs, paper, market, bare]) router.register(p);

/** Land on `path` in the home position with an empty buffer. */
function at(path) {
  for (const p of [monitor, arb, pairs, paper, market, bare]) p.seen.length = 0;
  cmd.clear();
  router.navigate(path);
  keys.focusCommand();
  assert.equal(keys.currentScope(), keys.SCOPE.COMMAND);
}

function press(key, opts) {
  const e = keydown(key, opts);
  keys.onKey(e);
  return e;
}

// ================= scope =================

test("scopeOf derives the scope from the target, not from a mode flag", () => {
  assert.equal(keys.scopeOf(null), keys.SCOPE.COMMAND);
  assert.equal(keys.scopeOf(documentStub.body), keys.SCOPE.COMMAND);
  assert.equal(keys.scopeOf(cmdLine), keys.SCOPE.COMMAND, "the ARB> line is COMMAND, not LIST");
  assert.equal(keys.scopeOf(plainDiv), keys.SCOPE.COMMAND);
  assert.equal(keys.scopeOf(someButton), keys.SCOPE.COMMAND, "a focused button still types");
  assert.equal(keys.scopeOf(pairQ), keys.SCOPE.TEXT);
  assert.equal(keys.scopeOf(pairRows), keys.SCOPE.LIST);
  assert.equal(keys.scopeOf(pairRow), keys.SCOPE.LIST, "a row inside the region is LIST");
  const ce = mkEl("div", { isContentEditable: true });
  assert.equal(keys.scopeOf(ce), keys.SCOPE.TEXT);
});

// ================= 1. THE SHIPPED BUG =================

test('THE "RUN" BUG: a bare word in COMMAND scope never reaches the page', () => {
  at("/pairs");
  for (const ch of "RUN") {
    const e = press(ch, { target: cmdLine });
    assert.equal(e.prevented, 1, ch + " must be swallowed by the command line");
  }
  assert.equal(cmd.buffer(), "RUN", "the letters went to the ARB> buffer");
  assert.deepEqual(pairs.seen, [], "pages/pairs.js was NEVER consulted: no reload, no writes");
});

test("the same word typed at <body> (nothing focused at all) also types", () => {
  at("/pairs");
  for (const ch of "RUN") press(ch, { target: documentStub.body });
  assert.equal(cmd.buffer(), "RUN");
  assert.deepEqual(pairs.seen, []);
});

test("a single destructive letter from the home position is typing, not a write", () => {
  for (const ch of ["P", "X", "R"]) {
    at("/pairs");
    press(ch, { target: cmdLine });
    assert.equal(cmd.buffer(), ch);
    assert.deepEqual(pairs.seen, [], ch + " must not reach the page from COMMAND scope");
  }
});

test("the buffer uppercases, so a lower-case letter is a ticker keystroke", () => {
  at("/pairs");
  press("r", { target: cmdLine });
  assert.equal(cmd.buffer(), "R");
  assert.deepEqual(pairs.seen, []);
});

// ================= 2. LIST scope: the page DOES get its letters =================

test("a printable in LIST scope reaches the page, with the scope as 2nd arg", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  assert.equal(keys.currentScope(), keys.SCOPE.LIST);
  const e = press("P", { target: pairRows });
  assert.deepEqual(pairs.seen, [{ key: "P", scope: "list" }]);
  assert.equal(e.prevented, 1, "a claimed key is consumed");
  assert.equal(cmd.buffer(), "", "a claimed key does not also type");
});

test("a key aimed at a ROW inside the list region resolves to the page too", () => {
  at("/pairs");
  pairRow.focus();
  const e = press("X", { target: pairRow });
  assert.deepEqual(pairs.seen, [{ key: "X", scope: "list" }]);
  assert.equal(e.prevented, 1);
});

test("non-printables still reach the page in COMMAND scope", () => {
  // Only PRINTABLES are withheld from a page in COMMAND scope; a page that
  // binds PageDown or F5 keeps working from the home position.
  at("/bare");
  press("PageDown", { target: cmdLine });
  assert.deepEqual(bare.seen, [{ key: "PageDown", scope: "command" }]);
});

// ================= 3. the dirty-buffer rule =================

test("a dirty buffer keeps typing after focus moves into a list mid-word", () => {
  at("/pairs");
  press("K", { target: cmdLine });
  press("X", { target: cmdLine });
  keys.focusRegion("pair-rows"); // the user tabs/clicks into the list mid-ticker
  assert.equal(keys.currentScope(), keys.SCOPE.LIST);
  const e = press("P", { target: pairRows }); // P is a key /pairs BINDS
  assert.equal(e.prevented, 1);
  assert.equal(cmd.buffer(), "KXP", "the characters keep going where the first one went");
  assert.deepEqual(pairs.seen, [], "a bound letter must not fire mid-word");
});

test("the dirty-buffer rule outranks the page in COMMAND scope as well", () => {
  at("/pairs");
  cmd.setBuffer("KX");
  press("R", { target: cmdLine });
  assert.equal(cmd.buffer(), "KXR");
  assert.deepEqual(pairs.seen, []);
});

test("a real text field owns its keys even with a dirty buffer", () => {
  // Rule 3 (TEXT owns everything) sits ABOVE the dirty-buffer rule: once the
  // keyboard is inside a filter box the box gets the characters, and the
  // half-typed ARB> buffer is left exactly as it was.
  at("/pairs");
  cmd.setBuffer("KX");
  pairQ.focus();
  const e = press("Z", { target: pairQ });
  assert.equal(e.prevented, 0, "the browser must be left to insert the character");
  assert.equal(cmd.buffer(), "KX", "untouched, not appended to");
  assert.deepEqual(pairs.seen, []);
});

// ================= 4. the LIST fall-through =================

test("an unclaimed printable in LIST blurs to COMMAND and THEN types", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = press("K", { target: pairRows }); // K is not one of /pairs' letters
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, cmdLine, "focus came home");
  assert.equal(keys.currentScope(), keys.SCOPE.COMMAND);
  assert.equal(cmd.buffer(), "K", "the keystroke is typed, not swallowed");
});

test("typing a whole ticker into a focused list gets the whole ticker", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  // Aimed where the browser would aim it: at whatever currently has focus.
  for (const ch of "KXPRES") press(ch, { target: documentStub.activeElement });
  assert.equal(cmd.buffer(), "KXPRES");
  // Only the FIRST character was offered to the page (and it declined); after
  // that the buffer is dirty and the page is never asked again.
  assert.deepEqual(pairs.seen, [{ key: "K", scope: "list" }]);
});

// ================= 5. e.repeat =================

test("a held printable in LIST is dropped, so a leaned-on key cannot POST twice", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = press("P", { target: pairRows, repeat: true });
  assert.equal(e.prevented, 1, "consumed");
  assert.deepEqual(pairs.seen, [], "autorepeat must never reach a G2/G3 write");
  assert.equal(cmd.buffer(), "", "and it must not fall through into the buffer either");
});

test("the repeat guard does not block autorepeat typing at the ARB> line", () => {
  at("/pairs");
  press("K", { target: cmdLine, repeat: true });
  assert.equal(cmd.buffer(), "K", "holding a key while typing a ticker still types");
});

test("autorepeat on a non-printable in LIST is left alone", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  press("ArrowDown", { target: pairRows, repeat: true });
  assert.deepEqual(pairs.seen, [{ key: "ArrowDown", scope: "list" }], "arrow repeat still scrolls");
});

// ================= 6. the Escape ladder, rung by rung =================

test("ESC rung 1: a field with something in it is cleared and KEEPS focus", () => {
  at("/pairs");
  pairQ.value = "KXPRES";
  pairQ.focus();
  let inputs = 0;
  pairQ.addEventListener("input", () => {
    inputs += 1;
  });
  const e = press("Escape", { target: pairQ });
  assert.equal(e.prevented, 1);
  assert.equal(pairQ.value, "");
  assert.equal(inputs, 1, "an input event fires so the page re-filters");
  assert.equal(documentStub.activeElement, pairQ, "you stay in the field");
});

test("ESC rung 2: an EMPTY field hands the keyboard back to ARB>", () => {
  at("/pairs");
  pairQ.value = "";
  pairQ.focus();
  const e = press("Escape", { target: pairQ });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, cmdLine);
  assert.equal(keys.currentScope(), keys.SCOPE.COMMAND);
});

test("ESC rung 3: leaving a list disarms the page", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = press("Escape", { target: pairRows });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, cmdLine);
  assert.deepEqual(pairs.seen, [], "Escape resolves BEFORE the page, always");
});

test("ESC rung 4: a half-typed command is the innermost thing open", () => {
  at("/arb");
  cmd.setBuffer("KXPRE");
  const e = press("Escape", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(cmd.buffer(), "");
  assert.equal(router.currentPage().id, "arb", "clearing the buffer does NOT also close the page");
});

test("ESC rung 5: an empty command line on a page closes it to MONITOR", () => {
  at("/arb");
  const e = press("Escape", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "monitor");
});

test("ESC rung 5 on MONITOR is a no-op: there is nowhere further to go", () => {
  at("/");
  const e = press("Escape", { target: cmdLine });
  assert.equal(e.prevented, 1, "still consumed, so the browser does nothing either");
  assert.equal(router.currentPage().id, "monitor");
  assert.equal(locationStub.pathname, "/");
});

test("the whole ladder in order from one starting position", () => {
  at("/arb");
  pairQ.value = "AB";
  pairQ.focus();
  press("Escape", { target: pairQ });
  assert.equal(pairQ.value, "");
  press("Escape", { target: pairQ });
  assert.equal(documentStub.activeElement, cmdLine);
  cmd.setBuffer("KX");
  press("Escape", { target: cmdLine });
  assert.equal(cmd.buffer(), "");
  press("Escape", { target: cmdLine });
  assert.equal(router.currentPage().id, "monitor");
});

// ================= 7. Enter runs the command, in every scope =================

test("Enter with a non-empty buffer runs the command from COMMAND scope", () => {
  at("/");
  cmd.setBuffer("PAIRS");
  const e = press("Enter", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "pairs");
  assert.equal(cmd.buffer(), "", "exec empties the buffer");
});

test("Enter with a non-empty buffer runs the command from LIST scope too", () => {
  at("/");
  cmd.setBuffer("ARB"); // typed while the focus sat in a list, per the dirty-buffer rule
  keys.focusRegion("pair-rows");
  const e = press("Enter", { target: pairRows });
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "arb");
  assert.deepEqual(pairs.seen, [], "the page never saw the Enter");
  assert.equal(documentStub.activeElement, cmdLine, "a command always ends on the ARB> line");
});

test("a whitespace-only buffer is not a command", () => {
  at("/arb");
  cmd.setBuffer("   ");
  press("Enter", { target: cmdLine });
  assert.equal(router.currentPage().id, "arb");
});

test("Enter with an empty buffer opens DES for the selection instead", () => {
  at("/");
  state.markets = [{ market_id: "M-1", ticker: "KXA" }];
  state.byId = new Map([["M-1", state.markets[0]]]);
  state.selectedId = "M-1";
  const e = press("Enter", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "market");
  assert.equal(locationStub.pathname, "/market/M-1");
});

test("a focused BUTTON keeps Enter and Space as its own activation keys", () => {
  at("/pairs");
  someButton.focus();
  for (const k of ["Enter", " "]) {
    const e = press(k, { target: someButton });
    assert.equal(e.prevented, 0, JSON.stringify(k) + " belongs to the button");
  }
  assert.deepEqual(pairs.seen, []);
});

// ================= 8. arrows, Tab, Backspace, "/" =================

test("an arrow from the home position moves FOCUS into the list", () => {
  at("/pairs");
  const e = press("ArrowDown", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, pairRows);
  assert.equal(keys.currentScope(), keys.SCOPE.LIST);
});

test("on a page with no list, an arrow walks the market selection", () => {
  at("/bare");
  state.markets = [
    { market_id: "A", ticker: "A" },
    { market_id: "B", ticker: "B" },
  ];
  state.byId = new Map(state.markets.map((m) => [m.market_id, m]));
  state.selectedId = "A";
  const e = press("ArrowDown", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(state.selectedId, "B");
  press("ArrowUp", { target: cmdLine });
  assert.equal(state.selectedId, "A");
});

test("Backspace edits the command buffer", () => {
  at("/arb");
  cmd.setBuffer("KXP");
  const e = press("Backspace", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(cmd.buffer(), "KX");
});

test('"/" from a list opens the page\'s first text region', () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = press("/", { target: pairRows });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, pairQ);
  assert.equal(keys.currentScope(), keys.SCOPE.TEXT);
});

test('"/" from the home position is a character, not a search shortcut', () => {
  at("/pairs");
  press("/", { target: cmdLine });
  assert.equal(cmd.buffer(), "/");
  assert.equal(documentStub.activeElement, cmdLine);
});

test("Tab from the home position enters the page's first declared region", () => {
  at("/pairs");
  const e = press("Tab", { target: cmdLine });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, pairQ, "first region, the filter box");
});

test("Tab inside a TEXT field is the browser's, because TEXT owns everything", () => {
  at("/pairs");
  pairQ.focus();
  const e = press("Tab", { target: pairQ });
  assert.equal(e.prevented, 0, "rule 3 returns before tabStep for a real field");
});

test("Tab past the last region from a LIST comes home to ARB>", () => {
  at("/pairs");
  keys.focusRegion("pair-rows"); // the last of ["pair-q", "pair-rows"]
  const e = press("Tab", { target: pairRows });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, cmdLine);
});

test("Shift+Tab from a LIST steps back to the region before it", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = press("Tab", { target: pairRows, shiftKey: true });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, pairQ);
});

test("Shift+Tab from the home position enters the regions from the end", () => {
  at("/pairs");
  press("Tab", { target: cmdLine, shiftKey: true });
  assert.equal(documentStub.activeElement, pairRows);
});

test("Tab in COMMAND scope past the end is left to the browser", () => {
  at("/bare"); // no regions at all, and no listRegion either
  const e = press("Tab", { target: cmdLine });
  assert.equal(e.prevented, 0, "the browser's own tab order must still work");
});

// ================= 9. TEXT scope ownership =================

test("a text field owns every printable, and the page is never told", () => {
  at("/pairs");
  pairQ.value = "";
  pairQ.focus();
  for (const ch of "KXP") {
    const e = press(ch, { target: pairQ });
    assert.equal(e.prevented, 0);
  }
  assert.equal(cmd.buffer(), "");
  assert.deepEqual(pairs.seen, []);
});

test("Enter in a filter box hands off to the list it filters", () => {
  at("/pairs");
  pairQ.focus();
  const e = press("Enter", { target: pairQ });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, pairRows);
});

test("Enter in a filter box on a listless page goes home instead", () => {
  at("/bare");
  pairQ.focus();
  const e = press("Enter", { target: pairQ });
  assert.equal(e.prevented, 1);
  assert.equal(documentStub.activeElement, cmdLine);
});

test("ArrowDown in a filter box steps into the list without leaving text behind", () => {
  at("/pairs");
  pairQ.value = "KX";
  pairQ.focus();
  press("ArrowDown", { target: pairQ });
  assert.equal(documentStub.activeElement, pairRows);
  assert.equal(pairQ.value, "KX", "the filter is not cleared by stepping into the list");
});

// ================= 10. the nav chord =================

test("mac: CTRL is the nav modifier and ALT stays live as an alias", () => {
  assert.equal(keysMac.NAVMOD, "ctrlKey");
  assert.equal(keysMac.NAVLABEL, "CTRL");
  at("/");
  const e = keydown("2", { code: "Digit2", ctrlKey: true, target: cmdLine });
  keysMac.onKey(e);
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "arb");
  at("/");
  const alt = keydown("2", { code: "Digit2", altKey: true, target: cmdLine });
  keysMac.onKey(alt);
  assert.equal(router.currentPage().id, "arb", "ALT is an alias everywhere, just never labelled");
});

test("non-mac: ALT navigates and a bare CTRL+digit is NOT the chord", () => {
  assert.equal(keys.NAVMOD, "altKey");
  assert.equal(keys.NAVLABEL, "ALT");
  at("/");
  const e = keydown("2", { code: "Digit2", altKey: true, target: cmdLine });
  keys.onKey(e);
  assert.equal(router.currentPage().id, "arb");
  at("/");
  const ctl = keydown("2", { code: "Digit2", ctrlKey: true, target: cmdLine });
  keys.onKey(ctl);
  assert.equal(ctl.prevented, 0, "CTRL+2 is the browser's tab switcher off a Mac");
  assert.equal(router.currentPage().id, "monitor");
});

test("the chord is matched on e.code, whatever the modifier would have typed", () => {
  // Option+3 types "£" on a Mac keyboard; the physical digit still navigates.
  at("/");
  const e = keydown("£", { code: "Digit3", altKey: true, target: cmdLine });
  keysMac.onKey(e);
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "pairs");
  assert.equal(cmd.buffer(), "", "and it certainly does not type the £");
});

test("a spare digit past the nav count returns false rather than preventDefault", () => {
  assert.equal(router.navPages().length, 4);
  at("/arb");
  const e = keydown("9", { code: "Digit9", ctrlKey: true, target: cmdLine });
  keysMac.onKey(e);
  assert.equal(e.prevented, 0, "an unbound digit belongs to the browser, not to a dead binding");
  assert.equal(router.currentPage().id, "arb");
});

test("the chord cycles with [ and ], and wraps in both directions", () => {
  const order = router.navPages().map((p) => p.id);
  assert.deepEqual(order, ["monitor", "arb", "pairs", "paper"]);
  at("/");
  keysMac.onKey(keydown("]", { code: "BracketRight", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "arb");
  keysMac.onKey(keydown("[", { code: "BracketLeft", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "monitor");
  keysMac.onKey(keydown("[", { code: "BracketLeft", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "paper", "wraps backwards off the front");
  keysMac.onKey(keydown("]", { code: "BracketRight", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "monitor", "wraps forwards off the end");
});

test("cycling from a non-nav page enters the nav strip at its ends", () => {
  at("/market/M-9");
  keysMac.onKey(keydown("]", { code: "BracketRight", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "monitor");
  at("/market/M-9");
  keysMac.onKey(keydown("[", { code: "BracketLeft", ctrlKey: true, target: cmdLine }));
  assert.equal(router.currentPage().id, "paper");
});

test("the history chords are NOT bound: they stay the browser's own", () => {
  at("/arb");
  // macOS: Cmd+[ / Cmd+] are back/forward.
  for (const code of ["BracketLeft", "BracketRight"]) {
    const e = keydown(code === "BracketLeft" ? "[" : "]", { code, metaKey: true, target: cmdLine });
    keysMac.onKey(e);
    assert.equal(e.prevented, 0, "Cmd+" + code + " is history, not page nav");
  }
  // And Cmd held TOGETHER with the nav modifier is still history. Without
  // this case the loop above passes even with the `e.metaKey` guard deleted
  // from chordHeld, because a bare Cmd+[ carries no ctrl/alt to match on.
  const both = keydown("[", { code: "BracketLeft", metaKey: true, ctrlKey: true, target: cmdLine });
  keysMac.onKey(both);
  assert.equal(both.prevented, 0, "Cmd+CTRL+[ is not page nav either");
  // elsewhere: Alt+Left / Alt+Right are back/forward.
  for (const code of ["ArrowLeft", "ArrowRight"]) {
    const e = keydown(code, { code, altKey: true, target: cmdLine });
    keys.onKey(e);
    assert.equal(e.prevented, 0, "Alt+" + code + " is history, not page nav");
  }
  assert.equal(router.currentPage().id, "arb");
});

test("Shift cancels the chord: CTRL+SHIFT+1 is not page 1", () => {
  at("/arb");
  const e = keydown("!", { code: "Digit1", ctrlKey: true, shiftKey: true, target: cmdLine });
  keysMac.onKey(e);
  assert.equal(e.prevented, 0);
  assert.equal(router.currentPage().id, "arb");
});

test("the chord fires from inside a TEXT field, where you most want it", () => {
  at("/");
  pairQ.value = "KX";
  pairQ.focus();
  const e = keydown("3", { code: "Digit3", ctrlKey: true, target: pairQ });
  keysMac.onKey(e);
  assert.equal(e.prevented, 1);
  assert.equal(router.currentPage().id, "pairs");
});

test("the chord fires from inside a LIST without the page seeing it", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  const e = keydown("4", { code: "Digit4", ctrlKey: true, target: pairRows });
  keysMac.onKey(e);
  assert.equal(router.currentPage().id, "paper");
  assert.deepEqual(pairs.seen, []);
});

test("chordLabel is sized to the nav that actually exists", () => {
  assert.equal(keysMac.chordLabel(), "CTRL+1-4");
  assert.equal(keys.chordLabel(), "ALT+1-4");
});

// ================= 11. the platform regex =================

test("the platform test accepts macOS AND MacIntel, case-insensitively", async () => {
  // Both spellings shipped: navigator.userAgentData.platform reports "macOS"
  // with a lower-case m, and a case-SENSITIVE /Mac/ once mislabelled every
  // Chromium browser on a Mac as ALT while CTRL was what actually worked.
  const cases = [
    [{ userAgentData: { platform: "macOS" }, platform: "" }, "CTRL"],
    [{ platform: "MacIntel" }, "CTRL"],
    [{ platform: "iPhone" }, "CTRL"],
    [{ platform: "iPad" }, "CTRL"],
    [{ platform: "", userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" }, "CTRL"],
    [{ platform: "Win32" }, "ALT"],
    [{ platform: "Linux x86_64" }, "ALT"],
    [{ userAgentData: { platform: "Windows" }, platform: "Win32" }, "ALT"],
  ];
  for (const [nav, want] of cases) {
    const m = await loadKeys(nav);
    assert.equal(m.NAVLABEL, want, JSON.stringify(nav));
    assert.equal(m.NAVMOD, want === "CTRL" ? "ctrlKey" : "altKey");
  }
});

test("userAgentData short-circuits navigator.platform, so it must be read first", async () => {
  // A Chromium Mac reports userAgentData.platform "macOS" while
  // navigator.platform can be anything; reading only .platform is how the
  // label and the live binding drift apart.
  const m = await loadKeys({ userAgentData: { platform: "macOS" }, platform: "Win32" });
  assert.equal(m.NAVLABEL, "CTRL");
});

// ================= 12. the guards at the top of onKey =================

test("an IME composition owns its own keystrokes", () => {
  at("/pairs");
  for (const opts of [{ isComposing: true }, { keyCode: 229 }]) {
    const e = press("a", { target: cmdLine, ...opts });
    assert.equal(e.prevented, 0);
  }
  press("Dead", { target: cmdLine });
  assert.equal(cmd.buffer(), "", "nothing reached the buffer");
  assert.deepEqual(pairs.seen, []);
});

test("a key something else already claimed is left alone", () => {
  at("/pairs");
  const e = press("R", { target: cmdLine, defaultPrevented: true });
  assert.equal(e.prevented, 0);
  assert.equal(cmd.buffer(), "");
  assert.deepEqual(pairs.seen, []);
});

test("a bare modifier keydown types nothing and navigates nowhere", () => {
  at("/arb");
  for (const k of ["Control", "Alt", "Shift", "Meta"]) press(k, { target: cmdLine });
  assert.equal(cmd.buffer(), "");
  assert.equal(router.currentPage().id, "arb");
});

test("CTRL+C is left to the clipboard, not typed", () => {
  at("/arb");
  const e = keydown("c", { code: "KeyC", ctrlKey: true, target: cmdLine });
  keys.onKey(e); // non-mac instance: ctrl is not the nav modifier here
  assert.equal(e.prevented, 0);
  assert.equal(cmd.buffer(), "");
});

// ================= 13. the keys strip reflects the live scope =================

test("the LIST band names the page's own keys, and empties in COMMAND scope", () => {
  at("/pairs");
  keys.focusRegion("pair-rows");
  keys.renderKeys();
  assert.equal(cmdMode.hidden, false);
  assert.equal(cmdMode.textContent, "LIST · P PROPOSE · X REJECT");
  assert.equal(documentStub.getElementById("keys-mode").textContent, "LIST KEYS");
  keys.focusCommand();
  assert.equal(cmdMode.hidden, true);
  assert.equal(cmdMode.textContent, "");
  assert.equal(documentStub.getElementById("keys-mode").textContent, "ARB> COMMAND");
});

test("a page whose keyHints throws does not take the strip down", () => {
  const broken = makePage("broken", "/broken", {
    nav: false,
    keyHints: () => {
      throw new Error("boom");
    },
  });
  router.register(broken);
  at("/broken");
  keys.renderKeys();
  assert.ok(keysHints.children.length > 0, "the global hints still render");
});

test("onScopeChange fires on a real change and not on a repeat", () => {
  at("/pairs");
  const seen = [];
  const off = keys.onScopeChange((s, prev) => seen.push(prev + "->" + s));
  keys.focusRegion("pair-rows");
  keys.focusRegion("pair-rows");
  keys.focusCommand();
  off();
  keys.focusRegion("pair-rows");
  assert.deepEqual(seen, ["command->list", "list->command"]);
  keys.focusCommand();
});
