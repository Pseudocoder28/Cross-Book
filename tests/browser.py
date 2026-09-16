"""Headless-Chrome harness: pytest drives the REAL frontend, over CDP.

Why this exists
---------------
The terminal's frontend is ~6k lines of dependency-free ES modules whose two
shipped defects were both *browser* defects — a key-resolution order that let a
typed word write to Postgres, and a render loop that destroyed an ``<input>``
between keystrokes. Neither is visible to a Python unit test, and neither is
visible to ``node --check``. They need a real DOM, real focus and a real
render loop, so this harness runs one.

What it deliberately does NOT do
--------------------------------
* It does not start ``arb ui``. That would open live WebSockets to Kalshi and
  poll Polymarket US on every test run, and would want Postgres. Instead it
  serves ``create_app(<stub state>, static_dir=STATIC_DIR)`` — the real app,
  the real static assets, deterministic data, zero venue traffic.
* It does not add a dependency. Chrome is spoken to over CDP using
  ``websockets`` (already a runtime dependency) and ``json``; the server is
  ``uvicorn`` (already a runtime dependency). No npm, no playwright, no
  selenium, no driver binary.

Shape
-----
``terminal(state)`` is the one entry point: it starts uvicorn on an ephemeral
port in a background thread, launches Chrome headless with its own throwaway
``--user-data-dir``, and yields a :class:`Browser`. Both halves are torn down
in ``finally`` blocks even when a test raises: no orphaned Chrome, no leaked
temp dir, no port left bound.

Keys go through ``Input.dispatchKeyEvent``, never a synthetic
``KeyboardEvent``. A ``KeyboardEvent`` dispatched on ``document`` has
``document`` as its target, so ``core/keys.js``'s ``scopeOf(e.target)`` reads
COMMAND no matter what is really focused, and every LIST-scope binding is
untestable. A CDP key event is delivered by the browser to the real
``activeElement``, which is the whole point.

Skipping
--------
``chrome_available()`` is false on a machine with no browser (a bare CI
container), and the tests skip rather than fail. This suite must never be the
reason the suite goes red somewhere without a browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from websockets.sync.client import ClientConnection, connect

from arb.config import AppConfig
from arb.paper import PaperLimits
from arb.run import RunContext
from arb.ui.control import ControlPlane
from arb.ui.security import OriginGuardMiddleware
from arb.ui.server import STATIC_DIR, create_app, no_control_payload
from tests.test_ui_server import StubState

# Generous but bounded: every wait below is a poll-until with this ceiling, so
# a hung browser fails a test in seconds instead of hanging the suite.
DEFAULT_TIMEOUT_S = 20.0
POLL_S = 0.02

# Input.dispatchKeyEvent modifier bitmask.
ALT, CTRL, META, SHIFT = 1, 2, 4, 8


class BrowserError(RuntimeError):
    """Anything the harness could not do: no Chrome, a CDP error, a timeout."""


# --------------------------------------------------------------------------
# finding Chrome
# --------------------------------------------------------------------------

CHROME_ENV = "ARB_CHROME"

_APP_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
_ON_PATH = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def chrome_path() -> str | None:
    """The Chrome/Chromium binary to drive, or None if there is none.

    ``ARB_CHROME`` wins so a machine with a browser somewhere unusual (or a CI
    image with a pinned build) can point at it without editing this file.
    """
    override = os.environ.get(CHROME_ENV, "").strip()
    if override:
        return override if Path(override).exists() else None
    for candidate in _APP_PATHS:
        if Path(candidate).exists():
            return candidate
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    return None


def chrome_available() -> bool:
    """True when a browser is present. Tests SKIP on false, never fail."""
    return chrome_path() is not None


# --------------------------------------------------------------------------
# the server under test
# --------------------------------------------------------------------------


def guarded_app(state: Any) -> Any:
    """``create_app`` wrapped exactly the way ``run_ui`` wraps it.

    The Origin/Host guard is applied in ``run_ui``, not in ``create_app``, so a
    test that talks to a bare ``create_app`` would be testing a server the
    operator never runs. The browser reaches this over ``127.0.0.1``, which the
    guard treats as loopback and lets through.
    """
    return OriginGuardMiddleware(create_app(state, static_dir=STATIC_DIR))


@contextlib.contextmanager
def serve(app: FastAPI | Any) -> Iterator[str]:
    """Run ``app`` on an ephemeral loopback port in a thread; yield its base URL.

    Port 0 means the OS picks a free port, so parallel runs and a previous
    run's lingering socket can never collide.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="arb-ui-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + DEFAULT_TIMEOUT_S
    try:
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise BrowserError("test UI server did not start")
            time.sleep(POLL_S)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{int(port)}"
    finally:
        server.should_exit = True
        thread.join(timeout=DEFAULT_TIMEOUT_S)


