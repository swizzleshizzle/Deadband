import json

import httpx
import pytest

from venues.coinbase_client import CoinbaseCredentials, fetch_all_fills

PEM = None  # set in fixture below


@pytest.fixture(autouse=True)
def _keypair(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("COINBASE_API_KEY", "organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID")
    monkeypatch.setenv("COINBASE_API_SECRET", pem)


def _page(fills, cursor=""):
    return httpx.Response(200, json={"fills": fills, "cursor": cursor})


async def test_pagination_follows_the_cursor_to_the_end():
    """The defect this guards: a loop that returns after the first page
    reports success having fetched a fraction of the history, and nothing
    in the output says so."""
    pages = [
        _page([{"trade_id": "t1"}], cursor="c1"),
        _page([{"trade_id": "t2"}], cursor="c2"),
        _page([{"trade_id": "t3"}], cursor=""),
    ]
    seen = []

    def handler(request):
        seen.append(request.url.params.get("cursor"))
        return pages[len(seen) - 1]

    text = await fetch_all_fills(
        CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
    )
    assert [f["trade_id"] for f in json.loads(text)["fills"]] == ["t1", "t2", "t3"]
    assert seen == [None, "c1", "c2"]


async def test_a_repeating_cursor_raises_instead_of_looping_forever():
    """A server that echoes the same cursor back would spin this loop until
    the process is killed. Bounded explicitly."""
    def handler(request):
        return _page([{"trade_id": "t"}], cursor="same")

    with pytest.raises(RuntimeError, match="cursor"):
        await fetch_all_fills(
            CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
        )


async def test_missing_credentials_raise_rather_than_returning_empty(monkeypatch):
    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="COINBASE_API_KEY"):
        CoinbaseCredentials.from_env()


async def test_a_rejected_key_raises_rather_than_returning_empty():
    """401 must not degrade to 'no fills found'."""
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(RuntimeError, match="401"):
        await fetch_all_fills(
            CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
        )


async def test_the_request_carries_a_bearer_token():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization", "")
        return _page([])

    await fetch_all_fills(
        CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
    )
    assert captured["auth"].startswith("Bearer ey")
