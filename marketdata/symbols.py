"""Ledger instrument -> provider symbol.

Pure, and deliberately strict. An instrument this cannot map raises rather
than falling back to the ledger's own spelling: Fidelity writes an option as
`-PYPL280121C100`, which is not a Yahoo symbol, and a near-miss that happens
to resolve returns some OTHER security's price into a P&L figure with nothing
downstream able to detect it. Refusing is visible; guessing is not.

The reason travels with the refusal, because it reaches the screen -- a row
that reads "no symbol recorded" invites the user to type the price by hand,
where an empty box is indistinguishable from one nobody got to.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal


class UnmappableInstrument(Exception):
    """No provider symbol can be built for this instrument, with the reason."""


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
        cleaned = symbol.strip().upper()
        if not cleaned:
            raise UnmappableInstrument("no symbol recorded for this instrument")
        return cleaned

    if asset_class == "option":
        if not (underlying and expiry and strike is not None and option_right):
            raise UnmappableInstrument(
                "option is missing the underlying, expiry, strike or right "
                "needed to build a contract symbol"
            )
        # OCC: ROOT + YYMMDD + C|P + (strike x 1000) zero-padded to 8 digits.
        # `to_integral_value` on the scaled Decimal rather than int(strike *
        # 1000) on a float: 12.50 * 1000 is 12500.000000000002 in binary
        # floating point, and int() of that truncates to 12500 by luck rather
        # than by rule.
        thousandths = int((strike * 1000).to_integral_value())
        return (
            f"{underlying.strip().upper()}"
            f"{expiry:%y%m%d}"
            f"{'C' if option_right == 'call' else 'P'}"
            f"{thousandths:08d}"
        )

    raise UnmappableInstrument(
        f"{asset_class} instruments are not quoted by this provider"
    )
