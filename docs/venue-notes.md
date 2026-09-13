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
- Sequencing: `seq` is a "Sequential number that should be checked if you want
  to guarantee you received all the messages". The docs do **not** prescribe a
  recovery procedure for gaps; our policy (resubscribe + fresh snapshot) is our
  own choice, see decisions.md.

### Price and quantity encoding

- Current format is **fixed-point dollar strings**, not integer cents: prices
  are `*_dollars` strings with subpenny precision (e.g. `"0.4200"`), counts are
  `*_fp` fixed-point strings (e.g. `"13.00"` = 13 contracts). The old
  integer-cent fields were removed 2026-07-09 per the changelog.
  — https://docs.kalshi.com/getting_started/orderbook_responses.md
  — https://docs.kalshi.com/changelog/index.md
- Four-decimal dollar strings map exactly onto our $0.0001 integer ticks
  (`"0.4200"` → 4200). Contract counts are fixed-point strings — whether
  fractional contracts actually occur must be confirmed from captured data
  before we hard-code integer quantities for Kalshi.
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

### Open items

- WS gap recovery is unspecified in docs — our resubscribe+snapshot policy is
  self-imposed.
- Exact WS array field names to be confirmed against
  https://docs.kalshi.com/asyncapi.yaml before coding the parser.
- Whether `GET /markets` / `GET /events` require auth was not explicitly
  stated; `GET /markets/{ticker}/orderbook` explicitly does.
- Reliable market categorization (event `category` is deprecated).
- Fractional contract counts (`*_fp`) vs our integer-quantity assumption.

## Polymarket US

Scope: **only** docs.polymarket.us. The international Polymarket
(polymarket.com, its CLOB and Gamma APIs) is out of scope.

_Nothing verified yet._

- Docs: https://docs.polymarket.us
- Auth scheme:
- REST base URL:
- WebSocket URL:
- Order book message format (one YES instrument; buying NO is selling YES):
- Sequencing / snapshot semantics:
