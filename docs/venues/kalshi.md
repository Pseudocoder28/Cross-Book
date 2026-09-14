# Kalshi integration

Code: [`src/arb/venues/kalshi/`](../../src/arb/venues/kalshi/). Verified API
facts and doc citations: [`../venue-notes.md`](../venue-notes.md#kalshi).
This file explains how that code is put together; it does not re-derive
facts already recorded there.

## Why every Kalshi connection needs credentials

Kalshi's market-data WebSocket channels (`orderbook_delta`, `ticker`,
`trade`, `market_lifecycle_v2`) carry public data, but **the connection
itself must be authenticated** — there is no unauthenticated WS channel at
all. So even read-only book streaming needs a Key ID + RSA private key.
(REST market-data endpoints, by contrast, were observed answering 200
without auth in practice, despite docs claiming the orderbook endpoint
requires it — see the venue-notes conflict note. The client signs REST
requests anyway once credentials exist, since docs win per `CLAUDE.md`.)

## Auth: RSA-PSS request signing

[`auth.py`](../../src/arb/venues/kalshi/auth.py). The scheme, verified
against the Kalshi quickstart docs:

- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (milliseconds),
  `KALSHI-ACCESS-SIGNATURE`.
- String to sign: `timestamp + METHOD + path`, where `path` starts at the
  API root and **excludes query parameters**
  (`1703123456789GET/trade-api/v2/portfolio/balance`).
- Signature: RSA-PSS over SHA-256 (MGF1/SHA-256, salt length = digest
  length), base64-encoded.
- The WebSocket handshake is signed the same way, but as a plain GET against
  the fixed path `/trade-api/ws/v2` (`WS_SIGN_PATH`) — the three headers go
  on the connection upgrade request itself, not a post-connect message.

`load_private_key()` reads the PEM from the path in `AppConfig` and asserts
it's actually an RSA key. `sign()` and `auth_headers()`/`ws_auth_headers()`
build the header dict for one request or the WS handshake. **Key material
never leaves this module** — nothing here logs or prints it, and it's
tested only with throwaway generated keys (never real keys in tests).

Because both venues sign a timestamp into every request, headers must be
recomputed fresh on *every* connection attempt, not just the first — that's
why `KalshiWSSource` passes a `headers_factory` closure (not a static header
dict) into `websockets_connector()`.

## WebSocket wire format and parsing

[`ws.py`](../../src/arb/venues/kalshi/ws.py), verified against
`docs.kalshi.com/asyncapi.yaml` and tested against real captured frames in
[`tests/fixtures/kalshi/ws_orderbook_capture.jsonl`](../../tests/fixtures/kalshi/ws_orderbook_capture.jsonl)
and the NO-side companion fixture.

**Subscribe command:**

```json
{"id": 1, "cmd": "subscribe",
 "params": {"channels": ["orderbook_delta"], "market_tickers": ["..."]}}
```

**Envelope:** `{"type": ..., "sid": ..., "seq": ..., "msg": {...}}`. Kalshi
answers a subscribe with `{"type": "subscribed", "id": N, "msg": {"channel":
"orderbook_delta", "sid": S}}`, then streams `orderbook_snapshot` (one per
subscribed market) followed by `orderbook_delta` messages interleaved across
every market in the subscription.

**Snapshot** (`msg`): `market_ticker`, `yes_dollars_fp` and `no_dollars_fp`
— each an array of `[price_dollars, count_fp]` pairs. `yes_dollars_fp`
becomes the YES-bid ladder directly; `no_dollars_fp` becomes the YES-ask
ladder **at the complement price** (a NO bid at `x` is a YES ask at
`10000 - x`).

**Delta** (`msg`): `market_ticker`, `price_dollars`, `delta_fp` (**signed**,
e.g. `"-54.00"`), `side` (`"yes"` or `"no"`), `ts`/`ts_ms`. Mapping:

- `side: "yes"` → `BookSide.BID` at `price_dollars` as-is.
- `side: "no"` → `BookSide.ASK` at `complement(price_dollars)` — a NO-bid
  change is, by definition, a YES-ask change at the complement price. This
  was pinned against a real captured frame (`side: "no"`, `price_dollars:
  "0.7500"`, `delta_fp: "-35.32"` on `KXNEXTPRESSEC-29JAN21-MBAR`) rather
  than assumed.

