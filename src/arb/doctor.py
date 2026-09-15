"""``arb doctor``: environment and connectivity checks.

Checks env/config, key-file presence (never contents), venue reachability,
the local clock (an SNTP query for millisecond skew, plus the venues' whole-
second Date header), database connectivity and migration state, and free disk.
Each check reports ok/warn/fail; the command exits non-zero only on failures.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

import httpx
from sqlalchemy import text

from arb.clock import query_clock_offset
from arb.config import AppConfig
from arb.storage.db import make_engine

Status = Literal["ok", "warn", "fail"]

# Venue clocks matter: Kalshi/Polymarket US signatures embed a timestamp
# (Polymarket US rejects anything ±30 s from server time).
CLOCK_SKEW_WARN_S = 3.0
# Deliberately the same 25 ms as SKEW_WARN_MS in src/arb/ui/static/app.js:20,
# so doctor warns at exactly the point the UI flips to its CLOCK SKEW banner.
CLOCK_OFFSET_WARN_MS = 25.0
# The UI's other banner trigger is a negative one-way median, which happens as
# soon as the local clock lags by more than the real push delay — measured at
# 5.5 to 12.5 ms for Kalshi's WS (docs/venue-notes.md). So a lag past this warns
# too, well before the symmetric 25 ms: otherwise doctor would report "ok" for
# a clock that is already making every latency reading negative.
CLOCK_LAG_WARN_MS = 5.5
DISK_FREE_WARN_GB = 5.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def check_env(config: AppConfig) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(
        CheckResult("env", "ok", ".env found")
        if Path(".env").exists()
        else CheckResult("env", "warn", "no .env file — using defaults (see .env.example)")
    )
    for name, key_id, key_path in (
        ("kalshi keys", config.kalshi_api_key_id, config.kalshi_private_key_path),
        (
            "polymarket_us keys",
            config.polymarket_us_access_key,
            config.polymarket_us_secret_key_path,
        ),
    ):
        if key_id and key_path.exists():
            results.append(CheckResult(name, "ok", f"key id set, key file at {key_path}"))
        elif key_id or key_path.exists():
            results.append(CheckResult(name, "warn", "incomplete: need both key id and key file"))
        else:
            results.append(CheckResult(name, "warn", "not provisioned (WS market data needs them)"))
    return results


def check_disk() -> CheckResult:
    usage = shutil.disk_usage(".")
    free_gb = usage.free / 1e9
    status: Status = "ok" if free_gb >= DISK_FREE_WARN_GB else "warn"
    return CheckResult("disk", status, f"{free_gb:.1f} GB free")


async def check_venue(client: httpx.AsyncClient, name: str, url: str) -> list[CheckResult]:
    try:
        t0 = time.time()
        response = await client.get(url)
        t1 = time.time()
    except Exception as exc:
        return [CheckResult(name, "fail", f"unreachable: {type(exc).__name__}: {exc}")]
    results: list[CheckResult] = []
    if response.status_code == 200:
        results.append(CheckResult(name, "ok", f"HTTP 200 in {(t1 - t0) * 1e3:.0f} ms"))
    else:
        results.append(CheckResult(name, "fail", f"HTTP {response.status_code}"))
    date_header = response.headers.get("date")
    if date_header:
        try:
            server_ts = parsedate_to_datetime(date_header).timestamp()
        except ValueError:
            results.append(
                CheckResult(f"{name} clock", "warn", f"unparseable Date: {date_header!r}")
            )
        else:
            # Date has 1 s resolution; compare against the request midpoint.
            skew = (t0 + t1) / 2 - server_ts
            status: Status = "ok" if abs(skew) <= CLOCK_SKEW_WARN_S else "warn"
            results.append(
                CheckResult(
                    f"{name} clock",
                    status,
                    f"skew {skew:+.1f} s (Date header, 1 s resolution: catches only "
                    "gross skew that would break request signing — see the ntp clock "
                    "check for milliseconds)",
                )
            )
    return results


def _clock_fix_hint(server: str) -> str:
    if sys.platform == "darwin":
        return f"sudo sntp -sS {server}"
    return "enable chrony or systemd-timesyncd (sudo timedatectl set-ntp true)"


async def check_clock(config: AppConfig) -> CheckResult:
    """Millisecond-resolution clock check against ``config.ntp_server``.

    Never fails: a skewed clock does not break read-only market data, it only
    biases the one-way latency the UI reports. UDP 123 is blocked often enough
    (containers, locked-down networks) that not being able to ask is a warn too.
    """
    try:
        sample = await query_clock_offset(config.ntp_server)
    except Exception as exc:
        return CheckResult(
            "ntp clock",
            "warn",
            f"could not query {config.ntp_server} (UDP 123 may be blocked): "
            f"{type(exc).__name__}: {exc}",
        )
    detail = (
        f"offset {sample.skew_ms:+.1f} ms vs {config.ntp_server} "
        f"(rtt {sample.round_trip_ms:.1f} ms)"
    )
    lagging = sample.skew_ms < -CLOCK_LAG_WARN_MS
    if not lagging and abs(sample.skew_ms) <= CLOCK_OFFSET_WARN_MS:
        return CheckResult("ntp clock", "ok", detail)
    direction = (
        "local clock behind — one-way latency will read negative"
        if sample.skew_ms < 0
        else "local clock ahead"
    )
    return CheckResult(
        "ntp clock", "warn", f"{detail} — {direction}; fix: {_clock_fix_hint(config.ntp_server)}"
    )


async def check_database(config: AppConfig) -> list[CheckResult]:
    engine = make_engine(config.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            try:
                version = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
            except Exception:
                return [
                    CheckResult("database", "ok", "connected"),
                    CheckResult(
                        "migrations", "warn", "alembic_version missing — run alembic upgrade head"
                    ),
                ]
            return [
                CheckResult("database", "ok", "connected"),
                CheckResult("migrations", "ok", f"at revision {version}"),
            ]
    except Exception as exc:
        return [CheckResult("database", "fail", f"{type(exc).__name__}: {exc}")]
    finally:
        await engine.dispose()


async def run_doctor(config: AppConfig | None = None) -> list[CheckResult]:
    config = config if config is not None else AppConfig()
    results: list[CheckResult] = []
    results.extend(check_env(config))
    async with httpx.AsyncClient(timeout=10.0) as client:
        results.extend(
            await check_venue(client, "kalshi", f"{config.kalshi_api_base}/markets?limit=1")
        )
        results.extend(
            await check_venue(
                client,
                "polymarket_us",
                f"{config.polymarket_us_gateway_base}/v1/markets?limit=1",
            )
        )
    results.append(await check_clock(config))
    results.extend(await check_database(config))
    results.append(check_disk())
    return results


def format_results(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results)
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    return "\n".join(f"[{marks[r.status]}] {r.name:<{width}}  {r.detail}" for r in results)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0
