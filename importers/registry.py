"""Venue name → importer lookup. Pure — no I/O, no clock."""

from __future__ import annotations

from importers.base import Importer
from importers.coinbase import CoinbaseImporter
from importers.coinbase_api import CoinbaseAPIImporter
from importers.fidelity import FidelityImporter

_IMPORTERS: dict[str, Importer] = {
    # §10 gap 6, closed 2026-08-08: fills and cash for Coinbase now come
    # from two DIFFERENT importers, deliberately. The Advanced Trade API
    # (coinbase-api) returns trade executions only -- it has no deposits,
    # withdrawals, rewards, or staking income -- so the CSV importer
    # (coinbase) is kept alive for cash movements alone rather than
    # retired wholesale, which would have silently destroyed every
    # Coinbase cash movement ever imported. See importers/coinbase.py's
    # _FILL_TYPES branch for the other half of this cut-over.
    "coinbase": CoinbaseImporter(),  # cash movements only, see gap 6
    "coinbase-api": CoinbaseAPIImporter(),  # fills only
    "fidelity": FidelityImporter(),
}


def get_importer(name: str) -> Importer:
    try:
        return _IMPORTERS[name.lower()]
    except KeyError:
        raise KeyError(f"unknown importer {name!r}; available: {sorted(_IMPORTERS)}") from None


def list_importers() -> list[str]:
    return sorted(_IMPORTERS)
