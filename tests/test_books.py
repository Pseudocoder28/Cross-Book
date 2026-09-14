"""BookManager tests: real captured Kalshi frames replayed through the
adapter, including a simulated subscription-level seq gap."""

from pathlib import Path

from arb.book import Book, BookSnapshot, InvalidReason, Level
from arb.books import BookManager, venue_of
from arb.interfaces import ResyncRequired
from arb.types import RawMessage
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"

STALE_NS = 10**9


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


def snapshot_frames() -> list[bytes]:
    return [f for f in frames() if b'"type":"orderbook_snapshot"' in f]


def test_venue_of() -> None:
    assert venue_of("kalshi:KXWC-30-POR") == "kalshi"
    assert venue_of("polymarket_us:some-slug") == "polymarket_us"


def test_book_mark_invalid_is_sticky_until_snapshot() -> None:
    book = Book("test-market", staleness_limit_ns=STALE_NS)
    snap = BookSnapshot(
        market_id="test-market", bids=(Level(4000, 10),), asks=(Level(4200, 5),), seq=None
    )
    assert book.apply_snapshot(snap, mono_ns=0).valid

    status = book.mark_invalid(InvalidReason.SEQ_GAP)
    assert not status.valid
    assert status.reason is InvalidReason.SEQ_GAP
    assert book.needs_resync
    assert book.status(now_mono_ns=0).reason is InvalidReason.SEQ_GAP
    # Only a fresh snapshot recovers it.
    assert book.apply_snapshot(snap, mono_ns=0).valid
    assert book.status(now_mono_ns=0).valid


def test_replay_capture_builds_valid_books() -> None:
    adapter = KalshiMarketDataAdapter()
    manager = BookManager(staleness_limit_ns=STALE_NS)
    changed_total: set[str] = set()
    for frame in frames():
        changed_total |= manager.apply(adapter.parse(raw(frame)), mono_ns=0)
    assert len(manager.books) == 5
    assert changed_total == set(manager.books)
    for market_id, book in manager.books.items():
        status = manager.status(market_id, now_mono_ns=0)
        assert status is not None and status.valid
        assert manager.last_update_mono_ns(market_id) == 0
        best_bid, best_ask = book.best_bid(), book.best_ask()
        if best_bid is not None and best_ask is not None:
            assert best_bid.price < best_ask.price
    assert manager.get("kalshi:nonexistent") is None
    assert manager.status("kalshi:nonexistent", now_mono_ns=0) is None


def test_seq_gap_invalidates_venue_books_then_snapshots_recover() -> None:
    adapter = KalshiMarketDataAdapter()
    manager = BookManager(staleness_limit_ns=STALE_NS)
    all_frames = frames()
    delta_indexes = [
        i for i, frame in enumerate(all_frames) if b'"type":"orderbook_delta"' in frame
    ]
    skipped = delta_indexes[1]  # simulate one lost delta frame
    saw_resync = False
    for i, frame in enumerate(all_frames):
        if i == skipped:
            continue
        events = adapter.parse(raw(frame))
        saw_resync = saw_resync or any(isinstance(e, ResyncRequired) for e in events)
        manager.apply(events, mono_ns=0)
    assert saw_resync

    # The gap taints every book on the venue, and deltas after it are ignored.
    for market_id in manager.books:
        status = manager.status(market_id, now_mono_ns=0)
        assert status is not None
        assert not status.valid
        assert status.reason is InvalidReason.SEQ_GAP

    # Fresh snapshots (the real captured ones, replayed) recover each book.
    for frame in snapshot_frames():
        manager.apply(adapter.parse(raw(frame)), mono_ns=0)
    for market_id in manager.books:
        status = manager.status(market_id, now_mono_ns=0)
        assert status is not None
        assert status.valid, (market_id, status.reason)


def test_targeted_resync_marks_only_listed_markets() -> None:
    adapter = KalshiMarketDataAdapter()
    manager = BookManager(staleness_limit_ns=STALE_NS)
    for frame in snapshot_frames():
        manager.apply(adapter.parse(raw(frame)), mono_ns=0)
    target = next(iter(manager.books))
    changed = manager.apply([ResyncRequired("kalshi", (target, "kalshi:unknown"))], mono_ns=0)
    assert changed == {target}
    for market_id in manager.books:
        status = manager.status(market_id, now_mono_ns=0)
        assert status is not None
        assert status.valid == (market_id != target)


def test_delta_for_unseen_market_creates_book_that_needs_resync() -> None:
    # A delta with no prior snapshot lazily creates the book, which stays
    # invalid (no snapshot) and ignores level updates until one arrives.
    adapter = KalshiMarketDataAdapter()
    manager = BookManager(staleness_limit_ns=STALE_NS)
    delta_frame = next(f for f in frames() if b'"type":"orderbook_delta"' in f)
    events = adapter.parse(raw(delta_frame))
    assert manager.apply(events, mono_ns=0) == set()
    (market_id,) = {e.market_id for e in events if not isinstance(e, ResyncRequired)}
    book = manager.get(market_id)
    assert book is not None
    assert book.needs_resync
    assert book.bids() == () and book.asks() == ()
