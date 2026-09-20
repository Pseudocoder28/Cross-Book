"""SIGTERM takes the Ctrl+C path: cleanup runs to completion, exit status 0.

The real-process tests spawn ``tests/shutdown_target.py`` — the production
``arb.shutdown.serve_then_cleanup`` around a real uvicorn server — deliver a
real signal and read the sentinel its cleanup writes. Without the SIGTERM
handler the child dies of signal 15 with no sentinel, which is the bug: uvicorn
re-delivers SIGTERM after it shuts down and the default disposition kills the
process before any ``finally`` runs.

Synchronisation is by events, never by sleeping: the child prints a line when
it reaches a state and the test blocks on that line with a timeout.
"""

from __future__ import annotations

import asyncio
import http.client
import inspect
import queue
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from prometheus_client import REGISTRY
from websockets.asyncio.client import connect as websockets_connect

from arb.config import AppConfig
from arb.recorder import Recorder
from arb.shutdown import Shutdown, serve_then_cleanup
from arb.supervise import Backoff
from arb.types import RawMessage
from arb.ui import server as ui_server
from arb.ui.control import JOB_TERMINATE_GRACE_S

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signals: no SIGTERM delivery on Windows"
)

REPO = Path(__file__).resolve().parent.parent
TARGET = Path(__file__).with_name("shutdown_target.py")
# Generous: a loaded CI box importing uvicorn and uvloop cold is slow, and a
# wait that passes returns at once anyway.
TIMEOUT_S = 60.0
CLEAN_SENTINEL = "cleanup-started\ncleanup-complete\n"


class Child:
    """``shutdown_target.py`` as a subprocess, with its stdout as an event feed."""

    def __init__(self, loop_name: str, mode: str, sentinel: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(TARGET), loop_name, mode, str(sentinel)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.seen: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def expect(self, prefix: str) -> str:
        """Block until the child prints a line starting with ``prefix``."""
        while True:
            try:
                line = self._lines.get(timeout=TIMEOUT_S)
            except queue.Empty:
                pytest.fail(f"no {prefix!r} line within {TIMEOUT_S}s; output: {self.seen}")
            if line is None:
                pytest.fail(f"child exited before printing {prefix!r}; output: {self.seen}")
            self.seen.append(line)
            if line.startswith(prefix):
                return line

    def wait(self) -> int:
        code = self.proc.wait(timeout=TIMEOUT_S)
        self._pump.join(timeout=TIMEOUT_S)
        while True:  # whatever it printed on the way out, for failure messages
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                self.seen.append(line)
        return code

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=TIMEOUT_S)
        if self.proc.stdout is not None:
            self.proc.stdout.close()


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[tuple[type[Child], Path]]:
    children: list[Child] = []
    sentinel = tmp_path / "sentinel"

    class Tracked(Child):
        def __init__(self, loop_name: str, mode: str, sentinel: Path) -> None:
            super().__init__(loop_name, mode, sentinel)
            children.append(self)

    try:
        yield Tracked, sentinel
    finally:
        for child in children:
            child.close()


def get_ok(port: int) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT_S)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        return response.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# real process, real uvicorn, real signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loop_name", ["uvloop", "asyncio"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_signal_while_serving_runs_cleanup_and_exits_clean(
    spawn: tuple[type[Child], Path], loop_name: str, sig: signal.Signals
) -> None:
    """The bug, and Ctrl+C beside it: both must finish cleanup and exit 0."""
    child_type, sentinel = spawn
    child = child_type(loop_name, "serve", sentinel)
    port = int(child.expect("SERVING").split()[1])
    assert get_ok(port) == b"ok"  # it really is serving, not just started

    child.proc.send_signal(sig)

    code = child.wait()
    assert code == 0, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen


def test_second_sigterm_during_cleanup_changes_nothing(spawn: tuple[type[Child], Path]) -> None:
    """The child's cleanup does not finish until it has *handled* the second
    SIGTERM, so a clean exit here means one arrived mid-cleanup and was
    survived — not that it arrived too late to matter."""
    child_type, sentinel = spawn
    child = child_type("uvloop", "second-sigterm", sentinel)
    child.expect("SERVING")

    child.proc.send_signal(signal.SIGTERM)
    child.expect("CLEANUP-STARTED")
    child.proc.send_signal(signal.SIGTERM)

    code = child.wait()
    assert code == 0, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen


