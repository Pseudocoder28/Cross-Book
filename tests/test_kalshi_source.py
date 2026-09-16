import asyncio
import json
from contextlib import aclosing

from arb.config import AppConfig
from arb.run import RunContext
from arb.venues.kalshi.source import KalshiWSSource
from arb.ws import WSConfig, WSConnection


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str | bytes] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionError("dropped")

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


def config() -> AppConfig:
    cfg = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    cfg.kalshi_api_key_id = "test-key-id"
    return cfg


async def test_resubscribes_with_fresh_cmd_id_on_every_connect() -> None:
    connections: list[FakeConnection] = []

    async def connector() -> WSConnection:
        conn = FakeConnection(['{"type":"subscribed"}'])
        connections.append(conn)
        return conn

    source = KalshiWSSource(
        config=config(),
        run=RunContext(run_id="testrun"),
        market_tickers=["AAA", "BBB"],
        ws_config=WSConfig(backoff_initial_s=0.001, backoff_max_s=0.002, backoff_jitter_frac=0),
        connector=connector,
    )
    assert source.venue == "kalshi"

    received = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            received.append(message)
            if len(received) >= 2:  # one frame per connection → 2 connects
                break

    assert len(connections) == 2
    for index, conn in enumerate(connections, start=1):
        assert len(conn.sent) == 1
        cmd = json.loads(conn.sent[0])
        assert cmd["cmd"] == "subscribe"
        assert cmd["id"] == index  # fresh command id each reconnect
        assert cmd["params"] == {
            "channels": ["orderbook_delta"],
            "market_tickers": ["AAA", "BBB"],
        }
    assert all(m.venue == "kalshi" and m.stream == "ws" for m in received)


async def test_rejects_missing_credentials_or_tickers() -> None:
    import pytest

    cfg = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValueError):
        KalshiWSSource(config=cfg, run=RunContext(), market_tickers=["AAA"])
    with pytest.raises(ValueError):
        KalshiWSSource(config=config(), run=RunContext(), market_tickers=[])


async def test_recorder_drain_waits_for_durable_writes() -> None:
    from arb.recorder import Recorder
    from arb.supervise import Backoff
    from arb.types import RawMessage

    written: list[RawMessage] = []

    async def sink(batch) -> None:  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)  # simulate a slow write
        written.extend(batch)

    recorder = Recorder(
        sink, queue_max=10, batch_max=2, retry_backoff=Backoff(initial_s=0.001, jitter_frac=0)
    )
    for i in range(5):
        recorder.enqueue(
            RawMessage(
                venue="v",
                stream="s",
                payload=b"x",
                recv_ts_ns=i,
                recv_mono_ns=i,
                run_id="r",
                ingest_seq=i,
            )
        )
    writer = asyncio.create_task(recorder.run())
    assert await recorder.drain(timeout_s=5)
    assert len(written) == 5
    writer.cancel()
    import pytest

    with pytest.raises(asyncio.CancelledError):
        await writer


async def test_set_tickers_applies_on_the_next_connect() -> None:
    connections: list[FakeConnection] = []

    async def connector() -> WSConnection:
        conn = FakeConnection(['{"type":"subscribed"}'])
        connections.append(conn)
        return conn

    source = KalshiWSSource(
        config=config(),
        run=RunContext(run_id="testrun"),
        market_tickers=["AAA"],
        ws_config=WSConfig(backoff_initial_s=0.001, backoff_max_s=0.002, backoff_jitter_frac=0),
        connector=connector,
    )
    assert source.tickers == ["AAA"]

    received = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            received.append(message)
            if len(received) == 1:
                # Mid-stream universe change: the live socket keeps its old
                # subscription until it drops, then resubscribes to the new set.
                assert source.set_tickers(["BBB", "CCC"]) is True
            if len(received) >= 2:
                break

    subscribed = [json.loads(c.sent[0])["params"]["market_tickers"] for c in connections]
    assert subscribed == [["AAA"], ["BBB", "CCC"]]
    assert source.tickers == ["BBB", "CCC"]


async def test_set_tickers_rejects_empty_and_reports_no_op() -> None:
    import pytest

    async def connector() -> WSConnection:
        return FakeConnection([])

    source = KalshiWSSource(
        config=config(), run=RunContext(), market_tickers=["AAA"], connector=connector
    )
    with pytest.raises(ValueError):
        source.set_tickers([])
    assert source.tickers == ["AAA"]  # rejected outright, not applied then undone
    assert source.set_tickers(["AAA"]) is False  # no change → caller can skip the reconnect
    assert source.set_tickers(("AAA", "BBB")) is True
