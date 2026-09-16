# Quote Source for Marks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch delayed quotes for the instruments the ledger holds and pre-fill the marks screen with them, so recording marks stops being fifteen hand-typed prices.

**Architecture:** A new `marketdata/` package holding subsystem D's first slice — a `Quote`, a `QuoteSource` protocol, one yfinance-backed implementation, and a pure ledger-instrument→provider-symbol mapper. A read-only `GET /api/quotes` proposes prices; the existing `POST /api/marks` remains the only write path, gaining an optional per-mark `source` so a fetched price records its provider instead of masquerading as manual.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, `yfinance`; React 19 + TypeScript on the front end.

**Spec:** `docs/superpowers/specs/2026-08-05-market-data-screeners-design.md` — this plan builds the smallest useful piece of it (D5's `QuoteSource`, and "Marks published into A"). Everything else in that spec — bars, calendars, news, screeners, the pre-trade gate, the fallback chain, caching/TTL config — is explicitly out of scope here.

## Global Constraints

- **Money is `Decimal`, never `float`.** Providers return binary floats (Yahoo gave `0.5299999713897705` and `4.440000057220459` when probed on 2026-09-16). Convert at the boundary with `Decimal(str(value))` and quantize; a float must never reach a NUMERIC column or a JSON response.
- **Money crosses the API as a string,** end to end, as everywhere else in this codebase.
- **Symbol mapping never guesses.** An instrument that cannot be mapped returns a reason, not a best-effort ticker. A wrong symbol silently returning a different security's price is the worst failure available here.
- **Provider tests use recorded fixtures, never live endpoints** (spec §9). No test may make a network call.
- **`GET /api/quotes` never writes.** Read pool only. The write path stays `POST /api/marks` with its existing `require_trusted_identity` declared BEFORE `get_write_conn`.
- **Every pytest command needs `--env-file .env`;** a bare run silently skips the DB and API lanes.
- **Mutation-test every load-bearing guard, and commit first.** `git checkout <file>` to undo a mutation reverts everything uncommitted in that file.
- `uv run ruff check .` and `pnpm exec oxlint --max-warnings 0` must both pass — CI gates on them since PR #42. Check exit codes directly; piping to `tail` returns 0 on a red run.

---

### Task 1: `Quote` and the `QuoteSource` protocol

**Files:**
- Create: `marketdata/__init__.py`, `marketdata/types.py`, `marketdata/base.py`
- Test: `tests/test_marketdata_types.py`

**Interfaces:**
- Produces: `Quote(symbol: str, price: Decimal, currency: str, quoted_at: datetime | None, source: str)`; `QuoteSource` protocol with `quote(symbols: Sequence[str]) -> dict[str, Quote]`; `quantize_price(value: float | str | Decimal) -> Decimal`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from marketdata.types import Quote, quantize_price


def test_a_provider_float_is_quantized_not_stored_raw():
    """Yahoo returns binary floats: 0.53 arrives as 0.5299999713897705.
    Decimal(float) would preserve every one of those digits into a NUMERIC
    column, so the conversion goes through str() and then quantizes."""
    assert quantize_price(0.5299999713897705) == Decimal("0.5300")
    assert quantize_price(4.440000057220459) == Decimal("4.4400")


def test_quantization_keeps_sub_cent_precision():
    """Sub-penny prices are real -- DEMCF quoted at 0.0795 on 2026-09-16.
    Rounding to 2dp would have turned it into 0.08, a 0.6% error on a
    position, so the scale is 4 rather than 2."""
    assert quantize_price(0.0795) == Decimal("0.0795")


def test_a_negative_quote_is_refused():
    """mark_price_chk is `price >= 0`. A negative quote is a provider bug or
    a mis-parse, and it must not reach the API as a proposal."""
    with pytest.raises(ValueError, match="negative"):
        quantize_price(-1.0)


def test_quote_carries_its_source_and_time():
    q = Quote(
        symbol="GME",
        price=Decimal("21.44"),
        currency="USD",
        quoted_at=datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
        source="yahoo",
    )
    assert q.source == "yahoo"
    assert q.quoted_at.tzinfo is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run --env-file .env pytest tests/test_marketdata_types.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'marketdata'`

- [ ] **Step 3: Implement**

`marketdata/types.py`:

```python
"""Quotes, and the one conversion every provider must go through.

Spec D7: every mark carries its source, so a delayed third-party price is
never mistaken for a broker-confirmed one. `Quote` is the shape that carries
that provenance out of a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

# Four decimal places. Two is wrong: DEMCF quoted at 0.0795 when this was
# written, and rounding that to 0.08 is a 0.6% error on the whole position.
# More than four is provider noise, not precision -- Yahoo's own display
# rounds well before that.
_SCALE = Decimal("0.0001")


def quantize_price(value: float | str | Decimal) -> Decimal:
    """A provider's price as a Decimal the ledger can store.

    Goes through str() deliberately. Decimal(0.53) is
    0.5299999999999999822364316059974953532218933105468750 -- the binary
    float's true value -- whereas Decimal(str(0.53)) is Decimal("0.53").
    Yahoo returns exactly this shape: 0.5299999713897705 for a 53-cent
    option.
    """
    d = Decimal(str(value))
    if d < 0:
        raise ValueError(f"refusing a negative quote: {value!r}")
    if not d.is_finite():
        raise ValueError(f"refusing a non-finite quote: {value!r}")
    return d.quantize(_SCALE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    currency: str
    # None when the provider does not say. Never defaulted to now(): "I do
    # not know how old this is" and "this is current" are different claims,
    # and the screen shows the difference so a stale quote can be rejected.
    quoted_at: datetime | None
    source: str
```

`marketdata/base.py`:

```python
"""The provider seam (spec D5).

One implementation today (yahoo). The protocol exists so that swapping or
adding a provider is a contained change -- yfinance scrapes an undocumented
endpoint and will break, the same way yt-dlp does, and that must not become a
rewrite when it happens.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from marketdata.types import Quote


class QuoteSource(Protocol):
    name: str

    def quote(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Quotes by symbol. A symbol the provider cannot answer for is
        OMITTED from the result rather than given a placeholder -- the caller
        distinguishes "no quote" from "a quote of zero", which is a legal
        mark for an expired option."""
        ...
```

- [ ] **Step 4: Run the tests** — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add marketdata/ tests/test_marketdata_types.py
git commit -m "Add the Quote type and the QuoteSource seam"
```

---

### Task 2: ledger instrument → provider symbol

**Files:**
- Create: `marketdata/symbols.py`
- Test: `tests/test_marketdata_symbols.py`

**Interfaces:**
- Consumes: `ledger.types.AssetClass`.
- Produces: `provider_symbol(*, asset_class, symbol, underlying, expiry, strike, option_right) -> str`, raising `UnmappableInstrument(reason)`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from decimal import Decimal

import pytest

from marketdata.symbols import UnmappableInstrument, provider_symbol


def _equity(symbol):
    return dict(asset_class="equity", symbol=symbol, underlying=None,
                expiry=None, strike=None, option_right=None)


def test_an_equity_maps_to_its_own_ticker():
    assert provider_symbol(**_equity("GME")) == "GME"


def test_an_otc_ticker_maps_unchanged():
    """DEMCF (OTC) and NPSCY (Pink Sheets) both resolved on Yahoo when probed
    2026-09-16 -- no suffix, no exchange prefix."""
    assert provider_symbol(**_equity("DEMCF")) == "DEMCF"


def test_an_option_is_built_into_occ_form():
    """The ledger stores Fidelity's spelling (-PYPL280121C100). Yahoo wants
    OCC: root + YYMMDD + C/P + strike*1000 zero-padded to 8. Verified live on
    2026-09-16: PYPL280121C00100000 quoted 0.53."""
    assert provider_symbol(
        asset_class="option", symbol="-PYPL280121C100", underlying="PYPL",
        expiry=date(2028, 1, 21), strike=Decimal("100"), option_right="call",
    ) == "PYPL280121C00100000"


def test_a_fractional_strike_survives_the_occ_encoding():
    """Strike 12.50 is 00012500, not 00012.50 or 0001250. An off-by-one here
    names a different contract that may well exist and quote."""
    assert provider_symbol(
        asset_class="option", symbol="x", underlying="AAPL",
        expiry=date(2026, 6, 19), strike=Decimal("12.50"), option_right="put",
    ) == "AAPL260619P00012500"


def test_a_blank_symbol_is_unmappable_with_a_reason():
    with pytest.raises(UnmappableInstrument, match="no symbol"):
        provider_symbol(**_equity(""))


def test_an_option_missing_its_parts_is_unmappable_not_guessed():
    """Rather than fall back to the Fidelity spelling, which is not a Yahoo
    symbol and could collide with an unrelated ticker."""
    with pytest.raises(UnmappableInstrument, match="option"):
        provider_symbol(asset_class="option", symbol="-PYPL280121C100",
                        underlying=None, expiry=None, strike=None, option_right=None)


def test_an_unsupported_asset_class_is_unmappable():
    with pytest.raises(UnmappableInstrument, match="crypto_perp"):
        provider_symbol(asset_class="crypto_perp", symbol="BTC-PERP",
                        underlying=None, expiry=None, strike=None, option_right=None)
```

- [ ] **Step 2: Run it and watch it fail** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `marketdata/symbols.py`**

```python
"""Ledger instrument -> provider symbol.

Pure, and deliberately strict. A symbol this cannot construct raises rather
than falling back to the ledger's own spelling: Fidelity writes an option as
-PYPL280121C100, which is not a Yahoo symbol, and a near-miss that happens to
resolve returns some other security's price into a P&L figure. Refusing is
visible; guessing is not.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


class UnmappableInstrument(Exception):
    """Carries the reason, which reaches the screen so the row can be typed
    in by hand instead of silently going unpriced."""


def provider_symbol(
    *,
    asset_class: str,
    symbol: str,
    underlying: str | None,
    expiry: date | None,
    strike: Decimal | None,
    option_right: str | None,
) -> str:
    if asset_class == "equity":
        if not symbol.strip():
            raise UnmappableInstrument("no symbol recorded for this instrument")
        return symbol.strip().upper()

    if asset_class == "option":
        if not (underlying and expiry and strike is not None and option_right):
            raise UnmappableInstrument(
                "option is missing the underlying, expiry, strike or right "
                "needed to build a contract symbol"
            )
        # OCC: ROOT + YYMMDD + C|P + strike*1000, zero-padded to 8 digits.
        thousandths = int((strike * 1000).to_integral_value())
        return (
            f"{underlying.strip().upper()}"
            f"{expiry:%y%m%d}"
            f"{'C' if option_right == 'call' else 'P'}"
            f"{thousandths:08d}"
        )

    raise UnmappableInstrument(f"{asset_class} instruments are not quoted by this provider")
```

- [ ] **Step 4: Run the tests** — Expected: PASS

- [ ] **Step 5: Commit**

---

### Task 3: the yfinance provider, tested against fixtures

**Files:**
- Create: `marketdata/yahoo.py`
- Test: `tests/test_marketdata_yahoo.py`
- Modify: `pyproject.toml` (add `yfinance` to `dependencies`)

**Interfaces:**
- Consumes: `Quote`, `quantize_price`.
- Produces: `YahooQuotes(fetch=...)` — a `QuoteSource` whose network call is injected so tests never touch the network.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

from marketdata.yahoo import YahooQuotes

# Shapes recorded from a real yfinance fast_info on 2026-09-16, including the
# float noise, which is the entire reason quantize_price exists.
_RECORDED = {
    "GME": {"lastPrice": 21.44, "currency": "USD"},
    "DEMCF": {"lastPrice": 0.0795, "currency": "USD"},
    "NPSCY": {"lastPrice": 4.440000057220459, "currency": "USD"},
    "PYPL280121C00100000": {"lastPrice": 0.5299999713897705, "currency": "USD"},
}


def test_quotes_come_back_as_quantized_decimals():
    src = YahooQuotes(fetch=lambda syms: {s: _RECORDED[s] for s in syms if s in _RECORDED})
    out = src.quote(["GME", "NPSCY"])
    assert out["GME"].price == Decimal("21.4400")
    assert out["NPSCY"].price == Decimal("4.4400")
    assert isinstance(out["GME"].price, Decimal)


def test_an_unknown_symbol_is_omitted_not_zeroed():
    """GMEWS -- a warrant -- returned "Quote not found" on 2026-09-16. A zero
    here would be recorded as a legal mark meaning "worth nothing", which is
    a false statement about a position, not a missing one."""
    src = YahooQuotes(fetch=lambda syms: {s: _RECORDED[s] for s in syms if s in _RECORDED})
    out = src.quote(["GME", "GMEWS"])
    assert "GMEWS" not in out
    assert "GME" in out


def test_one_bad_row_does_not_lose_the_rest():
    """A provider that returns a malformed entry for one symbol must not cost
    the other fourteen their quotes."""
    def fetch(_syms):
        return {"GME": {"lastPrice": 21.44, "currency": "USD"},
                "DEMCF": {"lastPrice": None, "currency": "USD"}}
    out = YahooQuotes(fetch=fetch).quote(["GME", "DEMCF"])
    assert set(out) == {"GME"}


def test_a_negative_price_is_dropped_rather_than_proposed():
    def fetch(_syms):
        return {"GME": {"lastPrice": -1.0, "currency": "USD"}}
    assert YahooQuotes(fetch=fetch).quote(["GME"]) == {}


def test_the_source_name_is_the_provider():
    src = YahooQuotes(fetch=lambda syms: {"GME": _RECORDED["GME"]})
    assert src.quote(["GME"])["GME"].source == "yahoo"
```

- [ ] **Step 2: Run it and watch it fail** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `marketdata/yahoo.py`**

The module-level `_live_fetch` is the only code that imports `yfinance` or
touches the network, and it is injected so no test reaches it.

```python
"""Quotes from Yahoo via yfinance.

UNOFFICIAL. yfinance scrapes endpoints Yahoo does not document or support, so
it breaks periodically and needs upgrading -- the same class of dependency as
yt-dlp. That is what QuoteSource is for: replacing this is a new file, not a
rewrite.

The network call is a constructor argument. Nothing in the test suite may
reach Yahoo (spec section 9: recorded fixtures, never live endpoints).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from marketdata.types import Quote, quantize_price

FetchFn = Callable[[Sequence[str]], dict[str, dict]]


def _live_fetch(symbols: Sequence[str]) -> dict[str, dict]:
    import yfinance  # imported here so the package is not needed to import this module

    out: dict[str, dict] = {}
    tickers = yfinance.Tickers(" ".join(symbols))
    for s in symbols:
        try:
            fi = tickers.tickers[s].fast_info
            out[s] = {"lastPrice": fi.get("lastPrice"), "currency": fi.get("currency")}
        except Exception:
            # Per-symbol: a warrant with no Yahoo listing raises rather than
            # returning empty, and must not cost the other symbols their
            # quotes. The omission is reported upstream by the caller.
            continue
    return out


class YahooQuotes:
    name = "yahoo"

    def __init__(self, fetch: FetchFn = _live_fetch) -> None:
        self._fetch = fetch

    def quote(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raw = self._fetch(list(symbols))
        quotes: dict[str, Quote] = {}
        for symbol, row in raw.items():
            price = row.get("lastPrice")
            if price is None:
                continue
            try:
                quotes[symbol] = Quote(
                    symbol=symbol,
                    price=quantize_price(price),
                    currency=(row.get("currency") or "USD").upper(),
                    quoted_at=None,
                    source=self.name,
                )
            except (ValueError, ArithmeticError):
                # A negative or non-finite price is a provider fault. Dropping
                # it leaves the row hand-enterable; proposing it would put a
                # number the database would refuse in front of the user.
                continue
        return quotes
```

- [ ] **Step 4: Run the tests** — Expected: PASS

- [ ] **Step 5: Add the dependency and re-lock**

```bash
uv add yfinance
uv run --env-file .env pytest tests/test_marketdata_yahoo.py -q
```

- [ ] **Step 6: Commit**

---

### Task 4: `GET /api/quotes`

**Files:**
- Create: `api/quotes.py`
- Modify: `api/app.py` (include the router — a READ route, registered unconditionally)
- Test: `tests/api/test_quotes.py`

**Interfaces:**
- Consumes: `db.positions.open_positions`, `provider_symbol`, a `QuoteSource`.
- Produces: `GET /api/quotes` → `{"quotes": [{instrument_id, symbol, provider_symbol, price, currency, source}], "unquoted": [{instrument_id, symbol, reason}], "generated_at": ...}`.

- [ ] **Step 1: Write the failing tests**

Cover: the markable set matches `GET /api/marks`; an unmappable instrument
appears under `unquoted` **with its reason** rather than vanishing; a symbol
the provider omits also appears under `unquoted`; prices are JSON **strings**;
the route needs no identity and is absent from the write-route list.

- [ ] **Step 2: Run and watch fail**

- [ ] **Step 3: Implement.** The provider is an app-state dependency so the
test injects a fake — no network in tests. Mirror `api/marks.py`'s dedupe
exactly (by `instrument_id`, not by `(account_id, instrument_id)`), and reuse
its `unvaluable_reason is None` filter, so the two endpoints cannot drift into
proposing a price for a row the marks table does not show.

- [ ] **Step 4: Run** — Expected: PASS

- [ ] **Step 5: Mutation-test the "never writes" property** — delete the
`unvaluable_reason` filter and confirm a test goes red; confirm
`tests/api/test_write_identity.py`'s structural route walk still reports no
new write route.

- [ ] **Step 6: Commit**

---

### Task 5: `POST /api/marks` records a mark's source

**Files:**
- Modify: `api/marks.py` (`MarkIn` gains `source: str | None = None`; pass it to `set_mark`)
- Test: `tests/api/test_marks_write.py`

**Interfaces:**
- Consumes: `db.marks.set_mark(..., source=...)`, which already takes it.

- [ ] **Step 1: Write the failing tests**

Cover: omitting `source` still stores `"manual"` (the existing default, so no
caller changes); `"yahoo"` is stored as given; an unknown source string is
**accepted** (it is provenance, not an enum — a future provider must not
require a migration); an empty or whitespace `source` is refused with a 422
rather than silently stored as a blank provenance.

- [ ] **Step 2: Run and watch fail**

- [ ] **Step 3: Implement** — validate in `api/validation.py` alongside the
other parsers so the API cannot drift from itself.

- [ ] **Step 4: Run** — Expected: PASS

- [ ] **Step 5: Mutation-test** — hardcode `source="manual"` in the handler
and confirm the `"yahoo"` test goes red. This is the guard that keeps D7 true.

- [ ] **Step 6: Commit**

---

### Task 6: the marks screen fetches and pre-fills

**Files:**
- Modify: `web/src/api.ts` (`QuotesPage`, `UnquotedRow`, `fetchQuotes`; `MarkIn.source?`)
- Modify: `web/src/screens/Marks.tsx`
- Modify: `web/src/styles.css` if needed

- [ ] **Step 1: Client types and `fetchQuotes`**

Prices stay strings end to end, as in the rest of this file.

- [ ] **Step 2: The button**

A `Fetch quotes` button beside `Save marks`. On success it merges fetched
prices into the existing `prices` state — **merging, not replacing**: a price
already typed by hand is not overwritten, because the user typing a number is
a stronger signal than a scrape.

- [ ] **Step 3: Show provenance per row**

A fetched row shows its source next to the input. Rows that came back
`unquoted` show their reason in the same place — the warrant and the
blank-symbol instrument must read as "type this one yourself", not as an
empty box indistinguishable from one nobody got to.

- [ ] **Step 4: Submit carries the source**

Rows whose value is still exactly the fetched one submit `source: "yahoo"`;
a row the user edited afterwards submits no source, so it stores `manual`.
That is what makes the stored provenance true rather than decorative.

- [ ] **Step 5: `pnpm exec oxlint --max-warnings 0` and `pnpm build`**

Both must pass; CI gates on them.

- [ ] **Step 6: Commit**

---

### Task 7: docs

**Files:**
- Modify: `docs/known-gaps.md`

- [ ] **Step 1: Record what this slice leaves open**

One entry: the `mark` table has no `quoted_at`, so a provider's own timestamp
is shown at review time and then discarded — the stored `as_of` is the user's
chosen valuation instant. Name the consequence: a mark fetched from a stale
quote is indistinguishable in the database from one fetched live, and closing
it is a schema change.

- [ ] **Step 2: Commit**

---

## Verification before opening a PR

- `uv run ruff check .` — exit code checked directly, not through a pipe
- `uv run --env-file .env pytest tests --ignore=tests/db --ignore=tests/api -q`
- `uv run --env-file .env pytest tests/api -q`
- `uv run --env-file .env pytest tests/db -q` — **split by file**; the whole
  DB lane approaches the 600s tool ceiling
- `cd web && pnpm exec oxlint --max-warnings 0 && pnpm build`
- A live smoke test of `GET /api/quotes` against the real ledger, run
  deliberately and reported as live — it is the only network call in this
  work, and no test covers what Yahoo actually returns today.
