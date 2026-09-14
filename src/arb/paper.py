"""Paper execution: simulated fills against measured edges, under risk limits.

The simulator is deliberately optimistic in one specific way — a fill is
assumed at the displayed liquidity the edge walk consumed, instantly, on both
legs — and honest everywhere else: fees are the venues' models, sizes are
capped by what the books show and by the risk limits, and "P&L" is the net
edge locked in at settlement (both legs together pay exactly $1.00), so it
is expected value under equivalence, not a mark-to-market.

Nothing here talks to a venue. Day 1's read-only rule holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from arb.arbmon import TrackedPair
from arb.edge import EdgeQuote, Leg
from arb.types import QTY_PER_CONTRACT, Qty, Ticks


@dataclass(frozen=True, slots=True)
class PaperLimits:
    min_net_ticks: Ticks = 50  # 0.5¢ per contract after fees
    max_qty_per_pair: Qty = 100 * QTY_PER_CONTRACT
    max_notional_ticks: int = 1_000 * 10_000  # $1,000 of cost across all pairs

    def payload(self) -> dict[str, Any]:
        return {
            "min_net_ticks": self.min_net_ticks,
            "max_qty_per_pair": self.max_qty_per_pair,
            "max_notional_ticks": self.max_notional_ticks,
        }


@dataclass(frozen=True, slots=True)
class PaperTrade:
    trade_id: int
    ts_ms: int
    pair_id: int
    label: str
    direction: str
    qty: Qty
    cost_ticks: int  # both legs, ticks (dollars x 10^4)
    fee_ticks: Ticks
    net_ticks: int  # expected profit at settlement, ticks
    legs: tuple[Leg, Leg]

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.trade_id,
            "ts_ms": self.ts_ms,
            "pair_id": self.pair_id,
            "label": self.label,
            "direction": self.direction,
            "qty": self.qty,
            "cost_ticks": self.cost_ticks,
            "fee_ticks": self.fee_ticks,
            "net_ticks": self.net_ticks,
            "legs": [
                {
                    "venue": leg.venue,
                    "market_id": leg.market_id,
                    "side": leg.side,
                    "worst_price": leg.worst_price,
                    "qty": leg.qty,
                    "fee_ticks": leg.fee_ticks,
                }
                for leg in self.legs
            ],
        }


@dataclass
class PaperPosition:
    pair_id: int
    label: str
    qty: Qty = 0
    cost_ticks: int = 0
    fee_ticks: Ticks = 0
    net_ticks: int = 0
    trades: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "label": self.label,
            "qty": self.qty,
            "cost_ticks": self.cost_ticks,
            "fee_ticks": self.fee_ticks,
            "net_ticks": self.net_ticks,
            "trades": self.trades,
        }


@dataclass
class PaperTrader:
    limits: PaperLimits = field(default_factory=PaperLimits)
    trades: list[PaperTrade] = field(default_factory=list)
    positions: dict[int, PaperPosition] = field(default_factory=dict)
    notional_ticks: int = 0
    _next_id: int = 1

    def consider(self, pair: TrackedPair, quote: EdgeQuote, *, ts_ms: int) -> PaperTrade | None:
        """Take the edge if it clears the floor and fits the limits."""
        if quote.qty <= 0 or quote.net_per_contract_ticks < self.limits.min_net_ticks:
            return None
        pos = self.positions.get(pair.pair_id)
        held = pos.qty if pos else 0
        room_pair = self.limits.max_qty_per_pair - held
        if room_pair <= 0:
            return None
        cost_tq = quote.legs[0].cost_tq + quote.legs[1].cost_tq  # tick·Qty for the full quote
        cost_per_contract = Fraction(cost_tq, quote.qty)  # ticks per contract
        room_notional = self.limits.max_notional_ticks - self.notional_ticks
        if room_notional <= 0 or cost_per_contract <= 0:
            return None
        max_by_notional = int(Fraction(room_notional * QTY_PER_CONTRACT) / cost_per_contract)
        qty = min(quote.qty, room_pair, max_by_notional)
        if qty <= 0:
            return None
        frac = Fraction(qty, quote.qty)
        cost_ticks = int(Fraction(cost_tq, QTY_PER_CONTRACT) * frac)
        fee_ticks = int(quote.fee_ticks * frac)
        net_ticks = int(Fraction(quote.net_tq, QTY_PER_CONTRACT) * frac)
        legs = tuple(
            Leg(
                venue=leg.venue,
                market_id=leg.market_id,
                side=leg.side,
                worst_price=leg.worst_price,
                qty=qty,
                cost_tq=int(leg.cost_tq * frac),
                fee_ticks=int(leg.fee_ticks * frac),
            )
            for leg in quote.legs
        )
        trade = PaperTrade(
            trade_id=self._next_id,
            ts_ms=ts_ms,
            pair_id=pair.pair_id,
            label=pair.label,
            direction=quote.direction,
            qty=qty,
            cost_ticks=cost_ticks,
            fee_ticks=fee_ticks,
            net_ticks=net_ticks,
            legs=(legs[0], legs[1]),
        )
        self._next_id += 1
        self.trades.append(trade)
        if pos is None:
            pos = PaperPosition(pair_id=pair.pair_id, label=pair.label)
            self.positions[pair.pair_id] = pos
        pos.qty += qty
        pos.cost_ticks += cost_ticks
        pos.fee_ticks += fee_ticks
        pos.net_ticks += net_ticks
        pos.trades += 1
        self.notional_ticks += cost_ticks
        return trade

    def totals(self) -> dict[str, Any]:
        return {
            "trades": len(self.trades),
            "qty": sum(t.qty for t in self.trades),
            "cost_ticks": sum(t.cost_ticks for t in self.trades),
            "fee_ticks": sum(t.fee_ticks for t in self.trades),
            "net_ticks": sum(t.net_ticks for t in self.trades),
        }

    def payload(self, *, enabled: bool = True, max_trades: int = 200) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "limits": self.limits.payload(),
            "totals": self.totals(),
            "positions": [p.payload() for p in self.positions.values()],
            "trades": [t.payload() for t in reversed(self.trades[-max_trades:])],
        }
