# Progress

## Current milestone

**M20 — the CLI collapses to three commands and the UI becomes the control plane.**

## What works

- **Everything is operated from the browser.** A new `/control` page (the 7th
  nav page; the `CTRL+1`–`CTRL+N` chord label is derived from the nav count, so
  it re-sized itself) carries 13 actions in 11 sections behind 12 buttons:
  recorder START/STOP, paper SUSPEND/RESUME and APPLY LIMITS, tracked-pairs
  RELOAD, the Kalshi SUBSCRIBE and Polymarket US SET TARGETS universe fields,
  and the jobs — RUN DOCTOR, PROPOSE PAIRS, BACKFILL SLUGS, REPLAY — plus a
  CANCEL on each running job.
- **The CLI is three commands**: `arb ui` (the server the buttons live in;
  flags are *starting* values now, plus a new `--read-only`), `arb doctor`
  (what you run when the UI will not start) and `arb replay` (also the worker
  the UI's `jobs.replay` spawns as a subprocess). `arb record` and the whole
  `arb pairs` subtree — `propose`, `list`, `backfill`, `show`, `confirm`,
  `reject` — are deleted, module and all. `infra/prometheus.yml` lost its `arb`
  job (`app:9000`): only `arb record` ever served that port, so it was a scrape
  target that could never come up.
- **One executor.** `ControlPlane.execute(action, params, *, confirm, actor)`
  is the only way anything in the runtime changes, and `POST
  /api/control/{action}` is the only HTTP write. The read-only refusal, the
  arm-then-confirm, the audit row and the metric are properties of the
  executor, not of thirteen handlers that each have to remember them. The
  browser renders from the server's own action descriptors (`GET
  /api/control`), so a button cannot claim a grade or a confirmation the server
  does not implement.
- **Server-side confirmation.** A first call with no token is armed: the server
  mints a single-use token with a 90 s TTL bound to a SHA-256 fingerprint of
  `(action, params)`, and answers `428` with the sentence to show. Confirming
  with changed parameters is refused ("the parameters changed since this action
  was armed") and burns the token. `jobs.propose` and `jobs.backfill` always
  confirm; `jobs.replay` confirms only with `--persist`, the one form that
  writes.
- **An audit trail in Postgres** (`control_actions`, migration `0004`) storing
  the effect sentence that was *shown*, verbatim, alongside the action,
  parameters, actor, result and error. Armings, refusals and failures are rows
  too. A G3 action's arming write is `required=True`, so an action that could
  not be recorded never becomes confirmable — verified live: with the table
  missing, `jobs.propose` returned `503` and did not run. G2 actions proceed
  and count `arb_control_audit_failures_total` instead, so a database problem
  cannot stop you turning the recorder off.
- **Jobs**: threads for `propose`/`backfill` (single-flight per group,
  progress-reported by phase), a **subprocess** for `replay` with its stdout
  streamed back line by line, in-process for `doctor`. Output is bounded at 500
  lines per job (overflow counted), finished jobs stay readable for 32 jobs, and
  every job is cancellable (SIGTERM then SIGKILL after 5 s for the subprocess).
- **Network security floor** (`src/arb/ui/security.py`, 332 lines). Before:
  a cross-site `POST` with `Origin: https://evil.example` returned `200` and
  `WS /ws` accepted any Origin. Now a pure-ASGI middleware — outside FastAPI,
  because the WebSocket handshake is the one scope Starlette's HTTP middleware
  never sees — checks `Host` against a loopback allowlist (kills DNS rebinding)
  and, on anything but `GET`/`HEAD`, refuses a foreign `Origin` or
  `Sec-Fetch-Site: cross-site`. Verified: cross-site POST `403`, same-origin
  POST `200`, `Host: attacker.example` `403`, cross-site WS closed. Plus a
  startup bind guard (non-loopback needs `ARB_ALLOW_REMOTE_BIND=1`) and
  `--read-only` / `UI_READ_ONLY=1`.
- **Runtime mutability, with the landmines it laid defused at the right layer**:
  `KalshiWSSource.set_tickers()` + `force_resync()` (costs a reconnect and a
  snapshot burst, documented on the method); `PolymarketUSRestSource.set_targets()`
  live with no reconnect, and an **empty** set is now a supported idle state
  rather than a `ZeroDivisionError` inside a supervised task;
  `BookManager.retain()`/`evict()` so a dropped market's book stops being
  re-sent to every new client forever; `set_venue_staleness()` retunes **live**
  books and returns the ids it touched, because growing the poll universe used
  to leave every existing book flapping STALE against a budget computed for a
  smaller one; `PaperTrader.suspend()`/`resume()`/`set_limits()` instead of
  rebuild, because a fresh trader re-opens the whole `max_notional` budget and
  discards the ledger; `pairs/run.py` borrowing the server's engine,
  `RunContext` and sink, because a second context on one `run_id` collides on
  `UNIQUE (run_id, ingest_seq)` and `Recorder._write_with_retry` retries
  forever — all recording would have stopped permanently with the REC pill
  still reading ON; and `replay.py`'s `SystemExit` becoming `ReplayError`,
  because `SystemExit` is a `BaseException` that Starlette's error middleware
  does not catch.
- **Verified live in a browser**: `/control` renders 11 sections and 12
  buttons; clicking START turned recording on; PROPOSE armed with the server's
  sentence and a 90 s countdown instead of running; RUN DOCTOR produced an
  11-line report still readable after it finished.
- **300 tests pass** (was 195 at M19) — 35 in `tests/test_control.py`, 28 in
  `tests/test_security.py`, 19 in `tests/test_cli.py` (which pins the `arb
  replay` argv the control plane builds and asserts the deleted commands are
  gone). `ruff check`, `ruff format --check` (97 files), `pyright` (0 errors)
  and `node --check` on all 16 JS modules (6,767 lines) are clean.
- **Open — read this before deploying:**
  - `uv run alembic upgrade head` is now **required** before the controls work.
    G3 actions fail closed without the `control_actions` table, so `/control`
    will answer `503` on PROPOSE, BACKFILL and a persisting REPLAY against a
    database that has not been migrated.
  - **`docker compose up -d` will not start the app container as it stands.**
    Compose runs `arb ui --top 20 --host 0.0.0.0` but sets no
    `ARB_ALLOW_REMOTE_BIND`, and `check_bind_host` refuses a non-loopback bind
    without it (confirmed: `RemoteBindRefused` for `0.0.0.0` with
    `allow_remote=False`, and `AppConfig().ui_allow_remote_bind` is `False`).
    `docker-compose.yml` needs `ARB_ALLOW_REMOTE_BIND: "1"` in the app service's
    `environment`, and `.env.example` documents neither of the new settings.
  - **Still no JS test harness.** 16 modules and 6,767 lines of browser code
    are checked by `node --check` and a manual browser pass, and nothing else
    (`docs/testing.md`). M19's open item is unchanged and now covers a page
    that starts and stops processes.
  - `/help` does not know about `/control` yet: `SCREENS` in `pages/help.js`
    has no `control` entry, so the READING THE SCREENS section renders "no
    description available for this page yet". The page *does* appear in HELP's
    PAGES list and its footer keys are read verbatim, both of which are derived
    from the live router.
  - There is no `ARB>` command for the control page — `MON`, `ARB`, `PAIRS`,
    `PAPER`, `SYS`, `HELP`, `BACK` and `DES` exist; `/control` is reachable
    only by the nav tab, its chord and its URL.
  - `arb_ui_requests_rejected_total` is declared in `src/arb/ui/security.py`,
    not in `src/arb/metrics.py` where the hard rules say every metric name
    lives. There is a `TODO(control-plane)` on it.
  - `books.py` changes a live book's staleness budget by writing
    `book._staleness_limit_ns` through a single documented `_retune()` helper;
    `Book` should grow a public setter.
  - `run_ui` constructs `PolymarketUSRestSource` with a `["__idle__"]`
    placeholder and clears it before `stream()` is ever called, because the
    constructor still rejects an empty list while `set_targets([])` does not.
    No request is made for it, but the constructor should accept empty.
  - Day 3's hard rule holds: still no code that places, amends or cancels an
    order. G4 has no implementation, by design.

### From M19

**M19 — the keyboard gets a focus model: typing can no longer write to the database.**

- **The reported bug is fixed.** "Why can't I type R, U and N in the search
  bar" was the symptom; the disease was that on `/pairs` with nothing focused,
  typing the word "RUN" ran `R` reload → `U` set the selected pair PROPOSED →
  `N` set it REJECTED — two Postgres writes from someone who believed they
  were typing. `core/keys.js` gave the active page's `onKey()` first refusal on
  every key including bare printables, *before* the `ARB>` line saw them, so
  any page with single-letter actions turned those letters into hotkeys.
- Keys now resolve by **scope**, derived from `document.activeElement` on every
  keydown, never from a mode flag: `TEXT` (an input owns every key), `LIST`
  (focus is inside a `[data-keyregion="list"]` row container — the page's
  action keys are live) and `COMMAND` (everything else — the `ARB>` buffer owns
  every printable, always). The printable branch sits ABOVE `page.onKey` in the
  resolution order, so a page can only ever be handed a bare letter in `LIST`
  scope; pages receive `onKey(e, scope)` and re-assert the same guard
  themselves. The old `cmd.buffer() === ""` guards in `monitor.js`, `paper.js`
  and `help.js` are deleted rather than widened — the buffer is empty precisely
  when you type the first character, so they could never protect the character
  that fires the action.
- Every page mounts with focus on `#cmd` (the router's mount-focus guard only
  takes focus when nothing better holds it, so DES's `{replace: true}`
  re-navigation on each arrow does not steal it). `↑`/`↓` from `COMMAND`
  focuses the list **without** moving the cursor, so row 0 is the next
  candidate. Clicking a row selects it but does **not** enter `LIST` scope.
  `Esc` leaves `LIST` back to `ARB>`. A printable with a non-empty command
  buffer keeps typing in every scope (the dirty-buffer rule), and an unclaimed
  printable in `LIST` blurs to `COMMAND` and then types, so `KXPRES` into a
  focused list gets you `KXPRES` rather than six swallowed keystrokes.
- One five-rung global Escape ladder (clear a field → leave an empty field →
  disarm and leave a list → clear the buffer → back to MONITOR); pages no
  longer implement Escape at all. `Enter` with a non-empty buffer always runs
  the command, whatever the scope.
- **Consequence grading** is now the standing rule for every future binding,
  recorded in `docs/decisions.md` M19: G0 view/scroll is free in any scope, G1
  navigation is a cheap chord, G2 one-row writes (`Y` `N` `U`) are one key in
  `LIST` scope with no autorepeat, G3 many-row writes (`Shift+Y`/`Shift+N`)
  arm then confirm with the exact count stated, and **G4 irreversible/money is
  never a hotkey** — a typed command plus a typed confirmation. G4 is written
  down now because day 3 is live order placement.
- Destructive keys reject `e.repeat`: a leaned-on `Y` was one POST per
  autorepeat, and `decidePair` advances the cursor under a status filter, so it
  walked the queue writing as it went. A pending `Shift+Y`/`Shift+N` disarms on
  the list's `focusout`, which covers Escape, TAB, a click on the detail pane
  and a page jump alike.
- **macOS**: the page chord is `CTRL+1`–`CTRL+6` / `CTRL+[` `CTRL+]` on macOS
  and `ALT` elsewhere — Option is the insert-special-character modifier there
  (`Option+1` types `¡`), so the old `ALT` chord was a typo generator on a Mac.
  One `NAVMOD`/`NAVLABEL` constant drives the nav hint, the keys strip, every
  page footer and `/help`; Alt stays live as an alias everywhere. A
  platform-detection bug was caught in testing:
  `navigator.userAgentData.platform` reports `"macOS"` with a lower-case m and
  short-circuits `navigator.platform`, so a case-sensitive `/Mac/` test
  mislabelled every Chromium browser on a Mac — the test is now
  `/mac|iphone|ipad|ipod/i`.
- The four history bindings are **deleted**, not re-mapped: `⌘[`/`⌘]` on macOS
  and `Alt+←`/`Alt+→` elsewhere are already the browser's own, and `chordHeld()`
  refuses `metaKey` so the app can never shadow them.
- `/pairs`: the status filter moved off `TAB` (which trapped focus — you could
  not `Tab` out of the page) onto `←`/`→`; real `CONFIRM` / `REJECT` /
  `UNDECIDE` buttons were added, because a decision only a letter can make is
  unreachable with a mouse; and in `LIST` scope the keys strip and the `ARB>`
  line show a reversed-video band naming the keys that are live. `/help`'s `/`
  binding is gone (`TAB` focuses its filter). MARKET and SYSTEM footers say
  `ESC MONITOR`, list pages say `ESC ARB>`, and every footer's live text is
  authored in its page module — `help.js` parses each page's key table out of
  that element, so a binding and its documentation change in one edit.
- Spec frozen before implementation as `.context/keyboard-model.md` (the model
  was picked 3-0 by a judged panel of user / implementation / safety judges) so
  that parallel agents coded against the same names.
- **Verified in a real browser** (see the checklist below): typing "RUN" on
  `/pairs` from the home position leaves the buffer reading `RUN`, the chips
  unchanged and **zero POSTs**.
- 195 tests (unchanged — this milestone is entirely browser code); ruff,
  pyright and `node --check` clean across all 15 JS modules, now 6,245 lines.
- **Open**: there is still no JS test harness. 15 modules and 6,245 lines of
  browser code are checked by `node --check` and by driving a real browser, and
  nothing else (`docs/testing.md`). That means this fix — a *safety* property,
  not a cosmetic one — is guarded only by the manual pass below. Re-run it
  after any change to `core/keys.js`, `core/router.js`, or any page's `onKey`,
  `regions`, `listRegion` or `keyHints`:

  | # | Do this | Expect |
  | --- | --- | --- |
  | 1 | Load `/pairs`, click nothing | `document.activeElement` is `#cmd`; keys strip reads `ARB> COMMAND` |
  | 2 | Type `RUN` | buffer shows `RUN`; PROPOSED/CONFIRMED/REJECTED chips unchanged; **zero** `POST /api/pairs/*` in the network log |
  | 3 | `Esc` | buffer clears, focus stays on `#cmd` |
  | 4 | `↓` | focus moves to `#pair-rows`; cursor does **not** move (row 0 is still the candidate); `ARB>` becomes a reversed-video `LIST` band |
  | 5 | `Y` | one POST; CONFIRMED count +1 (observed 94 → 95); list head reads `#40 CONFIRMED` |
  | 6 | `UNDO` chip | CONFIRMED back to 94 |
  | 7 | Hold `Y` for ~2 s | exactly one POST, not one per autorepeat |
  | 8 | `Esc`, then type `Y` `N` at the `ARB>` line | buffer reads `YN`; CONFIRMED unchanged; zero POSTs |
  | 9 | Click a pair row, then type `Y` | it selects and shows the detail, but `Y` **types** — scope stayed `COMMAND` |
  | 10 | `Shift+Y` once, then `Esc` | armed prompt states a count, then disarms; zero POSTs |
  | 11 | `Shift+Y` once, then click away | armed prompt disarms on `focusout`; zero POSTs |
  | 12 | `CTRL+2` (macOS) / `ALT+2` | navigates to `/arb`; `ALT+3` still works as an alias on macOS |
  | 13 | Read the nav hint | `CTRL+1-6 PAGE · CTRL+[ ] CYCLE` on macOS, `ALT+…` elsewhere |
  | 14 | `TAB` on `/pairs` and on `/help` | focus enters the filter box and `TAB` again returns to `ARB>` — the page never traps it |
  | 15 | `⌘[` (macOS) / `Alt+←` | browser history back; the app does not intercept it |

### From M18

**M18 — the terminal becomes a multi-page app: real URLs, ES modules, SYSTEM and HELP.**

- The browser UI is a real multi-page app. Seven routes, all served by the one
  `arb ui` process on one port: `/` MONITOR, `/arb`, `/pairs`, `/paper`,
  `/system`, `/help`, and `/market/<market_id>` (DES, reachable by Enter or
  the `DES` command, not in the nav). Every screen is bookmarkable, reloads
  cold, and browser back/forward works. Previously DES, PAIRS, ARB and PAPER
  were `role="dialog"` overlays hidden inside the monitor's own document, with
  no URLs and no way to discover them except by typing a command.
- Routing is client side over the History API precisely so the session keeps
  **one** WebSocket: navigation calls `mount`/`unmount` and toggles a root
  element's `hidden`, never `connect()`. Verified live in Chrome — the tape
  kept counting (5 → 10 MSGS) across a full six-page round trip and the
  connection pill never left LIVE.
- `src/arb/ui/static/app.js` (1,886 lines, one IIFE) is **deleted**. In its
  place: 15 ES modules, 5,461 lines — `js/main.js`, `js/core/` (state,
  format, dom, ws, router, keys, cmd) and `js/pages/` (monitor, market, arb,
  pairs, paper, system, help) — plus `style.css` for the shell and shared
  components and one `css/<page>.css` per page. No build step, no npm, no
  dependency, no external request; `<script type="module">` is the whole
  loader. Each page module default-exports
  `{id, path, title, nav, root, mount, unmount, render, onKey}`.
- `src/arb/ui/server.py` enumerates the shell routes (`SPA_ROUTES` plus
  `/market/{market_id:path}`) rather than using a catch-all, so `/api/*`,
  `/ws`, `/metrics` and `/static/*` cannot be shadowed and a typo'd URL is
  still a 404 instead of a silent 200. `market_id` is deliberately not
  validated: a deep link to a market that rolled off discovery opens and lets
  the page report the miss.
- New page `/system`: engine counters, recorder state, database status and the
  Polymarket US block — all four of which the restructure had dropped on the
  floor — restored from the pre-multipage source, plus Kalshi/Polymarket
  health cards and a CLOCK & LATENCY block that explains on the page why a
  one-way reading can go negative and points at `uv run arb doctor`.
- New page `/help`: keyboard reference, command reference, how to read every
  screen column by column, and a glossary. Most of it is derived rather than
  typed — the page list, paths and nav-chord numbers come from the router's
  `navPages()` (and the modifier's label from `NAVLABEL`, M19), and each
  page's key table is parsed out of that page's own footer strip on mount —
  so it cannot drift from the app.
- Two reported bugs fixed on the monitor: the header said `MONITOR — KALSHI`
  while the list held both venues (now `MONITOR — KALSHI 13 · POLYMARKET US 9`
  from live counts, and the stat line reflects the filter), and the `#` column
  went blank after row 9 (every row is numbered; the first nine carry the 1-9
  quick-select shortcut and say so by emphasis).
- Also new on the monitor: a VENUE column, a ticker/title/id filter box, an
  ALL/K/PM venue filter and sortable columns — all persisted in
  `localStorage` under `arb.monitor.v1`, and applied by moving existing row
  nodes so a re-sort never costs the selection or a drag in progress.
- New keys: `ALT+1`–`ALT+6` jump to a page, `ALT+[` / `ALT+]` cycle,
  `ALT+←` / `ALT+→` are history back/forward. *(M19: the chord is `CTRL` on
  macOS and `ALT` elsewhere, and the history bindings are gone — the
  browser's own do it.)* Escape is global — it clears a half-typed command,
  otherwise it returns to MONITOR. New `ARB>` commands:
  `MON`/`MONITOR`, `SYS`/`SYSTEM`, `HELP`/`?`, `BACK`; `PAIRS`, `ARB`,
  `PAPER`, `DES`, `<TICKER>` and `<TICKER> DES` all still work and now
  navigate instead of opening an overlay.
- Two screens stopped asserting things that were not true: ARB no longer
  presents the paper trader's threshold as in force when no trader is running
  (`/api/paper` serves `PaperLimits()` defaults with `enabled: false`), and
  PAPER no longer draws capacity percentages against limits nothing is
  enforcing. PAIRS' `Shift+Y`/`Shift+N` bulk decide now acts only on visible
  rows and arms on the first keypress. See `docs/decisions.md` M18.
- It was a port, not a rewrite: the moved functions were diffed against the
  pre-multipage `app.js` function by function and most are byte-identical
  modulo indentation; the few intentional changes carry a comment saying so.
- **Open**: there is still no JS test harness. 15 modules and 5,461 lines of
  browser code are checked by `node --check` and by driving a real browser,
  and nothing else — the largest untested surface in the repo
  (`docs/testing.md`).
- 195 tests; ruff, pyright, `node --check` clean.

### From M17

- Root-caused the negative latency panel (MED −14.4 ms, SKEW −27 ms): the one-way sample subtracts Kalshi's wall clock from the local one, so it measures `true_transit + (local − venue)`; the local clock was ~20 ms behind (`sntp` +0.0197 s, the new `arb doctor` check −19.7 ms). Nothing in the trading path was affected — staleness, `BookManager`, `ArbMonitor` and replay are all monotonic — so it was a measurement/visibility defect.
- `arb doctor` gained an `ntp clock` check backed by `src/arb/clock.py`, a stdlib SNTP client (RFC 4330; 4 samples, lowest round trip wins): warns at `|offset| > 25 ms` *or* a lag past 5.5 ms, never fails, degrades to `warn` when UDP 123 is blocked, sign is local-minus-server to match the UI.
- Four metrics — `arb_ws_one_way_latency_ms` (buckets spanning negative; a count at `le="0"` is proof of clock offset), `arb_ws_one_way_latency_negative_total`, `arb_ws_rtt_ms`, `arb_clock_skew_ms`, the gauges going `NaN` rather than stale — plus a Grafana "Clock & latency" row (36 panels). Deliberately **not** done: de-biasing the sample; see `docs/decisions.md` M17.
- Latency sparkline stopped lying: negative samples pin to the bottom edge with an amber tick instead of drawing off-canvas, and a delta-free second renders as a gap instead of repeating the never-reset `latency_ms.last`.
- **Open**: the host clock is still ~19 ms behind. `sudo sntp -sS pool.ntp.org` needs an interactive password; run it to clear the warn. Runbook in `docs/ops.md` ("Host clock discipline").
- 189 tests; ruff, pyright, `node --check` clean.

### From M16

- Latency panel: the Kalshi source exposes the keepalive RTT (`ReconnectingWebSocket.rtt_s`); stats carry `rtt_ms` and `clock_skew_ms = median − rtt/2`; the panel shows RTT and SKEW and flips to `CLOCK SKEW · TRUST RTT/2` when the one-way median is negative or |skew| > 25 ms. Observed live: median −24.5 ms, RTT 24 ms → skew ≈ −36 ms (local clock behind the venue; see the `sntp` fix in the conversation notes).
- Real NO-side Kalshi delta captured (`ws_orderbook_capture_no_side.jsonl`, 12 markets, 2 `side: "no"` deltas) via the now-parameterized capture script; parser test pins the complement mapping (NO bid at 0.7500 → YES ask at 2500 ticks, −35.32 contracts).
- 160 tests; ruff, pyright, `node --check` clean.

### From M15

- `src/arb/replay.py` + `arb replay [RUN_ID|latest]`: recorded raw messages replayed in order through the identical adapters/`BookManager` with the recorded monotonic clock; per-stream counts, parse errors, final book validity, per-pair edge stats; `--paper` runs the trader over history, `--persist` stores trades as `replay:<run_id>`.
- `src/arb/paper.py` + `paper_store.py` + alembic `0003`: `PaperTrader` with `PaperLimits` (min net/contract, max contracts per pair, max total cost), proportional partial fills, positions and totals; `arb ui --paper` fills on live quotes and persists per run; `GET /api/paper`; terminal `PAPER` page (ledger, positions, trades tape, limits) built by a review-scoped agent.
- `src/arb/pairs/tracked.py`: one loader for confirmed pairs + fee parameters shared by `arb ui` and `arb replay`.
- 159 tests (paper limits/scaling, fixture-driven replay); ruff, pyright, `node --check` clean.

### From M14

- `src/arb/fees.py`: exact integer fee models — Kalshi (0.07·mult·C·P·(1−P), 6-dp round-up then tick alignment, maker 0/0.25/0.5 by `fee_type`) and Polymarket US (Θ·C·p·(1−p), banker's cents, maker rebate) — verified against both venues' documented examples. `src/arb/edge.py`: depth-aware two-direction edge walk in tick·Qty integers.
- `src/arb/arbmon.py`: quotes every tracked confirmed pair off the live books; `arb ui --pairs-top N` resolves fee parameters at startup (Kalshi `GET /series/{ticker}` with event overrides; Polymarket `GET /v1/markets?slug=…`), subscribes both legs, broadcasts an `arb` snapshot on every relevant book change, `GET /api/arb`.
- Terminal `ARB` page: ranked table (net/ct, size, gross, fees, direction, both BBOs, book state with QUIET for quiet-but-live books), detail with legs, fee model, reverse direction; Enter jumps to the Kalshi leg's DES.
- Live: 28 Bitcoin year-end ladder pairs confirmed as a verified test set (identical CF BRTI resolution); the monitor showed real +0.2 to +1.0¢/contract net edges on thin Polymarket books, and surfaced that the `KXBTCY` series carries `fee_multiplier 0`.
- 155 tests; ruff, pyright, `node --check` clean.

### From M13

- `src/arb/pairs/`: `text.py` (venue-vocabulary normalization, name similarity with containment), `matcher.py` (blocked, IDF-weighted title similarity + outcome overlap → one-to-one outcome pairing; explainable features), `store.py` (chunked upserts that never overwrite a human decision; list/decide/decide-many), `run.py` (`arb pairs propose` fetches both universes — Kalshi `/events` cursor pages, Polymarket `/v1/events` at `limit=500` — records them, proposes, persists). Alembic `0002` adds the `pairs` table. *Superseded by M20: the `arb pairs` CLI is deleted; `run.py` is now driven by the `jobs.propose`/`jobs.backfill` controls.*
- Universe fetchers: `kalshi.discovery.fetch_universe` / `event_refs`, `polymarket_us.discovery.fetch_active_markets` (429-retry) / `event_refs`.
- Review in the terminal: `PAIRS` command → list sorted by score with both legs, right-hand detail with both rules texts and match features; ↑↓ select, Y/N decide, Shift+Y/Shift+N decide the whole event pairing, U undecide, TAB cycles proposed/confirmed/rejected/all. API: `GET /api/pairs`, `POST /api/pairs/{id}/decide`, `POST /api/pairs/decide` (batch).
- Evaluated on the live universes (6,000 Kalshi events × 3,563 Polymarket events, 88k markets): thousands of high-confidence proposals — identical-title events (NFL divisions, EPL/Serie A, Bitcoin/Musk/gas-price ladders, Supreme Court) at 1.0, MVP/Cy Young/ROTY at ~0.96, state governor/senate races at 0.95. Real data fixes along the way: Polymarket `minimumTradeQty` can be `0.01` (Decimal now), asyncpg's 32,767-parameter cap (chunked upserts).
- 138 tests; ruff, pyright, `node --check` clean.

### From M12

- `src/arb/venues/polymarket_us/{discovery,source,adapter,detail}.py`: category-ranked discovery over `/v1/events` (recorded), a rate-budgeted round-robin book poller (`PolymarketUSRestSource`, an `EventSource`), an adapter emitting unsequenced snapshots + book stats, and a DES payload (tick size, fee coefficient, min qty). `level_deltas` in `books.py` diffs consecutive snapshots into tape events. Per-venue staleness in `BookManager`.
- `arb ui`/`arb record` take `--poly-top N` / `--poly-slugs` (*M20: `arb record` is gone; the poll targets are a `/control` field*); both venues are recorded (`rest:events`, `rest:book`). Status reports `polled`; the terminal shows K/P badges, an amber `POLY US POLLED` pill, and live poll stats in the Polymarket panel; DES works for both venues.
- **Measured the real Polymarket limiter**: a 5-token bucket refilling ~1 token/2 s on the book endpoint (docs claim 20 req/s). Default poll rate 0.45 req/s; verified zero 429s at that rate.
- Infra (M16, committed separately): Grafana "ARB — Data Plane" dashboard (31 panels) provisioned and loaded; compose app container now runs the recording terminal with `restart: unless-stopped`.
- 134 tests; ruff, pyright, `node --check` clean.

### From M11

- Enter / `DES` / double-click opens a Bloomberg-style description page for the selected market: event title and candidate, ticker anatomy (series → event → market), full resolution rules, settlement sources, status/category/type/mutually-exclusive/early-close, venue quote with implied probability, live book summary (best levels, depth totals, age), lifetime + 24h volume, open interest, open/close/expected-expiration in UTC and ET. Arrows page between markets; Esc closes.
- Backend: `GET /api/markets/{market_id}` served from discovery-seeded metadata with a 30 s TTL live refresh via the verified `GET /markets/{ticker}` and `GET /events/{event_ticker}` (both recorded before parsing); 404 for unknown markets. New parsers + `build_market_detail` tested against real captured fixtures.
- Verified in real Chrome over CDP: open/page/close/command flows; live refresh observed (`LIVE · 1s AGO`). 125 tests; ruff, pyright, `node --check` clean.

### From M10

- Selecting anything in the terminal copies it to the clipboard and shows a bottom-right `COPIED · N CHARS · N ROWS` toast (red `COPY BLOCKED` if the browser refuses). Multi-row selections copy as TSV (tab-separated cells, one row per line) so ladder/tape/system selections paste into a spreadsheet with columns intact; a selection inside a single cell copies the exact highlighted substring. Live re-rendering pauses while the pointer is down so updates can't wipe a selection mid-drag, then flushes on release.
- Verified in real Chrome over the DevTools protocol: multi-row ladder → `"1.90\t98.10\t8,838.22\n1.80\t98.20\t1,550"`, system rows → `"MSG TOTAL\t507\nRATE 1S\t0.0/s"`, partial cell → exact substring, plain click copies nothing, UI resumes after the drag, toast auto-hides.

### From M9

- `uv run arb ui` at http://127.0.0.1:8080 — black/amber terminal: live market monitor (real volumes + event titles from discovery), depth ladder with complement NO prices, mid/spread seam and flash-on-change, tape, latency sparkline (last/median/p95), system panel (recorder, parse errors, seq gaps, DB rows by run), Polymarket US down-screen driven by live REST reachability, keyboard navigation, dual UTC/ET clocks.
- Backend: `BookManager` (`src/arb/books.py`) + `KalshiMarketDataAdapter` (per-`sid` seq tracking; gap → metric + `ResyncRequired` + forced WS reconnect for fresh snapshots per the reliability rules); FastAPI server (`src/arb/ui/server.py`) with `/api/status`, `/metrics`, and a WS push protocol (integer ticks / 0.0001-contract units on the wire); recorder-first ingest identical to `arb record` (*M20: `arb ui` is the only recorder now*); per-client bounded send queues so a slow browser can never stall the feed.
- Frontend: three static files, vanilla JS/CSS, no build step, no external requests; design synthesized from a judged three-way panel; staleness shown as calm "QUIET", red INVALID reserved for structural book failures. (M18 replaced the three files with 15 ES modules and moved the system/Polymarket panels to `/system`; no build step and no external requests still hold.)
- Verified live end-to-end (2026-09-14): REST + WS contract probed, headless-Chrome renders confirmed live books, tape deltas, latency ~18 ms median, recorder rows growing in Postgres during viewing.
- 119 tests; ruff, pyright, `node --check` clean.

### From M8

- `src/arb/venues/kalshi/discovery.py`: liquidity-ranked discovery via `/events?with_nested_markets=true` (documented params only); every REST response goes to the recorder before parsing.
- `src/arb/venues/kalshi/source.py`: `KalshiWSSource` — shared `ReconnectingWebSocket` + signed handshake (headers recomputed per attempt) + resubscribe with fresh cmd id on every (re)connect.
- `src/arb/record.py` + `uv run arb record` (*deleted in M20 — `arb ui` does this, and recording is a `/control` toggle*): sources → recorder → Postgres, supervised tasks, Prometheus metrics server (`metrics_host:metrics_port`; compose sets `METRICS_HOST=0.0.0.0` for in-network scraping), graceful drain on shutdown (`Recorder.drain` via queue join).
- **Verified live end-to-end (2026-09-14)**: 25 s run recorded 2 discovery pages + 10 WS frames into `raw_messages` with contiguous `ingest_seq` under one `run_id`; payload bytes byte-exact in Postgres.
- 99 tests; ruff and pyright clean.

### From M7

- Compose stack running and verified (2026-09-14): Postgres+pgvector healthy, Prometheus ready, Grafana healthy with provisioned datasource, app container built — every port bound to 127.0.0.1 only.
- Migration `0001` applied to real Postgres; verified a live write/read roundtrip through `insert_raw_messages` (rows cleaned up afterwards).
- `uv run arb doctor` exits 0 locally (all ok except the expected Polymarket US keys warn) and **inside the container** via `docker compose exec app uv run arb doctor` (DB over the compose network, keys via mounted `secrets/`, env via `env_file`).
- Dockerfile sets `UV_NO_SYNC=1` so in-container `uv run` uses the baked `--no-dev` environment instead of re-syncing at runtime.
- Note: Prometheus's `arb` scrape target (`app:9000`) stays down until `arb record` serves `/metrics` — expected. *M20 removed that job: nothing binds `:9000`, and Prometheus scrapes `arb ui`'s own `/metrics`.*

### From M6

- `src/arb/venues/kalshi/auth.py`: RSA-PSS request/handshake signing (doc-verified scheme), tested with throwaway keys; real key confirmed working against the production WS.
- `src/arb/venues/kalshi/ws.py`: subscribe command + `orderbook_snapshot`/`orderbook_delta` parser (signed `delta_fp`, NO-side folded by complement); non-book frames yield no events.
- Live WS capture committed (`tests/fixtures/kalshi/ws_orderbook_capture.jsonl`: 1 ack, 5 snapshots, 15 deltas from five liquid markets) via `scripts/capture_kalshi_ws.py`; replaying the whole capture through `Book`s stays valid and uncrossed.
- **Key protocol finding (recorded in venue-notes): WS `seq` is per-subscription, not per-market** — Kalshi books will run unsequenced with subscription-level gap detection (`get_snapshot` for recovery).
- 96 tests; ruff and pyright clean.

### From M5

- `uv run arb doctor` (`src/arb/doctor.py`): env/.env presence, key provisioning (paths only, never contents), venue reachability + clock skew via HTTP Date (verified live: both venues 200, skew ≈ +0.2 s), database + migration state, free disk. Exits non-zero only on failures. Verified end-to-end against production endpoints; database check correctly FAILs while the compose stack is down (Docker wasn't running on this machine).
- CLI runs under uvloop; `arb` with no command prints help.

### From M4

- Quantities generalized to fixed-point `Qty` (0.0001-contract units) after live Kalshi books showed fractional counts (`"15.17"`).
- `src/arb/venues/kalshi/rest.py`: `parse_markets_response` (cursor pagination) and `parse_orderbook_response` — NO bids folded into YES asks by complement at the edge.
- `src/arb/venues/polymarket_us/rest.py`: `parse_markets_response` (Decimal-exact tick size / fee coefficient) and `parse_book_response`.
- Real fixtures captured live 2026-09-13 into `tests/fixtures/{kalshi,polymarket_us}/` (markets pages + liquid order books); parser tests pin exact normalized values and apply snapshots into `Book` cleanly.
- Empirical findings recorded in venue-notes: Kalshi REST market data is public in practice (docs conflict noted), `/markets` listing is flooded with zero-volume multivariate shards (discover via `/events`), Polymarket market objects carry undocumented fields.

### From M3

- `src/arb/config.py`: `AppConfig` (pydantic-settings, `.env`) with doc-verified endpoint defaults; secrets only as file paths.
- `src/arb/storage/`: SQLAlchemy 2.0 async `raw_messages` model + Alembic (async env, URL from `AppConfig`); migration `0001` renders correct Postgres DDL (BIGSERIAL, BYTEA, timestamptz, unique `(run_id, ingest_seq)`).
- `src/arb/recorder.py`: bounded-queue recorder — non-blocking `enqueue` (drop+count on overflow), batching writer, in-place retry with backoff, supervised.
- Infra: `docker-compose.yml` (Postgres+pgvector, Prometheus, Grafana, app — all on 127.0.0.1), `Dockerfile` (uv, layer-cached), Prometheus scrape config, Grafana datasource provisioning.
- Recorder metrics: enqueued/dropped/written/write-failures counters + queue-depth gauge.
- 71 tests; ruff and pyright clean.

### From M2

- `src/arb/run.py`: `RunContext` — `run_id` (sortable UTC stamp + suffix) and the per-run cross-source `ingest_seq`.
- `src/arb/supervise.py`: `Backoff` (exponential, jittered, resettable) and `supervise()` — restarts long-running tasks with backoff, never swallows cancellation.
- `src/arb/ws.py`: `ReconnectingWebSocket` (an `EventSource`) — per-attempt connector (fresh signed headers every attempt), resubscribe callback on every connect, stall detection via recv timeout, protocol ping/pong heartbeat in the default `websockets` connector, `RawMessage` stamping.
- New metrics: `arb_ws_connects_total`, `arb_ws_connect_failures_total`, `arb_ws_disconnects_total{reason}`, `arb_supervisor_restarts_total`.
- 64 tests; ruff and pyright clean.

### From M1

- `src/arb/types.py`: `Ticks` ($0.0001 units), exact dollar-string ↔ ticks conversion, price bounds, complement, `BookSide`, `RawMessage` envelope (`recv_ts_ns`, `recv_mono_ns`, `run_id`, `ingest_seq`).
- `src/arb/book.py`: normalized `Book` — YES bid/ask ladders best-first, complement-derived NO views, snapshot + SET/DELTA level updates, sequence-gap/crossed/bad-level/staleness validity rules, sticky structural invalidation with `needs_resync`.
- `src/arb/interfaces.py`: `EventSource` and `MarketDataAdapter` protocols, `BookEvent`, `ParseError`.
- `src/arb/metrics.py`: first counters (`arb_book_invalidations_total`, `arb_parse_errors_total`).
- 53 tests (unit + hypothesis) covering tick math and all book validity transitions; ruff and pyright clean.

### From M0

- Repository structure and Python packaging (`pyproject.toml`, src layout, `arb` entry point).
- Tooling config: ruff, pyright (standard mode), pytest (+ pytest-asyncio, hypothesis).
- Secret hygiene: `.gitignore` excludes `.env`, `secrets/` and key files; `.env.example` holds paths and non-secret config only.
- Doc scaffolding: `docs/venue-notes.md`, `docs/decisions.md`, this file.
- Package skeleton: `src/arb/` with `metrics.py` placeholder, `venues/{kalshi,polymarket_us}/`, and a minimal `cli.py` (`arb` prints "not implemented yet").
- Test scaffolding: `tests/` with `fixtures/{kalshi,polymarket_us}/`.
- Kalshi API facts verified against docs.kalshi.com and recorded in `docs/venue-notes.md` (hosts, RSA-PSS auth, WS channels, orderbook snapshot/delta format, fixed-point dollar-string prices, rate limits, discovery endpoints).
- Polymarket US API facts verified against docs.polymarket.us and recorded in `docs/venue-notes.md` (gateway REST is public; markets WS needs Ed25519 API-key auth on handshake; full-book WS messages with no seq numbers; whole contracts; limit/offset pagination; fee formula Θ·C·p·(1−p)).

## What's next

Day 3 (live trading at tiny size). The control plane is the half of it that
exists; the other half is the part with money in it:

- Fix the two deployment gaps above first: `ARB_ALLOW_REMOTE_BIND` in
  `docker-compose.yml` (the app container will not start without it) and
  `alembic upgrade head` in the deploy path.
- Order placement, amend and cancel — still unwritten, still gated on a prompt
  that asks for it. It is **G4**, so by the rule set in M19 it is never a
  hotkey and never a one-click button: a typed command plus a typed
  confirmation, through the same executor, with the same audit row.
- Venue credentials for authenticated order entry, and a kill switch that is
  reachable when the UI is not (the one control that must not live only in a
  browser page).
- A JS test harness, or an explicit written decision to keep verifying the
  browser by hand.

## Open questions

- Credentials not yet provisioned, and **both venues require authenticated WebSockets even for public market data**. Kalshi: Key ID + RSA PEM (demo or production, user's choice). Polymarket US: Key ID + Ed25519 secret from polymarket.us/developer (app signup + KYC; no sandbox). Until then, Polymarket books can be polled over unauthenticated gateway REST at 20 req/s/IP; Kalshi has no unauthenticated fallback.
- Kalshi: WS gap-recovery procedure unspecified in docs (we chose resubscribe + fresh snapshot); exact WS field names to confirm against asyncapi.yaml; whether `GET /markets`/`GET /events` need auth; market categorization source; fractional contract counts vs integer-quantity assumption.
- Polymarket US: WS wire format is contradictory in the docs (snake_case + numeric enums vs camelCase + string enums) — settle from captured payloads; no seq numbers on the markets WS, so validity rests on staleness + `transactTime` + periodic REST reconciliation; REST book depth and heartbeat cadence undocumented; rules-text field unclear (`description` vs `rulesDisclaimer`).
