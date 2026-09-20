"""SIGTERM takes the Ctrl+C path: cleanup runs to completion, exit status 0.

The real-process tests spawn ``tests/shutdown_target.py`` — the production
``arb.shutdown.serve_then_cleanup`` around a real uvicorn server — deliver a
real signal and read the sentinel its cleanup writes. Without the SIGTERM
handler the child dies of signal 15 with no sentinel, which is the bug: uvicorn
re-delivers SIGTERM after it shuts down and the default disposition kills the
process before any ``finally`` runs. ``tests/run_ui_target.py`` is the same
idea one level up: the real ``run_ui``, with a config that can reach nothing.

Synchronisation is by events, never by sleeping: the child prints a line when
it reaches a state and the test blocks on that line with a timeout.
"""

from __future__ import annotations

import asyncio
import http.client
import inspect
import logging
import queue
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import FrameType

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from prometheus_client import REGISTRY
from websockets.asyncio.client import connect as websockets_connect

from arb.recorder import Recorder
from arb.shutdown import Server, Shutdown, serve_then_cleanup
from arb.supervise import Backoff
from arb.types import RawMessage
from arb.ui import server as ui_server
from arb.ui.control import JOB_TERMINATE_GRACE_S
from tests.run_ui_target import closed_port_config

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signals: no SIGTERM delivery on Windows"
)

REPO = Path(__file__).resolve().parent.parent
TARGET = Path(__file__).with_name("shutdown_target.py")
RUN_UI_TARGET = Path(__file__).with_name("run_ui_target.py")
# Generous: a loaded CI box importing uvicorn and uvloop cold is slow, and a
# wait that passes returns at once anyway.
TIMEOUT_S = 60.0
CLEAN_SENTINEL = "cleanup-started\ncleanup-complete\n"

type Spawn = Callable[..., Child]


class Child:
    """A target script as a subprocess, with its stdout as an event feed."""

    def __init__(self, script: Path, *args: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(script), *args],
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

    def expect(self, marker: str, *, anywhere: bool = False) -> str:
        """Block until the child prints a line starting with ``marker`` (or,
        for a log line with a logger-name prefix, containing it)."""
        while True:
            try:
                line = self._lines.get(timeout=TIMEOUT_S)
            except queue.Empty:
                pytest.fail(f"no {marker!r} line within {TIMEOUT_S}s; output: {self.seen}")
            if line is None:
                pytest.fail(f"child exited before printing {marker!r}; output: {self.seen}")
            self.seen.append(line)
            if marker in line if anywhere else line.startswith(marker):
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
def spawn() -> Iterator[Spawn]:
    """Start target scripts; whatever is still running when the test ends —
    passed, failed or timed out — is killed. (A test runner that is itself
    SIGKILLed cannot do this, which is why the targets also set an alarm.)"""
    children: list[Child] = []

    def start(script: Path, *args: str) -> Child:
        child = Child(script, *args)
        children.append(child)
        return child

    try:
        yield start
    finally:
        for child in children:
            child.close()


@pytest.fixture
def sentinel(tmp_path: Path) -> Path:
    return tmp_path / "sentinel"


def get_ok(port: int) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT_S)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        return response.read()
    finally:
        conn.close()


async def bounded[T](awaitable: Awaitable[T]) -> T:
    """The in-process tests' time limit: the project has no pytest-timeout, and
    a seam that hangs must fail a test, not the whole run."""
    async with asyncio.timeout(TIMEOUT_S):
        return await awaitable


# ---------------------------------------------------------------------------
# real process, real uvicorn, real signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loop_name", ["uvloop", "asyncio"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_signal_while_serving_runs_cleanup_and_exits_clean(
    spawn: Spawn, sentinel: Path, loop_name: str, sig: signal.Signals
) -> None:
    """The bug, and Ctrl+C beside it: both must finish cleanup and exit 0."""
    child = spawn(TARGET, loop_name, "serve", "quick", str(sentinel))
    port = int(child.expect("SERVING").split()[1])
    assert get_ok(port) == b"ok"  # it really is serving, not just started

    child.proc.send_signal(sig)

    code = child.wait()
    assert code == 0, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen


