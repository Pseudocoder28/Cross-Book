# Architecture

This is the end-to-end tour: what processes exist, how a byte from a venue
becomes a book, a quote, and (in paper mode) a simulated trade, and which
module owns which responsibility. Read [`data-model.md`](data-model.md)
alongside this for the exact shapes involved.

## The shape of the system

```
                    ┌────────────────────────────────────────────────┐
                    │                  venue layer                    │
                    │        src/arb/venues/<kalshi|polymarket_us>/   │
                    │                                                  │
  Kalshi WS  ──────▶│  auth.py   ws.py   rest.py   discovery.py       │
  Polymarket  ─────▶│  (signing) (parse) (parse)   (universe/targets) │
  REST polls         │  source.py = EventSource   adapter.py = MDA    │
                    └───────────────┬───────────────────┬────────────┘
                                    │ RawMessage          │ BookEvent
                                    ▼                     ▼
                    ┌───────────────────────┐   ┌──────────────────────┐
                    │   Recorder (queue)     │   │  BookManager (books) │
                    │   src/arb/recorder.py  │   │  src/arb/books.py    │
                    └───────────┬────────────┘   └──────────┬───────────┘
                                │ batched insert             │ per-market Book
                                ▼                             │ (src/arb/book.py)
                    ┌───────────────────────┐                 │
                    │  Postgres              │                 ▼
                    │  raw_messages table    │   ┌──────────────────────────┐
                    └───────────┬────────────┘   │  ArbMonitor (edge quotes)│
                                │                 │  src/arb/arbmon.py       │
                       arb replay reads this      │  uses fees.py + edge.py │
                                │                 └───────────┬──────────────┘
                                ▼                              │
                    ┌───────────────────────┐                 ▼
                    │  src/arb/replay.py     │   ┌──────────────────────────┐
                    │  same adapters +       │   │  PaperTrader (optional)  │
                    │  BookManager, replayed │   │  src/arb/paper.py        │
                    └────────────────────────┘   └──────────────────────────┘

  All of the above is driven by two entry points that wire it together:
  src/arb/record.py (`arb record`, headless) and
  src/arb/ui/server.py (`arb ui`, terminal UI + optional recording).
```

## The shared interfaces every venue implements

Two `Protocol`s in [`src/arb/interfaces.py`](../src/arb/interfaces.py) are
the seam between venue-specific code and everything else:

- **`EventSource`** — an async stream of raw venue messages
  (`stream() -> AsyncIterator[RawMessage]`). Implementations own reconnect,
  backoff, heartbeats, stall detection and resubscribe; consumers just
  iterate. Kalshi's implementation (`KalshiWSSource`) wraps a WebSocket;
  Polymarket US's (`PolymarketUSRestSource`) wraps a rate-limited REST
  poller. Both yield the same `RawMessage` envelope, so `arb record`, the UI
  server and the recorder never care which kind of source they're reading.
- **`MarketDataAdapter`** — turns one `RawMessage` into zero or more
  normalized `BookEvent`s (`BookSnapshot | BookLevelUpdate | ResyncRequired`).
  All venue-specific field names, price parsing and NO-side folding happen
  here; what comes out is venue-agnostic.

Everything downstream of an adapter — `Book`, `BookManager`, `ArbMonitor`,
`PaperTrader`, the UI — works only in terms of these normalized types, ticks
and `Qty`. See [`data-model.md`](data-model.md) for their exact shapes and
[`venues/kalshi.md`](venues/kalshi.md) /
[`venues/polymarket-us.md`](venues/polymarket-us.md) for how each venue's
`EventSource` and `MarketDataAdapter` are built.

Venue-specific code lives *only* under `src/arb/venues/<venue>/`; this is
enforced by convention (and by the fact that nothing outside those
directories imports venue modules except through the two protocols above,
`market_id()` helpers, and the discovery/detail functions the CLI and UI
call directly).

## Recorder-first ingest

Every raw inbound message — WebSocket frame or REST response — is handed to
the [`Recorder`](../src/arb/recorder.py) **before** it is parsed. This is a
hard rule (see `CLAUDE.md`): the archive must be complete even when a parser
chokes on an unexpected payload shape.

`Recorder.enqueue()` is synchronous and non-blocking: it puts the message on
a bounded `asyncio.Queue` (default 100k) and returns immediately. If the
queue is full, the message is dropped and counted
(`arb_recorder_dropped_total`) rather than blocking the caller — losing one
message is preferable to stalling a venue feed and losing the connection.

`Recorder.run()` is the writer loop: it drains the queue into batches (up to
`recorder_batch_max`, default 500) and hands each batch to a `Sink` — in
production, `insert_raw_messages` ([`storage/db.py`](../src/arb/storage/db.py))
which bulk-inserts into the `raw_messages` table in one transaction. A
failing write retries in place with backoff (`arb_recorder_write_failures_total`);
batches are never reordered or silently dropped. `Recorder.run()` is always
run under `supervise()` (see below).

On shutdown, `Recorder.drain(timeout_s)` waits for the queue to empty
(`Queue.join()`) before the writer task is cancelled, so a clean Ctrl-C
doesn't lose the last few messages.

