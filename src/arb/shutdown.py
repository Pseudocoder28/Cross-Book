"""Serve, then always clean up — on Ctrl+C *and* on SIGTERM.

``uvicorn.Server.serve()`` takes SIGINT and SIGTERM for itself with
``signal.signal``, shuts the HTTP server down gracefully, puts the previous
handlers back and then **re-delivers** every signal it captured with
``signal.raise_signal`` (``uvicorn/server.py``, ``capture_signals``). What
happens next depends entirely on what the previous handler was:

- SIGINT: ``asyncio.Runner`` installed one. It cancels the main task, so the
  code after ``serve()`` still runs.
- SIGTERM: nobody installed one, so the previous handler is ``SIG_DFL`` and the
  re-delivered signal kills the process *in the kernel*. No ``finally`` block
  runs. For ``arb ui`` that was every ``docker compose stop``: queued recorder
  messages lost, subprocess jobs orphaned, the database engine never disposed.

:func:`serve_then_cleanup` closes that hole by installing a SIGTERM handler
that does not terminate, *before* uvicorn looks. uvicorn saves it as the
previous handler, restores it and replays SIGTERM into it; the replay is a
no-op, ``serve()`` returns normally and cleanup runs to completion.

The handler never raises. A handler that raised would work for the replay,
which happens synchronously inside the main task, but the same handler also
fires wherever the main thread happens to be when a signal arrives outside
uvicorn's window — inside the event loop's internals or inside another task
(the recorder writer, mid-batch). Where an exception lands would be luck.

What SIGTERM does, by phase:

- **before the call** (interpreter start, imports, config, building the engine:
  about a second for ``arb ui``): the default disposition still applies and
  SIGTERM ends the process. Nothing has been queued or spawned by then, so
  there is nothing to flush.
- **startup** (before ``serve()``): cancels the main task, exactly what
  ``asyncio.Runner`` does for Ctrl+C. Cleanup runs, and the cancellation is
  claimed on the way out so the process exits 0 instead of with a traceback.
- **serving**: uvicorn owns the signal; this handler only sees the replay.
- **cleanup**: nothing. Cleanup is bounded, asking twice does not make a drain
  faster, and turning a repeated polite request into data loss is the opposite
  of the point. uvicorn treats a second SIGTERM the same way.
- **after cleanup**: the previous disposition is back, so SIGTERM ends the
  process at once. That is deliberate. What is left is interpreter teardown,
  which can wait a long time for a worker thread (``asyncio.to_thread``) to
  finish, and a process in that state has to stay killable.

Cleanup is also out of reach of a *cancellation*. It runs in a task of its
own, so a Ctrl+C that arrives while it is running — ``asyncio.Runner`` answers
the first one by cancelling the main task — cannot land on one of its awaits
and abandon a drain halfway. The cancellation is held until cleanup has
finished and re-raised then: everything is flushed and the exit status is 130.
Without this a single Ctrl+C after a SIGTERM-initiated stop (``kill <pid>``,
then Ctrl+C in the terminal) cut the drain short. What still abandons cleanup
is what should: a **second** Ctrl+C, which ``asyncio.Runner`` turns into
``KeyboardInterrupt`` and a teardown that cancels every task, and SIGKILL.

``signal.signal`` only works on the main thread, so off it (an in-process
caller, a test) nothing is installed and nothing crashes — uvicorn does not
capture signals there either. Whatever was installed is put back on exit, so
the handler never outlives the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
from collections.abc import Awaitable, Callable
from types import FrameType
from typing import Protocol

log = logging.getLogger(__name__)

type _Handler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None

_STARTUP = "startup"
_SERVING = "serving"
_CLEANUP = "cleanup"


class Server(Protocol):
    """The slice of ``uvicorn.Server`` that :meth:`Shutdown.serve` needs."""

    should_exit: bool

    async def serve(self) -> None: ...


class Shutdown:
    """The SIGTERM guard for one :func:`serve_then_cleanup` call."""

    def __init__(self) -> None:
        self._phase = _STARTUP
        self._server: Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None
        # One object, kept: ``self._on_sigterm`` builds a new bound method on
        # every access, and uninstalling compares by identity.
        self._handler = self._on_sigterm
        self._previous: _Handler = None
        self._installed = False
        self._sigterms = 0
        self._cancel_outstanding = False

    @property
    def installed(self) -> bool:
        """False off the main thread, where no handler can be installed."""
        return self._installed

    @property
    def sigterms(self) -> int:
        """SIGTERMs seen so far, uvicorn's replay included."""
        return self._sigterms

    async def serve(self, server: Server) -> None:
        """``await server.serve()`` with SIGTERM routed to a clean return."""
        if self._sigterms:
            # SIGTERM arrived during startup. Its cancellation normally never
            # lets execution get this far; if it is still pending, this is
            # where it lands — before anything is served, not inside cleanup.
            await asyncio.sleep(0)
            return
        self._server, self._phase = server, _SERVING
        try:
            await server.serve()
        finally:
            self._server, self._phase = None, _CLEANUP

    # -- the handler -------------------------------------------------------

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        # Runs on the main thread between two bytecodes of whatever was
        # executing. It must not raise, block or log from here.
        self._sigterms += 1
        server = self._server
        if server is not None:
            # uvicorn's replay (already exiting: a no-op), or the few
            # bytecodes either side of uvicorn's own capture window.
            server.should_exit = True
        elif self._phase == _STARTUP and not self._cancel_outstanding:
            task = self._task
            if task is not None and not task.done():
                self._cancel_outstanding = True
                task.cancel()
        loop = self._loop
        if loop is not None:
            # Wakes the loop, and logs from a context where logging is safe.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._announce, self._sigterms, self._phase)

    @staticmethod
    def _announce(count: int, phase: str) -> None:
        if count == 1:
            log.info("SIGTERM during %s: shutting down cleanly", phase)
        else:
            log.info("SIGTERM again during %s: already shutting down, cleanup continues", phase)

    # -- used by serve_then_cleanup ----------------------------------------

    def _install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            log.debug("not on the main thread: SIGTERM handling left as it is")
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            self._previous = signal.signal(signal.SIGTERM, self._handler)
        except ValueError:  # the main thread of a sub-interpreter
            log.debug("signal.signal refused: SIGTERM handling left as it is")
            return
        self._installed = True

    def _uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        # Undo our own install and nothing else: a handler someone put in
        # place after this one is theirs to remove.
        if signal.getsignal(signal.SIGTERM) is self._handler:
            # None means "installed from C": it cannot be handed back.
            previous = self._previous if self._previous is not None else signal.SIG_DFL
            signal.signal(signal.SIGTERM, previous)

    def _begin_cleanup(self) -> None:
        self._server, self._phase = None, _CLEANUP

    async def _absorb_pending_cancel(self) -> None:
        """Give a cancellation that is *pending* somewhere to land before cleanup.

        uvicorn replays Ctrl+C at the end of ``serve()``, inside the main
        task; ``asyncio.Runner`` answers by cancelling that task, and a task
        cancelled while it is running only finds out at its next suspension —
        which would be cleanup's first ``await``. It asked for a shutdown and
        a shutdown is what is happening, so it is taken here, and declared
        handled the way the asyncio docs require of anyone who swallows one.
        """
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            self._cancel_outstanding = False

    def _claim_cancellation(self) -> bool:
        """True when the CancelledError leaving startup is the one this
        handler sent, and nobody else has asked for the task to be cancelled."""
        if not self._cancel_outstanding or self._task is None:
            return False
        self._cancel_outstanding = False
        return self._task.uncancel() == 0


