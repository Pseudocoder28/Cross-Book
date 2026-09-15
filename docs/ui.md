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
  onKey(e) { return false }, // page-scoped keys; return true to claim one
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
| `core/keys.js` | the document-level `keydown` listener and the whole resolution order below |
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

You type anywhere and it lands on the `ARB>` line. That is the primary input of
the terminal, and every other binding is arranged around not breaking it.

`core/keys.js` resolves one keydown in this order:

1. **A real text field wins outright.** If the target is an `<input>`,
   `<textarea>` or `contenteditable`, nothing below runs — not even the Alt
   bindings, because Option+arrow is word navigation inside a field. This is what
   lets the monitor filter, the pairs search and the help filter coexist with
   type-anywhere-to-command. Those boxes each bind Escape (clear, then blur) and
   Enter (blur, handing the keyboard back to `ARB>`) themselves.
2. **Alt bindings.** `Alt+1`…`Alt+N` jump to the *n*th nav page (`N` = 6 today;
   the handler has room for seven), `Alt+[` / `Alt+]` cycle with wrap, and
   `Alt+←` / `Alt+→` are browser history back/forward.
3. **Any other modifier returns**, and a focused `<button>`/`<a>`/`<select>`
   keeps its own Enter and Space.
4. **The active page's `onKey(e)`.** Returning `true` claims the key and
   `preventDefault()`s it.
5. **The global bindings**, below.

| Key | Global behaviour |
| --- | --- |
| any printable character | appended to the `ARB>` buffer, uppercased, 48 characters max |
| `⌫` | delete the last character |
| `⏎` | empty buffer: open DES for the current selection. Otherwise: run the command |
| `Esc` | clear a half-typed command; if there is none, leave the page for `/` |
| `↑` `↓` | move the market selection through the full `hello`-order list |

Escape is handled once, globally, and no page may claim it. A half-typed command
is the innermost thing open so it goes first; otherwise the rule is the one the
keys strip has always advertised — ESC CLOSE, which on a route-based terminal
means `navigate("/")`. Note that this is a forward navigation to the monitor,
not a history pop; `BACK` and `Alt+←` are the history controls.

`1`–`9` quick-select is **not** global: it is the monitor page's own `onKey`, and
only fires with an empty command buffer (otherwise typing a ticker containing a
digit would jump the selection). Pages claim arrows for their own lists
(`pairs`, `arb`, `paper`), scrolling (`help`), or paging between markets
(`market`).

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

Keys: `↑↓` move through the list *as filtered and sorted*, `1`–`9` quick-select
a visible row, click selects, double-click opens DES.

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
button. Escape returns to the monitor, like everywhere else.

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

Keys: `↑↓` select, `PgUp`/`PgDn` ±10, `Home`/`End`, `Tab` cycles the status
filter, `/` focuses the search box, `Y` / `N` decide, `U` un-decides, `R`
reloads. `Shift+Y` / `Shift+N` decide every candidate in the selected row's
event pairing at once — a 30-team pennant race is one judgement, not thirty —
and are two keystrokes whenever the group holds more than one row: the first
arms and names the exact count and event, the second spends it (8 s window). A
bulk write only ever touches rows the operator can *see* — siblings hidden by
the search box or by the 500-row cap are left alone, and the prompt says so.
The `UNDO` button restores the previous status of the last decision, row by row,
whatever mix of statuses that was.

### PAPER — `/paper`

The simulated ledger: running totals, a cumulative expected-net curve over the
trade sequence, per-pair positions, the trade tape with per-trade leg detail, and
the active `PaperLimits` — see [`engine.md`](engine.md#paper-trading).

It polls `GET /api/paper` every 3 s **while mounted only**, and folds in `paper`
WS frames subscribed at module load, so fills taken while you were on another
page are already there when you arrive. Clicking a position (or `⏎` on a trade)
filters the trade table to one pair; `R` refreshes.

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
commands, a glossary and a safety statement. Sections are filterable (`/` focuses
the box, the head stat shows `n OF m LINES`) with a jump rail that tracks
scrolling.

A help page that lies is worse than none, so most of it is **derived** rather
than typed. The page list, paths and Alt numbers come from the router's
`navPages()`; the `Alt+1..N` range is computed from how many nav pages exist; and
the per-page key tables are read out of each page's own `.des-foot` strip in the
DOM on every mount, verbatim, so they cannot drift from what the page advertises.
What is written by hand — globals, commands, glossary — is written against
`core/keys.js`, `core/cmd.js` and these docs.

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
