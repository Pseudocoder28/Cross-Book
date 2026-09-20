"""The child process ``tests/test_shutdown.py`` signals. Not a test module.

It is ``arb ui`` with everything but the shutdown path removed: the same
``arb.shutdown.serve_then_cleanup`` that ``run_ui`` calls, a real uvicorn
server on an ephemeral port, a trivial ASGI app, and a cleanup that writes a
sentinel file. The exit status mirrors ``arb.cli._run_ui``.

    python shutdown_target.py <uvloop|asyncio> <phase> <cleanup> <sentinel-path>

``phase`` is what ``main`` does before cleanup:

- ``serve``: start uvicorn and serve until a signal arrives;
- ``startup``: block before ``serve()``, the way ``run_ui`` does while it is
  still discovering markets;
- ``early-sigterm``: take a SIGTERM inside *synchronous* startup code and then
  go straight on to ``serve()``, which must not serve anything.

``cleanup`` is what cleanup does between its two sentinel lines:

- ``quick``: one short suspension, as a drain with nothing queued;
- ``second-sigterm``: wait until a second SIGTERM has been handled;
- ``interrupt``: wait until the main task has been cancelled (the first Ctrl+C
  of a stop that SIGTERM started), then suspend once more;
- ``abandon``: a drain waiting out a dead database. Only being abandoned ends
  it early.

It talks to the test on stdout, one line per event: ``STARTING``,
``SERVE-ENTERED``, ``SERVING <port>``, ``CLEANUP-STARTED``.
"""

from __future__ import annotations

import asyncio
import signal
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn
import uvloop

from arb.shutdown import Shutdown, serve_then_cleanup

# How long cleanup waits for the signal a test is about to send before giving
# up loudly.
SIGNAL_WAIT_S = 30.0
# The "abandon" cleanup: long enough that finishing it cannot be mistaken for
# abandoning it, short enough that a regression fails before the test times out.
ABANDONED_DRAIN_S = 20.0
# Nobody is coming back for a child that is still alive after this: the test
# runner was killed hard (SIGKILL skips its fixtures). SIGALRM's default action
# ends the process, so an orphan removes itself.
ORPHAN_LIMIT_S = 300


def say(line: str) -> None:
    print(line, flush=True)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class AnnouncingServer(uvicorn.Server):
    """Says when ``serve()`` is entered, and prints the ephemeral port once
    uvicorn is listening — by which point uvicorn has also taken SIGINT and
    SIGTERM for itself."""

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        say("SERVE-ENTERED")
        await super().serve(sockets=sockets)

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        say(f"SERVING {self.servers[0].sockets[0].getsockname()[1]}")


async def run(phase: str, cleanup_kind: str, sentinel: Path) -> None:
    handle: list[Shutdown] = []
    outer = asyncio.current_task()
    assert outer is not None

    async def main(shutdown: Shutdown) -> None:
        handle.append(shutdown)
        if phase == "startup":
            # Startup that has not finished: run_ui awaiting venue discovery.
            say("STARTING")
            await asyncio.Event().wait()
        if phase == "early-sigterm":
            # Startup that is not awaiting anything when the signal arrives:
            # the handler's cancellation is still pending at serve().
            signal.raise_signal(signal.SIGTERM)
        server = AnnouncingServer(
            uvicorn.Config(
                app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off"
            )
        )
        await shutdown.serve(server)

    async def cleanup() -> None:
        with sentinel.open("a") as out:
            out.write("cleanup-started\n")
        # Read before the test is told it may send the next signal.
        cancels_so_far = outer.cancelling()
        say("CLEANUP-STARTED")
        if cleanup_kind == "second-sigterm":
            # Hold cleanup open until the second SIGTERM has been handled, so
            # the test proves it arrived mid-cleanup and changed nothing.
            async with asyncio.timeout(SIGNAL_WAIT_S):
                # The count is bumped from a signal handler: there is no
                # event to wait on, so this one condition is polled.
                while handle[0].sigterms < 2:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        elif cleanup_kind == "interrupt":
            # Hold cleanup open until asyncio.Runner has answered the Ctrl+C
            # by cancelling the main task. If that cancellation could reach
            # cleanup, it would land on the sleep below, or on the next one.
            async with asyncio.timeout(SIGNAL_WAIT_S):
                while outer.cancelling() == cancels_so_far:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        elif cleanup_kind == "abandon":
            await asyncio.sleep(ABANDONED_DRAIN_S)
        else:
            # Cleanup that suspends, as a recorder drain does: a cancellation
            # left pending by the signal would surface here, not pass unseen.
            await asyncio.sleep(0.05)
        with sentinel.open("a") as out:
            out.write("cleanup-complete\n")

    await serve_then_cleanup(main, cleanup)


def main(argv: list[str]) -> int:
    loop_name, phase, cleanup_kind, sentinel = argv
    # The child models a foreground `arb ui`, whatever launched pytest. A
    # shell's background job (`pytest &`, nohup, some CI runners) starts with
    # SIGINT ignored; that is inherited, Python then leaves it ignored, and
    # asyncio.Runner only takes SIGINT over from default_int_handler — so
    # without this line Ctrl+C during startup would do nothing at all.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.alarm(ORPHAN_LIMIT_S)
    runner = uvloop.run if loop_name == "uvloop" else asyncio.run
    try:
        runner(run(phase, cleanup_kind, Path(sentinel)))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
