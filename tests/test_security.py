"""The UI's network security floor: the origin/host guard over http and
websocket, the non-loopback bind guard, and the control-action audit table
round-tripping on SQLite.

Note for anyone wiring the middleware into ``create_app``: Starlette's
``TestClient`` sends ``Host: testserver`` by default (and hardcodes it for
``websocket_connect``), which the host guard correctly refuses. Tests that go
through the guard must set ``base_url="http://127.0.0.1:8080"`` and, for
websockets, pass ``headers={"Host": "127.0.0.1:8080"}``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from arb.config import AppConfig
from arb.storage.models import (
    Base,
    insert_control_action,
    list_control_actions,
)
from arb.ui.security import (
    BindInfo,
    OriginGuardMiddleware,
    RemoteBindRefused,
    check_bind_host,
    is_loopback_host,
    is_loopback_origin,
    parse_csv,
    split_host_port,
)

LOOPBACK = "http://127.0.0.1:8080"
EVIL = "https://evil.example"


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/status")
    async def status() -> dict[str, str]:
        return {"state": "ok"}

    @app.post("/api/pairs/decide")
    async def decide() -> dict[str, str]:
        return {"decided": "yes"}

    @app.websocket("/ws")
    async def feed(sock: WebSocket) -> None:
        await sock.accept()
        await sock.send_json({"t": "hello"})
        await sock.close()

    return app


def client(**guard_kwargs: Any) -> TestClient:
    return TestClient(OriginGuardMiddleware(build_app(), **guard_kwargs), base_url=LOOPBACK)


# --------------------------------------------------------------------------
# host guard (DNS rebinding)
# --------------------------------------------------------------------------


def test_foreign_host_header_is_refused_even_on_get() -> None:
    """A DNS-rebound request reaches 127.0.0.1 but still carries the
    attacker's hostname in Host."""
    with TestClient(OriginGuardMiddleware(build_app()), base_url="http://evil.example") as c:
        assert c.get("/api/status").status_code == 403


def test_loopback_host_headers_are_allowed() -> None:
    for host in ("127.0.0.1:8080", "localhost:8080", "[::1]:8080", "127.0.0.1"):
        with TestClient(OriginGuardMiddleware(build_app()), base_url=LOOPBACK) as c:
            assert c.get("/api/status", headers={"Host": host}).status_code == 200, host


def test_configured_host_is_allowed() -> None:
    with TestClient(
        OriginGuardMiddleware(build_app(), allowed_hosts=["arb.internal"]),
        base_url="http://arb.internal",
    ) as c:
        assert c.get("/api/status").status_code == 200


# --------------------------------------------------------------------------
# origin guard on state-changing HTTP (CSRF)
# --------------------------------------------------------------------------


def test_cross_site_post_is_rejected() -> None:
    """Recon verified this returned 200 before the guard existed."""
    with client() as c:
        response = c.post("/api/pairs/decide", headers={"Origin": EVIL})
    assert response.status_code == 403
    assert "origin" in response.text


def test_cross_site_post_is_rejected_by_sec_fetch_site_even_without_origin() -> None:
    with client() as c:
        response = c.post("/api/pairs/decide", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "sec_fetch_site" in response.text


def test_null_origin_is_rejected() -> None:
    with client() as c:
        assert c.post("/api/pairs/decide", headers={"Origin": "null"}).status_code == 403


def test_same_origin_post_is_allowed() -> None:
    with client() as c:
        response = c.post(
            "/api/pairs/decide",
            headers={"Origin": LOOPBACK, "Sec-Fetch-Site": "same-origin"},
        )
    assert response.status_code == 200
    assert response.json() == {"decided": "yes"}


def test_post_without_an_origin_is_allowed() -> None:
    """curl / httpx / the CLI: already a process on this machine."""
    with client() as c:
        assert c.post("/api/pairs/decide").status_code == 200


def test_configured_origin_is_allowed() -> None:
    with client(allowed_origins=["https://ops.example"]) as c:
        response = c.post("/api/pairs/decide", headers={"Origin": "https://ops.example"})
    assert response.status_code == 200


def test_cross_site_get_is_still_allowed() -> None:
    """Reads are not state-changing, and the same-origin policy already keeps
    the response body away from the attacker's page."""
    with client() as c:
        assert c.get("/api/status", headers={"Origin": EVIL}).status_code == 200


# --------------------------------------------------------------------------
# origin guard on the websocket handshake
# --------------------------------------------------------------------------


def ws_headers(**extra: str) -> dict[str, str]:
    # TestClient hardcodes Host: testserver for websocket_connect.
    return {"Host": "127.0.0.1:8080", **extra}


def test_cross_site_websocket_is_rejected() -> None:
    """Recon verified WS /ws accepted any Origin. The same-origin policy does
    not cover WebSocket, so Origin is the only thing standing here."""
    with client() as c, pytest.raises(WebSocketDisconnect) as excinfo:
        with c.websocket_connect("/ws", headers=ws_headers(Origin=EVIL)):
            pass
    assert excinfo.value.code == 1008


def test_cross_site_websocket_is_rejected_by_sec_fetch_site() -> None:
    headers = ws_headers()
    headers["Sec-Fetch-Site"] = "cross-site"
    with client() as c, pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers=headers):
            pass


def test_foreign_host_websocket_is_rejected() -> None:
    with client() as c, pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers=ws_headers(Host="evil.example")):
            pass


def test_same_origin_websocket_is_allowed() -> None:
    with client() as c:
        with c.websocket_connect("/ws", headers=ws_headers(Origin=LOOPBACK)) as sock:
            assert sock.receive_json() == {"t": "hello"}


