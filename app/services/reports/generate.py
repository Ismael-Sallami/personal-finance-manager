"""Report builders that read the DB. Shared by the web pages and the bot."""
from __future__ import annotations

import calendar
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BankAccount, Transaction
from app.services.aggregation import (
    _f,
    build_dashboard,
    cashflow_in_range,
    expense_categories_in_range,
    income_categories_in_range,
    invested_capital,
    invested_in_period,
    invested_in_range,
    latest_investments,
    period_totals,
    range_totals,
)
from app.services import insights
from app.services.pnl import return_pct
from app.services.reports import pdf
from app.services.reports.pdf import MONTHS


def _expense_categories(db: Session, year: int, month: int | None) -> list[tuple]:
    cond = [Transaction.year == year, Transaction.kind == "expense"]
    if month:
        cond.append(Transaction.month == month)
    rows = db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(*cond).group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    ).all()
    return [(c, float(v or 0)) for c, v in rows]


def _prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _month_consumption(db: Session, y: int, m: int) -> float:
    return float(db.scalar(select(func.sum(Transaction.amount)).where(
        Transaction.year == y, Transaction.month == m, *insights._consumption())) or 0)


def _daily_cumulative(db: Session, y: int, m: int, until: int) -> list[float]:
    """Cumulative spending per day (bank entries only, they have a real date)."""
    rows = db.execute(
        select(Transaction.date, func.sum(Transaction.amount)).where(
            Transaction.year == y, Transaction.month == m, Transaction.kind == "expense",
            Transaction.origin == "bank").group_by(Transaction.date)).all()
    by_day = {d.day: float(v or 0) for d, v in rows}
    out, acc = [], 0.0
    for day in range(1, until + 1):
        acc += by_day.get(day, 0.0)
        out.append(round(acc, 2))
    return out


def _monthly_extra(db: Session, year: int, month: int) -> dict:
    today = date.today()
    py, pm = _prev_month(year, month)
    x: dict = {}
    pe, pi = period_totals(db, py, pm)
    if pe or pi:
        x["prev"] = (pe, pi)
        x["prev_cats"] = _expense_categories(db, py, pm)
    x["consumption"] = _month_consumption(db, year, month)
    earlier, (yy, mm) = [], (year, month)
    for _ in range(3):
        yy, mm = _prev_month(yy, mm)
        c = _month_consumption(db, yy, mm)
        if c:
            earlier.append(c)
    x["avg3"] = sum(earlier) / len(earlier) if earlier else None

    days = calendar.monthrange(year, month)[1]
    if db.scalar(select(func.count()).where(
            Transaction.year == year, Transaction.month == month,
            Transaction.kind == "expense", Transaction.origin == "bank")):
        until = min(today.day, days) if (today.year, today.month) == (year, month) else days
        pdays = calendar.monthrange(py, pm)[1]
        x["daily"] = (_daily_cumulative(db, year, month, until),
                      _daily_cumulative(db, py, pm, pdays),
                      MONTHS[month], MONTHS[pm])
    x["top"] = [(c, float(v), n) for c, v, n in insights.top_merchants(db, year, month)]
    ref = min(today, date(year, month, days))
    x["subs"] = [(s.merchant, float(s.amount)) for s in insights.subscriptions(db, ref)]

    by_kind: dict = {"account": 0, "savings": 0, "cash": 0, "investment": 0}
    for b in db.scalars(select(BankAccount)).all():
        k = b.kind if b.kind in by_kind else "account"
        by_kind[k] += _f(b.balance)
    if any(by_kind.values()):
        by_kind["platforms"] = [(c.name, _f(c.balance)) for c in invested_capital(db)]
        x["net_worth"] = by_kind
    return x


def monthly_pdf(db: Session, year: int, month: int) -> bytes:
    expense, income = period_totals(db, year, month)
    return pdf.monthly_pdf(
        settings.report_owner, year, month, expense, income,
        _expense_categories(db, year, month), invested_in_period(db, year, month),
        extra=_monthly_extra(db, year, month))


def yearly_pdf(db: Session, year: int) -> bytes:
    rows = db.execute(
        select(Transaction.month, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year)
        .where(Transaction.kind.in_(("expense", "income")))
        .group_by(Transaction.month, Transaction.kind)
    ).all()
    income = [0.0] * 12
    expenses = [0.0] * 12
    for m, kind, total in rows:
        if kind == "income":
            income[m - 1] = float(total or 0)
        elif kind == "expense":  # transfers are neither
            expenses[m - 1] = float(total or 0)
    return pdf.yearly_pdf(
        settings.report_owner, year, income, expenses,
        _expense_categories(db, year, None), invested_in_period(db, year, None))


def period_pdf(db: Session, y1: int, m1: int, y2: int, m2: int, label: str) -> bytes:
    """Report for the month range [y1/m1 .. y2/m2]."""
    expense, income = range_totals(db, y1, m1, y2, m2)
    cats = expense_categories_in_range(db, y1, m1, y2, m2)
    invested = invested_in_range(db, y1, m1, y2, m2)
    return pdf.period_pdf(settings.report_owner, label, expense, income, cats, invested)


def detailed_pdf(db: Session, y1: int, m1: int, y2: int, m2: int, label: str) -> bytes:
    """Detailed report: net worth broken down (positions and accounts), expenses
    and income by category, and the monthly cashflow of the period."""
    dash = build_dashboard(db)
    invs = [
        (p.asset, p.broker, _f(p.invested), _f(p.current_value), _f(p.profit),
         float(return_pct(p.invested or 0, p.profit or 0)))
        for p in latest_investments(db)
    ]
    banks = [
        (b.name, _f(b.balance))
        for b in db.scalars(select(BankAccount).order_by(BankAccount.balance.desc())).all()
    ]
    expenses_cat = expense_categories_in_range(db, y1, m1, y2, m2)
    income_cat = income_categories_in_range(db, y1, m1, y2, m2)
    cashflow = cashflow_in_range(db, y1, m1, y2, m2)
    invested = invested_in_range(db, y1, m1, y2, m2)
    return pdf.detailed_pdf(
        settings.report_owner, label, dash["kpis"], invs, banks,
        expenses_cat, income_cat, cashflow, invested)


def net_worth_pdf(db: Session) -> bytes:
    dash = build_dashboard(db)
    split = list(zip(dash["charts"]["split"]["labels"],
                     dash["charts"]["split"]["data"]))
    evolution = (dash["charts"]["net_worth"]["labels"],
                 dash["charts"]["net_worth"]["data"])
    invs = [(p.asset, p.broker, p.invested, p.current_value, p.profit)
            for p in latest_investments(db)]
    return pdf.net_worth_pdf(settings.report_owner, dash["kpis"], split, invs, evolution)
