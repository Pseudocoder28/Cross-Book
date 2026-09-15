# CLI reference

Entry point: [`src/arb/cli.py`](../src/arb/cli.py) (`arb = "arb.cli:main"`
in `pyproject.toml`). Runs under `uvloop`. Every command works identically
run locally (`uv run arb ...`) or inside the app container
(`docker compose exec app arb ...`) — see [`ops.md`](ops.md) for the
Compose stack.

Running `arb` with no subcommand prints help and exits 0.

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
`fail`s (missing keys are a `warn`, not a `fail` — the system can still
record public REST/WS market data is fine without them for Kalshi's REST
paths, though Kalshi's WebSocket always needs credentials — see
[`venues/kalshi.md`](venues/kalshi.md)).

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

## `arb record`

```sh
uv run arb record [--tickers T1,T2 | --top N] [--poly-top N | --poly-slugs S1,S2] [--duration S]
```

Headless recorder: streams raw Kalshi WebSocket frames and Polymarket US
REST-polled book data into Postgres, with no book parsing at all — the
minimal read path for pure data capture. Starts a dedicated Prometheus
metrics HTTP server on `metrics_host:metrics_port` (default
`127.0.0.1:9000`; Compose overrides the host to `0.0.0.0` inside the
network so Prometheus can scrape it, while the host port mapping stays
`127.0.0.1`-only).

| Flag | Default | Meaning |
| --- | --- | --- |
| `--tickers` | auto-discover | comma-separated Kalshi tickers; omit to discover the most liquid |
| `--top` | 10 | number of liquid Kalshi markets to auto-discover |
| `--duration` | run until Ctrl-C | seconds to run |
| `--poly-top` | 8 | Polymarket US markets to poll over REST (0 disables) |
| `--poly-slugs` | — | explicit comma-separated slugs, overrides discovery |

A Polymarket US discovery or polling failure never stops Kalshi recording —
each venue's ingest loop is an independently supervised task (see
[`architecture.md`](architecture.md)). Ctrl-C drains the recorder queue
(up to 10s) before exiting, so nothing already enqueued is lost.

Details: [`src/arb/record.py`](../src/arb/record.py).

## `arb ui`

```sh
uv run arb ui [--tickers ... | --top N] [--poly-top N | --poly-slugs ...] \
              [--pairs-top N] [--port 8080] [--no-record] \
              [--paper] [--min-net-ticks 50] [--max-cts-per-pair 100] [--max-notional 1000]
```

Serves the terminal UI at `http://<host>:<port>` (default
`127.0.0.1:8080`). Records the same way `arb record` does while it runs,
unless `--no-record` is given. See [`ui.md`](ui.md) for the screens and
wire protocol.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--tickers` | auto-discover | comma-separated Kalshi tickers |
| `--top` | 8 | liquid Kalshi markets to auto-discover |
| `--host` | `UI_HOST` env / `127.0.0.1` | bind host |
| `--port` | `UI_PORT` env / 8080 | bind port |
| `--no-record` | off | don't write raw messages to Postgres |
| `--poly-top` | 8 | Polymarket US markets to poll (0 disables) |
| `--poly-slugs` | — | explicit slugs, overrides discovery |
| `--pairs-top` | 10 | confirmed pairs (by score) to track on the ARB screen (0 disables) |
| `--paper` | off | simulate fills on measured edges (see [`engine.md`](engine.md#paper-trading)) |
| `--min-net-ticks` | 50 | paper: minimum net edge per contract, in ticks, to take a trade |
| `--max-cts-per-pair` | 100 | paper: maximum contracts held per pair |
| `--max-notional` | 1000 | paper: maximum total cost across all pairs, in dollars |

Details: [`src/arb/ui/server.py`](../src/arb/ui/server.py).

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

Details: [`src/arb/replay.py`](../src/arb/replay.py).

## `arb pairs`

Cross-venue pair matching — propose candidates, then review. See
[`pairs.md`](pairs.md) for the matching algorithm and storage model.

### `arb pairs propose`

```sh
uv run arb pairs propose [--min-score 0.75] [--no-record]
```

Fetches both venues' full open/active universes (recorded before parsing,
unless `--no-record`), runs the matcher, and upserts candidates into
Postgres (never overwriting an existing human decision). Prints a
plain-text table of the top proposals.

### `arb pairs list`

```sh
uv run arb pairs list [--status proposed|confirmed|rejected] [--limit N]
```

Lists stored pairs, sorted by score descending, optionally filtered by
status.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--status` | all | show only `proposed` / `confirmed` / `rejected` |
| `--limit` | 50 | max rows to print, best score first (`0` = all) |

A full `propose` run over both live universes stores **thousands** of
candidates, so the listing is capped by default; the cap is applied in SQL,
not after fetching everything. When output is truncated, a note goes to
stderr (never stdout, so it can't pollute a pipe). Piping into `head` or
`less` is safe — a closed pipe exits quietly with status 141 rather than
raising `BrokenPipeError`.

### `arb pairs show ID`

```sh
uv run arb pairs show 28
```

Full detail for one pair with **identifiers printed in full** — both legs'
market ids, tickers/slugs, event titles, outcomes, close times, rules text
and the matcher's scoring features.

This exists because a table row can't carry what you need to actually look a
market up. Neither venue's website matches a market slug or ticker in its
search box, and Polymarket US's site routes by *event* slug, not market slug
— so pasting `pnwpc-elonmusk-2026-12-31-gt600b` into the site finds nothing
even though that market is live. `show` prints the event title (which the
site does match) and, for Polymarket US, the verified
`https://polymarket.us/event/<event-slug>` link. See
[`venues/polymarket-us.md`](venues/polymarket-us.md).

Pairs proposed before the event slug was captured are fixed by
`arb pairs backfill` (below); the event title is always available either way.

### `arb pairs backfill`

```sh
uv run arb pairs backfill [--no-record]
```

Records the venue `event_slug` on pairs that were proposed before the
matcher captured it, so `arb pairs show` can build a link for them. Fetches
both universes exactly as `propose` does, but writes **only** the missing
field — no re-scoring, no new rows, and no human decision is touched.

Markets that have since closed or resolved are not in the active universe
and keep an empty event slug; they are untradeable anyway, so no link is
needed. Run it once after upgrading past the change that added event slugs.

### `arb pairs confirm ID` / `arb pairs reject ID`

```sh
uv run arb pairs confirm 42
uv run arb pairs reject 43
```

Sets one pair's status by id. (Batch decisions and "undo back to proposed"
are available in the terminal UI's `PAIRS` screen — see
[`pairs.md`](pairs.md#human-review-in-the-terminal-ui) — but not yet as a
CLI subcommand.)

Details: [`src/arb/pairs/run.py`](../src/arb/pairs/run.py),
[`src/arb/pairs/store.py`](../src/arb/pairs/store.py).

## Database migrations (not an `arb` subcommand)

```sh
uv run alembic upgrade head
```

Reads the database URL from `AppConfig` (environment / `.env`), never from
`alembic.ini` directly — so no connection string, credentials included,
ever lives in a committed file. See [`ops.md`](ops.md#migrations) and
[`data-model.md`](data-model.md#storage-schema).

## Checks (not `arb` subcommands, run directly)

```sh
uv run pytest
uv run ruff check .
uv run pyright
```

Run these after any change; see [`testing.md`](testing.md).
