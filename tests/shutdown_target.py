"""The child process ``tests/test_shutdown.py`` signals. Not a test module.

It is ``arb ui`` with everything but the shutdown path removed: the same
``arb.shutdown.serve_then_cleanup`` that ``run_ui`` calls, a real uvicorn
server on an ephemeral port, a trivial ASGI app, and a cleanup that writes a
sentinel file. The exit status mirrors ``arb.cli._run_ui``.

    python shutdown_target.py <uvloop|asyncio> <serve|startup|second-sigterm> <sentinel-path>

It talks to the test on stdout, one line per event: ``STARTING``,
``SERVING <port>``, ``CLEANUP-STARTED``.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn
import uvloop

from arb.shutdown import Shutdown, serve_then_cleanup

# How long cleanup waits to see the second SIGTERM before giving up loudly.
SECOND_SIGTERM_WAIT_S = 30.0


def say(line: str) -> None:
    print(line, flush=True)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class AnnouncingServer(uvicorn.Server):
    """Prints the ephemeral port once uvicorn is listening — by which point
    uvicorn has also taken SIGINT and SIGTERM for itself."""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        say(f"SERVING {self.servers[0].sockets[0].getsockname()[1]}")


async def run(mode: str, sentinel: Path) -> None:
    handle: list[Shutdown] = []

    async def main(shutdown: Shutdown) -> None:
        handle.append(shutdown)
        if mode == "startup":
            # Startup that has not finished: run_ui awaiting venue discovery.
            say("STARTING")
            await asyncio.Event().wait()
        server = AnnouncingServer(
            uvicorn.Config(
                app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="off"
            )
        )
        await shutdown.serve(server)

    async def cleanup() -> None:
        with sentinel.open("a") as out:
            out.write("cleanup-started\n")
        say("CLEANUP-STARTED")
        if mode == "second-sigterm":
            # Hold cleanup open until the second SIGTERM has been handled, so
            # the test proves it arrived mid-cleanup and changed nothing.
            async with asyncio.timeout(SECOND_SIGTERM_WAIT_S):
                # The count is bumped from a signal handler: there is no
                # event to wait on, so this one condition is polled.
                while handle[0].sigterms < 2:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        else:
            # Cleanup that suspends, as a recorder drain does: a cancellation
            # left pending by the signal would surface here, not pass unseen.
            await asyncio.sleep(0.05)
        with sentinel.open("a") as out:
            out.write("cleanup-complete\n")

    await serve_then_cleanup(main, cleanup)


def main(argv: list[str]) -> int:
    loop_name, mode, sentinel = argv
    runner = uvloop.run if loop_name == "uvloop" else asyncio.run
    try:
        runner(run(mode, Path(sentinel)))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
