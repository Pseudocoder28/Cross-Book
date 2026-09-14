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


def test_level_deltas_diff_snapshots() -> None:
    from arb.book import Book, BookSnapshot, Level
    from arb.books import level_deltas
    from arb.types import BookSide

    book = Book("polymarket_us:x", staleness_limit_ns=10**9)
    book.apply_snapshot(
        BookSnapshot("polymarket_us:x", (Level(400, 10), Level(300, 5)), (Level(500, 7),), None),
        mono_ns=0,
    )
    incoming = BookSnapshot(
        "polymarket_us:x",
        (Level(400, 12), Level(200, 1)),  # 400: +2, 300: removed, 200: new
        (Level(500, 7), Level(600, 3)),  # 500: unchanged, 600: new
        None,
    )
    deltas = level_deltas(book, incoming)
    assert [(d.side, d.price, d.qty) for d in deltas] == [
        (BookSide.BID, 400, 2),
        (BookSide.BID, 300, -5),
        (BookSide.BID, 200, 1),
        (BookSide.ASK, 600, 3),
    ]
    assert level_deltas(None, incoming)[0].qty == 12  # from nothing: every level is new


def test_per_venue_staleness_override() -> None:
    from arb.book import BookSnapshot
    from arb.books import BookManager

    manager = BookManager(staleness_limit_ns=5_000_000_000)
    manager.set_venue_staleness("polymarket_us", 20_000_000_000)
    manager.apply([BookSnapshot("kalshi:A", (), (), None)], mono_ns=0)
    manager.apply([BookSnapshot("polymarket_us:B", (), (), None)], mono_ns=0)
    ten_s = 10_000_000_000
    kalshi = manager.status("kalshi:A", now_mono_ns=ten_s)
    poly = manager.status("polymarket_us:B", now_mono_ns=ten_s)
    assert kalshi is not None and not kalshi.valid  # streaming default: stale after 5s
    assert poly is not None and poly.valid  # polled venue: 20s budget
    assert manager.staleness_limit_ns("polymarket_us:B") == 20_000_000_000