def test_second_sigterm_during_cleanup_changes_nothing(spawn: Spawn, sentinel: Path) -> None:
    """The child's cleanup does not finish until it has *handled* the second
    SIGTERM, so a clean exit here means one arrived mid-cleanup and was
    survived — not that it arrived too late to matter."""
    child = spawn(TARGET, "uvloop", "serve", "second-sigterm", str(sentinel))
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
    spawn: Spawn, sentinel: Path, sig: signal.Signals, expected_code: int
) -> None:
    child = spawn(TARGET, "uvloop", "startup", "quick", str(sentinel))
    child.expect("STARTING")

    child.proc.send_signal(sig)

    code = child.wait()
    assert code == expected_code, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen
    assert not any(line.startswith("SERVING") for line in child.seen)


@pytest.mark.parametrize("loop_name", ["uvloop", "asyncio"])
def test_sigterm_inside_synchronous_startup_serves_nothing(
    spawn: Spawn, sentinel: Path, loop_name: str
) -> None:
    """Startup that is not awaiting anything when SIGTERM arrives: the
    handler's cancellation is still pending when ``main`` reaches ``serve()``.
    It must land there, before uvicorn is entered — not after a port is bound,
    and not inside cleanup."""
    child = spawn(TARGET, loop_name, "early-sigterm", "quick", str(sentinel))

    code = child.wait()
    assert code == 0, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen
    assert "SERVE-ENTERED" not in child.seen
    assert "CLEANUP-STARTED" in child.seen


@pytest.mark.parametrize("loop_name", ["uvloop", "asyncio"])
@pytest.mark.parametrize(("phase", "ready"), [("serve", "SERVING"), ("startup", "STARTING")])
def test_one_ctrl_c_during_a_cleanup_sigterm_started_does_not_abandon_it(
    spawn: Spawn, sentinel: Path, loop_name: str, phase: str, ready: str
) -> None:
    """``kill <pid>``, then Ctrl+C in the terminal. uvicorn replays SIGTERM
    only, so this Ctrl+C is asyncio.Runner's *first* and it cancels the main
    task — which used to land on whatever cleanup was awaiting and abandon the
    drain there. The child's cleanup does not finish until that cancellation
    has been sent, and then suspends again, so a complete sentinel means the
    cancellation arrived mid-cleanup and was held. 130: it is still delivered,
    once cleanup is done."""
    child = spawn(TARGET, loop_name, phase, "interrupt", str(sentinel))
    child.expect(ready)

    child.proc.send_signal(signal.SIGTERM)
    child.expect("CLEANUP-STARTED")
    child.proc.send_signal(signal.SIGINT)

    code = child.wait()
    assert code == 130, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == CLEAN_SENTINEL, child.seen


@pytest.mark.parametrize("loop_name", ["uvloop", "asyncio"])
def test_a_second_ctrl_c_still_abandons_cleanup(
    spawn: Spawn, sentinel: Path, loop_name: str
) -> None:
    """The way out has to keep working: the child's cleanup is a 20 s wait
    that only being abandoned can shorten."""
    child = spawn(TARGET, loop_name, "serve", "abandon", str(sentinel))
    child.expect("SERVING")

    child.proc.send_signal(signal.SIGINT)
    child.expect("CLEANUP-STARTED")
    child.proc.send_signal(signal.SIGINT)

    code = child.wait()
    assert code == 130, f"exit {code}; output: {child.seen}"
    assert sentinel.read_text() == "cleanup-started\n", child.seen


# ---------------------------------------------------------------------------
# real process, the real run_ui
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def throwaway_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An RSA key generated here and used nowhere else: ``run_ui`` will not
    build its Kalshi source without one to sign with."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("shutdown") / "throwaway.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_the_real_run_ui_cleans_up_and_exits_clean(
    spawn: Spawn, throwaway_key: Path, sig: signal.Signals
) -> None:
    """What an operator stops: ``run_ui`` itself under ``uvloop.run``, real
    uvicorn, a real signal. Taken off ``serve_then_cleanup`` it exits -15 on
    SIGTERM and never prints its last line."""
    child = spawn(RUN_UI_TARGET, str(throwaway_key))
    running = child.expect("Uvicorn running on", anywhere=True)
    match = re.search(r"http://127\.0\.0\.1:(\d+)", running)
    assert match, running
    get_ok(int(match.group(1)))  # the real app answers: it is serving

    child.proc.send_signal(sig)

    code = child.wait()
    assert code == 0, f"exit {code}; output: {child.seen}"
    last_lines = [line for line in child.seen if "shutdown complete in" in line]
    assert len(last_lines) == 1, child.seen
    assert last_lines[0].endswith(", drained)"), last_lines


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


