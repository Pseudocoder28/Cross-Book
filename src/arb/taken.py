"""Displayed liquidity the paper trader has already taken.

A paper fill does not reach the venue, so the size it "took" keeps sitting in
the book — and a simulator that re-reads the book on every change takes the
same resting contracts again and again. Observed live: a 16-contract bid on
Polymarket US was filled against six times in a minute, 100 contracts out of
16, until the per-pair cap stopped it. Every one of those fills after the
first was liquidity that did not exist.

This ledger remembers, per (market, side, price), how much of the displayed
size paper fills have consumed, and nets it out of the ladders the trader is
shown. The rules, each chosen to be wrong only against the book:

- available = displayed - taken, never below zero;
- if the displayed size falls below what was taken, ``taken`` shrinks to it:
  the venue is saying that size has gone, so a later increase is genuinely
  new and may be taken;
- a price that leaves the ladder is forgotten — if it comes back it is new
  liquidity — but only while the ladder still has other levels: an empty
  side is a book mid-resync, not a market where every order was pulled.

What this cannot know is whether a maker would have replenished a level we
really hit. It assumes not, which undercounts fills; the alternative
overcounts them, and a paper P&L that flatters is worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence

from arb.book import Level
from arb.types import Qty, Ticks

type _Key = tuple[str, str, Ticks]  # (market_id, "bid" | "ask", price)


class TakenLiquidity:
    def __init__(self) -> None:
        self._taken: dict[_Key, Qty] = {}

    def net(self, market_id: str, side: str, levels: Sequence[Level]) -> list[Level]:
        """``levels`` (best first) with paper-consumed size removed."""
        if not self._taken:
            return list(levels)
        out: list[Level] = []
        for lvl in levels:
            key = (market_id, side, lvl.price)
            taken = self._taken.get(key)
            if taken is None:
                out.append(lvl)
                continue
            if taken > lvl.qty:
                taken = lvl.qty
                self._taken[key] = taken
            if lvl.qty > taken:
                out.append(Level(lvl.price, lvl.qty - taken))
        if levels:
            shown = {lvl.price for lvl in levels}
            gone = [
                k for k in self._taken if k[0] == market_id and k[1] == side and k[2] not in shown
            ]
            for k in gone:
                del self._taken[k]
        return out

    def consume(self, market_id: str, side: str, levels: Sequence[Level], qty: Qty) -> Qty:
        """Record a fill of ``qty`` against ``levels``, best first.

        Returns the quantity that could not be placed — zero whenever the
        fill was sized off :meth:`net` of the same ladder, which is the only
        way the trader sizes one.
        """
        left = qty
        for lvl in levels:
            if left <= 0:
                break
            key = (market_id, side, lvl.price)
            taken = min(self._taken.get(key, 0), lvl.qty)
            take = min(left, lvl.qty - taken)
            if take > 0:
                self._taken[key] = taken + take
                left -= take
        return left

    def forget_market(self, market_id: str) -> None:
        for k in [k for k in self._taken if k[0] == market_id]:
            del self._taken[k]

    @property
    def levels(self) -> int:
        return len(self._taken)

    @property
    def qty(self) -> Qty:
        return sum(self._taken.values())
