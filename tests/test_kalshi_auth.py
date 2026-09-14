"""Kalshi signing tests with throwaway generated keys (never real ones)."""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from arb.venues.kalshi.auth import auth_headers, load_private_key, sign, ws_auth_headers

_PSS_VERIFY = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def verify(key: rsa.RSAPrivateKey, signature_b64: str, message: str) -> None:
    key.public_key().verify(
        base64.b64decode(signature_b64), message.encode(), _PSS_VERIFY, hashes.SHA256()
    )  # raises InvalidSignature on mismatch


def test_signature_is_pss_sha256_over_message(rsa_key: rsa.RSAPrivateKey) -> None:
    message = "1703123456789GET/trade-api/v2/portfolio/balance"
    verify(rsa_key, sign(rsa_key, message), message)


def test_auth_headers_sign_timestamp_method_path(rsa_key: rsa.RSAPrivateKey) -> None:
    headers = auth_headers(
        key_id="test-key-id",
        private_key=rsa_key,
        method="GET",
        path="/trade-api/v2/markets",
        timestamp_ms=1703123456789,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    verify(rsa_key, headers["KALSHI-ACCESS-SIGNATURE"], "1703123456789GET/trade-api/v2/markets")


def test_ws_headers_sign_the_ws_path(rsa_key: rsa.RSAPrivateKey) -> None:
    headers = ws_auth_headers(key_id="test-key-id", private_key=rsa_key)
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    assert ts.isdigit() and len(ts) == 13  # milliseconds
    verify(rsa_key, headers["KALSHI-ACCESS-SIGNATURE"], f"{ts}GET/trade-api/ws/v2")


def test_load_private_key_roundtrip(tmp_path, rsa_key: rsa.RSAPrivateKey) -> None:  # type: ignore[no-untyped-def]
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    loaded = load_private_key(path)
    message = "roundtrip-check"
    verify(loaded, sign(loaded, message), message)


def test_load_private_key_rejects_non_rsa(tmp_path) -> None:  # type: ignore[no-untyped-def]
    other = ed25519.Ed25519PrivateKey.generate()
    pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    with pytest.raises(ValueError):
        load_private_key(path)
