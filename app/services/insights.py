"""Analysis over the movements: month pace, comparison, merchants,
subscriptions, unusual spending and budgets. Read-only functions except
`budget_alerts` (it records the last alert sent)."""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Budget, Transaction
from app.services.aggregation import INVESTMENT_CATEGORIES

SUBSCRIPTION_MIN_CHARGES = 3
SUBSCRIPTION_DAYS = (25, 35)
SUBSCRIPTION_TOLERANCE = Decimal("0.10")
UNUSUAL_FACTOR = 3
UNUSUAL_MIN = Decimal("50")
UNUSUAL_NEW_MIN = Decimal("100")


def _consumption():
    """Condition: expenses that are not investing (what is really consumed)."""
    return [Transaction.kind == "expense",
            func.lower(Transaction.category).notin_(tuple(INVESTMENT_CATEGORIES))]


def _sum(db: Session, *cond) -> Decimal:
    return Decimal(str(db.scalar(select(func.sum(Transaction.amount)).where(*cond)) or 0))


def _previous_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


# --- Month pace ---
@dataclass
class Pace:
    spent: Decimal
    projection: Decimal
    average: Decimal | None    # average of the 3 previous months with data
    day: int
    days_in_month: int


def pace(db: Session, today: date) -> Pace:
    y, m = today.year, today.month
    spent = _sum(db, *_consumption(), Transaction.year == y, Transaction.month == m,
                 Transaction.date <= today)
    days = calendar.monthrange(y, m)[1]
    projection = (spent / today.day * days).quantize(Decimal("0.01"))
    previous = []
    py, pm = y, m
    for _ in range(3):
        py, pm = _previous_month(py, pm)
        s = _sum(db, *_consumption(), Transaction.year == py, Transaction.month == pm)
        if s:
            previous.append(s)
    average = (sum(previous) / len(previous)).quantize(Decimal("0.01")) if previous else None
    return Pace(spent, projection, average, today.day, days)


# --- Comparison with the previous month up to the same day ---
def comparison(db: Session, today: date) -> list[tuple[str, Decimal, Decimal]]:
    """[(category, this_month, last_month)] up to the same day, biggest change first."""
    py, pm = _previous_month(today.year, today.month)
    prev_day = min(today.day, calendar.monthrange(py, pm)[1])

    def by_category(y, m, until):
        rows = db.execute(
            select(Transaction.category, func.sum(Transaction.amount)).where(
                *_consumption(), Transaction.year == y, Transaction.month == m,
                Transaction.date <= until,
            ).group_by(Transaction.category)).all()
        return {c: Decimal(str(v)) for c, v in rows}

    current = by_category(today.year, today.month, today)
    previous = by_category(py, pm, date(py, pm, prev_day))
    cats = set(current) | set(previous)
    rows = [(c, current.get(c, Decimal(0)), previous.get(c, Decimal(0))) for c in cats]
    rows.sort(key=lambda r: abs(r[1] - r[2]), reverse=True)
    return rows


# --- Top merchants ---
def top_merchants(db: Session, y: int, m: int, n: int = 5) -> list[tuple[str, Decimal, int]]:
    rows = db.execute(
        select(Transaction.merchant, func.sum(Transaction.amount), func.count()).where(
            *_consumption(), Transaction.year == y, Transaction.month == m,
            Transaction.merchant.is_not(None),
        ).group_by(Transaction.merchant)
        .order_by(func.sum(Transaction.amount).desc()).limit(n)).all()
    return [(c, Decimal(str(s)), int(k)) for c, s, k in rows]


# --- Subscriptions ---
@dataclass
class Subscription:
    merchant: str
    amount: Decimal             # latest charge
    previous: Decimal | None    # the charge before (to spot price rises)
    last: date
    charges: int


def is_subscription(charges: list[tuple[date, Decimal]]) -> bool:
    """Latest charges of a merchant: >= 3, roughly monthly, similar amount.

    The last charge may differ by up to 50 % (a price rise): otherwise a
    subscription that gets pricier would stop being detected exactly when the
    alert is needed."""
    if len(charges) < SUBSCRIPTION_MIN_CHARGES:
        return False
    charges = sorted(charges)[-SUBSCRIPTION_MIN_CHARGES:]
    lo, hi = SUBSCRIPTION_DAYS
    for (a, _), (b, _) in zip(charges, charges[1:]):
        if not lo <= (b - a).days <= hi:
            return False
    earlier = [i for _, i in charges[:-1]]
    med = Decimal(str(median(earlier)))
    if not med:
        return False
    if not all(abs(i - med) <= med * SUBSCRIPTION_TOLERANCE for i in earlier):
        return False
    return abs(charges[-1][1] - med) <= med * Decimal("0.5")


