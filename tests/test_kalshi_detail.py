"""Market/event parsers and the DES payload, against real captured payloads."""

from pathlib import Path

import pytest

from arb.interfaces import ParseError
from arb.types import RawMessage
from arb.venues.kalshi.detail import build_market_detail
from arb.venues.kalshi.rest import (
    KalshiEvent,
    KalshiMarket,
    parse_event_response,
    parse_market_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"


def raw(payload: bytes, stream: str) -> RawMessage:
    return RawMessage(
        venue="kalshi",
        stream=stream,
        payload=payload,
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id="testrun",
        ingest_seq=0,
    )


def load_market() -> KalshiMarket:
    return parse_market_response(
        raw((FIXTURES / "rest_market_kxpresperson-28-tgab.json").read_bytes(), "rest:market")
    )


def load_event() -> KalshiEvent:
    return parse_event_response(
        raw((FIXTURES / "rest_event_kxpresperson-28.json").read_bytes(), "rest:event")
    )


def test_parses_captured_market() -> None:
    market = load_market()
    assert market.ticker == "KXPRESPERSON-28-TGAB"
    assert market.event_ticker == "KXPRESPERSON-28"
    assert market.status == "active"
    assert market.yes_sub_title == "Tulsi Gabbard"
    assert market.rules_primary.startswith("If Tulsi Gabbard is the next person inaugurated")
    assert market.expected_expiration_time is not None
    assert market.expected_expiration_time.year == 2029
    assert market.volume_fp == "2000797.00"
    assert market.yes_bid_dollars == "0.0040"


def test_parses_captured_event() -> None:
    event = load_event()
    assert event.event_ticker == "KXPRESPERSON-28"
    assert event.series_ticker == "KXPRESPERSON"
    assert event.title == "2028 U.S. Presidential Election winner?"
    assert event.category == "Elections"
    assert event.mutually_exclusive is True
    assert [s.name for s in event.settlement_sources] == ["Office of the Presidency"]


def test_detail_payload_uses_ticks_and_floats() -> None:
    detail = build_market_detail(load_market(), load_event(), source="live", fetched_at_ms=123)
    assert detail["market_id"] == "kalshi:KXPRESPERSON-28-TGAB"
    assert detail["series_ticker"] == "KXPRESPERSON"
    assert detail["event_title"] == "2028 U.S. Presidential Election winner?"
    # "0.0040" dollars → 40 ticks (0.40¢); "0.0070" last → 70 ticks.
    assert detail["yes_bid_ticks"] == 40
    assert detail["yes_ask_ticks"] == 60
    assert detail["last_price_ticks"] == 70
    assert detail["volume"] == 2000797.0
    assert detail["open_interest"] == pytest.approx(1687417.73)
    assert detail["settlement_sources"][0]["url"].startswith("https://www.whitehouse.gov")
    assert detail["source"] == "live" and detail["fetched_at_ms"] == 123


def test_detail_without_event_is_still_complete() -> None:
    detail = build_market_detail(load_market(), None, source="discovery", fetched_at_ms=1)
    assert detail["event_title"] == ""
    assert detail["settlement_sources"] == []
    assert detail["mutually_exclusive"] is None


def test_garbage_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_market_response(raw(b"{}", "rest:market"))
    with pytest.raises(ParseError):
        parse_event_response(raw(b"not json", "rest:event"))