async def nothing_to_clean() -> None:
    return None


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

    await bounded(serve_then_cleanup(main, cleanup))

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
        await bounded(serve_then_cleanup(main, cleanup))

    assert cleaned == ["yes"]
    assert signal.getsignal(signal.SIGTERM) is before


async def test_an_exception_from_cleanup_propagates_and_the_handler_is_restored() -> None:
    """Cleanup runs in a task of its own; what it raises must still come out
    of the call, not be left behind as an unretrieved task exception."""
    before = signal.getsignal(signal.SIGTERM)

    async def main(shutdown: Shutdown) -> None:
        return None

    async def cleanup() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("engine dispose failed")

    with pytest.raises(RuntimeError, match="engine dispose failed"):
        await bounded(serve_then_cleanup(main, cleanup))

    assert signal.getsignal(signal.SIGTERM) is before


async def test_a_sigterm_handler_someone_installed_later_is_left_alone() -> None:
    """Uninstalling undoes our own install and nothing else."""
    before = signal.getsignal(signal.SIGTERM)

    def theirs(signum: int, frame: FrameType | None) -> None:
        return None

    async def main(shutdown: Shutdown) -> None:
        signal.signal(signal.SIGTERM, theirs)

    try:
        await bounded(serve_then_cleanup(main, nothing_to_clean))
        assert signal.getsignal(signal.SIGTERM) is theirs
    finally:
        signal.signal(signal.SIGTERM, before)


async def test_sigterm_beside_uvicorns_capture_window_still_asks_the_server_to_exit() -> None:
    """While ``serve()`` is running but uvicorn does not hold the signal (the
    few bytecodes either side of its capture), the handler is the only thing
    that can tell the server to stop."""

    class SignalledWhileServing:
        should_exit = False
        should_exit_after_sigterm: bool | None = None

        async def serve(self) -> None:
            signal.raise_signal(signal.SIGTERM)
            self.should_exit_after_sigterm = self.should_exit

    server = SignalledWhileServing()
    served: Server = server
    cleaned: list[str] = []

    async def main(shutdown: Shutdown) -> None:
        assert shutdown.installed
        await shutdown.serve(served)
        assert shutdown.sigterms == 1

    async def cleanup() -> None:
        cleaned.append("yes")

    await bounded(serve_then_cleanup(main, cleanup))

    assert server.should_exit_after_sigterm is True
    assert cleaned == ["yes"]


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
            asyncio.run(bounded(serve_then_cleanup(main, cleanup)))
        except BaseException as exc:
            seen["error"] = exc

    # A daemon: if it ever did hang, it must not also hold the interpreter open.
    thread = threading.Thread(target=body, daemon=True)
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

    assert await bounded(asyncio.create_task(body())) == 0
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
    await bounded(started.wait())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bounded(task)
    assert cleaned == ["complete"]


