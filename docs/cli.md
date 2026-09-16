# CLI reference

Entry point: [`src/arb/cli.py`](../src/arb/cli.py) (`arb = "arb.cli:main"`
in `pyproject.toml`). Runs under `uvloop`. Three commands — `ui`, `doctor`,
`replay` — and each works identically run locally (`uv run arb ...`) or
inside the app container (`docker compose exec app arb ...`); see
[`ops.md`](ops.md) for the Compose stack.

Running `arb` with no subcommand prints help and exits 0.

## Why only these three

Operating this system is the UI's job. Recording, paper trading, the market
universe, the tracked pairs, pair proposal/backfill/review and replay are
buttons on [`/control`](ui.md) and `/pairs`, and every one of them goes
through a single audited executor
([`src/arb/ui/control.py`](../src/arb/ui/control.py)) that owns the
read-only refusal, the arm-then-confirm and the audit row. A second way in
that skipped all of that would be worse than no second way in.

What survived is what a button cannot be:

| Command | Why it stays on the command line |
| --- | --- |
| `arb ui` | it *is* the process the buttons live in. Its flags are the **starting** values the UI changes from, and `docker-compose.yml` launches the container with `arb ui --top 20 --host 0.0.0.0` — so those flags are a live dependency, not a convenience |
| `arb doctor` | you run it when the UI will **not** start. A button inside a dead server diagnoses nothing. (`/control` also exposes it as the `jobs.doctor` job, for when the server *is* up.) |
| `arb replay` | it is a **machine interface** as well as a human one: `/control`'s REPLAY spawns this exact command as a subprocess. Replay's per-row loop has no `await`, so in-process it would hold the event loop for minutes, trip the 10 s WS ping timeout and drop the Kalshi socket |

