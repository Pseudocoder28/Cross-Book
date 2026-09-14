import asyncio
from contextlib import aclosing

from arb.run import RunContext
from arb.types import RawMessage
from arb.ws import ReconnectingWebSocket, WSConfig, WSConnection


class FakeConnection:
    """Scripted connection: serves frames, then hangs or drops.

    Like the real ``websockets`` connection, a hanging ``recv`` raises once
    ``close`` is called.
    """

    def __init__(self, frames: list[str | bytes], *, hang_when_empty: bool = False) -> None:
        self._frames = list(frames)
        self._hang_when_empty = hang_when_empty
        self.sent: list[str | bytes] = []
        self.closed = False
        self._closed_event = asyncio.Event()

    async def recv(self) -> str | bytes:
        if self.closed:
            raise ConnectionError("connection closed")
        if self._frames:
            return self._frames.pop(0)
        if self._hang_when_empty:
            await self._closed_event.wait()
        raise ConnectionError("connection dropped")

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


def fast_config() -> WSConfig:
    return WSConfig(
        connect_timeout_s=1.0,
        stall_timeout_s=0.05,
        backoff_initial_s=0.001,
        backoff_max_s=0.002,
        backoff_jitter_frac=0.0,
        healthy_after_s=999.0,
    )


async def collect(source: ReconnectingWebSocket, n: int) -> list[RawMessage]:
    out: list[RawMessage] = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            out.append(message)
            if len(out) >= n:
                break
    return out


async def test_envelopes_reconnect_and_resubscribe() -> None:
    run = RunContext(run_id="testrun")
    connections: list[FakeConnection] = []
    subscribed: list[WSConnection] = []

    async def connector() -> WSConnection:
        conn = FakeConnection(["a", b"b"])  # two frames, then the connection drops
        connections.append(conn)
        return conn

    async def on_connected(conn: WSConnection) -> None:
        subscribed.append(conn)
        await conn.send("subscribe-please")

    source = ReconnectingWebSocket(
        venue="testvenue",
        stream_name="ws",
        run=run,
        connector=connector,
        config=fast_config(),
        on_connected=on_connected,
    )
    messages = await collect(source, 4)

    # str frames arrive UTF-8 encoded, bytes pass through.
    assert [m.payload for m in messages] == [b"a", b"b", b"a", b"b"]
    # ingest_seq is per-run and survives reconnects without gaps.
    assert [m.ingest_seq for m in messages] == [0, 1, 2, 3]
    assert all(m.run_id == "testrun" for m in messages)
    assert all(m.venue == "testvenue" and m.stream == "ws" for m in messages)
    assert all(m.recv_ts_ns > 0 and m.recv_mono_ns > 0 for m in messages)
    # Two connections were made; each got the subscribe callback; both closed.
    assert len(connections) == 2
    assert subscribed == connections
    assert all(conn.sent == ["subscribe-please"] for conn in connections)
    assert all(conn.closed for conn in connections)


async def test_stall_triggers_reconnect() -> None:
    attempts = 0

    async def connector() -> WSConnection:
        nonlocal attempts
        attempts += 1
        return FakeConnection(["x"], hang_when_empty=True)

    source = ReconnectingWebSocket(
        venue="testvenue",
        stream_name="ws",
        run=RunContext(),
        connector=connector,
        config=fast_config(),
    )
    messages = await collect(source, 2)
    assert [m.payload for m in messages] == [b"x", b"x"]
    assert attempts == 2  # the silent connection was declared stalled


async def test_force_reconnect_closes_and_resubscribes() -> None:
    """force_reconnect drops the live connection; the loop reconnects and
    resubscribes — which is how a consumer gets fresh snapshots after a gap."""
    run = RunContext(run_id="testrun")
    connections: list[FakeConnection] = []
    subscribed: list[WSConnection] = []

    async def connector() -> WSConnection:
        conn = FakeConnection(["snap"], hang_when_empty=True)
        connections.append(conn)
        return conn

    async def on_connected(conn: WSConnection) -> None:
        subscribed.append(conn)

    config = fast_config()
    config.stall_timeout_s = 5.0  # only force_reconnect can trigger the retry
    source = ReconnectingWebSocket(
        venue="testvenue",
        stream_name="ws",
        run=run,
        connector=connector,
        config=config,
        on_connected=on_connected,
    )
    out: list[RawMessage] = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            out.append(message)
            if len(out) == 1:
                await source.force_reconnect()
            if len(out) >= 2:
                break
    assert [m.payload for m in out] == [b"snap", b"snap"]
    assert len(connections) == 2
    assert connections[0].closed
    assert subscribed == connections  # resubscribed on the new connection


async def test_connect_failure_backs_off_and_retries() -> None:
    attempts = 0

    async def connector() -> WSConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("connection refused")
        return FakeConnection(["ok"], hang_when_empty=True)

    source = ReconnectingWebSocket(
        venue="testvenue",
        stream_name="ws",
        run=RunContext(),
        connector=connector,
        config=fast_config(),
    )
    messages = await collect(source, 1)
    assert messages[0].payload == b"ok"
    assert attempts == 2
