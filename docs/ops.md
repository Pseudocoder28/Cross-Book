# Operations

Covers the local/VM infrastructure stack, deployment posture, migrations,
and monitoring. See [`cli.md`](cli.md) for `arb doctor` and every other
command referenced here.

## Security posture: localhost-only

Every stateful service — Postgres, Prometheus, Grafana, and the app's UI
port — binds to `127.0.0.1` on the host, never `0.0.0.0`, per `CLAUDE.md`.
On a VM these are reached over an SSH tunnel, never exposed publicly. Inside
the Compose network (container-to-container), services do bind `0.0.0.0` on
their *internal* interface so other containers can reach them — the
`docker-compose.yml` comments call this out explicitly at each override —
but the **host port mapping** for every service stays `127.0.0.1:<port>:<port>`.

## Docker Compose stack

[`docker-compose.yml`](../docker-compose.yml). Four services:

| Service | Image | Host port | Purpose |
| --- | --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | primary datastore (pgvector included for future use; not yet used) |
| `prometheus` | `prom/prometheus:v2.53.0` | `127.0.0.1:9090` | scrapes `arb`'s `/metrics` |
| `grafana` | `grafana/grafana:11.1.0` | `127.0.0.1:3000` | dashboards over the Prometheus datasource (`admin`/`admin`) |
| `app` | built from `Dockerfile` | `127.0.0.1:8080` | runs `arb ui --top 20 --host 0.0.0.0`, i.e. the terminal UI **and** the recorder in one process |

Images are pinned (not `:latest`) so a `docker compose pull` doesn't
silently change behavior underneath the stack. `postgres` has a real
healthcheck (`pg_isready`); `app` waits on `postgres: condition:
service_healthy` before starting, so the app never races a not-yet-ready
database.

`app`'s environment overrides three things for the in-network posture:
`DATABASE_URL` points at the `postgres` service name (not `127.0.0.1`,
since it's a different container), `METRICS_HOST=0.0.0.0` (so Prometheus,
in the same network, can scrape `app:9000`... — see the note below), and
`UI_HOST=0.0.0.0` (so the UI is reachable from the host's own mapped port).
`secrets/` is bind-mounted read-only into the container at `/app/secrets`.

**Note on the metrics port**: `arb ui` serves `/metrics` from its own
FastAPI app on the UI port (8080), not a separate `:9000` server — that
separate server is only started by `arb record`. Since Compose's `command`
runs `arb ui`, Prometheus's `arb` job target (`app:9000`) stays down by
design while the stack runs `arb ui`; the `arb-ui` job (`app:8080`) is the
one that's actually live. If the Compose command is ever changed to
`arb record` instead, the reverse becomes true. See
[`infra/prometheus.yml`](../infra/prometheus.yml).

### Bringing the stack up

```sh
docker compose up -d
uv run alembic upgrade head       # from the host; DATABASE_URL from .env points at 127.0.0.1:5432
docker compose exec app arb doctor
```

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
Compose network and keys are read from the mounted `secrets/` volume.

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
[`data-model.md`](data-model.md#storage-schema) for the three tables
(`raw_messages`, `pairs`, `paper_trades`) and their migrations (`0001`,
`0002`, `0003`).

```sh
uv run alembic upgrade head                 # local, against DATABASE_URL in .env
docker compose exec app uv run alembic upgrade head   # in-container
```

## Monitoring: Prometheus

Scrape config: [`infra/prometheus.yml`](../infra/prometheus.yml), 5-second
interval. Every metric name in the system is declared in one place,
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
| `arb_seq_gaps_total` | `venue` | subscription-level sequence gaps detected |
| `arb_ui_ws_clients` | — | connected terminal-UI WS clients (gauge) |
| `arb_ui_ws_clients_dropped_total` | — | UI WS clients dropped for a full send queue |
| `arb_ws_one_way_latency_ms` | `venue` | venue-stamped one-way push delay (histogram; buckets span negative — see below) |
| `arb_ws_one_way_latency_negative_total` | `venue` | one-way samples that came out negative, i.e. proof of clock skew |
| `arb_ws_rtt_ms` | `venue`, `stream` | WS keepalive round-trip time (gauge, skew-immune) |
| `arb_clock_skew_ms` | `venue` | estimated local-vs-venue clock offset (gauge; negative = local behind) |

The last four exist because a local clock running behind the venue's makes
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
  disagree about the skew estimate. They are therefore exported by `arb ui`,
  not by a bare `arb record` run.

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

## Secrets hygiene

`.env`, `secrets/`, and any key file are gitignored — see `.gitignore`.
`.env` holds only **paths** to key files and non-secret config (see
[`.env.example`](../.env.example)); actual key material lives under
`secrets/` and is never logged, printed, or committed. `arb doctor`'s key
check confirms a key *file exists at the configured path*, never reads or
echoes its contents. `docker-compose.yml` mounts `secrets/` read-only.
