"""Network security floor for the terminal UI.

The UI is about to grow write endpoints that start and stop a process which
will eventually place real orders. This module is what stands between those
endpoints and the rest of the machine's network.

Threat model, and why this is the whole story
---------------------------------------------
The UI is a **single-operator localhost tool**. There is no login, no session
and no user table, so there is nothing to bind a CSRF token *to*: a token
minted by the same server that accepts it, with no session cookie behind it,
is an Origin check with extra steps and a cookie jar to get wrong. What a
browser-borne attacker actually has is (a) the ability to make a cross-site
request from a page the operator visited and (b) DNS rebinding, which turns a
hostname the attacker controls into ``127.0.0.1`` and lets that page speak to
this server as if it were same-origin. So:

* ``Host`` must resolve to loopback (or be explicitly allowlisted). That kills
  DNS rebinding, because a rebound request still carries the attacker's
  hostname in ``Host``.
* A state-changing request (anything but ``GET``/``HEAD``) that carries an
  ``Origin`` must carry one we accept, and ``Sec-Fetch-Site: cross-site`` is
  refused outright. That kills the cross-site POST. A missing ``Origin`` is
  allowed: that is ``curl``/``httpx``/the CLI, which is a process already on
  this machine and could talk to the socket directly anyway.
* The WebSocket handshake gets the same ``Origin`` treatment. ``Origin`` is
  the *only* defence there — the same-origin policy does not apply to
  ``WebSocket``, so without this a cross-site page gets the live feed.

**Do not "improve" this into tokens, cookies or sessions.** Until there is
real authentication to attach an identity to, they add moving parts and no
security. If the UI is ever exposed beyond loopback for real, the answer is an
authenticating reverse proxy (or the SSH tunnel that is already the documented
way in), not a home-grown token.

Nothing here is fatal at request time: a rejection is counted, logged at
WARNING and answered with 403 (HTTP) or a 1008 close (WebSocket).
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from arb.metrics import UI_REQUESTS_REJECTED

log = logging.getLogger(__name__)

# Hostnames that are loopback by definition rather than by parsing. Anything
# that parses as an IP goes through ipaddress.is_loopback instead, so this list
# does not need every 127.x.x.x form.
LOOPBACK_HOST_NAMES: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

# Methods that may not change state, per RFC 9110. OPTIONS is deliberately not
# here: we do not serve CORS preflights, so an OPTIONS is either harmless or
# something we would rather refuse.
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


# --------------------------------------------------------------------------
# host / origin parsing
# --------------------------------------------------------------------------


def parse_csv(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Split a comma-separated config string into stripped, non-empty items.

    Config allowlists are plain strings rather than JSON lists because
    pydantic-settings parses complex env values as JSON, and ``a,b`` is not
    JSON. An already-split iterable passes through unchanged.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    return tuple(item.strip() for item in items if item.strip())


def split_host_port(value: str) -> tuple[str, int | None]:
    """Split a ``Host``-header-shaped value into (hostname, port).

    Handles bracketed IPv6 (``[::1]:8080``), bare IPv6 (``::1``, which has no
    port because it is ambiguous), ``host:port`` and bare ``host``. The
    hostname is lowercased and stripped of its brackets.
    """
    raw = value.strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return raw.lower(), None
        host = raw[1:end]
        rest = raw[end + 1 :]
        port: int | None = None
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                port = None
        return host.lower(), port
    if raw.count(":") > 1:
        # Bare IPv6 literal, no port (a Host header must bracket it to carry one).
        return raw.lower(), None
    if ":" in raw:
        host, _, port_s = raw.partition(":")
        try:
            return host.lower(), int(port_s)
        except ValueError:
            return host.lower(), None
    return raw.lower(), None


def is_loopback_host(host: str) -> bool:
    """True if ``host`` (a hostname, no port) is unambiguously loopback."""
    candidate = host.strip().lower().strip("[]")
    if not candidate:
        return False
    if candidate in LOOPBACK_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def normalize_origin(value: str) -> str:
    """Canonical ``scheme://host[:port]`` form, lowercased, for comparison."""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip().lower()
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def is_loopback_origin(origin: str) -> bool:
    """True if an ``Origin`` header names a loopback http(s) origin.

    ``null`` (sandboxed iframe, ``file://``, some redirects) is never loopback.
    """
    value = origin.strip()
    if not value or value.lower() == "null":
        return False
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    host, _ = split_host_port(parts.netloc)
    return is_loopback_host(host)


# --------------------------------------------------------------------------
# bind guard
# --------------------------------------------------------------------------


class RemoteBindRefused(RuntimeError):
    """Raised when the UI is asked to bind a non-loopback host without the
    explicit escape hatch."""


