from arb.config import AppConfig
from arb.doctor import CheckResult, check_database, check_env, exit_code, format_results


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
