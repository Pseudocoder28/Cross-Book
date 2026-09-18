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
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from tests.browser import BrowserError, TerminalState, guarded_app

from arb.ui.control import ConfirmRequired, ControlResult, InvalidParams

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
        self._seed_control()

    # -- a pretend control plane, so /control can be driven end to end --------
    #
    # Nothing here is the real executor: it flips values in the payload the
    # page renders from, prices a preview, arms G3 jobs and prints a DOCTOR
    # report, which is everything the page's interaction design needs to be
    # judged. The real rules (audit, read-only, single-flight) are tested
    # against the real ControlPlane in tests/test_control.py.

    def _seed_control(self) -> None:
        k = [m["ticker"] for m in self._markets if m["venue"] == "kalshi"]
        pm = [m["ticker"] for m in self._markets if m["venue"] == "polymarket_us"]
        legs_k, legs_pm = (
            ["KXMUSKNW-26DEC31-T900", "KXSCOURT-29-NR"],
            ["pnwpc-elonmusk-2026-12-31-gt900b"],
        )
        c = self.control
        c["bind"] = {"host": "0.0.0.0", "loopback": False, "remote_allowed": True}
        c["paper"].update(
            notional_ticks=960_000,
            trades=7,
            positions=1,
            skipped_invalid=3,
            skipped_suspended=0,
            taken_levels=2,
        )
        c["pairs_top"] = 10
        c["pairs"] = {
            "confirmed": 69,
            "tracked": 14,
            "total": 11967,
            "live": 11,
            "settled": [136, 137, 138],
            "poll": {
                "attached": True,
                "targets": len(pm) + len(legs_pm),
                "interval_s": 2.222,
                "cycle_s": 2.222 * (len(pm) + len(legs_pm)),
                "per_pair_s": 2.2,
            },
        }
        c["tracked_pairs"] = 11
        c["universe"] = {
            "kalshi": {"tickers": k + legs_k, "base": k, "pairs": legs_k, "attached": True},
            "polymarket_us": {
                "slugs": pm + legs_pm,
                "base": pm,
                "pairs": legs_pm,
                "attached": True,
                "interval_s": 2.222,
                "cycle_s": 2.222 * (len(pm) + len(legs_pm)),
            },
        }
        c["jobs"] = []
        self._tokens: dict[str, str] = {}
        self.audit = []

    def _effect(self, action: str, params: dict[str, Any]) -> str:
        if action == "pairs.top":
            n = params.get("n")
            if not isinstance(n, int) or n < 0:
                raise InvalidParams("n must be a whole number, 0 or more")
            pr = self.control["pairs"]
            targets = len(self.control["universe"]["polymarket_us"]["base"]) + n
            poll = pr["poll"]
            return (
                f"replace the watch set with {n} confirmed pairs, dealt one at a time "
                f"across Kalshi events: {abs(n - pr['tracked']) + 2} rows change, "
                f"watching {n} of {pr['confirmed']} confirmed pairs; "
                f"Polymarket US {poll['targets']} \u2192 {targets} poll targets, "
                f"{poll['cycle_s']:.1f}s \u2192 {targets * 2.222:.1f}s per book"
            )
        if action == "pairs.track":
            ids = params.get("ids") or []
            return f"stop watching {len(ids)} pairs: {len(ids)} rows change ({len(ids)} untracked)"
        if action == "universe.kalshi":
            n_k = len(params.get("tickers") or [])
            return (
                f"subscribe to {n_k} Kalshi markets: the socket reconnects (seconds of "
                "gap, every book resnapshots) and dropped books are evicted"
            )
        if action == "universe.polymarket":
            n_pm = len(params.get("slugs") or [])
            return f"poll {n_pm} Polymarket US markets besides the watched pairs' legs"
        if action == "jobs.propose":
            return (
                "fetch both venues' catalogues, score every candidate and upsert the "
                "proposals (thousands of rows)"
            )
        if action == "jobs.backfill":
            return "fill in missing event slugs on already-proposed pairs"
        return action.replace(".", " ")

    async def preview_control(self, action: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "action": action,
            "effect": self._effect(action, params or {}),
            "grade": "G2",
            "preview": True,
        }

    async def execute_control(
        self, action: str, params: dict[str, Any] | None, *, confirm: str | None
    ) -> ControlResult:
        p = params or {}
        c = self.control
        effect = self._effect(action, p)
        armed = self._tokens.get(action)
        if action in ("jobs.propose", "jobs.backfill") and (confirm is None or armed != confirm):
            self._tokens[action] = token = f"tok-{action}-{time.time_ns()}"
            raise ConfirmRequired(action, token, effect, 30.0)
        changed, message, detail = True, "", {}
        if action in ("paper.suspend", "paper.resume"):
            want = action == "paper.suspend"
            changed = c["paper"]["suspended"] != want
            c["paper"].update(suspended=want, enabled=not want)
            message = (
                ("paper trading suspended" if want else "paper trading resumed")
                if changed
                else ("already suspended" if want else "already trading")
            )
        elif action in ("recording.start", "recording.stop"):
            want = action == "recording.start"
            changed = c["recording"] != want
            c["recording"] = self.recording = want
            message = (
                ("recording on" if want else "recording off")
                if changed
                else "already " + ("on" if want else "off")
            )
        elif action == "paper.limits":
            lim = c["paper"]["limits"]
            new = {
                "min_net_ticks": p.get("min_net_ticks", lim["min_net_ticks"]),
                "max_qty_per_pair": p.get("max_cts_per_pair", lim["max_qty_per_pair"] // C) * C,
                "max_notional_ticks": p.get("max_notional_ticks", lim["max_notional_ticks"]),
            }
            changed = new != lim
            c["paper"]["limits"] = new
            message = (
                "limits applied" if changed else "nothing changed: those are already the limits"
            )
        elif action == "pairs.top":
            n = p["n"]
            changed = n != c["pairs"]["tracked"] or bool(c["pairs"]["settled"])
            c["pairs"].update(tracked=n, live=n, settled=[])
            c["pairs_top"], c["tracked_pairs"] = n, n
            message = (
                f"watching {n} pairs"
                if changed
                else f"nothing changed: the top {n} are already the watch set"
            )
        elif action == "pairs.track":
            gone = len(p.get("ids") or [])
            c["pairs"]["tracked"] -= gone
            c["pairs"]["settled"] = []
            message = f"0 tracked, {gone} untracked; watching {c['pairs']['live']} pairs"
        elif action == "universe.kalshi":
            u = c["universe"]["kalshi"]
            u["base"] = list(p["tickers"])
            u["tickers"] = u["base"] + u["pairs"]
            message = f"subscribed to {len(u['tickers'])} Kalshi markets; the socket reconnected"
        elif action == "universe.polymarket":
            u = c["universe"]["polymarket_us"]
            u["base"] = list(p["slugs"])
            u["slugs"] = u["base"] + u["pairs"]
            c["pairs"]["poll"].update(targets=len(u["slugs"]), cycle_s=2.222 * len(u["slugs"]))
            message = f"polling {len(u['slugs'])} Polymarket US markets"
        elif action.startswith("jobs.") and action != "jobs.cancel":
            name = action.split(".", 1)[1]
            job_id = f"{name}-{time.time_ns()}"
            threading.Thread(target=self._run_job, args=(job_id, name), daemon=True).start()
            message, detail = f"{name} started", {"job_id": job_id}
        elif action == "jobs.cancel":
            for j in c["jobs"]:
                if j["job_id"] == p.get("job_id") and j["status"] == "running":
                    j["status"] = "cancelled"
            message = "cancelled"
        self.audit.insert(
            0,
            {
                "id": len(self.audit) + 1,
                "ts_ns": time.time_ns(),
                "action": action,
                "effect": effect,
                "result": "ok",
                "error": None,
                "params": p,
            },
        )
        self.send({"t": "control", "control": c})
        return ControlResult(
            action, effect, changed, {"message": message, **detail}, len(self.audit)
        )

    DOCTOR: ClassVar[list[str]] = [
        "[ok  ] kalshi keys          key id set, key file at secrets/kalshi_private_key.pem",
        "[warn] polymarket_us keys   not provisioned (WS market data needs them)",
        "[ok  ] kalshi               HTTP 200 in 102 ms",
        "[ok  ] polymarket_us        HTTP 200 in 55 ms",
        "[ok  ] ntp clock            offset -0.3 ms vs pool.ntp.org (rtt 10.8 ms)",
        "[ok  ] database             connected",
        "[ok  ] migrations           at revision 0005",
        "[ok  ] disk                 440.7 GB free",
    ]

    def _run_job(self, job_id: str, name: str) -> None:
        lines = (
            self.DOCTOR if name == "doctor" else [f"{name}: step {i + 1} of 12" for i in range(12)]
        )
        job = {
            "job_id": job_id,
            "name": name,
            "group": name,
            "params": {},
            "status": "running",
            "phase": "start",
            "message": "",
            "step": 0,
            "total": len(lines),
            "started_ts_ns": time.time_ns(),
            "finished_ts_ns": None,
            "elapsed_s": 0.0,
            "error": None,
            "result": None,
            "lines_total": 0,
            "lines_dropped": 0,
        }
        self.control["jobs"].insert(0, job)
        self._job_lines = getattr(self, "_job_lines", {})
        self._job_lines[job_id] = []
        t0 = time.monotonic()
        for i, line in enumerate(lines):
            time.sleep(0.35 if name == "doctor" else 1.0)
            if job["status"] != "running":
                break
            self._job_lines[job_id].append(line)
            job.update(
                step=i + 1,
                phase="checks" if name == "doctor" else "scoring",
                elapsed_s=time.monotonic() - t0,
                lines_total=i + 1,
            )
            self.send({"t": "job", "job": dict(job), "tail": self._job_lines[job_id][-20:]})
        if job["status"] == "running":
            job["status"] = "ok"
        job.update(finished_ts_ns=time.time_ns(), elapsed_s=time.monotonic() - t0)
        self.send({"t": "job", "job": dict(job), "tail": self._job_lines[job_id][-20:]})
        self.send({"t": "control", "control": self.control})

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        for j in self.control["jobs"]:
            if j["job_id"] == job_id:
                return {**j, "lines": list(getattr(self, "_job_lines", {}).get(job_id, []))}
        return None

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
            st.send({"t": "control", "control": st.control})
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
