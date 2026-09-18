# Decisions

Design choices and why. Newest first.

## M9 — terminal UI (`arb ui`)

- **One process: visualize + record.** `arb ui` runs the same
  recorder-first ingest as `arb record` (every raw frame enqueued before
  parsing) and additionally parses through the Kalshi adapter into
  `BookManager`, broadcasting to browser clients. `--no-record` exists for
  DB-less viewing. *Completed by M20:* `arb record` is deleted — this was
  always the same ingest path twice — and `--no-record` now only sets the
  starting value of a runtime toggle on `/control`.
- **Frontend is three static files, zero build step, zero external
  requests.** Vanilla JS/CSS served by FastAPI; system mono font stack; no
  CDN. A Python repo should not grow a node toolchain for one page.
  *Superseded in part by M18:* the file count is now 15 ES modules and 8
  stylesheets, but the no-build-step and no-external-request rules held.
- **Wire format keeps integers.** Prices cross the WebSocket as ticks and
  quantities as 0.0001-contract units; the browser formats (ticks/100 =
  cents). No float drift server-side.
- **A slow browser must never stall ingest.** Broadcast is non-blocking:
  per-client bounded queues (1024) drained by per-connection sender tasks;
  an overflowing client is dropped (metric
  `arb_ui_ws_clients_dropped_total`).
- **Fresh snapshot after any gap, via forced reconnect.**
  `ReconnectingWebSocket.force_reconnect()` closes the live connection; the
  loop reconnects and resubscribes, and Kalshi answers every subscribe with
  full snapshots. The cheaper `update_subscription`/`get_snapshot` path
  stays an open item.
- **UI presents staleness as QUIET, not INVALID.** The Book's 5 s staleness
  rule is a trading-validity gate; a prediction market that simply hasn't
  ticked is normal. The depth banner shows a dim "QUIET · LAST UPDATE Ns
  AGO"; red INVALID is reserved for structural reasons (seq gap, crossed,
  bad level).
- **Design chosen by a judged panel** (Bloomberg purist vs modern desk vs
  density maximalist): black/amber terminal chrome, tabular-nums data,
  flash-on-change, depth bars, function-key strip, and an intentional
  Polymarket US down-screen driven by live REST reachability.

## M21 — the frontend gets tested, in two layers

- **Two suites, because the two shipped bugs failed in different layers.**
  Typing `RUN` on `/pairs` writing to Postgres was pure resolution-order logic
  in `onKey` — a unit test catches it. `/control`'s fields being uneditable was
  the browser dropping focus when a rendered-over `<input>` was removed — no
  unit test can see that. One harness would have caught one bug.
- **`node --test`, not vitest or jest.** The standing rule is that a Python
  project should not grow a node toolchain for one page, and node 22 ships a
  test runner, so the suite has no `package.json`, no `node_modules` and no
  dependency. It is the same binary already used for `node --check`.
- **The unit shim is a stub and says so.** It installs the handful of globals
  the core modules touch at import and nothing more. It deliberately cannot
  reproduce focus-stealing, and that was demonstrated rather than asserted: the
  pre-fix `control.js` was restored and the suite stayed green. Faking that
  behaviour would have produced a test that passes for the wrong reason.
- **The acceptance suite does not run `arb ui`.** It serves `create_app` with a
  stub state and the real static assets, wrapped in the same Origin guard
  `run_ui` applies. Real app, real JS, no venue traffic, no Postgres — a test
  suite that hits production venues on every run is a suite people stop running.
- **Keys go through CDP, never a synthetic `KeyboardEvent`.** A `KeyboardEvent`
  dispatched on `document` has `document` as its target, so `scopeOf(e.target)`
  reads COMMAND no matter what is focused. The `RUN` test would have passed
  against a completely broken key path.
- **The browser suite reads its action descriptors from a real `ControlPlane`.**
  The `/control` page renders entirely from them, so a hand-written copy is a
  suite driving a fiction — and the first one had already drifted (9 actions
  against 13, `pairs.top` published G3 when it is G2).
- **Every test was mutation-tested, and two were rewritten because they could
  not fail.** One waited on the wall clock for a 1 s tick, so deleting the tick
  left it green; one never consumed `Log.entryAdded`, so a 404'd stylesheet that
  left the page unstyled was invisible. A test that cannot fail buys false
  confidence, which is worse than an acknowledged gap.
- **Still no CI.** Both suites are things a human types. That is the remaining
  hole and it is recorded as one in `docs/testing.md` rather than implied away.

## M20 — the CLI collapses to three commands and the UI becomes the control plane

- **Three CLI commands survive, and each one survives for a reason a button
  cannot cover.** The ask was "remove all the CLI commands and instead add
  them as triggers in the UI", and the honest reading of that is *operating*
  the system, not *starting* it. So `arb record` and the whole `arb pairs`
  subtree (`propose`, `list`, `backfill`, `show`, `confirm`, `reject`) are
  deleted — module and all — and what is left is:

  | Command | Why it cannot be a button |
  | --- | --- |
  | `arb ui` | it is the process the buttons live in. Its flags are now *starting* values the UI changes from there, and `docker-compose.yml` runs `arb ui --top 20 --host 0.0.0.0`, so the flag names are a live dependency, not decoration. |
  | `arb doctor` | you run it when the UI will **not** start. A button inside a dead server diagnoses nothing. `/control` also has a `jobs.doctor` action, for when the server is alive and the database is not. |
  | `arb replay` | it is the **worker** the UI spawns (see below). It is a machine interface now as well as a human one, which means its argv is a contract — `tests/test_cli.py` pins the flags `ControlPlane._apply_replay` builds. |

  What made deletion safe rather than destructive is that `arb record` was
  never a separate capability: `arb ui` has run the same recorder-first ingest
  since M9, so the recorder command was a second way to spell a flag. Removing
  it also removed the last thing that bound `:9000`, so `infra/prometheus.yml`
  lost its `arb` job and now scrapes `arb ui`'s own `/metrics` and nothing
  else — a scrape target that can never come up is an alert that means nothing.
- **`replay` is a subprocess, not a coroutine, and that is a latency
  decision.** The obvious implementation of a REPLAY button is to `await
  run_replay(...)` in the server, and it would have been wrong in a way that
  looks like a venue outage. `replay.py`'s per-row loop contains no `await` and
  yields only every `BATCH = 2000` rows, so it holds the event loop for seconds
  to minutes; `ws.py` runs with `ping_timeout_s = 10.0`, so the Kalshi socket
  drops, reconnects, gaps and resnapshots every book — because someone clicked
  replay. `asyncio.to_thread` does not fix it either: the engine, the
  `RunContext` and the recorder sink are all loop-affine. A child process
  cannot stall this loop at all, and streaming its stdout back line by line
  gives the job log for free. The contrast with `propose`/`backfill` is
  deliberate and is the general rule: those already carry their one blocking
  stage (scoring) into a worker thread and keep every loop-affine object on the
  loop, so they are awaited in-process; `replay` has no such seam, so it leaves
  the process entirely.
- **One executor, not a route per toggle.** Every control goes through
  `ControlPlane.execute(action, params, *, confirm, actor)`, and the HTTP
  surface is one handler, `POST /api/control/{action}`. Thirteen handlers would
  have been thirteen chances to forget the read-only refusal, the
  arm-then-confirm, the audit row or the metric — and the forgetting is silent,
  because a toggle that works is indistinguishable from a toggle that works and
  records nothing. Here those are structural: an action is a row in an action
  table (`ActionSpec`: name, grade, summary, validate, effect, apply) and
  cannot opt out. The order inside `execute` is the design — unknown action →
  validate → read-only refusal → arm-or-confirm → apply → audit → broadcast —
  and validation runs *before* the refusal so a read-only server still tells
  you your parameters were wrong rather than hiding one failure behind another.
  The same table is what the browser renders from: `GET /api/control` returns
  the action descriptors, so the page cannot show a button the server does not
  have, or a grade the server disagrees with.

  | Action | Grade | Notes |
  | --- | --- | --- |
  | `recording.start` `recording.stop` | G2 | the recorder and its writer now always exist; recording is a flag read per message |
  | `paper.suspend` `paper.resume` `paper.limits` | G2 | one trader for the life of the run |
  | `pairs.top` | G2 | reloads tracked pairs; costs a Kalshi reconnect |
  | `universe.kalshi` | G2 | reconnect, snapshot burst, eviction |
  | `universe.polymarket` | G2 | live, no reconnect; retunes staleness |
  | `jobs.propose` `jobs.backfill` | G3 | always confirm — thousands of rows |
  | `jobs.replay` | G3 | confirms only when `persist` is set — the one form that writes |
  | `jobs.doctor` | G0 | `mutates=False`, so it runs in read-only mode |
  | `jobs.cancel` | G2 | SIGTERM, then SIGKILL after 5 s, for the subprocess |

