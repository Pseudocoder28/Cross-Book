"""Serve the real terminal UI against simulated order books.

Usage: uv run python scripts/preview_ui.py [--port 8765] [--failures]

The real app and the real static files (edits show on reload), fed by books
that move the way the venues' books move: Kalshi streams small level deltas
continuously on a 1¢ grid; Polymarket US arrives as a whole-book replacement
once per poll, one market on a 0.1¢ grid. Frames have the exact wire shapes
server.py broadcasts (book / delta / stats / control), so every page renders
as it would live.

It exists because the MONITOR depth panel is mostly motion, and motion cannot
be judged against a quiet market or a screenshot: this gives a busy book on
demand. Nothing here talks to a venue, needs a key or touches the database.
`--failures` adds a seq-gap window to one Kalshi market and a crossed book to
another every 40s, to see the INVALID and CROSSED states.

A dev tool, not an `arb` command. `scripts/snap.py` screenshots it.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from tests.browser import BrowserError, TerminalState, guarded_app

C = 10_000  # Qty units per contract
POLL_GAP_S = (5.0, 9.0)  # Polymarket US: seconds between polls, per market
CYCLE_S = sum(POLL_GAP_S) / 2  # the cycle the control frame advertises
LATENCY_KEEP = 2000

rng = random.Random(7)

# venue, ticker, title, fair value (ticks), 24h volume, price grid (ticks), updates/s
MARKETS: list[tuple[str, str, str, int, float, int, float]] = [
    (
        "kalshi",
        "KXBTCD-26SEP1717-T117999.99",
        "Bitcoin above $118,000 on Sep 17 at 5pm ET?",
        5400,
        812_000,
        100,
        9.0,
    ),
    (
        "kalshi",
        "KXNFLGAME-26SEP18KCBUF-KC",
        "Chiefs at Bills — Chiefs win",
        4700,
        655_000,
        100,
        5.0,
    ),
    (
        "kalshi",
        "KXFEDDECISION-26OCT-H0",
        "Fed holds rates at the October meeting",
        8850,
        420_000,
        100,
        2.5,
    ),
    (
        "kalshi",
        "KXCPIYOY-26SEP-T3.0",
        "CPI year-over-year above 3.0% in September",
        3100,
        260_000,
        100,
        1.6,
    ),
    (
        "kalshi",
        "KXSCOURT-29-AT",
        "Next Supreme Court justice — Amul Thapar",
        1200,
        91_000,
        100,
        0.7,
    ),
    (
        "kalshi",
        "KXBTCY-27JAN0100-B22500",
        "Bitcoin at end of 2026 — $20,000 to $24,999.99",
        150,
        40_000,
        100,
        0.3,
    ),
    (
        "polymarket_us",
        "nfl-kc-buf-2026-09-18-kc",
        "Chiefs at Bills — Chiefs win",
        4750,
        0.0,
        100,
        0.0,
    ),
    (
        "polymarket_us",
        "fed-oct-2026-hold",
        "Fed holds rates at the October meeting",
        8800,
        0.0,
        10,
        0.0,
    ),
    (
        "polymarket_us",
        "cpc-btc-pricerange-yr-12-31-2026-22500",
        "Bitcoin at end of 2026 — $20,000 to $24,999.99",
        200,
        0.0,
        100,
        0.0,
    ),
]


class Sim:
    """One market's book: price -> Qty, per side."""

    def __init__(self, venue: str, ticker: str, fair: int, grid: int, rate: float) -> None:
        self.venue = venue
        self.market_id = f"{venue}:{ticker}"
        self.fair = float(fair)
        self.grid = grid
        self.rate = rate
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.failures = False
        self.seed_book()

    def _size(self, dist: int) -> int:
        """Size `dist` grid steps from the touch: thin at the touch, fattening
        out, the occasional wall, and Kalshi's fractional counts."""
        base = rng.lognormvariate(math.log(180 + 90 * dist), 0.9)
        if rng.random() < 0.08:
            base *= rng.uniform(6, 22)
        frac = rng.choice([0, 0, 0, 0, rng.randrange(1, C)])
        return int(base) * C + frac

    def _snap(self, x: float) -> int:
        g = self.grid
        return int(max(g, min(10_000 - g, round(x / g) * g)))

    def seed_book(self) -> None:
        half = max(self.grid, rng.choice([1, 1, 2, 3]) * self.grid)
        best_bid = self._snap(self.fair - half)
        best_ask = max(self._snap(self.fair + half), best_bid + self.grid)
        self.bids.clear()
        self.asks.clear()
        for i in range(rng.randint(18, 34)):
            if i and rng.random() < 0.12:
                continue  # gaps in the ladder
            if (p := best_bid - i * self.grid) > 0:
                self.bids[p] = self._size(i)
            if (p := best_ask + i * self.grid) < 10_000:
                self.asks[p] = self._size(i)

    def step(self) -> list[tuple[str, int, int]]:
        """One streamed event: a take at the touch, an improvement inside the
        spread, or an add/cancel near the top. Returns (side, price, dq)."""
        out: list[tuple[str, int, int]] = []
        self.fair += rng.gauss(0, self.grid * 0.18)
        self.fair = max(self.grid * 2, min(10_000 - self.grid * 2, self.fair))
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        side = "bid" if rng.random() < 0.5 else "ask"
        book = self.bids if side == "bid" else self.asks
        touch = bb if side == "bid" else ba
        if touch is None:
            return out
        r = rng.random()
        if r < 0.22:
            take = min(book[touch], int(book[touch] * rng.uniform(0.15, 1.05)))
            book[touch] -= take
            out.append((side, touch, -take))
            if book[touch] <= 0:
                del book[touch]
        elif r < 0.34 and bb is not None and ba is not None and ba - bb > self.grid:
            p = self._snap(self.fair + (-0.5 if side == "bid" else 0.5) * self.grid)
            p = max(bb + self.grid, min(ba - self.grid, p))
            q = self._size(0)
            book[p] = book.get(p, 0) + q
            out.append((side, p, q))
        else:
            prices = sorted(book, reverse=(side == "bid"))[:10]
            p = prices[min(len(prices) - 1, int(rng.expovariate(0.45)))]
            if rng.random() < 0.55:
                q = int(rng.lognormvariate(math.log(120), 1.0)) * C
                book[p] += q
                out.append((side, p, q))
            else:
                q = min(book[p], int(book[p] * rng.uniform(0.1, 0.7)))
                book[p] -= q
                out.append((side, p, -q))
                if book[p] <= 0:
                    del book[p]
        # refill the far end so the ladder never empties out
        for side2, bk in (("bid", self.bids), ("ask", self.asks)):
            if not bk:
                self.seed_book()
                return out
            if len(bk) < 14:
                p = min(bk) - self.grid if side2 == "bid" else max(bk) + self.grid
                if 0 < p < 10_000:
                    bk[p] = self._size(len(bk))
                    out.append((side2, p, bk[p]))
        # a book that has drifted far from fair re-seeds around it
        if self.bids and self.asks:
            mid = (max(self.bids) + min(self.asks)) / 2
            if abs(mid - self.fair) > self.grid * 3:
                self.seed_book()
        return out

    def repoll(self) -> list[tuple[str, int, int]]:
        """A Polymarket-style poll: the whole book arrives at once; diff it."""
        old_b, old_a = dict(self.bids), dict(self.asks)
        self.fair += rng.gauss(0, self.grid * 1.4)
        self.fair = max(self.grid * 2, min(10_000 - self.grid * 2, self.fair))
        for _ in range(rng.randint(4, 14)):
            self.step()
        out: list[tuple[str, int, int]] = []
        for side, old, new in (("bid", old_b, self.bids), ("ask", old_a, self.asks)):
            for p in set(old) | set(new):
                if dq := new.get(p, 0) - old.get(p, 0):
                    out.append((side, p, dq))
        return out

    def payload(self) -> dict[str, Any]:
        bids = [[p, q] for p, q in sorted(self.bids.items(), reverse=True)]
        asks = [[p, q] for p, q in sorted(self.asks.items())]
        valid, reason = True, None
        if self.failures and 10 <= time.time() % 40 < 25:
            if "KXCPIYOY" in self.market_id:
                valid, reason = False, "seq_gap"
            if "KXSCOURT" in self.market_id and bids and asks:
                bids = [[asks[0][0] + 100, 250 * C], *bids]  # a bid through the ask
                valid, reason = False, "crossed"
        return {
            "t": "book",
            "market_id": self.market_id,
            "bids": bids,
            "asks": asks,
            "valid": valid,
            "reason": reason,
            "age_ms": 0.0,
            "ts_ms": int(time.time() * 1000),
        }