`arb record` and the whole `arb pairs` subtree are gone, module and all.
[Where the deleted commands went](#where-the-deleted-commands-went) maps each
one to the control that replaced it.

## `arb ui`

```sh
uv run arb ui [--tickers ... | --top N] [--poly-top N | --poly-slugs ...] \
              [--pairs-top N] [--host H] [--port 8080] [--no-record] \
              [--read-only] \
              [--paper] [--min-net-ticks 50] [--max-cts-per-pair 100] [--max-notional 1000]
```

Serves the terminal UI at `http://<host>:<port>` (default
`127.0.0.1:8080`), and records raw venue messages into Postgres while it
runs unless `--no-record` is given. It also serves `GET /metrics` from the
same app on the same port — there is no separate metrics server any more.
See [`ui.md`](ui.md) for the screens and the wire protocol.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--tickers` | auto-discover | comma-separated Kalshi tickers |
| `--top` | 8 | liquid Kalshi markets to auto-discover |
| `--host` | `UI_HOST` env / `127.0.0.1` | bind host — non-loopback is refused unless `ARB_ALLOW_REMOTE_BIND=1`, see [below](#the-bind-guard) |
| `--port` | `UI_PORT` env / 8080 | bind port |
| `--no-record` | off | *start* with recording off |
| `--read-only` | off | serve every view, refuse every control (also `UI_READ_ONLY=1`) |
| `--poly-top` | 8 | Polymarket US markets to poll (0 disables) |
| `--poly-slugs` | — | explicit slugs, overrides discovery |
| `--pairs-top` | 10 | confirmed pairs (by score) to track on `/arb` (0 disables) |
| `--paper` | off | *start* taking paper edges (see [`engine.md`](engine.md#paper-trading)) |
| `--min-net-ticks` | 50 | paper: minimum net edge per contract, in ticks, to take a trade |
| `--max-cts-per-pair` | 100 | paper: maximum contracts held per pair |
| `--max-notional` | 1000 | paper: maximum total cost across all pairs, in dollars |

### The flags are starting values, not settings

Everything below `--host` in that table is the state the process *boots*
into; `/control` changes it from there, live, through the control plane.
`--no-record` starts with recording off and the RECORDER card turns it back
on. `--paper` decides only whether the paper trader starts taking edges —
**one** `PaperTrader` is built either way and starts suspended without the
flag, because rebuilding one on a toggle would reopen the whole
`max_notional` budget and throw the ledger away, so RESUME on `/control`
works on a run started without `--paper`. `--tickers`/`--top`,
`--poly-*` and `--pairs-top` are all replaceable from the UNIVERSE card.

Three things are fixed for the life of the process, because no control can
change what the process already bound or refused: `--host`, `--port` and
`--read-only`.

### `--read-only` and the bind guard

`--read-only` (or `UI_READ_ONLY=1`) makes every mutating control-plane
action return 403 and write an audit row with `result='refused'` — so a
tunnelled port can be shown to someone without handing them the controls.
The flag only ever turns read-only *on*: passing it when `UI_READ_ONLY` is
already set changes nothing, and omitting it does not clear the environment
variable. Note what it does **not** cover: pair decisions on `/pairs` post
to `/api/pairs/...`, which is not a control-plane action, so they are still
writable. See [`ops.md`](ops.md#read-only-mode).

<a id="the-bind-guard"></a>
Binding a non-loopback host (`0.0.0.0`, a LAN address, `::`) is **refused
at startup** unless `ARB_ALLOW_REMOTE_BIND=1` is set — the UI has no
authentication and its controls drive a process that will place real
orders. The refusal happens before anything binds or connects, so it is a
clean exit, not a half-started server. Compose needs `0.0.0.0` inside its
network; see [`ops.md`](ops.md#binding-beyond-loopback) for what to set.

### Every screen is a URL

One process, one port, one WebSocket — but eight routes. Each one deep-links
and survives a reload, and the browser's back/forward buttons work:

| Path | Screen | Reached by |
| --- | --- | --- |
| `/` | MONITOR — market list, depth ladder, tape, latency | nav, the chord + `1`, `MON` |
| `/arb` | cross-venue edge over tracked pairs | nav, the chord + `2`, `ARB` |
| `/pairs` | pair review queue | nav, the chord + `3`, `PAIRS` |
| `/paper` | simulated ledger; with paper suspended it shows stored history and says so | nav, the chord + `4`, `PAPER` |
| `/system` | run, venue, engine, recorder, database and clock diagnostics | nav, the chord + `5`, `SYS` |
| `/control` | every runtime control, the job list and the audit trail | nav, the chord + `6` |
| `/help` | keys, commands, how to read each screen, glossary | nav, the chord + `7`, `HELP` |
| `/market/<market_id>` | DES — one market's rules, metadata and live book | `⏎` on a selection, or `DES` — not in the nav |

`/control` has no `ARB>` word: typing `CONTROL` falls through to the ticker
search and reports `NO MATCH`. The nav tab and the chord are the way in.

Routing is client side, over the History API, on purpose: the session holds
exactly **one** WebSocket, and serving a separate document per page would tear
it down on every navigation, dropping the tape, the latency window and every
book with it. The server therefore hands the same shell document to each of
these paths. It enumerates them (`SPA_ROUTES` in
[`server.py`](../src/arb/ui/server.py)) rather than using a catch-all, so
`/api/*`, `/ws`, `/metrics` and `/static/*` keep their own handlers and an
unknown path is still a 404 instead of a shell that hides a broken link.
`market_id` is deliberately not validated server-side — a link to a market
that has since rolled off the discovery list opens the page, and the browser
reports the miss.

### Keys and commands

A bare printable character **always** goes to the `ARB>` line. A page's
single-letter keys fire only while focus is inside its row list, which you enter
deliberately with `↑`/`↓` — clicking a row selects it but does not move focus.
That rule exists because typing the word `RUN` on `/pairs` used to reload the
list and then write two pair decisions to Postgres; see
[`ui.md`](ui.md#the-keyboard-model) for the full model and
[`decisions.md`](decisions.md) for why.

The page chord is **`CTRL` on macOS and `ALT` everywhere else** — Option is the
insert-special-character modifier on a Mac, so `⌥1` types `¡`. `ALT` stays live
as an alias on every platform; only the label changes.

| Key | Effect |
| --- | --- |
| chord + `1`…`7` | jump to that nav page |
| chord + `[` / `]` | previous / next page, wrapping |
| `↑` `↓` | from `ARB>`, focus the page's row list; inside it, move the selection |
| `1`–`9` | quick-select one of the first nine monitor rows (with the list focused) |
| `⏎` | run the typed command; with an empty command line, the page's action on the current selection (DES on the monitor, the Kalshi leg on `/arb`) |
| `ESC` | clear a filter box, then leave the list for `ARB>`, then clear a half-typed command, then return to the monitor |
| `⌫` | edit the command line |

The chord's digit range is sized to the nav that exists, so adding `/control`
widened it from six to seven without a second place to update.

History back/forward is **not** bound here — `⌘[` / `⌘]` on macOS and
`ALT+←` / `ALT+→` elsewhere are already the browser's own.

Commands are typed at `ARB>` and run with `⏎`; a trailing `<GO>` or `GO` is
stripped first (Bloomberg muscle memory, kept deliberately).

| Command | Effect |
| --- | --- |
| `MON` / `MONITOR` | go to `/` |
| `ARB`, `PAIRS`, `PAPER` | go to that screen |
| `SYS` / `SYSTEM` | go to `/system` |
| `HELP` / `?` | go to `/help` |
| `BACK` | browser history back, one step |
| `DES` | description page for the current selection (`NO MARKET SELECTED` if there is none) |
| `<TICKER>` | select a market: exact ticker first, then prefix, then substring |
| `<TICKER> DES` | select it and open its description page |

`/help` is the in-app version of this table, and most of it is derived at
render time from the router's page list and each page's own footer rather
than hardcoded, so it cannot drift from the bindings.

Details: [`src/arb/ui/server.py`](../src/arb/ui/server.py) for the backend,
[`src/arb/ui/control.py`](../src/arb/ui/control.py) for the controls, and
[`src/arb/ui/static/js/`](../src/arb/ui/static/js/) for the router, pages and
command line.

## `arb doctor`

```sh
uv run arb doctor
```

Checks, in order: `.env` presence, Kalshi/Polymarket US key provisioning
(paths only — file contents are never read into a log or printed), venue
reachability plus gross clock skew (via each venue's HTTP `Date` header vs.
local time — both venues sign timestamps into requests, so skew matters), the
local clock at millisecond resolution (`ntp clock`, below), database
connectivity and Alembic migration state, and free disk space. Each check
reports `ok` / `warn` / `fail`; the process exits non-zero only if any check
`fail`s. Missing keys are a `warn`, not a `fail` — Polymarket US market data
and Kalshi's public REST paths need no credentials, though Kalshi's
WebSocket always does (see [`venues/kalshi.md`](venues/kalshi.md)).

The `migrations` check reports the revision the database is **at**, and calls
any revision `ok`; it does not compare against `head`. Read the number —
behind `head` is how a control action gets a 503 it cannot explain
([`ops.md`](ops.md#the-migration-is-not-optional-any-more)).

`/control`'s RUN DOCTOR button (`jobs.doctor`) runs these same checks
in-process and keeps the output readable after the fact. It is the only
control graded G0: it changes nothing, so it neither confirms nor is refused
in read-only mode. The command remains the version that works when there is
no server to press a button in.

### The `ntp clock` check

The per-venue `Date`-header check has **1 second** resolution, so it can only
catch skew gross enough to break request signing — it is blind to the tens of
milliseconds that actually matter. `ntp clock` closes that gap: a real SNTP
exchange ([`src/arb/clock.py`](../src/arb/clock.py), RFC 4330, stdlib sockets,
no new dependency) against `NTP_SERVER` (default `pool.ntp.org`), four samples
keeping the lowest-round-trip one.

It reports the offset in this repo's convention — **local minus server, so
negative means the local clock is running behind** — matching the UI's
`clock_skew_ms` so the same reality reads the same sign in both places. Note
this is the *negation* of RFC 5905's `offset`, which is the correction to
apply to the local clock.

It `warn`s when either:

- `|offset| > 25 ms`, the same `SKEW_WARN_MS` the UI uses to flip its
  `CLOCK SKEW · TRUST RTT/2` banner, or
- the local clock lags by more than `5.5 ms` — the floor of Kalshi's measured
  WS push delay ([`venue-notes.md`](venue-notes.md)). Past that, one-way
  latency readings go negative, which is the UI's *other* banner trigger.
  Without this second rule doctor would report `ok` for a clock that is
  already making every latency number in the terminal wrong.

It never `fail`s (a skewed clock does not break read-only market data) and it
degrades to `warn`, never an exception, when outbound UDP 123 is blocked —
common in containers and on locked-down networks. The warn text carries the
platform-appropriate remediation command. See
[`ops.md`](ops.md#host-clock-discipline) for the runbook.

Details: [`src/arb/doctor.py`](../src/arb/doctor.py).

## `arb replay`

```sh
uv run arb replay [RUN_ID] [--pairs-top N] [--paper ...] [--persist]
```

Replays a previously recorded run's raw messages, in order, through the
identical pipeline live code uses — see
[`engine.md`](engine.md#replay-the-live-pipeline-fed-from-postgres).
`RUN_ID` defaults to the most recent run in `raw_messages`. Prints a
plain-text report: message/stream counts, parse errors, final book
validity, and (if `--pairs-top > 0`) a per-pair table of quotes seen, how
many had a real edge, and the best net-per-contract ever observed.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pairs-top` | 10 | confirmed pairs to quote during replay (0 = book reconstruction only) |
| `--paper` | off | run a `PaperTrader` over the replayed history |
| `--min-net-ticks`, `--max-cts-per-pair`, `--max-notional` | same defaults as `arb ui` | paper limits |
| `--persist` | off | store any paper trades taken, tagged `replay:<run_id>` |

Without `--persist`, `--paper` still computes and reports totals in the
summary — it just doesn't write to `paper_trades`.

### This argv is a contract

`/control`'s REPLAY button does not import this code: `ControlPlane._apply_replay`
builds

```
[sys.executable, "-m", "arb.cli", "replay"] [RUN_ID] --pairs-top N [--paper] [--persist]
```

and runs it with `asyncio.create_subprocess_exec`, streaming stdout back into
the job's output line by line. A subprocess rather than a coroutine because
`replay.py`'s per-row loop contains no `await` and yields only every 2000
rows: in-process it holds the event loop for seconds to minutes, past
`ws.py`'s `ping_timeout_s = 10.0`, and the Kalshi socket dies while a
"harmless read-only replay" runs.

So **renaming a flag, changing a default or making the positional required
breaks the UI silently** — the button would start a subprocess that exits
non-zero with an argparse error. [`tests/test_cli.py`](../tests/test_cli.py)
pins the exact argv the control plane builds, and asserts the parser accepts
it, so that failure shows up as a red test instead of a red job.

Two asymmetries worth knowing:

- The REPLAY button sends only the run id (blank means latest). A `--paper`
  or `--persist` replay is either this command, or a `POST /api/control/jobs.replay`
  with those params — and `persist: true` is the one replay parameter that
  requires a confirmation, because it writes rows.
- Cancelling the job sends `SIGTERM`, then `SIGKILL` after 5 s. The command
  handles the closed stdout pipe quietly (exit 141) rather than dumping a
  `BrokenPipeError` traceback over a fine result.

Details: [`src/arb/replay.py`](../src/arb/replay.py),
[`src/arb/ui/control.py`](../src/arb/ui/control.py).

## Where the deleted commands went

Every capability below still exists; the entry point moved. Names in `CAPS`
are the on-screen labels on [`/control`](ui.md) or `/pairs`, and the dotted
names are the control-plane actions behind them (`POST /api/control/<action>`,
one audit row each).

| Removed | Now |
| --- | --- |
| `arb record` | `arb ui` records while it runs; RECORDER → `START` / `STOP` (`recording.start`, `recording.stop`) toggles it live. The audit row is the only record that a gap in `raw_messages` was deliberate — replay reads straight across it |
| `arb record --tickers/--top` | the same flags on `arb ui` for the starting set; UNIVERSE → `KALSHI SUBSCRIPTION` → `SUBSCRIBE` (`universe.kalshi`) replaces it live, at the cost of a reconnect (a gap, then every book resnapshots) |
| `arb record --poly-top/--poly-slugs` | the same flags on `arb ui`; UNIVERSE → `POLYMARKET US POLL TARGETS` → `SET TARGETS` (`universe.polymarket`), which is live with no reconnect and retunes the staleness budget to the new poll cycle |
| `arb record --duration` | no replacement: stop it from the UI, or stop the process |
| `arb record`'s `:9000` metrics server | `arb ui` serves `GET /metrics` on its own port. Nothing binds `:9000` any more and Prometheus's `arb` job was removed — see [`ops.md`](ops.md#monitoring-prometheus) |
| `arb pairs propose` | JOBS → `PROPOSE PAIRS` (`jobs.propose`). Same matcher, same upsert-never-overwriting-a-human-decision rule; arm-then-confirm, then it runs without blocking ingest (the scorer goes to a worker thread), with progress on the page and the summary kept afterwards |
| `arb pairs backfill` | JOBS → `BACKFILL SLUGS` (`jobs.backfill`), also arm-then-confirm |
| `arb pairs list [--status]` | the `/pairs` page: the review queue, with status chips, a text filter and the same best-score-first order |
| `arb pairs show ID` | the `/pairs` detail pane: both legs' event title, outcome, ticker/slug, close time and rules text, plus the matcher's features. It does not build the `polymarket.us/event/<slug>` link that `show` printed; the event title is what that site's search actually matches, and the slug is in the filter's search text |
| `arb pairs confirm ID` / `arb pairs reject ID` | `/pairs`: `Y` / `N` / `U` with the list focused, or the `CONFIRM` / `REJECT` / `UNDECIDE` buttons in the detail pane; `SHIFT+Y` / `SHIFT+N` decide every *visible* row of the selected row's event, behind an arm-then-confirm that states the exact count. Undo back to `proposed` exists here and never did on the CLI |

Two controls have no CLI ancestor at all: PAPER (`paper.suspend`,
`paper.resume`, `paper.limits` — limits change live, on the one trader) and
TRACKED PAIRS → `RELOAD PAIRS` (`pairs.top`, which re-resolves both venues'
fee parameters). Both used to require a restart.

### Driving the control plane without a browser

The buttons are a client of a plain HTTP API, so `curl` is a first-class
operator too — and it gets the same refusals, the same confirmations and the
same audit rows, because they live in the executor and not in the page.

```sh
curl -s 127.0.0.1:8080/api/control | jq -r \
  '.actions[] | [.grade, .action, .summary] | @tsv'          # what exists, and its grade
curl -s -XPOST 127.0.0.1:8080/api/control/recording.stop      # G2: takes effect immediately
curl -s -XPOST 127.0.0.1:8080/api/control/jobs.propose \
     -H 'content-type: application/json' -d '{"params":{"min_score":0.8}}'
# -> 428 {"confirm_required":true,"confirm_token":"...","effect":"fetch both venues' ..."}
curl -s -XPOST 127.0.0.1:8080/api/control/jobs.propose \
     -H 'content-type: application/json' \
     -d '{"params":{"min_score":0.8},"confirm":"<token>"}'    # same params, or it is refused
curl -s '127.0.0.1:8080/api/control/jobs/<job_id>'            # output, live or after the fact
curl -s '127.0.0.1:8080/api/control/log?limit=20'             # the audit trail
```

The token is single-use, expires in 90 s and is bound to a hash of
`(action, params)`: arming at `min_score 0.75` and confirming at `0.1` is
refused with `the parameters changed since this action was armed`. A request
with no `Origin` header is allowed on purpose — that is `curl` from a process
already on this machine, which could talk to the socket directly anyway;
a cross-site *browser* POST is rejected with 403. See
[`ops.md`](ops.md#the-control-plane).

## Database migrations (not an `arb` subcommand)

```sh
uv run alembic upgrade head
```

Reads the database URL from `AppConfig` (environment / `.env`), never from
`alembic.ini` directly — so no connection string, credentials included,
ever lives in a committed file. **Not optional any more**: without migration
`0004` there is no `control_actions` table, and every control action that
must be audited fails closed with a 503. See
[`ops.md`](ops.md#migrations) and
[`data-model.md`](data-model.md#storage-schema).

## Checks (not `arb` subcommands, run directly)

```sh
uv run pytest
uv run ruff check .
uv run pyright
```

Run these after any change; see [`testing.md`](testing.md).
