# A-2 part 2b-1: Coinbase Advanced Trade API source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Coinbase **fills** from the Advanced Trade REST API, keyed on the venue's own trade id, and close spec §10 gap 6 by making the API the only path that can ever produce a Coinbase fill.

**Architecture:** A strict I/O ⇄ purity seam, matching the rest of this codebase. `venues/coinbase_client.py` does *all* network work — ES256 JWT auth, pagination — and returns a JSON document. `importers/coinbase_api.py` is pure: JSON text → `ImportBatch`, no clock, no sockets. That seam is what keeps the three-phase parse → preview → commit discipline intact: `sync` fetches to a document, and the document then travels the *existing* preview/commit path, so nothing is written before it is seen.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, `pyjwt[crypto]` (ES256), `httpx` (async), pytest.

---

## Deviation from the spec, and why — READ FIRST

A2-16 says the Advanced Trade API "replaces the CSV importer." **That is true for fills and false for cash**, and building to it literally would silently destroy data.

`GET /orders/historical/fills` returns trade executions only. It has **no** deposits, withdrawals, transfers, rewards income, staking income or interest — every one of which `importers/coinbase.py` currently maps via `_CASH_TYPES`. Verified against Coinbase's REST endpoint index on 2026-08-08: the Advanced Trade surface has no non-trade cash endpoint at all; those live on the separate Coinbase App API v2 (`/v2/accounts/{id}/transactions`).

So the cut-over is **split by row kind**, which still fully closes gap 6:

| Row kind | Source after this plan | Dedupe key |
|---|---|---|
| Coinbase **fills** | API only | `venue_fill_id` |
| Coinbase **cash** | CSV only | `content_hash` |

Gap 6's hazard is one fill reachable by two paths keyed differently. After this plan a fill has exactly one path, so the hazard is gone — without a single line of reconciliation code, and without losing cash movements. A Coinbase App API v2 cash source is possible later (a 2b-1b), but is **not** in this plan.

**The precondition that makes this free:** the live database held 0 accounts and 0 fills when this plan was written (checked 2026-08-08). If that is no longer true when you execute this, STOP — re-run the check in Task 5 Step 1 and escalate, because a populated database changes the answer.

---

## Global Constraints

- **Purity.** Anything under `importers/` is pure: no I/O, no clock, no randomness. Network code lives under `venues/`. This is enforced by `tests/test_purity.py` — read it before adding a module.
- **`Decimal`, never `float`.** Parse JSON with `json.loads(text, parse_float=Decimal)`. A price that round-trips through `float` is a silent money bug.
- **Fail loud, never fail empty.** Missing, malformed or rejected credentials, and any non-2xx response, must raise. A sync that reports success while fetching nothing is the same failure shape as the silent-zero defect that motivated this whole effort (spec §10 gap 5).
- **Three-phase import.** parse → preview → commit. `sync` must not write anything the user has not previewed.
- **Credentials never enter the repository.** They are read from the environment only. A read-only `view` key still discloses complete position and balance history. Invoke the `public-repo-hygiene` skill before any commit.
- **Every new test is gated against a mutant before acceptance.** A mutant only proves something if it *reaches* the code under test.
- **Run the full suite yourself**, with `set -a && . ./.env && set +a && uv run pytest`, and confirm the summary says neither "skipped" nor a stale count. It takes ~190s. Never run a mutation harness while it is running — that rewrites tracked source underneath it and voids the result.

---

## File Structure

| File | Responsibility |
|---|---|
| `venues/__init__.py` | new package marker — the I/O side of the seam |
| `venues/coinbase_auth.py` | build an ES256 JWT for one request. Clock injected, so it is testable |
| `venues/coinbase_client.py` | httpx calls, pagination, error mapping. The only file that touches the network |
| `importers/coinbase_api.py` | **pure**: fills JSON → `ImportBatch` |
| `importers/registry.py` | modify: add `coinbase-api`, retire `coinbase`'s fill path |
| `importers/coinbase.py` | modify: stop emitting fills, report them instead |
| `cli.py` | modify: add `sync coinbase` |
| `pyproject.toml` | modify: add `pyjwt[crypto]`, `httpx` |
| `tests/fixtures/coinbase/api_fills.json` | synthetic API response, real shapes |
| `tests/test_coinbase_auth.py`, `tests/test_coinbase_api.py`, `tests/test_coinbase_client.py` | tests |

---

