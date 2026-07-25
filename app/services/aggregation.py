"""Aggregation for the dashboard and the bot.

Net worth = current value of the investments (latest valuation per broker) plus
the balance of every bank account. Everything else here are period KPIs and the
series the charts draw.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BankAccount, Investment, NetWorthSnapshot, Transaction


def _f(x) -> float:
    return float(x or 0)


def total_banks(db: Session) -> float:
    """Sum of every account balance, cash included."""
    return _f(db.scalar(select(func.sum(BankAccount.balance))))


def _net_worth_parts(db: Session) -> tuple[float, float]:
    """(investment value [latest valuation per broker], total bank balance)."""
    inv_value = sum(_f(inv.current_value) for inv in latest_investments(db))
    return inv_value, total_banks(db)


def snapshot_today(db: Session) -> NetWorthSnapshot:
    """Upsert today's net worth snapshot (investments + banks).

    Called after contributing, editing an investment or editing an account, and
    from the monthly job, so the evolution chart fills up as the user works.
    """
    inv_value, bank_value = _net_worth_parts(db)
    today = date.today()
    snap = db.scalar(select(NetWorthSnapshot).where(NetWorthSnapshot.date == today))
    if snap is None:
        snap = NetWorthSnapshot(date=today)
        db.add(snap)
    snap.investments = Decimal(str(round(inv_value, 2)))
    snap.banks = Decimal(str(round(bank_value, 2)))
    snap.total = Decimal(str(round(inv_value + bank_value, 2)))
    db.commit()
    return snap


def latest_investments(db: Session) -> list[Investment]:
    """For each broker, the positions of its most recent valuation."""
    rows: list[Investment] = []
    brokers = db.scalars(select(Investment.broker).distinct()).all()
    for broker in brokers:
        max_date = db.scalar(
            select(func.max(Investment.valued_on)).where(Investment.broker == broker)
        )
        if max_date is None:
            continue
        rows.extend(
            db.scalars(
                select(Investment).where(
                    Investment.broker == broker,
                    Investment.valued_on == max_date,
                )
            ).all()
        )
    return rows


def years_with_data(db: Session) -> list[int]:
    ys = db.scalars(
        select(Transaction.year).distinct().order_by(Transaction.year.desc())
    ).all()
    return list(ys) or [date.today().year]


def period_totals(db: Session, year: int, month: int | None) -> tuple[float, float]:
    """(expenses, income) for the month, or for the whole year when month is None."""
    cond = [Transaction.year == year]
    if month:
        cond.append(Transaction.month == month)
    expense = _f(db.scalar(
        select(func.sum(Transaction.amount)).where(*cond, Transaction.kind == "expense")
    ))
    income = _f(db.scalar(
        select(func.sum(Transaction.amount)).where(*cond, Transaction.kind == "income")
    ))
    return expense, income


# Expense categories that are really investing: the money is not consumed, it
# already shows up in net worth. They still count as an expense, but the reports
# call them out separately ("of which X was invested"). Compared lower cased.
INVESTMENT_CATEGORIES = {"investment", "investments", "investing"}


def invested_in_period(db: Session, year: int, month: int | None) -> float:
    """Expenses categorised as investment in the month, or in the whole year."""
    cond = [Transaction.year == year, Transaction.kind == "expense",
            func.lower(Transaction.category).in_(tuple(INVESTMENT_CATEGORIES))]
    if month:
        cond.append(Transaction.month == month)
    return _f(db.scalar(select(func.sum(Transaction.amount)).where(*cond)))


def _month_ordinals(y1: int, m1: int, y2: int, m2: int) -> tuple[int, int]:
    """Inclusive range as month ordinals (year*12+month), always low to high.

    Working with one integer per month avoids all the year-boundary logic.
    """
    start, end = y1 * 12 + m1, y2 * 12 + m2
    return (end, start) if start > end else (start, end)


_ORDINAL = Transaction.year * 12 + Transaction.month


def invested_in_range(db: Session, y1: int, m1: int, y2: int, m2: int) -> float:
    """Investment-tagged expenses inside the inclusive range [y1/m1 .. y2/m2]."""
    start, end = _month_ordinals(y1, m1, y2, m2)
    return _f(db.scalar(
        select(func.sum(Transaction.amount)).where(
            _ORDINAL >= start, _ORDINAL <= end, Transaction.kind == "expense",
            func.lower(Transaction.category).in_(tuple(INVESTMENT_CATEGORIES)))
    ))


def range_totals(db: Session, y1: int, m1: int, y2: int, m2: int) -> tuple[float, float]:
    """(expenses, income) summed over the inclusive month range [y1/m1 .. y2/m2]."""
    start, end = _month_ordinals(y1, m1, y2, m2)
    cond = [_ORDINAL >= start, _ORDINAL <= end]
    expense = _f(db.scalar(
        select(func.sum(Transaction.amount)).where(*cond, Transaction.kind == "expense")
    ))
    income = _f(db.scalar(
        select(func.sum(Transaction.amount)).where(*cond, Transaction.kind == "income")
    ))
    return expense, income


_MONTHS_SHORT = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                 "Sep", "Oct", "Nov", "Dec"]


def _categories_in_range(db: Session, y1: int, m1: int, y2: int, m2: int,
                         kind: str) -> list[tuple]:
    """[(category, total)] of the given kind in the range, biggest first."""
    start, end = _month_ordinals(y1, m1, y2, m2)
    rows = db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(_ORDINAL >= start, _ORDINAL <= end, Transaction.kind == kind)
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    ).all()
    return [(c, round(_f(v), 2)) for c, v in rows]


def expense_categories_in_range(db: Session, y1: int, m1: int, y2: int, m2: int) -> list[tuple]:
    return _categories_in_range(db, y1, m1, y2, m2, "expense")


def income_categories_in_range(db: Session, y1: int, m1: int, y2: int, m2: int) -> list[tuple]:
    return _categories_in_range(db, y1, m1, y2, m2, "income")


def cashflow_in_range(db: Session, y1: int, m1: int, y2: int, m2: int) -> tuple[list, list, list]:
    """(labels, income, expenses) month by month over the inclusive range."""
    start, end = _month_ordinals(y1, m1, y2, m2)
    rows = db.execute(
        select(Transaction.year, Transaction.month, Transaction.kind,
               func.sum(Transaction.amount))
        .where(_ORDINAL >= start, _ORDINAL <= end)
        .group_by(Transaction.year, Transaction.month, Transaction.kind)
    ).all()
    months = []
    for o in range(start, end + 1):
        y, m0 = divmod(o - 1, 12)
        months.append((y, m0 + 1))
    idx = {ym: i for i, ym in enumerate(months)}
    income = [0.0] * len(months)
    expenses = [0.0] * len(months)
    for y, m, kind, v in rows:
        i = idx.get((y, m))
        if i is not None:
            (income if kind == "income" else expenses)[i] = round(_f(v), 2)
    labels = [f"{_MONTHS_SHORT[m]} {str(y)[2:]}" for y, m in months]
    return labels, income, expenses


def build_dashboard(db: Session, year: int | None = None, month: int | None = None) -> dict:
    today = date.today()
    year = year or today.year
    month = month or today.month

    # --- Investments: value and P&L ---
    invs = latest_investments(db)
    inv_by_broker: dict[str, float] = defaultdict(float)
    inv_value = inv_invested = inv_profit = 0.0
    for inv in invs:
        inv_by_broker[inv.broker] += _f(inv.current_value)
        inv_value += _f(inv.current_value)
        inv_invested += _f(inv.invested)
        inv_profit += _f(inv.profit)
    return_pct = (inv_profit / inv_invested * 100) if inv_invested else 0.0

    # --- Banks and cash ---
    banks = db.scalars(select(BankAccount).order_by(BankAccount.balance.desc())).all()
    bank_value = sum(_f(b.balance) for b in banks)

    net_worth = inv_value + bank_value

    # --- KPIs of the selected month ---
    month_expense, month_income = period_totals(db, year, month)

    # --- Cashflow: expenses vs income per month of the year ---
    rows = db.execute(
        select(Transaction.month, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year)
        .group_by(Transaction.month, Transaction.kind)
    ).all()
    cf_e = [0.0] * 12
    cf_i = [0.0] * 12
    for m, kind, total in rows:
        (cf_i if kind == "income" else cf_e)[m - 1] = _f(total)

    # --- Doughnut: expenses by category in the selected month ---
    cat_rows = db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(Transaction.year == year, Transaction.month == month,
               Transaction.kind == "expense")
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    ).all()
    cat_labels = [c for c, _ in cat_rows]
    cat_data = [round(_f(s), 2) for _, s in cat_rows]

    # --- Net worth split: brokers plus each account ---
    split_labels = [b.capitalize() for b in inv_by_broker]
    split_data = [round(v, 2) for v in inv_by_broker.values()]
    for b in banks:
        if _f(b.balance):
            split_labels.append(b.name)
            split_data.append(round(_f(b.balance), 2))

    # --- Net worth over time: the snapshot series. Falls back to grouping by
    # valuation date while no snapshot exists yet. ---
    snaps = db.execute(
        select(NetWorthSnapshot.date, NetWorthSnapshot.total)
        .order_by(NetWorthSnapshot.date)
    ).all()
    if snaps:
        evo_labels = [d.strftime("%d/%m/%y") for d, _ in snaps]
        evo_data = [round(_f(s), 2) for _, s in snaps]
    else:
        evo = db.execute(
            select(Investment.valued_on, func.sum(Investment.current_value))
            .group_by(Investment.valued_on)
            .order_by(Investment.valued_on)
        ).all()
        evo_labels = [d.strftime("%d/%m/%y") for d, _ in evo]
        evo_data = [round(_f(s), 2) for _, s in evo]

    return {
        "year": year,
        "month": month,
        "years": years_with_data(db),
        "kpis": {
            "net_worth": round(net_worth, 2),
            "bank_balance": round(bank_value, 2),
            "inv_value": round(inv_value, 2),
            "inv_invested": round(inv_invested, 2),
            "inv_profit": round(inv_profit, 2),
            "return_pct": round(return_pct, 5),
            "month_expense": round(month_expense, 2),
            "month_income": round(month_income, 2),
            "month_saved": round(month_income - month_expense, 2),
            "month_invested": round(invested_in_period(db, year, month), 2),
        },
        "charts": {
            "cashflow": {"labels": [str(i) for i in range(1, 13)],
                         "expenses": [round(x, 2) for x in cf_e],
                         "income": [round(x, 2) for x in cf_i]},
            "categories": {"labels": cat_labels, "data": cat_data},
            "split": {"labels": split_labels, "data": split_data},
            "net_worth": {"labels": evo_labels, "data": evo_data},
        },
        "owner": settings.report_owner,
        "has_data": bool(invs or rows or banks),
    }
