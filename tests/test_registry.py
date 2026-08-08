"""importers/registry.py: venue name -> importer lookup. Pure -- no I/O."""

from __future__ import annotations

import pytest

from importers.coinbase import CoinbaseImporter
from importers.coinbase_api import CoinbaseAPIImporter
from importers.fidelity import FidelityImporter
from importers.registry import get_importer, list_importers


def test_registry_exposes_the_api_importer():
    """§10 gap 6: the API importer must be registered as a distinct venue
    (`coinbase-api`), not folded into `coinbase` -- CoinbaseImporter and
    CoinbaseAPIImporter now map disjoint row kinds (cash vs. fills) for the
    same real-world venue, so they need two names, not one."""
    assert "coinbase-api" in list_importers()
    assert get_importer("coinbase-api").venue == "coinbase-api"


def test_coinbase_csv_importer_is_still_registered_for_cash():
    """Retiring the CSV path wholesale (instead of narrowing it to cash
    only) would have silently destroyed every Coinbase cash movement --
    `coinbase` must still resolve, and still to a `CoinbaseImporter`."""
    assert "coinbase" in list_importers()
    assert isinstance(get_importer("coinbase"), CoinbaseImporter)


def test_coinbase_api_importer_is_the_registered_instance_type():
    assert isinstance(get_importer("coinbase-api"), CoinbaseAPIImporter)


def test_fidelity_importer_is_unaffected_by_the_coinbase_cut_over():
    assert "fidelity" in list_importers()
    assert isinstance(get_importer("fidelity"), FidelityImporter)


def test_list_importers_is_sorted_and_names_exactly_three_venues():
    """Pins the CLI-visible venue choices (cli.py's `import` subcommand uses
    list_importers() for its --venue choices): a reader of `deadband import
    --help` should see `coinbase`, `coinbase-api`, and `fidelity`, in that
    order, and nothing else."""
    assert list_importers() == ["coinbase", "coinbase-api", "fidelity"]


def test_unknown_venue_raises_key_error_naming_the_available_ones():
    with pytest.raises(KeyError, match="unknown importer"):
        get_importer("not-a-real-venue")


def test_get_importer_is_case_insensitive():
    assert get_importer("COINBASE-API").venue == "coinbase-api"
