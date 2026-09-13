# Decisions

Design choices and why. Newest first.

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
