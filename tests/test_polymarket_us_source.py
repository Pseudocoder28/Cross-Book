"""REST poller: pacing, round-robin, 429 pause, error backoff — with a fake
transport and a recorded fake sleep (no network, no real waiting)."""

from contextlib import aclosing

import pytest

from arb.config import AppConfig
from arb.run import RunContext
from arb.venues.polymarket_us.source import (
    IDLE_SLEEP_S,
    RATE_LIMIT_PAUSE_S,
    PolymarketUSRestSource,
)


def config() -> AppConfig:
    return AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]


class Harness:
    def __init__(self, script: list[tuple[int, bytes] | Exception]) -> None:
        self.script = list(script)
        self.urls: list[str] = []
        self.sleeps: list[float] = []

    async def fetch(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def collect(source: PolymarketUSRestSource, n: int) -> list[bytes]:
    out: list[bytes] = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            out.append(message.payload)
            if len(out) >= n:
                break
    return out


async def test_round_robin_paced_polling() -> None:
    h = Harness([(200, b"a"), (200, b"b"), (200, b"c")])
    source = PolymarketUSRestSource(
        config=config(),
        run=RunContext(run_id="r"),
        slugs=["one", "two"],
        rate_per_s=4.0,
        fetch=h.fetch,
        sleep=h.sleep,
    )
    payloads = await collect(source, 3)
    assert payloads == [b"a", b"b", b"c"]
    assert [u.rsplit("/", 2)[1] for u in h.urls] == ["one", "two", "one"]
    # Each poll is followed by a pacing sleep of at most one interval (0.25s).
    assert len(h.sleeps) >= 2 and all(0 < s <= 0.25 for s in h.sleeps)
    assert source.polls == 3 and source.rate_limited == 0
    assert source.venue == "polymarket_us"


async def test_429_pauses_polling_and_counts() -> None:
    h = Harness([(429, b"{}"), (200, b"ok")])
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["x"], rate_per_s=4.0, fetch=h.fetch, sleep=h.sleep
    )
    payloads = await collect(source, 1)
    assert payloads == [b"ok"]
    assert source.rate_limited == 1
    assert h.sleeps[0] == RATE_LIMIT_PAUSE_S  # the documented-by-measurement cooldown


async def test_transport_error_backs_off_and_continues() -> None:
    h = Harness([OSError("boom"), (200, b"ok")])
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["x"], rate_per_s=4.0, fetch=h.fetch, sleep=h.sleep
    )
    assert await collect(source, 1) == [b"ok"]
    assert source.errors == 1
    assert h.sleeps[0] >= 0.5  # backoff, not a pacing sleep


def test_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        PolymarketUSRestSource(config=config(), run=RunContext(), slugs=[])
    with pytest.raises(ValueError):
        PolymarketUSRestSource(config=config(), run=RunContext(), slugs=["x"], rate_per_s=0)


async def test_set_targets_takes_effect_without_a_reconnect() -> None:
    h = Harness([(200, b"a"), (200, b"b"), (200, b"c"), (200, b"d")])
    source = PolymarketUSRestSource(
        config=config(),
        run=RunContext(run_id="r"),
        slugs=["one", "two"],
        rate_per_s=4.0,
        fetch=h.fetch,
        sleep=h.sleep,
    )
    seen = 0
    async with aclosing(source.stream()) as messages:
        async for _message in messages:
            seen += 1
            if seen == 1:
                assert source.set_targets(["three"]) is True
            if seen >= 3:
                break
    # The very next poll uses the new set: no reconnect, no snapshot burst,
    # and "two" is never polled again.
    assert [u.rsplit("/", 2)[1] for u in h.urls[:3]] == ["one", "three", "three"]
    assert source.targets == ["three"]


async def test_empty_targets_idle_instead_of_dividing_by_zero() -> None:
    # The old loop did slugs[index % len(slugs)]: an empty set was a
    # ZeroDivisionError inside a supervised task, i.e. a crash-restart loop.
    h = Harness([(200, b"a"), (200, b"b")])
    box: list[PolymarketUSRestSource] = []

    async def sleep(seconds: float) -> None:
        h.sleeps.append(seconds)
        if box[0].idle_waits == 3:  # idled politely; now hand it work again
            box[0].set_targets(["two"])

    source = PolymarketUSRestSource(
        config=config(),
        run=RunContext(run_id="r"),
        slugs=["one"],
        rate_per_s=4.0,
        fetch=h.fetch,
        sleep=sleep,
    )
    box.append(source)
    payloads: list[bytes] = []
    async with aclosing(source.stream()) as messages:
        async for message in messages:
            payloads.append(message.payload)
            if len(payloads) == 1:
                assert source.set_targets([]) is True
                assert source.targets == [] and source.cycle_s == 0.0
            if len(payloads) >= 2:
                break
    assert payloads == [b"a", b"b"]
    assert source.idle_waits >= 3
    assert h.sleeps.count(IDLE_SLEEP_S) >= 3  # napping, not spinning
    assert [u.rsplit("/", 2)[1] for u in h.urls] == ["one", "two"]
    assert source.polls == 2  # nothing polled while idle


def test_set_targets_dedupes_and_reports_no_op() -> None:
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["a", "a", "b"], rate_per_s=4.0
    )
    assert source.targets == ["a", "b"]  # the rate budget is ~0.5 req/s; no slug twice
    assert source.set_targets(["a", "b", "b"]) is False
    assert source.set_targets(["b", "a"]) is True  # order is the poll order


def test_poll_cycle_is_exposed_for_staleness_retuning() -> None:
    source = PolymarketUSRestSource(
        config=config(), run=RunContext(), slugs=["a", "b"], rate_per_s=4.0
    )
    assert source.interval_s == 0.25
    assert source.cycle_s == 0.5  # two targets at 4/s
    source.set_targets(["a", "b", "c", "d"])
    assert source.cycle_s == 1.0  # a book can now only be this fresh
    source.set_rate(2.0)
    assert source.rate_per_s == 2.0 and source.cycle_s == 2.0
    with pytest.raises(ValueError):
        source.set_rate(0)
