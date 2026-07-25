"""Investments: upload a statement -> preview and value -> save the P&L."""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_session
from app.models import BrokerImport, Investment, User
from app.security import get_csrf_token, validate_csrf
from app.services import contributions
from app.services.aggregation import latest_investments, snapshot_today
from app.services.parsers.broker_types import D
from app.services.parsers.dispatch import BROKERS, parse
from app.services.pnl import total_profit
from app.services.prices import crypto_symbol, refresh_all, refresh_position
from app.services.symbol_lookup import isin_to_yahoo
from app.templating import templates

router = APIRouter(prefix="/investments")

MAX_BYTES = 8 * 1024 * 1024  # 8 MB


def _num(raw, default="0") -> Decimal:
    """Form text to Decimal. Accepts a comma or a dot as decimal separator."""
    try:
        return Decimal(str(raw or default).strip().replace(",", "."))
    except Exception:
        return Decimal(default)


def _resolve_and_value(inv: Investment) -> None:
    """Find a symbol if there is none (crypto or ISIN -> OpenFIGI) and revalue.

    Without a symbol the typed value stays and only the profit is recomputed.
    """
    if not inv.yahoo_symbol:
        if inv.broker == "kraken" or crypto_symbol(inv.asset):
            inv.yahoo_symbol = crypto_symbol(inv.asset)
        elif inv.isin:
            inv.yahoo_symbol = isin_to_yahoo(inv.isin)
    inv.auto_value = bool(inv.yahoo_symbol)
    if not (inv.auto_value and refresh_position(inv)):
        inv.profit = total_profit(
            inv.broker, inv.invested, inv.withdrawn, inv.current_value,
            Decimal("0"), sold=False,
        )


