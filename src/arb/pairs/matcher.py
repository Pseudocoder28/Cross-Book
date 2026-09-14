"""Propose Kalshi ↔ Polymarket US market pairs.

Two stages, both explainable:

1. Event pairing — blocked by shared informative title tokens, scored by
   title similarity plus *outcome overlap* (how many of one event's outcomes
   have a name twin on the other side). Outcome overlap is what separates a
   baseball pennant from a chess league with the same word "champion".
2. Market pairing within matched events — outcome names matched one-to-one.

Dates are a weak feature only: Kalshi ``close_time`` often trails the real
event by months. Rules text is *not* scored — it is carried in the detail
for the human reviewer, because that is where equivalence actually lives.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from arb.pairs.text import name_similarity, name_tokens, title_tokens

MIN_EVENT_SCORE = 0.40
MIN_NAME_SIM = 0.75
MAX_TOKEN_DF = 400  # tokens this common don't block ("election" tier)
DATE_SLACK_DAYS = 400.0


@dataclass(frozen=True, slots=True)
class MarketRef:
    """One tradable outcome on one venue, as the matcher sees it."""

    venue: str
    market_id: str
    ticker: str
    outcome: str
    rules: str
    close_time: datetime | None
    series_ticker: str = ""  # Kalshi: fee_type/fee_multiplier live on the series
    fee_coefficient: str | None = None  # Polymarket US: per-market taker Θ
    # The venue's *event* identifier. Both sites address markets by event, not
    # by market slug/ticker, so this is what a human-facing link needs.
    event_slug: str = ""


@dataclass(frozen=True, slots=True)
class EventRef:
    venue: str
    event_id: str
    title: str
    category: str
    markets: tuple[MarketRef, ...]
    end_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class PairCandidate:
    kalshi: MarketRef
    polymarket: MarketRef
    score: float
    features: dict[str, Any] = field(default_factory=dict)
    kalshi_event: str = ""
    polymarket_event: str = ""


def _days_apart(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 86400.0


def _date_feature(days: float | None) -> float:
    if days is None:
        return 0.5
    return max(0.0, 1.0 - days / DATE_SLACK_DAYS)


def _weighted_jaccard(a: frozenset[str], b: frozenset[str], idf: dict[str, float]) -> float:
    """Jaccard where each token counts by rarity, so "silver slugger" outweighs
    the "american league" both titles happen to share."""
    if not a or not b:
        return 0.0
    union = sum(idf.get(t, 1.0) for t in a | b)
    return sum(idf.get(t, 1.0) for t in a & b) / union if union else 0.0


def _outcome_matches(
    a: tuple[MarketRef, ...], b: tuple[MarketRef, ...]
) -> list[tuple[MarketRef, MarketRef, float]]:
    """Greedy one-to-one outcome matching by name similarity, best first."""
    ta = [(m, name_tokens(m.outcome)) for m in a]
    tb = [(m, name_tokens(m.outcome)) for m in b]
    scored: list[tuple[float, int, int]] = []
    for i, (_, na) in enumerate(ta):
        for j, (_, nb) in enumerate(tb):
            sim = name_similarity(na, nb)
            if sim >= MIN_NAME_SIM:
                scored.append((sim, i, j))
    scored.sort(key=lambda t: -t[0])
    used_a: set[int] = set()
    used_b: set[int] = set()
    out: list[tuple[MarketRef, MarketRef, float]] = []
    for sim, i, j in scored:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        out.append((ta[i][0], tb[j][0], sim))
    return out


def propose_pairs(
    kalshi_events: list[EventRef],
    polymarket_events: list[EventRef],
    *,
    min_score: float = 0.5,
) -> list[PairCandidate]:
    # Inverted index over Kalshi title tokens for blocking; document
    # frequencies across both venues drive IDF weights.
    k_tokens = [title_tokens(e.title) for e in kalshi_events]
    p_tokens_all = [title_tokens(e.title) for e in polymarket_events]
    df: dict[str, int] = defaultdict(int)
    for toks in (*k_tokens, *p_tokens_all):
        for t in toks:
            df[t] += 1
    n_docs = max(1, len(k_tokens) + len(p_tokens_all))
    idf = {t: math.log(1.0 + n_docs / d) for t, d in df.items()}
    index: dict[str, list[int]] = defaultdict(list)
    for i, toks in enumerate(k_tokens):
        for t in toks:
            if df[t] <= MAX_TOKEN_DF:
                index[t].append(i)

    candidates: list[PairCandidate] = []
    for pe, p_toks in zip(polymarket_events, p_tokens_all, strict=True):
        if not p_toks:
            continue
        block: set[int] = set()
        for t in p_toks:
            block.update(index.get(t, ()))
        for ki in block:
            ke = kalshi_events[ki]
            title_sim = _weighted_jaccard(p_toks, k_tokens[ki], idf)
            matches = _outcome_matches(ke.markets, pe.markets)
            if not matches:
                continue
            overlap = len(matches) / min(len(ke.markets), len(pe.markets))
            days = _days_apart(ke.end_time, pe.end_time)
            event_score = 0.55 * title_sim + 0.35 * overlap + 0.10 * _date_feature(days)
            if event_score < MIN_EVENT_SCORE:
                continue
            for km, pm, sim in matches:
                score = 0.6 * event_score + 0.4 * sim
                if score < min_score:
                    continue
                candidates.append(
                    PairCandidate(
                        kalshi=km,
                        polymarket=pm,
                        score=round(score, 4),
                        features={
                            "title_similarity": round(title_sim, 4),
                            "outcome_overlap": round(overlap, 4),
                            "outcome_similarity": round(sim, 4),
                            "days_apart": round(days, 1) if days is not None else None,
                            "event_score": round(event_score, 4),
                        },
                        kalshi_event=ke.title,
                        polymarket_event=pe.title,
                    )
                )
    # One proposal per Polymarket market: keep the best-scoring Kalshi twin.
    best: dict[str, PairCandidate] = {}
    for c in candidates:
        cur = best.get(c.polymarket.market_id)
        if cur is None or c.score > cur.score:
            best[c.polymarket.market_id] = c
    return sorted(best.values(), key=lambda c: -c.score)
