import json
import os

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
    the process is killed. Bounded explicitly.

    Matching the exception message on "cursor" alone only distinguishes this
    guard from the separate `_MAX_PAGES` backstop because the two messages
    happen not to share that word today. Reword either message and a
    text-only version of this test would start passing for the wrong
    reason -- meaning the `seen_cursors` guard could be deleted (falling
    through to 1000 iterations of the `_MAX_PAGES` path instead) without
    the test noticing. The call-count assertion below is structural rather
    than textual: with the guard intact the handler is called exactly
    twice (page 1 establishes the cursor, page 2 repeats it and trips the
    check); with the guard deleted the handler is called 1000 times before
    `_MAX_PAGES` gives up. `calls == 2` fails either way the guard is
    removed, independent of what either exception message says.
    """
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return _page([{"trade_id": "t"}], cursor="same")

    with pytest.raises(RuntimeError, match="cursor"):
        await fetch_all_fills(
            CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
        )
    assert calls == 2


async def test_a_malformed_200_page_raises_rather_than_being_treated_as_empty():
    """`body.get("fills") or []` would fold a 200 response with no `fills`
    key at all -- e.g. an API contract change, or a proxy/error page
    returned with a 200 status -- into 'an empty page'; if `cursor` is also
    absent (as it is here), the loop would end and the function would
    return a document that looks like a complete, successful fetch but is
    actually partial. That is exactly the gap-5 failure shape this whole
    task exists to prevent, and it happens silently unless the response
    shape is checked, not merely its truthiness. Page 1 is well-formed so
    this exercises the check kicking in mid-pagination, not just on the
    first request."""
    def handler(request):
        if request.url.params.get("cursor") is None:
            return _page([{"trade_id": "t1"}], cursor="c1")
        return httpx.Response(200, json={"no_fills_key_at_all": True})

    with pytest.raises(RuntimeError, match="fills"):
        await fetch_all_fills(
            CoinbaseCredentials.from_env(), transport=httpx.MockTransport(handler)
        )


def test_credentials_repr_never_prints_the_private_key():
    """A stray `print(creds)`, an unhandled exception with locals captured
    by a debugger, or a crash reporter serializing frame variables would
    otherwise leak a Coinbase private key verbatim -- and this repo is
    PUBLIC. Checked against the actual PEM value the fixture generated
    (not merely the literal words "PRIVATE KEY"), so the test still catches
    a mutant that changes the PEM's own header/footer text or reformats it,
    as long as the raw value itself makes it into repr()."""
    creds = CoinbaseCredentials.from_env()
    rendered = repr(creds)
    assert os.environ["COINBASE_API_SECRET"] not in rendered
    assert "PRIVATE KEY" not in rendered


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