## Reliability layer

Two small modules under `src/arb/` provide the reliability guarantees
`CLAUDE.md` requires, and every venue and long-running loop builds on them:

- **`supervise.py`** — `Backoff` (exponential with jitter, resets after a
  run survives `healthy_after_s`) and `supervise(task_factory, name=...)`,
  which runs a coroutine forever, restarting it with backoff on any
  exception (never swallowing `CancelledError`) and counting
  `arb_supervisor_restarts_total{task=...}`. Every long-running loop in the
  system — the recorder writer, each venue's consume loop, the UI's stats
  loop, book-flush loop, etc. — runs under `supervise()`, each with its own
  task, so one venue or one loop failing never takes another down.
- **`ws.py`** — `ReconnectingWebSocket`, a venue-agnostic `EventSource` over
  one WebSocket endpoint. Venue code supplies a `connector` (owns the URL
  and recomputes signed auth headers on every attempt — both venues sign a
  timestamp into the handshake) and an `on_connected` callback
  (re-subscribe). The client handles reconnect with backoff+jitter, stall
  detection (no inbound frame within `stall_timeout_s` ⇒ assume half-open,
  reconnect), protocol-level ping/pong heartbeat, and stamps every yielded
  frame into a `RawMessage`. `force_reconnect()` lets a consumer force a
  fresh snapshot after a detected gap, by closing the live connection so the
  loop reconnects and resubscribes.

`RunContext` ([`run.py`](../src/arb/run.py)) allocates a sortable `run_id`
(UTC timestamp + random suffix) once per process run, plus a monotonically
increasing `ingest_seq` shared across every source in that run — this is
what makes `raw_messages` totally ordered per run even with two venues
ingesting concurrently, and is the join key `arb replay` uses to reconstruct
history.

## Books: from events to validated state

[`Book`](../src/arb/book.py) is a pure data structure — no I/O, no clock
access, no metrics — holding one market's YES bid/ask ladders and enforcing
the validity rules from `CLAUDE.md` (snapshot present, no seq gap, not
crossed, all levels positive-qty and in-range, not stale). See
[`data-model.md`](data-model.md) for the full rule set and state machine.

[`BookManager`](../src/arb/books.py) owns one `Book` per market id (created
lazily), applies the stream of `BookEvent`s from an adapter, and reports
which market ids changed on each call to `apply()` — that's the fan-out
signal the UI and `ArbMonitor` use to know what to re-publish or re-quote.
It's also where `arb_book_invalidations_total` is incremented (the `Book`
itself has no metrics dependency) and where per-venue staleness budgets live
(`set_venue_staleness`) — a polled venue like Polymarket US only refreshes
once per poll cycle, so it needs a longer staleness allowance than a
streaming venue or every book would flag stale between polls.

## The two live entry points

- **`arb record`** ([`record.py`](../src/arb/record.py)) — headless: wires
  a Kalshi `EventSource` and (if targets resolve) a Polymarket US
  `EventSource` straight into the `Recorder`, with no parsing into `Book`s
  at all. This is the minimal read path used for pure data capture.
- **`arb ui`** ([`ui/server.py`](../src/arb/ui/server.py)) — does everything
  `arb record` does (recorder-first ingest, supervised tasks) *and*
  additionally parses every frame through the venue adapters into a shared
  `BookManager`, optionally loads confirmed pairs into an `ArbMonitor`,
  optionally runs a `PaperTrader` over live quotes, and pushes JSON frames
  to connected browser clients over WebSocket. See [`ui.md`](ui.md) for the
  wire protocol and [`engine.md`](engine.md) for the monitor/trader.

Both entry points are supervised the same way: each venue's consume loop is
its own `asyncio.Task` under `supervise()`, so a Kalshi WS failure never
stops Polymarket US polling (or vice versa) and a crash restarts with
backoff rather than killing the process.

## Offline reconstruction: replay

[`replay.py`](../src/arb/replay.py) is not a second implementation of the
live pipeline — it is *the same* adapters and the same `BookManager`, fed
from `raw_messages` instead of a live source, using the recorded
`recv_mono_ns` as the clock. This is why replay results are deterministic
and why there is no drift risk between "what the live system would have
done" and "what replay says happened": there is only one code path from
`RawMessage` to `Book` state and edge quotes; replay just supplies the
`RawMessage`s from Postgres instead of a venue. See
[`engine.md`](engine.md#replay) for details, including optional paper
trading over history.

## Where the data ends up

- **Postgres** (`raw_messages`, `pairs`, `paper_trades` — see
  [`data-model.md`](data-model.md#storage-schema)) is the durable record.
- **Prometheus** scrapes `/metrics` from whichever entry point is running
  (`arb record` starts a dedicated metrics server; `arb ui` serves
  `/metrics` from its own FastAPI app). Metric names are all declared in
  [`metrics.py`](../src/arb/metrics.py).
- **Grafana** has one provisioned dashboard ("ARB — Data Plane") over that
  Prometheus data. See [`ops.md`](ops.md).