# --------------------------------------------------------------------------
# CDP over one WebSocket
# --------------------------------------------------------------------------


class _Cdp:
    """One DevTools session: request/response by id, events collected.

    A reader thread drains the socket so events (Network, Runtime) accumulate
    whether or not a command is in flight; commands wait on their own id.
    """

    def __init__(self, conn: ClientConnection) -> None:
        self._conn = conn
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._waiters: dict[int, queue.SimpleQueue[dict[str, Any]]] = {}
        self._events: list[dict[str, Any]] = []
        self._reader = threading.Thread(target=self._pump, name="cdp-reader", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        while True:
            try:
                raw = self._conn.recv()
            except Exception:
                return
            text = raw.decode() if isinstance(raw, bytes) else raw
            try:
                message: dict[str, Any] = json.loads(text)
            except ValueError:
                continue
            message_id = message.get("id")
            if message_id is None:
                with self._lock:
                    self._events.append(message)
                continue
            with self._lock:
                waiter = self._waiters.pop(int(message_id), None)
            if waiter is not None:
                waiter.put(message)

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        message_id = next(self._ids)
        waiter: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        with self._lock:
            self._waiters[message_id] = waiter
        self._conn.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        try:
            reply = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._lock:
                self._waiters.pop(message_id, None)
            raise BrowserError(f"CDP timed out waiting for {method}") from exc
        error = reply.get("error")
        if error:
            raise BrowserError(f"CDP {method} failed: {error}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def events(self, method: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e.get("params", {}) for e in self._events if e.get("method") == method]

    def drop_events(self) -> None:
        with self._lock:
            self._events.clear()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
        self._reader.join(timeout=2.0)


# --------------------------------------------------------------------------
# launching Chrome
# --------------------------------------------------------------------------

_FLAGS = (
    "--headless=new",
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",  # containers run as root; harmless on a workstation
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-sync",
    "--mute-audio",
    "--hide-scrollbars",
    "--window-size=1600,1000",
    # The pages render on requestAnimationFrame and repaint on a 1 s timer; a
    # throttled background renderer would make every wait below flaky.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # macOS: never touch the login keychain from a test.
    "--use-mock-keychain",
    "--password-store=basic",
)


def _devtools_port(user_dir: Path, proc: subprocess.Popen[bytes], deadline: float) -> int:
    marker = user_dir / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BrowserError(f"chrome exited with code {proc.returncode} before listening")
        if marker.exists():
            lines = marker.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return int(lines[0].strip())
        time.sleep(POLL_S)
    raise BrowserError("chrome never wrote DevToolsActivePort")


def _page_target(port: int, deadline: float) -> str:
    """The WebSocket URL of the first page target Chrome opens."""
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as body:
                targets = json.loads(body.read().decode())
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except Exception as exc:  # the endpoint is up a beat after the port file
            last = exc
        time.sleep(POLL_S)
    raise BrowserError(f"no chrome page target appeared ({last})")


@contextlib.contextmanager
def _chrome() -> Iterator[_Cdp]:
    """Launch Chrome in a throwaway profile; yield an attached CDP session.

    Teardown kills the process and removes the profile whatever happened
    inside — an orphaned headless Chrome holding a temp dir is exactly the
    kind of debris a test suite must not leave behind.
    """
    binary = chrome_path()
    if binary is None:
        raise BrowserError("no Chrome/Chromium found")
    user_dir = Path(tempfile.mkdtemp(prefix="arb-chrome-"))
    proc = subprocess.Popen(
        [binary, *_FLAGS, f"--user-data-dir={user_dir}", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp: _Cdp | None = None
    stack = contextlib.ExitStack()
    try:
        deadline = time.monotonic() + DEFAULT_TIMEOUT_S
        port = _devtools_port(user_dir, proc, deadline)
        # websockets requires its sync connection to be used as a context
        # manager; the stack closes it before Chrome is signalled.
        conn = stack.enter_context(
            connect(_page_target(port, deadline), open_timeout=DEFAULT_TIMEOUT_S, max_size=None)
        )
        cdp = _Cdp(conn)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.send("Log.enable")
        yield cdp
    finally:
        if cdp is not None:
            cdp.close()
        stack.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        shutil.rmtree(user_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Request:
    """One request the page made, as the Network domain saw it."""

    method: str
    url: str

    def hits(self, fragment: str, method: str | None = None) -> bool:
        return fragment in self.url and (method is None or self.method == method)


@dataclass(frozen=True, slots=True)
class ConsoleLine:
    level: str
    text: str


_SPECIAL_KEYS: dict[str, tuple[int, str | None]] = {
    "Enter": (13, "\r"),
    "Escape": (27, None),
    "Backspace": (8, None),
    "Tab": (9, None),
    "ArrowUp": (38, None),
    "ArrowDown": (40, None),
    "ArrowLeft": (37, None),
    "ArrowRight": (39, None),
    "Home": (36, None),
    "End": (35, None),
    "PageUp": (33, None),
    "PageDown": (34, None),
    "Delete": (46, None),
}

_PUNCT_CODES: dict[str, tuple[str, int]] = {
    " ": ("Space", 32),
    "/": ("Slash", 191),
    "-": ("Minus", 189),
    ".": ("Period", 190),
    ",": ("Comma", 188),
    ";": ("Semicolon", 186),
    ":": ("Semicolon", 186),
    "?": ("Slash", 191),
}


def _printable_key(char: str) -> tuple[str, int]:
    """(code, windowsVirtualKeyCode) for a single printable character."""
    if char.isalpha():
        return "Key" + char.upper(), ord(char.upper())
    if char.isdigit():
        return "Digit" + char, ord(char)
    return _PUNCT_CODES.get(char, ("", 0))


class Browser:
    """A small driver over one page target. Everything is a poll-until."""

    def __init__(self, cdp: _Cdp, base_url: str) -> None:
        self._cdp = cdp
        self.base_url = base_url

    # -- evaluation ------------------------------------------------------

    def eval(self, js: str) -> Any:
        """Evaluate an expression in the page and return it by value."""
        result = self._cdp.send(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": True},
        )
        details = result.get("exceptionDetails")
        if details:
            text = details.get("exception", {}).get("description") or details.get("text")
            raise BrowserError(f"page threw while evaluating: {text}")
        return result.get("result", {}).get("value")

    def wait_for(self, js: str, what: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
        """Poll ``js`` until it is truthy. Returns the value it settled on."""
        deadline = time.monotonic() + timeout
        value: Any = None
        while time.monotonic() < deadline:
            value = self.eval(js)
            if value:
                return value
            time.sleep(POLL_S)
        raise BrowserError(
            f"timed out waiting for {what} (last value {value!r}); console: {self.console()}"
        )

    # -- navigation ------------------------------------------------------

    def goto(self, path: str, *, ready: str | None = None) -> None:
        """Full-page load of ``path``, then wait for the SPA shell to boot.

        A token planted in the current document is checked for absence after
        the navigation, so a stale evaluation against the outgoing document
        can never be mistaken for the new page being ready.
        """
        self.eval("window.__arbNavToken = 1")
        self._cdp.send("Page.navigate", {"url": self.base_url + path})
        # show() sets document.title to "ARB · <PAGE>" as it mounts, on
        # every route including /market/<id> which has no nav tab.
        self.wait_for(
            'window.__arbNavToken === undefined && document.readyState === "complete"'
            ' && document.title.indexOf("ARB \\u00b7 ") === 0',
            f"{path} to boot",
        )
        if ready:
            self.wait_for(ready, f"{path} to be ready")

    def wait_ws_live(self) -> None:
        """Wait until the session WebSocket is open (status bar says LIVE)."""
        self.wait_for(
            'document.getElementById("conn").className.indexOf("conn-live") >= 0',
            "the session WebSocket to open",
        )

    # -- input -----------------------------------------------------------

    def key(self, name: str, *, modifiers: int = 0, repeat: bool = False) -> None:
        """Dispatch one REAL key press+release to the focused element."""
        special = _SPECIAL_KEYS.get(name)
        if special is not None:
            vk, text = special
            code = name
            key_name = name
        elif len(name) == 1:
            code, vk = _printable_key(name)
            text = name
            key_name = name
        else:
            raise BrowserError(f"unknown key {name!r}")
        down: dict[str, Any] = {
            "type": "keyDown" if text else "rawKeyDown",
            "key": key_name,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
            "autoRepeat": repeat,
        }
        if text:
            down["text"] = text
            down["unmodifiedText"] = text
        self._cdp.send("Input.dispatchKeyEvent", down)
        self._cdp.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key_name,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "modifiers": modifiers,
            },
        )

    def type(self, text: str) -> None:
        """Type a string one real keystroke at a time."""
        for char in text:
            self.key(char)

    def click(self, selector: str) -> None:
        """A real left click at the centre of ``selector``."""
        where = self.eval(
            "(() => { const e = document.querySelector(" + json.dumps(selector) + ");"
            ' if (!e) return null; e.scrollIntoView({block: "center"});'
            " const r = e.getBoundingClientRect();"
            " return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()"
        )
        if not where:
            raise BrowserError(f"no element matches {selector!r}")
        x, y = int(where[0]), int(where[1])
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self._cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **base})
        self._cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "buttons": 0, **base})

    # -- observation -----------------------------------------------------

    def focused(self) -> str:
        """id of the focused element, or "<TAG>" when it has none."""
        return str(
            self.eval(
                "(() => { const a = document.activeElement;"
                ' return !a ? "<none>" : (a.id || "<" + a.tagName + ">"); })()'
            )
        )

    def text(self, selector: str) -> str:
        return str(
            self.eval(
                "(() => { const e = document.querySelector("
                + json.dumps(selector)
                + '); return e ? e.textContent : ""; })()'
            )
        )

    def requests(self) -> list[Request]:
        """Every request the page has issued since the last ``clear()``."""
        out: list[Request] = []
        for params in self._cdp.events("Network.requestWillBeSent"):
            request = params.get("request", {})
            out.append(Request(str(request.get("method", "")), str(request.get("url", ""))))
        return out

    def console(self) -> list[ConsoleLine]:
        """console.* output, uncaught exceptions, and browser-level errors.

        ``Log.entryAdded`` is the one that catches what page JS never sees: a
        stylesheet or module that 404s, a CSP violation, mixed content. Without
        it a page can render completely unstyled and still look "clean".
        """
        out: list[ConsoleLine] = []
        for params in self._cdp.events("Runtime.consoleAPICalled"):
            args = params.get("args", [])
            parts = [str(a.get("value", a.get("description", ""))) for a in args]
            out.append(ConsoleLine(str(params.get("type", "log")), " ".join(parts).strip()))
        for params in self._cdp.events("Log.entryAdded"):
            entry = params.get("entry", {})
            if entry.get("level") == "error":
                out.append(ConsoleLine("error", str(entry.get("text", ""))))
        for params in self._cdp.events("Runtime.exceptionThrown"):
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text", "")
            out.append(ConsoleLine("exception", str(text)))
        return out

    def clear(self) -> None:
        """Forget every captured request and console line."""
        self._cdp.drop_events()


