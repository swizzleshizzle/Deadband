"""Quotes from Yahoo via yfinance.

UNOFFICIAL. yfinance scrapes endpoints Yahoo does not document or support, so
it breaks periodically and needs upgrading -- the same class of dependency as
yt-dlp on this host, which is managed exactly that way. `QuoteSource` exists
so that replacing this is a new file rather than a rewrite.

The network call is a constructor argument. No test may reach Yahoo (spec
section 9: recorded fixtures, never live endpoints), and injecting it is what
makes that structurally true rather than a convention.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from marketdata.types import Quote, quantize_price

FetchFn = Callable[[Sequence[str]], dict[str, dict]]


def _live_fetch(symbols: Sequence[str]) -> dict[str, dict]:
    """The only code here that touches the network."""
    import yfinance  # imported lazily: importing this module must not need it

    out: dict[str, dict] = {}
    tickers = yfinance.Tickers(" ".join(symbols))
    for s in symbols:
        try:
            fi = tickers.tickers[s].fast_info
            out[s] = {"lastPrice": fi.get("lastPrice"), "currency": fi.get("currency")}
        except Exception:
            # Per symbol, deliberately broad. A warrant with no Yahoo listing
            # raises KeyError from deep inside yfinance rather than returning
            # empty, and one such holding must not cost the other fourteen
            # their quotes. The omission is reported by the caller, which can
            # see which symbols it asked for.
            continue
    return out


class YahooQuotes:
    name = "yahoo"

    def __init__(self, fetch: FetchFn = _live_fetch) -> None:
        self._fetch = fetch

    def quote(self, symbols: Sequence[str]) -> dict[str, Quote]:
        requested = list(symbols)
        if not requested:
            # Not merely an optimisation: yfinance.Tickers("") does not mean
            # "no tickers", and an empty request must not become a live call
            # with undefined meaning.
            return {}

        raw = self._fetch(requested)
        quotes: dict[str, Quote] = {}
        for symbol, row in raw.items():
            price = row.get("lastPrice")
            # `is None`, never falsiness: 0.0 is a legal price meaning the
            # thing is worthless, which is exactly what an expired option is
            # and exactly what the marks screen needs to be able to record.
            if price is None:
                continue
            try:
                quotes[symbol] = Quote(
                    symbol=symbol,
                    price=quantize_price(price),
                    currency=(row.get("currency") or "USD").upper(),
                    # Yahoo's fast_info carries no trustworthy per-quote
                    # timestamp. Stamping now() here would assert freshness
                    # this provider never claimed -- see known-gaps on the
                    # absent `quoted_at` column.
                    quoted_at=None,
                    source=self.name,
                )
            except (ValueError, ArithmeticError):
                # Negative, NaN or Infinity: all three are refused by
                # mark_price_chk, so proposing one would put a number in front
                # of the user that submitting could only turn into a 500.
                continue
        return quotes
