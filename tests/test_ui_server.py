"""UI app tests over ASGI (no network, no database): stubbed state object,
plus ServerState payload shapes fed from the real captured Kalshi frames."""

import asyncio
import time
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from arb.books import BookManager
from arb.types import RawMessage
from arb.ui.server import DatabaseStatus, ServerState, create_app
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"


class StubState:
    """Minimal UIState implementation with canned values."""

    def __init__(self) -> None:
        self.run_id = "testrun"
        self.recording = True
        self.polymarket_rest_reachable = True
        self.added = 0
        self.removed = 0

    def uptime_s(self) -> float:
        return 1.5

    def kalshi_status(self) -> tuple[str, str]:
        return "connecting", "awaiting first WebSocket frame"

    def polymarket_status(self) -> tuple[str, str]:
        return "down", "awaiting API credentials"

    async def database_status(self) -> DatabaseStatus:
        return DatabaseStatus(
            connected=True,
            raw_messages_total=42,
            runs=(("run-b", 30), ("run-a", 12)),
        )

    def hello_markets(self) -> list[dict[str, Any]]:
        return [{"market_id": "kalshi:AAA", "ticker": "AAA", "title": "", "volume_24h": 0.0}]

    def book_payloads(self) -> list[dict[str, Any]]:
        return []

    async def market_detail(self, market_id: str) -> dict[str, Any] | None:
        if market_id != "kalshi:AAA":
            return None
        return {"market_id": market_id, "ticker": "AAA", "source": "discovery"}

    async def list_pairs(self, status: str | None) -> list[dict[str, Any]]:
        rows = [
            {"id": 1, "status": "proposed", "score": 0.9},
            {"id": 2, "status": "confirmed", "score": 0.8},
        ]
        return [r for r in rows if status is None or r["status"] == status]

    async def decide_pair(self, pair_id: int, status: str) -> dict[str, Any] | None:
        return {"id": pair_id, "status": status, "score": 0.9} if pair_id == 1 else None

    async def decide_pairs(self, pair_ids: list[int], status: str) -> int:
        return len([i for i in pair_ids if i in (1, 2)])

    def add_client(self, ws: object) -> asyncio.Queue[str]:
        self.added += 1
        return asyncio.Queue()

    def remove_client(self, ws: object) -> None:
        self.removed += 1


