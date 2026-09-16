"""The provider seam (spec D5).

One implementation today. The protocol exists because yfinance scrapes
endpoints Yahoo does not document or support, so it will break -- the same
class of dependency as yt-dlp on this host. When it does, replacing it must be
a new file rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from marketdata.types import Quote


class QuoteSource(Protocol):
    name: str

    def quote(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Quotes by symbol.

        A symbol the provider cannot answer for is OMITTED from the result,
        never given a placeholder. The caller must be able to tell "no quote"
        from "a quote of zero" -- zero is a legal mark meaning the thing is
        worthless, which is a claim, not an absence.
        """
        ...
