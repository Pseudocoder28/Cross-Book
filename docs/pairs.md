# Cross-venue pair matcher

Code: [`src/arb/pairs/`](../src/arb/pairs/). This is how the system decides
which Kalshi market and which Polymarket US market are betting on the same
real-world outcome — the foundation everything in [`engine.md`](engine.md)
is built on.

## Why a matcher proposes instead of deciding

Market equivalence lives in the *resolution rules*, and no lexical score can
judge that reliably: Kalshi's presidential-succession market might resolve
on who is **inaugurated**, while a superficially identical-sounding market
on another venue resolves on who **wins the election** — those are not the
same bet, even though every title token matches. So the matcher's job is to
propose good *candidates* with full transparency into why they scored the
way they did, and a human makes the final call. Only pairs a human has
`confirmed` ever feed the fee/edge engine — a `proposed` pair, no matter how
high its score, is inert.

## Two-stage scoring

[`matcher.py`](../src/arb/pairs/matcher.py), `propose_pairs()`. Runs in two
stages, each independently explainable:

### 1. Event pairing

Every Kalshi event and every Polymarket US event is reduced to a
`frozenset` of title tokens (`title_tokens()`, see "Text normalization"
below). Two features combine into an `event_score`:

- **Title similarity** — an IDF-weighted Jaccard: `_weighted_jaccard(a, b,
  idf)`, where each shared token contributes by its inverse document
  frequency across *both* venues' event titles rather than counting
  equally. This is what stops "American League" (extremely common across
  hundreds of MLB markets) from outweighing "Silver Slugger" (the token
  that actually identifies *which* MLB award market this is) when scoring
  two titles that share both.