## Task 1: Dependencies and the ES256 JWT signer

**Files:**
- Modify: `pyproject.toml`
- Create: `venues/__init__.py`, `venues/coinbase_auth.py`
- Test: `tests/test_coinbase_auth.py`

**Interfaces:**
- Consumes: nothing
- Produces: `build_jwt(api_key: str, private_key_pem: str, uri: str, *, now: datetime, nonce: str) -> str`

Coinbase CDP keys authenticate with a short-lived ES256 JWT bearer token: `iss="cdp"`, `sub=<api key name>`, `nbf=now`, `exp=now+120s`, header `kid=<api key name>` and a `nonce`. `uri` is `"GET api.coinbase.com/api/v3/brokerage/orders/historical/fills"`.

`now` and `nonce` are **parameters, not calls**. A signer that reads the clock internally cannot be tested for expiry behaviour without sleeping.

- [ ] **Step 1: Add dependencies**

```toml
dependencies = [
    "asyncpg>=0.30",
    "pyjwt[crypto]>=2.9",
    "httpx>=0.27",
]
```

Run: `uv sync --extra dev`

- [ ] **Step 2: Write the failing test**

```python
# tests/test_coinbase_auth.py
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from venues.coinbase_auth import build_jwt

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
URI = "GET api.coinbase.com/api/v3/brokerage/orders/historical/fills"


def _keypair():
    """A throwaway P-256 key. Generated per-test: a private key committed to
    a public repo is a leaked credential even when it opens nothing."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM,
        format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PrivateFormat.PKCS8,
        encryption_algorithm=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).NoEncryption(),
    ).decode()
    return pem, key.public_key()


def test_jwt_carries_the_claims_coinbase_requires():
    pem, pub = _keypair()
    token = build_jwt("organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID", pem, URI, now=NOW, nonce="abc")

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID"
    assert header["nonce"] == "abc"

    claims = jwt.decode(token, pub, algorithms=["ES256"], audience=None, options={"verify_aud": False})
    assert claims["iss"] == "cdp"
    assert claims["sub"] == "organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID"
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
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_coinbase_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'venues'`

- [ ] **Step 4: Implement**

```python
# venues/coinbase_auth.py
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
```

- [ ] **Step 5: Run and confirm pass**

Run: `uv run pytest tests/test_coinbase_auth.py -v`
Expected: 3 passed

- [ ] **Step 6: Mutation gate**

Change `_EXPIRY_SECONDS = 120` to `7200`. Run the tests — `test_expiry_is_two_minutes_not_two_hours` must FAIL. Restore.
Change `raise ValueError(...)` to `return ""`. `test_a_malformed_private_key_raises_rather_than_returning_none` must FAIL. Restore.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock venues/ tests/test_coinbase_auth.py
git commit -m "feat(venues): ES256 JWT signer for Coinbase CDP keys"
```

---

## Task 2: The pure mapper — API fills JSON → ImportBatch

**Files:**
- Create: `importers/coinbase_api.py`, `tests/fixtures/coinbase/api_fills.json`
- Test: `tests/test_coinbase_api.py`

**Interfaces:**
- Consumes: `importers.base.{CanonicalFill, ImportBatch, zero_price_warning}`, `ledger.types.{AssetClass, Instrument, Side}`
- Produces: `CoinbaseAPIImporter` with `venue = "coinbase-api"` and `parse(text: str) -> ImportBatch`

The document is `{"fills": [...], "cursor": "..."}` — exactly what the client concatenates. Field names, confirmed against Coinbase's reference on 2026-08-08: `entry_id`, `trade_id`, `order_id`, `trade_time`, `trade_type`, `price`, `size`, `size_in_quote`, `commission`, `product_id`, `sequence_timestamp`, `side`, `liquidity_indicator`.

**Four traps this task exists to handle.** Each has a test:

1. **`size_in_quote`.** When true, `size` is denominated in the *quote* currency, not the base asset. Reading it as a base quantity records a catastrophically wrong position — e.g. `size: "500"` meaning $500, imported as 500 BTC. There is no safe conversion from the fill alone, so such a row **blocks**.
2. **`trade_time` vs `sequence_timestamp`.** `trade_time` is the execution time and is what `executed_at` must be. `sequence_timestamp` is the pagination/ordering clock and is what the range filters key on. Using one for the other silently shifts every fill.
3. **JSON floats.** `json.loads` turns an unquoted number into a `float`. Coinbase quotes its money fields, but a single unquoted one would slip a float into a `Decimal` pipeline. `parse_float=Decimal` removes the possibility.
4. **`side`** is `BUY`/`SELL` uppercase, unlike the CSV's lowercase vocabulary.

- [ ] **Step 1: Create the fixture**

```json
{
  "fills": [
    {
      "entry_id": "e1", "trade_id": "t1", "order_id": "o1",
      "trade_time": "2026-05-11T14:03:21.512Z",
      "sequence_timestamp": "2026-05-11T14:03:21.998Z",
      "trade_type": "FILL", "price": "61250.44", "size": "0.0125",
      "size_in_quote": false, "commission": "3.83",
      "product_id": "BTC-USD", "side": "BUY", "liquidity_indicator": "TAKER"
    },
    {
      "entry_id": "e2", "trade_id": "t2", "order_id": "o2",
      "trade_time": "2026-05-12T09:15:02.100Z",
      "sequence_timestamp": "2026-05-12T09:15:02.640Z",
      "trade_type": "FILL", "price": "2410.10", "size": "1.4",
      "size_in_quote": false, "commission": "0.00",
      "product_id": "ETH-USD", "side": "SELL", "liquidity_indicator": "MAKER"
    },
    {
      "entry_id": "e3", "trade_id": "t3", "order_id": "o3",
      "trade_time": "2026-05-13T18:40:00.000Z",
      "sequence_timestamp": "2026-05-13T18:40:00.220Z",
      "trade_type": "FILL", "price": "0.9998", "size": "500",
      "size_in_quote": true, "commission": "0.50",
      "product_id": "USDC-USD", "side": "BUY", "liquidity_indicator": "TAKER"
    }
  ],
  "cursor": ""
}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_coinbase_api.py
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.coinbase_api import CoinbaseAPIImporter
from ledger.types import AssetClass, Side

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
FIXTURE = (_FIXTURES / "coinbase" / "api_fills.json").read_text()


