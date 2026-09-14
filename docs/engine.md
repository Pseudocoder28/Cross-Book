# Engine: fees, edge, monitoring, paper trading, replay

Code: [`src/arb/fees.py`](../src/arb/fees.py),
[`src/arb/edge.py`](../src/arb/edge.py),
[`src/arb/arbmon.py`](../src/arb/arbmon.py),
[`src/arb/paper.py`](../src/arb/paper.py),
[`src/arb/paper_store.py`](../src/arb/paper_store.py),
[`src/arb/replay.py`](../src/arb/replay.py). This is day 2 of the build: once
a pair is confirmed ([`pairs.md`](pairs.md)) and both legs have live books
([`data-model.md`](data-model.md)), this layer measures whether there's
money on the table, and simulates taking it. **Nothing here places, amends
or cancels a real order** — day 1's read-only rule holds throughout day 2;
paper trading talks to no venue at all.

## Fees

[`fees.py`](../src/arb/fees.py). Both venues' fee formulas are the same *shape* —
`Θ × C × P × (1 − P)`, a coefficient times contracts times price times
one-minus-price — but different coefficients, different rounding, and
different maker/taker treatment. Everything is computed with
`fractions.Fraction` so the formula is evaluated exactly before any
rounding happens; the only floats anywhere near this module are the
`Decimal` fee-coefficient inputs parsed off the wire, converted to exact
`Fraction`s immediately.

### Kalshi

```
model_fee = 0.07 * fee_multiplier * C * P * (1 - P)     # dollars, exact
trade_fee = ceil(model_fee, to $0.000001)                 # venue's own rounding
net_cost  = ceil(trade_fee, to the next tick = $0.0001)   # direct-member precision
```

