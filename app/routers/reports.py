"""Reports: Excel and PDF downloads generated from the database."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_session
from app.models import Transaction, User
from app.security import get_csrf_token
from app.templating import MONTHS, templates

# openpyxl (Excel), reportlab and matplotlib (generate -> pdf -> charts) take
# seconds to import. They are imported inside each handler so the process starts
# fast: on hosts that put the app to sleep, a slow start makes Telegram drop the
# webhook before the app can answer.

router = APIRouter(prefix="/reports")


@router.get("")
def index(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    today = date.today()
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "user": user, "active": "reports",
         "csrf_token": get_csrf_token(request),
         "year": today.year, "month": today.month},
    )


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/excel/monthly")
def excel_monthly(
    year: int, month: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    rows = db.execute(
        select(Transaction.category, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year, Transaction.month == month)
        .group_by(Transaction.category, Transaction.kind)
    ).all()
    expenses = [(c, v) for c, k, v in rows if k == "expense"]
    income = [(c, v) for c, k, v in rows if k == "income"]
    from app.services.reports import excel
    data = excel.monthly_xlsx(MONTHS[month], year, expenses, income)
    return _xlsx(data, f"Expenses_{MONTHS[month]}_{year}.xlsx")


@router.get("/excel/yearly")
def excel_yearly(
    year: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    rows = db.execute(
        select(Transaction.month, Transaction.kind, func.sum(Transaction.amount))
        .where(Transaction.year == year)
        .group_by(Transaction.month, Transaction.kind)
    ).all()
    exp = [0.0] * 12
    inc = [0.0] * 12
    for m, k, v in rows:
        (inc if k == "income" else exp)[m - 1] = float(v or 0)
    by_month = [[MONTHS[m + 1], inc[m], exp[m], inc[m] - exp[m]] for m in range(12)]

    cat = db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(Transaction.year == year, Transaction.kind == "expense")
        .group_by(Transaction.category).order_by(func.sum(Transaction.amount).desc())
    ).all()
    from app.services.reports import excel
    data = excel.yearly_xlsx(year, by_month, [(c, v) for c, v in cat])
    return _xlsx(data, f"Yearly_summary_{year}.xlsx")


def _pdf(data: bytes, filename: str) -> Response:
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/pdf/monthly")
def pdf_monthly(
    year: int, month: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    from app.services.reports import generate
    return _pdf(generate.monthly_pdf(db, year, month),
                f"Report_{MONTHS[month]}_{year}.pdf")


@router.get("/pdf/yearly")
def pdf_yearly(
    year: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    from app.services.reports import generate
    return _pdf(generate.yearly_pdf(db, year), f"Yearly_summary_{year}.pdf")


@router.get("/pdf/net-worth")
def pdf_net_worth(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    from app.services.reports import generate
    return _pdf(generate.net_worth_pdf(db), "Net_worth_report.pdf")
