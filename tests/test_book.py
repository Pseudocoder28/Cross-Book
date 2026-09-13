from hypothesis import given
from hypothesis import strategies as st

from arb.book import (
    Book,
    BookLevelUpdate,
    BookSnapshot,
    InvalidReason,
    Level,
    UpdateMode,
)
from arb.types import BookSide, complement

STALE_NS = 1_000_000_000  # 1s staleness limit for tests
T0 = 10_000_000_000  # arbitrary monotonic origin


def make_book() -> Book:
    return Book("test-market", staleness_limit_ns=STALE_NS)


def snapshot(
    bids: list[tuple[int, int]],
    asks: list[tuple[int, int]],
    seq: int | None = 1,
) -> BookSnapshot:
    return BookSnapshot(
        market_id="test-market",
        bids=tuple(Level(p, q) for p, q in bids),
        asks=tuple(Level(p, q) for p, q in asks),
        seq=seq,
    )


def update(
    side: BookSide,
    price: int,
    qty: int,
    mode: UpdateMode = UpdateMode.SET,
    seq: int | None = None,
) -> BookLevelUpdate:
    return BookLevelUpdate(
        market_id="test-market", side=side, price=price, qty=qty, mode=mode, seq=seq
    )


class TestLifecycle:
    def test_fresh_book_is_invalid_and_needs_resync(self) -> None:
        book = make_book()
        status = book.status(now_mono_ns=T0)
        assert not status.valid
        assert status.reason is InvalidReason.NO_SNAPSHOT
        assert book.needs_resync
        assert not book.has_snapshot

    def test_snapshot_makes_book_valid(self) -> None:
        book = make_book()
        status = book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)]), mono_ns=T0)
        assert status.valid
        assert book.status(now_mono_ns=T0).valid
        assert not book.needs_resync
        assert book.has_snapshot
        assert book.last_seq == 1

    def test_resnapshot_recovers_an_invalid_book(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=1), mono_ns=T0)
        book.apply_level(update(BookSide.BID, 4100, 3, seq=5), mono_ns=T0)  # gap
        assert book.needs_resync
        status = book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=9), mono_ns=T0)
        assert status.valid
        assert book.last_seq == 9


class TestLadders:
    def test_ladders_are_best_first(self) -> None:
        book = make_book()
        book.apply_snapshot(
            snapshot([(3900, 1), (4000, 2), (3800, 3)], [(4300, 4), (4200, 5)]),
            mono_ns=T0,
        )
        assert book.bids() == (Level(4000, 2), Level(3900, 1), Level(3800, 3))
        assert book.asks() == (Level(4200, 5), Level(4300, 4))
        assert book.best_bid() == Level(4000, 2)
        assert book.best_ask() == Level(4200, 5)

    def test_no_side_is_the_complement(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 2), (3900, 1)], [(4200, 5), (4300, 4)]), mono_ns=T0)
        # Best NO bid comes from the best YES ask: 10000 - 4200 = 5800.
        assert book.no_bids() == (Level(5800, 5), Level(5700, 4))
        assert book.no_asks() == (Level(6000, 2), Level(6100, 1))

    def test_empty_sides_are_allowed(self) -> None:
        book = make_book()
        status = book.apply_snapshot(snapshot([], []), mono_ns=T0)
        assert status.valid
        assert book.best_bid() is None
        assert book.best_ask() is None


class TestLevelUpdates:
    def make_valid(self) -> Book:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=None), mono_ns=T0)
        return book

    def test_set_adds_changes_and_removes(self) -> None:
        book = self.make_valid()
        book.apply_level(update(BookSide.BID, 3900, 7), mono_ns=T0)
        assert book.bids() == (Level(4000, 10), Level(3900, 7))
        book.apply_level(update(BookSide.BID, 3900, 2), mono_ns=T0)
        assert book.bids() == (Level(4000, 10), Level(3900, 2))
        book.apply_level(update(BookSide.BID, 3900, 0), mono_ns=T0)
        assert book.bids() == (Level(4000, 10),)

    def test_delta_accumulates_and_removes_at_zero(self) -> None:
        book = self.make_valid()
        book.apply_level(update(BookSide.ASK, 4200, 3, UpdateMode.DELTA), mono_ns=T0)
        assert book.best_ask() == Level(4200, 8)
        book.apply_level(update(BookSide.ASK, 4200, -8, UpdateMode.DELTA), mono_ns=T0)
        assert book.best_ask() is None

    def test_delta_below_zero_is_a_bad_level(self) -> None:
        book = self.make_valid()
        status = book.apply_level(update(BookSide.ASK, 4200, -6, UpdateMode.DELTA), mono_ns=T0)
        assert status.reason is InvalidReason.BAD_LEVEL
        assert book.needs_resync

    def test_set_negative_qty_is_a_bad_level(self) -> None:
        book = self.make_valid()
        status = book.apply_level(update(BookSide.BID, 4000, -1), mono_ns=T0)
        assert status.reason is InvalidReason.BAD_LEVEL

    def test_price_outside_bounds_is_a_bad_level(self) -> None:
        for price in (0, 10000, -100, 10100):
            book = self.make_valid()
            status = book.apply_level(update(BookSide.BID, price, 1), mono_ns=T0)
            assert status.reason is InvalidReason.BAD_LEVEL

    def test_updates_are_ignored_while_invalid(self) -> None:
        book = self.make_valid()
        book.apply_level(update(BookSide.BID, 0, 1), mono_ns=T0)  # invalidate
        status = book.apply_level(update(BookSide.BID, 3900, 7), mono_ns=T0)
        assert status.reason is InvalidReason.BAD_LEVEL
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)]), mono_ns=T0)
        assert book.bids() == (Level(4000, 10),)  # the 3900 update was dropped


