# Data model

This covers the fixed-point number types, the raw-message envelope, the
normalized order book and its validity state machine, and the Postgres
schema. Everything here is venue-agnostic; venue-specific parsing is covered
in [`venues/kalshi.md`](venues/kalshi.md) and
[`venues/polymarket-us.md`](venues/polymarket-us.md).

## Fixed-point numbers: `Ticks` and `Qty`

Defined in [`src/arb/types.py`](../src/arb/types.py).

```python
type Ticks = int   # 1 tick = $0.0001, so 1¢ = 100, $0.555 = 5550
type Qty   = int    # 1 unit = 0.0001 contracts
```

Both are plain `int` type aliases, not wrapper classes — there is no runtime
overhead, and every arithmetic operator just works. The discipline is
entirely in *how values enter the system*:

- `ticks_from_dollars(text)` / `dollars_from_ticks(ticks)` convert exactly
  between a decimal dollar string (`"0.4200"`) and an integer tick count.
  Parsing rejects signs, exponents, non-ASCII digits, and any string with
  more than 4 fractional digits (`raise ValueError`) — a venue payload with
  finer precision than a tick is a bug to catch, not silently round away.
- `qty_from_contracts(text)` / `contracts_from_qty(qty)` do the same for
  quantities, at the same 4-decimal scale.
- `qty_delta_from_contracts(text)` is the signed variant, for delta fields
  only (`"-54.00"` → `-540000`); resting quantities themselves are never
  negative.

**Why `Qty` is fixed-point and not a plain integer contract count:** the
project's original assumption (integer contracts) didn't survive contact
with real Kalshi data — live books contain fractional counts like
`"15.17"` and `"4903190.79"` (`FixedPointCount`, 0.01-contract granularity
per Kalshi's docs). See `docs/venue-notes.md` and decision M4 in
`docs/decisions.md`.

Two helpers complete the price side:

- `is_valid_price(price)` — a quotable price is strictly inside `(0, 10000)`
  ticks; 0 and 10000 are settlement values, not something a resting order
  can be priced at.
- `complement(price)` — `10000 - price`, the NO price for a given YES price
  (or vice versa). This is the single formula the whole system uses to
  derive one side of a market from the other; see "Complement pricing"
  below.

Prices, quantities and fees are integers everywhere in book state, storage
and the fee/edge engine — never floats. Floats appear only in analytics
paths that are explicitly not part of book state or money math (e.g.
24h-volume ranking for discovery, which is display-only).

## `RawMessage`: the universal envelope

```python
@dataclass(frozen=True, slots=True)
class RawMessage:
    venue: str
    stream: str              # e.g. "ws", "rest:orderbook", "rest:book"
    payload: bytes            # exact bytes received, unparsed
    recv_ts_ns: int            # time.time_ns() at receipt (UTC wall clock)
    recv_mono_ns: int          # time.monotonic_ns() at receipt
    run_id: str
    ingest_seq: int             # per-run, monotonic across all sources
```

Every inbound message — WebSocket frame or REST response — is wrapped in
one of these the instant it's received, before any parsing. It's what the
recorder writes to Postgres (`payload` verbatim as `BYTEA`), what venue
adapters take as input, and what `arb replay` reconstructs from stored rows.
`recv_mono_ns` is only ever compared within one `run_id` — monotonic clocks
have no cross-process meaning — which is exactly how replay uses it: as the
clock fed to `Book.apply_snapshot`/`apply_level` so staleness and ordering
reconstruct deterministically.

`RawMessage` is a frozen, slotted dataclass rather than a Pydantic model on
purpose — it's the hot-path envelope wrapping *unparsed* bytes, so there is
nothing to validate at this layer. Pydantic v2 is the rule for config and
for parsed venue message models (see `KalshiMarket`, `PolymarketUSMarket`,
etc.).

## Complement pricing: one book, two sides

Both venues expose a market as effectively one side:

- **Kalshi** publishes YES bids and NO bids (no asks, by design — see
  `docs/venue-notes.md`). A NO bid at price `x` is economically a YES ask at
  `10000 - x`: someone willing to pay `x` for NO is equivalent to someone
  willing to sell YES at `1 - x`.
- **Polymarket US** has exactly one instrument per market (the YES outcome);
  buying NO means shorting that instrument, i.e. it's not a separate
  order book at all.

So the system normalizes to **one book per market, YES-only**: `Book` holds
YES bid and ask ladders directly (best price first), and NO views
(`no_bids()`, `no_asks()`) are *derived* on read by mapping every YES level
through `complement()`. Kalshi's adapter folds NO-side WS/REST data into the
YES-ask ladder by complement before it ever reaches `Book` — so from
`Book`'s perspective (and everything downstream: `BookManager`, `ArbMonitor`,
the UI), there is no venue-specific branching on which side of the market
a piece of data described.

## `Book`: validity and the update state machine