@pytest.mark.parametrize(
    ("sig", "expected_code"),
    [
        # SIGTERM cancels startup the way asyncio.Runner cancels it for Ctrl+C,
        # then claims its own cancellation: a requested stop is not an error.
        (signal.SIGTERM, 0),
        # Unchanged: asyncio turns the cancellation into KeyboardInterrupt and
        # the CLI maps that to 130.
        (signal.SIGINT, 130),
    ],
    ids=["SIGTERM", "SIGINT"],
)
def test_signal_during_startup_still_runs_cleanup(
    spawn: tuple[type[Child], Path], sig: signal.Signals, expected_code: int
) -> None:
    child_type, sentinel = spawn
    child = child_type("uvloop", "startup", sentinel)
    child.expect("STARTING")

    child.proc.send_signal(sig)

    code = child.wait()
    assert code == expected_code, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen
    assert not any(line.startswith("SERVING") for line in child.seen)


# ---------------------------------------------------------------------------
# in process: the handler must not leak, and must not need the main thread
# ---------------------------------------------------------------------------


def quick_server() -> uvicorn.Server:
    """A real uvicorn server that starts, then shuts down by itself."""

    async def app(scope: object, receive: object, send: object) -> None:
        return None

    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off"
        )
    )
    server.should_exit = True
    return server


async def test_sigterm_handler_is_installed_for_the_call_and_restored_after() -> None:
    before = signal.getsignal(signal.SIGTERM)
    during: list[object] = []
    cleaned: list[str] = []

    async def main(shutdown: Shutdown) -> None:
        assert shutdown.installed
        during.append(signal.getsignal(signal.SIGTERM))
        await shutdown.serve(quick_server())
        # uvicorn took the signal for serve() and put OUR handler back.
        during.append(signal.getsignal(signal.SIGTERM))

    async def cleanup() -> None:
        cleaned.append("yes")

    await serve_then_cleanup(main, cleanup)

    assert cleaned == ["yes"]
    assert during[0] is during[1]
    assert during[0] is not before
    assert signal.getsignal(signal.SIGTERM) is before


async def test_sigterm_handler_is_restored_when_startup_raises() -> None:
    before = signal.getsignal(signal.SIGTERM)
    cleaned: list[str] = []

    async def main(shutdown: Shutdown) -> None:
        raise RuntimeError("discovery failed")

    async def cleanup() -> None:
        cleaned.append("yes")

    with pytest.raises(RuntimeError, match="discovery failed"):
        await serve_then_cleanup(main, cleanup)

    assert cleaned == ["yes"]
    assert signal.getsignal(signal.SIGTERM) is before


def test_off_the_main_thread_nothing_is_installed_and_nothing_breaks() -> None:
    """``signal.signal`` raises off the main thread; an in-process caller
    there gets no SIGTERM handling and no crash."""
    before = signal.getsignal(signal.SIGTERM)
    seen: dict[str, object] = {}

    async def main(shutdown: Shutdown) -> None:
        seen["installed"] = shutdown.installed
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        await shutdown.serve(quick_server())

    async def cleanup() -> None:
        seen["cleaned"] = True

    def body() -> None:
        try:
            asyncio.run(serve_then_cleanup(main, cleanup))
        except BaseException as exc:
            seen["error"] = exc

    thread = threading.Thread(target=body)
    thread.start()
    thread.join(timeout=TIMEOUT_S)

    assert not thread.is_alive()
    assert "error" not in seen, seen
    assert seen == {"installed": False, "handler": before, "cleaned": True}
    assert signal.getsignal(signal.SIGTERM) is before


async def test_a_cancellation_pending_when_main_returns_does_not_abort_cleanup() -> None:
    """Ctrl+C without the signal: asyncio.Runner cancels the main task while it
    is *running* (uvicorn replays SIGINT synchronously), so the cancellation is
    only pending when ``main`` returns. It used to land on cleanup's first
    await and survive only because that await sat in ``suppress``."""
    cleaned: list[str] = []

    async def main(shutdown: Shutdown) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    async def cleanup() -> None:
        await asyncio.sleep(0)
        cleaned.append("complete")

    async def body() -> int:
        await serve_then_cleanup(main, cleanup)
        task = asyncio.current_task()
        assert task is not None
        return task.cancelling()

    assert await asyncio.create_task(body()) == 0
    assert cleaned == ["complete"]


async def test_someone_elses_cancellation_is_not_swallowed() -> None:
    """Only the cancellation this handler sent during startup is claimed."""
    cleaned: list[str] = []
    started = asyncio.Event()

    async def main(shutdown: Shutdown) -> None:
        started.set()
        await asyncio.Event().wait()

    async def cleanup() -> None:
        await asyncio.sleep(0)
        cleaned.append("complete")

    task = asyncio.create_task(serve_then_cleanup(main, cleanup))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == ["complete"]


