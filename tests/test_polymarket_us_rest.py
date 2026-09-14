"""Polymarket US REST parser tests against real captured payloads (never invented)."""

from decimal import Decimal
from pathlib import Path

import pytest

from arb.book import Book, Level
from arb.interfaces import ParseError
from arb.types import RawMessage
from arb.venues.polymarket_us.rest import (
    market_id,
    parse_book_response,
    parse_markets_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "polymarket_us"
SLUG = "tec-mlb-nlchamp-2026-09-27-nym"


def raw(payload: bytes, stream: str = "rest") -> RawMessage:
    return RawMessage(
        venue="polymarket_us",
        stream=stream,
        payload=payload,
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id="testrun",
        ingest_seq=0,
    )


class TestParseMarkets:
    def test_parses_captured_markets_page(self) -> None:
        payload = (FIXTURES / "rest_markets_limit2.json").read_bytes()
        markets = parse_markets_response(raw(payload))
        assert len(markets) == 2

        first = markets[0]
        assert first.slug == SLUG
        assert first.id == "7898"
        assert first.question == "National League Champion"
        assert first.category == "sports"
        assert first.active and not first.closed
        assert first.status == "MARKET_STATUS_OPEN"
        assert first.start_date is not None and first.end_date is not None
        # Exact decimals — no float contamination for tick size or fees.
        assert first.order_price_min_tick_size == Decimal("0.001")
        assert first.fee_coefficient == Decimal("0.06")
        assert first.minimum_trade_qty == Decimal("1")

    def test_garbage_payload_raises_parse_error(self) -> None:
        with pytest.raises(ParseError):
            parse_markets_response(raw(b"not json"))
        with pytest.raises(ParseError):
            parse_markets_response(raw(b"{}"))


class TestParseBook:
    def test_normalizes_captured_book(self) -> None:
        payload = (FIXTURES / f"rest_book_{SLUG}.json").read_bytes()
        snap = parse_book_response(raw(payload))

        assert snap.market_id == market_id(SLUG)
        assert snap.seq is None
        # Captured book: one bid ["0.0010" x "45.0000"], offers from 0.0020.
        assert snap.bids == (Level(10, 450_000),)
        assert snap.asks[0] == Level(20, 130_020_000)

    def test_captured_book_applies_cleanly_and_is_uncrossed(self) -> None:
        payload = (FIXTURES / f"rest_book_{SLUG}.json").read_bytes()
        snap = parse_book_response(raw(payload))
        book = Book(snap.market_id, staleness_limit_ns=10**9)
        assert book.apply_snapshot(snap, mono_ns=0).valid
        best_bid, best_ask = book.best_bid(), book.best_ask()
        assert best_bid == Level(10, 450_000)
        assert best_ask == Level(20, 130_020_000)
        # Buying NO is selling YES: NO ask = complement of the YES bid.
        assert book.no_asks()[0] == Level(9990, 450_000)

    def test_garbage_payload_raises_parse_error(self) -> None:
        with pytest.raises(ParseError):
            parse_book_response(raw(b"not json"))
        with pytest.raises(ParseError):
            parse_book_response(raw(b"{}"))


class TestEventsAndStats:
    def test_parses_captured_events_page(self) -> None:
        from arb.venues.polymarket_us.rest import parse_events_response

        payload = (FIXTURES / "rest_events_active_subset.json").read_bytes()
        pairs = parse_events_response(raw(payload))
        assert len(pairs) == 2
        event, markets = pairs[0]
        assert event.slug == "usho-midterms-2026-11-03"
        assert event.category == "politics"
        assert event.series_slug == "us-midterms-2026"
        assert [m.slug for m in markets] == [
            "paccc-usho-midterms-2026-11-03-dem",
            "paccc-usho-midterms-2026-11-03-rep",
        ]
        assert markets[0].question == "U.S House Midterm Winner"
        assert markets[0].description  # rules text rides along in nested markets

    def test_parses_book_stats(self) -> None:
        from arb.venues.polymarket_us.rest import parse_book_stats

        payload = (FIXTURES / f"rest_book_{SLUG}.json").read_bytes()
        stats = parse_book_stats(raw(payload))
        assert stats.slug == SLUG
        assert stats.state == "MARKET_STATE_OPEN"
        assert stats.shares_traded == 314_590_000  # "31459.0000" in 0.0001-contract units
        assert stats.open_interest == 1_998_350_000
        assert stats.last_trade_ticks == 20  # "0.0020"
        assert stats.transact_time is not None and stats.transact_time.year == 2026


class TestSelection:
    def test_non_sports_first(self) -> None:
        from arb.venues.polymarket_us.discovery import DiscoveredPMMarket, select_poll_targets
        from arb.venues.polymarket_us.rest import parse_events_response

        payload = (FIXTURES / "rest_events_active_subset.json").read_bytes()
        found = [
            DiscoveredPMMarket(slug=m.slug, title=m.question, market=m, event=e)
            for e, ms in parse_events_response(raw(payload))
            for m in ms
        ]
        # Fixture lists the politics event first anyway; reverse to prove ordering.
        chosen = select_poll_targets(list(reversed(found)), 3)
        assert [m.market.category for m in chosen] == ["politics", "politics", "sports"]