@router.get("")
def index(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    positions = latest_investments(db)
    by_broker: dict[str, list[Investment]] = {}
    for p in positions:
        by_broker.setdefault(p.broker, []).append(p)

    totals = {
        "value": sum((p.current_value for p in positions), Decimal("0")),
        "invested": sum((p.invested for p in positions), Decimal("0")),
        "profit": sum((p.profit for p in positions), Decimal("0")),
    }
    return templates.TemplateResponse(
        "investments.html",
        {
            "request": request, "user": user, "active": "investments",
            "csrf_token": get_csrf_token(request),
            "brokers": BROKERS, "by_broker": by_broker, "totals": totals,
        },
    )


@router.post("/refresh")
def refresh(
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Pull fresh market prices for every position that has a symbol."""
    if validate_csrf(request, csrf_token):
        refresh_all(db)
    return RedirectResponse("/investments", status_code=303)


@router.post("/symbol/{inv_id}")
def set_symbol(
    inv_id: int,
    request: Request,
    yahoo_symbol: str = Form(""),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Set or clear the Yahoo symbol of a position."""
    if validate_csrf(request, csrf_token):
        inv = db.get(Investment, inv_id)
        if inv:
            inv.yahoo_symbol = (yahoo_symbol.strip() or None)
            inv.auto_value = bool(inv.yahoo_symbol)
            db.commit()
    return RedirectResponse("/investments", status_code=303)


@router.post("/position/add")
def position_add(
    request: Request,
    broker: str = Form(...),
    asset: str = Form(...),
    isin: str = Form(""),
    yahoo_symbol: str = Form(""),
    quantity: str = Form("0"),
    invested: str = Form("0"),
    current_value: str = Form(""),
    monthly_contribution: str = Form("0"),
    auto_price: str = Form(""),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Add a position by hand, without a statement.

    Automatic pricing is opt-in ("auto_price"), because it is only reliable for
    ETFs and crypto. With the box ticked, or with a symbol pasted in, the
    position is resolved and revalued; otherwise it stays manual and the value
    is whatever was typed (or the invested amount if left empty).
    """
    if not validate_csrf(request, csrf_token) or not asset.strip():
        return RedirectResponse("/investments", status_code=303)
    invested_amount = _num(invested)
    value_amount = _num(current_value) if current_value.strip() else invested_amount
    inv = Investment(
        broker=(broker.strip() or "other"), asset=asset.strip(),
        isin=(isin.strip() or None), yahoo_symbol=(yahoo_symbol.strip() or None),
        quantity=_num(quantity), invested=invested_amount, withdrawn=Decimal("0"),
        current_value=value_amount, profit=Decimal("0"),
        valued_on=date.today(), monthly_contribution=_num(monthly_contribution),
    )
    if auto_price.strip() or inv.yahoo_symbol:
        _resolve_and_value(inv)
    else:
        inv.auto_value = False
        inv.profit = total_profit(
            inv.broker, inv.invested, inv.withdrawn, inv.current_value,
            Decimal("0"), sold=False,
        )
    db.add(inv)
    db.commit()
    snapshot_today(db)
    return RedirectResponse("/investments", status_code=303)


@router.post("/position/{inv_id}/contribute")
def position_contribute(
    inv_id: int,
    request: Request,
    amount: str = Form("0"),
    units: str = Form("0"),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """One-off contribution: adds to the invested cost and, if needed, the value."""
    if validate_csrf(request, csrf_token):
        inv = db.get(Investment, inv_id)
        if inv:
            contributions.contribute(db, inv, _num(amount), _num(units))
            db.commit()
            snapshot_today(db)
    return RedirectResponse("/investments", status_code=303)


@router.post("/position/{inv_id}/edit")
def position_edit(
    inv_id: int,
    request: Request,
    asset: str = Form(...),
    quantity: str = Form("0"),
    invested: str = Form("0"),
    current_value: str = Form("0"),
    yahoo_symbol: str = Form(""),
    monthly_contribution: str = Form("0"),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Edit a position. Editing never re-resolves the ISIN: it only revalues when
    the user keeps a symbol, otherwise the typed value wins. Every edit feeds the
    evolution chart through today's snapshot."""
    if not validate_csrf(request, csrf_token):
        return RedirectResponse("/investments", status_code=303)
    inv = db.get(Investment, inv_id)
    if inv:
        inv.asset = asset.strip() or inv.asset
        inv.quantity = _num(quantity)
        inv.invested = _num(invested)
        inv.current_value = _num(current_value)
        inv.yahoo_symbol = (yahoo_symbol.strip() or None)
        inv.monthly_contribution = _num(monthly_contribution)
        inv.auto_value = bool(inv.yahoo_symbol)
        if not (inv.auto_value and refresh_position(inv)):
            inv.profit = total_profit(
                inv.broker, inv.invested, inv.withdrawn, inv.current_value,
                Decimal("0"), sold=False,
            )
        db.commit()
        snapshot_today(db)
    return RedirectResponse("/investments", status_code=303)


@router.post("/position/{inv_id}/delete")
def position_delete(
    inv_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if validate_csrf(request, csrf_token):
        inv = db.get(Investment, inv_id)
        if inv:
            db.delete(inv)
            db.commit()
    return RedirectResponse("/investments", status_code=303)


@router.post("/upload")
async def upload(
    request: Request,
    broker: str = Form(...),
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if not validate_csrf(request, csrf_token) or broker not in BROKERS:
        return RedirectResponse("/investments", status_code=303)

    content = await file.read()
    if len(content) > MAX_BYTES:
        return templates.TemplateResponse(
            "investments_preview.html",
            {"request": request, "user": user, "active": "investments",
             "csrf_token": get_csrf_token(request), "broker": broker,
             "positions": [], "warnings": ["File too big (8 MB max)."],
             "filename": file.filename},
            status_code=400,
        )

    result = parse(broker, content)
    return templates.TemplateResponse(
        "investments_preview.html",
        {
            "request": request, "user": user, "active": "investments",
            "csrf_token": get_csrf_token(request),
            "broker": broker, "positions": result.positions,
            "warnings": result.warnings, "filename": file.filename,
        },
    )


@router.post("/save")
async def save(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    form = await request.form()
    if not validate_csrf(request, form.get("csrf_token")):
        return RedirectResponse("/investments", status_code=303)

    broker = form.get("broker", "")
    if broker not in BROKERS:
        return RedirectResponse("/investments", status_code=303)

    filename = form.get("filename") or None
    n = int(form.get("n", 0))
    imp = BrokerImport(broker=broker, source_file=filename)
    db.add(imp)
    db.flush()

    today = date.today()
    created = 0
    for i in range(n):
        asset = form.get(f"asset_{i}")
        if not asset:
            continue
        invested = D(form.get(f"invested_{i}", 0))
        withdrawn = D(form.get(f"withdrawn_{i}", 0))
        realised = D(form.get(f"pnl_{i}", 0))
        quantity_raw = form.get(f"quantity_{i}") or "0"
        current_value = D((form.get(f"value_{i}") or "0").replace(",", "."))
        sold = form.get(f"sold_{i}") == "on"

        profit = total_profit(broker, invested, withdrawn, current_value, realised, sold)
        try:
            quantity = Decimal(str(quantity_raw))
        except Exception:
            quantity = Decimal("0")

        db.add(Investment(
            broker=broker, asset=asset, isin=(form.get(f"isin_{i}") or None),
            quantity=quantity, invested=invested, current_value=current_value,
            withdrawn=withdrawn, profit=profit, valued_on=today,
            import_id=imp.id,
        ))
        created += 1

    # No rows saved: drop the empty import so it does not clutter the history.
    if created == 0:
        db.delete(imp)
    db.commit()
    return RedirectResponse("/investments", status_code=303)