`book_events_from_doc(doc)` is the shared core — it takes an
already-JSON-decoded envelope and returns normalized `BookSnapshot` /
`BookLevelUpdate` events (or `[]` for non-book frame types like
`subscribed` or channel errors — those still got archived by the recorder,
they just produce no book event). `parse_ws_message(raw)` is the thin
`RawMessage`-in wrapper used directly by tests; the adapter (below) calls
`book_events_from_doc` itself because it needs the decoded `doc` a second
time for sequence tracking.

Malformed frames raise `ParseError` (a `ValueError` subclass) rather than
crashing the consumer — callers count these
(`arb_parse_errors_total{venue="kalshi"}`) and keep going.

## The critical protocol finding: `seq` is per-subscription

Kalshi's docs don't specify a gap-recovery procedure, and a naive reading of
the envelope (`{"seq": N}`) suggests per-market sequencing. **A live
capture proved otherwise**: subscribing five markets at once produced `seq`
1..5 for the five snapshots, then 6.. for deltas interleaved across all five
markets — i.e. `seq` is scoped to the *subscription* (`sid`), shared by
every market in it, not scoped to an individual market.

This means `Book`'s own `seq == last + 1` contiguity check must never see
Kalshi's raw `seq` — if it did, any multi-market subscription would produce
constant false-positive gaps on every book except the one that happened to
receive the next sequential update. The fix lives in the **adapter**, not in
`Book`:

## `KalshiMarketDataAdapter`: subscription-scoped gap detection

[`adapter.py`](../../src/arb/venues/kalshi/adapter.py). Tracks
`_last_seq_by_sid: dict[sid, seq]`. On each `orderbook_snapshot` or
`orderbook_delta` frame:

1. Extract `sid` and `seq` from the envelope; `ParseError` if either is
   missing (frames of the sequenced types are expected to always carry
   both).
2. If this `sid` has a prior seq and the new one isn't `prior + 1`, that's a
   gap: emit `ResyncRequired("kalshi", None)` *before* the frame's own
   event, increment `arb_seq_gaps_total{venue="kalshi"}`. `market_ids=None`
   means "every book in this venue" because seq is subscription-scoped and
   the adapter can't know which specific markets missed data.
3. Adopt the new `seq` for this `sid` regardless (so a resync is armed
   exactly once per gap, not once per subsequent frame).
4. Re-emit the parsed event(s) with `seq=None` — **books always receive
   unsequenced events from this venue**. `Book`'s own contiguity check is
   therefore a no-op for Kalshi; all gap detection happens here, at the
   subscription level, where it's actually meaningful.

The adapter also tracks `last_delta_ts_ms` (the exchange's `ts_ms` on the
most recently parsed delta) so callers can measure one-way push latency
without re-parsing the payload — this feeds the UI's latency panel (see
[`ui.md`](../ui.md)).

**Recovery**: Kalshi answers every (re)subscribe with a full
`orderbook_snapshot` per market (observed live). So the cheapest recovery
after a `ResyncRequired` is simply forcing a WebSocket reconnect — the
reconnect's `on_connected` callback resubscribes, and fresh snapshots follow
automatically. This is `KalshiWSSource.force_resync()`, and it's what the
UI's consume loop calls whenever it sees a `ResyncRequired` event. A
cheaper `update_subscription`/`get_snapshot` path (documented, avoids a full
reconnect) exists in the API but is not yet implemented — an open item.

## `KalshiWSSource`: the `EventSource`

