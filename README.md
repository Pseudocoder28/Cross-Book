# Cross-Book — Kalshi × Polymarket US cross-venue arbitrage

Cross-Book (`arb` on the command line) detects — and, from a later milestone
on, trades — price gaps between equivalent binary markets listed on both
**Kalshi** and **Polymarket US** (docs.polymarket.us; the international
Polymarket is out of scope).

Everything built so far is **read-only measurement and simulation**: venue
adapters, a normalized order book, a raw-message recorder, a cross-venue
pair matcher, fee models, depth-aware edge measurement, paper execution, and
a control plane that drives all of it from the browser. No code that places,
amends or cancels a real order exists yet — that's a later milestone.

See [`PROGRESS.md`](PROGRESS.md) for the current milestone and exactly what
works today. The build plan, hard rules and conventions that govern every
change live in [`CLAUDE.md`](CLAUDE.md); read it before adding code.

## What's actually running today

- Live order books for both venues (Kalshi over an authenticated WebSocket,
  Polymarket US over polled public REST), normalized into one YES-side book
  model with NO derived by complement.
- Every raw inbound message recorded to Postgres before parsing, so any run
  can be replayed byte-for-byte.
- A cross-venue pair matcher that proposes candidate equivalent markets for
  human review, with a `/pairs` screen to confirm or reject them.
- A depth-aware, fee-exact edge calculation over confirmed pairs, live on the
  `/arb` screen.
- A paper trader that simulates fills against measured edges under risk
  limits — no order is ever placed, amended or cancelled.
- A black/amber terminal UI (`arb ui`) serving all of the above at
  `http://127.0.0.1:8080`, plus a Grafana dashboard and Prometheus metrics.
- A **control plane** on the `/control` screen: the recorder, paper trading
  and its risk limits, both venues' market universes, the tracked pairs and
  the long jobs (doctor, pair proposal, slug backfill, replay) are all
  runtime controls, not command-line flags. Every action goes through one
  server-side executor that owns the read-only refusal, the arm-then-confirm
  handshake and an audit row in Postgres recording the exact sentence you
  were shown.