def client_for(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://ui.test")


async def test_api_status_matches_contract(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/api/status")
    assert response.status_code == 200
    assert response.json() == {
        "run_id": "testrun",
        "uptime_s": 1.5,
        "venues": {
            "kalshi": {"state": "connecting", "detail": "awaiting first WebSocket frame"},
            "polymarket_us": {
                "state": "down",
                "detail": "awaiting API credentials",
                "rest_reachable": True,
            },
        },
        "recording": True,
        "database": {
            "connected": True,
            "raw_messages_total": 42,
            "runs": [{"run_id": "run-b", "count": 30}, {"run_id": "run-a", "count": 12}],
        },
    }


async def test_market_detail_route(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        ok = await client.get("/api/markets/kalshi:AAA")
        missing = await client.get("/api/markets/kalshi:NOPE")
    assert ok.status_code == 200
    assert ok.json()["ticker"] == "AAA"
    assert missing.status_code == 404
    assert missing.json()["market_id"] == "kalshi:NOPE"


async def test_pairs_routes(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        all_rows = await client.get("/api/pairs")
        confirmed = await client.get("/api/pairs?status=confirmed")
        bad = await client.get("/api/pairs?status=nope")
        decided = await client.post("/api/pairs/1/decide", json={"status": "rejected"})
        missing = await client.post("/api/pairs/9/decide", json={"status": "confirmed"})
        invalid = await client.post("/api/pairs/1/decide", json={"status": "maybe"})
        batch = await client.post(
            "/api/pairs/decide", json={"ids": [1, 2, 7], "status": "confirmed"}
        )
        batch_bad = await client.post("/api/pairs/decide", json={"ids": [], "status": "confirmed"})
    assert batch.status_code == 200 and batch.json() == {"updated": 2, "status": "confirmed"}
    assert batch_bad.status_code == 400
    assert [r["id"] for r in all_rows.json()["pairs"]] == [1, 2]
    assert [r["id"] for r in confirmed.json()["pairs"]] == [2]
    assert bad.status_code == 400
    assert decided.status_code == 200 and decided.json()["status"] == "rejected"
    assert missing.status_code == 404
    assert invalid.status_code == 400


async def test_root_falls_back_when_assets_missing(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.text == "UI assets missing"


async def test_root_serves_index_when_present(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>terminal</body></html>")
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        root = await client.get("/")
        static = await client.get("/static/index.html")
    assert root.status_code == 200
    assert "terminal" in root.text
    assert static.status_code == 200
    assert "terminal" in static.text


async def test_metrics_served_from_app(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert b"arb_ui_ws_clients" in response.content


def test_ws_sends_hello_then_registers_and_cleans_up(tmp_path: Path) -> None:
    state = StubState()
    app = create_app(state, static_dir=tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
    assert hello == {
        "t": "hello",
        "run_id": "testrun",
        "markets": [{"market_id": "kalshi:AAA", "ticker": "AAA", "title": "", "volume_24h": 0.0}],
    }
    assert state.added == 1
    assert state.removed == 1  # no client leak on disconnect


async def test_broadcast_never_blocks_and_drops_slow_clients() -> None:
    """The ingest path calls broadcast, so a stalled UI client must not stall
    it: frames go to bounded queues and an overflowing client is dropped."""

    class FakeWS:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            self.closed = True

    books = BookManager(staleness_limit_ns=10**12)
    state = ServerState(run_id="testrun", recording=False, books=books)
    fast, slow = FakeWS(), FakeWS()
    fast_queue = state.add_client(cast(WebSocket, fast))
    slow_queue = state.add_client(cast(WebSocket, slow))
    while True:  # jam the slow client's queue
        try:
            slow_queue.put_nowait("x")
        except asyncio.QueueFull:
            break

    state.broadcast({"t": "stats"})

    assert fast_queue.get_nowait() == '{"t": "stats"}'
    assert state.ws_clients == 1  # the slow client was dropped
    await asyncio.sleep(0)  # let the background close run
    assert slow.closed
    assert not fast.closed


def test_server_state_payload_shapes_from_real_capture() -> None:
    adapter = KalshiMarketDataAdapter()
    books = BookManager(staleness_limit_ns=10**12)
    state = ServerState(run_id="testrun", recording=False, books=books)
    for line in FIXTURE.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        raw = RawMessage(
            venue="kalshi",
            stream="ws",
            payload=line,
            recv_ts_ns=1,
            recv_mono_ns=1,
            run_id="testrun",
            ingest_seq=0,
        )
        state.on_kalshi_frame()
        state.mark_dirty(books.apply(adapter.parse(raw), mono_ns=time.monotonic_ns()))

    payloads = state.book_payloads()
    assert len(payloads) == 5
    for payload in payloads:
        assert payload["t"] == "book"
        assert payload["valid"] is True and payload["reason"] is None
        bid_prices = [price for price, _qty in payload["bids"]]
        ask_prices = [price for price, _qty in payload["asks"]]
        assert bid_prices == sorted(bid_prices, reverse=True)  # best (highest) first
        assert ask_prices == sorted(ask_prices)  # best (lowest) first
        assert all(isinstance(v, int) for pair in payload["bids"] + payload["asks"] for v in pair)
        assert payload["age_ms"] >= 0

    assert state.take_dirty() == {p["market_id"] for p in payloads}
    assert state.take_dirty() == set()

    state.record_latency(8.5)
    stats = state.stats_payload(msg_rate_1s=21.0)
    assert stats["t"] == "stats"
    assert stats["msg_total"] == 21
    assert stats["latency_ms"] == {"last": 8.5, "median": 8.5, "p95": 8.5, "n": 1}
    assert stats["recorder"] is None  # not recording
    assert stats["ws_clients"] == 0
    assert state.kalshi_status()[0] == "live"