- **Outcome overlap** — `len(matches) / min(len(ke.markets), len(pe.markets))`,
  where `matches` is the one-to-one outcome pairing computed in stage 2
  (yes, stage 2 runs *inside* stage 1's scoring loop — see the code). This
  is the feature that separates a real pennant race (30 team names,
  matching in both event's outcome lists) from a chess league whose event
  title happens to also contain the word "champion": if outcome names don't
  actually pair up, the overlap term collapses to near zero regardless of
  how similar the titles read.
- **Date proximity** — a *weak* feature (weight 0.10) on purpose: Kalshi's
  `close_time` can trail the real-world event by up to a year (season-long
  markets closing long after the season ends), so leaning on dates would
  systematically punish exactly the long-running markets most likely to
  have a genuine cross-venue twin. Missing a date on either side scores a
  neutral 0.5 rather than zero.

```
event_score = 0.55 * title_similarity + 0.35 * outcome_overlap + 0.10 * date_feature
```

Events below `MIN_EVENT_SCORE` (0.40) are dropped before any market-level
scoring happens.

### 2. Market (outcome) pairing within a matched event

`_outcome_matches()` — greedy one-to-one matching by outcome-name
similarity (`name_similarity()`, on `name_tokens()`), highest-similarity
pairs claimed first, `MIN_NAME_SIM = 0.75` floor. `name_similarity` is
containment-aware, not plain Jaccard: `"Dodgers"` inside `"Los Angeles
Dodgers"` should score high even though token *sets* barely overlap by
count, so the similarity is `max(containment, jaccard)` where containment
is `|intersection| / min(|a|, |b|)`. A single shared token is only treated
as decisive when it dominates one side entirely (`inter == 1 and
min(len(a), len(b)) > 1` falls back to plain Jaccard, so a one-word overlap
between two multi-word names doesn't over-score).

The final per-market score blends the event-level and outcome-level
signals:

```
score = 0.6 * event_score + 0.4 * outcome_similarity
```

Candidates below the caller's `min_score` (CLI default 0.75) are dropped.
For any Polymarket US market that matched multiple Kalshi candidates, only
the best-scoring one survives (`propose_pairs` keeps one proposal per
Polymarket market id).

**Rules text is deliberately never scored** — it rides along in the
proposal's `detail` purely for the human reviewer, because judging
resolution-condition equivalence from free text is exactly the kind of
subtle call the matcher shouldn't pretend to automate.

## Text normalization

[`text.py`](../src/arb/pairs/text.py). Deterministic, dependency-free (no
ML, no LLM — reproducible and free, and because a human reviews every
proposal anyway, the goal is high recall with an explainable score, not
cleverness):

- `fold()` — lowercase, Unicode NFKD accent-folding, punctuation collapsed
  to spaces.
- `title_tokens()` — expands venue shorthand first via `TITLE_SYNONYMS`
  (`"nl"` → `"national league"`, `"mvp"`, `"gov"` → `"governor"`, etc.),
  then folds and drops `TITLE_STOPWORDS` (generic election/race vocabulary
  that carries no identifying signal — "winner", "election", "2026", ...)
  and single-character tokens.
- `name_tokens()` — strips parenthetical decoration (`"(D)"`, `"(Ind)"`)
  and name suffixes (`Jr.`, `Sr.`, `II`/`III`/`IV`) via regex before
  folding, then drops `NAME_STOPWORDS` (`fc`, `sc`, `party`, ...) — this is
  what lets `"Alex Smith Jr. (D)"` and `"Alex Smith"` match cleanly.

## Blocking: making 6,000 × 3,500 events tractable

`propose_pairs()` doesn't compare every Kalshi event against every
Polymarket US event (that's ~21M pairs at real universe sizes). Instead it
builds an inverted index from informative title tokens to the Kalshi events
that contain them (`index: token -> [kalshi event indices]`), then for each
Polymarket US event, unions the index lookups across its own title tokens
to get a small candidate block. Tokens common enough to be
useless for blocking (`df[t] > MAX_TOKEN_DF = 400`, roughly "election"-tier
frequency) are excluded from the index entirely, so they don't explode a
block's size while still counting toward IDF weighting in the score itself.
Measured on the real universes (6,000 Kalshi events × 3,563 Polymarket US
events, 88k markets): full evaluation in about 0.5 seconds.

## Storage and the human review workflow

[`store.py`](../src/arb/pairs/store.py) — the `pairs` table (schema in
[`data-model.md`](data-model.md#pairs-migration-0002)).

- `upsert_proposals()` — `INSERT ... ON CONFLICT (kalshi_market_id,
  polymarket_market_id) DO UPDATE SET score=..., detail=...`. Re-running the
  proposer refreshes score and detail for an already-seen pair but **never
  touches `status` or `decided_at`** — a human's confirm/reject decision is
  permanent until explicitly undone (`arb pairs` has no "undo" CLI command
  yet, but the underlying `decide()` function accepts any status including
  reverting to `"proposed"`, and the terminal UI's `U` key does exactly
  that). Chunked at `UPSERT_CHUNK = 2000` rows per statement — asyncpg caps
  bind parameters at 32,767 per statement, and each row binds 5 params, so
  10,000 params per chunk stays comfortably under that.
- `list_pairs(status=...)` — sorted by score descending, optionally filtered
  by `proposed`/`confirmed`/`rejected`.
- `decide(pair_id, status)` / `decide_many(pair_ids, status)` — the single-
  and batch-decision entry points. Batch decisions matter in practice: a
  30-team pennant race is naturally reviewed and confirmed as one judgment
  ("yes, this whole event is well matched"), not thirty separate clicks —
  the terminal UI's `PAIRS` screen exposes this as Shift+Y/Shift+N on a
  selected event.

## Fetching the universes and running the proposer

[`run.py`](../src/arb/pairs/run.py), `propose()` — the `arb pairs propose`
entry point:

1. Fetch the full open Kalshi universe (`kalshi.discovery.fetch_universe`,
   up to 80 pages) and the full active Polymarket US universe
   (`polymarket_us.discovery.fetch_active_markets`, up to 12 pages) —
   **every page recorded before parsing**, same rule as everywhere else in
   the system. Recording can be disabled with `--no-record` for a
   dry-run-style scoring pass.
2. Convert each to the matcher's `EventRef`/`MarketRef` shape via each
   venue's `event_refs()`.
3. `propose_pairs(kalshi_refs, pm_refs, min_score=...)`.
4. `upsert_proposals()` into Postgres.

`format_candidates()` / `format_rows()` render a plain-text table for CLI
output (`arb pairs propose`, `arb pairs list`).

## Resolving fee parameters for confirmed pairs: `tracked.py`

[`tracked.py`](../src/arb/pairs/tracked.py), `load_tracked_pairs()`. Shared
by `arb ui` (the live ARB screen) and `arb replay`, so both quote with
*identical* fee models — there is exactly one function that turns a
confirmed `pairs` row into a fully fee-resolved `TrackedPair`:

1. Load the top `top_n` confirmed pairs by score.
2. `GET /v1/markets?slug=...` (batched, one call) resolves every tracked
   Polymarket US market's `feeCoefficient` in one round trip.
3. Per Kalshi leg: `fetch_market` → `fetch_event` → `fetch_series`
   (series responses cached per `series_ticker` across pairs, since many
   confirmed pairs share a series — e.g. every NFL game). Event-level
   `fee_type_override`/`fee_multiplier_override` take precedence over the
   series defaults, per the documented precedence rule.
4. Build a `TrackedPair` with `kalshi_fee`/`polymarket_fee` as
   `functools.partial`-bound fee functions ready to call with just
   `(qty, price)` — see [`engine.md`](engine.md#fees) for the fee formulas
   themselves.

Every REST call this makes is recorded before parsing, same as discovery.
This is a live-only lookup — fee parameters are **not** stored in `pairs`
or `raw_messages` in a way `arb replay` reads back for fees; replay calls
`load_tracked_pairs` itself, live, at replay time (see
[`engine.md`](engine.md#replay)), so a fee-schedule change on either venue
is reflected in a fresh replay even of old book data.

## Human review in the terminal UI

The `PAIRS` command in `arb ui` (see [`ui.md`](ui.md)) lists proposals
sorted by score, shows both legs side by side with their full rules text
and the scoring features that produced the match, and lets a reviewer:

- `↑`/`↓` select a candidate
- `Y` / `N` confirm / reject the selected pair
- `Shift+Y` / `Shift+N` confirm / reject *every* proposal belonging to the
  same event pairing at once
- `U` undo a decision back to `proposed`
- `TAB` cycle the `proposed` / `confirmed` / `rejected` / `all` filter

Backed by `GET /api/pairs`, `POST /api/pairs/{id}/decide`, and
`POST /api/pairs/decide` (the batch form) on the FastAPI server.
