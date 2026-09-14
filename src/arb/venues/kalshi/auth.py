"""Kalshi request signing.

Scheme verified against
https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.md
and https://docs.kalshi.com/getting_started/quick_start_websockets.md
(recorded in docs/venue-notes.md):

- headers ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-TIMESTAMP`` (ms),
  ``KALSHI-ACCESS-SIGNATURE``
- string to sign: ``timestamp + METHOD + path`` (path without query params;
  for the WebSocket handshake the path is ``/trade-api/ws/v2`` with GET)
- RSA-PSS with SHA-256, MGF1/SHA-256, salt length = digest length,
  base64-encoded

Key material never leaves this module and is never logged or printed.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

WS_SIGN_PATH = "/trade-api/ws/v2"

_PSS_PADDING = padding.PSS(
    mgf=padding.MGF1(hashes.SHA256()),
    salt_length=padding.PSS.DIGEST_LENGTH,
)


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"{path} is not an RSA private key")
    return key


def sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(message.encode(), _PSS_PADDING, hashes.SHA256())
    return base64.b64encode(signature).decode()


def auth_headers(
    *,
    key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Signed headers for one request. ``path`` starts at the API root and
    excludes query parameters (e.g. ``/trade-api/v2/portfolio/balance``)."""
    ts = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, f"{ts}{method}{path}"),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


def ws_auth_headers(*, key_id: str, private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    """Headers for the WebSocket connection handshake (recomputed per attempt)."""
    return auth_headers(key_id=key_id, private_key=private_key, method="GET", path=WS_SIGN_PATH)
