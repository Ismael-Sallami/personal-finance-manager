"""Contributions to investments (dollar-cost averaging).

Manual first: a position keeps one `invested` figure, the accumulated cost, and
it grows with every contribution. `profit = value - invested` is never typed in.
The market value is either entered by hand (funds) or refreshed automatically
(ETFs and crypto with a symbol).

Three ways to contribute, all adding to the same `invested`:
  (a) the initial value when the position is created,
  (b) the one-off "+ contribute" button,
  (c) the automatic monthly contribution.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Investment
from app.services.pnl import total_profit
from app.services.prices import refresh_position

log = logging.getLogger("contributions")

ZERO = Decimal("0")


def contribute(db: Session, inv: Investment, amount: Decimal,
               units: Decimal = ZERO) -> None:
    """Apply a contribution to a position. Does not commit; the caller does.

    - `invested += amount` (accumulated cost).
    - with `units`: `quantity += units`.
    - auto valued with a symbol -> revalue at market price. Otherwise the money
      goes straight into the value (`current_value += amount`) and the user
      adjusts the real market value later.
    """
    amount = Decimal(amount or 0)
    inv.invested = Decimal(inv.invested or 0) + amount
    if units:
        inv.quantity = Decimal(inv.quantity or 0) + Decimal(units)

    if not (inv.auto_value and refresh_position(inv)):
        inv.current_value = Decimal(inv.current_value or 0) + amount
        inv.profit = total_profit(
            inv.broker, inv.invested, inv.withdrawn, inv.current_value,
            ZERO, sold=False,
        )


def apply_monthly_contributions(db: Session) -> int:
    """Apply the monthly contribution of every position that has one.

    Meant to run on the 1st of each month. Returns how many were applied.
    """
    positions = db.scalars(
        select(Investment).where(Investment.monthly_contribution > 0)
    ).all()
    applied = 0
    for inv in positions:
        try:
            contribute(db, inv, inv.monthly_contribution)
            applied += 1
        except Exception as exc:  # one bad position must not abort the batch
            log.warning("monthly contribution failed on %s: %s", inv.asset, exc)
    if applied:
        db.commit()
        # Refresh the net worth snapshot after the contributions of the month.
        from app.services.aggregation import snapshot_today
        snapshot_today(db)
    return applied