async def _run_to_completion(cleanup: Callable[[], Awaitable[None]]) -> None:
    """``await cleanup()``, except that cancelling the caller cannot stop it.

    ``cleanup`` runs in its own task. A cancellation of *this* task while it
    waits — the first Ctrl+C during a cleanup that SIGTERM started, or an
    in-process caller's timeout — is held, not swallowed: it is re-raised once
    cleanup has finished, so whoever sent it still sees it arrive.

    Cancelling the cleanup task itself still stops it. That is what
    ``asyncio.run`` does to every task after a second Ctrl+C, and it is the
    way out that has to keep working.
    """
    inner = asyncio.ensure_future(cleanup())
    held: asyncio.CancelledError | None = None
    while not inner.done():
        try:
            # wait(), not wait_for() or a bare await: when the waiting task is
            # cancelled, wait() leaves the task it was waiting on alone.
            await asyncio.wait([inner])
        except asyncio.CancelledError as exc:
            if held is None:
                log.info("interrupted during cleanup: finishing it first (Ctrl+C again abandons)")
            held = exc
    if inner.cancelled():
        raise held if held is not None else asyncio.CancelledError()
    inner.result()  # cleanup's own exception, if it raised one
    if held is not None:
        raise held


async def serve_then_cleanup(
    main: Callable[[Shutdown], Awaitable[None]],
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    """Run ``main(shutdown)``, then ``cleanup()`` — however ``main`` ended.

    ``main`` does its startup and then ``await shutdown.serve(server)``.
    ``cleanup`` runs to completion after a normal return, an exception, Ctrl+C
    or SIGTERM, and a cancellation that arrives while it runs waits for it to
    finish. Only SIGKILL and a second Ctrl+C cut it short. Exceptions from
    ``main`` propagate once cleanup is done.
    """
    shutdown = Shutdown()
    shutdown._install()
    try:
        try:
            await main(shutdown)
        finally:
            shutdown._begin_cleanup()
            await shutdown._absorb_pending_cancel()
            await _run_to_completion(cleanup)
    except asyncio.CancelledError:
        if not shutdown._claim_cancellation():
            raise
    finally:
        shutdown._uninstall()
