"""Common types returned by every broker parser."""
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Position:
    """A position found in a statement, before it is valued."""
    asset: str
    isin: str | None = None
    quantity: Decimal | None = None
    invested: Decimal = field(default_factory=lambda: Decimal("0"))
    withdrawn: Decimal = field(default_factory=lambda: Decimal("0"))
    # P&L already realised according to the statement (partial sells)
    realised_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    # Hint that the position looks closed (invested ~ withdrawn)
    looks_sold: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    broker: str
    positions: list[Position]
    warnings: list[str] = field(default_factory=list)


def D(x) -> Decimal:
    try:
        return Decimal(str(x)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")
