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

from arb.book import Book, Level
from arb.books import BookManager
from arb.edge import EdgeQuote, FeeFn, best_edge
from arb.taken import TakenLiquidity
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

    def best_quotes(
        self, pair: TrackedPair, *, taken: TakenLiquidity | None = None
    ) -> tuple[EdgeQuote, EdgeQuote]:
        """Both directions for one pair off the current books.

        With ``taken``, the ladders are first netted of the size paper fills
        have already consumed: /arb shows the market as it is, the paper
        trader is shown the market as it would be had its fills been real.
        """
        kb = self._books.get(pair.kalshi_market_id)
        pb = self._books.get(pair.polymarket_market_id)
        k, p = pair.kalshi_market_id, pair.polymarket_market_id

        def side(book: Book | None, market_id: str, which: str) -> Sequence[Level]:
            if book is None:
                return ()
            levels = book.bids() if which == "bid" else book.asks()
            return taken.net(market_id, which, levels) if taken is not None else levels

        return best_edge(
            venue_a="kalshi",
            market_a=k,
            bids_a=side(kb, k, "bid"),
            asks_a=side(kb, k, "ask"),
            fee_a=pair.kalshi_fee,
            venue_b="polymarket_us",
            market_b=p,
            bids_b=side(pb, p, "bid"),
            asks_b=side(pb, p, "ask"),
            fee_b=pair.polymarket_fee,
        )

    def consume(self, pair: TrackedPair, direction: str, qty: int, taken: TakenLiquidity) -> None:
        """Record a paper fill against the levels it took, on both legs.

        ``yes_a_no_b`` lifts Kalshi's asks and hits Polymarket's YES bids;
        ``yes_b_no_a`` is the mirror.
        """
        ask_mid, bid_mid = (
            (pair.kalshi_market_id, pair.polymarket_market_id)
            if direction == "yes_a_no_b"
            else (pair.polymarket_market_id, pair.kalshi_market_id)
        )
        ask_book, bid_book = self._books.get(ask_mid), self._books.get(bid_mid)
        if ask_book is not None:
            taken.consume(ask_mid, "ask", ask_book.asks(), qty)
        if bid_book is not None:
            taken.consume(bid_mid, "bid", bid_book.bids(), qty)

    def untradable(self, pair: TrackedPair, *, now_mono_ns: int) -> str | None:
        """Why this pair must not be traded right now, or None.

        A missing book, or one that is structurally wrong — a sequence gap, a
        crossed book, a bad level — is a ladder nobody should price off until
        it resyncs. ``stale`` alone is not structural: on a streamed venue a
        quiet book is still the book.
        """
        for venue, market_id in (
            ("kalshi", pair.kalshi_market_id),
            ("polymarket_us", pair.polymarket_market_id),
        ):
            book = self._books.get(market_id)
            if book is None:
                return f"{venue}: no book"
            status = book.status(now_mono_ns=now_mono_ns)
            if not status.valid and status.reason is not None and status.reason.value != "stale":
                return f"{venue}: {status.reason.value}"
        return None

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
