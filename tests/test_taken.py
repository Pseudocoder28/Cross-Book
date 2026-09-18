"""Paper fills consume the displayed liquidity they took (arb.taken).

The regression these pin: a paper fill never reaches the venue, so the size it
took keeps sitting in the book, and a trader that re-reads the book on every
change takes the same resting contracts again. Live, one 16-contract bid was
filled against six times in a minute — 100 contracts out of 16.
"""

from arb.arbmon import ArbMonitor, TrackedPair
from arb.book import BookSnapshot, InvalidReason, Level
from arb.books import BookManager
from arb.paper import PaperLimits, PaperTrader
from arb.taken import TakenLiquidity

C = 10_000
K, P = "kalshi:K", "polymarket_us:p"


def no_fee(_qty: int, _price: int) -> int:
    return 0


def make() -> tuple[BookManager, ArbMonitor, TrackedPair]:
    books = BookManager(staleness_limit_ns=10**12)
    pair = TrackedPair(7, 1.0, K, P, no_fee, no_fee, "Musk — above $900B")
    return books, ArbMonitor(books, [pair]), pair


def show(books: BookManager, market: str, bids: list[tuple[int, int]], asks: list[tuple[int, int]]):
    books.apply(
        [
            BookSnapshot(
                market,
                tuple(Level(p, q * C) for p, q in bids),
                tuple(Level(p, q * C) for p, q in asks),
                None,
            )
        ],
        mono_ns=0,
    )


def trade(trader: PaperTrader, mon: ArbMonitor, pair: TrackedPair, ts: int = 1):
    return trader.trade_pair(mon, pair, ts_ms=ts, now_mono_ns=1)


def test_the_same_resting_bid_is_not_filled_twice() -> None:
    """The live glitch, reproduced: Kalshi asks 52¢ in size, Polymarket bids
    56¢ for 16 contracts. The edge is 16 contracts, once."""
    books, mon, pair = make()
    show(books, K, [(4600, 129)], [(5200, 148)])
    show(books, P, [(5600, 16)], [(5700, 33)])
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_qty_per_pair=100 * C))

    first = trade(trader, mon, pair)
    assert first is not None and first.qty == 16 * C

    # The Kalshi book ticks six more times; the Polymarket bid still shows 16
    # because paper never hit it. None of that is new liquidity.
    for ts in range(2, 8):
        show(books, K, [(4600, 129 + ts)], [(5200, 148)])
        assert trade(trader, mon, pair, ts) is None
    assert trader.totals()["qty"] == 16 * C
    # /arb still shows the market as it is: the raw quote is untouched.
    assert mon.quote(pair, now_mono_ns=1)["best"]["qty"] == 16 * C


def test_size_added_to_a_taken_level_is_new_and_a_level_that_returns_is_new() -> None:
    books, mon, pair = make()
    show(books, K, [(4600, 100)], [(5200, 500)])
    show(books, P, [(5600, 16), (5500, 10)], [(5700, 33)])
    trader = PaperTrader(PaperLimits(min_net_ticks=50, max_qty_per_pair=1000 * C))
    both = trade(trader, mon, pair)
    assert both is not None and both.qty == 26 * C  # both bids clear 52¢

    show(books, P, [(5600, 20), (5500, 10)], [(5700, 33)])  # 4 more join at 56¢
    more = trade(trader, mon, pair, 2)
    assert more is not None and more.qty == 4 * C

    show(books, P, [(5500, 10)], [(5700, 33)])  # the 56¢ bid is pulled...
    assert trade(trader, mon, pair, 3) is None
    show(books, P, [(5600, 8), (5500, 10)], [(5700, 33)])  # ...and a new one arrives
    again = trade(trader, mon, pair, 4)
    assert again is not None and again.qty == 8 * C


def test_a_level_that_shrinks_then_regrows_only_offers_the_regrowth() -> None:
    led = TakenLiquidity()
    led.consume(P, "bid", [Level(5600, 16 * C)], 16 * C)
    assert led.net(P, "bid", [Level(5600, 16 * C)]) == []
    # The venue now shows 10: that size is gone, whoever took it.
    assert led.net(P, "bid", [Level(5600, 10 * C)]) == []
    # Back to 16: six contracts arrived after ours would have been filled.
    assert led.net(P, "bid", [Level(5600, 16 * C)]) == [Level(5600, 6 * C)]


def test_an_empty_side_is_a_resync_not_a_pulled_market() -> None:
    """A book mid-resync shows no levels. Forgetting there would re-offer
    every consumed level the moment the snapshot lands."""
    led = TakenLiquidity()
    led.consume(P, "bid", [Level(5600, 16 * C)], 16 * C)
    assert led.net(P, "bid", []) == []
    assert led.net(P, "bid", [Level(5600, 16 * C)]) == []
    assert led.levels == 1 and led.qty == 16 * C


def test_a_structurally_invalid_book_is_never_traded() -> None:
    books, mon, pair = make()
    show(books, K, [(4600, 100)], [(5200, 500)])
    show(books, P, [(5600, 16)], [(5700, 33)])
    trader = PaperTrader(PaperLimits(min_net_ticks=50))
    book = books.get(K)
    assert book is not None
    book.mark_invalid(InvalidReason.SEQ_GAP)

    assert trade(trader, mon, pair) is None
    assert trader.skipped_invalid == 1 and trader.payload()["skipped_invalid"] == 1
    show(books, K, [(4600, 100)], [(5200, 500)])  # the fresh snapshot heals it
    assert trade(trader, mon, pair, 2) is not None


def test_a_missing_leg_is_declined_and_the_ledger_is_in_the_payload() -> None:
    books, mon, pair = make()
    show(books, K, [(4600, 100)], [(5200, 500)])
    trader = PaperTrader(PaperLimits(min_net_ticks=50))
    assert trade(trader, mon, pair) is None and trader.skipped_invalid == 1
    show(books, P, [(5600, 16)], [(5700, 33)])
    assert trade(trader, mon, pair, 2) is not None
    assert trader.payload()["liquidity_taken"] == {"levels": 2, "qty": 32 * C}
