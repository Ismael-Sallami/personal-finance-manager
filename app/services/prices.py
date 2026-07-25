"""Automatic revaluation of investments with market prices (yfinance).

For every position with a yahoo_symbol, a quantity and auto_value on, the last
price is downloaded and current_value = quantity * price, then the profit is
recomputed. Positions without a symbol (an unlisted fund, for example) keep the
value from the statement. yfinance is best effort: a network failure just means
nothing changes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models import Investment
from app.services.pnl import total_profit

# Kraken pairs are named BTC/EUR; Yahoo wants BTC-EUR.
CRYPTO_EUR = {
    "BTC": "BTC-EUR", "XBT": "BTC-EUR", "ETH": "ETH-EUR", "SOL": "SOL-EUR",
    "ADA": "ADA-EUR", "DOT": "DOT-EUR", "XRP": "XRP-EUR", "DOGE": "DOGE-EUR",
    "LTC": "LTC-EUR", "MATIC": "MATIC-EUR", "AVAX": "AVAX-EUR", "LINK": "LINK-EUR",
}


def crypto_symbol(asset: str) -> str | None:
    """Yahoo symbol derived from a pair name such as 'BTC/EUR'."""
    base = (asset or "").upper().split("/")[0].strip()
    return CRYPTO_EUR.get(base)


def _to_decimal(x) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_price(symbol: str) -> Decimal | None:
    """Last price of a Yahoo symbol. None when it cannot be fetched."""
    if not symbol:
        return None
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        ticker = yf.Ticker(symbol)
        # fast_info avoids downloading the whole history.
        price = None
        try:
            price = ticker.fast_info.get("last_price")
        except Exception:
            price = None
        if not price:
            hist = ticker.history(period="5d")
            if not hist.empty:
                price = float(hist["Close"].dropna().iloc[-1])
        return _to_decimal(price) if price else None
    except Exception:
        return None


def refresh_position(inv: Investment) -> bool:
    """Update price, value and profit of one position. True if anything changed."""
    if not inv.auto_value:
        return False
    symbol = inv.yahoo_symbol or (
        crypto_symbol(inv.asset) if inv.broker == "kraken" else None
    )
    if not symbol or inv.quantity is None:
        return False
    price = fetch_price(symbol)
    if price is None:
        return False

    inv.yahoo_symbol = symbol
    inv.current_price = price
    inv.price_updated_at = datetime.now(timezone.utc)
    inv.current_value = (Decimal(inv.quantity) * price).quantize(Decimal("0.01"))
    inv.profit = total_profit(
        inv.broker, inv.invested, inv.withdrawn, inv.current_value,
        Decimal("0"), sold=False,
    )
    return True


def refresh_all(db: Session) -> dict:
    """Revalue every position of the latest valuation per broker.

    Returns {'updated': n, 'total': m, 'errors': [..]}.
    """
    from app.services.aggregation import latest_investments

    positions = latest_investments(db)
    updated = 0
    errors: list[str] = []
    for inv in positions:
        try:
            if refresh_position(inv):
                updated += 1
        except Exception as exc:  # one bad symbol must not abort the batch
            errors.append(f"{inv.asset}: {exc}")
    if updated:
        db.commit()
    return {"updated": updated, "total": len(positions), "errors": errors}
