# Documentation map

This directory documents what the system is and how it works. It is split
into two kinds of file:

- **Reference** — verified facts and recorded decisions, updated as the
  project evolves. These are the source of truth when code and prose
  disagree with memory:
  - [`venue-notes.md`](venue-notes.md) — every verified API fact, cited
    against the vendor docs, with the date it was checked. When a design
    prompt and the docs disagree, the docs win and the conflict is noted
    here.
  - [`decisions.md`](decisions.md) — design choices and the reasoning
    behind them, newest first, organized by milestone.

- **Explainers** — this set of files. They walk through *how* the code
  works, part by part, assuming no prior context beyond
  [`../README.md`](../README.md) and [`../CLAUDE.md`](../CLAUDE.md).

  | File | Covers |
  | --- | --- |
  | [`architecture.md`](architecture.md) | The system end to end: process layout, data flow from a venue frame to a paper trade, the shared interfaces every venue implements. Read this first. |
  | [`data-model.md`](data-model.md) | Fixed-point `Ticks`/`Qty` types, the `Book`/`BookManager` order-book model and its validity rules, the Postgres schema. |
  | [`venues/kalshi.md`](venues/kalshi.md) | Kalshi auth, discovery, WebSocket protocol, REST parsers, the adapter's sequence-gap handling. |
  | [`venues/polymarket-us.md`](venues/polymarket-us.md) | Polymarket US auth, discovery, the REST poller (rate-limit behavior), REST parsers, the adapter. |
  | [`pairs.md`](pairs.md) | The cross-venue pair matcher: text normalization, scoring, blocking, human review workflow, storage. |
  | [`engine.md`](engine.md) | Fee models, the depth-aware edge walk, the ARB monitor, paper trading, and the replay engine that ties them together deterministically. |
  | [`ui.md`](ui.md) | The terminal UI: FastAPI backend, `ServerState`, the WebSocket wire protocol, and the static frontend. |
  | [`cli.md`](cli.md) | Every `arb` subcommand and flag, with examples, both local and in Docker. |
  | [`ops.md`](ops.md) | The Docker Compose stack, Prometheus/Grafana, `arb doctor`, Alembic migrations, deployment posture. |
  | [`testing.md`](testing.md) | Test strategy, fixture-driven parser tests, hypothesis usage, what's covered where. |

Also see [`../PROGRESS.md`](../PROGRESS.md) for the current milestone, what
works today, and open questions.

## How to keep these current

Whenever a change lands that these explainers describe, update the relevant
file in the same commit — the goal is that `docs/` never drifts from
`src/arb/`. Verified external facts (endpoints, wire formats, rate limits,
fee formulas) belong in `venue-notes.md`, not scattered across the
explainers; the explainers should link to it rather than repeat it.