The UI is a keyboard-driven multi-page app: one URL per screen — `/`
(markets, depth ladder, tape, latency), `/arb` (cross-venue edge), `/pairs`
(pair review), `/paper` (simulated ledger), `/system` (diagnostics),
`/control` (every runtime toggle and job), `/help` (keys, commands, glossary)
and `/market/<id>` (one market's rules and live book) — all served by the one
`arb ui` process on the one port, so every screen deep-links, reloads and
back-buttons like a normal web page.

`CTRL+1`…`CTRL+7` jump between the nav pages on macOS (`ALT` elsewhere), a bare
letter typed anywhere always goes to the `ARB>` command line, and a page's own
single-letter keys fire only once you have focused its row list with `↑`/`↓`.
`/help` documents the rest of it in the app. See
[`docs/ui.md`](docs/ui.md) for how each screen reads and
[`docs/cli.md`](docs/cli.md#arb-ui) for the routes, keys and flags.

## Documentation

Start with [`docs/README.md`](docs/README.md) for the full documentation
map. Highlights:

- [`docs/architecture.md`](docs/architecture.md) — how the pieces fit
  together, end to end
- [`docs/data-model.md`](docs/data-model.md) — ticks/Qty fixed-point types,
  the `Book` model, storage schema
- [`docs/venues/kalshi.md`](docs/venues/kalshi.md) and
  [`docs/venues/polymarket-us.md`](docs/venues/polymarket-us.md) — per-venue
  integration details
- [`docs/pairs.md`](docs/pairs.md) — the cross-venue pair matcher
- [`docs/engine.md`](docs/engine.md) — fees, edge math, the ARB monitor,
  paper trading, replay
- [`docs/ui.md`](docs/ui.md) — the terminal UI: its pages, backend and
  wire protocol
- [`docs/cli.md`](docs/cli.md) — every `arb` subcommand, with examples
- [`docs/ops.md`](docs/ops.md) — Docker Compose stack, Prometheus/Grafana,
  `arb doctor`, deployment
- [`docs/testing.md`](docs/testing.md) — test strategy and fixtures
- [`docs/venue-notes.md`](docs/venue-notes.md) — verified API facts, with
  doc citations (nothing here is guessed)
- [`docs/decisions.md`](docs/decisions.md) — design choices and why, newest
  first

## Getting started

```sh
cp .env.example .env      # fill in; .env is gitignored
uv sync                   # install dependencies (dev group included)
uv run pytest
uv run ruff check .
uv run pyright
```

`arb doctor` checks environment, keys, clock skew (including a
millisecond-resolution SNTP check — a lagging host clock makes every latency
reading negative), database and venue reachability before you run anything
else:

```sh
uv run arb doctor
```

Bring up the local infrastructure (Postgres + pgvector, Prometheus, Grafana):

```sh
docker compose up -d
uv run alembic upgrade head
```

`alembic upgrade head` is not optional: the control plane audits every action
to the `control_actions` table, and the actions that write many rows refuse to
run if that row cannot be written.

The app container binds `0.0.0.0` *inside* the compose network (the published
port stays `127.0.0.1`-only), and a non-loopback bind is refused at startup
unless `ARB_ALLOW_REMOTE_BIND=1` is set for that container — the UI has no
authentication and its controls drive a trading process, so exposing it is an
explicit opt-in. Run it locally and you never meet this.

Then open the terminal UI — which is the whole interface. It records while it
runs, and everything else is a control inside it:

```sh
uv run arb ui --top 8 --pairs-top 10   # terminal UI at :8080 — start on /, then /help
```

Go to `/control` (`CTRL+6` on macOS, `ALT+6` elsewhere) to turn recording on
and off, suspend or resume paper trading and change its risk limits, change
either venue's market universe, reload the tracked pairs, or run a job —
doctor, pair proposal, slug backfill, replay. Nothing there needs a restart.
`arb ui --read-only` serves every screen and refuses every control, which is
what to use when showing the terminal to someone.

There are only three CLI commands, and the other two exist for the cases a
button cannot cover: `arb doctor` runs when the UI *won't* start, and
`arb replay` is also the worker the REPLAY control spawns as a subprocess.
See [`docs/cli.md`](docs/cli.md) for every flag.

## Layout

```
src/arb/              shared package: types, Book, BookManager, interfaces,
                       metrics, config, recorder, CLI (ui/doctor/replay),
                       fees, edge, arb monitor, paper trader, replay engine
src/arb/venues/        venue-specific code only (kalshi/, polymarket_us/) —
                       auth, discovery, REST/WS parsing, adapters
src/arb/pairs/          cross-venue pair matcher, storage, the propose and
                       backfill jobs the UI runs
src/arb/storage/        SQLAlchemy models + engine/session helpers
src/arb/ui/             FastAPI backend, the control plane (control.py) and
                       the host/origin guard (security.py), plus the static
                       frontend it serves (ES modules, one per page; no build
                       step, no deps)
migrations/             Alembic migrations (Postgres schema)
infra/                  Prometheus + Grafana provisioning
tests/                  pytest suite
tests/fixtures/         real captured payloads for parser tests
docs/                   architecture, venue notes and design decisions
```

## Hard rules (summary)

The full list is in [`CLAUDE.md`](CLAUDE.md); the ones that matter most day
to day:

- No order placement/amend/cancel code until a later prompt explicitly asks
  for it — everything today is read-only measurement and simulation.
- Prices are integer ticks of $0.0001; quantities are integer units of
  0.0001 contracts. No floats for prices, quantities or fees in book state
  or storage.
- Every venue fact in the docs is verified against the vendor's own
  documentation and cited in `docs/venue-notes.md` — nothing is guessed.
- Secrets are never logged, printed or committed; `.env`, `secrets/` and key
  files are gitignored.
- Postgres, Prometheus and Grafana bind to `127.0.0.1` only.
