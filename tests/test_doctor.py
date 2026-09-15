import pytest

from arb.clock import ClockQueryError, ClockSample
from arb.config import AppConfig
from arb.doctor import (
    CheckResult,
    check_clock,
    check_database,
    check_env,
    exit_code,
    format_results,
)


class TestFormatting:
    def test_format_and_exit_code(self) -> None:
        results = [
            CheckResult("alpha", "ok", "fine"),
            CheckResult("beta", "warn", "meh"),
        ]
        out = format_results(results)
        assert "[ok  ] alpha  fine" in out
        assert "[warn] beta   meh" in out
        assert exit_code(results) == 0
        assert exit_code([*results, CheckResult("gamma", "fail", "broken")]) == 1


class TestChecks:
    def test_env_check_warns_without_credentials(self) -> None:
        config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
        names = {r.name: r for r in check_env(config)}
        assert names["kalshi keys"].status == "warn"
        assert names["polymarket_us keys"].status == "warn"

    async def test_database_check_fails_cleanly_when_unreachable(self) -> None:
        config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
        config.database_url = "postgresql+asyncpg://nobody:nothing@127.0.0.1:59999/nope"
        results = await check_database(config)
        assert results[0].name == "database"
        assert results[0].status == "fail"

    async def test_database_check_passes_on_sqlite(self) -> None:
        config = AppConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
        config.database_url = "sqlite+aiosqlite://"
        results = await check_database(config)
        statuses = {r.name: r.status for r in results}
        assert statuses["database"] == "ok"
        assert statuses["migrations"] == "warn"  # empty db, no alembic_version


class TestClockCheck:
    async def test_warns_past_the_threshold_with_a_fix_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_query(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
            return ClockSample(skew_ms=-27.0, round_trip_ms=9.0)

        monkeypatch.setattr("arb.doctor.query_clock_offset", fake_query)
        result = await check_clock(AppConfig(_env_file=None))  # pyright: ignore[reportCallIssue]
        assert result.status == "warn"
        assert "-27.0 ms" in result.detail
        assert "local clock behind" in result.detail
        assert "fix:" in result.detail

    async def test_ok_inside_the_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_query(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
            return ClockSample(skew_ms=24.9, round_trip_ms=9.0)

        monkeypatch.setattr("arb.doctor.query_clock_offset", fake_query)
        result = await check_clock(AppConfig(_env_file=None))  # pyright: ignore[reportCallIssue]
        assert result.status == "ok"
        assert "+24.9 ms" in result.detail

    async def test_warns_on_a_lag_the_symmetric_threshold_would_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """-19.7 ms was the real reading while the UI showed a negative median."""

        async def fake_query(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
            return ClockSample(skew_ms=-19.7, round_trip_ms=50.3)

        monkeypatch.setattr("arb.doctor.query_clock_offset", fake_query)
        result = await check_clock(AppConfig(_env_file=None))  # pyright: ignore[reportCallIssue]
        assert result.status == "warn"
        assert "will read negative" in result.detail

    async def test_degrades_to_warn_when_the_query_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_query(server: str, samples: int = 4, timeout_s: float = 1.5) -> ClockSample:
            raise ClockQueryError("no usable reply from pool.ntp.org: TimeoutError: ")

        monkeypatch.setattr("arb.doctor.query_clock_offset", fake_query)
        result = await check_clock(AppConfig(_env_file=None))  # pyright: ignore[reportCallIssue]
        assert result.status == "warn"
        assert "UDP 123" in result.detail
