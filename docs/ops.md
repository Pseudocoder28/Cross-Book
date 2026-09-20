# Operations

Covers the local/VM infrastructure stack, deployment posture, migrations,
the control plane's operational surface, and monitoring. See
[`cli.md`](cli.md) for the three commands (`ui`, `doctor`, `replay`)
referenced here — everything else is operated from the UI.

## Security posture: localhost-only

Every stateful service — Postgres, Prometheus, Grafana, and the app's UI
port — binds to `127.0.0.1` on the host, never `0.0.0.0`, per `CLAUDE.md`.
On a VM these are reached over an SSH tunnel, never exposed publicly. Inside
the Compose network (container-to-container), services do bind `0.0.0.0` on
their *internal* interface so other containers can reach them — the
`docker-compose.yml` comments call this out explicitly at each override —
but the **host port mapping** for every service stays `127.0.0.1:<port>:<port>`.

Since the UI grew controls that start and stop the trading process, that
posture is enforced in code rather than in convention: `arb ui` refuses a
non-loopback bind without an explicit opt-in, and every request is checked
against `Host` and `Origin` — see [The control
plane](#the-control-plane).

## Docker Compose stack

[`docker-compose.yml`](../docker-compose.yml). Four services:

| Service | Image | Host port | Purpose |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | primary datastore (pgvector included for future use; not yet used) |
| `prometheus` | `prom/prometheus:v2.53.0` | `127.0.0.1:9090` | scrapes the app's `/metrics` (one job, `arb-ui` → `app:8080`) |
| `grafana` | `grafana/grafana:11.1.0` | `127.0.0.1:3000` | dashboards over the Prometheus datasource (`admin`/`admin`) |
| `app` | built from `Dockerfile` | `127.0.0.1:8080` | runs `arb ui --top 20 --host 0.0.0.0` — the terminal UI, the recorder **and** the control plane in one process |

Images are pinned (not `:latest`) so a `docker compose pull` doesn't
silently change behavior underneath the stack. `postgres` has a real
healthcheck (`pg_isready`); `app` waits on `postgres: condition:
service_healthy` before starting, so the app never races a not-yet-ready
database.

`app`'s environment overrides two things that still matter for the
in-network posture: `DATABASE_URL` points at the `postgres` service name
(not `127.0.0.1`, since it's a different container) and `UI_HOST=0.0.0.0`
(so the UI is reachable from the host's own mapped port). `secrets/` is
bind-mounted read-only into the container at `/app/secrets`.

**Note on the metrics port**: there is no `:9000` server any more. `arb ui`
serves `/metrics` from its own FastAPI app on the UI port (8080), and the
only command that ever started a standalone metrics server was `arb record`,
which is deleted. Prometheus's `arb` job (`app:9000`) was removed with it;
`arb-ui` (`app:8080`) is the only arb target
([`infra/prometheus.yml`](../infra/prometheus.yml)). `METRICS_HOST` /
`METRICS_PORT` survive in `AppConfig` and in Compose's environment but bind
nothing — harmless, and not a target to point a scrape at.

### Bringing the stack up

```sh
docker compose up -d
uv run alembic upgrade head       # REQUIRED; from the host, DATABASE_URL from .env points at 127.0.0.1:5432
docker compose exec app arb doctor
```

`alembic upgrade head` is no longer a nicety: without migration `0004` the
`control_actions` table does not exist and half the UI's controls refuse to
run — see [below](#the-migration-is-not-optional-any-more).

**The app container needs `ARB_ALLOW_REMOTE_BIND=1`.** Compose runs
`arb ui --host 0.0.0.0`, and a non-loopback bind is refused at startup
([Binding beyond loopback](#binding-beyond-loopback)), so without the
escape hatch the container exits immediately and `restart: unless-stopped`
turns that into a crash loop. Set it in the `app` service's `environment:`,
or in the `.env` the service already loads through `env_file`:

```sh
echo 'ARB_ALLOW_REMOTE_BIND=1' >> .env    # published port stays 127.0.0.1:8080
```

Putting it in `.env` also hands it to host-side `uv run arb ui`, where it is
inert: the hatch is only consulted when a non-loopback host is actually
requested, and the host default is `127.0.0.1`.

### Stopping, and what a stop flushes

`docker compose stop`, `down` and `restart` all send the app **SIGTERM**, and
`arb ui` treats it exactly like Ctrl+C in a terminal: the HTTP server stops,
then cleanup runs to the end, then the process exits 0. (Until M22 it did
not: SIGTERM killed the process the moment uvicorn finished, and every stop
silently dropped whatever the recorder still had queued. See
[`decisions.md`](decisions.md), M22.) In order, with the most each step can
take:

| Step | At most | What bounds it |
| --- | --- | --- |
| uvicorn closes browser connections | 5 s | `HTTP_GRACEFUL_TIMEOUT_S`; normally 0.1 s |
| running jobs are cancelled | 5 s | a `replay` subprocess gets SIGTERM, then SIGKILL (`JOB_TERMINATE_GRACE_S`) |
| venue feeds are closed | 10 s | the Kalshi socket's closing handshake (`websockets` default `close_timeout`) |
| the recorder queue is written to Postgres | 10 s | `DRAIN_TIMEOUT_S` |
| writer cancelled, database engine disposed | — | no timeout of its own; normally instant |
| the interpreter exits | — | waits for any worker thread still running (see the 137 bullet below) |

A normal stop is well under a second. The bounded steps add up to 30 s, which
is why the `app` service sets `stop_grace_period: 45s`: Docker's default is
10 s, after which it sends SIGKILL, and SIGKILL flushes nothing. The 15 s of
slack is what covers the unbounded engine dispose. `tests/test_shutdown.py`
fails if those constants outgrow the compose number, or if `init: true` goes.
`uv run` does forward SIGTERM to the Python process (documented by uv, and
checked), so `init: true` is not what makes the drain work — it is there so a
stop never depends on how PID 1 treats a signal it has no handler for.

The last log line of a clean stop says where cleanup's time went:

```
shutdown complete in 0.2s (jobs and feeds 0.0s, recorder drain 0.1s, drained)
```

It times cleanup only. uvicorn's own step comes first and is not in the total
(compare the timestamp of uvicorn's `Shutting down` line with this line's),
and neither is whatever the interpreter does after cleanup.

What to do with what you see in `docker compose logs app`:

- **`recorder drain timed out after 10s` and `TIMED OUT` in the last line** —
  queued messages were abandoned. The sink could not write for the whole
  window: Postgres was down or unreachable, or the writer was still inside a
  retry backoff (up to 30 s) after an outage. The run has a hole at its end;
  `arb_recorder_write_failures_total` in the minutes before the stop says how
  long the database had been gone. Stopping `postgres` before `app` does this
  every time — `docker compose stop` orders it correctly; stopping services by
  hand may not.
- **About 10 s under `jobs and feeds`** — the Kalshi socket did not complete
  its closing handshake. Nothing is lost; it is just slow.
- **uvicorn's `Cancel N running task(s), timeout graceful shutdown exceeded`
  (ERROR)** — the stop spent its first 5 s on an HTTP request that never
  answered; uvicorn cancelled it and cleanup went ahead. Nothing is lost.
- **No `shutdown complete` line at all** — the process was killed before
  cleanup finished: SIGKILL, `docker compose kill`, an OOM kill, or a grace
  period that ran out (`docker inspect` shows exit code 137). Assume the tail
  of the run is missing. The one harmless case is exit code 143 from a stop
  issued in the container's first second: until `arb ui` has finished
  importing and building its engine SIGTERM still has its default effect, and
  by then nothing has been recorded or spawned.
- **Exit code 137 (or 143 after a repeated `kill`) *with* a `shutdown
  complete` line** — cleanup finished and nothing was lost. The process was
  waiting for a worker thread to end: a propose or backfill job's scoring
  stage runs in a thread for minutes and cannot be interrupted, and Python
  does not exit until it returns. A stop, restart or redeploy during scoring
  therefore takes the full 45 s and ends in SIGKILL. Do not raise
  `stop_grace_period` to cover it, and `jobs.cancel` cannot stop the thread
  either: wait for scoring to finish before stopping, or accept the 45 s.
- **A second SIGTERM changes nothing**, by design; cleanup carries on. Neither
  does one Ctrl+C: if cleanup is already running when it arrives (`kill <pid>`,
  then Ctrl+C in the terminal), it is held until cleanup has finished — the log
  says `interrupted during cleanup: finishing it first` — and the exit status
  is 130 instead of 0. A **second** Ctrl+C, or SIGKILL, is the way to abandon
  cleanup, and abandoning it means losing whatever was still queued.
- **Before a host reboot or a Docker daemon restart, run `docker compose stop`
  yourself.** `depends_on` ordering is applied by the Compose client; when the
  daemon shuts containers down on its own it does not know about it, so
  Postgres can be gone while the app is still draining — the `TIMED OUT` case
  above. (Reasoned from how Compose works; not exercised here.)
- Do not use `docker compose kill` or `docker compose down -t 0` on a
  recording container unless losing the queue is the intent.

On the host it is the same path: Ctrl+C, or `kill <pid>` aimed at either the
`uv run` process or the Python one, drains and exits 0. Use plain `kill`
(SIGTERM) for a stop from another shell: uv always forwards SIGTERM to the
process it started, but
[by its own rule](https://docs.astral.sh/uv/concepts/projects/run/#signal-handling)
it forwards a SIGINT only if it is sent more than once or the child is in a
different process group. (Checked with uv 0.8.3 from a script with no
terminal: SIGTERM aimed at uv alone reached the child, and so did a single
SIGINT — which is more than the documented rule promises, so do not count on
it at a prompt.) Ctrl+C itself is unaffected: the terminal signals the whole
foreground process group, Python included.

## Dockerfile

[`Dockerfile`](../Dockerfile). `python:3.12-slim` base, `uv` installed by
copying its binary from the official `uv` image (no separate install step).
Dependency layer is cached independently of source (`uv sync --frozen
--no-dev --no-install-project` runs before `COPY src`), so an application
code change doesn't invalidate the dependency layer. `UV_NO_SYNC=1` is set
so that `uv run` *inside the running container* uses the environment baked
at build time rather than re-syncing (and potentially pulling dev
dependencies) at container start.

## `arb doctor`

The pre-flight check for all of the above — see
[`cli.md`](cli.md#arb-doctor) for what it checks and how to read its
output, including the millisecond-resolution `ntp clock` check that backs
[Host clock discipline](#host-clock-discipline) below. Works identically on
the host (`uv run arb doctor`) and inside the container (`docker compose exec
app arb doctor`); inside the container, the database check goes over the
Compose network and keys are read from the mounted `secrets/` volume. The
same checks run from the UI as the `jobs.doctor` job (`/control` → RUN
DOCTOR) when the server is up — which is exactly when the command is least
needed.

## Host clock discipline

One-way latency (venue timestamp → local receive) is a subtraction across two
machines' wall clocks, so it measures `true_transit + (local clock − venue
clock)`. Real Kalshi push delay is 5.5–12.5 ms
([`venue-notes.md`](venue-notes.md)); a local clock lagging by more than that
drives every reading negative. This has bitten twice (M16: median −24.5 ms;
M17: median −14.4 ms, SKEW −27 ms), so it is a runbook item, not a one-off.

Check it first — this is what `arb doctor`'s `ntp clock` check is for:

```sh
uv run arb doctor | grep 'ntp clock'
# [ok  ] ntp clock  offset -2.1 ms vs pool.ntp.org (rtt 24.0 ms)
```

**macOS.** "Set date and time automatically" being on is *not* sufficient:
`timed` slews lazily and tolerates tens of milliseconds of error, which is
exactly the range that matters here. Step-correct it:

```sh
sudo sntp -sS time.apple.com     # or the NTP_SERVER you configured
```

That is a one-shot correction — the clock drifts again after sleep/wake, so
re-run `arb doctor` when the latency panel looks wrong rather than assuming
the last sync still holds.

**The VM.** Discipline the *host*, not the container: a container shares the
host kernel's clock, so running an NTP client inside it needs `CAP_SYS_TIME`
and would only fight the host. On the VM host:

```sh
sudo timedatectl set-ntp true    # systemd-timesyncd
timedatectl status               # confirm "System clock synchronized: yes"
```

Prefer `chrony` over `systemd-timesyncd` where accuracy matters — it
disciplines continuously rather than stepping periodically.

**In the container**, `arb doctor`'s clock check needs outbound UDP 123. The
Compose network allows it by default, but a locked-down host firewall will
make the check report `warn: could not query ... (UDP 123 may be blocked)`.
That is a degraded check, not a failure — the container's clock is the host's
clock, so measuring it from the host is equivalent.

## Migrations

[`alembic.ini`](../alembic.ini) + [`migrations/`](../migrations/). Alembic's
async environment reads the database URL from `AppConfig` (environment /
`.env`) at runtime — **never** from a connection string baked into
`alembic.ini` — so no credential ever lives in a committed file. Migrations
are Postgres-specific (they use `sa.BigInteger`, `sa.JSON`, etc. targeting
Postgres DDL); the SQLAlchemy *models* themselves stay dialect-portable so
fast tests can run the same models against in-memory SQLite. See
[`data-model.md`](data-model.md#storage-schema) for the four tables
(`raw_messages`, `pairs`, `paper_trades`, `control_actions`) and their
migrations (`0001`–`0004`).

```sh
uv run alembic upgrade head                 # local, against DATABASE_URL in .env
docker compose exec app uv run alembic upgrade head   # in-container
```

### The migration is not optional any more

Through `0003`, an un-migrated database degraded gracefully: no `pairs`
table meant an empty review queue. `0004` adds `control_actions`, the audit
log the control plane writes to, and an action that **must** be audited
fails closed when that write fails. Concretely, on a database still at
`0003`:

- `jobs.propose`, `jobs.backfill` and a `jobs.replay` with `persist` — the
  actions that arm before they run — return **503** (`"was not performed:
  its audit row could not be written"`) and do not run. That is the design
  working: an action nobody can prove happened must not happen. It is also
  completely baffling if you don't know the table is missing.
- Every other action still works, and silently leaves no record:
  `arb_control_audit_failures_total` increments for each one. Alert on it.

`arb doctor` will **not** catch this. Its `migrations` check reports the
revision the database is at and calls any revision `ok`; it only `warn`s
when `alembic_version` is missing entirely. Read the number it prints and
compare it with `head`:

```sh
uv run arb doctor | grep migrations       # [ok  ] migrations  at revision 0004
uv run alembic heads                      # what head actually is
```

## The control plane

The UI is how this system is operated: recording, paper trading, the market
universe, the tracked pairs, pair proposal/backfill and replay are buttons on
`/control`, and the CLI is [down to three commands](cli.md#why-only-these-three).
Operationally that turns a browser tab into something that starts and stops a
process which will place real orders, so it comes with four things an operator
has to know about — an audit table, a read-only switch, a bind guard and a
job runner.

All of it lives behind one executor, `ControlPlane.execute`
([`src/arb/ui/control.py`](../src/arb/ui/control.py)), which no route can
bypass: the read-only refusal, the confirmation and the audit write are
properties of the executor, not of the thirteen actions or of the handlers
that call it. Its HTTP surface is `GET /api/control` (state and the action
list, with each action's consequence grade), `POST /api/control/<action>`,
`GET /api/control/jobs/<id>` and `GET /api/control/log`; the `control` and
`job` WebSocket frames push the same state to every open tab.

### The audit trail (`control_actions`)

**What it is for**: a recording gap, a universe change, a suspended paper
trader and a crash all look the same in the data afterwards. `raw_messages`
has no hole marker; a replay reads straight across a gap as if the venue
went quiet. The audit row is the only evidence that the gap was a decision.

One row per action *attempt*, written by the same executor that performs it:

| Column | Notes |
| --- | --- |
| `ts_ns`, `created_at` | `time.time_ns()` at the attempt; `created_at` is the server default |
| `run_id` | the run the UI was serving — joins straight to `raw_messages` |
| `action` | dotted name, e.g. `recording.stop`, `jobs.propose` |
| `params_json` | the *validated* parameters, not the raw body |
| `effect` | the exact human sentence the operator was SHOWN, stored verbatim. Not re-derived later: the point of an audit is what they agreed to, not what today's code would say |
| `actor` | always `ui` today — the HTTP route does not take one, so a `curl` and a click are indistinguishable here. `execute()` accepts an actor for the day there is an identity to put in it |
| `result` | `ok`, `armed`, `refused` or `error` — arming is recorded even when the action is never confirmed, and so is a read-only refusal |
| `error` | the failure text when `result` is not `ok` |

Read it from the terminal on `/control` (the AUDIT TRAIL card, newest 40),
over HTTP, or in SQL:

```sh
curl -s '127.0.0.1:8080/api/control/log?limit=20' | jq -r \
  '.actions[] | [.result, .action, .effect] | @tsv'

docker compose exec postgres psql -U arb -d arb -c "
  SELECT to_timestamp(ts_ns/1e9) AT TIME ZONE 'UTC' AS ts_utc,
         action, result, actor, effect
  FROM control_actions ORDER BY ts_ns DESC LIMIT 20;"
```

Rows are never updated or deleted by the application. `GET /api/control/log`
caps `limit` at 500 and orders newest first in SQL.

### Jobs

Long actions become jobs: `jobs.doctor`, `jobs.propose`, `jobs.backfill`,
`jobs.replay`, cancellable with `jobs.cancel`. Three operational properties:

- **Single-flight per group.** `propose` and `backfill` share the `pairs`
  group because they fetch the same two universes and write the same table;
  a second one gets a 409 instead of a job record that was always going to
  fail.
- **Nothing blocks ingest.** `doctor` is cheap and runs in-process;
  `propose`/`backfill` are awaited in the server (their blocking scorer goes
  to a worker thread inside `arb.pairs.run`); `replay` runs as a **subprocess**
  (`python -m arb.cli replay ...`) because its per-row loop has no `await`
  and would hold the event loop past the 10 s WS ping timeout, killing the
  Kalshi socket. Cancelling sends `SIGTERM`, then `SIGKILL` after 5 s.
- **Output survives the browser.** The last 32 jobs are kept with up to 500
  output lines each, readable at `GET /api/control/jobs/<id>` after they
  finish. Dropped overflow lines are counted
  (`arb_control_job_output_dropped_total`), so an incomplete log says so.

### Read-only mode

```sh
uv run arb ui --read-only          # or UI_READ_ONLY=1
```

Serves every view and refuses every mutating control with a 403 — plus an
audit row with `result='refused'`, because an attempted control is worth
recording too. `/control` renders its buttons disabled from the server's own
`read_only` flag, so the state cannot drift from what the server will do.
Use it for a tunnel you are sharing with someone who should watch and not
touch.

Two limits to be honest about: it is a **capability** switch, not an
identity — anyone who reaches the port has the same rights, and the flag can
only be set at startup (there is no control that turns it off, deliberately).
And it covers control-plane actions only: pair decisions on `/pairs` post to
`/api/pairs/...`, which predates the control plane and is still writable in
read-only mode.

### Binding beyond loopback

`arb ui` refuses a non-loopback bind host at startup unless
`ARB_ALLOW_REMOTE_BIND=1` is set. The UI has no login, no session and no user
table, so a reachable port *is* the authorization. When the hatch is used the
server logs a warning and the UI shows the bind in its status, rather than
pretending the posture is unchanged.

On top of that, an ASGI guard
([`src/arb/ui/security.py`](../src/arb/ui/security.py)) runs over both `http`
and `websocket` scopes:

- `Host` must be loopback or in `UI_ALLOWED_HOSTS`. This is the DNS-rebinding
  defence: a rebound request still carries the attacker's hostname.
- Any non-`GET`/`HEAD` request, and every WebSocket handshake, is refused if
  it carries `Sec-Fetch-Site: cross-site` or an `Origin` that is neither
  loopback nor in `UI_ALLOWED_ORIGINS`. A **missing** `Origin` is allowed on
  purpose: that is `curl` or a script already running on this machine, which
  could open the socket directly anyway.

Refusals are 403 (HTTP) or a 1008 close (WebSocket), logged at WARNING and
counted in `arb_ui_requests_rejected_total{scope,reason}`. There are no CSRF
tokens by design — with no session to bind one to, a token is an Origin check
with extra steps. If this ever needs to be exposed for real, put an
authenticating reverse proxy in front of it; do not loosen these checks.

Both allowlists are comma-separated strings (`UI_ALLOWED_HOSTS=arb.internal`),
not JSON lists, because pydantic-settings would try to JSON-decode a list
field.

### What to watch

| Signal | Means |
| --- | --- |
| `arb_control_audit_failures_total` | an action ran (or was refused) without a record. Usually the missing migration; always worth an alert |
| `arb_control_actions_total{result="read_only"}` | someone is pressing buttons on a read-only server |
| `arb_control_actions_total{result="error"}` | actions failing; pair with the `error` column in `control_actions` |
| `arb_ui_requests_rejected_total` | the origin/host guard refused something — a misconfigured reverse proxy, or an actual cross-site attempt |
| `arb_control_jobs_total{result="error"}` | a propose/backfill/replay job died; its output is still readable |

## Monitoring: Prometheus

Scrape config: [`infra/prometheus.yml`](../infra/prometheus.yml), 5-second
interval, two jobs: `prometheus` itself and `arb-ui` (`app:8080`, the UI's
own `GET /metrics`). There used to be an `arb` job on `app:9000` for the
standalone recorder's metrics server; both the command and the job are gone,
so if you are looking at an old dashboard or alert that references target
`app:9000`, it will never come back up.

Every metric name in the system is declared in one place,
[`src/arb/metrics.py`](../src/arb/metrics.py) — a new failure mode always
gets a new metric there, per `CLAUDE.md`. Current metrics:

| Metric | Labels | What it counts |
| --- | --- | --- |
| `arb_book_invalidations_total` | `venue`, `reason` | `Book` transitions to invalid |
| `arb_parse_errors_total` | `venue`, `stream` | malformed venue payloads rejected by an adapter |
| `arb_ws_connects_total` | `venue`, `stream` | successful WebSocket (re)connections |
| `arb_ws_connect_failures_total` | `venue`, `stream` | connection attempts that failed before establishment |
| `arb_ws_disconnects_total` | `venue`, `stream`, `reason` | disconnects after establishment (`"stall"` or an exception class name) |
| `arb_supervisor_restarts_total` | `task` | any `supervise()`-wrapped task restarting |
| `arb_rest_polls_total` | `venue`, `status` | REST poll attempts (status code, or `"error"`) |
| `arb_rest_rate_limited_total` | `venue` | REST polls answered 429 |
| `arb_recorder_enqueued_total` | `venue` | raw messages accepted onto the recorder queue |
| `arb_recorder_dropped_total` | `venue` | raw messages dropped, queue full |
| `arb_recorder_written_total` | — | raw messages durably written |
| `arb_recorder_write_failures_total` | — | sink write attempts that failed (retried in place) |
| `arb_recorder_queue_depth` | — | current queue depth (gauge) |
| `arb_recorder_drain_timeouts_total` | — | shutdown drains that timed out with messages still queued. **Not scrapeable from `arb ui`** — see below |
| `arb_seq_gaps_total` | `venue` | subscription-level sequence gaps detected |
| `arb_ui_ws_clients` | — | connected terminal-UI WS clients (gauge) |
| `arb_ui_ws_clients_dropped_total` | — | UI WS clients dropped for a full send queue |
| `arb_ws_one_way_latency_ms` | `venue` | venue-stamped one-way push delay (histogram; buckets span negative — see below) |
| `arb_ws_one_way_latency_negative_total` | `venue` | one-way samples that came out negative, i.e. proof of clock skew |
| `arb_ws_rtt_ms` | `venue`, `stream` | WS keepalive round-trip time (gauge, skew-immune) |
| `arb_clock_skew_ms` | `venue` | estimated local-vs-venue clock offset (gauge; negative = local behind) |
| `arb_control_actions_total` | `action`, `result` | control actions by outcome: `ok`, `armed`, `refused`, `read_only`, `confirm_invalid`, `unknown`, `error` |
| `arb_control_audit_failures_total` | `action` | audit writes that failed — for an arming action the action was also refused, for the rest it happened unrecorded |
| `arb_control_jobs_total` | `job`, `result` | background jobs by outcome (`ok` / `error` / `cancelled`) |
| `arb_control_job_output_dropped_total` | `job` | output lines dropped from a job's bounded buffer; nonzero means its log is incomplete |
| `arb_ui_requests_rejected_total` | `scope`, `reason` | requests refused by the origin/host guard (`reason` is `host`, `origin` or `sec_fetch_site`) |

The last one is incremented from
[`src/arb/ui/security.py`](../src/arb/ui/security.py) and declared in
`metrics.py` like every other metric.

`arb_recorder_drain_timeouts_total` is declared because every failure mode
gets a metric, and it should not be oversold: `arb ui` runs its drain *after*
the HTTP server that serves `/metrics` has stopped, so the value it increments
is never scraped from that process, and the process is gone a moment later. It
will read 0 on every dashboard. The record of an abandoned queue is the WARNING
in the container log and the `TIMED OUT` in the last line
([above](#stopping-and-what-a-stop-flushes)); the signal Prometheus *can* see
is the one before the stop — `arb_recorder_write_failures_total` climbing with
`arb_recorder_queue_depth`. The counter would earn its keep in a caller that
outlives its drain; there is none today (the pairs jobs borrow the server's
recorder and drain nothing), so tests are the only place it is seen to move.

The clock group exists because a local clock running behind the venue's makes
every one-way latency reading negative, and until they were added that failure
mode was invisible outside the terminal UI — on the VM, where nobody is
watching the terminal, nothing caught it. Three things about them:

- `arb_ws_one_way_latency_ms`'s buckets deliberately extend below zero. A
  one-way delay cannot physically be negative, so **any count at or below the
  `le="0"` bucket is proof of a clock offset**, not of fast networking. The
  cost of negative buckets is that `prometheus_client` suppresses the `_sum`
  series, so bucket counts are the histogram's only output — there is no
  `arb_ws_one_way_latency_ms_sum` to average with, and `histogram_quantile`
  interpolates unreliably once mass piles up in the lowest bucket. Alert on
  the skew gauge and the negative counter; read the quantiles for shape only.
- The gauges are set to `NaN`, not left at their last value, whenever the
  quantity was not measured (a reconnecting WebSocket reports no RTT). A
  frozen gauge reading `-27 ms` through an outage would look like a live
  measurement of that outage.
- Both are populated from `ServerState.stats_payload`, the same arithmetic
  that feeds the UI's latency panel, so Grafana and the terminal can never
  disagree about the skew estimate. They exist only while `arb ui` is
  running, which is now the only thing that ingests anyway.

## Dashboards: Grafana

Provisioned automatically from
[`infra/grafana/provisioning/`](../infra/grafana/provisioning/) —
`allowUiUpdates: false`, so the JSON files under `dashboards/` are the
source of truth; edit them, don't edit in the Grafana UI (changes there
would be lost on the next provisioning pass). One dashboard currently
exists: **"ARB — Data Plane"** (`dashboards/arb.json`, 36 panels) covering
recorder throughput, book invalidations, WS reliability, REST poll health,
UI client counts, and a **"Clock & latency"** row (skew gauge, negative-sample
count, one-way quantiles, skew vs. keepalive RTT). Datasource: `Prometheus` at `http://prometheus:9090`
(the in-network service name), provisioned as the default datasource.

Access at `http://127.0.0.1:3000`, `admin`/`admin` (change this before
anything resembling a production deployment — it's the Grafana image's
documented default and is not treated as a secret here on purpose, since
the whole stack is localhost/SSH-tunnel-only).

## Deployment posture (VM)

The VM runs the same Compose stack; every port stays bound to `127.0.0.1`
on the VM itself, and is reached from a workstation over an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090
<vm>`), never exposed on a public interface. `app`'s `restart:
unless-stopped` means the recording terminal survives a VM reboot without
manual intervention.

The tunnel is the authentication. Anyone you forward that port to has the
full control plane — recording, the universe, paper limits, the pair jobs —
so forward `8080` to someone who should only watch by running the container
[read-only](#read-only-mode), and remember that the container's controls
outlive your SSH session: recording stopped from a browser stays stopped
after you disconnect. The audit trail is how the next person finds out.

## Secrets hygiene

`.env`, `secrets/`, and any key file are gitignored — see `.gitignore`.
`.env` holds only **paths** to key files and non-secret config (see
[`.env.example`](../.env.example)); actual key material lives under
`secrets/` and is never logged, printed, or committed. `arb doctor`'s key
check confirms a key *file exists at the configured path*, never reads or
echoes its contents. `docker-compose.yml` mounts `secrets/` read-only.