def _charges_by_merchant(db: Session, since: date) -> dict[str, list[tuple[date, Decimal]]]:
    rows = db.execute(select(Transaction.merchant, Transaction.date, Transaction.amount)
                      .where(Transaction.kind == "expense", Transaction.origin == "bank",
                             Transaction.merchant.is_not(None), Transaction.date >= since)
                      ).all()
    out: dict[str, list] = defaultdict(list)
    for c, d, i in rows:
        out[c].append((d, Decimal(str(i))))
    return out


def subscriptions(db: Session, today: date) -> list[Subscription]:
    out = []
    for merchant, charges in _charges_by_merchant(db, today - timedelta(days=200)).items():
        if not is_subscription(charges):
            continue
        charges.sort()
        if (today - charges[-1][0]).days > SUBSCRIPTION_DAYS[1] + 10:
            continue  # no longer charged: cancelled
        out.append(Subscription(merchant, charges[-1][1],
                                charges[-2][1] if len(charges) > 1 else None,
                                charges[-1][0], len(charges)))
    out.sort(key=lambda s: s.amount, reverse=True)
    return out


def subscription_alert(db: Session, tx: Transaction) -> str | None:
    """If `tx` (just imported) creates or raises a subscription, the alert text."""
    if tx.kind != "expense" or not tx.merchant:
        return None
    charges = _charges_by_merchant(db, tx.date - timedelta(days=200)).get(tx.merchant, [])
    without_this = [c for c in charges if not (c[0] == tx.date and c[1] == tx.amount)]
    if is_subscription(charges) and not is_subscription(without_this):
        return f"🔁 New subscription detected: <b>{tx.merchant}</b> ({tx.amount} €/month)"
    if is_subscription(without_this):
        previous = sorted(without_this)[-1][1]
        if tx.amount > previous * Decimal("1.02"):
            return f"📈 <b>{tx.merchant}</b> went up: {previous} € → {tx.amount} €"
    return None


# --- Unusual spending ---
def unusual_alert(db: Session, tx: Transaction) -> str | None:
    if tx.kind != "expense" or (tx.category or "").lower() in INVESTMENT_CATEGORIES:
        return None
    previous = db.scalars(select(Transaction.amount).where(
        Transaction.kind == "expense", Transaction.origin == "bank",
        Transaction.category == tx.category, Transaction.id != tx.id,
        Transaction.date >= tx.date - timedelta(days=180))).all()
    if tx.amount >= UNUSUAL_MIN and len(previous) >= 3:
        med = Decimal(str(median(previous)))
        if tx.amount > med * UNUSUAL_FACTOR:
            return (f"⚠ High spending in <b>{tx.category}</b>: {tx.amount} € at "
                    f"{tx.merchant} (usually around {med:.2f} €)")
    if tx.amount >= UNUSUAL_NEW_MIN and tx.merchant:
        seen = db.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.merchant == tx.merchant, Transaction.id != tx.id))
        if not seen:
            return f"🆕 First charge at <b>{tx.merchant}</b>: {tx.amount} €"
    return None


# --- Budgets ---
def budget_status(db: Session, today: date) -> list[tuple[Budget, Decimal]]:
    out = []
    for b in db.scalars(select(Budget).order_by(Budget.category)).all():
        spent = _sum(db, Transaction.kind == "expense",
                     func.lower(Transaction.category) == b.category.lower(),
                     Transaction.year == today.year, Transaction.month == today.month)
        out.append((b, spent))
    return out


def budget_alerts(db: Session, today: date) -> list[str]:
    """Alerts when crossing 80 % and 100 %, once per threshold and month."""
    alerts = []
    month = f"{today.year}-{today.month:02d}"
    for b, spent in budget_status(db, today):
        if not b.monthly_limit:
            continue
        pct = spent / b.monthly_limit * 100
        threshold = 100 if pct >= 100 else 80 if pct >= 80 else None
        if threshold is None:
            continue
        mark = f"{month}:{threshold:03d}"  # "080" < "100" when compared as text
        if b.last_alert and b.last_alert >= mark:
            continue
        b.last_alert = mark
        icon = "🔴" if threshold == 100 else "🟠"
        alerts.append(f"{icon} <b>{b.category}</b>: {spent:.2f} € of {b.monthly_limit:.2f} € "
                      f"({pct:.0f} %)")
    db.commit()
    return alerts
