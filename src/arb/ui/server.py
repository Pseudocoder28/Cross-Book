"""Terminal UI backend: FastAPI app factory plus the live wiring.

``create_app(state)`` builds the HTTP/WS surface against a small
:class:`UIState` interface so tests can stub it (no network, no database).
``run_ui`` mirrors ``arb record``'s wiring — recorder-first ingest, supervised
tasks — and additionally parses frames through the Kalshi adapter, maintains
normalized books and pushes JSON frames to connected UI WebSocket clients.

Wire contract (server → client JSON text frames):

- ``hello`` on connect: run id + market list (sorted by 24h volume desc)
- ``book``: full YES-book state per market, coalesced to <= 10/s per market
- ``delta``: one tape entry per applied orderbook delta
- ``stats``: totals, rates and latency percentiles, broadcast every second

Prices are integer ticks of $0.0001; quantities are integer units of 0.0001
contracts. Metrics are served on ``GET /metrics`` from this app — ``run_ui``
does not start a separate metrics HTTP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from arb.book import BookLevelUpdate
from arb.books import BookManager
from arb.config import AppConfig
from arb.interfaces import ParseError, ResyncRequired
from arb.metrics import PARSE_ERRORS, UI_WS_CLIENTS, UI_WS_CLIENTS_DROPPED
from arb.recorder import Recorder
from arb.run import RunContext
from arb.storage.db import insert_raw_messages, make_engine
from arb.storage.models import RawMessageRow
from arb.supervise import supervise
from arb.types import RawMessage
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter
from arb.venues.kalshi.discovery import fetch_liquid_markets
from arb.venues.kalshi.rest import market_id as kalshi_market_id
from arb.venues.kalshi.source import KalshiWSSource

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


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    connected: bool
    raw_messages_total: int | None
    runs: tuple[tuple[str, int], ...]  # (run_id, message count), newest first


class UIState(Protocol):
    """What the routes need from the shared state. Tests stub this."""

    run_id: str
    recording: bool
    polymarket_rest_reachable: bool

    def uptime_s(self) -> float: ...

    def kalshi_status(self) -> tuple[str, str]:
        """(state, detail) where state is "live" | "connecting" | "down"."""
        ...

    async def database_status(self) -> DatabaseStatus: ...

    def hello_markets(self) -> list[dict[str, Any]]: ...

    def book_payloads(self) -> list[dict[str, Any]]:
        """Current full-state "book" messages, for freshly connected clients."""
        ...

    def add_client(self, ws: WebSocket) -> asyncio.Queue[str]:
        """Register a client; returns its bounded outbound frame queue."""
        ...

    def remove_client(self, ws: WebSocket) -> None: ...


def create_app(state: UIState, *, static_dir: Path | None = None) -> FastAPI:
    static_root = static_dir if static_dir is not None else STATIC_DIR
    app = FastAPI(title="arb ui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=static_root, check_dir=False), name="static")

    @app.get("/")
    async def index() -> Response:
        index_file = static_root / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return PlainTextResponse("UI assets missing")

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        kalshi_state, kalshi_detail = state.kalshi_status()
        db = await state.database_status()
        return {
            "run_id": state.run_id,
            "uptime_s": state.uptime_s(),
            "venues": {
                "kalshi": {"state": kalshi_state, "detail": kalshi_detail},
                "polymarket_us": {
                    "state": "down",
                    "detail": "awaiting API credentials",
                    "rest_reachable": state.polymarket_rest_reachable,
                },
            },
            "recording": state.recording,
            "database": {
                "connected": db.connected,
                "raw_messages_total": db.raw_messages_total,
                "runs": [{"run_id": run_id, "count": count} for run_id, count in db.runs],
            },
        }

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
        return payloads

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

    def on_kalshi_frame(self) -> None:
        self.msg_total += 1
        self._kalshi_last_frame_mono_ns = time.monotonic_ns()

    def record_latency(self, latency_ms: float) -> None:
        self.last_latency_ms = latency_ms
        self._latencies.append(latency_ms)

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
        window = sorted(self._latencies)
        n = len(window)
        recorder = (
            {"enqueued": self.recorder_enqueued, "dropped": self.recorder_dropped}
            if self.recording
            else None
        )
        return {
            "t": "stats",
            "msg_total": self.msg_total,
            "msg_rate_1s": msg_rate_1s,
            "latency_ms": {
                "last": self.last_latency_ms,
                "median": statistics.median(window) if n else None,
                "p95": window[min(n - 1, int(0.95 * n))] if n else None,
                "n": n,
            },
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
) -> None:
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

    recorder: Recorder | None = None
    writer: asyncio.Task[object] | None = None
    if record:
        recorder = Recorder(
            partial(insert_raw_messages, engine),
            queue_max=config.recorder_queue_max,
            batch_max=config.recorder_batch_max,
        )
        writer = asyncio.create_task(supervise(recorder.run, name="recorder-writer"))

    def record_raw(message: RawMessage) -> None:
        if recorder is None:
            return
        if recorder.enqueue(message):
            state.recorder_enqueued += 1
        else:
            state.recorder_dropped += 1

    tasks: list[asyncio.Task[object]] = []
    try:
        if not tickers:
            discovered = await fetch_liquid_markets(
                config, run, top_n=top_n, sink=record_raw if record else None
            )
            log.info("discovered %d liquid kalshi markets", len(discovered))
            tickers = [m.ticker for m in discovered]
            state.set_markets(
                [
                    {
                        "market_id": kalshi_market_id(m.ticker),
                        "ticker": m.ticker,
                        "title": m.title,
                        "volume_24h": m.volume_24h,
                    }
                    for m in discovered
                ]
            )
        else:
            # Explicit tickers: no discovery metadata available.
            state.set_markets(
                [
                    {"market_id": kalshi_market_id(t), "ticker": t, "title": "", "volume_24h": 0.0}
                    for t in tickers
                ]
            )

        source = KalshiWSSource(config=config, run=run, market_tickers=tickers)
        adapter = KalshiMarketDataAdapter()

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

        async def flush_books() -> None:
            while True:
                await asyncio.sleep(BOOK_FLUSH_INTERVAL_S)
                for market_id in sorted(state.take_dirty()):
                    payload = state.book_payload(market_id)
                    if payload is not None:
                        state.broadcast(payload)

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

        app = create_app(state)
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
        )
        log.info("ui listening on http://%s:%d", host, port)
        await server.serve()  # returns (or raises KeyboardInterrupt) on Ctrl-C
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if recorder is not None and writer is not None:
            # Flush what's queued before tearing the writer down.
            if not await recorder.drain(DRAIN_TIMEOUT_S):
                log.warning("recorder drain timed out; some queued messages were not written")
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        await engine.dispose()
