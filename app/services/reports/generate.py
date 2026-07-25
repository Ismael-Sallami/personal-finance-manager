"""Report builders that read the DB. Shared by the web pages and the bot."""
from __future__ import annotations

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
    invested_in_period,
    invested_in_range,
    latest_investments,
    range_totals,
)
from app.services.pnl import return_pct
from app.services.reports import pdf


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


def monthly_pdf(db: Session, year: int, month: int) -> bytes:
    expense = float(db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.year == year, Transaction.month == month,
            Transaction.kind == "expense")) or 0)
    income = float(db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.year == year, Transaction.month == month,
            Transaction.kind == "income")) or 0)
    return pdf.monthly_pdf(
        settings.report_owner, year, month, expense, income,
        _expense_categories(db, year, month), invested_in_period(db, year, month))


def yearly_pdf(db: Session, year: int) -> bytes:
    rows = db.execute(
        select(Transaction.month, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year)
        .group_by(Transaction.month, Transaction.kind)
    ).all()
    income = [0.0] * 12
    expenses = [0.0] * 12
    for m, kind, total in rows:
        (income if kind == "income" else expenses)[m - 1] = float(total or 0)
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