[`source.py`](../../src/arb/venues/kalshi/source.py). Composes
`ReconnectingWebSocket` (see [`architecture.md`](../architecture.md#reliability-layer))
with:

- A `connector` built from `websockets_connector(kalshi_ws_url,
  headers_factory=...)`, where the factory recomputes `ws_auth_headers()`
  (fresh timestamp, fresh signature) on every connection attempt.
- An `on_connected` callback (`_resubscribe`) that sends
  `subscribe_orderbook_cmd` with a freshly incremented command id on every
  (re)connect.

`rtt_ms()` exposes the transport's keepalive round-trip time (immune to
clock skew, since only the local clock is involved) — used by the UI's
latency panel to sanity-check one-way latency estimates. `force_resync()`
is the gap-recovery entry point described above.

## REST: discovery, market/event/series lookups, orderbook snapshots

[`rest.py`](../../src/arb/venues/kalshi/rest.py) holds the Pydantic models
(`KalshiMarket`, `KalshiEvent`, `KalshiSeries`, `KalshiSettlementSource` —
all `extra="ignore"`, so unrecognized live fields never break parsing) and
the response parsers. Every parser takes a `RawMessage` (already recorded)
and either returns a typed value or raises `ParseError`.

Key shapes:

- `GET /markets/{ticker}` → `{"market": Market}`. Extra live fields beyond
  the docs' get-markets list include `expected_expiration_time`,
  `can_close_early`, `previous_price_dollars`.
- `GET /events/{event_ticker}` → `{"event": EventData, "markets": [...]}`.
  `EventData` carries `series_ticker`, `title`, `sub_title`, `category`
  (deprecated but populated), `mutually_exclusive`, and
  **`settlement_sources`** (`[{name, url}]`) — the resolution-source
  citations the DES page and pair matcher's human reviewer need.
- `GET /series/{series_ticker}` → `{"series": {ticker, title, category,
  fee_type, fee_multiplier}}` — this is where the fee engine's parameters
  live (see [`engine.md`](../engine.md#fees)); events may carry
  `fee_type_override` / `fee_multiplier_override` that take precedence.
- `GET /markets/{ticker}/orderbook` → `{"orderbook_fp": {"yes_dollars":
  [...], "no_dollars": [...]}}`. Same NO-bid-as-YES-ask complement mapping
  as the WS snapshot. The response carries **no ticker or sequence
  number** — the caller supplies the ticker, and `seq` is always `None`.

Ticker anatomy, confirmed against real data:
`KXPRESPERSON` (series) → `KXPRESPERSON-28` (event) →
`KXPRESPERSON-28-TGAB` (market).

### Discovery

[`discovery.py`](../../src/arb/venues/kalshi/discovery.py). Discovery goes
through `GET /events?with_nested_markets=true&status=open` (documented,
cursor-paginated, up to `limit=200` per page) rather than raw `GET
/markets`, because the unfiltered `/markets` listing is dominated by
zero-volume `KXMVECROSSCATEGORY-SHARD*` multivariate combo markets —
`/events` excludes multivariate events by design.

- `fetch_liquid_markets(top_n)` — pages through open events (nested markets
  come as full `Market` objects, so the same Pydantic model applies), ranks
  by `volume_24h_fp` descending, returns the top N. Used by `arb record`
  and `arb ui` when `--tickers` isn't given.
- `fetch_universe(max_pages=80)` — every open (event, markets) page,
  unbounded by a top-N cutoff, paced at ~7 req/s (well under the basic read
  tier's 200 tokens/s). This is what feeds the pair matcher — see
  [`pairs.md`](../pairs.md). `event_refs()` converts that raw universe into the
  matcher's `EventRef`/`MarketRef` shape, keeping only `binary` +
  `active` markets.
- `fetch_market` / `fetch_event` / `fetch_series` — single-object lookups,
  each recorded (`sink`) before parsing, used by the DES page's live
  refresh and by `pairs/tracked.py` to resolve fee parameters for confirmed
  pairs.

Every REST call in this module offers its raw response to `sink` (the
recorder's `enqueue`) before calling `response.raise_for_status()` or
parsing — so even a 4xx/5xx response, or one that fails to parse, is
archived.

## DES (market description) payload

[`detail.py`](../../src/arb/venues/kalshi/detail.py). `build_market_detail()`
maps a `KalshiMarket` + optional `KalshiEvent` into the venue-agnostic
dict the terminal UI's DES page renders (see [`ui.md`](../ui.md)) — ticker
anatomy, rules text, settlement sources, status, open/close/expiration
times (ISO), volumes/open-interest (floats — display-only), and
bid/ask/last as ticks. The Polymarket US equivalent
([`../../src/arb/venues/polymarket_us/detail.py`](../../src/arb/venues/polymarket_us/detail.py))
produces the *same* dict shape, so the frontend never branches on venue.

## Rate limits

Token bucket; default request cost 10 tokens; Basic tier 200 read / 100
write tokens/second, higher tiers up to Prestige (10,000/8,000); 429 with no
penalty on limit; live per-account budget via `GET /account/api_limits`.
None of the current code paths get close to this — discovery pages at
`limit=200` for the whole open universe, well under a second's budget.
