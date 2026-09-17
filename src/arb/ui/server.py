"""Terminal UI backend: FastAPI app factory plus the live wiring.

``create_app(state)`` builds the HTTP/WS surface against a small
:class:`UIState` interface so tests can stub it (no network, no database).
``run_ui`` mirrors ``arb record``'s wiring — recorder-first ingest, supervised
tasks — and additionally parses frames through the Kalshi adapter, maintains
normalized books and pushes JSON frames to connected UI WebSocket clients.

The browser UI is a multi-page app routed client-side over the History API on
one long-lived WebSocket, so every page URL must survive a deep link or a
reload. This app serves the same shell document (``static/index.html``) for
each of those routes:

- ``/`` ``/arb`` ``/pairs`` ``/paper`` ``/system`` ``/control`` ``/help``
  (:data:`SPA_ROUTES`)
- ``/market/{market_id}`` — ``market_id`` is opaque and never validated here,
  so a link to a market that has rolled off the discovery list still opens
  the shell and lets the browser report the miss

The route set is enumerated rather than a catch-all: ``/api/*``, ``/ws``,
``/metrics`` and ``/static/*`` keep their own handlers, and an unknown path is
still a 404 instead of a shell that hides broken links.

Wire contract (server → client JSON text frames):

- ``hello`` on connect: run id + market list (sorted by 24h volume desc)
- ``book``: full YES-book state per market, coalesced to <= 10/s per market
- ``delta``: one tape entry per applied orderbook delta
- ``stats``: totals, rates and latency percentiles, broadcast every second
- ``control``: the whole control-plane state (recording, paper, universe,
  jobs), sent on connect and after every control action so two open tabs can
  never disagree about whether paper trading is on
- ``job``: one long action's progress and output tail

Prices are integer ticks of $0.0001; quantities are integer units of 0.0001
contracts. Metrics are served on ``GET /metrics`` from this app — ``run_ui``
does not start a separate metrics HTTP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import httpx
import uvicorn
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from arb import paper_store
from arb.arbmon import ArbMonitor, TrackedPair
from arb.book import BookLevelUpdate, BookSnapshot
from arb.books import BookManager, level_deltas
from arb.config import AppConfig
from arb.interfaces import ParseError, ResyncRequired
from arb.metrics import (
    CLOCK_SKEW_MS,
    PARSE_ERRORS,
    UI_WS_CLIENTS,
    UI_WS_CLIENTS_DROPPED,
    WS_ONE_WAY_LATENCY_MS,
    WS_ONE_WAY_LATENCY_NEGATIVE,
    WS_RTT_MS,
)
from arb.pairs import store as pairs_store
from arb.pairs.tracked import load_tracked_pairs
from arb.paper import PaperLimits, PaperTrader
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.db import insert_raw_messages, make_engine
from arb.storage.models import RawMessageRow
from arb.supervise import supervise
from arb.types import RawMessage
from arb.ui.control import (
    ConfirmRequired,
    ControlError,
    ControlPlane,
    ControlResult,
    NotAvailable,
    ReadOnlyRefused,
)
from arb.ui.security import (
    BindInfo,
    OriginGuardMiddleware,
    check_bind_host,
    parse_csv,
)
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter
from arb.venues.kalshi.detail import build_market_detail
from arb.venues.kalshi.discovery import fetch_event, fetch_liquid_markets, fetch_market
from arb.venues.kalshi.rest import KalshiEvent, KalshiMarket
from arb.venues.kalshi.rest import market_id as kalshi_market_id
from arb.venues.kalshi.source import STREAM as KALSHI_STREAM
from arb.venues.kalshi.source import VENUE as KALSHI_VENUE
from arb.venues.kalshi.source import KalshiWSSource
from arb.venues.polymarket_us.adapter import PolymarketUSMarketDataAdapter
from arb.venues.polymarket_us.detail import build_market_detail as build_pm_detail
from arb.venues.polymarket_us.discovery import fetch_active_markets, select_poll_targets
from arb.venues.polymarket_us.rest import PolymarketUSEvent, PolymarketUSMarket
from arb.venues.polymarket_us.rest import market_id as pm_market_id
from arb.venues.polymarket_us.source import PolymarketUSRestSource

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DRAIN_TIMEOUT_S = 10.0
# No inbound WS frame for this long → the venue is reported "down".
VENUE_DOWN_AFTER_S = 30.0
# Book pushes are coalesced to at most one per market per interval (<= 10/s).
BOOK_FLUSH_INTERVAL_S = 0.1
STATS_INTERVAL_S = 1.0
PM_REACHABILITY_INTERVAL_S = 60.0
DB_STATUS_TIMEOUT_S = 5.0
LATENCY_WINDOW = 512
# Per-client outbound frame queue. A client that falls this far behind is
# dropped: a slow UI consumer must never stall market-data ingest.
SEND_QUEUE_MAX = 1024
# DES metadata is re-fetched from the venue at most this often per market.
DETAIL_TTL_MS = 30_000
PAIR_STATUSES = ("proposed", "confirmed", "rejected")
# Client-side routes without a path parameter; each serves the app shell.
SPA_ROUTES = ("/", "/arb", "/pairs", "/paper", "/system", "/control", "/help")

# (ticker, cached event or None) -> fresh (market, event)
type DetailRefreshFn = Callable[
    [str, KalshiEvent | None], Awaitable[tuple[KalshiMarket, KalshiEvent | None]]
]


def _shell_response(static_root: Path) -> Response:
    """The app shell, or the plain-text fallback when assets are not built."""
    index_file = static_root / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return PlainTextResponse("UI assets missing")


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    connected: bool
    raw_messages_total: int | None
    runs: tuple[tuple[str, int], ...]  # (run_id, message count), newest first


def no_control_payload(*, run_id: str, recording: bool) -> dict[str, Any]:
    """Control state for a process with no control plane attached.

    Read-only by construction: with nothing wired up there is nothing to
    drive, and the browser renders the same shape it always does.
    """
    return {
        "run_id": run_id,
        "read_only": True,
        "recording": recording,
        "bind": None,
        "paper": {
            "attached": False,
            "enabled": False,
            "suspended": False,
            "limits": PaperLimits().payload(),
            "notional_ticks": 0,
            "skipped_suspended": 0,
        },
        "pairs_top": 0,
        "tracked_pairs": 0,
        "universe": {
            "kalshi": {"tickers": [], "base": [], "pairs": [], "attached": False},
            "polymarket_us": {
                "slugs": [],
                "base": [],
                "pairs": [],
                "attached": False,
                "interval_s": None,
                "cycle_s": None,
            },
        },
        "jobs": [],
        "actions": [],
        "confirm_ttl_s": 0.0,
        "ts_ms": time.time_ns() // 1_000_000,
    }


class UIState(Protocol):
    """What the routes need from the shared state. Tests stub this."""

    run_id: str
    recording: bool
    polymarket_rest_reachable: bool

    def uptime_s(self) -> float: ...

    def kalshi_status(self) -> tuple[str, str]:
        """(state, detail) where state is "live" | "connecting" | "down"."""
        ...

    def polymarket_status(self) -> tuple[str, str]:
        """(state, detail); state adds "polled" for REST polling without WS."""
        ...

    async def database_status(self) -> DatabaseStatus: ...

    def hello_markets(self) -> list[dict[str, Any]]: ...

    def book_payloads(self) -> list[dict[str, Any]]:
        """Current full-state "book" messages, for freshly connected clients."""
        ...

    async def market_detail(self, market_id: str) -> dict[str, Any] | None:
        """DES payload for one market, or None if the market is unknown."""
        ...

    async def list_pairs(self, status: str | None) -> list[dict[str, Any]]: ...

    async def decide_pair(self, pair_id: int, status: str) -> dict[str, Any] | None: ...

    async def decide_pairs(self, pair_ids: list[int], status: str) -> int: ...

    def arb_snapshot(self) -> list[dict[str, Any]]:
        """Current quotes for every tracked confirmed pair (best net first)."""
        ...

    async def paper_payload(self) -> dict[str, Any]:
        """Paper-trading ledger (live trader if enabled, else history)."""
        ...

    def control_payload(self) -> dict[str, Any]:
        """Control-plane state: what is on, what is running, what is allowed.

        Always present, even when no control plane is attached (tests, the
        replay harness), so the browser has one shape to render.
        """
        ...

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        """One job with its output tail, or None if the id is unknown."""
        ...

    async def execute_control(
        self, action: str, params: dict[str, Any] | None, *, confirm: str | None
    ) -> ControlResult:
        """Run one control action. Raises the ControlError the route maps."""
        ...

    async def control_log(self, limit: int) -> list[dict[str, Any]]:
        """Recent audit rows, newest first."""
        ...

    def add_client(self, ws: WebSocket) -> asyncio.Queue[str]:
        """Register a client; returns its bounded outbound frame queue."""
        ...

    def remove_client(self, ws: WebSocket) -> None: ...


def create_app(state: UIState, *, static_dir: Path | None = None) -> FastAPI:
    static_root = static_dir if static_dir is not None else STATIC_DIR
    app = FastAPI(title="arb ui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=static_root, check_dir=False), name="static")

    async def spa_shell() -> Response:
        return _shell_response(static_root)

    for spa_path in SPA_ROUTES:
        app.add_api_route(spa_path, spa_shell, methods=["GET"], include_in_schema=False)

    # ":path" so an id whose escaped form contains %2F survives ASGI's decode.
    @app.get("/market/{market_id:path}", include_in_schema=False)
    async def spa_market(market_id: str) -> Response:
        # Deliberately unvalidated: the browser resolves the id (and reports an
        # unknown one), so a deep link to a market that has since rolled off
        # the discovery list still opens the page instead of 404ing.
        return _shell_response(static_root)

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        kalshi_state, kalshi_detail = state.kalshi_status()
        pm_state, pm_detail = state.polymarket_status()
        db = await state.database_status()
        return {
            "run_id": state.run_id,
            "uptime_s": state.uptime_s(),
            "venues": {
                "kalshi": {"state": kalshi_state, "detail": kalshi_detail},
                "polymarket_us": {
                    "state": pm_state,
                    "detail": pm_detail,
                    "rest_reachable": state.polymarket_rest_reachable,
                },
            },
            "recording": state.recording,
            "control": state.control_payload(),
            "database": {
                "connected": db.connected,
                "raw_messages_total": db.raw_messages_total,
                "runs": [{"run_id": run_id, "count": count} for run_id, count in db.runs],
            },
        }

    @app.get("/api/markets/{market_id}")
    async def api_market_detail(market_id: str) -> Response:
        detail = await state.market_detail(market_id)
        if detail is None:
            return JSONResponse(
                {"error": "unknown market", "market_id": market_id}, status_code=404
            )
        return JSONResponse(detail)

    @app.get("/api/pairs")
    async def api_pairs(status: str | None = None) -> Response:
        if status is not None and status not in PAIR_STATUSES:
            return JSONResponse({"error": "invalid status"}, status_code=400)
        return JSONResponse({"pairs": await state.list_pairs(status)})

    @app.post("/api/pairs/decide")
    async def api_pairs_decide_many(body: dict[str, Any]) -> Response:
        status = body.get("status")
        ids = body.get("ids")
        if status not in PAIR_STATUSES or not isinstance(ids, list) or not ids:
            return JSONResponse({"error": "need ids[] and a valid status"}, status_code=400)
        try:
            pair_ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return JSONResponse({"error": "ids must be integers"}, status_code=400)
        updated = await state.decide_pairs(pair_ids, str(status))
        return JSONResponse({"updated": updated, "status": status})

    @app.post("/api/pairs/{pair_id}/decide")
    async def api_pair_decide(pair_id: int, body: dict[str, Any]) -> Response:
        status = body.get("status")
        if status not in PAIR_STATUSES:
            return JSONResponse(
                {"error": "status must be one of " + ", ".join(PAIR_STATUSES)}, status_code=400
            )
        row = await state.decide_pair(pair_id, str(status))
        if row is None:
            return JSONResponse({"error": "unknown pair", "id": pair_id}, status_code=404)
        return JSONResponse(row)

    @app.get("/api/arb")
    async def api_arb() -> Response:
        return JSONResponse({"quotes": state.arb_snapshot()})

    @app.get("/api/paper")
    async def api_paper() -> Response:
        return JSONResponse(await state.paper_payload())

    @app.get("/api/control")
    async def api_control() -> Response:
        """Control-plane state and the job list. Reading is G0: free."""
        return JSONResponse(state.control_payload())

    @app.post("/api/control/{action}")
    async def api_control_execute(action: str, body: dict[str, Any] | None = None) -> Response:
        """Run one control action. The ONLY write entry point.

        Every action goes through ``ControlPlane.execute``, which owns the
        read-only refusal, the arm-then-confirm handshake, the audit row and
        the metric. A route per toggle would be a route per chance to forget
        one of those, which is why this is a single path with the action in
        the URL rather than eleven handlers.

        Status codes carry the meaning the control page renders against:
        409 is "arm accepted, ask the operator", not a failure.
        """
        payload = body or {}
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            return JSONResponse({"error": "params must be an object"}, status_code=400)
        confirm = payload.get("confirm")
        if confirm is not None and not isinstance(confirm, str):
            return JSONResponse({"error": "confirm must be a string"}, status_code=400)
        try:
            result = await state.execute_control(action, params, confirm=confirm)
        except ConfirmRequired as exc:
            # Not a failure: the first call armed the action, and the body
            # carries the sentence the operator must be shown before the second.
            return JSONResponse(
                {"confirm_required": True, **exc.payload()}, status_code=exc.status_code
            )
        except ControlError as exc:
            # Every ControlError carries its own status, so the mapping lives
            # with the failure instead of in a table here that drifts from it.
            failure: dict[str, Any] = {"error": str(exc)}
            if isinstance(exc, ReadOnlyRefused):
                failure["read_only"] = True
            return JSONResponse(failure, status_code=exc.status_code)
        return JSONResponse(result.payload())

    @app.get("/api/control/log")
    async def api_control_log(limit: int = 50) -> Response:
        """The audit trail. It is the only record that a recording gap, a
        universe change or a bulk write was deliberate rather than a crash."""
        rows = await state.control_log(max(1, min(limit, 500)))
        return JSONResponse({"actions": rows})

    @app.get("/api/control/jobs/{job_id}")
    async def api_control_job(job_id: str) -> Response:
        """One job with its output. A finished job stays readable, so a
        browser that disconnected mid-job still sees how it ended."""
        job = state.job_payload(job_id)
        if job is None:
            return JSONResponse({"error": "unknown job", "job_id": job_id}, status_code=404)
        return JSONResponse(job)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue = state.add_client(ws)
        try:
            # No awaits between add_client and these puts: hello plus the
            # current book states enter the queue before any broadcast can
            # interleave, so a fresh client never misses or reorders state.
            queue.put_nowait(
                json.dumps({"t": "hello", "run_id": state.run_id, "markets": state.hello_markets()})
            )
            # Control state before the books: a tab that connects mid-job must
            # not render "paper: on" for even one frame if it is suspended.
            queue.put_nowait(json.dumps({"t": "control", "control": state.control_payload()}))
            for payload in state.book_payloads():
                queue.put_nowait(json.dumps(payload))
        except asyncio.QueueFull:
            state.remove_client(ws)
            with contextlib.suppress(Exception):
                await ws.close(code=1013)
            return

        async def send_from_queue() -> None:
            while True:
                await ws.send_text(await queue.get())

        sender = asyncio.create_task(send_from_queue())
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # Inbound client messages are ignored by contract.
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            state.remove_client(ws)

    return app


class ServerState:
    """Concrete :class:`UIState` plus the feed-side counters and fan-out."""

    def __init__(
        self,
        *,
        run_id: str,
        recording: bool,
        books: BookManager,
        database_status_fn: Callable[[], Awaitable[DatabaseStatus]] | None = None,
    ) -> None:
        self.run_id = run_id
        self.recording = recording
        self.polymarket_rest_reachable = False
        self.books = books
        self._database_status_fn = database_status_fn
        self._started_mono_ns = time.monotonic_ns()
        self._markets: list[dict[str, Any]] = []
        # DES metadata: market_id -> (market, event); refreshed on demand.
        self._detail_meta: dict[str, tuple[KalshiMarket, KalshiEvent | None]] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._detail_refresh_fn: DetailRefreshFn | None = None
        # Polymarket US REST polling (until WS credentials exist).
        self.pm_source: PolymarketUSRestSource | None = None
        self.pm_adapter: PolymarketUSMarketDataAdapter | None = None
        self._pm_last_frame_mono_ns: int | None = None
        self._pm_detail_meta: dict[str, tuple[PolymarketUSMarket, PolymarketUSEvent | None]] = {}
        self._pairs_engine: AsyncEngine | None = None
        self.arbmon: ArbMonitor | None = None
        self.trader: PaperTrader | None = None
        # Attached by run_ui once the runtime exists; None in tests and in any
        # process that has no controls (control_payload still answers).
        self.control: ControlPlane | None = None
        self.rtt_fn: Callable[[], float | None] | None = None
        self._clients: dict[WebSocket, asyncio.Queue[str]] = {}
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._dirty: set[str] = set()
        # Per-run stats for the 1s "stats" frame (Prometheus counters are
        # process-wide and unlabeled by run, so the UI keeps its own).
        self.msg_total = 0
        self.parse_errors = 0
        self.seq_gaps = 0
        self.recorder_enqueued = 0
        self.recorder_dropped = 0
        self.last_latency_ms: float | None = None
        self._latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._kalshi_last_frame_mono_ns: int | None = None

    # -- UIState ------------------------------------------------------------

    def uptime_s(self) -> float:
        return (time.monotonic_ns() - self._started_mono_ns) / 1e9

    def kalshi_status(self) -> tuple[str, str]:
        last = self._kalshi_last_frame_mono_ns
        if last is None:
            return "connecting", "awaiting first WebSocket frame"
        age_s = (time.monotonic_ns() - last) / 1e9
        if age_s > VENUE_DOWN_AFTER_S:
            return "down", f"no WebSocket frame for {age_s:.0f}s"
        return "live", f"last frame {age_s * 1000:.0f}ms ago"

    def polymarket_status(self) -> tuple[str, str]:
        src = self.pm_source
        if src is None:
            return "down", "awaiting API credentials"
        n = len(src.targets)
        if n == 0:
            # A supported state since the universe became mutable: the poller
            # naps instead of dividing by zero. Say so rather than claiming
            # a venue outage.
            return "idle", "no poll targets"
        budget = f"{n} markets @ {src.rate_per_s:g} req/s"
        last = self._pm_last_frame_mono_ns
        if last is None:
            return "connecting", f"REST polling {budget} — awaiting first book"
        age_s = (time.monotonic_ns() - last) / 1e9
        if age_s > VENUE_DOWN_AFTER_S:
            return "down", f"no successful poll for {age_s:.0f}s ({src.rate_limited} rate-limited)"
        return "polled", f"REST polling {budget} · WS awaiting credentials"

    def attach_polymarket(
        self, source: PolymarketUSRestSource, adapter: PolymarketUSMarketDataAdapter
    ) -> None:
        self.pm_source = source
        self.pm_adapter = adapter

    def on_polymarket_frame(self) -> None:
        self.msg_total += 1
        self._pm_last_frame_mono_ns = time.monotonic_ns()

    def add_markets(self, markets: list[dict[str, Any]]) -> None:
        self._markets = [*self._markets, *markets]
        self.broadcast_hello()

    def broadcast_hello(self) -> None:
        """Re-send the market list to every open tab.

        The browser builds MONITOR from the ``hello`` frame, and `onHello` is
        written to be re-run: it rebuilds the list, keeps the selection if it
        survived and prunes books for markets that left. Without this, a
        universe change (confirming a pair, then RELOAD PAIRS) subscribed both
        legs and streamed their books while MONITOR still showed the list from
        connect time — the new markets only appeared after a page reload.

        Every startup call site runs before a client exists, so this is a
        no-op then; it lives here rather than at the one runtime call site so
        a future one cannot forget it.
        """
        self.broadcast({"t": "hello", "run_id": self.run_id, "markets": self.hello_markets()})

    # -- pairs ----------------------------------------------------------------

    def set_pairs_engine(self, engine: AsyncEngine | None) -> None:
        self._pairs_engine = engine

    async def list_pairs(self, status: str | None) -> list[dict[str, Any]]:
        if self._pairs_engine is None:
            return []
        try:
            return await pairs_store.list_pairs(self._pairs_engine, status=status)
        except Exception:
            log.warning("pairs query failed", exc_info=True)
            return []

    async def decide_pair(self, pair_id: int, status: str) -> dict[str, Any] | None:
        if self._pairs_engine is None:
            return None
        return await pairs_store.decide(self._pairs_engine, pair_id, status)

    async def decide_pairs(self, pair_ids: list[int], status: str) -> int:
        if self._pairs_engine is None:
            return 0
        return await pairs_store.decide_many(self._pairs_engine, pair_ids, status)

    def seed_detail_pm(self, market: PolymarketUSMarket, event: PolymarketUSEvent | None) -> None:
        mid = pm_market_id(market.slug)
        self._pm_detail_meta[mid] = (market, event)
        self._detail_cache[mid] = build_pm_detail(
            market, event, None, source="discovery", fetched_at_ms=time.time_ns() // 1_000_000
        )

    async def database_status(self) -> DatabaseStatus:
        if self._database_status_fn is None:
            return DatabaseStatus(connected=False, raw_messages_total=None, runs=())
        try:
            return await self._database_status_fn()
        except Exception:
            log.warning("database status query failed", exc_info=True)
            return DatabaseStatus(connected=False, raw_messages_total=None, runs=())

    def hello_markets(self) -> list[dict[str, Any]]:
        return self._markets

    def book_payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for market_id in self.books.books:
            payload = self.book_payload(market_id)
            if payload is not None:
                payloads.append(payload)
        if self.arbmon is not None:
            payloads.append({"t": "arb", "quotes": self.arbmon.snapshot()})
        return payloads

    def arb_snapshot(self) -> list[dict[str, Any]]:
        return self.arbmon.snapshot() if self.arbmon is not None else []

    async def paper_payload(self) -> dict[str, Any]:
        if self.trader is not None:
            # No enabled= override: the trader's own suspend state is the
            # truth, and a toggle must not make the ledger disappear.
            return self.trader.payload()
        history: list[dict[str, Any]] = []
        if self._pairs_engine is not None:
            try:
                history = await paper_store.list_trades(self._pairs_engine, limit=200)
            except Exception:
                log.warning("paper history query failed", exc_info=True)
        return {
            "enabled": False,
            "limits": PaperLimits().payload(),
            "totals": {
                "trades": len(history),
                "qty": sum(int(t.get("qty", 0)) for t in history),
                "cost_ticks": sum(int(t.get("cost_ticks", 0)) for t in history),
                "fee_ticks": sum(int(t.get("fee_ticks", 0)) for t in history),
                "net_ticks": sum(int(t.get("net_ticks", 0)) for t in history),
            },
            "positions": [],
            "trades": history,
        }

    # -- control plane --------------------------------------------------------

    def attach_control(self, control: ControlPlane) -> None:
        self.control = control

    def control_payload(self) -> dict[str, Any]:
        if self.control is not None:
            return self.control.payload()
        return no_control_payload(run_id=self.run_id, recording=self.recording)

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        if self.control is None:
            return None
        record = self.control.jobs.get(job_id)
        return record.log_payload() if record is not None else None

    async def execute_control(
        self, action: str, params: dict[str, Any] | None, *, confirm: str | None
    ) -> ControlResult:
        if self.control is None:
            raise NotAvailable("this process has no control plane")
        return await self.control.execute(action, params, confirm=confirm)

    async def control_log(self, limit: int) -> list[dict[str, Any]]:
        if self.control is None:
            return []
        return await self.control.recent_actions(limit)

    def add_client(self, ws: WebSocket) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self._clients[ws] = queue
        UI_WS_CLIENTS.set(len(self._clients))
        return queue

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)
        UI_WS_CLIENTS.set(len(self._clients))

    # -- feed side ----------------------------------------------------------

    def set_markets(self, markets: list[dict[str, Any]]) -> None:
        self._markets = markets
        self.broadcast_hello()

    # -- DES (market description) ----------------------------------------------

    def set_detail_refresh(self, fn: DetailRefreshFn | None) -> None:
        self._detail_refresh_fn = fn

    def seed_detail(self, market: KalshiMarket, event: KalshiEvent | None) -> None:
        """Startup metadata from discovery; served until a live refresh lands."""
        mid = kalshi_market_id(market.ticker)
        self._detail_meta[mid] = (market, event)
        self._detail_cache[mid] = build_market_detail(
            market, event, source="discovery", fetched_at_ms=time.time_ns() // 1_000_000
        )

    async def market_detail(self, market_id: str) -> dict[str, Any] | None:
        known = any(m["market_id"] == market_id for m in self._markets)
        if not known and market_id not in self._detail_cache:
            return None
        cached = self._detail_cache.get(market_id)
        now_ms = time.time_ns() // 1_000_000
        pm_meta = self._pm_detail_meta.get(market_id)
        if pm_meta is not None:
            # Polled venue: activity numbers ride along with every book poll,
            # so the freshest detail needs no extra request.
            stats = self.pm_adapter.stats.get(market_id) if self.pm_adapter else None
            return build_pm_detail(
                pm_meta[0],
                pm_meta[1],
                stats,
                source="live" if stats is not None else "discovery",
                fetched_at_ms=now_ms,
            )
        if cached is not None and now_ms - cached["fetched_at_ms"] < DETAIL_TTL_MS:
            return cached
        if self._detail_refresh_fn is not None:
            ticker = market_id.split(":", 1)[1]
            prior = self._detail_meta.get(market_id)
            try:
                market, event = await self._detail_refresh_fn(ticker, prior[1] if prior else None)
            except Exception:
                log.warning("market detail refresh failed for %s", market_id, exc_info=True)
            else:
                self._detail_meta[market_id] = (market, event)
                cached = build_market_detail(market, event, source="live", fetched_at_ms=now_ms)
                self._detail_cache[market_id] = cached
        return cached

    def on_kalshi_frame(self) -> None:
        self.msg_total += 1
        self._kalshi_last_frame_mono_ns = time.monotonic_ns()

    def record_latency(self, latency_ms: float) -> None:
        """Record one venue-stamped one-way sample, exactly as measured.

        Kalshi's WS is the only source of these today. The value is never
        corrected or clamped: it carries the local-vs-venue clock offset, and
        the negative counter is how that offset makes itself visible.
        """
        self.last_latency_ms = latency_ms
        self._latencies.append(latency_ms)
        WS_ONE_WAY_LATENCY_MS.labels(venue=KALSHI_VENUE).observe(latency_ms)
        if latency_ms < 0:
            WS_ONE_WAY_LATENCY_NEGATIVE.labels(venue=KALSHI_VENUE).inc()

    def mark_dirty(self, market_ids: set[str]) -> None:
        self._dirty |= market_ids

    def take_dirty(self) -> set[str]:
        dirty, self._dirty = self._dirty, set()
        return dirty

    @property
    def ws_clients(self) -> int:
        return len(self._clients)

    def book_payload(self, market_id: str) -> dict[str, Any] | None:
        book = self.books.get(market_id)
        if book is None:
            return None
        now_mono_ns = time.monotonic_ns()
        status = book.status(now_mono_ns=now_mono_ns)
        last_update = self.books.last_update_mono_ns(market_id)
        age_ms = (now_mono_ns - last_update) / 1e6 if last_update is not None else 0.0
        return {
            "t": "book",
            "market_id": market_id,
            "bids": [[level.price, level.qty] for level in book.bids()],
            "asks": [[level.price, level.qty] for level in book.asks()],
            "valid": status.valid,
            "reason": status.reason.value if status.reason is not None else None,
            "age_ms": age_ms,
            "ts_ms": time.time_ns() // 1_000_000,
        }

    def stats_payload(self, *, msg_rate_1s: float) -> dict[str, Any]:
        """Build the 1s stats frame — and, as a side effect, publish the RTT
        and clock-skew gauges from the same arithmetic.

        Deliberate: one source of truth means the UI panel and Grafana can
        never disagree about the skew estimate.
        """
        window = sorted(self._latencies)
        n = len(window)
        median = statistics.median(window) if n else None
        rtt_ms = self.rtt_fn() if self.rtt_fn is not None else None
        # Keepalive RTT is skew-immune; one-way latency = true + skew, so
        # median - rtt/2 estimates the local clock's offset from the venue
        # (negative: local is behind).
        clock_skew_ms = (median - rtt_ms / 2) if (median is not None and rtt_ms) else None
        # NaN, not "leave the last value": a gauge that keeps reading -27 ms
        # through a reconnect would look like a live measurement of an outage.
        WS_RTT_MS.labels(venue=KALSHI_VENUE, stream=KALSHI_STREAM).set(
            rtt_ms if rtt_ms is not None else math.nan
        )
        CLOCK_SKEW_MS.labels(venue=KALSHI_VENUE).set(
            clock_skew_ms if clock_skew_ms is not None else math.nan
        )
        recorder = (
            {"enqueued": self.recorder_enqueued, "dropped": self.recorder_dropped}
            if self.recording
            else None
        )
        polymarket: dict[str, Any] | None = None
        if self.pm_source is not None:
            last = self._pm_last_frame_mono_ns
            polymarket = {
                "polls": self.pm_source.polls,
                "rate_limited": self.pm_source.rate_limited,
                "errors": self.pm_source.errors,
                "targets": len(self.pm_source.targets),
                "rate_per_s": self.pm_source.rate_per_s,
                "last_poll_age_ms": (
                    (time.monotonic_ns() - last) / 1e6 if last is not None else None
                ),
            }
        return {
            "polymarket_us": polymarket,
            "t": "stats",
            "msg_total": self.msg_total,
            "msg_rate_1s": msg_rate_1s,
            "latency_ms": {
                "last": self.last_latency_ms,
                "median": median,
                "p95": window[min(n - 1, int(0.95 * n))] if n else None,
                "n": n,
            },
            "rtt_ms": rtt_ms,
            "clock_skew_ms": clock_skew_ms,
            "parse_errors": self.parse_errors,
            "seq_gaps": self.seq_gaps,
            "ws_clients": len(self._clients),
            "recorder": recorder,
            "uptime_s": self.uptime_s(),
        }

    def broadcast(self, payload: dict[str, Any]) -> None:
        """Fan one frame out to every client without ever blocking.

        The ingest path calls this, so it must never await a client socket:
        frames go onto per-client bounded queues drained by each connection's
        sender task. A client whose queue is full is dropped (and closed) —
        a slow UI consumer must never stall market data or the recorder.
        """
        if not self._clients:
            return
        text = json.dumps(payload)
        slow: list[WebSocket] = []
        for ws, queue in self._clients.items():
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                slow.append(ws)
        for ws in slow:
            log.warning("dropping ui ws client: send queue full (%d frames)", SEND_QUEUE_MAX)
            UI_WS_CLIENTS_DROPPED.inc()
            self.remove_client(ws)
            # Close in the background so the client's endpoint task ends.
            task = asyncio.get_running_loop().create_task(_close_quietly(ws))
            self._close_tasks.add(task)
            task.add_done_callback(self._close_tasks.discard)


async def _close_quietly(ws: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=1013)  # 1013: try again later


async def fetch_database_status(engine: AsyncEngine) -> DatabaseStatus:
    """Raw-message totals and per-run counts (up to 10 runs, newest first)."""
    try:
        async with asyncio.timeout(DB_STATUS_TIMEOUT_S):
            async with engine.connect() as conn:
                total = (
                    await conn.execute(select(func.count()).select_from(RawMessageRow))
                ).scalar_one()
                rows = (
                    await conn.execute(
                        select(RawMessageRow.run_id, func.count())
                        .group_by(RawMessageRow.run_id)
                        .order_by(func.max(RawMessageRow.id).desc())
                        .limit(10)
                    )
                ).all()
    except Exception:
        return DatabaseStatus(connected=False, raw_messages_total=None, runs=())
    return DatabaseStatus(
        connected=True,
        raw_messages_total=int(total),
        runs=tuple((str(run_id), int(count)) for run_id, count in rows),
    )


async def run_ui(
    config: AppConfig,
    *,
    tickers: list[str] | None,
    top_n: int,
    record: bool,
    host: str,
    port: int,
    poly_top: int = 8,
    poly_slugs: list[str] | None = None,
    pairs_top: int = 10,
    paper: bool = False,
    paper_limits: PaperLimits | None = None,
) -> None:
    # Before anything binds or connects: the UI has no authentication and its
    # controls drive a process that will place real orders, so a non-loopback
    # bind is refused unless it was asked for explicitly.
    try:
        bind: BindInfo = check_bind_host(host, allow_remote=config.ui_allow_remote_bind)
    except Exception as exc:
        log.error("%s", exc)
        raise
    if not bind.loopback:
        log.warning(
            "ui bound to non-loopback host %s — anyone who can route here has the controls", host
        )

    run = RunContext(config.run_id or None)
    log.info("run_id=%s", run.run_id)

    engine = make_engine(config.database_url)
    books = BookManager(staleness_limit_ns=config.book_staleness_limit_ms * 1_000_000)
    state = ServerState(
        run_id=run.run_id,
        recording=record,
        books=books,
        database_status_fn=partial(fetch_database_status, engine),
    )
    state.set_pairs_engine(engine)

    # The recorder and its writer always exist, whatever --no-record said:
    # recording is a runtime toggle now, and a toggle that has to build a
    # recorder and start a supervised writer mid-flight is a toggle that can
    # fail halfway. An idle recorder costs one empty queue and one parked task.
    recorder = Recorder(
        partial(insert_raw_messages, engine),
        queue_max=config.recorder_queue_max,
        batch_max=config.recorder_batch_max,
    )
    writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))

    def record_raw(message: RawMessage) -> None:
        """The one sink. Gated on the LIVE flag, read per message.

        Callers pass this unconditionally; recording off means messages are
        dropped here, not that the sink is None. ``ingest_seq`` is allocated
        by the source either way, so a recording gap leaves a hole in the
        sequence — which only has to increase, not be contiguous.
        """
        if not state.recording:
            return
        if recorder.enqueue(message):
            state.recorder_enqueued += 1
        else:
            state.recorder_dropped += 1

    tasks: list[asyncio.Task[object]] = []
    control: ControlPlane | None = None
    try:
        if not tickers:
            discovered = await fetch_liquid_markets(config, run, top_n=top_n, sink=record_raw)
            log.info("discovered %d liquid kalshi markets", len(discovered))
            tickers = [m.ticker for m in discovered]
            state.set_markets(
                [
                    {
                        "market_id": kalshi_market_id(m.ticker),
                        "ticker": m.ticker,
                        "title": m.title,
                        "volume_24h": m.volume_24h,
                        "venue": "kalshi",
                    }
                    for m in discovered
                ]
            )
            for m in discovered:
                state.seed_detail(m.market, m.event)
        else:
            # Explicit tickers: no discovery metadata available.
            state.set_markets(
                [
                    {
                        "market_id": kalshi_market_id(t),
                        "ticker": t,
                        "title": "",
                        "volume_24h": 0.0,
                        "venue": "kalshi",
                    }
                    for t in tickers
                ]
            )

        assert tickers is not None
        # Confirmed pairs: fee parameters resolved from the documented
        # endpoints (recorded before parsing); both legs join the live sets.
        # Any failure disables the ARB screen, nothing else.
        tracked: list[TrackedPair] = []
        pair_pm_slugs: list[str] = []
        pair_pm_markets: dict[str, PolymarketUSMarket] = {}
        if pairs_top > 0:
            try:
                load = await load_tracked_pairs(
                    config, run, engine, top_n=pairs_top, sink=record_raw
                )
                tracked = load.tracked
                pair_pm_slugs = load.polymarket_slugs
                pair_pm_markets = load.polymarket_markets
                for k_market, k_event in load.kalshi_details:
                    state.seed_detail(k_market, k_event)
                for pmm in pair_pm_markets.values():
                    state.seed_detail_pm(pmm, None)
                for pair in tracked:
                    if pair.kalshi_ticker not in tickers:
                        tickers.append(pair.kalshi_ticker)
                        state.add_markets(
                            [
                                {
                                    "market_id": kalshi_market_id(pair.kalshi_ticker),
                                    "ticker": pair.kalshi_ticker,
                                    "title": pair.label,
                                    "volume_24h": 0.0,
                                    "venue": "kalshi",
                                }
                            ]
                        )
                log.info("arb monitor: tracking %d confirmed pairs", len(tracked))
            except Exception:
                log.warning(
                    "confirmed pairs could not be loaded; ARB screen disabled", exc_info=True
                )
                tracked, pair_pm_slugs, pair_pm_markets = [], [], {}

        # Polymarket US over public REST until WS credentials exist. Failure
        # here must never take Kalshi down: polling is simply disabled.
        pm_adapter = PolymarketUSMarketDataAdapter()
        pm_targets = list(poly_slugs or [])
        if not pm_targets and poly_top > 0:
            try:
                pm_markets = select_poll_targets(
                    await fetch_active_markets(config, run, sink=record_raw),
                    poly_top,
                )
            except Exception:
                log.warning("polymarket_us discovery failed; REST polling disabled", exc_info=True)
                pm_markets = []
            for pm in pm_markets:
                state.seed_detail_pm(pm.market, pm.event)
            state.add_markets(
                [
                    {
                        "market_id": pm_market_id(pm.slug),
                        "ticker": pm.slug,
                        "title": pm.title,
                        "volume_24h": 0.0,
                        "venue": "polymarket_us",
                    }
                    for pm in pm_markets
                ]
            )
            pm_targets = [pm.slug for pm in pm_markets]
            log.info("polymarket_us: polling %d markets over REST", len(pm_targets))
        elif pm_targets:
            state.add_markets(
                [
                    {
                        "market_id": pm_market_id(s),
                        "ticker": s,
                        "title": "",
                        "volume_24h": 0.0,
                        "venue": "polymarket_us",
                    }
                    for s in pm_targets
                ]
            )
        for slug in pair_pm_slugs:
            if slug not in pm_targets:
                pm_targets.append(slug)
                pmm = pair_pm_markets.get(slug)
                state.add_markets(
                    [
                        {
                            "market_id": pm_market_id(slug),
                            "ticker": slug,
                            "title": f"{pmm.question} — {pmm.title}" if pmm else "",
                            "volume_24h": 0.0,
                            "venue": "polymarket_us",
                        }
                    ]
                )
        # The poller always exists, even with nothing to poll: an empty target
        # set is a supported idle state (set_targets([])), and having the
        # source and its task there from the start is what lets the universe
        # control add targets later without spawning anything. The constructor
        # still rejects an empty list, so it is built with a placeholder that
        # is cleared before stream() is ever called — no request is made for it.
        pm_source = PolymarketUSRestSource(config=config, run=run, slugs=pm_targets or ["__idle__"])
        if not pm_targets:
            pm_source.set_targets([])
        state.attach_polymarket(pm_source, pm_adapter)

        async def refresh_detail(
            ticker: str, cached_event: KalshiEvent | None
        ) -> tuple[KalshiMarket, KalshiEvent | None]:
            sink = record_raw
            market = await fetch_market(config, run, ticker, sink=sink)
            event = cached_event
            if event is None or event.event_ticker != market.event_ticker:
                event = await fetch_event(config, run, market.event_ticker, sink=sink)
            return market, event

        state.set_detail_refresh(refresh_detail)

        if tracked:
            state.arbmon = ArbMonitor(books, tracked)
        # ONE trader for the life of the run. The committed spend, the open
        # positions and the ledger live on this instance, so the on/off toggle
        # suspends it — rebuilding one would silently reopen the whole
        # max_notional budget and throw the ledger away. It is built even when
        # --paper was not passed (starting suspended) so the control has
        # something to resume.
        state.trader = PaperTrader(paper_limits or PaperLimits())
        state.trader.set_suspended(not paper)
        log.info(
            "paper trading %s: %s",
            "enabled" if paper else "suspended (resume from the UI)",
            state.trader.limits.payload(),
        )

        source = KalshiWSSource(config=config, run=run, market_tickers=tickers)
        state.rtt_fn = source.rtt_ms
        adapter = KalshiMarketDataAdapter()

        # Everything mutable is now built: hand the control plane its handles.
        control = ControlPlane(
            config=config,
            host=state,
            run=run,
            engine=engine,
            sink=record_raw,
            bind=bind,
            pairs_top=pairs_top,
        )
        control.attach_kalshi(source)
        control.attach_polymarket(pm_source)
        control.seed_markets(state.hello_markets())
        control.set_base_universe(
            kalshi=[t for t in tickers if t not in {p.kalshi_ticker for p in tracked}],
            polymarket=[s for s in pm_targets if s not in set(pair_pm_slugs)],
        )
        control.set_pair_universe(
            kalshi=[p.kalshi_ticker for p in tracked], polymarket=pair_pm_slugs
        )
        # A polled book is only as fresh as its poll cycle, and the cycle
        # length is a function of the target count — so it is derived here,
        # from the live poller, by the same code the universe control reuses.
        control.retune_polymarket_staleness()

        async def consume() -> None:
            async for raw in source.stream():
                state.on_kalshi_frame()
                record_raw(raw)  # hard rule: enqueue before any parsing
                try:
                    events = adapter.parse(raw)
                except ParseError:
                    PARSE_ERRORS.labels(venue=source.venue, stream="ws").inc()
                    state.parse_errors += 1
                    log.warning("kalshi ws parse error", exc_info=True)
                    continue
                if not events:
                    continue
                state.mark_dirty(books.apply(events, mono_ns=time.monotonic_ns()))
                latency_ms: float | None = None
                if adapter.last_delta_ts_ms is not None:
                    latency_ms = raw.recv_ts_ns / 1e6 - adapter.last_delta_ts_ms
                    state.record_latency(latency_ms)
                gap = False
                for event in events:
                    if isinstance(event, ResyncRequired):
                        state.seq_gaps += 1
                        gap = True
                    elif isinstance(event, BookLevelUpdate):
                        book = books.get(event.market_id)
                        if book is None or book.needs_resync:
                            # The book manager skipped this update (awaiting
                            # a fresh snapshot): not an applied delta, so no
                            # tape entry by contract.
                            continue
                        state.broadcast(
                            {
                                "t": "delta",
                                "market_id": event.market_id,
                                "side": event.side.value,
                                "price": event.price,
                                "qty_delta": event.qty,
                                "latency_ms": latency_ms,
                                "ts_ms": (
                                    adapter.last_delta_ts_ms
                                    if adapter.last_delta_ts_ms is not None
                                    else raw.recv_ts_ns // 1_000_000
                                ),
                            }
                        )
                if gap:
                    # Reliability rule: a fresh snapshot after any gap.
                    # Reconnecting resubscribes, and Kalshi answers every
                    # subscribe with a full snapshot per market.
                    await source.force_resync()

        async def consume_poly() -> None:
            async for raw in pm_source.stream():
                state.on_polymarket_frame()
                record_raw(raw)  # hard rule: enqueue before any parsing
                try:
                    events = pm_adapter.parse(raw)
                except ParseError:
                    PARSE_ERRORS.labels(venue=pm_source.venue, stream=raw.stream).inc()
                    state.parse_errors += 1
                    log.warning("polymarket_us book parse error", exc_info=True)
                    continue
                for event in events:
                    if not isinstance(event, BookSnapshot):
                        continue
                    # A polled venue has no deltas; diff consecutive snapshots
                    # so the tape shows its level changes too.
                    ts_ms = pm_adapter.last_transact_ts_ms or raw.recv_ts_ns // 1_000_000
                    for delta in level_deltas(books.get(event.market_id), event):
                        state.broadcast(
                            {
                                "t": "delta",
                                "market_id": delta.market_id,
                                "side": delta.side.value,
                                "price": delta.price,
                                "qty_delta": delta.qty,
                                "latency_ms": None,
                                "ts_ms": ts_ms,
                            }
                        )
                state.mark_dirty(books.apply(events, mono_ns=time.monotonic_ns()))

        async def flush_books() -> None:
            while True:
                await asyncio.sleep(BOOK_FLUSH_INTERVAL_S)
                dirty_ids = state.take_dirty()
                for market_id in sorted(dirty_ids):
                    payload = state.book_payload(market_id)
                    if payload is not None:
                        state.broadcast(payload)
                # Rebind both once, up front: a control action can swap the
                # monitor (pairs reload) between these statements, and there
                # is an await below — `state.trader is not None` followed by
                # `state.trader.consider(...)` across an await is exactly the
                # race that a toggle would win.
                monitor, trader = state.arbmon, state.trader
                if monitor is not None and dirty_ids & monitor.market_ids:
                    state.broadcast({"t": "arb", "quotes": monitor.snapshot()})
                    if trader is not None:
                        ts_ms = time.time_ns() // 1_000_000
                        new_trades = []
                        for pair in monitor.affected(dirty_ids):
                            d1, d2 = monitor.best_quotes(pair)
                            best = (
                                d1 if d1.net_per_contract_ticks >= d2.net_per_contract_ticks else d2
                            )
                            # A suspended trader declines everything itself.
                            trade = trader.consider(pair, best, ts_ms=ts_ms)
                            if trade is not None:
                                new_trades.append(trade)
                        if new_trades:
                            try:
                                await paper_store.insert_trades(engine, run.run_id, new_trades)
                            except Exception:
                                log.warning("paper trade persist failed", exc_info=True)
                            state.broadcast(
                                {"t": "paper", "trades": [t.payload() for t in new_trades]}
                            )

        async def stats_loop() -> None:
            prev_total = state.msg_total
            prev_mono_ns = time.monotonic_ns()
            while True:
                await asyncio.sleep(STATS_INTERVAL_S)
                now_ns = time.monotonic_ns()
                total = state.msg_total
                dt_s = (now_ns - prev_mono_ns) / 1e9
                rate = (total - prev_total) / dt_s if dt_s > 0 else 0.0
                prev_total, prev_mono_ns = total, now_ns
                state.broadcast(state.stats_payload(msg_rate_1s=rate))

        async def poll_polymarket() -> None:
            url = config.polymarket_us_gateway_base + "/v1/markets?limit=1"
            while True:
                reachable = False
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        response = await client.get(url)
                    record_raw(
                        RawMessage(
                            venue="polymarket_us",
                            stream="rest:markets",
                            payload=response.content,
                            recv_ts_ns=time.time_ns(),
                            recv_mono_ns=time.monotonic_ns(),
                            run_id=run.run_id,
                            ingest_seq=run.next_ingest_seq(),
                        )
                    )
                    reachable = response.status_code == 200
                except Exception:
                    log.warning("polymarket_us gateway unreachable", exc_info=True)
                state.polymarket_rest_reachable = reachable
                await asyncio.sleep(PM_REACHABILITY_INTERVAL_S)

        tasks = [
            asyncio.create_task(supervise(consume, name="kalshi-ws-consume")),
            asyncio.create_task(supervise(flush_books, name="ui-book-flush")),
            asyncio.create_task(supervise(stats_loop, name="ui-stats")),
            asyncio.create_task(supervise(poll_polymarket, name="polymarket-us-reachability")),
        ]
        tasks.append(asyncio.create_task(supervise(consume_poly, name="polymarket-us-poll")))

        state.attach_control(control)
        app = create_app(state)
        # Pure-ASGI guard, outside FastAPI, so it also sees the WebSocket
        # handshake — the one scope Starlette's http middleware never gets.
        guarded = OriginGuardMiddleware(
            app,
            allowed_hosts=parse_csv(config.ui_allowed_hosts),
            allowed_origins=parse_csv(config.ui_allowed_origins),
        )
        server = uvicorn.Server(
            uvicorn.Config(guarded, host=host, port=port, log_config=None, access_log=False)
        )
        log.info(
            "ui listening on http://%s:%d%s%s",
            host,
            port,
            "" if bind.loopback else " (NON-LOOPBACK)",
            " [read-only]" if control.read_only else "",
        )
        await server.serve()  # returns (or raises KeyboardInterrupt) on Ctrl-C
    finally:
        if control is not None:
            await control.shutdown()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Flush what's queued before tearing the writer down.
        if not await recorder.drain(DRAIN_TIMEOUT_S):
            log.warning("recorder drain timed out; some queued messages were not written")
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        await engine.dispose()