- **Confirmation is server-side, and the token is bound to a hash of
  `(action, params)`.** A confirm dialog implemented in the browser is not a
  confirmation: it is a rendering choice that `curl -X POST` skips, that a
  second tab never sees, and that leaves nothing behind. So the first call with
  no token is *armed* — the server mints a single-use token with a 90 s TTL,
  fingerprinted over the canonical JSON of the action and its parameters, and
  answers `428` with the sentence to show. Binding the fingerprint is what
  stops the attack the arm/confirm split otherwise creates: arm
  `jobs.propose` at `min_score 0.75`, read a sentence about a conservative
  threshold, then confirm at `0.10` and write ten times the rows. Verified
  live — the second call is refused with "the parameters changed since this
  action was armed". A failed spend also *burns* the token, because one
  authorisation covers one exact set of parameters; letting the caller retry
  with the same token would make the binding advisory.
- **The audit row stores the sentence that was SHOWN, verbatim.** `effect` is
  a `Text` column in `control_actions` (migration `0004`), not something
  re-derived from `params_json` at read time. The point of the record is what
  the operator agreed to, and re-deriving it means the log silently changes
  meaning whenever the wording, a default or a fee model changes. The browser
  renders the same string it got in the `428` body, so the screen and the
  permanent record are the same bytes.
- **A G3 action cannot be armed without its audit row, which is how it fails
  closed.** The arming write passes `required=True`; every other audit write
  is best-effort. That asymmetry is the whole mechanism: a G3 action needs a
  token, a token is only minted after the arming row commits, so a G3 action
  that was never recorded was also never runnable. Verified live — with the
  `control_actions` table missing, `jobs.propose` returned `503` and did not
  run. G2 actions deliberately do *not* fail closed: refusing to stop the
  recorder because Postgres is unreachable turns a database problem into an
  inability to operate, so they proceed and increment
  `arb_control_audit_failures_total`, which is the thing to alert on.
