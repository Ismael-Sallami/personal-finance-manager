"""Bank accounts, savings and cash.

Add as many accounts as you want with their balance; the page shows the total
and the breakdown. Together with the investments they make up net worth, so
every balance change writes today's snapshot for the evolution chart.
"""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_session
from app.models import BankAccount, User
from app.security import get_csrf_token, validate_csrf
from app.services.aggregation import snapshot_today
from app.templating import templates

router = APIRouter(prefix="/banks")

KINDS = ["account", "savings", "cash"]


def _num(raw, default="0") -> Decimal:
    """Form text to Decimal. Accepts a comma or a dot as decimal separator."""
    try:
        return Decimal(str(raw or default).strip().replace(",", "."))
    except Exception:
        return Decimal(default)


@router.get("")
def index(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    accounts = db.scalars(select(BankAccount).order_by(BankAccount.balance.desc())).all()
    total = sum((a.balance for a in accounts), Decimal("0"))
    return templates.TemplateResponse(
        "banks.html",
        {
            "request": request, "user": user, "active": "banks",
            "csrf_token": get_csrf_token(request),
            "accounts": accounts, "total": total, "kinds": KINDS,
        },
    )


@router.post("/add")
def add(
    request: Request,
    name: str = Form(...),
    kind: str = Form("account"),
    balance: str = Form("0"),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if validate_csrf(request, csrf_token) and name.strip():
        db.add(BankAccount(
            name=name.strip(),
            kind=(kind if kind in KINDS else "account"),
            balance=_num(balance),
        ))
        db.commit()
        snapshot_today(db)
    return RedirectResponse("/banks", status_code=303)


@router.post("/{account_id}/edit")
def edit(
    account_id: int,
    request: Request,
    name: str = Form(...),
    kind: str = Form("account"),
    balance: str = Form("0"),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if validate_csrf(request, csrf_token):
        account = db.get(BankAccount, account_id)
        if account:
            account.name = name.strip() or account.name
            account.kind = kind if kind in KINDS else account.kind
            account.balance = _num(balance)
            account.updated_at = datetime.now(timezone.utc)
            db.commit()
            snapshot_today(db)
    return RedirectResponse("/banks", status_code=303)


@router.post("/{account_id}/delete")
def delete(
    account_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if validate_csrf(request, csrf_token):
        account = db.get(BankAccount, account_id)
        if account:
            db.delete(account)
            db.commit()
            snapshot_today(db)
    return RedirectResponse("/banks", status_code=303)
