"""Kalshi REST parser tests against real captured payloads (never invented)."""

from pathlib import Path

import pytest

from arb.book import Book, Level
from arb.interfaces import ParseError
from arb.types import RawMessage
from arb.venues.kalshi.rest import (
    market_id,
    parse_markets_response,
    parse_orderbook_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"


def raw(payload: bytes, stream: str = "rest") -> RawMessage:
    return RawMessage(
        venue="kalshi",
        stream=stream,
        payload=payload,
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id="testrun",
        ingest_seq=0,
    )


class TestParseMarkets:
    def test_parses_captured_markets_page(self) -> None:
        payload = (FIXTURES / "rest_markets_open_limit5.json").read_bytes()
        markets, cursor = parse_markets_response(raw(payload))
        assert len(markets) == 5
        assert cursor  # more pages existed
        for market in markets:
            assert market.ticker
            assert market.event_ticker
            assert market.market_type == "binary"
            assert market.status == "active"
            assert market.close_time is not None

    def test_garbage_payload_raises_parse_error(self) -> None:
        with pytest.raises(ParseError):
            parse_markets_response(raw(b"not json"))
        with pytest.raises(ParseError):
            parse_markets_response(raw(b"{}"))


class TestParseOrderbook:
    def test_normalizes_captured_book(self) -> None:
        payload = (FIXTURES / "rest_orderbook_kxwc-30-por.json").read_bytes()
        snap = parse_orderbook_response(raw(payload), ticker="KXWC-30-POR")

        assert snap.market_id == market_id("KXWC-30-POR")
        assert snap.seq is None
        # The captured book has 19 YES-bid levels and 37 NO-bid levels.
        assert len(snap.bids) == 19
        assert len(snap.asks) == 37
        # First captured YES bid: ["0.0010", "15000.00"].
        assert Level(10, 150_000_000) in snap.bids
        # First captured NO bid ["0.0100", "4903190.79"] → YES ask at the
        # complement, with the fractional count carried exactly.
        assert Level(9900, 49_031_907_900) in snap.asks

    def test_captured_book_applies_cleanly_and_is_uncrossed(self) -> None:
        payload = (FIXTURES / "rest_orderbook_kxwc-30-por.json").read_bytes()
        snap = parse_orderbook_response(raw(payload), ticker="KXWC-30-POR")
        book = Book(snap.market_id, staleness_limit_ns=10**9)
        assert book.apply_snapshot(snap, mono_ns=0).valid
        best_bid, best_ask = book.best_bid(), book.best_ask()
        assert best_bid is not None and best_ask is not None
        # Best captured YES bid 0.0410; best NO bid 0.9430 → YES ask 0.0570.
        assert best_bid == Level(410, 62_512_200)
        assert best_ask == Level(570, 460_000)
        # NO view: best NO bid is the complement of the best YES ask.
        assert book.no_bids()[0] == Level(9430, 460_000)

    def test_garbage_payload_raises_parse_error(self) -> None:
        with pytest.raises(ParseError):
            parse_orderbook_response(raw(b"not json"), ticker="X")
        with pytest.raises(ParseError):
            parse_orderbook_response(raw(b"{}"), ticker="X")
