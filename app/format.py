"""Number formatting shared by the web pages, the PDF reports and the bot."""
from decimal import Decimal

from app.config import settings


def money(value) -> str:
    """Format an amount as 1,234.56 €. The symbol comes from CURRENCY_SYMBOL."""
    if value is None:
        return "-"
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    return f"{d:,.2f} {settings.currency_symbol}"


def pct(value, decimals: int = 0, zero_decimals: int = 3) -> str:
    """Percentage string.

    Uses `decimals` digits normally. When the value is not zero but would round
    to zero at that precision, it falls back to `zero_decimals` so a small real
    return is never shown as a flat `0%`.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    d = zero_decimals if (v != 0 and round(v, decimals) == 0) else decimals
    return f"{v:.{d}f}%"
