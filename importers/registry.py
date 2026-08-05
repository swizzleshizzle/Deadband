"""Venue name → importer lookup. Pure — no I/O, no clock."""

from __future__ import annotations

from importers.base import Importer
from importers.coinbase import CoinbaseImporter
from importers.fidelity import FidelityImporter

_IMPORTERS: dict[str, Importer] = {
    "coinbase": CoinbaseImporter(),
    "fidelity": FidelityImporter(),
}


def get_importer(name: str) -> Importer:
    try:
        return _IMPORTERS[name.lower()]
    except KeyError:
        raise KeyError(f"unknown importer {name!r}; available: {sorted(_IMPORTERS)}") from None


def list_importers() -> list[str]:
    return sorted(_IMPORTERS)