@contextlib.contextmanager
def terminal(state: Any) -> Iterator[Browser]:
    """Serve the real UI over ``state`` and drive it in headless Chrome."""
    with serve(guarded_app(state)) as base_url, _chrome() as cdp:
        yield Browser(cdp, base_url)


# --------------------------------------------------------------------------
# the stub state the browser talks to
# --------------------------------------------------------------------------


def _pair(pair_id: int, ticker: str, slug: str, score: float) -> dict[str, Any]:
    return {
        "id": pair_id,
        "status": "proposed",
        "score": score,
        "kalshi": {
            "ticker": ticker,
            "outcome": "YES",
            "event_title": "TEST EVENT " + ticker,
            "event_slug": ticker.lower(),
            "close_time": "2026-01-01T00:00:00Z",
            "rules": "settles YES if the thing happens",
        },
        "polymarket_us": {
            "ticker": slug,
            "outcome": "YES",
            "event_title": "TEST EVENT " + slug,
            "event_slug": slug,
            "close_time": "2026-01-01T00:00:00Z",
            "rules": "resolves YES if the thing happens",
        },
        "features": {
            "title_similarity": 0.91,
            "outcome_similarity": 0.88,
            "outcome_overlap": 1.0,
            "days_apart": 0,
        },
    }


def _real_action_specs() -> list[Any]:
    """Every ActionSpec the real ControlPlane registers.

    Built against a throwaway host with no runtime attached: registration does
    not touch the sources, so the descriptors are the production ones even
    though nothing here can be executed.
    """
    from tests.test_control import FakeHost

    plane = ControlPlane(
        config=AppConfig(_env_file=None),  # pyright: ignore[reportCallIssue]
        host=FakeHost(),
        run=RunContext("browsertest"),
        engine=None,
    )
    return list(plane.actions.values())