- **Origin and Host checks, not CSRF tokens.** Before this milestone a
  cross-site `POST` with `Origin: https://evil.example` returned `200`, and
  `WS /ws` accepted any `Origin` at all — the same-origin policy simply does
  not apply to WebSocket, so any page the operator visited could read the live
  feed. `security.py` is a pure-ASGI middleware *outside* FastAPI, because the
  WebSocket handshake is the one scope Starlette's HTTP middleware never sees.
  It checks `Host` against a loopback allowlist (which is what actually kills
  DNS rebinding: a rebound request still carries the attacker's hostname) and,
  on anything but `GET`/`HEAD`, refuses a foreign `Origin` or
  `Sec-Fetch-Site: cross-site`. Verified: cross-site POST `403`, same-origin
  POST `200`, `Host: attacker.example` `403`. A missing `Origin` is allowed on
  purpose — that is `curl` and the replay subprocess, processes already on this
  machine that could speak to the socket directly. **CSRF tokens were rejected,
  not overlooked**: there is no login, no session and no user table, so there
  is nothing to bind a token *to*; a token minted and accepted by the same
  server with no session behind it is an Origin check with extra steps and a
  cookie jar to get wrong. If this is ever exposed for real, the answer is an
  authenticating proxy or the SSH tunnel that is already the documented way in.
  Two smaller pieces of the same floor: binding off loopback now requires an
  explicit `ARB_ALLOW_REMOTE_BIND=1` (refused at startup, before anything
  listens), and `--read-only` / `UI_READ_ONLY=1` serves every view and refuses
  every mutating action, so a tunnelled port can be shown to someone without
  handing them the controls.
- **Paper trading suspends; it is never rebuilt.** The tempting implementation
  of an on/off toggle is to construct a `PaperTrader` on and drop it off, and
  it would have quietly defeated the only risk limit that matters:
  `room_notional = max_notional_ticks - notional_ticks` is computed from state
  on the *instance*, so a fresh trader re-opens the entire spend budget from
  zero — each off-and-on cycle grants another full `max_notional`, so a
  "$1,000 max" becomes $1,000 per toggle — and takes the ledger and every open
  position with it. So one trader lives for the whole run (built
  even when `--paper` was not passed, starting suspended, so there is something
  to resume) and `suspend()`/`resume()` flip a flag that `consider()` checks
  first. `set_limits()` is live for the same reason and tightens without
  unwinding: there is no such thing as un-filling a fill, so a lowered
  `max_notional` leaves `room_notional` negative and stops new trades — a hard
  stop, not a rollback. That makes "tighten the limits" a usable panic button
  that never fabricates an exit.
- **A borrowed `RunContext`, because a second one wedges the recorder for
  good.** `pairs/run.py` now accepts an injected engine, `RunContext` and sink
  (`JobDeps`) and, when given them, constructs nothing and disposes nothing.
  The alternative — the job building its own `RunContext` — looks harmless and
  is the worst failure in this milestone: a second context on the *same*
  `run_id` hands out a second `ingest_seq` starting at 0, `raw_messages` is
  `UNIQUE (run_id, ingest_seq)`, `insert_raw_messages` has no `ON CONFLICT`,
  and `Recorder._write_with_retry` retries a failing batch **forever**. One
  duplicate therefore stops *all* recording permanently while the REC pill
  still reads ON. The standalone path never reuses a configured `RUN_ID`
  either, because the identical collision happens between two processes.
- **Three landmines the mutability itself laid, each fixed at the layer that
  owned it.** (1) `PolymarketUSRestSource` indexed `self._slugs[i % len(...)]`,
  so an empty target set was a `ZeroDivisionError` inside a *supervised* task —
  a crash-restart loop on a venue, forever. Empty is now a supported idle
  state, and `/system` reports it as "no poll targets" rather than a venue
  outage. (2) `BookManager.set_venue_staleness` applied only to books created
  afterwards, so growing the poll universe (which lengthens the cycle) left
  every existing book judged against a budget computed for a smaller one and
  flapping STALE; it now retunes live books and returns the ids it touched, so
  the caller can re-publish them. (3) Nothing ever removed a book, so a market
  dropped from the universe stopped updating, aged into permanent staleness and
  was re-sent to every newly connected client for the life of the process —
  hence `BookManager.retain()`, called by whoever changes a venue's universe.
- **`SystemExit` became a real exception.** `run_replay` raised `SystemExit`
  for "no recorded runs", which was fine while a CLI was the only caller.
  `SystemExit` is a `BaseException`: Starlette's error middleware only catches
  `Exception`, and asyncio task groups treat it as a shutdown request. The
  first time a button awaited that coroutine, "you have not recorded anything
  yet" would have taken the server down instead of returning a 4xx. It is
  `ReplayError`/`NoRecordedRuns` now, and the CLI turns it into an exit status.
- **The control page has no state of its own.** It renders from the server's
  descriptors, never flips a toggle optimistically (it waits for the POST, then
  re-renders from the `control` WebSocket frame, so two tabs cannot disagree),
  and shows the server's confirm sentence verbatim. Every screen in this app
  has drifted from its backend at some point; a page that can start a process
  which will later place real orders is the wrong place to keep a second copy
  of the truth.

## M19 — the keyboard gets a focus model, and keys get a price

- **The bug: typing was writing to the database.** On `/pairs` with nothing
  focused, typing the word "RUN" ran `R` reload → `U` set the selected pair
  PROPOSED → `N` set it REJECTED. Two Postgres writes from a user who
  believed they were typing in a search box; reproduced in a real browser
  with the REJECTED chip going 11 → 12. The root cause was the resolution
  order in `core/keys.js`: it called `page.onKey(e)` for EVERY key including
  bare printables, *before* the `ARB>` command line saw them. Any page with
  single-letter actions therefore turned every one of those letters into a
  hotkey, and the terminal's whole premise is that you type at `ARB>`
  without clicking anything first. This was not a `/pairs` bug — `/pairs`
  was merely the page whose letters cost the most.
- **The existing `cmd.buffer() === ""` guards were deleted, not extended.**
  `monitor.js`, `paper.js` and `help.js` each gated their letter on an empty
  command buffer. That guard is structurally incapable of doing the job: the
  buffer is empty *precisely* when you type the first character, so the
  first character is exactly the one it cannot protect — and the first
  character is what fires the action. Widening the condition (add a mode
  flag, add a timer, check whether a list is non-empty) would have kept a
  guard whose failure case is its intended case. What replaced it is
  ordering, not a condition: the printable branch moved ABOVE `page.onKey`,
  so a page is never *offered* a bare letter outside a list. Pages keep an
  explicit `if (scope !== SCOPE.LIST) return false;` anyway, so the
  guarantee survives a future edit to the core.
- **Three scopes, derived from `document.activeElement` on every keydown —
  not from a mode flag.** `TEXT` (an `<input>`/`<textarea>`/contenteditable
  owns every key), `LIST` (focus is inside a `[data-keyregion="list"]` row
  container, so the page's action keys are live) and `COMMAND` (everything
  else — `#cmd`, `<body>`, a focused button — where the `ARB>` buffer owns
  every printable, always). Focus is the state variable because it is the
  only one that cannot desynchronize from what the user sees: the browser
  maintains it, it survives a page mount, a click, a `Tab` and an alert, and
  it is already rendered (the focus ring and the reversed-video band are the
  same fact drawn twice). A `mode` boolean would be a second copy of that
  fact, and every escape path out of a mode — Escape, click-away, navigate,
  reload, a page that forgot to reset it — is a chance for the copy to
  disagree with the screen. A mode flag left stuck on `LIST` is the original
  bug again, and this time invisible. The invariant is stated once, in
  [`core/keys.js`](../src/arb/ui/static/js/core/keys.js): *the focused region
  owns every key; focus starts and ends at the `ARB>` line on every page.*
- **Clicking a row selects it but does NOT hand it the keyboard.** A click
  inside a list region blurs back to `#cmd`. This looks unhelpful until you
  price it: if clicking a row entered `LIST` scope, then clicking a pair to
  *read* its rules text — the reason the detail pane exists — would arm `Y`
  and `N` under the user's hands, and the next thing they typed would decide
  it. Mouse users get real `CONFIRM` / `REJECT` / `UNDECIDE` buttons in
  `#pair-detail` instead; the mouse route to a write is a button, never a
  letter that became live because you clicked nearby. Rows also stay
  non-focusable (selection is `aria-activedescendant`, never a roving
  `tabindex`) — a focusable row would put `Y` in a scope the resolver
  believes is `COMMAND`.
- **Consequence grading is now the standing rule for every binding.** The
  question "should this be a hotkey?" was being answered per page, by
  whoever wrote the page. It is answered here instead, and day 3 is live
  order placement, so the top row is the one that matters most:

  | Grade | Example | Price |
  | --- | --- | --- |
  | G0 view/scroll | arrows, filters, sort | free, any scope |
  | G1 navigation | page jump, DES | free, cheap chord |
  | G2 one-row write | `Y` `N` `U` | one key, LIST scope only, no autorepeat |
  | G3 many-row write | `Shift+Y` `Shift+N` | arm then confirm, count stated |
  | G4 irreversible / money | (day 3: live orders) | never a hotkey — typed command plus typed confirmation |

  G4 is the reason the table is in this file rather than in a page comment.
  Placing an order is not `Y` with a bigger confirmation dialog: it is a
  typed command plus a typed confirmation, because the whole point of the
  `ARB>` line is that what you typed is on screen before it happens. The
  G2/G3 rows also earned a concrete hardening — every decision key rejects
  `e.repeat`, since `decidePair` advances the cursor and a status filter
  drops the decided row, so a leaned-on `Y` marched down the queue POSTing
  once per autorepeat; and a pending `Shift+Y`/`Shift+N` disarms on
  `focusout`, because once Escape stopped reaching the page the old
  "any other key clears `armed`" side effect disappeared with it.
- **The page chord is CTRL on macOS and ALT elsewhere, from one constant.**
  Option is the insert-special-character modifier on macOS — `Option+1` types
  `¡`, `Option+[` types `“` — so `Alt+1`..`Alt+6` were not shortcuts on a
  Mac, they were typos. `NAVMOD`/`NAVLABEL` in `core/keys.js` is the single
  source for the modifier *and* its label, read by the nav hint, the keys
  strip, every page footer, `help.js` and `system.js`; a hand-edited "ALT+"
  in a footer is how the strip and the docs drift apart. Alt stays live as an
  alias everywhere (inert where the browser claims it) but is never *labelled*
  on a Mac. **The platform test is case-insensitive on purpose**, and this was
  a real bug caught in testing: `navigator.userAgentData.platform` reports
  `"macOS"` with a lower-case m and short-circuits `navigator.platform`
  (`"MacIntel"`), so the obvious `/Mac/` test mislabelled every Chromium
  browser on a Mac as non-Mac — it would have shipped the exact bug it was
  written to fix. The test is `/mac|iphone|ipad|ipod/i`.
- **The four history bindings were deleted, not re-mapped.** `Alt+←`/`Alt+→`
  collided with the new chord's modifier, and the tempting fix was to move
  them somewhere free. They went away entirely instead: `⌘[`/`⌘]` on macOS
  and `Alt+←`/`Alt+→` elsewhere are *already* the browser's own history
  controls, and re-implementing a binding the platform provides buys nothing
  and costs a chord plus a line in every key table. `chordHeld()` explicitly
  refuses `metaKey` so the app can never shadow them. Spare digits past the
  nav count return `false` rather than `preventDefault`, so an unbound
  `CTRL+9` belongs to the browser, not to a dead binding.
- **Escape is one global ladder; pages no longer implement it.** Five rungs,
  first match wins: a `TEXT` field with a value clears and stays → an empty
  field goes to `ARB>` → `LIST` disarms and goes to `ARB>` → a non-empty
  command buffer clears → anything that is not the monitor navigates to `/`.
  M18 had already moved Escape into the core; what M19 adds is that rung 3
  must disarm *before* it blurs, which is only expressible in one place.
  Correspondingly, `Enter` with a non-empty buffer runs the command in every
  scope: a typed command always wins over a page's `Enter` action, because
  the buffer is on screen and the page's intent is not.
- **Two focus-trap fixes fell out of the same model.** `/pairs` cycled its
  status filter on `TAB` — `TAB` is how a keyboard user *leaves* a region, so
  a page that eats it is a page you cannot get out of. The filter moved to
  `←`/`→`, which are non-printable and shadow nothing typeable. `/help`'s `/`
  binding is gone for the same family of reason (`/` at `ARB>` is now just a
  character) and `TAB` focuses its filter box instead. `↑`/`↓` from `COMMAND`
  focuses the list *without* moving the cursor, so row 0 is the next
  candidate and you can see where the keyboard went before anything acts.
- **The model was chosen by a judged three-way panel, 3-0.** Three
  interaction models were written up and scored by user-experience,
  implementation and safety judges; "REGION FOCUS — `ARB>` is home" was
  unanimous, and the winning spec was frozen as
  [`.context/keyboard-model.md`](../.context/keyboard-model.md) and
  implemented verbatim so that parallel agents coded against the same names.
  Same procedure as M9's design panel, and for the same reason: an
  interaction model is a taste question with safety consequences, and one
  author's taste is not evidence.
- **Still no JS test harness, so this is guarded by a manual browser pass.**
  M18's open item is unchanged and now carries more weight: the fix lives
  entirely in browser code, and `node --check` cannot see a resolution order.
  The acceptance pass is written down as a re-runnable checklist in
  [`PROGRESS.md`](../PROGRESS.md) rather than left in a commit message.

## M18 — the terminal becomes a multi-page app

- **Routing is client side over the History API, not one HTML document per
  page.** The constraint that decided it: *one WebSocket for the whole
  session.* The tape counter, the rolling latency window (300 one-second
  buckets) and every entry in `state.books` live in page memory, and a fresh
  document would take all of them with it — plus force the server to re-send
  `hello` and a full book payload per navigation (`ui/server.py`'s
  `ws_endpoint`). So `main.js` calls `connect()` exactly once in `boot()`,
  and `core/router.js` does nothing more violent than toggle a root
  element's `hidden` and call `unmount()`/`mount()`. The price is that the
  server must serve the shell at every route and a browser without JS gets
  the `noscript` bar; the screens were unusable without JS before this
  anyway.
- **ES modules, still no build step and no dependencies.** M9's rule — a
  Python repo should not grow a node toolchain for one page — was re-tested
  when "one page" became seven, and kept. `<script type="module">` plus
  native `import` already provides the module boundary a bundler would have
  been introduced for. What it buys: the file on disk is the file the
  browser runs, so a stack trace points at a real line, and `node --check`
  is the entire JS toolchain. What it costs: one request per module (cheap
  against the default 127.0.0.1 bind), no minification, and no npm test
  runner — so there is still no JS unit test and page behaviour is verified
  in a real browser instead. See `docs/testing.md`.
- **Every page is one module with a fixed shape, and the router owns its
  lifecycle.** `js/pages/<page>.js` default-exports
  `{id, path, title, nav, root, mount, unmount, render, onKey}` plus an
  optional `onMessage`. The router — not the page — sets `hidden`, sets
  `document.title`, and marks the nav tab. Reason: `mount`/`unmount` is the
  only reliable place to start and stop a timer or a fetch, and a page that
  also controlled its own visibility could leave itself on screen while
  "closed". The concrete bug this forecloses is the PAPER page's 3 s
  `/api/paper` poll outliving navigation. Registration order in `main.js`'s
  `PAGES` *is* nav order, so the nav strip, the nav-chord numbering and the
  page table on `/help` all read from one list. *(M19 made the modifier
  itself a constant too — `NAVLABEL`, `CTRL` on macOS and `ALT` elsewhere.)*
- **The rAF batch drops dirty keys nobody claimed, so ported pages
  re-register their old key.** `core/state.js` sweeps `schedule()` keys with
  no renderer rather than leaking them. The router auto-registers a page's
  `render` under its `id` only, so `pages/market.js` calls
  `registerRenderer("des", …)` and `pages/system.js` registers both
  `"system"` and `"poly"` — those are the names `ws.js`, `state.select()`
  and the status poll have always scheduled, and silently dropping them is
  how a screen stops updating with nothing in the console.
- **A page module that fails to import degrades to a stub, loudly.**
  `main.js` wraps each `import()` and falls back to a placeholder that keeps
  the route resolving and the nav tab working, after `console.error`. One
  broken page should cost that page, not the terminal — but it must not
  cost it silently, or a screen goes missing in production and looks empty.
- **The server enumerates its shell routes instead of using a catch-all.**
  `SPA_ROUTES = ("/", "/arb", "/pairs", "/paper", "/system", "/help")` are
  registered one by one, alongside a single `/market/{market_id:path}`.
  A catch-all would answer 200-with-the-shell for `/api/typo`, `/wss` or a
  renamed static asset, which turns every broken link and every stale
  endpoint into a blank screen instead of a 404. `/api/*`, `/ws`,
  `/metrics` and `/static/*` therefore keep their own handlers and an
  unknown path stays a 404 — pinned by
  `test_spa_routes_do_not_shadow_the_api` and `test_unknown_path_is_still_404`.
- **`/market/<id>` is the one deliberately unvalidated route.** The id is
  opaque to the server: a deep link to a market that has since rolled off
  the discovery list should open and let the page report the miss, rather
  than 404 a URL that worked yesterday. It is declared `{market_id:path}`
  so an id whose escaped form contains `%2F` survives ASGI's decode, and
  the client reads it as `decodeURIComponent(pathname.slice("/market/".length))`.
- **Escape is handled globally, once.** `core/keys.js` takes it before any
  page: a half-typed command is the innermost thing open so it is cleared
  first; otherwise anything that is not the monitor navigates to `/`. The
  keys strip has advertised `ESC CLOSE` since M9, and seven pages each
  implementing it is seven chances for one of them to disagree. Pages are
  forbidden from claiming Escape in `onKey`. *(Superseded in part by M19: the
  two rungs became five once TEXT and LIST scopes existed, and the strip now
  reads `ESC CLEAR` / `ESC ARB>` / `ESC MONITOR` depending on the scope.)*
- **This was a port, not a rewrite.** Each moved function was diffed against
  the pre-multipage `static/app.js` (now only in git history) and most are
  byte-identical modulo indentation and an `export` keyword; where one did
  change, the change carries a comment saying so — `pages/system.js`'s run
  list tagging the live run is the example. The reason is that these screens
  were verified against live venue data over M9–M17. A rewrite would have
  re-opened every one of those questions simultaneously and left no way to
  tell a restructure bug from an intended behaviour change.
- **`/help` derives itself from live sources rather than restating them.**
  The page list, paths and nav-chord numbers come from the router's
  `navPages()` (and the chord's label from `NAVLABEL`, M19); each page's key table is parsed out of that page's own
  `.des-foot` strip on every mount. A hardcoded help page is a second source
  of truth that decays with no test to catch it, and a help page that lies
  is worse than no help page. What remains hand-written — the global keys,
  the command list, the glossary — is written against the source it
  describes (`core/keys.js`, `core/cmd.js`, `docs/data-model.md`).
- **A screen must not assert something that is not true.** Two fixes landed
  under that one rule, and it is the rule, not the two instances, that is
  being recorded. `GET /api/paper` answers with `PaperLimits()` defaults and
  `enabled: false` when no trader is running, so the numbers alone never
  mean a limit is in force: ARB's threshold preset now reads `PAPER 50` only
  when the fetch says enabled and `DEFAULT 50` otherwise, with a tooltip
  saying it filters this view only; and PAPER's capacity meters drop the
  percentage entirely when the trader is off, labelling the block
  `DEFAULTS — NOT IN FORCE · TRADER OFF` rather than drawing "1% OF $1000"
  against a ceiling nothing enforces (with the trader off those totals are
  also summed across every stored run, so they do not belong to this run
  either). Generally: where the backend substitutes a default for a live
  value, the screen has to show which one it got.
- **A bulk write may only touch rows the operator can see, and it asks
  first.** PAIRS' `Shift+Y`/`Shift+N` decide a whole event pairing at once —
  a 30-team pennant race is one judgement, not thirty. It used to run over
  the full status-filtered set and ignore the search box, so searching for
  one candidate and pressing `Shift+Y` wrote all of its unseen siblings. The
  group is now exactly the visible rows, the message says how many of the
  event that reaches, and anything larger than one row is armed by the first
  keypress and spent by a second within 8 s. The pairs table is the input to
  what gets traded; it is the last place for a keystroke to reach further
  than it looks.
- **The monitor's view belongs to the operator and survives a reload.**
  Filter text, venue filter (`ALL`/`K`/`PM`) and sort column/direction
  persist under `arb.monitor.v1` in `localStorage`. Re-sorting moves the
  existing row nodes via `replaceChildren` instead of rebuilding them, so
  sorting by a live price cannot cost the selection, the flash-on-change
  state or a drag in progress; markets with no book yet always sort last,
  because an absent price is not a low one.
- **The header counts what the list actually holds.** It read
  `MONITOR — KALSHI` from M9 until now while the list had carried both
  venues since M12; it is built from live counts
  (`MONITOR — KALSHI 13 · POLYMARKET US 9`) and the row count reflects the
  filter. Related: the `#` column numbers *every* row. It previously wrote
  an empty string past row 9 because only nine rows have a quick-select key
  — conflating "this row has a shortcut" with "this row has a position".
  The number is positional information; the shortcut is a subset of it, and
  is marked by emphasis.

## M17 — the negative latency, and what was deliberately not fixed

- **Diagnosis: it was never a latency.** `latency_ms = raw.recv_ts_ns / 1e6 -
  adapter.last_delta_ts_ms` subtracts Kalshi's wall clock from the local one,
  so it measures `true_transit + (local clock − venue clock)`. Units and
  pairing were both verified clean (`ts_ms` is genuine epoch ms, floored from
  µs, which biases *positive*; `adapter.parse()` resets `last_delta_ts_ms` on
  entry so a frame can only pair with its own stamp). With the local clock
  ~20–27 ms behind and real push delay 5.5–12.5 ms, every sample read
  negative. M16's banner was correctly diagnosing it, not malfunctioning.
- **No trading impact, and that was checked rather than assumed.** Every
  decision path is monotonic: `Book.status(now_mono_ns=…)`, `books.apply(…,
  mono_ns=…)`, `ArbMonitor`'s staleness; `edge.py`, `fees.py` and `paper.py`
  hold no clock at all, and `replay.py` uses the recorded `recv_mono_ns`. The
  wall clock only reaches display `ts_ms` fields — and recorded
  `recv_ts_ns`, which means a skewed host does bias cross-run wall-clock
  analytics even though nothing within a run moves.
- **Deliberately NOT fixed: de-biasing the metric.** The obvious fix — track
  a rolling offset estimate and subtract it from every sample — was rejected.
  Two reasons. `clock_skew_ms = median − rtt/2` is circular as a *correction*
  (subtracting it forces the corrected median to equal `rtt/2` by
  construction) even though it is fine as a *warning*; and a number that
  quietly self-corrects hides a broken host clock instead of reporting it.
  The sample stays raw everywhere it is stored, reported or exported. What
  changed is that the raw number is now visible in four places instead of
  silently wrong in one.
- **`arb doctor` warns before the symmetric threshold, not at it.** The
  `ntp clock` check keeps `|offset| > 25 ms` to match the UI's
  `SKEW_WARN_MS`, but adds a second rule: a *lag* past 5.5 ms also warns,
  because that is the floor of Kalshi's measured push delay and therefore the
  point where one-way readings start going negative — the UI's other banner
  trigger. Without it, doctor reported `ok` at the real −19.7 ms reading
  while the terminal was already showing a negative median.
- **Doctor measures against a time server, not a venue.** The existing HTTP
  `Date` check has 1 s resolution and exists to protect request signing; it
  is structurally blind to tens of milliseconds. `src/arb/clock.py` is a
  ~150-line stdlib SNTP client (RFC 4330) — 4 samples, keep the lowest
  round trip, never fatal, `warn` when UDP 123 is blocked.
- **Sign convention is load-bearing and pinned by a test.** RFC 5905's
  `offset` is the correction to *apply* to the local clock (positive =
  behind); this repo reports local-minus-venue (negative = behind) so doctor,
  the UI and `arb_clock_skew_ms` all read the same sign for the same reality.
  `clock.py` negates explicitly and `test_local_behind_server_reads_negative`
  guards it.
- **Negative histogram buckets, and what they cost.**
  `arb_ws_one_way_latency_ms` spans below zero so the failure mode shows up
  as bucket counts: a delay cannot physically be negative, so any count at
  `le="0"` is proof of clock offset. The price is that `prometheus_client`
  suppresses the `_sum` series for a negative-floored histogram, so bucket
  counts are its only output — the skew gauge and the negative counter are
  the alerting signals, the quantiles are for shape.
- **Gauges go `NaN`, not stale.** `arb_ws_rtt_ms` and `arb_clock_skew_ms` are
  set to `NaN` whenever the quantity was not measured. A gauge frozen at
  −27 ms through a reconnect would look like a live measurement of the
  outage — the same fabricated-continuity bug being fixed in the sparkline,
  one layer down.

## M16 — skew-aware latency, NO-side fixture, infra

- **The latency panel now trusts RTT over one-way when clocks disagree.**
  One-way latency (exchange `ts_ms` → local receive) needs synchronized
  clocks; the WebSocket keepalive RTT does not. The server publishes both
  plus `median − rtt/2` as a clock-skew estimate; the UI flips to a
  `CLOCK SKEW · TRUST RTT/2` banner when the median goes negative or the
  estimate exceeds 25 ms — which is exactly how the −11 ms readings would
  have announced themselves.
- **The capture script is parameterized** (`CAPTURE_OUT`, `_SECONDS`,
  `_TOP_N`, `_MIN_DELTAS`, `_REQUIRE_SIDE`) so rarer message shapes can be
  captured into new fixture files without disturbing the pinned one.

## M15 — replay and paper trading

- **Replay is the live pipeline fed from Postgres.** `arb replay` streams
  a run's `raw_messages` in `ingest_seq` order through the *same* adapters
  and `BookManager`, using the recorded `recv_mono_ns` as the clock, so
  books, invalidations and edges are reconstructed deterministically. There
  is no second code path to drift from the live one.
- **Paper fills are honest about their one optimism.** A fill is assumed
  at the displayed liquidity the edge walk consumed, instantly, on both
  legs — everything else (fees, sizes, limits) is the real model. "P&L" is
  the net edge locked in at settlement (both legs pay exactly $1.00
  together), i.e. expected value under pair equivalence, not a mark.
- **Risk limits are the only thing standing between an edge and a fill:**
  minimum net per contract, maximum contracts per pair, maximum total cost.
  Deliberately simple and explicit; the live trader on Day 3 inherits them.
- **Same trader, live or replayed.** `arb ui --paper` runs it on the live
  monitor (trades persisted per run); `arb replay --paper` runs it over
  history (persisted only with `--persist`, tagged `replay:<run_id>`).
- **Still no orders.** Paper trading talks to no venue.

## M14 — fees, edge math, ARB screen

- **Fees in exact integer arithmetic, rounded the venue's way.** Kalshi:
  0.07 × multiplier × C × P × (1 − P), trade fee rounded up to $0.000001,
  then up to the direct-member tick; maker multipliers by `fee_type`
  (0 / 0.25 / 0.5). Polymarket US: Θ × C × p × (1 − p) with the market's
  `feeCoefficient` (maker −0.0125), banker's rounding to the cent. Both
  reproduce the venues' documented examples in tests. Per-series Kalshi
  parameters are fetched from the documented `GET /series` endpoint (event
  overrides win), Polymarket's from the market object.
- **Edge is measured depth-aware, in both directions.** Buy YES on A + NO
  on B costs `yes_ask_A + (10000 − yes_bid_B)`; the walk merges both ladders
  in cost order and stops at the first fill whose gross no longer covers
  its own fees. Sizes are what the books actually offer, not a hope.
- **The ARB screen quotes only confirmed pairs, and labels quiet books
  honestly.** Tracked pairs' legs are subscribed on both venues at startup;
  every book change re-quotes affected pairs and broadcasts a full ranked
  snapshot. A Kalshi book that merely hasn't ticked is QUIET, not invalid —
  the feed is live and every change arrives.
- **Measurement only.** Nothing here places, amends or cancels an order;
  Day 1's read-only rule still holds until a later prompt lifts it.

## M13 — pair matcher with human review

- **The matcher proposes; a human confirms.** Equivalence lives in the
  resolution rules, which no lexical score can judge (Kalshi's presidential
  market resolves on who is *inaugurated*; another venue's may resolve on
  who *wins*). Proposals carry both rules texts and the scoring features so
  the reviewer sees exactly what the matcher saw; only confirmed pairs feed
  the arb engine.
- **Two-stage, explainable, deterministic scoring.** Events pair on
  IDF-weighted title similarity plus *outcome overlap* (how many outcomes
  have a name twin on the other side) — the overlap is what separates a
  pennant race from a chess league sharing the word "champion", and the IDF
  weighting is what stops "American League" outweighing "Silver Slugger".
  Markets then pair one-to-one on outcome-name similarity (party suffixes,
  accents, "Jr." stripped; containment counts so "Dodgers" ⊂ "Los Angeles
  Dodgers"). Dates are a weak feature because Kalshi ``close_time`` trails
  the real event by up to a year. No LLM: reproducible and free.
- **Blocking makes it cheap.** An inverted index on informative title tokens
  keeps 6,000 × 3,500 events to ~0.5 s. Universes are fetched with
  documented params only and recorded before parsing like all REST.
- **Batch review is a first-class action.** Shift+Y / Shift+N decide a whole
  event pairing (a 30-team pennant is one judgement, not thirty). Decisions
  are never overwritten by re-proposal: upserts refresh score/detail only.

## M12 — Polymarket US REST poller (both venues live)

- **Poll the public gateway now; swap in the WebSocket later behind the
  same interface.** `PolymarketUSRestSource` is an `EventSource` like the
  Kalshi WS source, so `arb record`, `arb ui`, the book manager and the
  recorder are venue-blind. When credentials arrive only the source changes.
- **Rate budget from measurement, not the docs.** The book endpoint is a
  5-token bucket refilling ~1/2 s (venue-notes), so the default is
  0.45 req/s with a 10 s stop on any 429. A 429 must never cascade: the
  poller pauses, Kalshi is untouched (separate supervised task).
- **A polled venue gets a poll-cycle staleness budget.** `BookManager`
  supports per-venue staleness; Polymarket books are allowed three cycles
  before STALE, otherwise every book would flag stale between polls.
- **Snapshot diffs feed the tape.** `level_deltas` turns consecutive polled
  snapshots into the same DELTA events a streaming venue emits, so the tape
  and any downstream consumer see one event shape.
- **Targets are chosen by category, not volume.** Live listings carry no
  volume fields; non-sports markets (where Kalshi overlap lives) are
  selected first, then sports. Explicit `--poly-slugs` overrides.
- **The UI says POLLED, not LIVE.** Amber state, poll stats and "last book
  Ns ago" in the panel — a polled book must never masquerade as streaming.

## M11 — DES (market description) page

- **Bloomberg's `DES` is the drill-down.** Enter on a selected market (or
  `DES` / `<TICKER> DES` on the command line, or a double-click) replaces
  the workspace with a description page; Esc returns; arrows page through
  markets without leaving it. Familiar to anyone who has used a terminal,
  and it keeps the workspace layout untouched.
- **Metadata is seeded from discovery and refreshed on demand.** The
  `/events?with_nested_markets=true` pages already carry full Market and
  EventData objects, so DES works instantly with no extra calls; opening a
  page refreshes via the documented `GET /markets/{ticker}` (and
  `GET /events/{event_ticker}` when the event isn't cached) behind a 30 s
  TTL, and falls back to the seed if the venue call fails. Every refresh
  response goes through the recorder before parsing, like all REST.
- **Implied probability is shown next to cents.** With $0.0001 ticks the
  two share a number (50 ticks = 0.50¢ = 0.50%), which is exactly the kind
  of thing a reader shouldn't have to work out for a long-shot market.

## M10 — select-to-copy in the terminal UI

- **Copy fires on `mouseup`, not `selectionchange`.** The async clipboard API
  needs transient user activation; `selectionchange` fires mid-drag without
  it and would be rejected. `mouseup` (and `keyup` for Cmd/Ctrl+A) carries
  activation. Fallback is `document.execCommand("copy")`, which copies the
  live selection as-is; if both fail the toast says COPY BLOCKED rather than
  lying about success.
- **Tabular selections are rebuilt as TSV.** The ladder, monitor, tape and
  system rows are flex grids, so the DOM's own serialization loses the
  column boundaries. Multi-row selections copy as tab-separated cells, one
  row per line, so a book selection pastes into a spreadsheet intact.
  Selecting inside a single cell returns the exact highlighted substring —
  half a number stays half a number.
- **Rendering pauses for the duration of a drag.** A live re-render replaces
  the text nodes a selection is anchored in, which destroys it mid-gesture.
  `frame()` returns early while the pointer is down and `drainTape()` stops
  prepending rows; dirty flags accumulate and flush on release, so data is
  only ever delayed, never dropped. Stale-pause is impossible: a `mousemove`
  reporting no buttons held, or a window blur, ends the pause.

## M6 — Kalshi signing, WS parser, live capture

- **Kalshi WS books get unsequenced events.** The live capture proved `seq`
  is subscription-scoped, not market-scoped, so `Book`'s `seq == last + 1`
  rule would false-positive on any multi-market subscription. The upcoming
  Kalshi WS source tracks seq per `sid`; on a gap it requests fresh
  snapshots (`update_subscription` / `get_snapshot`, documented) and
  invalidates the affected books. Books rely on staleness + explicit resync
  for this venue, same as Polymarket US.
- **Delta mapping**: `side: "yes"` → YES-bid ladder at the quoted price;
  `side: "no"` → YES-ask ladder at the complement. Signed `delta_fp` parses
  through `qty_delta_from_contracts` (resting quantities stay unsigned).
- **`scripts/capture_kalshi_ws.py` is committed** so WS fixtures can be
  re-captured reproducibly (documented params only; discovery via
  `/events?with_nested_markets=true` because `/markets` is flooded with
  zero-volume multivariate shards). It never prints key material.
- **Signing lives in `venues/kalshi/auth.py`** (RSA-PSS SHA-256, salt =
  digest length, per docs) and is tested with throwaway generated keys —
  real keys never enter tests or logs.

## M4 — REST adapters and real fixtures

- **Quantities are integer units of 0.0001 contracts (`Qty`), not integer
  contracts.** CLAUDE.md assumed integer contracts "unless a venue's docs
  prove otherwise" — the live Kalshi book proved otherwise (counts like
  `"15.17"`; see venue-notes). Same fixed-point discipline as prices: exact
  parse or loud failure, no floats.
- **`market_id` is venue-qualified: `kalshi:<ticker>` /
  `polymarket_us:<slug>`.** Native identifiers stay untouched for API calls;
  the qualified form is globally unique across the book map and storage.
- **Polymarket JSON numbers are parsed as `Decimal`** (`parse_float=Decimal`)
  so `orderPriceMinTickSize` and `feeCoefficient` stay exact for the fee
  engine. Kalshi encodes everything as strings already.
- **Fixtures are live captures, committed.** Both venues' market-data REST
  answered unauthenticated (Kalshi's docs claim auth on the orderbook — the
  conflict is recorded in venue-notes; the client will sign anyway once
  credentials exist). WS parsers wait for captured WS payloads — they need
  credentials on both venues.
- **Parsers normalize at the edge**: Kalshi NO bids become YES asks by
  complement inside the adapter, so a `BookSnapshot` is venue-agnostic the
  moment it leaves venue code.

## M3 — config, storage, recorder, compose stack

- **Recorder never blocks the ingest path.** `enqueue` is non-blocking; on a
  full queue the message is dropped and counted
  (`arb_recorder_dropped_total`) — losing one message beats stalling a venue
  feed and losing the connection. The queue (default 100k) absorbs Postgres
  hiccups.
- **Failed sink writes retry in place with backoff.** Batches are never
  reordered or silently discarded; the writer runs under `supervise()`.
- **`raw_messages` stores payload bytes verbatim** (BYTEA), with BIGINT
  nanosecond timestamps (no floats, per project rules) and a unique
  `(run_id, ingest_seq)` so the archive is totally ordered per run and
  duplicate writes fail loudly. Index on `(venue, recv_ts_ns)` for replay
  queries.
- **Alembic reads the database URL from `AppConfig`** (environment / `.env`),
  never from `alembic.ini` — no connection strings in git.
- **Models stay dialect-portable; migrations are Postgres-only.** Fast tests
  run the real models against in-memory SQLite (aiosqlite, dev-only dep);
  the one concession is a `with_variant(Integer, "sqlite")` on the PK since
  SQLite only autoincrements INTEGER keys.
- **Compose images are pinned** (pgvector/pgvector:pg16, prometheus v2.53,
  grafana 11.1) and every host port binds to 127.0.0.1. The app service's
  command is a placeholder until `arb record` exists; inside the network its
  `DATABASE_URL` points at the `postgres` service.

## M2 — reliability layer (run identity, backoff, supervision, WS client)

- **The connector owns URL and auth; the WS client owns reliability.** Both
  venues sign a timestamp into the WebSocket handshake, so headers must be
  recomputed on every attempt — hence a `connector()` factory called per
  connect, with `headers_factory` in the default websockets-based connector.
  Venue code composes a connector + `on_connected` (resubscribe) callback;
  the shared client does reconnect, backoff+jitter, stall detection and
  envelope stamping.
- **Stall detection = no inbound frame within `stall_timeout_s`.**
  Protocol-level ping/pong (websockets' built-in) is the heartbeat;
  the recv timeout catches half-open connections where pings survive but
  data stops. The timeout must sit above the venue's heartbeat cadence
  (Polymarket US cadence is undocumented — measure, then configure).
- **Backoff resets only after a connection proves healthy**
  (`healthy_after_s` uptime), not on mere connect success — a flapping
  endpoint that accepts connections and immediately drops them still gets
  slowed down. Jitter multiplies the delay by a random factor in
  `[1 - jitter_frac, 1]`.
- **`supervise()` never swallows cancellation.** Everything else is logged,
  counted (`arb_supervisor_restarts_total`) and restarted; a clean return of
  a long-running task is treated as a failure and restarted too.
- **`ingest_seq` is allocated per run across all sources** (one
  `RunContext`), so the recorder's stream is totally ordered even when both
  venues are live; `run_id` is a sortable UTC timestamp + random suffix.

## M1 — shared core (types, Book, interfaces)

- **`Book` is a pure data structure.** No I/O, no clocks (callers pass
  monotonic timestamps from the message envelope), no metrics. Keeps it
  exhaustively testable; the manager layer owns metrics and resync policy.
- **Ladders are dicts keyed by price with sorted tuple views.** Prediction
  market books are small (< a few hundred levels, prices bounded 1..9999), so
  O(1) level updates plus O(n log n) reads are simpler than a sorted
  container and fast enough. Revisit only if profiling says so.
- **A locked book (best bid == best ask) counts as crossed.** On both venues a
  bid and ask at the same price would have matched in the engine, so a locked
  state means our book is wrong — invalidate and resync.
- **Structural invalidation is sticky; staleness is not.** NO_SNAPSHOT,
  SEQ_GAP, CROSSED and BAD_LEVEL require a fresh snapshot (`needs_resync`) and
  updates are dropped until one arrives. STALE is computed at query time and
  clears when a contiguous update lands, because a quiet-but-gapless book is
  merely untradeable, not wrong.
- **Sequence policy.** Contiguity (`seq == last_seq + 1`) is enforced when
  both sides have sequence numbers. A book with no sequence adopts the first
  one it sees (Kalshi snapshots may not carry `seq`); unsequenced updates are
  accepted without advancing it (for venues without sequencing). Docs don't
  specify Kalshi gap recovery, so resubscribe + fresh snapshot is our policy.
- **`RawMessage` is a frozen slotted dataclass, not a Pydantic model.** It's
  the hot-path envelope wrapping unparsed bytes for the recorder — there is
  nothing to validate yet. Pydantic v2 stays the rule for config and parsed
  venue message models.
- **Normalized events are YES-side only.** `BookSide` is bid/ask in YES terms;
  adapters fold venue NO-side data in by complement so shared code never
  branches on instrument side.

## M0 — repository scaffold

- **Prices as integer ticks (`Ticks = int`), 1 tick = $0.0001.** Avoids float
  error in book state, fees and storage; analytics may use floats. (Per CLAUDE.md.)
- **src layout, package name `arb`.** Keeps the importable package isolated from
  repo-root config and tests; `arb` is the CLI entry point.
- **Venue module names `kalshi` and `polymarket_us`.** Python-safe identifiers
  for the `src/arb/venues/<venue>/` and `tests/fixtures/<venue>/` dirs.
- **`asyncpg` as the async Postgres driver** for SQLAlchemy 2.0 async
  (`postgresql+asyncpg://`).
- **`.env` holds paths to key files, not secrets themselves.** Secrets live in
  `secrets/` (gitignored); env vars point at them.
- **Deferred to later prompts:** venue endpoints/auth (must be read from docs
  first), docker-compose infra, and all order placement code (Day 1 is read-only).

## A watch set is chosen per EVENT, not per row (2026-09-17)

`pairs.top` took the first N rows of `ORDER BY score DESC, id`. Every confirmed
pair scored exactly 1.000 — 64 of 64 on the live table — so that was `ORDER BY
id`, and ids cluster by event because a proposal run inserts one event's markets
together. Ten of ten slots went to consecutive strike bands of one Bitcoin
year-end ladder: ten rows, but one bet, all far out of the money and all still.

The selection now deals round-robin — the best row of every event, then the
second of every event, until N is full. Considered and rejected:

- **A hard per-event cap.** With three live events and N=10, a cap of one
  returns three pairs. Silently giving back fewer than asked is the same class
  of failure, wearing different clothes. Round-robin with unbounded rounds
  fills N *and* maximises spread.
- **Fixing the scorer so scores stop tying.** The right long-term answer, and it
  changes nothing until a re-propose runs. It also would not have untracked the
  ten Bitcoin strikes already holding the watch set.
- **Ordering by `decided_at`.** It is the only column with real variance
  (`created_at` has one distinct value across every confirmed row), but top-10
  by `decided_at DESC` is just a different single-event clump. It refines
  ranking *within* a bucket; it does not create buckets.

## Expiry is checked twice, in the order the checks cost (2026-09-17)

Three of thirteen watched pairs were esports maps that had resolved days
earlier. They held poll budget, showed no movement, and nothing said why.

1. **Selection** filters on the `close_time` recorded at proposal. Free, no
   I/O, and it runs *before* the operator arms — so the armed sentence can say
   how many pairs it skipped.
2. **Load** asks the venues, using the predicates the adapters already use:
   Kalshi tested positively against `"active"` so an unlisted future value fails
   closed, Polymarket on `closed` and never on `active`, which stays true after
   settlement. Both objects are already fetched to resolve fees, so this costs
   no extra request.

The first misses a market that closed early; the second catches it but only
after it has taken a slot. So the control plane remembers what the venues
reported and feeds it back into the next selection. Without that, every bulk
set re-picked the same dead pairs: asking for 12 produced 6 quoting, forever.

The load-time check SKIPS rather than clearing the `tracked` flag. A read path
must not mutate operator state; untracking is a control action with an audit
row.

## A paper fill's cost rounds up, its edge rounds down (2026-09-17)

The trader sat one tick under its notional cap and kept trading. It sized each
fill at a single Qty unit — 1e-4 contracts, true cost 0.98 ticks — and `int()`
truncated that to zero, so `notional_ticks` never advanced and the cap was
never reached. 163 free trades in one minute, spend meter frozen.

Cost and fees round up, net rounds down: every rounding goes against the book
and none toward it. A simulator is allowed to be wrong in exactly one
direction.

No notional reset control was added. `notional_ticks` is capital *deployed*,
not spent, and nothing models settlement — zeroing it would assert capital is
free while the positions are still open. The honest ways to free room are
raising the cap through `paper.limits` (audited) or restarting the run; every
trade already survives in `paper_trades` keyed by `run_id`.

## The DEPTH panel is a probability scope over an exact ladder (2026-09-18)

The centre of MONITOR was a 12-level strip floating in an empty panel, with
10%-opacity bars and a faint background flash as its only motion. It is now,
top to bottom: a state strip, a hero (best bid · mid as YES-implied
probability · best ask), one canvas (the whole 0-100¢ rail with every level as
a √size barcode and a bracket for the zoomed window; cumulative-depth terrain;
a size-at-price strip), and the full book as a mirrored DOM ladder. Chosen by a
judge panel over three alternatives (a Bookmap-style heatmap, a pro DOM ladder,
an instrument cluster); the full spec is `.context/depth-panel-spec.md`.

- **Motion annotates; it never interpolates.** Bars, terrain and the touch
  snap to the true state on the frame it arrives. Size added glows inside its
  bar and fades; size removed leaves a neutral dashed ghost outside the bar;
  a new touch price lights a column where it now is. A tween between two sizes
  would draw sizes that never rested, and a price sliding between levels would
  draw quotes that never existed. The only continuous motion is the camera,
  and every mark and label is re-projected through the same interpolated scale
  in every frame, so each frame is a true chart under its own axis.
- **"REMOVED (TRADE OR CANCEL)", never "trade".** The feed does not
  distinguish them.
- **Polymarket US reads POLLED, never LIVE.** A shutter sweeps once when a
  snapshot lands; past one poll cycle the chart is striped HELD, NOT OBSERVED.
  Texture, not dimming — dimming reads as smaller size.
- **No mid for a book that cannot stand behind one**: one-sided, crossed or
  structurally invalid books show — or INVALID, in the hero, on the rail and in
  the plot alike. Every level of an untrusted book stays printed, greyed.
- **The bar scale fits the 85th percentile of the top 20 levels a side, and
  walls clamp.** The spec's "max capped at 2.5x the second" failed on the
  first realistic book: one 71,694-contract wall set the scale to 60K and left
  40 of 43 bars as 1px stubs. A clamped bar gets a white-hot cap and its exact
  size is printed beside it, so a wall is louder, not hidden. The Y and size
  scales grow at once and shrink only after holding (4s / 8s), so a maximum
  that changes every tick never rescales every tick.
- **The loop idles to zero** and never runs under reduced motion, where every
  mark is drawn still for a second instead. Fade-only frames draw at 30fps
  (the camera and shutter get every frame): on a 9-updates/s book this took
  main-thread time from 18.6% to 12.4%, against 11.5% with no animation.
- **Everything on the canvas is also text**: in the ladder and hero, and in an
  aria description that announces state changes at once and throttles only
  window churn.

## A /pairs decision stops a pair trading the moment it commits (2026-09-18)

Rejecting a watched pair on /pairs cleared `tracked` in the database and did
nothing else: the running ArbMonitor kept the pair, so it stayed on /arb,
stayed subscribed and polled, and stayed in front of the paper trader until
some unrelated watch-set action happened to reload.

The first fix — re-run the watch-set reload after the decision — was reviewed
adversarially and was not enough: the reload makes one venue call per watched
market (10s timeout each), so the rejected pair traded for that whole window;
and a reload already in flight had read the database before the rejection and
installed the pair straight back.

What holds now:

1. **Synchronous prune.** Right after the decision commits, before the
   decide route awaits anything, the pair is dropped from the running monitor
   and /arb is sent the new quotes. The flush loop rebinds the monitor every
   tick, so the trader never sees it again.
2. **Veto.** If a reload is in flight when the decision lands, the pair is
   vetoed; every install drops vetoed ids, and a veto clears once a reload
   that started after it has installed.
3. **Reconcile in the background.** The usual reload then runs so
   subscriptions and poll targets follow, and an audit row (`pairs.decide`)
   says what stopped. If it fails, the stopped legs are dropped from the pair
   universe directly. The decide route does not wait for it, so a slow venue
   cannot stall /pairs or reorder its responses.

Also: every change to the monitor publishes an arb frame at once, and every
new connection gets the current quotes even when nothing is watched — /arb
only heard about quotes when a watched book moved, so an emptied watch set
left stale rows on screen for good.

## A paper fill consumes the liquidity it took (2026-09-18)

A paper fill never reaches the venue, so the size it took keeps sitting in the
book, and the trader re-read the raw book on every change. Live: a 16-contract
Polymarket bid was filled against six times in a minute — 100 contracts out of
16, stopped only by the per-pair cap — and the ledger's expected net was
inflated by the same factor.

`arb.taken` keeps, per (market, side, price), what paper fills have consumed;
the trader is quoted off ladders net of it, while /arb still shows the market
as it is. The rules are wrong only against the book: available = displayed −
taken; taken shrinks when the venue shows less (so a later increase is
genuinely new); a price that leaves the ladder is forgotten, but an empty side
is a resync, not a pulled market. It assumes a maker would NOT have
replenished a level we really hit, which undercounts fills; the alternative
overcounts them. `PaperTrader.trade_pair` is the single entry point for live
and replay — quote net, take, record — and it also declines any pair with a
missing or structurally invalid book (`stale` alone is not structural).

## A Kalshi seq that goes backwards is a new subscription, not a gap (2026-09-18)

Kalshi restarts at `sid=1, seq=1` on every connection (verified in the
recorder; docs/venue-notes.md). The adapter outlives the connection and
compared that seq=1 with the old connection's last seq, called it a gap, and
asked for a resync — which is a reconnect, which restarts at seq=1. From the
first reconnect of a run (a 30s stall on quiet markets is enough) the socket
was torn down within a second of every connect, forever, and every Kalshi book
sat at `seq_gap`. Earlier in the week this was misread as Kalshi rate-limiting
rapid watch-set changes. Only `seq > last + 1` is a gap now.

## SYSTEM gives a verdict, not counters (2026-09-18)

The page was seven cards of raw counters with a paragraph under each, and
never said whether anything was wrong: the reconnect loop above ran under a
green "KALSHI LIVE" for days. It is now a verdict line, a pipeline picture
(feeds → books → engine → paper; recorder → database) and one plain-English
check per part, problems first; the selected check explains what it is, the
numbers behind it and the fix, and ⏎ opens the page where the fix lives. Every
judgement is a pure function in `pages/system-model.js`, tested in node.

- A level is a judgement about NOW: a sequence gap that recovered stays OK
  ("6 gaps recovered"), only a book untrusted right now warns. Counters that
  only ever grow are read as a two-minute delta.
- OFF (paper suspended, nothing watched) is a choice and never counts against
  the verdict.
- The Kalshi check reads the connection count, because "last frame 2s ago"
  looks healthy in a reconnect loop — every reconnect delivers snapshots.
- Sentence case for the explanation and the fix: they are read, not scanned.

## /control is sections that read alike, with the answer where you asked (2026-09-18)

The page was five cards of uneven height with an uppercase paragraph under every
control, START and STOP both always on screen, raw ticks in the limit fields,
the audit trail below the fold, confirmations and receipts in a band at the top
of the page, and DOCTOR's output nowhere at all. The five rules it was built on
(descriptors from the server, no optimistic state, the server owns the confirm
copy, build once and update in place, a no-op is not a success) are unchanged.
What changed, and why:

- **One anatomy per section** — light · TITLE · STATE, one sentence, numbers,
  controls — ordered by use: PAPER and WATCH SET, the two universes, RECORDER
  as a strip. JOBS, JOB OUTPUT and the AUDIT TRAIL share the right rail: work,
  and its record, always on screen.
- **State is loud; the button is a verb.** One button per switch, in the
  header beside the state it changes. START+STOP always had one dead button; a
  slider would imply the instant local flip that "no optimistic state" forbids.
  A 700 ms lock after a flip stops a double-click flipping it back.
- **The operator's units.** ¢ per contract, contracts, dollars — what /arb and
  /paper print. Conversion lives in `pages/control-model.js` and is exact or
  refused: 0.505¢ is not a whole number of ticks, so it is an error, never a
  silent round. APPLY enables only for a valid change, and the line above it
  lists the change ("min edge 0.50¢ → 0.75¢"): no dead presses.
- **The answer appears where the question was asked.** A section's confirm box
  and its receipt open inside that section, under its buttons. One pending
  confirmation, owned by its action: finishing some other action used to clear
  it wholesale.
- **A confirm step is a tax, spent where a slip is expensive.** Switches and
  limits apply at once — one value, and the inverse is one press away. Anything
  that REPLACES a set is priced first through the new read-only
  `{"preview": true}` on the control route: the server's own sentence, rows
  changed and resulting poll cycle included, with nothing done and nothing
  audited. `pairs.top` was deliberately NOT made a confirm-action on the server:
  eight tests pin its one-call contract, and a preview gives the same sentence
  without changing what `execute` means. G3 jobs still arm on the server.
- **The universe boxes edit the base list only.** They showed base + watched-
  pair legs and wrote the union back as the base, so one press of SUBSCRIBE made
  every watched pair's leg a permanent base market.
- **The bind warning is a header chip, not a red band.** Non-loopback is only
  possible with `ARB_ALLOW_REMOTE_BIND=1`, i.e. on purpose; a full-width red bar
  on every visit for a deliberate state teaches the operator to ignore red. It
  stays on screen, in amber, with the explanation on hover.
- **Settled pairs can be untracked in one action**, and the engine's memory of
  them accumulates (it was overwritten by every reload, so the watch set
  alternated between right and half-dead on every press).
- **Explanations are one sentence in sentence case.** Labels are scanned;
  sentences are read.

