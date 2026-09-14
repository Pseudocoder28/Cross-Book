# Polymarket US integration

Code: [`src/arb/venues/polymarket_us/`](../../src/arb/venues/polymarket_us/).
Verified API facts and doc citations:
[`../venue-notes.md`](../venue-notes.md#polymarket-us). Scope note: **only**
`docs.polymarket.us` (the retail API) is in scope — the international
Polymarket (`polymarket.com`, its CLOB/Gamma APIs) is explicitly out of
bounds per `CLAUDE.md`, and the institutional "Trader Guide" surface
(`polymarketexchange.com`, gRPC/FIX, Auth0) is separate credentialing not
pursued here.

## Why this venue is polled, not streamed, today

Polymarket US's public gateway REST (`gateway.polymarket.us`) needs **no
auth** for market data. But the market-data **WebSocket**
(`wss://api.polymarket.us/v1/ws/markets`) requires API-key auth
(Ed25519-signed headers) on the connection handshake, and those credentials
require app signup + KYC with no sandbox — not yet provisioned. Until they
are, live streaming isn't available, and the fallback the docs themselves
recommend — unauthenticated REST polling at a stated 20 req/s/IP — is what
`PolymarketUSRestSource` implements.

Because the shared `EventSource` protocol doesn't care *how* a stream of
`RawMessage`s is produced, swapping in the real WebSocket later is a
same-shape change: a new `PolymarketUSWSSource` implementing `EventSource`
replaces `PolymarketUSRestSource` in the wiring, and `arb record`, `arb ui`,
`BookManager` and the recorder need no changes at all.

## Auth (for when it's wired up)

[Not yet implemented — no Polymarket US credentials are provisioned.]
Recorded for when it is: Key ID + Ed25519 secret from
`polymarket.us/developer`. Headers `X-PM-Access-Key`, `X-PM-Timestamp` (ms,
must be within 30s of server time), `X-PM-Signature` (base64). String to
sign: `timestamp + METHOD + path`, e.g.
`1234567890GET/v1/portfolio/positions`; secret is base64-decoded, message
signed with Ed25519, signature base64-encoded. The WS handshake signs
`timestamp + "GET" + "/v1/ws/markets"` the same way Kalshi's does.

## Market structure

Series → Events → Markets, with exactly **one instrument per market** (the
YES outcome) — "to take the NO side, you short the instrument." This is
exactly the book model `Book` already assumes (see
[`data-model.md`](../data-model.md#complement-pricing-one-book-two-sides)):
Polymarket US's `bids`/`offers` *are* the YES book directly, with no
complement mapping needed on this venue's side (Kalshi is the venue that
needs the NO→YES-ask folding). `slug` is the identifier used everywhere —
orders, books, WS subscriptions.

## REST parsers

[`rest.py`](../../src/arb/venues/polymarket_us/rest.py). JSON numbers are
parsed with `parse_float=Decimal` (`_loads()`), so per-market tick size and
fee coefficient survive exactly — this matters because
`orderPriceMinTickSize` and `feeCoefficient` feed the fee engine
([`engine.md`](../engine.md#fees)) and cannot tolerate float rounding.

Models (`extra="ignore"`, so live-only fields never break parsing):

- **`PolymarketUSMarket`** — `id`, `slug`, `question`, `title`,
  `description`, `category`, `active`/`closed`/`archived`/`hidden`,
  `status` (a string enum, e.g. `"MARKET_STATUS_OPEN"`, observed live but
  undocumented as such), `startDate`/`endDate`/`gameStartTime`,
  `minimumTradeQty` (a `Decimal`, **not necessarily an int** — see below),
  `orderPriceMinTickSize`, `feeCoefficient`, `bestBidQuote`/`bestAskQuote`
  (`Amount` objects).
- **`Amount`** — the gateway's money object: `{"value": "0.55", "currency":
  "USD"}`.
- **`PolymarketUSEvent`** — `id`, `slug`, `ticker`, `title`, `description`,
  `category`, `seriesSlug`, `active`, `closed`, date range.
- **`PolymarketUSBookStats`** — activity numbers riding along with a book
  poll: `state`, `transact_time`, `shares_traded`, `open_interest`,
  `last_trade_ticks`.

Parsers:

- `parse_markets_response` — `GET /v1/markets` → list of `PolymarketUSMarket`
  (pagination is `limit`+`offset`, not cursor-based).
- `parse_events_response` — `GET /v1/events` → `(event, nested markets)`
  pairs; a market that fails validation is skipped (never fatal to the
  whole page).
- `parse_book_response` — `GET /v1/markets/{slug}/book` →
  `BookSnapshot`. One instrument per market means `bids`/`offers` are
  already the YES book; **`seq` is always `None`** (this venue has no
  sequence numbers on market data at all — validity rests entirely on
  staleness plus periodic re-snapshotting, never on gap detection).
- `parse_book_stats` — the same book-poll payload's `marketData.stats` +
  `transactTime`, parsed separately from the book levels because the
  adapter needs both but they serve different purposes (book state vs.
  DES/monitor activity display).

### Fields the docs list but reality omits or adds

- Live listings (`GET /v1/markets`, `GET /v1/events`) carry **no
  volume/liquidity fields** despite the docs listing `volume`,
  `volume24hr`, `liquidity` — activity data only exists in a book poll's
  `stats` (`sharesTraded`, `openInterest`, `lastTradePx`) or the BBO
  endpoint. This is why discovery ranks by category, not volume (below).
- `minimumTradeQty` can be `0.01` on some markets (futures/awards) even
  though the docs say "whole contracts only" — parsed as `Decimal`, not
  assumed to be `1`.
- Real market objects carry extra fields beyond the documented list:
  `outcomePrices`/`outcomes` as JSON-encoded *strings*, `sportsMarketTypeV2`,
  `manualActivation`, `ep3Status` — all silently dropped by
  `extra="ignore"`.

## The REST poller: measured rate limit vs. documented rate limit

[`source.py`](../../src/arb/venues/polymarket_us/source.py). The docs say
"20 requests per second per IP" for the public gateway. **Measured
directly** (polling 8 markets at 0.25s and 0.5s spacing): exactly five
requests succeed, then a 429, at *every* spacing tried; after a 10-second
pause, five more succeed. That's a 5-token bucket refilling roughly one
token every 2 seconds — about 0.5 req/s sustained, ~40× below the
documented figure.

`PolymarketUSRestSource` is built around that measurement, not the docs:

- Default poll rate `polymarket_us_poll_rate = 0.45` req/s (leaving
  headroom for the UI's once-a-minute reachability probe on the same IP).
- Round-robins through the configured `slugs`, one `GET
  /v1/markets/{slug}/book` per tick, spaced by `1 / rate_per_s`.
- A `200` response: reset backoff, record `last_ok_mono_ns`, yield the
  `RawMessage`.
- A `429`: increment `rate_limited`/`arb_rest_rate_limited_total`, **pause
  the whole poller for `RATE_LIMIT_PAUSE_S` (10s)** — a full bucket refill
  — rather than backing off per-request. A 429 here never cascades into
  Kalshi's stream: this loop is a separate supervised task
  (`supervise(consume_poly, ...)`), so a Polymarket US rate-limit episode
  can't touch Kalshi recording.
- A transport error (timeout, connection refused, etc.): increment
  `errors`/`arb_rest_polls_total{status="error"}`, back off
  (`Backoff(initial_s=1.0, max_s=30.0)`), retry.

Every poll — successful or not — is counted in
`arb_rest_polls_total{venue,status}`, where `status` is the HTTP status
code or the literal `"error"`.

### Consequence for staleness

With N polled markets sharing one 0.45 req/s budget, each individual book
only refreshes roughly every `N / 0.45` seconds. A streaming-venue
staleness limit (default 5s) would flag every Polymarket US book stale
between polls, which is meaningless for a venue that's working as designed
— so callers set a per-venue staleness budget of about three poll cycles
via `BookManager.set_venue_staleness("polymarket_us", ...)` (see
[`data-model.md`](../data-model.md#bookmanager-the-fan-out-layer) and
[`ui.md`](../ui.md)).

## `PolymarketUSMarketDataAdapter`

[`adapter.py`](../../src/arb/venues/polymarket_us/adapter.py). Simple by
construction: every poll is a *complete* book, so every `rest:book` frame
becomes exactly one unsequenced `BookSnapshot` — there is no delta state to
track, no sequence gap to detect (there's no sequence number to check in
the first place). The adapter's only extra state is `stats: dict[market_id,
PolymarketUSBookStats]` (kept for the DES page and monitor display) and
`last_transact_ts_ms` (lets a caller measure how stale a snapshot already
was when it arrived, from the venue's own `transactTime`).

Turning these full snapshots into a discrete "tape" of level changes (like
a streaming venue naturally produces) is the caller's job, via
`level_deltas()` in `books.py` — see
[`data-model.md`](../data-model.md#bookmanager-the-fan-out-layer).

## Discovery and poll-target selection

[`discovery.py`](../../src/arb/venues/polymarket_us/discovery.py).
`GET /v1/events?active=true&closed=false&limit&offset` (the gateway
accepts `limit=500`, well above what the docs confirm as a maximum) with
nested markets.

- `fetch_active_markets(max_pages=12)` — pages through the active universe,
  spaced `PAGE_PACE_S = 2.2s` apart (one 5-token-bucket refill roughly every
  2s, so a burst of discovery pages reads as one client to the limiter, not
  several). A `429` mid-discovery retries the *same* page after a 10s pause
  (up to 3 retries) rather than losing it — another poller sharing the IP
  may have drained the bucket. Every response is recorded before parsing,
  same as every other REST call in the system.
- `select_poll_targets(markets, top_n)` — **live listings carry no volume
  field**, so instead of ranking by liquidity, targets are chosen
  non-sports-first (Kalshi's overlap with Polymarket US concentrates in
  non-sports categories — politics, economics, etc.) then sports, preserving
  listing order within each group. `--poly-slugs` on the CLI bypasses this
  entirely with an explicit list.
- `fetch_markets_by_slug(slugs)` — `GET /v1/markets?slug=A&slug=B` (the
  documented `slug[]` filter): one request refreshes fee coefficient, tick
  size and top-of-book for a specific set of tracked slugs — this is what
  `pairs/tracked.py` uses to resolve Polymarket US fee parameters for
  confirmed pairs without a full discovery pass.
- `event_refs(markets)` — groups discovered markets by event slug into the
  matcher's `EventRef`/`MarketRef` shape (see [`pairs.md`](../pairs.md)); the
  per-market `question` is preferred over the event `title` when they
  differ, since that's the more specific text.

## Observed live census (2026-09-14)

At the time of the last live check: ~3,560 active events / ~89,000 markets
total, mostly per-game sports; only ~60 events have markets that are
simultaneously `active` and not `closed` at any given moment — a page of
"200 open markets" was 196 sports and 4 politics. This is why non-sports
categories are prioritized for default poll targets: that's where the
cross-venue overlap with Kalshi actually is.

## Observed latency

Gateway REST `GET book`: ~21–22ms median warm round trip (residential
connection; a VM will differ — measure again once deployed).

## DES (market description) payload

[`detail.py`](../../src/arb/venues/polymarket_us/detail.py).
`build_market_detail()` produces the **same dict shape** as the Kalshi
version (see [`venues/kalshi.md`](kalshi.md#des-market-description-payload)),
so the terminal UI never branches on venue when rendering a DES page. Notable
mapping choices:

- `status` strips the `MARKET_STATUS_` prefix and lowercases
  (`"MARKET_STATUS_OPEN"` → `"open"`).
- `rules_primary` maps from `description` — the docs don't clearly name
  which field carries full rules text (`description` vs. `rulesDisclaimer`
  is an open item in venue-notes); `description` was chosen as the closer
  match to what's actually populated in captured payloads.
- Venue-specific extras not present on the Kalshi shape (`tick_size_ticks`,
  `fee_coefficient`, `min_trade_qty`) ride along as additional keys the
  frontend shows only when present.
- `volume`/`open_interest` come from the book poll's `stats`, not the
  market listing (which, as noted, carries no activity numbers at all).

## Open items

- WS wire format has a documentation conflict (snake_case + numeric enums
  on the overview page vs. camelCase + string enums on the markets page) —
  unresolved until real WS payloads can be captured, which needs
  credentials.
- No sequence numbers on the markets WS either, so validity there will also
  rest on staleness + `transactTime` + periodic REST reconciliation, same
  posture as the current REST poller.
- Whether `qty`/"shares" strings can ever be fractional despite
  whole-contract trading — not yet observed, parsed as fixed-point
  regardless so a fractional value wouldn't silently truncate if it showed
  up.
