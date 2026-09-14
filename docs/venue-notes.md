# Venue notes

Verified API facts only. Every entry cites the doc URL it came from and the
date verified. Nothing here is guessed — if it isn't confirmed against the
docs, it doesn't go in. When a prompt and the docs disagree, the docs win and
the conflict is noted here.

## Kalshi

_Verified 2026-09-13 against live docs at docs.kalshi.com._

### Hosts

- Production REST (current, recommended): `https://external-api.kalshi.com/trade-api/v2`
  — https://docs.kalshi.com/getting_started/api_environments.md
- Production REST (legacy, still supported): `https://api.elections.kalshi.com/trade-api/v2`
  — same source.
- Production WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
  — https://docs.kalshi.com/getting_started/quick_start_websockets.md
- Demo REST: `https://external-api.demo.kalshi.co/trade-api/v2`; demo WS:
  `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
  — https://docs.kalshi.com/getting_started/api_environments.md

### Auth

- API key = Key ID + RSA private key (PEM).
  — https://docs.kalshi.com/getting_started/api_keys.md
- Headers on every signed request: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`
  (milliseconds), `KALSHI-ACCESS-SIGNATURE`.
- String to sign: `timestamp + HTTP_METHOD + path` (path from the API root,
  **without** query params), e.g. `1703123456789GET/trade-api/v2/portfolio/balance`.
  Signature: RSA-PSS with SHA-256 (MGF1/SHA-256, salt length = digest length),
  base64-encoded.
  — https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.md
- WebSocket auth: the same three headers go on the **connection upgrade
  handshake** (not a post-connect message). WS string to sign:
  `timestamp + "GET" + "/trade-api/ws/v2"`.
  — https://docs.kalshi.com/getting_started/quick_start_websockets.md
- **There are no unauthenticated WS channels.** Market-data channels (`ticker`,
  `orderbook_delta`, `trade`, `market_lifecycle_v2`) carry public data but the
  connection itself must be authenticated. So we need Kalshi credentials even
  for read-only Day 1.
  — https://docs.kalshi.com/getting_started/quick_start_websockets.md

### Order book

- WS channel `orderbook_delta`: sends `orderbook_snapshot` first, then
  incremental `orderbook_delta` messages.
  — https://docs.kalshi.com/websockets/orderbook-updates.md
- Book contains **bids only** (YES bids and NO bids, separate arrays); no asks.
  REST doc verbatim: "It returns yes bids and no bids only (no asks are
  returned)". Confirms our complement model (NO bid at x = YES ask at 1 - x).
  — https://docs.kalshi.com/api-reference/market/get-market-orderbook.md
  — https://docs.kalshi.com/getting_started/orderbook_responses.md
- Snapshot fields: `market_ticker`, `yes_dollars_fp`, `no_dollars_fp` (arrays of
  `[price_in_dollars, contract_count_fp]`). Delta fields: `price_dollars`,
  `delta_fp`, `side` (`"yes"`/`"no"`), `seq`.
  — https://docs.kalshi.com/websockets/orderbook-updates.md
