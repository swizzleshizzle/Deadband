"""Coinbase CDP ES256 JWT construction. I/O-free, clock-free by parameter."""

from __future__ import annotations

from datetime import datetime

import jwt
from cryptography.hazmat.primitives import serialization

_EXPIRY_SECONDS = 120


def build_jwt(
    api_key: str,
    private_key_pem: str,
    uri: str,
    *,
    now: datetime,
    nonce: str,
) -> str:
    """A single-request bearer token.

    `now` and `nonce` are parameters rather than internal calls so expiry is
    testable without sleeping, and so this module stays clock-free like the
    pure layer it sits beside.
    """
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except Exception as exc:  # noqa: BLE001 - re-raised as ValueError below
        raise ValueError(f"Coinbase private key is not a readable PEM: {exc}") from exc

    issued = int(now.timestamp())
    return jwt.encode(
        {
            "iss": "cdp",
            "sub": api_key,
            "nbf": issued,
            "exp": issued + _EXPIRY_SECONDS,
            "uri": uri,
        },
        key,
        algorithm="ES256",
        headers={"kid": api_key, "nonce": nonce},
    )