def batch():
    return CoinbaseAPIImporter().parse(FIXTURE)


def test_a_fill_maps_with_the_venue_trade_id_as_its_dedupe_key():
    f = batch().fills[0]
    assert f.venue_fill_id == "t1"
    assert f.venue_order_id == "o1"
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"
    assert f.instrument.asset_class is AssetClass.CRYPTO_SPOT
    assert f.side is Side.BUY
    assert f.quantity == Decimal("0.0125")
    assert f.price == Decimal("61250.44")
    assert f.fee == Decimal("3.83")


def test_executed_at_is_trade_time_not_sequence_timestamp():
    """They differ by ~half a second in the fixture, deliberately. Reading
    the wrong one shifts every fill by an amount too small to notice and
    large enough to reorder same-second trades."""
    f = batch().fills[0]
    assert f.executed_at == datetime(2026, 5, 11, 14, 3, 21, 512000, tzinfo=UTC)


def test_uppercase_side_is_understood():
    assert batch().fills[1].side is Side.SELL


def test_size_in_quote_blocks_rather_than_recording_a_wrong_quantity():
    """`size` is in QUOTE currency when this flag is set, so importing it as
    a base quantity would record 500 units of the asset instead of $500 of
    it. No safe conversion exists from the fill alone."""
    b = batch()
    assert not [f for f in b.fills if f.venue_fill_id == "t3"]
    assert [ref for ref, _ in b.blocking] == [None]
    assert "size_in_quote" in b.blocking[0][1]


def test_money_fields_never_become_floats():
    """Coinbase quotes its money fields today. If it ever stops for one of
    them, parse_float=Decimal is what keeps a float out of the pipeline."""
    unquoted = FIXTURE.replace('"price": "61250.44"', '"price": 61250.44')
    f = CoinbaseAPIImporter().parse(unquoted).fills[0]
    assert isinstance(f.price, Decimal)
    assert f.price == Decimal("61250.44")


