import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arb.supervise import Backoff, supervise


class TestBackoff:
    def test_grows_exponentially_and_caps_without_jitter(self) -> None:
        backoff = Backoff(initial_s=1, max_s=8, factor=2, jitter_frac=0)
        assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 8, 8]

    def test_reset_starts_over(self) -> None:
        backoff = Backoff(initial_s=1, max_s=8, jitter_frac=0)
        backoff.next_delay()
        backoff.next_delay()
        backoff.reset()
        assert backoff.next_delay() == 1

    def test_jitter_scales_delay_within_band(self) -> None:
        low = Backoff(initial_s=1.0, max_s=8, jitter_frac=0.5, rng=lambda: 1.0)
        high = Backoff(initial_s=1.0, max_s=8, jitter_frac=0.5, rng=lambda: 0.0)
        assert low.next_delay() == pytest.approx(0.5)
        assert high.next_delay() == pytest.approx(1.0)

    def test_rejects_bad_params(self) -> None:
        with pytest.raises(ValueError):
            Backoff(initial_s=0)
        with pytest.raises(ValueError):
            Backoff(initial_s=1.0, max_s=0.1)
        with pytest.raises(ValueError):
            Backoff(factor=0.5)
        with pytest.raises(ValueError):
            Backoff(jitter_frac=1.5)

    @given(st.floats(min_value=0, max_value=1), st.integers(min_value=1, max_value=25))
    def test_delays_stay_positive_and_bounded(self, rand: float, n: int) -> None:
        backoff = Backoff(initial_s=0.5, max_s=30, jitter_frac=0.5, rng=lambda: rand)
        delays = [backoff.next_delay() for _ in range(n)]
        assert all(0 < d <= 30 for d in delays)


def tight_backoff() -> Backoff:
    return Backoff(initial_s=0.001, max_s=0.002, jitter_frac=0)


class TestSupervise:
    async def test_restarts_a_crashing_task_until_cancelled(self) -> None:
        runs = 0
        third_run = asyncio.Event()

        async def task() -> None:
            nonlocal runs
            runs += 1
            if runs >= 3:
                third_run.set()
            raise RuntimeError("boom")

        sup = asyncio.create_task(supervise(task, name="crashy", backoff=tight_backoff()))
        await asyncio.wait_for(third_run.wait(), timeout=5)
        sup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sup
        assert runs >= 3

    async def test_clean_exit_also_restarts(self) -> None:
        runs = 0
        second_run = asyncio.Event()

        async def task() -> None:
            nonlocal runs
            runs += 1
            if runs >= 2:
                second_run.set()

        sup = asyncio.create_task(supervise(task, name="quitter", backoff=tight_backoff()))
        await asyncio.wait_for(second_run.wait(), timeout=5)
        sup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sup

    async def test_cancellation_inside_the_task_propagates(self) -> None:
        started = asyncio.Event()

        async def task() -> None:
            started.set()
            await asyncio.sleep(3600)

        sup = asyncio.create_task(supervise(task, name="sleeper", backoff=tight_backoff()))
        await started.wait()
        sup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sup
