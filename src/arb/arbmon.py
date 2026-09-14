"""Arb monitor: quotes every confirmed pair off the live books.

Venue-agnostic. Fee functions are injected per leg (built from each venue's
fee parameters at startup), the books come from :class:`BookManager`, and
the output is a JSON-ready quote per pair: best direction, size the edge
holds for, gross / fees / net, both legs. Measurement only — never orders.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from arb.book import Book
from arb.books import BookManager
from arb.edge import EdgeQuote, FeeFn, best_edge
from arb.types import QTY_PER_CONTRACT


@dataclass(frozen=True, slots=True)
class TrackedPair:
    pair_id: int
    score: float
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_fee: FeeFn
    polymarket_fee: FeeFn
    label: str  # "<event> — <outcome>"
    kalshi_ticker: str = ""
    polymarket_ticker: str = ""
    fee_info: dict[str, Any] = field(default_factory=dict)


class ArbMonitor:
    def __init__(self, books: BookManager, pairs: Sequence[TrackedPair] = ()) -> None:
        self._books = books
        self._pairs: list[TrackedPair] = []
        self._by_market: dict[str, list[TrackedPair]] = {}
        for pair in pairs:
            self.add(pair)

    def add(self, pair: TrackedPair) -> None:
        self._pairs.append(pair)
        for mid in (pair.kalshi_market_id, pair.polymarket_market_id):
            self._by_market.setdefault(mid, []).append(pair)

    @property
    def pairs(self) -> list[TrackedPair]:
        return list(self._pairs)

    @property
    def market_ids(self) -> set[str]:
        return set(self._by_market)

    def affected(self, market_ids: Iterable[str]) -> list[TrackedPair]:
        seen: dict[int, TrackedPair] = {}
        for mid in market_ids:
            for pair in self._by_market.get(mid, ()):
                seen[pair.pair_id] = pair
        return list(seen.values())

    def best_quotes(self, pair: TrackedPair) -> tuple[EdgeQuote, EdgeQuote]:
        """Both directions for one pair off the current books."""
        kb = self._books.get(pair.kalshi_market_id)
        pb = self._books.get(pair.polymarket_market_id)
        return best_edge(
            venue_a="kalshi",
            market_a=pair.kalshi_market_id,
            bids_a=kb.bids() if kb else (),
            asks_a=kb.asks() if kb else (),
            fee_a=pair.kalshi_fee,
            venue_b="polymarket_us",
            market_b=pair.polymarket_market_id,
            bids_b=pb.bids() if pb else (),
            asks_b=pb.asks() if pb else (),
            fee_b=pair.polymarket_fee,
        )

    def quote(self, pair: TrackedPair, *, now_mono_ns: int | None = None) -> dict[str, Any]:
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        kb = self._books.get(pair.kalshi_market_id)
        pb = self._books.get(pair.polymarket_market_id)
        d1, d2 = self.best_quotes(pair)
        best = d1 if d1.net_per_contract_ticks >= d2.net_per_contract_ticks else d2
        return {
            "pair_id": pair.pair_id,
            "label": pair.label,
            "score": pair.score,
            "kalshi": _leg_state(kb, pair.kalshi_market_id, pair.kalshi_ticker, now),
            "polymarket_us": _leg_state(pb, pair.polymarket_market_id, pair.polymarket_ticker, now),
            "fee_info": pair.fee_info,
            "best": _quote_payload(best),
            "other": _quote_payload(d2 if best is d1 else d1),
            "ts_ms": time.time_ns() // 1_000_000,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic_ns()
        quotes = [self.quote(p, now_mono_ns=now) for p in self._pairs]
        quotes.sort(key=lambda q: -q["best"]["net_per_contract_ticks"])
        return quotes


def _leg_state(book: Book | None, market_id: str, ticker: str, now_mono_ns: int) -> dict[str, Any]:
    if book is None:
        return {
            "market_id": market_id,
            "ticker": ticker,
            "has_book": False,
            "valid": False,
            "reason": "no_book",
            "best_bid": None,
            "best_ask": None,
        }
    status = book.status(now_mono_ns=now_mono_ns)
    bb, ba = book.best_bid(), book.best_ask()
    return {
        "market_id": market_id,
        "ticker": ticker,
        "has_book": True,
        "valid": status.valid,
        "reason": status.reason.value if status.reason else None,
        "best_bid": [bb.price, bb.qty] if bb else None,
        "best_ask": [ba.price, ba.qty] if ba else None,
    }


def _quote_payload(q: EdgeQuote) -> dict[str, Any]:
    return {
        "direction": q.direction,
        "qty": q.qty,
        "contracts": q.qty / QTY_PER_CONTRACT,
        "gross_per_contract_ticks": q.gross_per_contract_ticks,
        "fee_per_contract_ticks": (q.fee_ticks * QTY_PER_CONTRACT) // q.qty if q.qty else 0,
        "net_per_contract_ticks": q.net_per_contract_ticks,
        "net_ticks": q.net_ticks,
        "fee_ticks": q.fee_ticks,
        "legs": [
            {
                "venue": leg.venue,
                "market_id": leg.market_id,
                "side": leg.side,
                "worst_price": leg.worst_price,
                "qty": leg.qty,
                "fee_ticks": leg.fee_ticks,
            }
            for leg in q.legs
        ],
    }
