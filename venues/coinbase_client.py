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
        missing = [
            n for n, v in (("COINBASE_API_KEY", key), ("COINBASE_API_SECRET", secret)) if not v
        ]
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

    async with httpx.AsyncClient(
        transport=transport, base_url=f"https://{_HOST}", timeout=30
    ) as c:
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
            raise RuntimeError(
                f"Coinbase pagination exceeded {_MAX_PAGES} pages; refusing to continue"
            )

    return json.dumps({"fills": collected, "cursor": ""})
