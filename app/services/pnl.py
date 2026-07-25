"""Profit and loss with Decimal, so cents never drift."""
from decimal import Decimal

ZERO = Decimal("0")
HALF = Decimal("0.5")


def total_profit(
    broker: str,
    invested: Decimal,
    withdrawn: Decimal,
    current_value: Decimal,
    realised_pnl: Decimal = ZERO,
    sold: bool = False,
) -> Decimal:
    """Total profit of a position given the current value on record.

    - myinvestor: (value + withdrawn) - invested. If the position is sold and
      the statement already recorded the redemption (withdrawn > 50% of the
      money put in), the value is not counted twice.
    - kraken: current_value - net invested.
    - traderepublic: (current_value - cost) + P&L already realised by sells.
    """
    inv = Decimal(invested or 0)
    wdr = Decimal(withdrawn or 0)
    val = Decimal(current_value or 0)
    pnl = Decimal(realised_pnl or 0)

    if broker == "myinvestor":
        val_calc = ZERO if (sold and wdr > inv * HALF) else val
        return ((val_calc + wdr) - inv).quantize(Decimal("0.01"))
    if broker == "kraken":
        return (val - inv).quantize(Decimal("0.01"))
    if broker == "traderepublic":
        return ((val - inv) + pnl).quantize(Decimal("0.01"))
    return ((val + wdr) - inv + pnl).quantize(Decimal("0.01"))


def return_pct(invested: Decimal, profit: Decimal) -> Decimal:
    inv = Decimal(invested or 0)
    if inv <= 0:
        return ZERO
    return (Decimal(profit) / inv * 100).quantize(Decimal("0.01"))