async def test_a_cancellation_during_cleanup_is_held_until_cleanup_has_finished(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Held, not swallowed: cleanup runs to its end first, then whoever
    cancelled still sees the cancellation arrive. The operator is told once,
    in the words docs/ops.md quotes."""
    caplog.set_level(logging.INFO, logger="arb.shutdown")
    cleaned: list[str] = []
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def main(shutdown: Shutdown) -> None:
        return None

    async def cleanup() -> None:
        cleaning.set()
        await release.wait()
        await asyncio.sleep(0)  # one more await it could have landed on
        cleaned.append("complete")

    task = asyncio.create_task(serve_then_cleanup(main, cleanup))
    await bounded(cleaning.wait())
    for _ in range(2):  # twice: the second must be held as well
        task.cancel()
        for _ in range(5):  # room for the cancellation to be delivered
            await asyncio.sleep(0)
        assert not task.done()
        assert cleaned == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await bounded(task)
    assert cleaned == ["complete"]
    told = [r.getMessage() for r in caplog.records if "during cleanup" in r.getMessage()]
    assert told == ["interrupted during cleanup: finishing it first (Ctrl+C again abandons)"]


async def test_cancelling_the_cleanup_task_itself_still_stops_it() -> None:
    """What ``asyncio.run`` does after a second Ctrl+C: it cancels *every*
    task. Holding the caller's cancellation must not make cleanup immortal."""
    cleaned: list[str] = []
    cleaning = asyncio.Event()

    async def main(shutdown: Shutdown) -> None:
        return None

    async def cleanup() -> None:
        cleaning.set()
        await asyncio.Event().wait()
        cleaned.append("complete")

    before = asyncio.all_tasks()
    task = asyncio.create_task(serve_then_cleanup(main, cleanup))
    await bounded(cleaning.wait())
    ours = asyncio.all_tasks() - before
    assert len(ours) == 2  # the caller, and the task cleanup runs in
    for each in ours:
        each.cancel()

    with pytest.raises(asyncio.CancelledError):
        await bounded(task)
    assert cleaned == []


# ---------------------------------------------------------------------------
# run_ui uses it; the drain timeout is counted; compose leaves room for it all
# ---------------------------------------------------------------------------


class ServeRecorded(Shutdown):
    """Stands in for the guard ``serve_then_cleanup`` hands to ``main``: it
    records the server ``run_ui`` built and returns without binding a port."""

    def __init__(self) -> None:
        super().__init__()
        self.servers: list[Server] = []

    async def serve(self, server: Server) -> None:
        self.servers.append(server)


class Wiring:
    """``run_ui`` with ``serve_then_cleanup`` and ``drain_recorder`` replaced
    by recorders — a miniature of the seam, so ``run_ui``'s own startup and
    cleanup closures are what run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.guard = ServeRecorded()
        self.events: list[str] = []
        self.drains: list[tuple[object, float]] = []
        monkeypatch.setattr(ui_server, "serve_then_cleanup", self._serve_then_cleanup)
        monkeypatch.setattr(ui_server, "drain_recorder", self._drain_recorder)

    async def _serve_then_cleanup(
        self, main: Callable[[Shutdown], Awaitable[None]], cleanup: Callable[[], Awaitable[None]]
    ) -> None:
        self.calls += 1
        try:
            await main(self.guard)
            self.events.append("main returned")
        finally:
            await cleanup()
            self.events.append("cleanup returned")

    async def _drain_recorder(self, recorder: Recorder, timeout_s: float) -> bool:
        self.events.append("drain")
        self.drains.append((recorder, timeout_s))
        return True

    async def run_ui(self, key_path: Path, *, key_id: str | None) -> None:
        # Every address is a closed local port and the key is a throwaway: a
        # run_ui that stopped going through the seam would start for real, and
        # even then it could not reach a venue. The timeout is what it fails on.
        async with asyncio.timeout(15):
            await ui_server.run_ui(
                closed_port_config(key_path, key_id=key_id),
                tickers=["T"],
                top_n=0,
                record=False,
                host="127.0.0.1",
                port=0,
                poly_top=0,
                pairs_top=0,
            )


async def test_run_ui_hands_its_startup_and_a_cleanup_that_drains_to_the_seam(
    monkeypatch: pytest.MonkeyPatch, throwaway_key: Path
) -> None:
    """The subprocess tests prove the seam and the whole; this pins the parts
    of ``run_ui`` they cannot see from outside. A cleanup that skips the drain
    still prints "drained", and a missing HTTP bound only shows when a request
    hangs — and the stop-budget arithmetic below depends on both."""
    wiring = Wiring(monkeypatch)

    await wiring.run_ui(throwaway_key, key_id="not-a-real-key-id")

    assert wiring.calls == 1
    assert wiring.events == ["main returned", "drain", "cleanup returned"]
    [(recorder, timeout_s)] = wiring.drains
    assert isinstance(recorder, Recorder)
    assert timeout_s == ui_server.DRAIN_TIMEOUT_S
    [server] = wiring.guard.servers
    assert isinstance(server, uvicorn.Server)
    assert server.config.timeout_graceful_shutdown == ui_server.HTTP_GRACEFUL_TIMEOUT_S


async def test_run_ui_still_drains_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch, throwaway_key: Path
) -> None:
    """No key id: startup raises before anything is served. By then discovery
    may already have queued recorded REST responses, so cleanup still runs."""
    wiring = Wiring(monkeypatch)

    with pytest.raises(ValueError, match="kalshi_api_key_id is not configured"):
        await wiring.run_ui(throwaway_key, key_id=None)

    assert wiring.calls == 1
    assert wiring.events == ["drain", "cleanup returned"]
    assert wiring.guard.servers == []


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
        assert await bounded(ui_server.drain_recorder(recorder, 0.05)) is False
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


def test_compose_runs_the_app_under_an_init() -> None:
    """docs/ops.md promises it: a stop must not depend on how PID 1 treats a
    signal it has no handler for, and a cancelled replay job's orphans need
    something to reap them."""
    compose = (REPO / "docker-compose.yml").read_text()
    app = compose.split("\n  app:\n", 1)[1]
    assert re.search(r"^    init:\s*true\s*$", app, re.MULTILINE), "app service: init: true"