class PreviewState(TerminalState):
    """The browser tests' stub state, with a market universe and live books."""

    def __init__(self, *, failures: bool) -> None:
        super().__init__()
        self.sims = [Sim(v, t, f, g, r) for v, t, _title, f, _vol, g, r in MARKETS]
        for s in self.sims:
            s.failures = failures
        self._markets = [
            {"market_id": f"{v}:{t}", "ticker": t, "title": title, "volume_24h": vol, "venue": v}
            for v, t, title, _f, vol, _g, _r in MARKETS
        ]
        self.msg_total = 0
        self.lat: list[float] = []

    def hello_markets(self) -> list[dict[str, Any]]:
        return self._markets

    def book_payloads(self) -> list[dict[str, Any]]:
        return [s.payload() for s in self.sims]

    def kalshi_status(self) -> tuple[str, str]:
        return "live", "streaming (simulated)"

    def polymarket_status(self) -> tuple[str, str]:
        return "live", "polling (simulated)"

    def send(self, frame: dict[str, Any]) -> None:
        try:
            self.broadcast(frame)
        except BrowserError:
            pass  # no browser connected yet


def run_sim(st: PreviewState) -> None:
    kalshi = [s for s in st.sims if s.venue == "kalshi"]
    poly = [s for s in st.sims if s.venue == "polymarket_us"]
    last_poll = time.monotonic()
    next_poll = {s.market_id: time.monotonic() + rng.uniform(1, 6) for s in poly}
    last_stats = started = time.monotonic()
    last_total = 0
    polls = 0
    total_rate = sum(s.rate for s in kalshi)
    while True:
        time.sleep(rng.expovariate(total_rate))
        now = time.monotonic()
        pick = rng.random() * total_rate  # one Kalshi market, weighted by activity
        sim = kalshi[-1]
        for s in kalshi:
            pick -= s.rate
            if pick <= 0:
                sim = s
                break
        if deltas := sim.step():
            st.send(sim.payload())
            for side, p, dq in deltas:
                lat = max(4.0, rng.gauss(38, 14))
                st.lat.append(lat)
                st.msg_total += 1
                st.send(
                    {
                        "t": "delta",
                        "market_id": sim.market_id,
                        "side": side,
                        "price": p,
                        "qty_delta": dq,
                        "latency_ms": lat,
                        "ts_ms": int(time.time() * 1000),
                    }
                )
            if len(st.lat) > 2 * LATENCY_KEEP:
                del st.lat[:-LATENCY_KEEP]
        for ps in poly:
            if now >= next_poll[ps.market_id]:
                next_poll[ps.market_id] = now + rng.uniform(*POLL_GAP_S)
                polls += 1
                last_poll = now
                deltas = ps.repoll()
                st.send(ps.payload())
                for side, p, dq in deltas:
                    st.msg_total += 1
                    st.send(
                        {
                            "t": "delta",
                            "market_id": ps.market_id,
                            "side": side,
                            "price": p,
                            "qty_delta": dq,
                            "latency_ms": None,
                            "ts_ms": int(time.time() * 1000),
                        }
                    )
        if now - last_stats >= 1.0:
            last_stats = now
            ctl = dict(st.control)
            uni = dict(ctl.get("universe") or {})
            uni["polymarket_us"] = {
                **(uni.get("polymarket_us") or {}),
                "cycle_s": CYCLE_S,
                "slugs": [s.market_id.split(":", 1)[1] for s in poly],
            }
            ctl["universe"] = uni
            st.send({"t": "control", "control": ctl})
            w = sorted(st.lat[-LATENCY_KEEP:])
            n = len(w)
            rate, last_total = float(st.msg_total - last_total), st.msg_total
            st.send(
                {
                    "t": "stats",
                    "msg_total": st.msg_total,
                    "msg_rate_1s": rate,
                    "recorder": {"enqueued": st.msg_total, "dropped": 0},
                    "polymarket_us": {
                        "polls": polls,
                        "rate_limited": 0,
                        "errors": 0,
                        "targets": len(poly),
                        "rate_per_s": round(len(poly) / CYCLE_S, 2),
                        "last_poll_age_ms": (now - last_poll) * 1000,
                    },
                    "latency_ms": {
                        "last": st.lat[-1] if st.lat else None,
                        "median": w[n // 2] if n else None,
                        "p95": w[int(n * 0.95)] if n else None,
                        "n": n,
                    },
                    "rtt_ms": 71.0,
                    "clock_skew_ms": 2.0,
                    "parse_errors": 0,
                    "seq_gaps": 0,
                    "ws_clients": 1,
                    "uptime_s": now - started,
                }
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--failures",
        action="store_true",
        help="cycle one market through a seq gap and another through a crossed book",
    )
    args = ap.parse_args()
    st = PreviewState(failures=args.failures)
    threading.Thread(target=run_sim, args=(st,), daemon=True).start()
    print(f"preview: http://127.0.0.1:{args.port}  (simulated books, no venues, no database)")
    uvicorn.run(guarded_app(st), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
