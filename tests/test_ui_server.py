"""UI app tests over ASGI (no network, no database): stubbed state object,
plus ServerState payload shapes fed from the real captured Kalshi frames."""

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx
from fastapi import FastAPI, WebSocket
from prometheus_client import REGISTRY
from starlette.testclient import TestClient

from arb.books import BookManager
from arb.config import AppConfig
from arb.paper import PaperLimits, PaperTrader
from arb.run import RunContext
from arb.types import RawMessage
from arb.ui.control import ControlResult, NotAvailable
from arb.ui.server import (
    SPA_ROUTES,
    DatabaseStatus,
    ServerState,
    create_app,
    no_control_payload,
)
from arb.venues.kalshi.adapter import KalshiMarketDataAdapter
from arb.venues.polymarket_us.adapter import PolymarketUSMarketDataAdapter
from arb.venues.polymarket_us.source import PolymarketUSRestSource

FIXTURE = Path(__file__).parent / "fixtures" / "kalshi" / "ws_orderbook_capture.jsonl"


class StubState:
    """Minimal UIState implementation with canned values."""

    def __init__(self) -> None:
        self.run_id = "testrun"
        self.recording = True
        self.polymarket_rest_reachable = True
        self.added = 0
        self.removed = 0
        self.control = no_control_payload(run_id="testrun", recording=True)
        self.controls: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self.audit: list[dict[str, Any]] = []

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

    def arb_snapshot(self) -> list[dict[str, Any]]:
        return [{"pair_id": 1, "best": {"net_per_contract_ticks": 12}}]

    async def paper_payload(self) -> dict[str, Any]:
        return {"enabled": False, "totals": {"trades": 0}, "positions": [], "trades": []}

    def control_payload(self) -> dict[str, Any]:
        return self.control

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        return {"job_id": job_id, "status": "ok", "lines": ["done"]} if job_id == "j1" else None

    async def execute_control(
        self, action: str, params: dict[str, Any] | None, *, confirm: str | None
    ) -> ControlResult:
        # The stub has no runtime to drive, so every action is unavailable —
        # which is exactly what a process with no control plane should answer.
        self.controls.append((action, params, confirm))
        raise NotAvailable("stub has no control plane")

    async def control_log(self, limit: int) -> list[dict[str, Any]]:
        return self.audit[:limit]

    def add_client(self, ws: object) -> asyncio.Queue[str]:
        self.added += 1
        return asyncio.Queue()

    def remove_client(self, ws: object) -> None:
        self.removed += 1


