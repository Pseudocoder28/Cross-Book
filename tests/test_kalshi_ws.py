"""Kalshi WS parser tests against real captured frames (never invented).

The capture (2026-09-14, production WS, one subscription over five markets)
also pins observed protocol facts: the envelope ``seq`` is per-subscription
and shared across markets, so book-level seq contiguity cannot be used with
multi-market subscriptions — the source tracks seq per ``sid`` and hands
books unsequenced events.
"""

import dataclasses
import itertools
import json
from pathlib import Path

import pytest

from arb.book import Book, BookLevelUpdate, BookSnapshot, UpdateMode
from arb.interfaces import ParseError
from arb.types import BookSide, RawMessage
from arb.venues.kalshi.ws import parse_ws_message, subscribe_orderbook_cmd

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"


def frames() -> list[bytes]:
    return [line for line in FIXTURE.read_bytes().split(b"\n") if line.strip()]


def raw(payload: bytes) -> RawMessage:
    return RawMessage(
        venue="kalshi",
        stream="ws",
        payload=payload,
        recv_ts_ns=1,
        recv_mono_ns=1,
        run_id="testrun",
        ingest_seq=0,
    )


def test_subscribe_cmd_matches_documented_format() -> None:
    cmd = json.loads(subscribe_orderbook_cmd(7, ["AAA", "BBB"]))
    assert cmd == {
        "id": 7,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": ["AAA", "BBB"]},
    }


def test_capture_parses_completely() -> None:
    all_events = [parse_ws_message(raw(frame)) for frame in frames()]
    # First frame is the "subscribed" ack → no book events.
    assert all_events[0] == []
    flat = [event for events in all_events for event in events]
    snapshots = [e for e in flat if isinstance(e, BookSnapshot)]
    deltas = [e for e in flat if isinstance(e, BookLevelUpdate)]
    assert len(snapshots) == 5
    assert len(deltas) == 15
    assert all(e.market_id.startswith("kalshi:KXPRESPERSON-28-") for e in flat)
    assert all(e.mode is UpdateMode.DELTA for e in deltas)


def test_envelope_seq_is_per_subscription_not_per_market() -> None:
    seqs = [json.loads(frame)["seq"] for frame in frames() if b'"seq"' in frame]
    # One counter across the whole subscription…
    assert seqs == list(range(1, 21))
    # …which means per-market seqs have gaps (here: snapshot 1, first delta 6).
    per_market: dict[str, list[int]] = {}
    for frame in frames():
        doc = json.loads(frame)
        if "seq" in doc:
            per_market.setdefault(doc["msg"]["market_ticker"], []).append(doc["seq"])
    assert any(any(b - a > 1 for a, b in itertools.pairwise(seqs)) for seqs in per_market.values())


def test_first_captured_delta_normalizes_exactly() -> None:
    # sid=1 seq=6: KXPRESPERSON-28-GNEWS side=yes px=0.0600 delta=-155.00
    delta_frame = next(frame for frame in frames() if b'"type":"orderbook_delta"' in frame)
    (event,) = parse_ws_message(raw(delta_frame))
    assert isinstance(event, BookLevelUpdate)
    assert event.market_id == "kalshi:KXPRESPERSON-28-GNEWS"
    assert event.side is BookSide.BID  # side "yes" → YES bid ladder
    assert event.price == 600
    assert event.qty == -1_550_000  # "-155.00" in 0.0001-contract units
    assert event.seq == 6


def test_captured_stream_applies_cleanly_unsequenced() -> None:
    """Replay the whole capture into Books the way the source will feed them:
    seq stripped (it's subscription-scoped), every apply must stay valid."""
    books: dict[str, Book] = {}
    for frame in frames():
        for event in parse_ws_message(raw(frame)):
            event = dataclasses.replace(event, seq=None)
            book = books.setdefault(
                event.market_id, Book(event.market_id, staleness_limit_ns=10**9)
            )
            if isinstance(event, BookSnapshot):
                status = book.apply_snapshot(event, mono_ns=0)
            else:
                status = book.apply_level(event, mono_ns=0)
            assert status.valid, (event.market_id, status.reason)
    assert len(books) == 5
    for book in books.values():
        best_bid, best_ask = book.best_bid(), book.best_ask()
        if best_bid is not None and best_ask is not None:
            assert best_bid.price < best_ask.price


def test_garbage_payload_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_ws_message(raw(b"not json"))
    with pytest.raises(ParseError):
        parse_ws_message(raw(b'{"type": "orderbook_delta"}'))


NO_SIDE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture_no_side.jsonl"
)


def test_no_side_delta_folds_into_the_yes_ask_ladder() -> None:
    """A NO-bid change at price p is a YES-ask change at 10000 - p (real frame,
    captured 2026-09-14: KXNEXTPRESSEC-29JAN21-MBAR, side "no", 0.7500, -35.32)."""
    frames = [f for f in NO_SIDE_FIXTURE.read_bytes().split(b"\n") if f.strip()]
    no_frames = [f for f in frames if b'"side":"no"' in f]
    assert len(no_frames) >= 1
    (event,) = parse_ws_message(raw(no_frames[0]))
    assert isinstance(event, BookLevelUpdate)
    assert event.market_id == "kalshi:KXNEXTPRESSEC-29JAN21-MBAR"
    assert event.side is BookSide.ASK
    assert event.price == 10_000 - 7500
    assert event.qty == -353_200  # "-35.32" contracts in 0.0001 units
    # The whole capture replays cleanly into books, NO-side deltas included.
    books: dict[str, Book] = {}
    for frame in frames:
        for ev in parse_ws_message(raw(frame)):
            ev = dataclasses.replace(ev, seq=None)
            book = books.setdefault(ev.market_id, Book(ev.market_id, staleness_limit_ns=10**9))
            status = (
                book.apply_snapshot(ev, mono_ns=0)
                if isinstance(ev, BookSnapshot)
                else book.apply_level(ev, mono_ns=0)
            )
            assert status.valid, (ev.market_id, status.reason)
    assert len(books) == 12