- Subscribe command (verified,
  https://docs.kalshi.com/getting_started/quick_start_websockets.md):
  `{"id": N, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"],
  "market_tickers": [...]}}`. Ack: `{"type": "subscribed", "id": N,
  "msg": {"channel": "orderbook_delta", "sid": S}}` (observed live).
- Envelope (verified, https://docs.kalshi.com/asyncapi.yaml):
  `{"type", "sid", "seq", "msg"}`. Snapshot msg: `market_ticker`,
  `market_id`, `yes_dollars_fp`, `no_dollars_fp`. Delta msg: `market_ticker`,
  `market_id`, `price_dollars`, `delta_fp` (**signed**, e.g. `"-54.00"`),
  `side` (`"yes"`/`"no"`), `ts`, `ts_ms`.
- `update_subscription` supports `add_markets` / `delete_markets` /
  `get_snapshot`; `get_snapshot` returns an `orderbook_snapshot` on demand
  without changing the subscription — useful for gap recovery without a full
  resubscribe. Channel error code 10 is terminal → must resubscribe.
  (asyncapi.yaml)
- **Sequencing (observed live, 2026-09-14, production): `seq` is
  per-subscription (`sid`), shared across all markets in it.** A five-market
  subscription produced seq 1..5 for the five snapshots then 6.. for deltas
  interleaved across markets — so per-market seq contiguity does NOT hold.
  Gap detection must run at subscription level; books receive unsequenced
  events from this venue. Capture:
  `tests/fixtures/kalshi/ws_orderbook_capture.jsonl`.
- Observed: WS text frames arrive with a trailing newline. WS auth with the
  RSA-PSS handshake headers confirmed working end-to-end.

### Price and quantity encoding

- Current format is **fixed-point dollar strings**, not integer cents: prices
  are `*_dollars` strings with subpenny precision (e.g. `"0.4200"`), counts are
  `*_fp` fixed-point strings (e.g. `"13.00"` = 13 contracts). The old
  integer-cent fields were removed 2026-07-09 per the changelog.
  — https://docs.kalshi.com/getting_started/orderbook_responses.md
  — https://docs.kalshi.com/changelog/index.md
- Four-decimal dollar strings map exactly onto our $0.0001 integer ticks
  (`"0.4200"` → 4200). **Fractional contract counts are real**: the live
  KXWC-30-POR book (captured 2026-09-13, in
  `tests/fixtures/kalshi/rest_orderbook_kxwc-30-por.json`) contains counts
  like `"4903190.79"` and `"15.17"`. Quantities therefore use integer
  fixed-point units of 0.0001 contracts (`Qty`), not integer contracts.
- REST orderbook response wraps arrays under top-level `orderbook_fp` with
  `yes_dollars` / `no_dollars`; WS snapshot names them `yes_dollars_fp` /
  `no_dollars_fp`. Same data, different field names — parsers must not share a
  schema blindly. Verify against https://docs.kalshi.com/asyncapi.yaml when
  writing the WS parser.
  — https://docs.kalshi.com/api-reference/market/get-market-orderbook.md

### Rate limits

- Token bucket; default request cost 10 tokens; 429 with no penalty on limit.
  Basic tier: 200 read / 100 write tokens per second. Higher tiers up to
  Prestige (10,000/8,000). Burst: most tiers hold up to 2 s of budget.
  — https://docs.kalshi.com/getting_started/rate_limits.md
- Live per-account limits: `GET /account/api_limits`.
  — https://docs.kalshi.com/api-reference/account/get-account-api-limits.md

### Market discovery

- `GET /markets`: `limit` (default 100, max 1000) + `cursor` pagination; cursor
  empty when done. Market fields: `ticker`, `event_ticker`, `market_type`
  (`binary`/`scalar`), `rules_primary`, `rules_secondary`, `yes_sub_title`,
  `no_sub_title`, `open_time`, `close_time`, `volume_fp`, `open_interest_fp`,
  `yes_bid_dollars`/`yes_ask_dollars`/... (`title`/`subtitle` deprecated).
  Status enum on the object: `initialized/inactive/active/closed/determined/
  disputed/amended/finalized`; the `status` **query filter** uses different
  buckets (`unopened/open/closed/settled`) — don't mix them.
  — https://docs.kalshi.com/api-reference/market/get-markets.md
- `GET /events`: `limit` (max 200) + `cursor`; **excludes multivariate events by
  design** (those live at `GET /events/multivariate`). Event `category` is
  deprecated; no `category` on markets — categorization source is an open
  question.
  — https://docs.kalshi.com/api-reference/events/get-events.md
  — https://docs.kalshi.com/api-reference/events/get-multivariate-events.md
- Sports metadata: `GET /milestones` (games, start times in `start_date`,
  free-form `details`) and `GET /structured_targets` (teams/players;
  `page_size` max 2000 + `cursor`).
  — https://docs.kalshi.com/api-reference/milestone/get-milestones.md
  — https://docs.kalshi.com/api-reference/structured-targets/get-structured-targets.md

### Observed live behavior (2026-09-13, production API, unauthenticated)

- `GET /markets`, `GET /events` and `GET /markets/{ticker}/orderbook` all
  returned **200 without auth headers**. The docs say the orderbook endpoint
  requires auth — conflict noted; per project rules the docs win for client
  design (we sign requests once credentials exist), but unauthenticated
  capture works today and produced our fixtures.
- The default `GET /markets?status=open` listing is dominated by
  `KXMVECROSSCATEGORY-SHARD*` multivariate combo markets with zero volume.
  Discovery should go through `GET /events` (which excludes multivariate
  events by design) rather than raw `/markets`.
- Real payloads match the documented field names (`orderbook_fp` with
  `yes_dollars`/`no_dollars` arrays of `[price, count]` strings; market
  objects with `ticker`, `event_ticker`, `rules_primary`, `yes_sub_title`,
  `volume_24h_fp`, status `active`, type `binary`).

- `GET /markets/{ticker}` → `{"market": Market}` (verified,
  https://docs.kalshi.com/api-reference/market/get-market.md; fixture
  `tests/fixtures/kalshi/rest_market_kxpresperson-28-tgab.json`). Extra
  live fields beyond the get-markets list: `expected_expiration_time`,
  `can_close_early`, `previous_price_dollars`, `yes_bid_size_fp`, etc.
- `GET /events/{event_ticker}` → `{"event": EventData, "markets": [...]}`
  (verified, https://docs.kalshi.com/api-reference/events/get-event.md;
  fixture `tests/fixtures/kalshi/rest_event_kxpresperson-28.json`).
  EventData carries `series_ticker`, `title`, `sub_title`, `category`
  (deprecated but populated), `mutually_exclusive` and
  **`settlement_sources`** (`[{name, url}]`) — the resolution sources the
  pair matcher and DES page need. Ticker anatomy confirmed:
  `KXPRESPERSON` (series) → `KXPRESPERSON-28` (event) →
  `KXPRESPERSON-28-TGAB` (market).
- `GET /events` supports `with_nested_markets=true` (verified,
  https://docs.kalshi.com/api-reference/events/get-events.md) — used for
  liquidity-ranked discovery. `GET /markets` also supports `event_ticker`
  and `tickers` filters (verified,
  https://docs.kalshi.com/api-reference/market/get-markets.md).

### Open items

- Gap recovery policy is ours (docs specify none): track seq per `sid`; on a
  gap, `update_subscription` + `get_snapshot` (or resubscribe on terminal
  errors) and invalidate the affected books.
- Reliable market categorization (event `category` is deprecated).
- No "no"-side delta captured yet — parser handles both sides but the
  fixture only exercises `side: "yes"`; extend on a future capture.

## Polymarket US

Scope: **only** docs.polymarket.us. The international Polymarket
(polymarket.com, its CLOB and Gamma APIs) is out of scope.

_Verified 2026-09-13 against docs.polymarket.us. We use the retail API; the
"Trader Guide" institutional surface (polymarketexchange.com, gRPC/FIX,
Auth0) is separate credentialing and out of scope for now._

### Hosts (retail API)

- Public REST (market data, **no auth**): `https://gateway.polymarket.us`
  — https://docs.polymarket.us/api-reference/introduction
- Authenticated REST (trading/portfolio): `https://api.polymarket.us`
  — same source.
- WebSocket market data: `wss://api.polymarket.us/v1/ws/markets`
  (books + trades); private: `wss://api.polymarket.us/v1/ws/private`.
  — https://docs.polymarket.us/api-reference/websocket/overview
- No sandbox/demo documented for the retail API.

### Auth

- API key pair: Key ID + Secret Key, created at polymarket.us/developer
  (app signup + KYC; secret shown once).
  — https://docs.polymarket.us/api-reference/authentication
- Headers: `X-PM-Access-Key`, `X-PM-Timestamp` (ms, within 30 s of server
  time), `X-PM-Signature` (base64).
- Signing: **Ed25519** (not RSA). String to sign =
  `timestamp + HTTP_METHOD + path`, e.g.
  `1234567890GET/v1/portfolio/positions`. Secret is base64-decoded, message
  signed, signature base64-encoded.
  — https://docs.polymarket.us/api-reference/authentication
- Public REST market data needs **no** auth (`security: []` on gateway
  endpoints). But the **market-data WebSocket requires API-key auth on the
  connection handshake** (same `X-PM-*` headers; string to sign
  `timestamp + "GET" + "/v1/ws/markets"`). So live streaming needs
  credentials on both venues; unauthenticated REST polling (20 req/s/IP) is
  the fallback.
  — https://docs.polymarket.us/api-reference/websocket/markets

### Market structure

- Hierarchy: Series → Events → Markets. **One instrument per market** (the
  YES outcome); "to take the NO side, you short the instrument" — buying NO
  is selling YES. Confirms our book model. Settles at $1.00 / $0.00.
  — https://docs.polymarket.us/concepts/events-and-markets
  — https://docs.polymarket.us/learn/trading/basics/buying-yes-vs-selling-no
- `slug` is the identifier used everywhere (orders, books, WS subscribe);
  also `id` and event-level `ticker`.
- Market object (gateway `GET /v1/markets`): `question`, `slug`,
  `description`, `category`, `active`/`closed`/`archived`/`hidden` booleans,
  `startDate`/`endDate`/`gameStartTime`, `orderPriceMinTickSize`,
  `minimumTradeQty`, `feeCoefficient`, `bestBidQuote`/`bestAskQuote`
  (Amount objects), volumes, `marketSides`, `tags`.
  — https://docs.polymarket.us/api-reference/markets/get-markets
- No dedicated rules-text field documented; candidates are `description`
  and `rulesDisclaimer`. Open question.
- Live trading state comes from the book's `state` enum
  (`MARKET_STATE_PREOPEN|OPEN|SUSPENDED|HALTED|EXPIRED|TERMINATED|
  MATCH_AND_CLOSE_AUCTION`).
  — https://docs.polymarket.us/api-reference/markets/get-market-book

### Order book data

- REST snapshot (public): `GET /v1/markets/{slug}/book` →
  `marketData { marketSlug, bids[], offers[], state, stats, transactTime }`;
  each level `{ "px": { "value": "0.55", "currency": "USD" }, "qty": "..." }`.
  Depth of response: undocumented.
  — https://docs.polymarket.us/api-reference/markets/get-market-book
- BBO (public): `GET /v1/markets/{slug}/bbo`.
  — https://docs.polymarket.us/api-reference/markets/get-market-bbo
- WS subscribe: `{"subscribe": {"requestId": ..., "subscriptionType":
  "SUBSCRIPTION_TYPE_MARKET_DATA", "marketSlugs": [...]}}`, optional
  `responsesDebounced`; max 100 markets per subscription. Types:
  `MARKET_DATA` (full book + stats), `MARKET_DATA_LITE`, `TRADE`.
  Heartbeat message `{"heartbeat": {}}` exists; cadence undocumented.
  — https://docs.polymarket.us/api-reference/websocket/markets
- **No sequence numbers and no documented snapshot-then-delta protocol.**
  MARKET_DATA messages carry full `bids[]`/`offers[]` arrays plus
  `transactTime` — apparently a full book per message, though the docs never
  state that explicitly. Book validity must lean on `transactTime` /
  staleness plus periodic REST snapshot reconciliation, not seq gaps.
- **Wire-format conflict in the docs**: the WS overview page shows
  snake_case fields with numeric enums (`"subscription_type": 1`); the
  markets page shows camelCase with string enums. Real captured payloads
  decide; parser is written against fixtures.
  — https://docs.polymarket.us/api-reference/websocket/overview vs
  https://docs.polymarket.us/api-reference/websocket/markets

### Price and quantity encoding

- Prices are Amount objects with decimal **dollar strings**
  (`{"value": "0.55", "currency": "USD"}`), $0–$1 range; fee formula domain
  implies tradable prices $0.01–$0.99. Maps exactly onto our $0.0001 ticks.
- Tick size is **per-market** (`orderPriceMinTickSize`); no global tick
  documented.
- Quantities: "Polymarket US only supports whole contracts" — integer
  contracts, confirming our assumption. `qty` values arrive as strings.
  — https://docs.polymarket.us/learn/faq/whole-contracts

### Trades and history

- WS `SUBSCRIPTION_TYPE_TRADE`: `marketSlug`, `price`, `quantity`,
  `tradeTime`, `maker`/`taker` (side, intent). No public REST trades
  endpoint on the gateway; public tape is daily CSVs at
  polymarketexchange.com/time-and-sales.html.
  — https://docs.polymarket.us/faqs/execution-tape
- Price history (public): `GET /v1/price-history?symbol=<slug>` with
  `fidelity` (minutes) and interval/timestamp params; returns
  `{timestamp, longPrice, shortPrice}`.
  — https://docs.polymarket.us/api-reference/price-history/get-price-history

### Market discovery

- `GET /v1/markets` and `GET /v1/events` on the gateway, no auth.
  Pagination is **`limit` + `offset`** (not cursor); documented max page
  size: not found. Rich filters (`active`, `closed`, `categories[]`,
  `sportsMarketTypes[]`, date ranges, `tagIds[]`, ...).
  — https://docs.polymarket.us/api-reference/markets/get-markets
  — https://docs.polymarket.us/api-reference/events/get-events
- Settlement: `GET /v1/markets/{slug}/settlement` → `{slug, settlement}`
  (404 until settled).

### Rate limits

- Authenticated: 20 req/s per API key. Public gateway: 20 req/s per IP.
  429 with JSON body on limit; docs recommend exponential backoff from 1 s.
  WS: only documented cap is 100 markets per subscription.
  — https://docs.polymarket.us/api-reference/rate-limits

### Fees (for the arb math)

- `Fee = Θ × C × p × (1 − p)`, p in dollars ($0.01–$0.99), C contracts.
  Taker Θ = 0.06; maker Θ = −0.0125 (rebate, applied on fill). Volume tiers
  add 10%/25%/50% extra maker rebate at ≥$250K/$1M/$10M monthly. Banker's
  rounding to the cent per fill, capped at the rounded cumulative exact fee.
  Effective 2026-07-01. Markets carry per-market `feeCoefficient`.
  — https://docs.polymarket.us/fees

### Observed rate limiting (2026-09-14, live, contradicts docs)

- Docs say "20 requests per second per IP" for the public gateway. In
  practice `GET /v1/markets/{slug}/book` 429'd on a burst of ~17 sequential
  requests (~20/s effective) and kept 429ing for ~10 s afterwards — a
  cooldown/penalty window the docs don't mention ("no penalty" is claimed
  only for the authenticated API). Even at 1 req/s one further 429 appeared
  right after recovery. Poller design: token bucket well under the cap
  (≤ ~5 req/s), immediate stop on 429 with ≥ 10 s backoff.

### Observed latency (2026-09-14, from a residential connection; VM will differ)

- Gateway REST `GET book`: ~21–22 ms median warm round trip.
- Kalshi REST `GET orderbook`: ~27–30 ms median warm round trip.
- Kalshi WS: ping RTT ~22–36 ms; **delta push latency (exchange `ts_ms` →
  local receive) median ~8 ms, range 5.5–12.5 ms across two sessions** —
  subject to local NTP accuracy, but consistent.

### Observed live behavior (2026-09-13, public gateway, unauthenticated)

- `GET /v1/markets` and `GET /v1/markets/{slug}/book` returned 200 with no
  auth, as documented. Captured payloads are the fixtures in
  `tests/fixtures/polymarket_us/`.
- Real market objects carry fields beyond the documented list: a `status`
  string enum (`"MARKET_STATUS_OPEN"`), `outcomePrices` / `outcomes` as
  JSON-encoded *strings*, `sportsMarketTypeV2`, `manualActivation`,
  `ep3Status`. `feeCoefficient` arrived as `0.06`, `orderPriceMinTickSize`
  as `0.001` (JSON numbers — we parse them as Decimal).
- Book `qty` strings are four-decimal (`"45.0000"`, `"13002.0000"`) — whole
  numbers in the captured book, consistent with whole-contract trading, but
  parsed as fixed-point 0.0001-contract units anyway.

### Open items

- WS wire format (casing + enum encoding) must be settled from captured
  payloads before the parser is written.
- No seq numbers on the markets WS → validity via staleness +
  `transactTime` + periodic REST reconciliation.
- REST book depth, WS heartbeat cadence and stall behavior: measure
  empirically during day-1 recording.
- Which field carries full rules text (`description` vs `rulesDisclaimer`).
- Whether `qty`/"shares" strings can ever be fractional despite
  whole-contract trading (validate on capture).