class TestSnapshotValidation:
    def test_zero_qty_level_invalidates(self) -> None:
        book = make_book()
        status = book.apply_snapshot(snapshot([(4000, 0)], []), mono_ns=T0)
        assert status.reason is InvalidReason.BAD_LEVEL

    def test_out_of_bounds_price_invalidates(self) -> None:
        book = make_book()
        status = book.apply_snapshot(snapshot([], [(10000, 1)]), mono_ns=T0)
        assert status.reason is InvalidReason.BAD_LEVEL

    def test_crossed_snapshot_invalidates(self) -> None:
        book = make_book()
        status = book.apply_snapshot(snapshot([(4300, 1)], [(4200, 1)]), mono_ns=T0)
        assert status.reason is InvalidReason.CROSSED


class TestSequencing:
    def test_contiguous_seq_is_accepted(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=1), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 3900, 1, seq=2), mono_ns=T0)
        assert status.valid
        assert book.last_seq == 2

    def test_gap_invalidates(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=1), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 3900, 1, seq=3), mono_ns=T0)
        assert status.reason is InvalidReason.SEQ_GAP
        assert book.needs_resync

    def test_replayed_seq_invalidates(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=2), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 3900, 1, seq=2), mono_ns=T0)
        assert status.reason is InvalidReason.SEQ_GAP

    def test_first_seq_is_adopted_when_snapshot_had_none(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=None), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 3900, 1, seq=17), mono_ns=T0)
        assert status.valid
        assert book.last_seq == 17
        status = book.apply_level(update(BookSide.BID, 3900, 2, seq=19), mono_ns=T0)
        assert status.reason is InvalidReason.SEQ_GAP

    def test_unsequenced_updates_are_accepted(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)], seq=4), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 3900, 1, seq=None), mono_ns=T0)
        assert status.valid
        assert book.last_seq == 4


class TestCrossing:
    def test_bid_through_ask_invalidates(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)]), mono_ns=T0)
        status = book.apply_level(update(BookSide.BID, 4300, 1), mono_ns=T0)
        assert status.reason is InvalidReason.CROSSED
        assert book.needs_resync

    def test_locked_book_counts_as_crossed(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)]), mono_ns=T0)
        status = book.apply_level(update(BookSide.ASK, 4000, 1), mono_ns=T0)
        assert status.reason is InvalidReason.CROSSED


class TestStaleness:
    def test_stale_book_is_invalid_but_recovers_on_update(self) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([(4000, 10)], [(4200, 5)]), mono_ns=T0)
        assert book.status(now_mono_ns=T0 + STALE_NS).valid
        status = book.status(now_mono_ns=T0 + STALE_NS + 1)
        assert status.reason is InvalidReason.STALE
        assert not book.needs_resync  # staleness alone doesn't force a snapshot
        book.apply_level(update(BookSide.BID, 3900, 1), mono_ns=T0 + STALE_NS + 1)
        assert book.status(now_mono_ns=T0 + STALE_NS + 2).valid


prices = st.integers(min_value=1, max_value=9999)
quantities = st.integers(min_value=0, max_value=5)
sides = st.sampled_from([BookSide.BID, BookSide.ASK])
set_updates = st.tuples(sides, prices, quantities)


class TestInvariants:
    @given(st.lists(set_updates, max_size=60))
    def test_views_stay_consistent_under_arbitrary_sets(
        self, ops: list[tuple[BookSide, int, int]]
    ) -> None:
        book = make_book()
        book.apply_snapshot(snapshot([], []), mono_ns=T0)
        for side, price, qty in ops:
            book.apply_level(update(side, price, qty), mono_ns=T0)

        bids, asks = book.bids(), book.asks()
        bid_prices = [level.price for level in bids]
        ask_prices = [level.price for level in asks]
        assert bid_prices == sorted(bid_prices, reverse=True)
        assert ask_prices == sorted(ask_prices)
        assert all(level.qty > 0 for level in bids + asks)
        assert all(0 < level.price < 10000 for level in bids + asks)
        # NO views are the exact complement of the YES views, best first.
        assert book.no_bids() == tuple(Level(complement(a.price), a.qty) for a in asks)
        assert book.no_asks() == tuple(Level(complement(b.price), b.qty) for b in bids)
        # A structurally valid book is never crossed.
        if book.status(now_mono_ns=T0).valid and bids and asks:
            assert bids[0].price < asks[0].price