def test_an_empty_document_is_empty_not_an_error():
    assert CoinbaseAPIImporter().parse('{"fills": [], "cursor": ""}').fills == ()
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/test_coinbase_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'importers.coinbase_api'`

- [ ] **Step 4: Implement**

```python
# importers/coinbase_api.py
"""Coinbase Advanced Trade fills JSON → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalFill, ImportBatch, zero_price_warning
from ledger.types import AssetClass, Instrument, Side

_SIDES = {"BUY": Side.BUY, "SELL": Side.SELL}


def _decimal(raw: object) -> Decimal:
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw).strip() or "0")


def _when(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


class CoinbaseAPIImporter:
    venue = "coinbase-api"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        warnings: list[str] = []
        unmapped: list[str] = []
        blocking: list[tuple[str | None, str]] = []

        def reject(raw: dict, idx: int, message: str) -> None:
            """ONE path for every dropped fill, same discipline as
            importers/fidelity.py's reject(). An API row always carries
            money -- it is a trade execution -- so unlike the CSV importers
            there is no 'no financial content' branch that only warns."""
            warnings.append(message)
            unmapped.append(str(raw))
            blocking.append((None, message))

        if not text.strip():
            return ImportBatch()

        # parse_float=Decimal: an unquoted JSON number would otherwise arrive
        # as a float and silently lose precision on the way to NUMERIC.
        document = json.loads(text, parse_float=Decimal)

        for idx, raw in enumerate(document.get("fills") or []):
            # size_in_quote flips the MEANING of `size` from base units to
            # quote currency. There is no conversion available from the fill
            # alone, and guessing produces a position wrong by the price --
            # so refuse, loudly, rather than record something plausible.
            if raw.get("size_in_quote"):
                reject(
                    raw,
                    idx,
                    f"fill {raw.get('trade_id')!r}: size_in_quote is set, so `size` is "
                    "denominated in the quote currency, not the base asset -- refusing "
                    "to record it as a quantity",
                )
                continue

            side = _SIDES.get(str(raw.get("side", "")).strip().upper())
            if side is None:
                reject(raw, idx, f"fill {raw.get('trade_id')!r}: unknown side {raw.get('side')!r}")
                continue

            product = str(raw.get("product_id") or "")
            base, _, quote = product.partition("-")
            if not base or not quote:
                reject(raw, idx, f"fill {raw.get('trade_id')!r}: unparseable product_id {product!r}")
                continue

            try:
                quantity = _decimal(raw.get("size"))
                price = _decimal(raw.get("price"))
                fee = _decimal(raw.get("commission"))
                when = _when(str(raw.get("trade_time")))
            except (InvalidOperation, ValueError) as exc:
                reject(raw, idx, f"fill {raw.get('trade_id')!r}: unparseable ({exc})")
                continue

            if not all(v.is_finite() for v in (quantity, price, fee)):
                reject(raw, idx, f"fill {raw.get('trade_id')!r}: non-finite number")
                continue
            if quantity <= 0:
                reject(raw, idx, f"fill {raw.get('trade_id')!r}: non-positive quantity")
                continue

            warn = zero_price_warning(idx, base, quantity, price)
            if warn is not None:
                warnings.append(warn)

            fills.append(
                CanonicalFill(
                    instrument=Instrument(
                        id=None,
                        asset_class=AssetClass.CRYPTO_SPOT,
                        symbol=base.upper(),
                        quote_currency=quote.upper(),
                    ),
                    executed_at=when,
                    side=side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    fee_currency=quote.upper(),
                    venue_fill_id=str(raw.get("trade_id")),
                    venue_order_id=str(raw.get("order_id")) if raw.get("order_id") else None,
                )
            )

        return ImportBatch(
            fills=tuple(fills),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            blocking=tuple(blocking),
        )
```

- [ ] **Step 5: Run and confirm pass**

Run: `uv run pytest tests/test_coinbase_api.py -v`
Expected: 6 passed

- [ ] **Step 6: Confirm purity**

Run: `uv run pytest tests/test_purity.py -v`
Expected: PASS — `importers/coinbase_api.py` imports no I/O module.

- [ ] **Step 7: Mutation gate**

- Delete the `size_in_quote` branch → `test_size_in_quote_blocks...` must FAIL.
- Change `raw.get("trade_time")` to `raw.get("sequence_timestamp")` → `test_executed_at_is_trade_time...` must FAIL.
- Remove `parse_float=Decimal` → `test_money_fields_never_become_floats` must FAIL.
- Change `blocking.append(...)` in `reject` to a no-op → `test_size_in_quote_blocks...` must FAIL.

Restore after each. **Do not run the full suite while doing this.**

- [ ] **Step 8: Commit**

```bash
git add importers/coinbase_api.py tests/test_coinbase_api.py tests/fixtures/coinbase/api_fills.json
git commit -m "feat(import): pure Coinbase Advanced Trade fills mapper"
```

---

## Task 3: The API client — pagination and loud failure

**Files:**
- Create: `venues/coinbase_client.py`
- Test: `tests/test_coinbase_client.py`

**Interfaces:**
- Consumes: `venues.coinbase_auth.build_jwt`
- Produces: `CoinbaseCredentials.from_env() -> CoinbaseCredentials`, `async fetch_all_fills(creds, *, start=None, end=None, transport=None) -> str`

`fetch_all_fills` returns the **JSON text** Task 2 parses — one merged `{"fills": [...]}` document. Returning text rather than objects is what keeps the seam honest: the client cannot accidentally hand a half-mapped structure to the pure layer.

**The failure this task exists to prevent** is spec §10 gap 5: *a sync that reports success while fetching nothing.* Three ways that happens, each with a test — absent credentials, a rejected key, and a pagination loop that stops after page one.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coinbase_client.py
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
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_coinbase_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'venues.coinbase_client'`

- [ ] **Step 3: Implement**

```python
# venues/coinbase_client.py
"""Coinbase Advanced Trade REST access. The ONLY module here that opens a socket."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from venues.coinbase_auth import build_jwt