@dataclass(frozen=True, slots=True)
class BindInfo:
    """What the UI bound, and whether that is loopback — the UI shows a banner
    when it is not."""

    host: str
    loopback: bool
    remote_allowed: bool


def check_bind_host(host: str, *, allow_remote: bool) -> BindInfo:
    """Validate a bind host before the server starts listening.

    A non-loopback bind (``0.0.0.0``, a LAN address, ``::``) exposes controls
    that start a trading process to every machine that can route to this one,
    so it requires an explicit opt-in: ``ARB_ALLOW_REMOTE_BIND=1``. Compose
    legitimately needs ``0.0.0.0`` *inside* its network (the published port is
    still ``127.0.0.1``-only), which is why the hatch exists and is settable
    from the environment.

    Raises ``RemoteBindRefused`` when the bind is not loopback and the hatch is
    not set. This is a startup-time refusal, not a runtime failure path.
    """
    hostname, _ = split_host_port(host)
    loopback = is_loopback_host(hostname)
    if not loopback and not allow_remote:
        raise RemoteBindRefused(
            f"refusing to bind UI to non-loopback host {host!r}: the UI has no "
            "authentication and its controls start a trading process. Bind "
            "127.0.0.1 and use an SSH tunnel, or set ARB_ALLOW_REMOTE_BIND=1 "
            "if this port is already protected (compose sets it for the app service)."
        )
    return BindInfo(host=host, loopback=loopback, remote_allowed=allow_remote)


# --------------------------------------------------------------------------
# the middleware
# --------------------------------------------------------------------------


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class OriginGuardMiddleware:
    """Pure-ASGI ``Host``/``Origin`` guard over both ``http`` and ``websocket``.

    Pure ASGI rather than Starlette's ``BaseHTTPMiddleware`` because the latter
    only sees ``http`` scopes, and the WebSocket handshake is exactly the hole
    that matters here.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Sequence[str] = (),
        allowed_origins: Sequence[str] = (),
    ) -> None:
        self.app = app
        # Allowlist entries are compared against the hostname with the port
        # stripped, so "arb.internal" and "arb.internal:8080" both work.
        self._allowed_hosts = frozenset(split_host_port(h)[0] for h in allowed_hosts if h.strip())
        self._allowed_origins = frozenset(normalize_origin(o) for o in allowed_origins if o.strip())

    # -- checks ------------------------------------------------------------

    def host_allowed(self, host_header: str | None) -> bool:
        if host_header is None:
            return False
        hostname, _ = split_host_port(host_header)
        if not hostname:
            return False
        return is_loopback_host(hostname) or hostname in self._allowed_hosts

    def origin_allowed(self, origin: str | None) -> bool:
        """A missing ``Origin`` is allowed (non-browser client); a present one
        must be loopback or explicitly allowlisted."""
        if origin is None:
            return True
        if normalize_origin(origin) in self._allowed_origins:
            return True
        return is_loopback_origin(origin)

    def reject_reason(self, scope: Scope) -> str | None:
        """The reason this scope must be refused, or None to let it through."""
        if not self.host_allowed(_header(scope, b"host")):
            return "host"
        is_ws = scope.get("type") == "websocket"
        method = str(scope.get("method", "GET")).upper()
        # A WebSocket handshake is a GET, but it is state-changing in every way
        # that matters (it hands out the live feed), so it is always checked.
        if not is_ws and method in SAFE_METHODS:
            return None
        site = (_header(scope, b"sec-fetch-site") or "").strip().lower()
        if site == "cross-site":
            return "sec_fetch_site"
        if not self.origin_allowed(_header(scope, b"origin")):
            return "origin"
        return None

    # -- ASGI --------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self.reject_reason(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        UI_REQUESTS_REJECTED.labels(scope=str(scope_type), reason=reason).inc()
        log.warning(
            "ui guard refused %s %s: %s (host=%r origin=%r sec-fetch-site=%r)",
            scope_type,
            scope.get("path", ""),
            reason,
            _header(scope, b"host"),
            _header(scope, b"origin"),
            _header(scope, b"sec-fetch-site"),
        )
        if scope_type == "http":
            await self._deny_http(send, reason)
        else:
            await self._deny_websocket(receive, send)

    async def _deny_http(self, send: Send, reason: str) -> None:
        body = f"forbidden: {reason}\n".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _deny_websocket(self, receive: Receive, send: Send) -> None:
        # The ASGI contract is: wait for websocket.connect, then accept or
        # close. Closing before accepting makes the server answer the
        # handshake with an HTTP rejection instead of upgrading.
        message = await receive()
        if message.get("type") == "websocket.connect":
            await send({"type": "websocket.close", "code": 1008})
