"""The yfinance provider, against recorded shapes only.

Spec section 9: provider implementations are tested against recorded
fixtures, never live endpoints -- live tests are flaky and rate-limited. The
network call is a constructor argument, so nothing here can reach Yahoo even
by accident.
"""

from decimal import Decimal

import pytest

from marketdata.yahoo import YahooQuotes

# Recorded from a real yfinance fast_info on 2026-09-16, float noise included
# -- that noise is the entire reason quantize_price exists.
_RECORDED = {
    "GME": {"lastPrice": 21.44, "currency": "USD"},
    "DEMCF": {"lastPrice": 0.0795, "currency": "USD"},
    "NPSCY": {"lastPrice": 4.440000057220459, "currency": "USD"},
    "PYPL280121C00100000": {"lastPrice": 0.5299999713897705, "currency": "USD"},
}


def _recorded(symbols):
    return {s: _RECORDED[s] for s in symbols if s in _RECORDED}


def test_quotes_come_back_as_quantized_decimals():
    out = YahooQuotes(fetch=_recorded).quote(["GME", "NPSCY"])
    assert out["GME"].price == Decimal("21.4400")
    assert out["NPSCY"].price == Decimal("4.4400")
    assert all(isinstance(q.price, Decimal) for q in out.values())


def test_an_option_quote_survives_the_same_path():
    out = YahooQuotes(fetch=_recorded).quote(["PYPL280121C00100000"])
    assert out["PYPL280121C00100000"].price == Decimal("0.5300")


def test_an_unknown_symbol_is_omitted_not_zeroed():
    """GMEWS -- a warrant -- returned 'Quote not found' on 2026-09-16. A zero
    here would be recorded as a legal mark meaning 'worth nothing', which is
    a false statement about a position rather than a missing one."""
    out = YahooQuotes(fetch=_recorded).quote(["GME", "GMEWS"])
    assert "GMEWS" not in out
    assert "GME" in out


def test_one_bad_row_does_not_cost_the_others_their_quotes():
    def fetch(_syms):
        return {
            "GME": {"lastPrice": 21.44, "currency": "USD"},
            "DEMCF": {"lastPrice": None, "currency": "USD"},
        }

    assert set(YahooQuotes(fetch=fetch).quote(["GME", "DEMCF"])) == {"GME"}


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan")])
def test_a_price_the_database_would_refuse_is_dropped_not_proposed(bad):
    """mark_price_chk refuses all three. Proposing one would put a number in
    front of the user that submitting could only turn into a 500."""
    def fetch(_syms):
        return {"GME": {"lastPrice": bad, "currency": "USD"}}

    assert YahooQuotes(fetch=fetch).quote(["GME"]) == {}


def test_zero_is_kept_because_it_is_a_legal_mark():
    """The one falsy price that must survive. An expired option is worth
    zero, and `if not price: continue` would silently drop exactly the case
    the marks screen exists to record."""
    def fetch(_syms):
        return {"GME": {"lastPrice": 0.0, "currency": "USD"}}

    assert YahooQuotes(fetch=fetch).quote(["GME"])["GME"].price == Decimal("0.0000")


def test_a_missing_currency_defaults_to_usd_rather_than_failing():
    def fetch(_syms):
        return {"GME": {"lastPrice": 21.44, "currency": None}}

    assert YahooQuotes(fetch=fetch).quote(["GME"])["GME"].currency == "USD"


def test_the_source_name_is_the_provider():
    """This string is stored on the mark and is how D7 stays true."""
    src = YahooQuotes(fetch=_recorded)
    assert src.name == "yahoo"
    assert src.quote(["GME"])["GME"].source == "yahoo"


def test_no_symbols_means_no_call_and_no_quotes():
    calls = []

    def fetch(syms):
        calls.append(list(syms))
        return {}

    assert YahooQuotes(fetch=fetch).quote([]) == {}
    assert calls == [], "an empty request must not hit the provider at all"