_HOST = "api.coinbase.com"
_PATH = "/api/v3/brokerage/orders/historical/fills"
_LIMIT = 100
# A page cap, not a history cap: 1000 pages x 100 fills is far beyond any
# personal account, and turns a server-side pagination bug into a loud
# failure instead of an unbounded loop.
_MAX_PAGES = 1000


@dataclass(frozen=True, slots=True)
class CoinbaseCredentials:
    api_key: str
    private_key_pem: str

    @classmethod
    def from_env(cls) -> CoinbaseCredentials:
        """Raise, never default. A missing key must not degrade into an
        unauthenticated request that returns an empty result set -- see
        spec §10 gap 5."""
        key = os.environ.get("COINBASE_API_KEY")
        secret = os.environ.get("COINBASE_API_SECRET")
        missing = [n for n, v in (("COINBASE_API_KEY", key), ("COINBASE_API_SECRET", secret)) if not v]
        if missing:
            raise RuntimeError(
                f"Coinbase credentials absent from the environment: {', '.join(missing)}. "
                "A read-only 'view' key still discloses full position history -- it belongs "
                "in the deployment environment, never in this repository."
            )
        return cls(api_key=key, private_key_pem=secret)


async def fetch_all_fills(
    creds: CoinbaseCredentials,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Every fill, following the cursor to exhaustion. Returns JSON text for
    the pure mapper in importers/coinbase_api.py."""
    collected: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    async with httpx.AsyncClient(transport=transport, base_url=f"https://{_HOST}", timeout=30) as c:
        for _ in range(_MAX_PAGES):
            params: dict[str, object] = {"limit": _LIMIT}
            if cursor:
                params["cursor"] = cursor
            if start:
                params["start_sequence_timestamp"] = start.astimezone(UTC).isoformat()
            if end:
                params["end_sequence_timestamp"] = end.astimezone(UTC).isoformat()

            token = build_jwt(
                creds.api_key,
                creds.private_key_pem,
                f"GET {_HOST}{_PATH}",
                now=datetime.now(UTC),
                nonce=secrets.token_hex(16),
            )
            r = await c.get(_PATH, params=params, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                raise RuntimeError(
                    f"Coinbase fills request failed with {r.status_code}: {r.text[:200]}"
                )

            body = r.json()
            collected.extend(body.get("fills") or [])
            cursor = body.get("cursor") or ""
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RuntimeError(
                    f"Coinbase returned a repeating pagination cursor ({cursor!r}); "
                    "refusing to loop"
                )
            seen_cursors.add(cursor)
        else:
            raise RuntimeError(f"Coinbase pagination exceeded {_MAX_PAGES} pages; refusing to continue")

    return json.dumps({"fills": collected, "cursor": ""})
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_coinbase_client.py -v`
Expected: 5 passed

- [ ] **Step 5: Mutation gate**

- Replace the pagination loop body's `if not cursor: break` with an unconditional `break` → `test_pagination_follows_the_cursor_to_the_end` must FAIL.
- Change the `!= 200` check to `>= 500` → `test_a_rejected_key_raises...` must FAIL.
- Make `from_env` return `cls("", "")` when a var is missing → `test_missing_credentials_raise...` must FAIL.
- Delete the `seen_cursors` check → `test_a_repeating_cursor_raises...` must FAIL (it will hit `_MAX_PAGES` and still raise, so **verify the message matches "cursor"**, not merely that it raised — if it passes on the `_MAX_PAGES` message, tighten the test).

- [ ] **Step 6: Commit**

```bash
git add venues/coinbase_client.py tests/test_coinbase_client.py
git commit -m "feat(venues): paginated Coinbase fills client that fails loudly"
```

---

## Task 4: The cut-over — Coinbase CSV stops producing fills

**Files:**
- Modify: `importers/coinbase.py`, `importers/registry.py`
- Test: `tests/test_coinbase.py` (modify), `tests/test_registry.py`

**Interfaces:**
- Consumes: Task 2's `CoinbaseAPIImporter`
- Produces: registry entry `"coinbase-api"`; `CoinbaseImporter` that maps cash only

This is spec §10 gap 6, closed. After this task no Coinbase fill can arrive by two paths, so the two dedupe keys can never meet on one row.

**The CSV importer keeps its cash mapping** — the API has no deposits, withdrawals, rewards or staking income (see the deviation note at the top). It stops emitting *fills*, and **says so**: a silently-ignored trade row would be the same silent-loss shape in a new costume. Trade rows become recognised-and-reported, never dropped without a word.

- [ ] **Step 1: Write the failing tests**

```python
def test_coinbase_csv_no_longer_produces_fills():
    """§10 gap 6, closed: fills come only from the API, so the two dedupe
    keys can never meet on one row."""
    result = CoinbaseImporter().parse(FIXTURE)
    assert result.fills == ()


def test_coinbase_csv_still_produces_cash():
    """The API has NO deposits, withdrawals, rewards or staking income.
    Retiring the CSV path wholesale would have silently destroyed every
    Coinbase cash movement."""
    kinds = {c.kind for c in CoinbaseImporter().parse(FIXTURE).cash}
    assert "deposit" in kinds


def test_ignored_trade_rows_are_reported_not_silently_dropped():
    """A trade row the CSV now declines to map must be visible. Dropping it
    without a word is the silent-loss shape this project keeps rediscovering."""
    result = CoinbaseImporter().parse(FIXTURE)
    assert any("coinbase-api" in w for w in result.warnings)
    assert result.blocking == ()   # reported, but must not block a cash-only import


def test_registry_exposes_the_api_importer():
    from importers.registry import get_importer, list_importers

    assert "coinbase-api" in list_importers()
    assert get_importer("coinbase-api").venue == "coinbase-api"
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_coinbase.py tests/test_registry.py -v`
Expected: FAIL — the CSV importer still returns fills.

- [ ] **Step 3: Implement**

In `importers/coinbase.py`, replace the `if kind in _FILL_TYPES:` body with a report-and-skip, keeping every cash branch untouched:

```python
            if kind in _FILL_TYPES:
                # §10 gap 6, closed 2026-08-08: Coinbase fills are imported
                # from the Advanced Trade API (`deadband sync coinbase`),
                # keyed on the venue's own trade id. Mapping them here too
                # would give one fill two dedupe keys -- content_hash from
                # this path, venue_fill_id from that one -- so a fill
                # imported by both would not dedupe against itself.
                #
                # Reported, never silently skipped: a trade row vanishing
                # without a word is the same silent-loss shape as the defect
                # that started this effort. It does NOT block, because a
                # cash-only Coinbase CSV import is now the intended use.
                warnings.append(
                    f"line {line_no}: {kind!r} is a trade row -- Coinbase fills are "
                    "imported via `deadband sync coinbase` (coinbase-api), not from CSV"
                )
                continue
```

In `importers/registry.py`:

```python
from importers.coinbase_api import CoinbaseAPIImporter

_IMPORTERS: dict[str, Importer] = {
    "coinbase": CoinbaseImporter(),          # cash movements only, see gap 6
    "coinbase-api": CoinbaseAPIImporter(),   # fills
    "fidelity": FidelityImporter(),
}
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_coinbase.py tests/test_registry.py -v`
Expected: PASS. Some existing Coinbase fill tests will now be asserting retired behaviour — **read each one before changing it.** A test that asserted the CSV maps a buy to a fill should be rewritten to assert it is reported, not deleted.

- [ ] **Step 5: Mutation gate**

- Restore the fill-building branch → `test_coinbase_csv_no_longer_produces_fills` must FAIL.
- Remove the `warnings.append` → `test_ignored_trade_rows_are_reported...` must FAIL.
- Delete the cash branches → `test_coinbase_csv_still_produces_cash` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add importers/coinbase.py importers/registry.py tests/test_coinbase.py tests/test_registry.py
git commit -m "feat(import): Coinbase fills come only from the API (closes gap 6)"
```

---

## Task 5: `deadband sync coinbase` — fetch, preview, commit

**Files:**
- Modify: `cli.py`
- Test: `tests/test_cli_sync.py`

**Interfaces:**
- Consumes: `venues.coinbase_client.{CoinbaseCredentials, fetch_all_fills}`, `importers.registry.get_importer`
- Produces: `cmd_sync(args)`

`sync` fetches to a JSON document and then travels the **existing** preview/commit path — the same `route_batch`, the same blocking rules, the same `--commit` gate. It must not grow a second, parallel write path.

- [ ] **Step 1: Re-verify the cut-over precondition**

```bash
set -a && . ./.env && set +a && uv run python - <<'PY'
import asyncio, os, asyncpg
async def main():
    c = await asyncpg.connect(os.environ["PG_DSN"], timeout=10)
    n = await c.fetchval("""SELECT count(*) FROM fill f JOIN account a ON a.id=f.account_id
                            WHERE a.venue='coinbase' AND f.content_hash IS NOT NULL""")
    print("CSV-imported Coinbase fills in the live DB:", n)
    await c.close()
asyncio.run(main())
PY
```

Expected: `0`. **If it is not 0, STOP and escalate** — the clean cut-over assumed an empty database, and a populated one needs the reconciliation this plan deliberately does not contain.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_cli_sync.py
import json

import pytest


async def test_sync_without_commit_writes_nothing(monkeypatch, capsys):
    """Three-phase discipline: fetch and preview must not touch the DB."""
    import cli

    async def fake_fetch(creds, **kw):
        return json.dumps({"fills": [], "cursor": ""})

    monkeypatch.setattr(cli, "fetch_all_fills", fake_fetch)
    monkeypatch.setattr(cli, "_connect", lambda *a, **k: pytest.fail("sync previewed but opened a DB connection"))
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    monkeypatch.setenv("COINBASE_API_SECRET", "pem")

    await cli.cmd_sync(_args(venue="coinbase", commit=False))
    assert "preview" in capsys.readouterr().out.lower()


async def test_sync_reports_absent_credentials_as_an_error_not_zero_fills(monkeypatch, capsys):
    import cli

    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        await cli.cmd_sync(_args(venue="coinbase", commit=False))
    assert "COINBASE_API_KEY" in capsys.readouterr().err
```

(`_args` is a small namespace helper — copy the pattern already used in `tests/test_cli.py`.)

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_cli_sync.py -v`
Expected: FAIL — `cli` has no attribute `cmd_sync`.

- [ ] **Step 4: Implement**

Add to `cli.py`, reusing `cmd_import`'s preview/commit body rather than duplicating it:

```python
    p_sync = sub.add_parser("sync", help="fetch from a venue API and import")
    p_sync.add_argument("venue", choices=["coinbase"])
    p_sync.add_argument("--account", required=True,
                        help="account UUID: the API carries no per-row account ref")
    p_sync.add_argument("--start", help="ISO-8601 lower bound on sequence_timestamp")
    p_sync.add_argument("--end", help="ISO-8601 upper bound on sequence_timestamp")
    p_sync.add_argument("--commit", action="store_true", help="write to the database")
    p_sync.set_defaults(fn=cmd_sync)
```

`cmd_sync` builds credentials (letting `RuntimeError` reach the existing top-level handler that prints to stderr and exits non-zero), calls `fetch_all_fills`, hands the text to `get_importer("coinbase-api").parse(...)`, and then calls the *same* preview/commit helper `cmd_import` uses.

- [ ] **Step 5: Run and confirm pass**

Run: `uv run pytest tests/test_cli_sync.py -v`

- [ ] **Step 6: Mutation gate**

- Make `cmd_sync` swallow the credentials `RuntimeError` and print "0 fills" → `test_sync_reports_absent_credentials...` must FAIL.
- Make preview open a DB connection → `test_sync_without_commit_writes_nothing` must FAIL.

- [ ] **Step 7: Full suite, then commit**

```bash
set -a && . ./.env && set +a && uv run pytest -q
```
Expected: all pass, and the summary line must not say "skipped".

```bash
git add cli.py tests/test_cli_sync.py
git commit -m "feat(cli): deadband sync coinbase, through the existing preview path"
```

---

## Task 6: Live verification, and settling `trade_id` vs `entry_id`

**Files:** none committed. This task produces a finding, not code.

**This is the task part 2a did not have, and its absence is why a branch passed six reviews while not importing the owner's real export.** Fixtures prove the mapper self-consistent; only a real response proves it right.

Requires a Coinbase CDP key with `view` scope, exported as `COINBASE_API_KEY` / `COINBASE_API_SECRET` in the shell only — never written to a file in the repo.

- [ ] **Step 1: Preview against the real account**

```bash
deadband sync coinbase --account <uuid>
```
Expected: a preview listing fills, no database writes.

- [ ] **Step 2: Settle the dedupe key empirically**

The spec says "the API supplies a venue trade id" and Task 2 uses `trade_id`. The response *also* carries `entry_id`, and **which of the two is unique per fill is not stated in the documentation.** If `trade_id` repeats — one trade producing two entries — then `fill_venue_id_uniq` would silently collapse two real fills into one, losing money.

```bash
# with the fetched document saved to /root/scratch/fills.json (NOT in the repo)
python3 - <<'PY'
import json, collections
d = json.load(open("/root/scratch/fills.json"))
f = d["fills"]
print("fills:", len(f))
for k in ("trade_id", "entry_id"):
    print(k, "distinct:", len({x.get(k) for x in f}))
PY
```

If `trade_id` distinct < fills, **change Task 2 to use `entry_id`** and add a regression test with a real-shaped duplicate-`trade_id` pair. Record the finding in `docs/known-gaps.md` either way — "we checked and it is unique" is worth as much as the fix.

- [ ] **Step 3: Verify the count against Coinbase's UI**

Compare the previewed fill count and a spot-checked handful of prices against the Coinbase web UI. A mapper can be internally consistent and still wrong about which field is the price.

- [ ] **Step 4: Record findings**

Add a `docs/known-gaps.md` entry under a new "Found by the first real Coinbase sync" heading. **Shapes, never specimens** — no amounts, no balances, no product mix. Reproduction cases go in `docs/ops/`, which is gitignored.

- [ ] **Step 5: Commit the findings only**

```bash
git add docs/known-gaps.md
git commit -m "docs: findings from the first real Coinbase API sync"
```

---

## Task 7: Document the operational dependency

**Files:**
- Modify: `README.md`, `docs/known-gaps.md`

- [ ] **Step 1: README** — document `COINBASE_API_KEY` / `COINBASE_API_SECRET`, that the key needs `view` scope only, and that `sync` refuses rather than returning zero fills when they are absent.

- [ ] **Step 2: known-gaps** — mark §10 gap 6 **closed** with the split-by-row-kind reasoning, and mark gap 5 (credentials as an operational dependency) **live**. Add a new gap: *Coinbase non-trade cash still requires a CSV export, because the Advanced Trade API has no endpoint for it; a Coinbase App API v2 transactions source would close it.*

- [ ] **Step 3: Commit**

```bash
git add README.md docs/known-gaps.md
git commit -m "docs: Coinbase API credentials and the remaining cash-import gap"
```

---

## Self-Review

**Spec coverage.** A2-16 (Advanced Trade fills source) → Tasks 1–3, 5. §10 gap 6 (cut-over) → Task 4, precondition re-checked in Task 5 Step 1. §10 gap 5 (credentials, fail-loud) → Task 3, Task 7. §9's "gated against a mutant" → a mutation step in every code task. **Deliberately not covered:** A2-16's claim that the API replaces the CSV importer wholesale — contradicted by the endpoint surface, see the deviation note, and the residue is recorded as a new gap in Task 7.

**Placeholders.** None: every code step carries the actual code, every test step the actual test.

**Type consistency.** `build_jwt` (Task 1) is called with exactly its signature in Task 3. `CoinbaseAPIImporter.parse(text) -> ImportBatch` (Task 2) matches the `Importer` Protocol and is what Task 4 registers and Task 5 calls. `fetch_all_fills` returns `str`, which is what `parse` consumes.

**Known soft spot.** Task 2 keys on `trade_id` because the spec says so; Task 6 Step 2 is what actually settles it. If Task 6 cannot run for lack of credentials, that soft spot ships unverified — and it is a money-losing one, so say so in the PR rather than letting it pass as done.