# ---------------------------------------------------------------------------
# run_ui uses it; the drain timeout is counted; compose leaves room for it all
# ---------------------------------------------------------------------------


async def test_run_ui_hands_startup_and_cleanup_to_the_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The subprocess tests above prove ``serve_then_cleanup``; this proves
    ``run_ui`` is built on it. The fake runs cleanup without any startup, which
    is also the state a failed startup leaves behind."""
    calls: list[object] = []

    async def fake(
        main: Callable[[Shutdown], Awaitable[None]], cleanup: Callable[[], Awaitable[None]]
    ) -> None:
        calls.append(main)
        await cleanup()

    monkeypatch.setattr(ui_server, "serve_then_cleanup", fake)
    # If run_ui ever stops going through the seam it will start for real, so
    # every address here is a closed local port and there is no key to sign
    # with: a regression fails on the timeout below, it never reaches a venue.
    nowhere = "127.0.0.1:9"
    config = AppConfig(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url="sqlite+aiosqlite:///:memory:",
        kalshi_api_base=f"http://{nowhere}",
        kalshi_ws_url=f"ws://{nowhere}",
        kalshi_api_key_id=None,
        kalshi_private_key_path=tmp_path / "no-such-key.pem",
        polymarket_us_gateway_base=f"http://{nowhere}",
        polymarket_us_api_base=f"http://{nowhere}",
        polymarket_us_ws_url=f"ws://{nowhere}",
    )

    async with asyncio.timeout(15):
        await ui_server.run_ui(
            config, tickers=["T"], top_n=0, record=False, host="127.0.0.1", port=0, poly_top=0
        )

    assert len(calls) == 1


def drain_timeouts() -> float:
    return REGISTRY.get_sample_value("arb_recorder_drain_timeouts_total") or 0.0


def message(seq: int) -> RawMessage:
    return RawMessage(
        venue="testvenue",
        stream="ws",
        payload=b"{}",
        recv_ts_ns=seq,
        recv_mono_ns=seq,
        run_id="testrun",
        ingest_seq=seq,
    )


async def test_a_drain_that_times_out_is_counted() -> None:
    async def database_down(batch: object) -> None:
        raise ConnectionRefusedError("no database")

    recorder = Recorder(
        database_down, retry_backoff=Backoff(initial_s=0.001, max_s=0.002, jitter_frac=0)
    )
    writer = asyncio.create_task(recorder.run())
    try:
        assert recorder.enqueue(message(1))
        before = drain_timeouts()
        assert await ui_server.drain_recorder(recorder, 0.05) is False
        assert drain_timeouts() == before + 1
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer


async def test_a_drain_that_finishes_returns_at_once_and_counts_nothing() -> None:
    """The investigation behind an 11 s Ctrl+C: with a sink that works, the
    drain is over as soon as the queue is written — it does not sit out its
    timeout once the feeds are cancelled."""
    written: list[int] = []

    async def sink(batch: object) -> None:
        assert isinstance(batch, list)
        written.extend(m.ingest_seq for m in batch)

    recorder = Recorder(sink, batch_max=2)
    writer = asyncio.create_task(recorder.run())
    try:
        for seq in range(5):
            assert recorder.enqueue(message(seq))
        before = drain_timeouts()
        async with asyncio.timeout(5):  # far below the 10 s it would sit out
            assert await ui_server.drain_recorder(recorder, ui_server.DRAIN_TIMEOUT_S) is True
        assert written == [0, 1, 2, 3, 4]
        assert drain_timeouts() == before
    finally:
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer


def test_compose_stop_grace_period_covers_the_whole_shutdown() -> None:
    """Docker's default is 10 s — exactly DRAIN_TIMEOUT_S, so SIGKILL used to
    be scheduled for the moment a slow drain would have finished. Raising any
    of these constants without raising compose's number fails here."""
    compose = (REPO / "docker-compose.yml").read_text()
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*$", compose, re.MULTILINE)
    assert match, "docker-compose.yml: the app service needs a stop_grace_period in seconds"

    # src/arb/ws.py connects with the library's default close_timeout.
    websockets_close_timeout_s = (
        inspect.signature(websockets_connect).parameters["close_timeout"].default
    )
    assert isinstance(websockets_close_timeout_s, int | float)
    worst_case_s = (
        ui_server.HTTP_GRACEFUL_TIMEOUT_S
        + JOB_TERMINATE_GRACE_S
        + websockets_close_timeout_s
        + ui_server.DRAIN_TIMEOUT_S
    )
    assert int(match.group(1)) >= 1.5 * worst_case_s