def client_for(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://ui.test")


async def test_api_status_matches_contract(tmp_path: Path) -> None:
    state = StubState()
    app = create_app(state, static_dir=tmp_path)
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
        "control": state.control,
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


async def test_arb_route(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/api/arb")
    assert response.status_code == 200
    assert response.json()["quotes"][0]["best"]["net_per_contract_ticks"] == 12


async def test_paper_route(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/api/paper")
    assert response.status_code == 200 and response.json()["enabled"] is False


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


async def test_spa_routes_serve_shell_when_present(tmp_path: Path) -> None:
    """Every client-side route is a deep link: a fresh GET returns the shell."""
    (tmp_path / "index.html").write_text("<html><body>terminal</body></html>")
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        for path in SPA_ROUTES:
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "terminal" in response.text, path


async def test_spa_routes_fall_back_when_assets_missing(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        for path in SPA_ROUTES:
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.text == "UI assets missing", path


async def test_market_page_route_serves_shell(tmp_path: Path) -> None:
    """market_id is opaque: it is never checked against the known markets, and
    an id needing URL escaping still resolves to the shell."""
    (tmp_path / "index.html").write_text("<html><body>terminal</body></html>")
    app = create_app(StubState(), static_dir=tmp_path)
    ids = [
        "kalshi:AAA",
        "kalshi:NOT-A-KNOWN-MARKET",  # rolled off discovery: must not 404
        "polymarket_us:will-x-happen?",
        "kalshi:A B/C",  # escapes to %20 and %2F
    ]
    async with client_for(app) as client:
        responses = [await client.get("/market/" + quote(market_id, safe="")) for market_id in ids]
    for market_id, response in zip(ids, responses, strict=True):
        assert response.status_code == 200, market_id
        assert "terminal" in response.text, market_id


async def test_market_page_falls_back_when_assets_missing(tmp_path: Path) -> None:
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        response = await client.get("/market/" + quote("kalshi:AAA", safe=""))
    assert response.status_code == 200
    assert response.text == "UI assets missing"


async def test_spa_routes_do_not_shadow_the_api(tmp_path: Path) -> None:
    """Regression guard: a greedy catch-all would answer the shell here and
    silently break every real endpoint."""
    (tmp_path / "index.html").write_text("<html><body>shell-sentinel</body></html>")
    (tmp_path / "app.js").write_text("export const marker = 1;\n")
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        status = await client.get("/api/status")
        detail = await client.get("/api/markets/kalshi:AAA")
        pairs = await client.get("/api/pairs")
        arb = await client.get("/api/arb")
        paper = await client.get("/api/paper")
        metrics = await client.get("/metrics")
        asset = await client.get("/static/app.js")
    assert status.status_code == 200 and status.json()["run_id"] == "testrun"
    assert detail.status_code == 200 and detail.json()["ticker"] == "AAA"
    assert pairs.status_code == 200 and [r["id"] for r in pairs.json()["pairs"]] == [1, 2]
    assert arb.status_code == 200 and "quotes" in arb.json()
    assert paper.status_code == 200 and paper.json()["enabled"] is False
    assert metrics.status_code == 200 and b"arb_ui_ws_clients" in metrics.content
    assert asset.status_code == 200 and asset.text == "export const marker = 1;\n"
    for response in (status, detail, pairs, arb, paper, metrics, asset):
        assert "shell-sentinel" not in response.text


async def test_unknown_path_is_still_404(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>terminal</body></html>")
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        nope = await client.get("/nope")
        nested = await client.get("/arb/deeper")
        api = await client.get("/api/nope")
    assert nope.status_code == 404
    assert nested.status_code == 404
    assert api.status_code == 404


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
            control = ws.receive_json()
    assert hello == {
        "t": "hello",
        "run_id": "testrun",
        "markets": [{"market_id": "kalshi:AAA", "ticker": "AAA", "title": "", "volume_24h": 0.0}],
    }
    # Control state rides in on connect, before any book: a tab that opens
    # mid-run must not have to guess whether paper trading is on.
    assert control["t"] == "control"
    assert control["control"]["run_id"] == "testrun"
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


def sample_value(name: str, labels: dict[str, str]) -> float:
    """Current value of a process-global collector, 0.0 when never touched."""
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_negative_latency_sample_is_counted_not_corrected() -> None:
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    labels = {"venue": "kalshi"}
    before_neg = sample_value("arb_ws_one_way_latency_negative_total", labels)
    before_count = sample_value("arb_ws_one_way_latency_ms_count", labels)
    # A one-way delay cannot be negative: this is the clock-skew signature.
    below_zero = {**labels, "le": "0.0"}
    before_below_zero = sample_value("arb_ws_one_way_latency_ms_bucket", below_zero)

    state.record_latency(-27.4)

    assert sample_value("arb_ws_one_way_latency_negative_total", labels) - before_neg == 1.0
    assert sample_value("arb_ws_one_way_latency_ms_count", labels) - before_count == 1.0
    assert sample_value("arb_ws_one_way_latency_ms_bucket", below_zero) - before_below_zero == 1.0
    assert state.last_latency_ms == -27.4  # stored raw, never de-biased

    state.record_latency(9.0)

    assert sample_value("arb_ws_one_way_latency_negative_total", labels) - before_neg == 1.0
    assert sample_value("arb_ws_one_way_latency_ms_count", labels) - before_count == 2.0
    assert sample_value("arb_ws_one_way_latency_ms_bucket", below_zero) - before_below_zero == 1.0


def test_stats_payload_publishes_skew_and_rtt_gauges() -> None:
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    state.rtt_fn = lambda: 25.0
    for latency_ms in (-30.0, -28.0, -26.0):
        state.record_latency(latency_ms)

    stats = state.stats_payload(msg_rate_1s=0.0)

    assert stats["clock_skew_ms"] == -28.0 - 25.0 / 2
    # Same arithmetic reaches Grafana, so the two can never disagree.
    assert sample_value("arb_clock_skew_ms", {"venue": "kalshi"}) == stats["clock_skew_ms"]
    assert sample_value("arb_ws_rtt_ms", {"venue": "kalshi", "stream": "ws"}) == stats["rtt_ms"]


def test_gauges_go_nan_when_nothing_was_measured() -> None:
    """A stale gauge would read as a live measurement of an outage."""
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    state.rtt_fn = lambda: 25.0
    state.record_latency(-28.0)
    state.stats_payload(msg_rate_1s=0.0)

    state.rtt_fn = lambda: None  # what a reconnecting WebSocket reports
    stats = state.stats_payload(msg_rate_1s=0.0)

    assert stats["rtt_ms"] is None
    assert stats["clock_skew_ms"] is None
    assert math.isnan(sample_value("arb_ws_rtt_ms", {"venue": "kalshi", "stream": "ws"}))
    assert math.isnan(sample_value("arb_clock_skew_ms", {"venue": "kalshi"}))


async def test_control_routes_are_read_only_views(tmp_path: Path) -> None:
    """The control state and one job's log are G0 reads. The write endpoints
    land with the control page; these two are how the browser catches up."""
    app = create_app(StubState(), static_dir=tmp_path)
    async with client_for(app) as client:
        control = await client.get("/api/control")
        job = await client.get("/api/control/jobs/j1")
        missing = await client.get("/api/control/jobs/nope")
    assert control.status_code == 200
    assert control.json()["run_id"] == "testrun"
    assert control.json()["read_only"] is True  # nothing attached: nothing to drive
    assert job.status_code == 200 and job.json()["lines"] == ["done"]
    assert missing.status_code == 404 and missing.json()["job_id"] == "nope"


def test_server_state_control_payload_without_a_plane() -> None:
    """A UI with no control plane still answers one shape, and claims nothing."""
    state = ServerState(
        run_id="testrun", recording=True, books=BookManager(staleness_limit_ns=10**12)
    )
    payload = state.control_payload()
    assert payload["read_only"] is True
    assert payload["recording"] is True
    assert payload["jobs"] == [] and payload["actions"] == []
    assert state.job_payload("anything") is None


def test_paper_payload_reports_the_live_suspend_state() -> None:
    """Regression guard: the ledger used to be reported enabled=True always,
    so a suspended trader looked live."""
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    state.trader = PaperTrader(PaperLimits())
    state.trader.suspend()
    payload = asyncio.run(state.paper_payload())
    assert payload["enabled"] is False and payload["suspended"] is True
    assert state.trader.resume() is True
    assert asyncio.run(state.paper_payload())["enabled"] is True


def test_polymarket_status_is_idle_not_down_without_targets() -> None:
    """An empty poll set is a supported state (the poller naps); calling it
    "down" would report a venue outage that is not happening."""
    config = AppConfig()
    source = PolymarketUSRestSource(config=config, run=RunContext("r"), slugs=["a"])
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    state.attach_polymarket(source, PolymarketUSMarketDataAdapter())
    assert state.polymarket_status()[0] == "connecting"
    source.set_targets([])
    assert state.polymarket_status() == ("idle", "no poll targets")


async def test_control_execute_route_maps_control_errors(tmp_path: Path) -> None:
    """Every ControlError carries its own status; the route must not invent one."""
    state = StubState()
    app = create_app(state, static_dir=tmp_path)
    async with client_for(app) as client:
        # The stub has no plane, so NotAvailable (409) is the honest answer.
        unavailable = await client.post("/api/control/recording.stop", json={})
        bad_params = await client.post("/api/control/recording.stop", json={"params": 7})
        bad_confirm = await client.post("/api/control/recording.stop", json={"confirm": 7})
    assert unavailable.status_code == 409
    assert "control plane" in unavailable.json()["error"]
    assert bad_params.status_code == 400
    assert bad_confirm.status_code == 400
    # The action and its params still reached the executor verbatim.
    assert state.controls == [("recording.stop", None, None)]


async def test_control_log_route_is_capped(tmp_path: Path) -> None:
    state = StubState()
    state.audit = [{"id": i, "action": "recording.stop"} for i in range(200)]
    app = create_app(state, static_dir=tmp_path)
    async with client_for(app) as client:
        default = await client.get("/api/control/log")
        capped = await client.get("/api/control/log?limit=5")
        absurd = await client.get("/api/control/log?limit=100000")
    assert len(default.json()["actions"]) == 50
    assert len(capped.json()["actions"]) == 5
    # A caller asking for everything gets the ceiling, not the whole table.
    assert len(absurd.json()["actions"]) == 200


def test_universe_change_pushes_a_fresh_hello() -> None:
    """A confirmed pair's legs must reach MONITOR without a page reload.

    The browser builds its market list from the `hello` frame and `onHello` is
    written to be re-run. Before this, a runtime universe change subscribed
    the new legs and streamed their books while MONITOR still showed the list
    from connect time.
    """
    state = ServerState(
        run_id="testrun", recording=False, books=BookManager(staleness_limit_ns=10**12)
    )
    sent: list[str] = []
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
    state._clients[cast(Any, object())] = queue  # pyright: ignore[reportPrivateUsage]

    state.set_markets([{"market_id": "kalshi:AAA", "ticker": "AAA", "venue": "kalshi"}])
    state.add_markets(
        [{"market_id": "polymarket_us:bbb", "ticker": "bbb", "venue": "polymarket_us"}]
    )

    while not queue.empty():
        sent.append(queue.get_nowait())
    frames = [json.loads(s) for s in sent]
    hellos = [f for f in frames if f.get("t") == "hello"]
    assert len(hellos) == 2, "both set_markets and add_markets must re-announce"
    # The last one carries the whole universe, not just the delta.
    assert [m["market_id"] for m in hellos[-1]["markets"]] == [
        "kalshi:AAA",
        "polymarket_us:bbb",
    ]
    assert hellos[-1]["run_id"] == "testrun"
