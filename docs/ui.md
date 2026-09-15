# Terminal UI

Code: backend [`src/arb/ui/server.py`](../src/arb/ui/server.py), frontend
[`src/arb/ui/static/`](../src/arb/ui/static/). Served by `arb ui` at
`http://127.0.0.1:8080` by default (see [`cli.md`](cli.md#arb-ui)).

The terminal is a multi-page app: seven routes, a visible nav strip, real
bookmarkable URLs, and **one** WebSocket for the whole session. It was a single
page whose screens were hidden modal overlays until the restructure; the
1886-line `static/app.js` IIFE that held all of it is deleted. Where this file
says *the pre-multipage source*, it means that file in git history — it is
still the reference for anything that was ported verbatim, and several page
modules name the functions they came from.

## Design intent

A black/amber Bloomberg-terminal-style desk for the whole system: live market
monitor, a depth ladder per market, a tape of changes, a latency panel, pair
review, the ARB screen, the paper-trading ledger, a diagnostics screen and a
reference card — keyboard-driven, no mouse required (though clicks, drags and
selections all work). The aesthetic was chosen by a judged three-way design
panel (a Bloomberg purist, a modern-desk take, and a density-maximalist take);
the black/amber/tabular-nums look won.

Two rules survived the restructure unchanged and explain most of what follows:
a Python repo does not grow an npm toolchain for one page, and a slow browser
never stalls ingest.

## `arb ui` does everything `arb record` does, plus rendering

`run_ui()` mirrors `record.run_record()`'s wiring — recorder-first ingest
(`record_raw()` enqueues before any parsing, the same hard rule as everywhere
else), every long-running loop under `supervise()` in its own task, one venue's
failure never stopping another. On top of that it:

- Parses every frame through the venue adapters into a shared `BookManager`.
- Optionally loads confirmed pairs into an `ArbMonitor` (`--pairs-top`, default
  10; `pairs/tracked.py` — see [`pairs.md`](pairs.md)) and optionally a
  `PaperTrader` on top of that (`--paper`). Both are built **once at startup**,
  so a pair confirmed mid-run reaches the ARB screen only after a restart.
- Pushes JSON frames to every connected browser over a WebSocket.
- Serves `GET /metrics` itself — `arb ui` does **not** start the separate
  Prometheus HTTP server that `arb record` does; there's one HTTP server, one
  port, and the browser's own SYSTEM page links to it.

`--no-record` disables the Postgres write path entirely (useful for a DB-less
demo) without touching anything else — books, the ARB screen and paper trading
(over live quotes) still work.

## Frontend layout: ES modules, no build step

```
src/arb/ui/static/
  index.html          the shell: status bar, nav, ARB> line, one root element per page
  style.css           base, shell, and every SHARED component class
                      (.panel .kv .des-* .sys-title .quiet-line .mon-row ...)
  css/<page>.css      styles for exactly one page, loaded after style.css
  js/main.js          entry point: shell, page registration, select-to-copy
  js/core/            state.js format.js dom.js ws.js router.js keys.js cmd.js
  js/pages/           monitor.js market.js arb.js pairs.js paper.js system.js help.js
```

There is no bundler, no transpiler, no `package.json` and no `node_modules`:
`index.html` ends in a single `<script type="module" src="/static/js/main.js">`
and the modules import each other by relative path with explicit `.js`
extensions, which is exactly what a browser's own module loader wants. FastAPI's
`StaticFiles` serves the directory as it is on disk. The cost of the restructure
was therefore zero new tooling — the same reason the UI was three plain files
before is the reason it is two dozen plain files now.

Stylesheets are split the same way the JS is: `style.css` owns the shell and
every class more than one page uses, and `css/<page>.css` owns only that page's
additions. `index.html` links all seven page stylesheets up front (they are
small and the page count is fixed), and every page module except the monitor's
*also* injects its own `<link>`, guarded by a `document.querySelector` check —
idempotent either way, and the reason a page is never served unstyled if its
module lands before the shell learns about it.

## Routes, and why routing is client side

| Path | Page id | Nav | Module | Root element |
| --- | --- | --- | --- | --- |
| `/` | `monitor` | `1` MONITOR | `pages/monitor.js` | `#monitor-page` |
| `/arb` | `arb` | `2` ARB | `pages/arb.js` | `#arbpage` |
| `/pairs` | `pairs` | `3` PAIRS | `pages/pairs.js` | `#pairs` |
| `/paper` | `paper` | `4` PAPER | `pages/paper.js` | `#paperpage` |
| `/system` | `system` | `5` SYSTEM | `pages/system.js` | `#system-page` |
| `/help` | `help` | `6` HELP | `pages/help.js` | `#help-page` |
| `/market/<market_id>` | `market` | not in the nav | `pages/market.js` | `#des` |

Navigation is client side over the History API, not one HTML document per page,
and the reason is the socket. The session's entire working set — the tape, the
120-second latency window, every book, the ARB snapshot, the paper ledger — is
pushed over one WebSocket and lives only in browser memory. A document-per-page
design would tear that socket down on every click: each page change would cost a
reconnect, a fresh `hello`, a re-send of every book, and a tape that starts from
zero. So `core/router.js` swaps the `hidden` attribute on page roots and
`core/ws.js` is never told that anything happened. Verified live: the tape kept
counting (5 → 10 MSGS) across a full six-page round trip and the connection pill
never left LIVE.

The server side of that bargain is in `create_app()`. Each parameterless route is
registered explicitly from `SPA_ROUTES`, and `/market/{market_id:path}` gets its
own handler; all of them return the same shell document (`_shell_response()`,
or a `UI ASSETS MISSING` plain-text body if `index.html` is not on disk). The
route set is **enumerated rather than a catch-all** so that `/api/*`, `/ws`,
`/metrics` and `/static/*` keep their own handlers and an unknown path is still
a 404 instead of a shell that hides a broken link. `tests/test_ui_server.py`
guards both halves of that: every `SPA_ROUTES` path deep-links to the shell,
and `/api/unknown`, `/nope` and `/a/b/c` still 404.

`market_id` is opaque — it is `<venue>:<native identifier>` and may contain a
`/` — so the route takes `:path`, the client writes it with
`encodeURIComponent` and reads it back by stripping the `/market/` prefix from
`location.pathname`, never by splitting on `/`. The id is deliberately not
validated server-side: a deep link to a market that has rolled off the discovery
list opens the page, and `GET /api/markets/{id}` returning 404 is what turns it
into the named UNKNOWN MARKET state.

Client-side, an unmatched path lands on the monitor and rewrites the URL to `/`.
Nav tabs and the DES back link are real `<a href>` elements with `data-page`;
the router intercepts only a plain left click, so middle-click, copy-link and
open-in-new-tab keep working. `document.title` tracks the page as
`ARB · <TITLE>`.

## The page module contract

Every `js/pages/<name>.js` default-exports one object:

```js
export default {
  id: "arb",                 // route key AND the dirty render key the router binds
  path: "/arb",              // canonical path ("/market/:id" for the param route)
  title: "ARB",              // nav label and document title
  nav: true,                 // appear in the nav strip
  root: "arbpage",           // id of this page's root element
  mount(params) {},          // navigated TO. params = {} or {id: "..."}
  unmount() {},              // navigated AWAY. Stop every timer and fetch here.
  render() {},               // called by the rAF batch when the dirty key `id` is set
  onKey(e, scope) { return false },  // page keys; return true to claim one
  regions: ["arb-min", "arb-rows"],  // optional: TAB order from ARB>; [0] takes "/"
  listRegion: "arb-rows",            // optional: what ↑/↓ from ARB> focuses
  keyHints(scope) { return [] },     // optional: what the keys strip prints per scope
  onMessage(msg) {},         // optional: every WS frame, after the core handled it
};
```

Rules that are load-bearing rather than stylistic:

- **The router owns visibility.** It toggles `hidden` on `#<root>`; a page never
  does. (`style.css` ends with `[hidden] { display: none !important }` because
  component rules like `.kv { display: flex }` would otherwise outrank the UA's
  own `[hidden]` rule.)
- **`mount`/`unmount` are the only place to start and stop timers and fetches.**
  A `setInterval` that outlives `unmount` is a bug — the PAPER page's 3-second
  `/api/paper` poll is the canonical example, and it also aborts its in-flight
  request on the way out.
- **`render()` is registered under the page `id`, and only while mounted.** The
  router wraps it so a dirty key for a page that is not showing does nothing.
- **A dirty key with no renderer is silently dropped** by the rAF batch. Two
  pages therefore call `registerRenderer()` for the *old* key names that the
  rest of the app still schedules: `pages/market.js` registers `"des"` (which is
  what `ws.js` and `state.select()` schedule — never `"market"`), and
  `pages/system.js` registers `"poly"` alongside its own `"system"`.
- **Frames a page cares about are subscribed at module load, not in `mount`.**
  `onMessage("arb", …)` and `onMessage("paper", …)` run whether or not their page
  is showing, so arriving on ARB or PAPER shows current data instead of a stale
  table. State updates always go through `schedule()`; nothing renders straight
  out of a socket handler.
- **Tabular rows need one of `.ladder-row .mon-row .tape-row .mid-row .kv`** or
  select-to-copy silently stops working on that page. Placeholders ("NO MATCH",
  "AWAITING DATA") deliberately carry none of them, so a banner never copies as
  if it were a data row.
- **Every page owes an empty state and an accurate `.des-foot` footer.** The
  footer is not decoration: `/help` reads the per-page key tables out of it, so a
  binding and its footer change in the same edit or the help page starts lying.
  There is exactly **one** footer per page — the pages that used to declare one
  in `index.html` *and* overwrite it from JS now build or adopt a single element
  (`buildFoot()` in `pairs.js` and `arb.js`, the module-scope block in
  `paper.js`), because two copies of a key list is how a page and its strip
  drift apart.
- **The keyboard belongs to the shell, not the page.** `onKey(e, scope)` takes
  the live scope, and a page guards every letter with it
  (`if (scope !== SCOPE.LIST) return false`); the shell will not hand a page a
  bare printable in any other scope, and the guard is the page saying so out
  loud. No page implements Escape. `regions`, `listRegion` and `keyHints(scope)`
  are how a page joins the focus model at all — see
  [The keyboard model](#the-keyboard-model).
- **Focus on mount is the router's, and it is conditional.** `restoreFocus()`
  puts the keyboard on `#cmd` only when nothing holds it, or when what held it
  was inside the root being unmounted — a fact read *before* hiding that root,
  since hiding the element containing `activeElement` drops focus to `<body>`
  and erases the answer. It must not be unconditional: `pages/market.js`
  re-navigates with `{replace: true}` on every arrow press and `show()` does not
  early-return when the `:id` param changed, so a blind `focus()` would fire on
  every DES arrow and fight whatever the user had focused.

Adding a page means five edits, and the fifth is the one that is easy to forget:
a root element in `index.html`, a nav anchor with `data-page` and its digit, a
`css/<page>.css` link, an entry in `PAGES` in `main.js` (registration order *is*
nav order), and the path in `SPA_ROUTES` in `server.py` — without that last one
the page works until someone reloads it and gets a 404. A module that fails to
load, or that is not on disk yet, degrades to a placeholder that keeps the route
and the nav tab alive and logs the failure; it never takes the terminal down.

## Core modules

| Module | Owns |
| --- | --- |
| `core/state.js` | the one shared `state` object, the rAF render batch and its dirty keys, the market selection (`select`, `selectRelative`), and the pointer-down render pause |
| `core/ws.js` | the session's single WebSocket, jittered exponential backoff (0.5 s → 15 s), the core handling of `hello`/`book`/`delta`/`stats`, the `onMessage` registry, the tape queue and the 1-second latency buckets |
| `core/router.js` | path compilation and matching, page registration, `navigate`/`popstate`, the nav's active tab, `hidden` toggling, `document.title` |
| `core/keys.js` | the document-level `keydown` listener, the scope resolver, the whole resolution order below, the focus helpers (`focusCommand` `focusRegion` `focusList`), the platform chord (`NAVMOD`/`NAVLABEL`) and the keys strip |
| `core/cmd.js` | the `ARB>` buffer and the command vocabulary |
| `core/format.js` | pure formatters — no DOM, no state. If a formatter is used by one page it belongs in that page |
| `core/dom.js` | `$ el titled flash setVal setText`, plus the live `prefers-reduced-motion` query |
| `js/main.js` | the shell: status bar, UTC/ET clocks, the 10-second `/api/status` poll, page registration, select-to-copy, and the one call to `connect()` |

Rendering is one rAF batch. Handlers mutate `state` and call `schedule(...keys)`;
the batch then calls each dirty renderer at most once per frame, re-walking in
registration order (bounded to four passes) because a renderer may dirty another
— the monitor dirties the depth ladder when the selected book moves. Keys nobody
registered a renderer for are dropped rather than leaked.

`flash()` coalesces to one flash per cell per 84 ms (≤ 12/s) and, under reduced
motion, swaps the 360 ms background animation for a persistent weight/glyph
change for one second. Both are honoured live: the media query is re-read per
use, not sampled at load.

The status bar's connection pill reads CONNECTING, LIVE, RECONNECTING, then DOWN
once three reconnect attempts have failed.

## The keyboard model

**The focused region owns every key.** Focus starts and ends on the `ARB>` line
on every page, so a bare letter is always typing unless you deliberately moved
the keyboard into a list that visibly says otherwise. The spec is
[`.context/keyboard-model.md`](../.context/keyboard-model.md); the whole
implementation is [`core/keys.js`](../src/arb/ui/static/js/core/keys.js), which
owns the one document-level `keydown` listener.

It used to be the other way round, and that was a data-integrity bug rather than
an ergonomic complaint. `core/keys.js` gave the active page's `onKey()` first
refusal on **every** key, bare printables included, *before* the command line
saw them — so on `/pairs`, with nothing focused, typing the word `RUN` did: `R`
reload, `U` set the selected pair PROPOSED, `N` set it REJECTED. Two Postgres
writes from someone who believed they were typing in a search box. The
`cmd.buffer() === ""` guards the pages carried could not help and have been
deleted rather than extended: the buffer is empty precisely when you start
typing, so nothing keyed on it can protect the *first* character — which is the
character that fired.

### Three scopes

`scopeOf(target)` derives the scope from `document.activeElement` on every
keydown. Nothing is remembered between keys, so the scope can never disagree
with where the caret visibly is.

| Scope | `activeElement` | A bare printable goes to | Entered by | Left by |
| --- | --- | --- | --- | --- |
| `TEXT` | `<input>`, `<textarea>`, contenteditable | that field, unconditionally | `TAB` from `ARB>`, `/` from a list, a click in the box | `ESC` (twice if the box has a value); `⏎` or `↓` to the page's list, or `⏎` back to `ARB>` if it has none |
| `LIST` | anything inside `[data-keyregion="list"]` | the page's action keys | `↑`/`↓` from `ARB>`, `TAB`, `⏎` or `↓` out of a filter box | `ESC`, `TAB` off the end, an unclaimed printable, any mouse click |
| `COMMAND` | everything else — `#cmd`, `<body>`, a focused button or anchor | the `ARB>` buffer, always | every page mount, every navigation, every rung of the `ESC` ladder | deliberately, by one of the moves above |

The list regions are `#mon-rows`, `#arb-rows`, `#pair-rows` and `#ptr-rows`,
each carrying `data-keyregion="list"` in `index.html`. The rows inside them are
**not focusable**: the cursor is `aria-activedescendant` on the container, never
a roving `tabindex`, because a focusable row would make `Y` fire in a scope the
resolver believes is `COMMAND`. PAPER's POSITIONS table is focusable but is
deliberately *not* a key region, so `R` does not fire from it; `#cmd` carries
`data-keyregion="command"` for the same kind of honesty, though `scopeOf()`
reaches `COMMAND` by falling through rather than by reading it.

### The resolution order

`onKey(e)` returns immediately for an IME composition (`isComposing`, a `Dead`
key, `keyCode === 229`) or anything already `defaultPrevented`, resolves the
scope from `e.target`, and then walks these steps — numbered as the source
numbers them:

1. **The nav chord**, checked in *every* scope including `TEXT`. It is digits
   and brackets with a modifier, so it cannot collide with editing a field —
   and the old code returned before it whenever a filter box had focus, which
   left the page shortcuts dead exactly when you wanted them. Anything still
   carrying Ctrl/Meta/Alt after this check belongs to the browser and returns.
2. **`Escape`**, resolved globally before any page sees it, down the ladder
   below. No page implements Escape any more, so a page can never be left
   holding an armed bulk decision.
3. **A text field owns everything else it is sent.** The only two keys lifted
   out are `⏎` and `↓`, which move focus to the page's list (or, for `⏎` on a
   page with no list, back to `ARB>`). Note what this implies for `TAB`: inside
   a field it is the browser's own tab order, not the region walk in step 12.
4. **A focused `<button>`, `<a>` or `<select>` keeps `⏎` and `Space`** — its
   own activation keys.
5. **A typed command always wins.** `⏎` with a non-empty buffer runs
   `cmd.exec()` — in `COMMAND` and in `LIST` alike. (`TEXT` never reaches here:
   a field owns its own `⏎`, which is step 3.)
6. **The dirty-buffer rule.** A printable with a non-empty buffer keeps typing,
   in both of the scopes that get this far.
7. **The fix.** A bare printable in `COMMAND` scope is typing, full stop — the
   page is never consulted, so a single-letter action cannot fire from the home
   position.
8. **Arrows from home move focus, not the cursor.** `↑`/`↓` in `COMMAND` on a
   page that declares a `listRegion` focuses that list and leaves the selection
   alone, so row 0 is the next candidate and you can see where you are.
9. **No autorepeat on a printable in `LIST`.** A leaned-on `Y` was one POST per
   key repeat, and `decidePair()` advances the cursor under a status filter, so
   a held key walked the queue writing as it went. The pages guard this too;
   doing it here makes it structural rather than per-page discipline.
10. **The page** — `page.onKey(e, scope)`, `true` claims the key. By
    construction it can only ever see a bare printable in `LIST` scope.
11. **`/` from a list** focuses that page's first text region, like every pager.
12. **Global non-printables.** `TAB` walks the page's `regions`; `↑`/`↓` move
    the market selection; `⏎` on an empty buffer opens DES for the selection;
    `⌫` deletes a character.
13. **The `LIST` fall-through.** An unclaimed printable blurs to `COMMAND` and
    *then* types itself.

| Key | Global behaviour (steps 5–13) |
| --- | --- |
| any printable | appended to the `ARB>` buffer, uppercased, 48 characters max — from `COMMAND` always, from `LIST` when the page does not claim it, never from `TEXT` |
| `⌫` | delete the last character of the buffer |
| `⏎` | non-empty buffer: run the command. Empty: the page's Enter action, else open DES for the current selection |
| `Esc` | one rung down the ladder below |
| `↑` `↓` | at `ARB>`: enter the page's list (or, on a page with none, move the selection). Inside a list: the page moves its own cursor |
| `TAB` | from `ARB>` into the page's `regions` in order (`⇧TAB` enters from the end); off either end of a non-text region, back to `ARB>`; a page with no regions and no list leaves `TAB` to the browser |

### Why the dirty buffer and the fall-through both exist

They are the two halves of "nothing you type is silently swallowed".

The **dirty-buffer rule** (step 6) keeps a word together across a focus change:
once the buffer is non-empty every subsequent character goes to it in any scope
the page can see, so a ticker half-typed at `ARB>` finishes as one word even if
`↓` moved the keyboard into a list mid-word. It sits *above* the page precisely
so that the second half of `PAIRS` can never be read as `A`, `I`, `R`, `S`.

The **`LIST` fall-through** (step 13) covers the other direction: with the rows
focused, typing `KXPRES` gets you `KXPRES` in the command line — `K` is not one
of the page's actions, so it blurs to `COMMAND` and types, and the rest follow
through the dirty-buffer rule. Without it those six keystrokes would vanish into
a list that had no use for them, which is the failure mode that teaches people
to distrust a terminal.

`cmd.exec()` calls its own `home()` before navigating for the same reason: a
command typed with the focus parked in a list is a `COMMAND`-scope act, so the
next bare letter has to be typing again.

### The Escape ladder

One key, one meaning: step back out of whatever is innermost. First match wins,
and `escapeLadder()` is the only implementation — pages are forbidden from
claiming Escape.

| Rung | Situation | Effect |
| --- | --- | --- |
| 1 | `TEXT`, box has a value | clear it (dispatching `input` so the page re-filters) and stay in the box |
| 2 | `TEXT`, box empty | back to `ARB>` |
| 3 | `LIST` | back to `ARB>`; leaving the region disarms any pending bulk decision (`/pairs` listens on the rows' `focusout`, and `keys.js` also publishes `onScopeChange`) |
| 4 | `COMMAND`, buffer non-empty | `cmd.clear()` — a half-typed command is the innermost thing open |
| 5 | `COMMAND`, buffer empty | `navigate("/")`, a no-op on the monitor |

Rung 5 is a forward navigation to the monitor, not a history pop: `BACK` and the
browser's own back binding are the history controls. The strip's wording tracks
the rung you are actually on — `ESC CLEAR` in `COMMAND`, `ESC ARB>` in `LIST`
and `TEXT` — and the page footers follow the same split: MARKET and SYSTEM, which
have no list and no filter box, say `ESC MONITOR`, while the list pages say
`ESC ARB>` because that is where their Escape lands first.

### The page chord, and why history is unbound

Option is the insert-special-character modifier on macOS — `Option+1` types `¡`,
`Option+[` types a curly quote — so on a Mac the page chord is Control:

| Binding | Action |
| --- | --- |
| `CTRL+1`…`CTRL+6` (macOS) / `ALT+1`…`ALT+6` (elsewhere) | jump to the *n*th nav page |
| `CTRL+[` `CTRL+]` / `ALT+[` `ALT+]` | previous / next page, wrapping |
| `⌘[` `⌘]` (macOS) / `ALT+←` `ALT+→` (elsewhere) | history back/forward — **the browser's own**, deliberately left unbound here |

Alt stays live as an alias on every platform (`chordHeld()` accepts `NAVMOD` or
`altKey`); it is simply never *labelled* on a Mac. The chord is matched on
`e.code` (`Digit1`…, `BracketLeft`/`BracketRight`), so the physical key works
whatever the modifier would otherwise have typed, and a digit past the nav count
returns `false` rather than `preventDefault()` — a spare digit belongs to the
browser, not to a dead binding. `Cmd` is never the chord (`chordHeld()` rejects
`metaKey`), because `⌘[` / `⌘]` are already history on macOS.

The four `history.back()` / `history.forward()` bindings that used to exist are
**deleted**, not moved. Shadowing a binding the browser already owns buys
nothing and costs the user their muscle memory.

One constant drives every label. `NAVMOD` (the event property to test) and
`NAVLABEL` (the string to print) are exported from `core/keys.js`, and the nav
hint, the keys strip (`chordLabel()`), every page footer that mentions the chord
and `/help`'s own key table all read them — a hand-edited `ALT+` is how the
strip and the documentation drift apart. `help.js` even derives *"am I on a
Mac"* as `NAVLABEL === "CTRL"` rather than sniffing the platform a second time.

Platform detection is one regex, and it is case-insensitive on purpose:
`navigator.userAgentData.platform` reports `"macOS"` with a lower-case m and
short-circuits `navigator.platform` (`"MacIntel"`), so the obvious `/Mac/` test
silently mislabelled every Chromium browser on a Mac. It is now
`/mac|iphone|ipad|ipod/i`.

### Consequence grading

The rule the scope model came out of, and the one that matters most for day 3's
live order placement: **how hard a key is to press scales with what it costs.**
It is written up in [`decisions.md`](decisions.md).

| Grade | Example | Price of pressing it |
| --- | --- | --- |
| G0 | view/scroll: arrows, filters, sort | free, any scope |
| G1 | navigation: page jump, DES | free, a cheap chord |
| G2 | one-row write: `Y` `N` `U` | one key, `LIST` scope only, no autorepeat |
| G3 | many-row write: `⇧Y` `⇧N` | arm, then confirm; the exact count is stated |
| G4 | irreversible / money (day 3: live orders) | **never a hotkey** — a typed command plus a typed confirmation |

### The on-screen cue

A scope model is only safe if the scope is visible, so `renderKeys()` repaints
on every `focusin`, `focusout` and navigation.

The keys strip (`#keys`) is: a mode chip — `ARB> COMMAND`, `LIST KEYS` or
`TEXT FIELD`, reversed video in `LIST` — then the page's own `keyHints(scope)`,
then whichever globals the page did not claim, then a note at the right
(`TYPE ANYWHERE TO COMMAND` / `KEYS ACT ON THE LIST` / `TYPING IN A FIELD`).
A page that names a key has the final word on it: `/help` binds the arrows to
scrolling, so the global `↑↓ SELECT` is dropped beside its own `↑↓ SCROLL`
rather than printed as a lie. `.keys` is one clipped `nowrap` line, which is why
the mode chip is pinned **left** — whatever sits leftmost is the last thing to be
clipped away — and why pages keep their hint lists to three or four entries.

The stronger cue is on the element whose behaviour actually changed: in `LIST`
scope the `ARB>` line itself becomes a reversed-video band (`.cmdline.scope-list`
— amber ground, black text, black cursor) and `#cmd-mode` spells out the live
keys, e.g. `LIST · Y / N DECIDE · SHIFT+Y/N EVENT · U UNDECIDE · / SEARCH`. You
cannot be in a scope where a letter writes to Postgres and not see it.

Mouse behaviour is arranged to keep that promise true. A click inside a list
selects the row but does **not** hand the row the keyboard (`install()` returns
focus to `#cmd`, ignoring keyboard-synthesised clicks with `e.detail === 0`), the
page modules do the same after a click on their own buttons, and the router
blurs a nav anchor clicked with the pointer. Anything that writes has to be
reached deliberately, and the mouse route to a decision is a button, not a letter
that fires because you clicked nearby.

### What a page must do to participate

Four optional fields. They are optional for real pages — `core/keys.js` reads
every one defensively — and `main.js`'s placeholder for a module that failed to
load answers all four explicitly (`regions: []`, `listRegion: null`,
`keyHints() { return [] }`, `onKey() { return false }`), so a stub page falls
through to the global bindings instead of throwing at the resolver:

| Field | Meaning |
| --- | --- |
| `onKey(e, scope)` | page-scoped keys; return `true` to claim one. The second argument is the scope — guard every letter with `if (scope !== SCOPE.LIST) return false` |
| `regions: ["pair-q", "pair-rows"]` | the `TAB` order from `ARB>`; `[0]` is also where `/` lands (the first `<input>` among them) |
| `listRegion: "pair-rows"` | the region `↑`/`↓` from `ARB>` focuses. A page with no row list — MARKET, SYSTEM, HELP — declares none, and the arrows reach its `onKey` in `COMMAND` scope instead |
| `keyHints(scope)` | what the strip and the `ARB>` band print in that scope; `{k, d}` objects or `"K LABEL"` strings. A page that throws here loses its hints, not the strip |

Nothing about any individual page is hardcoded in `core/keys.js`, and the router
does the rest: [every page mounts on the `ARB>` line](#the-page-module-contract),
so a fresh page always starts in `COMMAND`.

### Commands

`core/cmd.js` strips a trailing `<GO>` or `GO`, uppercases, and then:

| Command | Effect |
| --- | --- |
| `MON` / `MONITOR` | `/` |
| `ARB` | `/arb` |
| `PAIRS` | `/pairs` |
| `PAPER` | `/paper` |
| `SYS` / `SYSTEM` | `/system` |
| `HELP` / `?` | `/help` |
| `BACK` | `history.back()`, one step |
| `DES` | description page for the current selection, or `NO MARKET SELECTED` |
| `<TICKER>` | select a market: exact ticker, then prefix, then substring |
| `<TICKER> DES` | select it and open its description page |

Every command navigates; none of them opens an overlay any more. A miss leaves
the screen alone and flashes `NO MATCH · <what you typed>` beside the prompt.

## Screens

### MONITOR — `/`

The default workspace, and the only page with a three-panel layout: the market
list (360 px), the depth ladder, and a tape + latency column (360 px).

The list carries **both venues**, and now says so: the header reads
`MONITOR — KALSHI 13 · POLYMARKET US 9` from live counts. It was hardcoded to
`MONITOR — KALSHI` while listing Polymarket US markets underneath it, which is
the kind of label that quietly teaches an operator the wrong thing. The head
stat shows `N MKTS`, or `n OF N MKTS` under a filter, plus `· SEL HIDDEN` when
the selected market is filtered out.

| Column | Meaning |
| --- | --- |
| `#` | row number. Rows 1–9 additionally carry the quick-select shortcut, marked by emphasis and a `QUICK SELECT n` tooltip |
| `VEN` | `K` Kalshi (streamed) or `PM` Polymarket US (REST-polled) |
| `TICKER` | the venue's own identifier; the row tooltip adds title and 24h volume |
| `BID` `ASK` `MID` `SPR` | best YES bid and ask, their midpoint, and the spread, in dollars per contract. Bid and ask flash green or red with the direction of the move; mid and spread flash neutral |

The `#` column used to go blank after row 9, because the number *was* the
shortcut. Now every row is numbered and only the first nine advertise a
shortcut: a list you can count is worth more than a column that stops.

New on this screen: a `VEN` column, a filter box matching ticker, title or
market id, an ALL/K/PM venue filter, and sortable column headers (click to sort,
click again to reverse; price columns start descending, text columns ascending;
markets with no book always sort last and the server's volume order breaks every
tie). Filter and sort persist in `localStorage` under `arb.monitor.v1`.

Rows are never rebuilt for a data tick. Cells are updated in place and the order
is re-applied by moving the existing nodes (`replaceChildren` with the same
elements), so sorting by a live price cannot cost the selection, the flash state
or a drag in progress.

The **ladder** shows up to 12 levels a side, with NO prices derived by complement
(`10000 − YES`, in ticks), depth bars scaled to the largest level on screen, a
`MID · SPR` seam with the best bid and ask closest to it, and a banner that reads
`QUIET` or `INVALID · <REASON>` (below).

The **tape** prepends at most 3 rows every 250 ms (≤ 12 rows/s) and keeps 200,
so a burst cannot outrun the eye; the counter in the panel header is the run's
true total, which keeps climbing while you are on another page. The queue behind
it is capped at 24 frames in `core/ws.js`, so a long absence costs tape rows, not
memory. Draining pauses during a select-to-copy drag, because prepending rows
would shift the selection under the pointer.

The **latency** panel is the sparkline plus `LAST / MED / P95 / N / RTT / SKEW` —
see [Skew-aware latency](#skew-aware-latency).

Keys, all of them `LIST` scope — `↑`/`↓` at the `ARB>` line enters the rows, and
`TAB` or `/` opens the filter box: `↑↓` move through the list *as filtered and
sorted*, `1`–`9` quick-select a visible row, `⏎` opens DES. At the `ARB>` line a
digit is just a character in a ticker, which is why the footer reads
`1-9 QUICK (IN LIST)`. A click selects a row and leaves the keyboard on `ARB>`;
double-click opens DES.

### DES — `/market/<market_id>`

The market description page: `⏎` on a selection, `<TICKER> DES`, or a
double-click. It answers "what exactly does this market pay out on", which is the
question a cross-venue pair lives or dies by.

It carries the event title and sub-title, ticker anatomy (series → event →
market) plus the opaque id the CLI takes, the full primary and secondary
resolution rules, settlement sources, status/category/type/mutually-exclusive/
early-close, implied probability beside the quote (deliberately together — with
$0.0001 ticks the two share a representation: 50 ticks = $0.0050 = 0.50¢ = 0.50%
implied), a live book summary (best levels, depth per side, level count, age),
volumes and open interest, and open/close/expected-expiration in both UTC and ET
(rules texts are usually written in ET, per `CLAUDE.md`).

Becoming a route rather than an overlay added the chrome an overlay never needed:
a `◀ MONITOR` back link (a real anchor, so middle-click works), a venue badge —
this page serves Kalshi *and* Polymarket US and the ported layout never said
which — a `MARKET i OF n` position readout matching the array the arrows walk,
and a `COPY` button for the ticker.

`↑↓` page through markets without leaving the page, navigating with
`replace: true` so arrowing through twenty markets does not bury the back
button. This page declares no `listRegion` — there is no row list on it, so
paging the universe *is* its list behaviour — and the arrows therefore reach its
`onKey` in `COMMAND` scope rather than being diverted into a list. It owns no
letter keys at all, so its footer says `ESC MONITOR`: with no list and no filter
box to step out of, Escape lands on ladder rung 5 immediately. (SYSTEM says the
same, for the same reason; the list pages say `ESC ARB>`.)

Backed by `GET /api/markets/{id}`, seeded from discovery metadata and refreshed
live on open with a 30 s TTL (`DETAIL_TTL_MS`), falling back to the seed if the
live refresh fails; the head stat says `LIVE` or `DISCOVERY` and how old the
payload is. A 404 renders a named `UNKNOWN MARKET` state explaining that the id
may have closed or rolled off the discovery list — a deep link from an older run
is expected, not an error. The settlement-source list is rebuilt only when it
actually changes, because this page re-renders on the 1 s age tick and on every
book frame, and replacing those nodes would empty a URL someone had selected.

### ARB — `/arb`

Every tracked confirmed pair, ranked by net edge per contract, taker fees on both
legs already subtracted. **Measurement only — this page places nothing.**

Columns: `NET/CT`, `SIZE`, `GROSS`, `FEES`, `PAIR`, `DIRECTION`, both venues'
BBOs and each leg's book state. Selecting a row shows both legs with their worst
prices and fees, the fee model in play, and the reverse direction's numbers; `⏎`
opens the Kalshi leg's DES page (and says `NO LIVE BOOK FOR <ticker>` rather than
navigating into nothing).

Two things the overlay could not do. First, the `arb` frame is subscribed at
module load, so the snapshot stays current while you are elsewhere. Second, the
table has a `MIN NET/CT` filter (in ticks, with `ALL` / `EDGE` / paper-threshold
presets) and client-side sorting by net, size or pair, persisted under
`arb.arb.v1`; a summary line reads `TRACKED n · POSITIVE n · ACTIONABLE n · BEST
…`, and rows that clear the threshold are marked, so "is there anything to act
on" is one glance rather than a scan.

The threshold is read from `GET /api/paper`, and the label distinguishes
`PAPER 50` from `DEFAULT 50`: that endpoint serves `PaperLimits()` defaults even
with no trader running, so the number alone never means it is being enforced. The
screen assumes it is not until the payload's `enabled` says otherwise — claiming
enforcement that does not exist is the one error this screen must not make, and
the summary says `MARKED n (…, NO PAPER TRADER RUNNING)` in that case.

The cursor follows the top of the ranking until you pick a row; from then on it
stays pinned to that pair as the ranking reshuffles beneath it. The two empty
states are distinguished, because they mean completely different things: no
confirmed pairs tracked at all (confirm pairs, then restart `arb ui`) versus no
pair clearing the current filter.

Keys: `↑`/`↓` at the `ARB>` line enters `#arb-rows` and then moves the cursor,
`⏎` opens the Kalshi leg, `TAB` (or `/` from the list) lands in the `MIN NET/CT`
box. This page binds **no letter at all, in any scope**, so typing `PAIRS` or a
ticker here always reaches the command line.

### PAIRS — `/pairs`

The cross-venue pair review queue described in
[`pairs.md`](pairs.md#human-review-in-the-terminal-ui). A proposal run stores the
whole cross product above the cut — twelve thousand rows is normal — so the
screen is a review queue, not a table: one keystroke per decision, the cursor
landing on the next candidate, and both legs' full resolution rules beside the
row so the judgement can be made without leaving.

The list is fetched **once, unfiltered** (`GET /api/pairs`, 60 s TTL, `R` forces
a refresh) and filtered in the client, so switching status is instant and the
chip counts can be honest about the whole universe. Only the top 500 rows by
score are put in the DOM, with a banner saying exactly how many are held back.
A progress bar reports `proposed / confirmed / rejected · n% reviewed`.

This is the page the scope model was written for, and the only one whose keys
write to Postgres. Every letter below fires **only in `LIST` scope** — with the
focus actually inside `#pair-rows`, where the `ARB>` line is a reversed-video
band naming them. At the command line they are typing, and the word `RUN` is a
word.

Keys: `↑↓` select, `PgUp`/`PgDn` ±10, `Home`/`End`, `←`/`→` cycle the status
filter, `/` focuses the search box, `Y` / `N` decide, `U` un-decides, `R`
reloads. The filter moved off `TAB` because a page that eats `TAB` is a page you
cannot leave with the keyboard — `TAB` is how you get *out* of a region — and
`←`/`→`, being non-printable, shadow nothing anybody could type. `Y`, `N`, `U`
and the bulk keys all reject `e.repeat`: `decidePair()` advances the cursor and
a row leaves the queue under a status filter, so a leaned-on key used to march
down the list POSTing at the key-repeat rate.

`Shift+Y` / `Shift+N` decide every candidate in the selected row's
event pairing at once — a 30-team pennant race is one judgement, not thirty —
and are two keystrokes whenever the group holds more than one row: the first
arms and names the exact count and event, the second spends it (8 s window). A
bulk write only ever touches rows the operator can *see* — siblings hidden by
the search box or by the 500-row cap are left alone, and the prompt says so.
An armed decision disarms on any other key, on Escape and on the rows losing
focus, so it can never be spent by a keystroke aimed at something else.
The `UNDO` button restores the previous status of the last decision, row by row,
whatever mix of statuses that was.

Mouse parity is not an afterthought here: `#pair-detail` carries real `CONFIRM`,
`REJECT` and `UNDECIDE` buttons beside the `UNDO` chip, disabled when the row
already has that status so a click cannot POST a write that changes nothing.
Before them a mouse user had no route to a decision at all, and a keyboard user
had no route that was not a bare letter — the exact shape that made `RUN`
dangerous.

### PAPER — `/paper`

The simulated ledger: running totals, a cumulative expected-net curve over the
trade sequence, per-pair positions, the trade tape with per-trade leg detail, and
the active `PaperLimits` — see [`engine.md`](engine.md#paper-trading).

It polls `GET /api/paper` every 3 s **while mounted only**, and folds in `paper`
WS frames subscribed at module load, so fills taken while you were on another
page are already there when you arrive. Clicking a position (or `⏎` on a trade)
filters the trade table to one pair; `R` refreshes, and only from `LIST` scope —
at the `ARB>` line an `R` is the first letter of a command. The ledger
(`#ptr-rows`) is this page's list region; POSITIONS is focusable but is not a key
region, so the arrows and `R` do not act from it.

Limits are rendered as capacity *used*, not as three constants: `MAX NOTIONAL
$1000.00` says nothing on its own, while `$12.40 · 1% OF $1000.00` says whether
the run is anywhere near its ceiling (amber from 70%, red from 95%). That
reading only exists while the trader is on. With it off the server returns
`PaperLimits()` defaults that nothing enforces, and totals summed over stored
trades from *every* run, so the page drops the percentages and labels both
facts — `DEFAULTS — NOT IN FORCE · TRADER OFF` and `CUMULATIVE EXPECTED NET ·
HISTORY, ALL RUNS` — rather than inventing a ceiling.

### SYSTEM — `/system`

The plumbing, one card per subsystem, read-only, live off the 1 s `stats` frame
and `/api/status`. This page is where the restructure paid a debt: the
pre-multipage terminal carried the engine counters, recorder state, database
status and the whole Polymarket US block as panels on the single screen, and the
first cut of the port dropped their markup, leaving those numbers dark. They are
restored here, against markup this module builds inside an empty root.

| Card | Contents |
| --- | --- |
| ENGINE | message total and 1 s rate, parse errors, sequence gaps, WS clients, uptime. Errors and gaps go amber past zero — they are counted, never fatal |
| RECORDER | on/off, enqueued and dropped counts. `OFF` means `--no-record`: displayed, not persisted, not replayable |
| DATABASE | connection, total raw rows, and rows per run id newest first, with the current run tagged `LIVE` — those ids are what `arb replay` takes |
| CLOCK & LATENCY | one-way last/median/p95, sample count, keepalive RTT, RTT/2 and the skew estimate, with a banner and the fix written on the page |
| KALSHI · WEBSOCKET | transport state, detail, and how long ago the status was checked |
| POLYMARKET US | the polled venue: REST reachability, polls, 429s, errors, poll targets, configured rate, age of the last successful book |
| OBSERVABILITY | `/metrics`, the Grafana URL and the provisioned dashboard name, plus the series that back this page |

The CLOCK & LATENCY card explains the negative-latency case in place rather than
leaving it to a runbook: one-way latency is measured across two machines' clocks,
so it reads `true_transit + (local − venue)` and goes negative when the local
clock is behind; skew is `median − RTT/2`; RTT only ever touches the local clock,
so it survives skew; and the fix is `arb doctor`'s `ntp clock` check followed by
an `sntp`/`timedatectl` sync.

### HELP — `/help`

The reference card: start here, how to read each screen, the keyboard, the
commands, a glossary and a safety statement. Sections are filterable (`TAB`
focuses the box, the head stat shows `n OF m LINES`) with a jump rail that tracks
scrolling. The old `/` binding is **gone**: this page declares no `listRegion`,
so there is no scope in which `/` could be an action here, and at the `ARB>` line
it is a character like any other. Everything the page does own — `↑↓`,
`PgUp`/`PgDn`, `Home`/`End` — is a scroll, which is G0 and free in any scope.

A help page that lies is worse than none, so most of it is **derived** rather
than typed. The page list and paths come from the router's `navPages()`; the
chord numbering is `NAVLABEL` plus that count, so the page says `CTRL+1 - CTRL+6`
on a Mac and `ALT+…` elsewhere without sniffing the platform a second time (it
asks `NAVLABEL === "CTRL"`); and the per-page key tables are read out of each
page's own `.des-foot` strip in the DOM on every mount, verbatim, so they cannot
drift from what the page advertises. What is written by hand — the three scopes,
the Escape ladder, the globals, the commands and the glossary — is written
against `core/keys.js`, `core/cmd.js` and these docs, and it names the scopes
with the same three words the keys strip prints.

One deliberate departure from the house idiom, scoped to this page: definitions
are sentence case, because two thousand words of caps is unreadable.

## `UIState`: how the routes stay testable

`create_app(state: UIState) -> FastAPI` builds every route and the WebSocket
endpoint against a small `Protocol` (`UIState`), not against the concrete
`ServerState` class directly. Tests construct a stub implementing that protocol
and exercise the full HTTP/WS surface with **no network, no database, no live
venue connection** — `tests/test_ui_server.py` does exactly this, including the
shell routes, which take a `static_dir` so the same tests can check both the
real `index.html` and the assets-missing fallback. `ServerState` is the real,
stateful implementation `run_ui()` wires up in production: it tracks per-market
book payload caches, DES metadata (seeded from discovery, refreshed on demand
with a TTL), pair review, the arb monitor, the paper trader, per-client outbound
queues, and the rolling counters behind the stats frame.

## HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /` `/arb` `/pairs` `/paper` `/system` `/help` | the app shell (`SPA_ROUTES`), one entry per client-side route |
| `GET /market/{market_id:path}` | the app shell; the id is opaque and deliberately unvalidated |
| `GET /static/*` | `index.html`, `style.css`, `css/*.css`, `js/**/*.js`, served as-is |
| `GET /api/status` | run id, uptime, per-venue connection state, recording flag, database status (raw-message totals, per-run counts) |
| `GET /api/markets/{market_id}` | DES payload for one market (404 if unknown) |
| `GET /api/pairs?status=...` | pair review listing (see [`pairs.md`](pairs.md)) |
| `POST /api/pairs/{id}/decide` | confirm/reject one pair, body `{"status": "confirmed"\|"rejected"\|"proposed"}` |
| `POST /api/pairs/decide` | batch decide, body `{"ids": [...], "status": "..."}` |
| `GET /api/arb` | current `ArbMonitor.snapshot()` — every tracked pair's best quote |
| `GET /api/paper` | paper-trading ledger: `enabled`, limits, totals, positions, recent trades |
| `GET /metrics` | Prometheus exposition |
| `WS /ws` | the live push feed, below |

## WebSocket wire protocol

Server → client, JSON text frames, tagged by `"t"`:

- **`hello`** — sent once, immediately on connect: `run_id` and the full market
  list (id, ticker, title, 24h volume, venue), sorted by volume descending.
  Followed, in the same burst before any other broadcast can interleave, by a
  `book` frame for every market that already has book state, and (if an
  `ArbMonitor` is active) one `arb` frame — this ordering guarantee is
  deliberate: `queue.put_nowait()` is called for all of these with no `await` in
  between, so a freshly connected client's queue always has `hello` → all current
  `book`s → `arb` ahead of anything a concurrent broadcast could add, and never
  sees a `delta` for a market it hasn't been told exists yet.
- **`book`** — full YES-book state for one market: bid/ask ladders (`[price,
  qty]` pairs), validity (`valid`, `reason`), book age in ms, timestamp.
  Coalesced server-side to at most one push per market per
  `BOOK_FLUSH_INTERVAL_S` (0.1 s, i.e. ≤10/s per market) via a dirty-set + flush
  loop (`ServerState.mark_dirty()` / `take_dirty()`) — an ingest loop marks a
  market dirty on every applied event, but the actual `book` frame is only
  rendered and broadcast on the next flush tick, so a bursty market doesn't spam
  ten times its allotted rate.
- **`delta`** — one tape entry per applied level change: market, side, price,
  signed qty delta, latency (ms, when known), timestamp. For Kalshi this is a
  direct 1:1 mapping of parsed WS deltas; for Polymarket US (poll-based, full
  snapshots only) it's the output of `level_deltas()` diffing consecutive polls —
  see [`data-model.md`](data-model.md#bookmanager-the-fan-out-layer) — so the
  tape reads the same shape for both venues.
- **`stats`** — broadcast every `STATS_INTERVAL_S` (1 s): total message count and
  1-second rate, latency percentiles (last/median/p95 over the last
  `LATENCY_WINDOW` = 512 samples), keepalive RTT and a derived clock-skew
  estimate (below), parse-error and seq-gap counters, connected client count,
  recorder enqueued/dropped counts (when recording), Polymarket US poll stats
  (polls, rate-limited count, errors, targets, configured rate, age of the last
  successful poll), uptime. It is the heartbeat of both the latency panel and the
  whole SYSTEM page.
- **`arb`** — `{"t": "arb", "quotes": [...]}`, the full `ArbMonitor.snapshot()`.
  Re-broadcast whenever a flush tick's dirty set intersects the monitor's tracked
  market ids.
- **`paper`** — `{"t": "paper", "trades": [...]}`, sent only when new paper trades
  were just taken this tick. It is a delta, not a ledger: the PAPER page folds
  the trades into its snapshot (deduplicating by id) and `GET /api/paper` remains
  authoritative.

Inbound client messages are ignored by contract — this is a push-only feed; the
WS endpoint's receive loop exists purely to detect disconnects. The client side
reconnects on its own with jittered exponential backoff between 0.5 s and 15 s,
and unknown frame types are tolerated silently.

### Wire format keeps integers

Prices cross the wire as integer ticks ($0.0001) and quantities as integer `Qty`
units (0.0001 contracts) — never floats, never formatted strings. The browser
divides by 100 (ticks → cents) or 10,000 (`Qty` → contracts) purely for display,
in `core/format.js`, which is the only place in the frontend that does it; no
float ever touches server-side book state or gets serialized as a price. This
mirrors the same-discipline rule that governs storage and the fee/edge engine
(see [`data-model.md`](data-model.md)). Monitor, ladder and tape price cells
keep the raw tick value in their tooltip, so the integer is one hover away.

### A slow browser must never stall ingest

`ServerState.broadcast()` is called from the ingest path (the Kalshi/Polymarket
consume loops, the flush loop) and must never `await` a client socket — if it
did, one slow browser tab could back-pressure the entire market-data pipeline.
Instead, every connected client has its own bounded `asyncio.Queue`
(`SEND_QUEUE_MAX = 1024`), drained by a dedicated per-connection sender task that
does the actual `await ws.send_text(...)`. `broadcast()` only ever does
`queue.put_nowait()`; a client whose queue is full is dropped and closed (code
`1013`, "try again later") and counted (`arb_ui_ws_clients_dropped_total`) rather
than allowed to back up the feed for everyone else.

The browser holds up the same bargain from its end: WS handlers mutate state and
call `schedule()`, never render, so a slow paint costs a frame rather than
stalling the socket's read loop.

### Skew-aware latency

One-way latency (exchange `ts_ms` on a delta → local receive time) needs
synchronized clocks to mean anything; the WebSocket transport's keepalive RTT
does not — only the local clock is involved in a round trip. `clock_skew_ms =
median_one_way − rtt/2` estimates how far the local clock is offset from the
venue's. The monitor's latency panel flips to a `CLOCK SKEW · TRUST RTT/2` banner
whenever the one-way median goes negative or `|skew| > 25 ms` — a real observed
case: median −24.5 ms, RTT 24 ms → skew ≈ −36 ms, meaning the local clock was
running behind the venue's (fixed with an `sntp` clock sync, per `PROGRESS.md`).
`/system`'s CLOCK & LATENCY card uses the same threshold and adds the states the
one-line panel has no room for: `SKEW UNKNOWN · AWAITING KEEPALIVE RTT`, because
a missing estimate is not a clean bill of health.

The banner changes the *display*, not the measurement: the sample stays exactly
as measured, because a de-biased "latency" that silently subtracts a drifting
offset estimate would be a worse lie than an honest negative number. Three things
make the honest number legible instead:

- **The chart shows negative samples.** The sparkline (`pages/monitor.js`,
  `drawSpark`) has a y-domain of `0 -> 1.5x p95`, refit every 30 s, and `yAt()`
  clamps both edges, so a sub-zero sample pins to the bottom edge with a 3 px
  amber tick — the mirror of the over-range tick already used for spikes. Before,
  `yAt()` clamped only the top and negative points were drawn *below* the canvas:
  the panel printed `MED -14` next to a chart with no median line on it.
- **A quiet second is a gap, not a repeat.** `pushLatencyPoint()` in
  `core/ws.js` pushes a null bucket when no delta arrived in the last second. It
  used to fall back to `latency_ms.last`, which `ServerState` never resets — so a
  quiet market painted a confident flat line out of no data at all. `drawSpark()`
  splits segments on null, so a gap now renders as a gap.
- **`arb doctor` and Grafana can see it too.** The `ntp clock` check
  ([`cli.md`](cli.md#the-ntp-clock-check)) measures the offset against a time
  server rather than against a venue, and `arb_clock_skew_ms` /
  `arb_ws_one_way_latency_negative_total` carry it to Prometheus — so the failure
  mode is no longer invisible on a VM with nobody watching the terminal. Runbook:
  [`ops.md`](ops.md#host-clock-discipline).

## "QUIET" vs "INVALID"

The `Book`'s 5-second staleness rule (see
[`data-model.md`](data-model.md#book-validity-and-the-update-state-machine)) is a
*trading-validity* gate, not a display judgment — a prediction market that simply
hasn't traded in a while is normal, especially a low-volume one, and shouldn't
look broken. The ladder shows this state as a dim `QUIET · LAST UPDATE Ns AGO`
rather than red; red `INVALID` is reserved for the structural reasons
(`SEQ_GAP`, `CROSSED`, `BAD_LEVEL`, `NO_SNAPSHOT`) that actually mean the local
book state is wrong. The ARB page makes the same distinction per leg — a
stale-only book still quotes, a structurally broken one does not — and
`/help`'s glossary names all four structural reasons.

The ladder ages its own books between frames on a 1-second tick, so a feed that
goes silent turns QUIET on time instead of waiting for a frame that isn't coming.

## Polymarket US shows "POLLED", not "LIVE"

Because Polymarket US is currently REST-polled rather than streamed (see
[`venues/polymarket-us.md`](venues/polymarket-us.md)), the status bar shows an
amber `POLLED` state for it, distinct from Kalshi's green `LIVE` — and
`/system`'s POLYMARKET US card adds the poll budget, the 429 count and "last book
Ns ago" — so a polled book never visually claims to be something it isn't.
`ServerState.polymarket_status()` reports `"connecting"` before the first
successful poll, `"polled"` while polls are succeeding within `VENUE_DOWN_AFTER_S`
(30 s), and `"down"` past that. The monitor's `VEN` column carries the same
distinction per row: `K` streamed, `PM` polled.

## Clipped text must stay copy-exact

Several columns are too narrow for a full identifier — a Polymarket slug runs to
43 characters, a run id to 25. The rule is that **clipping is CSS-only**: the
element keeps its whole value in `textContent` (which is what select-to-copy
reads, below) and `text-overflow: ellipsis` handles the visuals, with the full
value mirrored into `title` so hovering reveals it. `titled()` in `core/dom.js`
is the one-liner that does the mirroring, and the status bar's run id
(`renderStatusBar` in `main.js`) and the SYSTEM page's run list both follow it.

Truncating the text itself is a bug, not a layout choice: a half-copied slug or
run id looks legitimate and resolves to nothing — the status bar's run id did
exactly that (`slice(0, 12)`), producing ids `arb replay` could not find. See
[`venues/polymarket-us.md`](venues/polymarket-us.md#website-links-vs-api-slugs).

## Select-to-copy

Selecting anything in the terminal — a ladder region, tape rows, an ARB row, a
help definition, any `.kv` — copies it to the clipboard and shows a `COPIED ·
N CHARS · N ROWS` toast (or a red `COPY BLOCKED` if the browser refuses). It
lives in `main.js` and works on every page, because it matches on a fixed set of
row classes (`.ladder-row, .mon-row, .tape-row, .mid-row, .kv`) rather than
knowing anything about screens.

Multi-row selections are rebuilt as tab-separated values (one row per line)
rather than relying on the DOM's own text serialization, because the UI's rows
are flex grids whose visual column boundaries the browser's default copy wouldn't
preserve — a ladder selection pastes into a spreadsheet with columns intact. A
selection inside a single cell copies exactly the highlighted substring. Copy
fires on `mouseup` (and `keyup` for Cmd/Ctrl+A, except inside a text field, where
select-all means select-this-field), not on `selectionchange`, because the async
Clipboard API requires transient user activation that a `selectionchange` event
firing mid-drag doesn't carry; `document.execCommand("copy")` is the fallback.

Live re-rendering pauses for the duration of a pointer-down drag
(`setSelecting()` in `core/state.js`) so a re-render can't destroy the DOM nodes
a selection is anchored in — dirty flags accumulate and flush on release, so data
is delayed during a drag, never dropped. The same pause holds the tape drain.
