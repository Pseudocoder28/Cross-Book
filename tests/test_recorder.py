import asyncio
from collections.abc import Sequence

import pytest

from arb.recorder import Recorder
from arb.supervise import Backoff
from arb.types import RawMessage


def msg(i: int) -> RawMessage:
    return RawMessage(
        venue="testvenue",
        stream="ws",
        payload=f"payload-{i}".encode(),
        recv_ts_ns=1_000 + i,
        recv_mono_ns=2_000 + i,
        run_id="testrun",
        ingest_seq=i,
    )


class FakeSink:
    def __init__(self, *, fail_times: int = 0, expect: int = 0) -> None:
        self.batches: list[list[RawMessage]] = []
        self.fail_times = fail_times
        self.expect = expect
        self.done = asyncio.Event()

    async def __call__(self, batch: Sequence[RawMessage]) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("db down")
        self.batches.append(list(batch))
        if sum(len(b) for b in self.batches) >= self.expect:
            self.done.set()


def tight_backoff() -> Backoff:
    return Backoff(initial_s=0.001, max_s=0.002, jitter_frac=0)


async def run_until_done(recorder: Recorder, sink: FakeSink) -> None:
    writer = asyncio.create_task(recorder.run())
    try:
        await asyncio.wait_for(sink.done.wait(), timeout=5)
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer


async def test_writes_everything_in_order_respecting_batch_max() -> None:
    sink = FakeSink(expect=5)
    recorder = Recorder(sink, queue_max=10, batch_max=2, retry_backoff=tight_backoff())
    for i in range(5):
        assert recorder.enqueue(msg(i))
    await run_until_done(recorder, sink)

    flat = [m for batch in sink.batches for m in batch]
    assert [m.ingest_seq for m in flat] == [0, 1, 2, 3, 4]
    assert all(len(batch) <= 2 for batch in sink.batches)


async def test_enqueue_drops_on_overflow_without_blocking() -> None:
    sink = FakeSink()
    recorder = Recorder(sink, queue_max=2, batch_max=10)
    assert recorder.enqueue(msg(0))
    assert recorder.enqueue(msg(1))
    assert not recorder.enqueue(msg(2))


async def test_failed_write_is_retried_in_place() -> None:
    sink = FakeSink(fail_times=2, expect=1)
    recorder = Recorder(sink, queue_max=10, batch_max=10, retry_backoff=tight_backoff())
    assert recorder.enqueue(msg(0))
    await run_until_done(recorder, sink)

    assert len(sink.batches) == 1
    assert sink.batches[0][0].payload == b"payload-0"


def test_rejects_nonpositive_batch_max() -> None:
    with pytest.raises(ValueError):
        Recorder(FakeSink(), batch_max=0)
