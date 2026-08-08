# tests/test_coinbase_auth.py
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from venues.coinbase_auth import build_jwt

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
URI = "GET api.coinbase.com/api/v3/brokerage/orders/historical/fills"


def _keypair():
    """A throwaway P-256 key. Generated per-test: a private key committed to
    a public repo is a leaked credential even when it opens nothing."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def test_jwt_carries_the_claims_coinbase_requires():
    pem, pub = _keypair()
    api_key = "organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID"
    token = build_jwt(api_key, pem, URI, now=NOW, nonce="abc")

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == api_key
    assert header["nonce"] == "abc"

    # NOW is a fixed synthetic clock value, not the real wall clock, so nbf/exp
    # validation against real time is disabled here -- those claims are
    # asserted explicitly below instead. Without this, the test's pass/fail
    # depends on whether it happens to run before or after the hardcoded NOW,
    # which is exactly the kind of clock-dependent flakiness `now` being a
    # parameter is supposed to prevent.
    claims = jwt.decode(
        token,
        pub,
        algorithms=["ES256"],
        audience=None,
        options={"verify_aud": False, "verify_nbf": False, "verify_exp": False},
    )
    assert claims["iss"] == "cdp"
    assert claims["sub"] == api_key
    assert claims["uri"] == URI
    assert claims["nbf"] == int(NOW.timestamp())
    assert claims["exp"] == int(NOW.timestamp()) + 120


def test_expiry_is_two_minutes_not_two_hours():
    """A too-long expiry turns a 120-second credential into a long-lived
    bearer token. Asserted on the delta, not the absolute value, so it
    cannot pass by coincidence of the chosen NOW."""
    pem, _ = _keypair()
    token = build_jwt("k", pem, URI, now=NOW, nonce="n")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["nbf"] == 120


def test_a_malformed_private_key_raises_rather_than_returning_none():
    """Fail loud. A signer that returns None on a bad key produces an
    unauthenticated request, which the API answers with an empty result --
    the 'success while fetching nothing' shape spec §10 gap 5 names."""
    with pytest.raises(ValueError):
        build_jwt("k", "not a pem", URI, now=NOW, nonce="n")