`KALSHI_TAKER_COEFF = 7/100`. Maker fees are a multiplier of the taker
coefficient, keyed by `fee_type`: `0` on plain `quadratic`, `1/4` on
`quadratic_with_maker_fees`, `1/2` on `quadratic_with_combo_maker_fees`. An
unrecognized `fee_type` (e.g. `flat`, whose table isn't exposed by the API)
is treated as `quadratic` for takers — the conservative reading — and as
having no maker discount. `fee_multiplier` (from the series, overridable
per event) scales the whole taker coefficient; a real example: the 2022
CFTC-filed INX schedule used `0.035 = 0.5 × 0.07`.

Rounding is **two-stage** and both stages round *up* (conservative,
matching what Kalshi documents): the model fee first rounds up to
$0.000001 precision (the venue's own stated rounding granularity, verified
against `docs.kalshi.com/getting_started/fee_rounding.md`'s worked example),
then that six-decimal trade fee rounds up again to the nearest whole tick
($0.0001) — the precision a direct (API) member's balance actually holds.
Kalshi documents banking sub-tick residue per order for later rebate; this
engine doesn't model that rebate, which makes its fee estimate slightly
conservative (an actual fill may cost fractionally less than quoted here).

### Polymarket US

```
fee = Θ * C * p * (1 - p)     # dollars, exact
cents = round_half_even(fee * 100)
```

Taker `Θ` is the market's own `feeCoefficient` (typically `0.06`); maker
`Θ = -0.0125` — a **rebate**, so `polymarket_fee_ticks()` can return a
negative tick count for a maker fill. Rounding is banker's rounding
(`ROUND_HALF_EVEN`) to the cent, per the fee docs.

Both formulas were checked against each venue's own documented worked
examples (see `fees.py`'s module docstring and `docs/venue-notes.md`) before
being trusted for the edge calculation below.

## Edge: the depth-aware, two-direction walk

[`edge.py`](../src/arb/edge.py). For one confirmed pair, buying YES on venue
A and NO on venue B locks in exactly $1.00 at settlement (both legs pay off
together, always, because they're the same outcome by construction) for a
combined cost of `yes_ask_A + no_ask_B`. Since buying NO is selling YES,
`no_ask_B = 10000 - yes_bid_B` — so the **gross edge per contract**, before
fees, is:

```
gross_ticks_per_contract = yes_bid_B - yes_ask_A
```

`compute_direction()` walks *both* ladders simultaneously in increasing-cost
order (`_walk()`: a merge over two price-sorted level sequences, taking
`min(ask_level_qty, bid_level_qty)` at each step) rather than assuming one
flat size. At each fill increment it checks whether the *marginal* gross
still covers *that increment's own* fees on both legs:

```
marginal_gross * take  >  (fee_a(take, ask) + fee_b(take, bid)) * QTY_PER_CONTRACT
```

and stops the walk the moment a level would be unprofitable to take —
sizes reported are strictly what the books actually offer and what's
actually worth taking, never a hopeful flat number. The result is an
`EdgeQuote`: total `qty`, `gross_tq`/`fee_ticks`/`net_tq` (the `_tq` suffix
means "tick·Qty" — a product that must be divided by `QTY_PER_CONTRACT`
to recover a plain tick amount; kept as a raw integer product throughout
the walk to stay exact without intermediate rounding), plus both `Leg`s
(venue, market, side, worst price paid, qty, cost, fee).

`best_edge()` runs `compute_direction()` in **both** directions — buy YES
on A + NO on B, and the mirror (buy YES on B + NO on A) — and returns both
`EdgeQuote`s; callers pick whichever has the better
`net_per_contract_ticks`. A pair can have a real edge in only one direction
at a time (the two are almost never simultaneously profitable, since that
would imply a true riskless loop through both venues' spreads), but
checking both is cheap and removes any directional assumption from the
caller.

## `ArbMonitor`: quoting every tracked pair off live books

[`arbmon.py`](../src/arb/arbmon.py). `TrackedPair` bundles everything
`edge.py` needs for one confirmed pair — both market ids, both fee
functions (bound via `functools.partial` in
[`pairs/tracked.py`](../src/arb/pairs/tracked.py), see
[`pairs.md`](pairs.md#resolving-fee-parameters-for-confirmed-pairs-trackedpy)),
a human-readable label, and `fee_info` for display.

`ArbMonitor` indexes tracked pairs by market id (`_by_market`) so
`affected(market_ids)` — called every time `BookManager.apply()` reports
which books changed — returns exactly the pairs that need re-quoting,
never a full re-scan. `quote(pair)` pulls both legs' current `Book` state,
runs `best_edge()`, and returns a JSON-ready dict: both legs' state
(including whether each book is currently valid, and why not if it isn't),
both directions' quotes (`best` and `other`), and a timestamp.
`snapshot()` quotes every tracked pair and sorts by best net edge per
contract, descending — this is exactly the payload behind the terminal
UI's `ARB` screen and `GET /api/arb`.

This module never touches a book that doesn't exist yet or that has gone
invalid without crashing — `_leg_state()` reports `has_book: false` /
`valid: false, reason: "no_book"` cleanly, and `Book.best_bid()`/
`best_ask()` returning `None` just means that side of the walk sees an
empty ladder, producing a zero-qty `EdgeQuote` rather than an error.

## Paper trading

[`paper.py`](../src/arb/paper.py). `PaperTrader.consider(pair, quote,
ts_ms)` is the single entry point: given a `TrackedPair` and the
`EdgeQuote` currently measured for it, decide whether to "take" it, size
it against risk limits, and record the simulated fill.

### The one deliberate optimism, and where honesty is preserved everywhere else

A paper fill is assumed to execute **instantly, at the full displayed
liquidity the edge walk already consumed, on both legs simultaneously**.
That's the one place this simulator is generous — no latency, no partial
fill below what the book showed, no chance the book moves between legs.
Everywhere else it's honest: fees are the exact venue models from
`fees.py`, sizes are capped by what the books actually offered *and* by the
risk limits below, and "P&L" is reported as the **net edge locked in at
settlement** — both legs of a confirmed pair always pay exactly $1.00
together by construction — which makes it an expected-value figure under
pair equivalence, not a mark-to-market price. If the pair match is wrong
(the two markets aren't actually the same bet), this number means nothing;
that's exactly why pair confirmation is a human gate (see
[`pairs.md`](pairs.md)).

### Risk limits: `PaperLimits`

Three explicit caps, deliberately simple — this is the same shape the real
trader inherits on day 3:

- `min_net_ticks` (default 50 = $0.005/contract) — a floor below which an
  edge isn't worth the operational risk of taking it at all.
- `max_qty_per_pair` (default 100 contracts, in `Qty` units) — position cap
  per pair.
- `max_notional_ticks` (default $1,000) — total cost cap across every pair
  combined.

`consider()` rejects immediately if the quote's net-per-contract is below
the floor or the pair is already at its position cap, then computes the
largest size that fits *both* the pair cap and the remaining notional
budget (`Fraction` arithmetic, so the room-by-notional calculation is
exact), takes `min(quote.qty, room_pair, max_by_notional)`, and — if
positive — proportionally scales every component of the trade (cost, fee,
net, both legs' quantities) by `qty / quote.qty` using exact `Fraction`
scaling before truncating to an integer. A trade is recorded
(`PaperTrade`), the pair's running `PaperPosition` is updated, and the
trader's running `notional_ticks` grows.

`PaperTrader.payload()` is the JSON shape behind the terminal UI's `PAPER`
screen and `GET /api/paper`: limits, running totals, per-pair positions,
and the most recent trades (newest first, capped at `max_trades`).

### Persistence

[`paper_store.py`](../src/arb/paper_store.py) — `insert_trades()` bulk-writes
`PaperTrade`s into the `paper_trades` table (schema in
[`data-model.md`](data-model.md#paper_trades-migration-0003)), tagged by
`run_id`; `list_trades()` reads them back, newest first, optionally filtered
to one run. `arb ui --paper` persists every simulated fill under the live
run's `run_id` as they happen; `arb replay --paper --persist` persists under
`replay:<run_id>` instead, so a replayed strategy run is never confused with
a live one in the ledger.

## Replay: the live pipeline, fed from Postgres

[`replay.py`](../src/arb/replay.py). This is not a second implementation —
it is **the same** `KalshiMarketDataAdapter`, the same
`PolymarketUSMarketDataAdapter`, and the same `BookManager` the live system
uses, fed `RawMessage`s read back from `raw_messages` (ordered by
`ingest_seq` within one `run_id`) instead of a live `EventSource`, using the
*recorded* `recv_mono_ns` as the clock passed into every `Book.apply_*`
call. Because there's only one code path from `RawMessage` to `Book` state
and (optionally) edge quotes, there's no risk of replay drifting from what
the live system would have concluded about the same bytes — that guarantee
is architectural, not something replay's code has to maintain by hand.

`replay_run(engine, run_id, books, arbmon=None, trader=None)` streams the
run's rows (`yield_per=2000` for memory-bounded iteration over potentially
large runs), routes each by `(venue, stream)` to the matching adapter
(`kalshi`/`ws` or `polymarket_us`/`rest:book`; anything else is counted as
`skipped`, not an error — discovery/event/series pages, for instance, carry
no book data), applies the resulting events to `BookManager`, and — if an
`ArbMonitor` was supplied — re-quotes every pair affected by that update,
optionally running the quote through a `PaperTrader`. It accumulates a
`ReplayReport`: message/stream counts, parse-error count, final book
validity breakdown, and per-pair `PairStats` (quotes seen, how many had a
real edge, the best net-per-contract ever observed, max size, last value) —
`ReplayReport.summary()` renders this as the plain-text table `arb replay`
prints.

`run_replay()` is the CLI entry: resolves `run_id` (via `latest_run_id()`
if none given — the run with the most recent `recv_ts_ns` among its rows),
builds a fresh `BookManager` with the same staleness posture as live
(`book_staleness_limit_ms`, plus a 120-second Polymarket US staleness
budget — replay has no live poll cadence to compute a cycle-based budget
from, so a generous fixed value is used instead), optionally loads tracked
pairs via the *same* `load_tracked_pairs()` [`pairs.md`](pairs.md) uses live
(so fee parameters are fetched fresh, not replayed from history — a stale
fee schedule never leaks into a replay's edge numbers), and optionally
attaches a fresh `PaperTrader`. See [`cli.md`](cli.md#arb-replay) for flags.