def control_payload(*, run_id: str, recording: bool) -> dict[str, Any]:
    """A control payload with a plane attached, built from the real shapes.

    Starts from :func:`no_control_payload` so every key the page reads exists,
    then flips ``read_only`` off and publishes a few real action descriptors —
    the page renders entirely from these, so a stub that omitted them would
    disable every button and test nothing.
    """
    payload = no_control_payload(run_id=run_id, recording=recording)
    payload["read_only"] = False
    payload["bind"] = {"host": "127.0.0.1", "loopback": True, "remote_allowed": False}
    payload["paper"] = {
        "attached": True,
        "enabled": True,
        "suspended": False,
        "limits": PaperLimits().payload(),
        "notional_ticks": 125_000,
        "skipped_suspended": 0,
    }
    payload["pairs_top"] = 20
    payload["tracked_pairs"] = 3
    # The real registry, not a hand-written copy. The page renders entirely
    # from these descriptors — grade, summary, whether it confirms — so a stub
    # list is a suite that drives a fiction: it had already drifted (9 actions
    # against 13, pairs.top published G3 when it is G2) before this was fixed.
    payload["actions"] = [spec.payload() for spec in _real_action_specs()]
    payload["confirm_ttl_s"] = 30.0
    return payload


class TerminalState(StubState):
    """``StubState`` with the data the pages actually render, plus fan-out.

    Extends the existing UIState stub rather than inventing a second one, so
    the shapes the browser sees are the same ones ``tests/test_ui_server.py``
    pins. Adds three things it needs: pair rows worth reviewing, a control
    payload with a plane attached, and ``push_control`` so a test can drive a
    live ``{"t":"control"}`` frame from the test thread.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = [
            _pair(101, "KXTEST-A", "test-a", 0.94),
            _pair(102, "KXTEST-B", "test-b", 0.88),
            _pair(103, "KXTEST-C", "test-c", 0.81),
        ]
        self.decisions: list[tuple[int, str]] = []
        self.control = control_payload(run_id=self.run_id, recording=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: list[tuple[object, asyncio.Queue[str]]] = []

    # -- pairs -----------------------------------------------------------

    async def list_pairs(self, status: str | None) -> list[dict[str, Any]]:
        return [r for r in self.rows if status is None or r["status"] == status]

    async def decide_pair(self, pair_id: int, status: str) -> dict[str, Any] | None:
        for row in self.rows:
            if row["id"] == pair_id:
                self.decisions.append((pair_id, status))
                row["status"] = status
                return row
        return None

    async def decide_pairs(self, pair_ids: list[int], status: str) -> int:
        updated = 0
        for pair_id in pair_ids:
            if await self.decide_pair(pair_id, status) is not None:
                updated += 1
        return updated

    # -- clients ---------------------------------------------------------

    def add_client(self, ws: object) -> asyncio.Queue[str]:
        self._loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._clients.append((ws, outbound))
        self.added += 1
        return outbound

    def remove_client(self, ws: object) -> None:
        self._clients = [c for c in self._clients if c[0] is not ws]
        self.removed += 1

    def broadcast(self, frame: dict[str, Any]) -> None:
        """Push one frame to every connected browser, from the test thread."""
        loop = self._loop
        if loop is None:
            raise BrowserError("no browser client has connected yet")
        text = json.dumps(frame)

        def put() -> None:
            for _ws, outbound in self._clients:
                with contextlib.suppress(asyncio.QueueFull):
                    outbound.put_nowait(text)

        loop.call_soon_threadsafe(put)

    def push_control(self, **overrides: Any) -> None:
        """Change the control state and broadcast it, as the real plane does."""
        self.control = {**self.control, **overrides, "ts_ms": int(time.time() * 1000)}
        self.recording = bool(self.control["recording"])
        self.broadcast({"t": "control", "control": self.control})
