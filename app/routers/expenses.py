"""Expenses and income: manual entry, text import, monthly and yearly views."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_session
from app.models import Transaction, User
from app.security import get_csrf_token, validate_csrf
from app.services.expenses_parse import parse_text
from app.templating import templates

router = APIRouter(prefix="/expenses")


def _parse_amount(raw: str) -> Decimal:
    """Accepts 1,234.56 / 1.234,56 / 1234.56 / 1234,56 -> Decimal."""
    s = raw.strip().replace(" ", "")
    if "," in s and "." in s:
        # The last separator is the decimal one; the other groups thousands.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _years(db: Session) -> list[int]:
    ys = db.scalars(select(Transaction.year).distinct().order_by(Transaction.year.desc())).all()
    return list(ys) or [date.today().year]


@router.get("")
def index(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    today = date.today()
    year = year or today.year
    month = month or today.month

    txs = db.scalars(
        select(Transaction)
        .where(Transaction.year == year, Transaction.month == month)
        .order_by(Transaction.kind, Transaction.category)
    ).all()

    by_cat = defaultdict(lambda: {"expense": Decimal("0"), "income": Decimal("0")})
    total_expense = total_income = Decimal("0")
    for t in txs:
        by_cat[t.category][t.kind] += t.amount
        if t.kind == "expense":
            total_expense += t.amount
        else:
            total_income += t.amount

    expense_cat = {k: float(v["expense"]) for k, v in by_cat.items() if v["expense"]}
    income_cat = {k: float(v["income"]) for k, v in by_cat.items() if v["income"]}

    return templates.TemplateResponse(
        "expenses.html",
        {
            "request": request, "user": user, "active": "expenses",
            "csrf_token": get_csrf_token(request),
            "year": year, "month": month, "years": _years(db),
            "txs": txs,
            "total_expense": total_expense, "total_income": total_income,
            "balance": total_income - total_expense,
            "chart_expenses": {"labels": list(expense_cat.keys()), "data": list(expense_cat.values())},
            "chart_incomes": {"labels": list(income_cat.keys()), "data": list(income_cat.values())},
        },
    )


@router.post("/add")
def add(
    request: Request,
    kind: str = Form(...),
    category: str = Form(...),
    amount: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    day: int = Form(1),
    note: str = Form(""),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(f"/expenses?year={year}&month={month}", status_code=303)
    kind = "income" if kind == "income" else "expense"
    month = max(1, min(12, month))
    year = max(2000, min(2100, year))
    # Capped at 28 so any month accepts the day without extra validation.
    day = max(1, min(28, day))
    db.add(Transaction(
        date=date(year, month, day), year=year, month=month,
        category=category.strip() or "Uncategorised", kind=kind,
        amount=_parse_amount(amount), note=(note.strip() or None),
    ))
    db.commit()
    return RedirectResponse(f"/expenses?year={year}&month={month}", status_code=303)


@router.post("/delete/{tx_id}")
def delete(
    tx_id: int,
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if validate_csrf(request, csrf_token):
        tx = db.get(Transaction, tx_id)
        if tx:
            db.delete(tx)
            db.commit()
    return RedirectResponse(f"/expenses?year={year}&month={month}", status_code=303)


@router.post("/import/preview")
def import_preview(
    request: Request,
    text: str = Form(...),
    year: int = Form(...),
    month: int = Form(0),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(f"/expenses?year={year}&month={month}", status_code=303)
    parsed = parse_text(text)
    month = parsed.month or month or date.today().month
    return templates.TemplateResponse(
        "expenses_import_preview.html",
        {
            "request": request, "user": user, "active": "expenses",
            "csrf_token": get_csrf_token(request),
            "year": year, "month": month,
            "rows": parsed.items, "ignored": parsed.ignored,
        },
    )


@router.post("/import/save")
async def import_save(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    if not validate_csrf(request, form.get("csrf_token")):
        return RedirectResponse("/expenses", status_code=303)
    year = int(form.get("year") or date.today().year)
    month = max(1, min(12, int(form.get("month") or date.today().month)))
    mode = form.get("mode") or "append"
    n = int(form.get("n") or 0)

    # Replace mode: wipe the month before inserting, so re-importing a corrected
    # list does not duplicate every row.
    if mode == "replace":
        db.execute(
            sql_delete(Transaction).where(
                Transaction.year == year, Transaction.month == month
            )
        )

    saved = 0
    for i in range(n):
        if not form.get(f"include_{i}"):
            continue
        category = (form.get(f"category_{i}") or "").strip() or "Uncategorised"
        kind = "income" if form.get(f"kind_{i}") == "income" else "expense"
        amount = _parse_amount(form.get(f"amount_{i}") or "0")
        db.add(Transaction(
            date=date(year, month, 1), year=year, month=month,
            category=category, kind=kind, amount=amount, note="imported",
        ))
        saved += 1
    db.commit()
    return RedirectResponse(f"/expenses?year={year}&month={month}", status_code=303)


@router.get("/yearly")
def yearly(
    request: Request,
    year: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    year = year or date.today().year
    rows = db.execute(
        select(Transaction.month, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year)
        .group_by(Transaction.month, Transaction.kind)
    ).all()
    months_e = [0.0] * 12
    months_i = [0.0] * 12
    for m, kind, total in rows:
        if kind == "expense":
            months_e[m - 1] = float(total or 0)
        else:
            months_i[m - 1] = float(total or 0)

    cat_rows = db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(Transaction.year == year, Transaction.kind == "expense")
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
    ).all()
    cat_labels = [c for c, _ in cat_rows]
    cat_data = [float(s or 0) for _, s in cat_rows]

    return templates.TemplateResponse(
        "expenses_yearly.html",
        {
            "request": request, "user": user, "active": "expenses",
            "csrf_token": get_csrf_token(request),
            "year": year, "years": _years(db),
            "months_expenses": months_e, "months_incomes": months_i,
            "total_expenses": round(sum(months_e), 2),
            "total_incomes": round(sum(months_i), 2),
            "balance": round(sum(months_i) - sum(months_e), 2),
            "cat": {"labels": cat_labels, "data": cat_data},
        },
    )
