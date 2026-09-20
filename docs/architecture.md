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
                    │  same adapters +       │   │  PaperTrader (one)       │
                    │  BookManager, replayed │   │  src/arb/paper.py        │
                    └────────────────────────┘   └──────────────────────────┘
                            ▲ subprocess
                            │
  ┌─────────────────────────┴──────────────────────────────────────────────┐
  │  ControlPlane.execute()   src/arb/ui/control.py                        │
  │  ONE executor: validate → read-only gate → arm/confirm → apply →       │
  │  audit row (control_actions) → broadcast. Holds live handles on the    │
  │  Kalshi source, the PM poller, BookManager, PaperTrader, ArbMonitor,   │
  │  the recording flag and the RunContext. JobRunner runs the long ones.  │
  └─────────────────────────▲──────────────────────────────────────────────┘
                            │ POST /api/control/{action}  (only write path)
  ┌─────────────────────────┴──────────────────────────────────────────────┐
  │  OriginGuardMiddleware (pure ASGI: http AND websocket)  ui/security.py │
  └────────────────────────────────────────────────────────────────────────┘

  One entry point wires all of the above together: src/arb/ui/server.py
  (`arb ui` — terminal UI, ingest, recording toggle and the control plane).
```

## The shared interfaces every venue implements

Two `Protocol`s in [`src/arb/interfaces.py`](../src/arb/interfaces.py) are
the seam between venue-specific code and everything else:

- **`EventSource`** — an async stream of raw venue messages
  (`stream() -> AsyncIterator[RawMessage]`). Implementations own reconnect,
  backoff, heartbeats, stall detection and resubscribe; consumers just
  iterate. Kalshi's implementation (`KalshiWSSource`) wraps a WebSocket;
  Polymarket US's (`PolymarketUSRestSource`) wraps a rate-limited REST
  poller. Both yield the same `RawMessage` envelope, so the UI server, the
  recorder and replay never care which kind of source they're reading. Each
  also carries the one mutator the control plane needs — `set_tickers()` +
  `force_resync()` on Kalshi, `set_targets()` on the poller — because the
  subscribed set is now a runtime decision, not a constructor argument.
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
`market_id()` helpers, and the discovery/detail functions the UI server and
the control plane's pair jobs call directly).

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
(`Queue.join()`) before the writer task is cancelled, so a clean stop doesn't
lose the last few messages. "Clean" covers SIGTERM as well as Ctrl-C:
`run_ui` hands its startup-and-serve and its cleanup to
`serve_then_cleanup` ([`shutdown.py`](../src/arb/shutdown.py)), which installs
a SIGTERM handler that does not terminate before uvicorn takes the signals.
Without it, uvicorn's re-delivery of SIGTERM after its own graceful shutdown
killed the process before the drain — on every `docker compose stop`. A drain
that runs out of time (the sink cannot write) is logged and counted
(`arb_recorder_drain_timeouts_total`); see
[`ops.md`](ops.md#stopping-and-what-a-stop-flushes) for the sequence, its
time budget and why that counter is not one Prometheus will ever see move.

Recording is a runtime toggle, so the recorder and its supervised writer are
built whatever `--no-record` said and the server's sink (`record_raw()`) reads a
flag **per message** and returns early when it is off. A toggle that had to
construct a recorder and start a writer mid-flight is a toggle that can fail
halfway. `ingest_seq` is allocated by the source either way, so a recording gap
leaves a hole in the sequence — which only has to increase, not be contiguous —
and the `control_actions` row is the only evidence the hole was deliberate.

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

There is exactly **one** `RunContext` per process, and the control plane's jobs
*borrow* it (`pairs.run.JobDeps` carries the server's engine, `RunContext` and
recorder sink) rather than building their own. A second `RunContext` on the same
`run_id` restarts `ingest_seq` at 0; `raw_messages` is `UNIQUE (run_id,
ingest_seq)`, `insert_raw_messages` has no `ON CONFLICT`, and
`Recorder._write_with_retry` retries a failing batch forever — so one duplicate
would wedge the server's recorder permanently while the UI's REC pill still read
ON. Borrowing is not tidiness; it is the difference between recording and not.

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

Because the universe is now mutable, the manager carries two operations the
control plane needs and that only make sense together:

- **`retain(ids, venue=...)` / `evict()`** — drop the books for markets that
  left the subscription. A dropped market's book never updates again: left in
  place it would age into permanent staleness and still be shipped to every new
  client as if it were a market being watched. `venue=` scopes the sweep, so
  changing the Kalshi subscription cannot evict Polymarket's books.
- **`set_venue_staleness()` retunes books that already exist**, not just ones
  created afterwards, and returns the ids it touched (which the UI then marks
  dirty, because a staleness verdict may have flipped). The poll cycle is a
  function of the target count, so growing the poll universe without this leaves
  every live book judged against a budget computed for a smaller one — flapping
  STALE for a reason the operator just caused and cannot see.

## The entry points: one process, three commands

The system used to be a command set; it is a process with a control plane now.
`arb record` and the whole `arb pairs` subtree are **deleted** — recording, pair
proposal, backfill, review and every limit are buttons. What is left is what a
button cannot be:

| Command | Why it survived |
| --- | --- |
| `arb ui` ([`ui/server.py`](../src/arb/ui/server.py)) | the server the buttons live in. Its flags are *starting* values the UI then changes; `docker-compose.yml` runs `arb ui --top 20 --host 0.0.0.0`, so the flag names are a live dependency. New: `--read-only` |
| `arb doctor` ([`doctor.py`](../src/arb/doctor.py)) | what you run when the UI will **not** start. A button inside a dead server diagnoses nothing. (`/control` also exposes it as the `jobs.doctor` job, for when the server *is* up) |
| `arb replay` ([`replay.py`](../src/arb/replay.py)) | now a **machine interface** as well as a human one: it is the worker `jobs.replay` spawns. Its argv is a contract — `tests/test_cli.py` pins the flags `ControlPlane._apply_replay` passes — and changing a flag name or a default breaks the UI silently |

`run_ui()` wires the lot: recorder-first ingest, every venue consume loop and
every service loop its own `asyncio.Task` under `supervise()` (so a Kalshi WS
failure never stops Polymarket US polling and a crash restarts with backoff
rather than killing the process), the adapters into a shared `BookManager`, the
confirmed pairs into an `ArbMonitor`, one `PaperTrader`, the WebSocket fan-out,
`GET /metrics`, and finally the control plane over all of it. Nothing binds
`:9000` any more, so Prometheus has a single `arb-ui` target. See
[`ui.md`](ui.md) for the wire protocol, [`engine.md`](engine.md) for the
monitor/trader and [`cli.md`](cli.md) for the flags.

## The control plane

[`ui/control.py`](../src/arb/ui/control.py) is the only thing in the system that
changes a running process. Everything mutable that used to be a local of
`run_ui()` — the Kalshi source, the Polymarket US poller, the `BookManager`, the
`PaperTrader`, the `ArbMonitor`, the recording flag, the `RunContext` — is a
handle on a `ControlPlane`, and a request handler can reach it.

### One executor, thirteen actions

Every action is an `ActionSpec` row (name, consequence grade, summary,
`validate`, `effect`, `apply`, `mutates`, optional `confirm`) and every call
goes through `ControlPlane.execute(action, params, *, confirm, actor)`, in this
order:

1. unknown action → `404`;
2. `spec.validate(params)` → a clean dict, or `400` **before anything happens**;
3. `spec.effect(clean)` → the human sentence, derived from the *validated*
   parameters and then used for everything: shown to the operator, carried on
   the confirm token, and written into the audit row;
4. read-only refusal (`403`) if the server was started `--read-only` and the
   action mutates — and the refusal is itself audited;
5. arm-or-confirm for actions that need it;
6. `spec.apply(clean)`;
7. the audit row;
8. `broadcast_state()` — a full `control` frame to every client.

A route per toggle would be a route per chance to forget one of those steps.
Here they are structural: an action is a row in a table and cannot opt out. The
HTTP layer is one handler with the action in the path, and `ControlError`
carries its own `status_code`, so the failure and the status it produces live
together instead of in a translation table that drifts.

The thirteen: `recording.start/stop`, `paper.suspend/resume`, `paper.limits`,
`pairs.top`, `universe.kalshi`, `universe.polymarket`, `jobs.doctor`,
`jobs.propose`, `jobs.backfill`, `jobs.replay`, `jobs.cancel`. Grades are the
consequence grades from [`decisions.md`](decisions.md) — one G0, nine G2, three
G3, no G4 yet — and the browser reads them off the wire rather than restating
them (see [`ui.md`](ui.md#control--control)).

### Confirmation is server-side, and so is the sentence

An action whose spec asks for confirmation — `jobs.propose` and `jobs.backfill`
always, `jobs.replay` only when `persist` is set, because a replay that writes
nothing is not worth a second click — called without a token is *armed*: the
plane writes the audit row, mints a single-use token bound to `sha256(action,
params)` with a 90 s TTL, and answers `428` with the token, the effect sentence
and the remaining seconds. The
second call spends the token. Binding to the parameter hash is the point —
arming `jobs.propose` at `min_score 0.75` and confirming it at `0.10` is refused
("the parameters changed since this action was armed"), and a mismatched attempt
*burns* the token rather than allowing another guess. A confirmation implemented
in the browser would be invisible to the audit log and to `curl`; this one is
not.

### The audit trail, and failing closed

Every executed, refused, failed or armed action writes a `control_actions` row
(migration `0004`) when a database engine is attached: timestamp, run id,
action, params, actor, result (`ok` / `armed` / `refused` / `error`), error, and
`effect` —
**the sentence the operator was shown**, stored verbatim rather than re-derived,
because what is worth reading back a week later is what they were told they were
agreeing to.

Arming a G3 action writes that row *before* the token exists, and fails closed:
if the write fails the action is refused with `503` and never becomes
confirmable. A G3 control that cannot be recorded must not be available. A G2
control does the opposite — it happens and the audit write is best-effort —
because refusing to stop recording because the database is unhappy is the wrong
failure. Both paths count `arb_control_audit_failures_total`, which is the thing
worth alerting on.

### The job runner: three execution strategies, each for one reason

Long actions run as `JobRecord`s under a `JobRunner` (single-flight per *group*,
cancellable, bounded output, history kept so a finished job is still readable
after the browser reconnected). What differs is *where* the work runs, and the
constraint is always the same: `ws.py` runs with `ping_timeout_s = 10.0`, so ten
seconds of blocked event loop drops the Kalshi socket — and any block at all
corrupts the one-way latency measurement.

| Job | Strategy | Why that one |
| --- | --- | --- |
| `jobs.propose`, `jobs.backfill` | `await`ed on the loop; the **scoring stage only** is carried to a worker thread inside [`pairs/run.py`](../src/arb/pairs/run.py) (`asyncio.to_thread`) | the loop-affine parts (venue fetches, the recorder sink, `RunContext` sequencing, every database write) must stay on the loop; the dominant cost (~minutes of pure CPU over immutable data) must not. `pairs/run.py`'s contract is explicit that the caller must **not** wrap the whole coroutine in `to_thread` |
| `jobs.replay` | a **subprocess** (`python -m arb.cli replay ...`), stdout streamed back line by line | `replay.py`'s per-row loop contains no `await` and yields only every 2000 rows, so in-process it holds the loop for seconds to minutes. A subprocess cannot stall this loop at all. Cancellation is SIGTERM, then SIGKILL after a 5 s grace |
| `jobs.doctor` | plain in-process `await` | it is a handful of short network and disk checks; a thread or a process would be ceremony |

Single-flight is per group, not per name: `propose` and `backfill` fetch the same
two universes and write the same table, so they share the `pairs` group and the
second one gets a `409` instead of a job record that was always going to fail.

### What the mutations cost, and why each one is a method rather than a rebuild

The runtime pieces grew narrow mutators instead of being reconstructed, and in
every case the reason is state that would be lost or silently reset:

- `KalshiWSSource.set_tickers()` + `force_resync()` — a resubscribe *is* a
  reconnect on Kalshi (it answers with a fresh snapshot per market), so this
  costs a gap and a full resnapshot. Stated on the button.
- `PolymarketUSRestSource.set_targets()` — live, no reconnect; an empty target
  list is a supported idle state.
- `PaperTrader.suspend()`/`resume()` rather than a new trader: `room_notional =
  max_notional_ticks − notional_ticks` lives on the instance, so rebuilding on
  toggle-on would reopen the whole spend budget and drop the positions and the
  ledger.
- `pairs.top` rebuilds the `ArbMonitor` and re-derives both venues' universes
  from the tracked pairs (the plane keeps the operator's *base* set and the
  *pair* set apart, so a pairs reload drops its own old legs and nobody else's).

## The network security floor

[`ui/security.py`](../src/arb/ui/security.py) exists because a browser page can
now start and stop a process that will later place real orders. Before it, a
cross-site POST returned `200` and `WS /ws` accepted any `Origin`.

The threat model is small and stated in the module: this is a single-operator
localhost tool with no login, no session and no user table, so there is nothing
to bind a CSRF token *to* — a token minted by the same server that accepts it,
with no session behind it, is an Origin check with extra steps. What a
browser-borne attacker actually has is a cross-site request and DNS rebinding.
So there are three defences and no tokens:

- **`OriginGuardMiddleware`** — pure ASGI (not `BaseHTTPMiddleware`, which only
  ever sees `http` scopes) so it covers the WebSocket handshake too, which is
  exactly the hole that matters: the same-origin policy does not apply to
  `WebSocket`, so `Origin` is the only defence there. `Host` must resolve to
  loopback or be allowlisted (that kills DNS rebinding — a rebound request still
  carries the attacker's hostname); any non-`GET`, and every WS handshake, is
  refused on `Sec-Fetch-Site: cross-site` or a non-loopback `Origin`. A *missing*
  `Origin` is allowed: that is `curl` or the CLI, a process already on this
  machine. Rejections are counted and answered with `403`, or a `1008` close on
  the socket; the counter is `arb_ui_requests_rejected_total{scope,reason}`,
  declared in `security.py` itself with a standing TODO to move it into
  `metrics.py` where every other metric name lives.
- **`check_bind_host()`** — a startup refusal, not a runtime path: binding
  anything but loopback needs `ARB_ALLOW_REMOTE_BIND=1`. Compose legitimately
  binds `0.0.0.0` *inside* its network (the published port is still
  `127.0.0.1`-only), which is why the hatch exists and why compose sets it. When
  the bind is not loopback, the UI says so in a red banner — the fact travels in
  the `control` payload rather than being guessed by the page.
- **`--read-only` / `UI_READ_ONLY`** — serve every view, refuse every mutating
  action at the executor (only `jobs.doctor`, the one non-mutating action, still
  runs). The flag only ever turns it *on*: `UI_READ_ONLY` in the environment
  stays in force when the flag is absent, so the safer setting cannot be
  silently dropped by a launch command that forgot it. The disabled buttons are
  a courtesy; the refusal is the control.

If the UI is ever exposed beyond loopback for real, the answer is an
authenticating reverse proxy or the SSH tunnel that is already the documented
way in (see [`ops.md`](ops.md#security-posture-localhost-only)) — not a
home-grown token.

## Offline reconstruction: replay

[`replay.py`](../src/arb/replay.py) is not a second implementation of the
live pipeline — it is *the same* adapters and the same `BookManager`, fed
from `raw_messages` instead of a live source, using the recorded
`recv_mono_ns` as the clock. This is why replay results are deterministic
and why there is no drift risk between "what the live system would have
done" and "what replay says happened": there is only one code path from
`RawMessage` to `Book` state and edge quotes; replay just supplies the
`RawMessage`s from Postgres instead of a venue. See
[`engine.md`](engine.md#replay-the-live-pipeline-fed-from-postgres) for details, including optional paper
trading over history.

Replay is also the one piece the live process runs *out of* process: the
`jobs.replay` action spawns `python -m arb.cli replay` and streams its stdout
into the job's output. Having two callers — a human on the VM and the control
plane — is why "bad or missing run id" is now a plain `ReplayError` rather than
`SystemExit`. `SystemExit` is a `BaseException`: Starlette's error middleware
does not catch it and task groups read it as a shutdown request, so "no recorded
runs yet" would have taken a server down instead of returning a 4xx. The CLI
turns it back into an exit status; that direction is the safe one.

## Where the data ends up

- **Postgres** (`raw_messages`, `pairs`, `paper_trades`, `control_actions` —
  see [`data-model.md`](data-model.md#storage-schema)) is the durable record.
  `control_actions` is the only record that a gap in recording, a limit change
  or a job that wrote twelve thousand rows was *deliberate* rather than a crash.
- **Prometheus** scrapes `GET /metrics` from the `arb ui` process, which serves
  it from its own FastAPI app; there is no second metrics server and nothing
  listens on `:9000` any more. Metric names are all declared in
  [`metrics.py`](../src/arb/metrics.py), including the control plane's
  `arb_control_actions_total{action,result}`,
  `arb_control_jobs_total{job,result}`, `arb_control_audit_failures_total` and
  `arb_control_job_output_dropped_total`.
- **Grafana** has one provisioned dashboard ("ARB — Data Plane") over that
  Prometheus data. See [`ops.md`](ops.md).