def test_websocket_without_an_origin_is_allowed() -> None:
    with client() as c:
        with c.websocket_connect("/ws", headers=ws_headers()) as sock:
            assert sock.receive_json() == {"t": "hello"}


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------


def test_split_host_port() -> None:
    assert split_host_port("127.0.0.1:8080") == ("127.0.0.1", 8080)
    assert split_host_port("LocalHost") == ("localhost", None)
    assert split_host_port("[::1]:8080") == ("::1", 8080)
    assert split_host_port("::1") == ("::1", None)
    assert split_host_port("host:notaport") == ("host", None)


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.0.0.53")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.4")
    assert not is_loopback_host("evil.example")
    assert not is_loopback_host("")


def test_is_loopback_origin() -> None:
    assert is_loopback_origin("http://127.0.0.1:8080")
    assert is_loopback_origin("https://localhost")
    assert not is_loopback_origin("null")
    assert not is_loopback_origin("file://")
    assert not is_loopback_origin("http://evil.example")
    # A hostname that merely *contains* a loopback-looking label is not one.
    assert not is_loopback_origin("http://127.0.0.1.evil.example")


def test_parse_csv() -> None:
    assert parse_csv("") == ()
    assert parse_csv(None) == ()
    assert parse_csv("a, b ,,c") == ("a", "b", "c")
    assert parse_csv(["a", " b "]) == ("a", "b")


# --------------------------------------------------------------------------
# bind guard
# --------------------------------------------------------------------------


def test_non_loopback_bind_is_refused_without_the_escape_hatch() -> None:
    with pytest.raises(RemoteBindRefused) as excinfo:
        check_bind_host("0.0.0.0", allow_remote=False)
    assert "ARB_ALLOW_REMOTE_BIND" in str(excinfo.value)


def test_non_loopback_bind_is_allowed_with_the_escape_hatch() -> None:
    info = check_bind_host("0.0.0.0", allow_remote=True)
    assert info == BindInfo(host="0.0.0.0", loopback=False, remote_allowed=True)


def test_loopback_bind_needs_no_hatch() -> None:
    assert check_bind_host("127.0.0.1", allow_remote=False).loopback is True
    assert check_bind_host("::1", allow_remote=False).loopback is True


def test_config_defaults_are_loopback_and_writable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UI_HOST", "ARB_ALLOW_REMOTE_BIND", "UI_ALLOW_REMOTE_BIND", "UI_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert config.ui_host == "127.0.0.1"
    assert config.ui_allow_remote_bind is False
    assert config.ui_read_only is False
    check_bind_host(config.ui_host, allow_remote=config.ui_allow_remote_bind)


def test_escape_hatch_reads_the_compose_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_ALLOW_REMOTE_BIND", "1")
    monkeypatch.setenv("UI_HOST", "0.0.0.0")
    monkeypatch.setenv("UI_READ_ONLY", "1")
    config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert config.ui_allow_remote_bind is True
    assert config.ui_read_only is True
    info = check_bind_host(config.ui_host, allow_remote=config.ui_allow_remote_bind)
    assert info.loopback is False  # the UI shows a banner for this


def test_allowlists_come_through_as_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UI_ALLOWED_ORIGINS", "https://ops.example, https://two.example")
    config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert parse_csv(config.ui_allowed_origins) == ("https://ops.example", "https://two.example")


# --------------------------------------------------------------------------
# control_actions audit table
# --------------------------------------------------------------------------


async def make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def test_audit_round_trips() -> None:
    engine = await make_engine()
    try:
        row_id = await insert_control_action(
            engine,
            run_id="testrun",
            action="recording.stop",
            effect="Stops recording. Raw market data after now is not saved.",
            actor="ui",
            result="ok",
            params={"reason": "disk"},
            ts_ns=1_000,
        )
        assert row_id > 0
        rows = await list_control_actions(engine, run_id="testrun")
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["ts_ns"] == 1_000
        assert rows[0]["action"] == "recording.stop"
        assert rows[0]["params"] == {"reason": "disk"}
        # The sentence shown to the operator is stored verbatim.
        assert rows[0]["effect"].startswith("Stops recording.")
        assert rows[0]["actor"] == "ui"
        assert rows[0]["result"] == "ok"
        assert rows[0]["error"] is None
        assert rows[0]["created_at"] is not None
    finally:
        await engine.dispose()


async def test_audit_records_failures_and_orders_newest_first() -> None:
    engine = await make_engine()
    try:
        await insert_control_action(
            engine, run_id="r", action="a.one", effect="one", ts_ns=1, actor="ui"
        )
        await insert_control_action(
            engine,
            run_id="r",
            action="a.two",
            effect="two",
            ts_ns=2,
            actor="cli",
            result="error",
            error="boom",
        )
        rows = await list_control_actions(engine)
        assert [r["action"] for r in rows] == ["a.two", "a.one"]
        assert rows[0]["result"] == "error"
        assert rows[0]["error"] == "boom"
        assert rows[0]["params"] == {}
    finally:
        await engine.dispose()


async def test_audit_filters_and_limits_in_sql() -> None:
    engine = await make_engine()
    try:
        for i in range(5):
            await insert_control_action(
                engine,
                run_id="run-a" if i % 2 == 0 else "run-b",
                action="pairs.propose" if i < 2 else "recording.start",
                effect=f"effect {i}",
                ts_ns=i,
            )
        assert len(await list_control_actions(engine, run_id="run-a")) == 3
        assert len(await list_control_actions(engine, action="pairs.propose")) == 2
        assert len(await list_control_actions(engine, limit=2)) == 2
    finally:
        await engine.dispose()