[`src/arb/book.py`](../src/arb/book.py). One `Book` instance per market,
holding two dicts (`price -> qty`) for bids and asks. No I/O, no clock
reads — callers pass in a `mono_ns` timestamp from the message envelope,
which is what keeps this class exhaustively unit-testable (including with
`hypothesis`).

### Update primitives

- **`apply_snapshot(BookSnapshot, mono_ns)`** — replaces all state. Rejects
  (invalidates as `BAD_LEVEL`) any level with non-positive quantity or a
  price outside `(0, 10000)`. Clears any prior invalidation, then re-checks
  for crossing.
- **`apply_level(BookLevelUpdate, mono_ns)`** — one incremental change to a
  single price level, in `SET` mode (qty is the new absolute value) or
  `DELTA` mode (qty is added, may be negative). Ignored entirely (state
  untouched, and the caller is expected to be fetching a fresh snapshot)
  while the book `needs_resync`. If a level's new quantity settles to 0, the
  level is removed; if it goes negative, that's `BAD_LEVEL`.
- **`mark_invalid(reason)`** — lets a caller (an adapter's gap detector)
  invalidate a book the `Book` itself never saw anything wrong with — used
  when Kalshi's subscription-level sequence gap taints every market in the
  subscription at once (see [`venues/kalshi.md`](venues/kalshi.md)).

### Validity rules (from `CLAUDE.md`, enforced here)

A book is valid only while **all** of:

1. it has a snapshot and no sequence gap since
2. it is not crossed (`max(bid) < min(ask)`; a *locked* book — best bid ==
   best ask — counts as crossed too, because on both venues a matching
   engine would have executed a bid and ask at the same price, so seeing
   both resting means the local state is wrong)
3. every level has positive quantity and a price strictly inside `(0,
   10000)`
4. it has updated within the staleness limit

`InvalidReason` enumerates the five ways a book can be invalid:
`NO_SNAPSHOT` (initial state, before the first snapshot), `SEQ_GAP`,
`CROSSED`, `BAD_LEVEL`, and `STALE`.

**Structural invalidation is sticky; staleness is not.** The first four
reasons (`_STRUCTURAL` in the code) set `needs_resync = True`: every
subsequent `apply_level` call is a no-op until a fresh `apply_snapshot`
arrives. `STALE`, by contrast, is computed only at query time
(`status(now_mono_ns=...)`) by comparing against `staleness_limit_ns` — it
is not stored, and clears itself the moment a contiguous update lands. The
reasoning: a quiet-but-gapless book (nothing has traded recently) is merely
untradeable right now, not *wrong* — there's no reason to throw away good
state over it, unlike a genuine structural fault.

### Sequence policy

Contiguity (`seq == last_seq + 1`) is enforced only when both the book's
current sequence and the incoming update's sequence are present. A book
with no sequence yet simply adopts the first one it sees. Updates that
carry no sequence number at all (`seq=None`) are accepted without advancing
the book's sequence — this is how both venues actually work in practice:
Kalshi's `Book`-level sequence tracking is bypassed entirely because
sequencing there is subscription-scoped, not market-scoped (see
[`venues/kalshi.md`](venues/kalshi.md)); Polymarket US carries no sequence
numbers on its market data at all.

### Read views

- `bids()` / `asks()` — full ladders, best price first (bids descending,
  asks ascending).
- `best_bid()` / `best_ask()` — top of book, or `None` if that side is
  empty.
- `no_bids()` / `no_asks()` — the complement views described above,
  computed from `asks()`/`bids()` respectively on every call (no caching —
  these ladders are small, at most a few hundred levels, prices bounded
  1..9999, so this is fast enough that profiling would have to justify
  anything fancier).

## `BookManager`: the fan-out layer

[`src/arb/books.py`](../src/arb/books.py). Owns every `Book` (created
lazily on first event for a market id), applies a batch of `BookEvent`s,
and returns the set of market ids that changed — this is the signal
consumers (the UI's flush loop, `ArbMonitor.affected()`) use to know what to
re-publish or re-quote without re-scanning every book on every tick.

`apply(events, mono_ns)` handles three event kinds:

- `BookSnapshot` / `BookLevelUpdate` → routed to the corresponding `Book`
  method; a metric (`arb_book_invalidations_total{venue,reason}`) fires
  whenever the result is invalid. Note: this is where the *manager*, not
  `Book` itself, touches metrics — `Book` stays pure.
- `ResyncRequired(venue, market_ids)` → marks every affected book (all of
  them, if `market_ids is None`) invalid with `SEQ_GAP`, and counts the
  invalidation the same way. This is how Kalshi's per-subscription gap
  detection (in the adapter, not in `Book`) propagates to every market that
  shares the tainted subscription.

Per-venue staleness (`set_venue_staleness(venue, limit_ns)`) exists because
a polled venue's books are, by construction, only as fresh as the last poll
cycle — applying the streaming default staleness limit would flag every
Polymarket US book stale between polls. `arb ui` and `arb replay` compute a
generous per-poll-cycle budget (see [`engine.md`](engine.md) and
[`ui.md`](ui.md)).

`level_deltas(before, after)` is a free function, not a `Book` method: given
a book's current state and an incoming full snapshot, it diffs the two
ladders and returns the list of `BookLevelUpdate`s (in `DELTA` mode) that
would take one to the other. This exists purely so a poll-based venue
(which only ever delivers full snapshots) can still drive a "tape" of
discrete level changes identical in shape to what a streaming venue emits —
downstream consumers (the UI's delta broadcast) see one event shape
regardless of venue.

`venue_of(market_id)` — every market id in the system is
`"<venue>:<native-id>"` (e.g. `kalshi:KXPRESPERSON-28-TGAB`,
`polymarket_us:tec-mlb-nlchamp-2026-09-27-nym`); this just splits on the
first `:`. Native identifiers are used untouched in API calls to that venue;
the qualified form is what's globally unique across the book map,
`ArbMonitor`, and pair storage.

## Storage schema

All defined in [`src/arb/storage/models.py`](../src/arb/storage/models.py)
(SQLAlchemy 2.0 declarative models) and created via Alembic migrations under
[`migrations/versions/`](../migrations/versions/). Schema changes always go
through a migration — models are never applied directly. Models stay
dialect-portable (fast tests run the same models against in-memory SQLite
via `aiosqlite`); migrations are Postgres-specific. Times are stored in
UTC; nanosecond timestamps are `BIGINT`, never floats.

### `raw_messages` (migration `0001`)

The complete, unparsed archive of every inbound message from every venue,
in every run.

| column | type | notes |
| --- | --- | --- |
| `id` | BIGSERIAL PK | |
| `run_id` | text | |
| `ingest_seq` | bigint | per-run, cross-source monotonic |
| `venue` | text | `"kalshi"` / `"polymarket_us"` |
| `stream` | text | e.g. `"ws"`, `"rest:orderbook"`, `"rest:book"`, `"rest:events"` |
| `payload` | bytea | exact bytes received |
| `recv_ts_ns` | bigint | wall clock, ns since epoch |
| `recv_mono_ns` | bigint | monotonic clock, ns; only meaningful within one `run_id` |
| `inserted_at` | timestamptz | server default `now()` |

Constraints: `UNIQUE (run_id, ingest_seq)` — the archive is totally ordered
per run, and a duplicate write fails loudly rather than silently
overlapping. Index on `(venue, recv_ts_ns)` for replay-style range queries.
`arb replay` reads this table ordered by `ingest_seq` within a `run_id`.

### `pairs` (migration `0002`)

One proposed or human-decided equivalence between one Kalshi market and one
Polymarket US market. See [`pairs.md`](pairs.md) for the full matching and
review workflow.

| column | type | notes |
| --- | --- | --- |
| `id` | BIGSERIAL PK | |
| `kalshi_market_id` | text | |
| `polymarket_market_id` | text | |
| `status` | text | `proposed` / `confirmed` / `rejected`, default `proposed` |
| `score` | float | matcher confidence, analytics-only (float is fine here) |
| `detail` | JSON | both legs' snapshot at proposal time + scoring features |
| `created_at` | timestamptz | |
| `decided_at` | timestamptz, nullable | set when a human confirms/rejects |

Constraints: `UNIQUE (kalshi_market_id, polymarket_market_id)` — re-running
the proposer upserts (refreshes `score`/`detail`) rather than duplicating,
and never overwrites a human's `status`/`decided_at`. Index on `status` for
the review screen's filtered listings.

### `paper_trades` (migration `0003`)

One simulated fill from the paper trader, live or replayed. See
[`engine.md`](engine.md#paper-trading).

| column | type | notes |
| --- | --- | --- |
| `id` | BIGSERIAL PK | |
| `run_id` | text | live run id, or `replay:<run_id>` for a replayed run |
| `pair_id` | bigint | references `pairs.id` (not FK-enforced) |
| `direction` | text | `"yes_a_no_b"` / `"yes_b_no_a"` |
| `qty` | bigint | `Qty` units (0.0001 contracts) |
| `cost_ticks` | bigint | both legs combined |
| `fee_ticks` | bigint | both legs combined |
| `net_ticks` | bigint | expected profit at settlement |
| `ts_ms` | bigint | |
| `detail` | JSON | full trade payload, including both legs |
| `created_at` | timestamptz | |

Index on `(run_id, ts_ms)` for the ledger view.

### Primary-key portability note

All primary keys use `BigInteger().with_variant(Integer(), "sqlite")` — SQLite
only auto-increments `INTEGER` primary keys, so fast unit tests (which run
against in-memory SQLite, not a real Postgres) get a plain `INTEGER`
autoincrement while Postgres gets a real `BIGSERIAL`.
