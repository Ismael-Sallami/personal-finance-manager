"""Manual position CRUD (symbol and price mocked, in-memory DB)."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import require_user
from app.db import Base, get_session
from app.main import app
from app.models import Investment, User
import app.routers.investments as inv_router


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(email="t@t.com", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[require_user] = lambda: user
    monkeypatch.setattr(inv_router, "validate_csrf", lambda *a, **k: True)
    # No network: ISIN lookup and price refresh are stubbed.
    monkeypatch.setattr(inv_router, "isin_to_yahoo", lambda isin: "SXR8.DE")
    monkeypatch.setattr(inv_router, "refresh_position", lambda inv: False)
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_add_resolves_the_isin(client):
    c, db = client
    # Auto price is opt-in, so the box has to be ticked to resolve the ISIN.
    r = c.post("/investments/position/add", data={
        "csrf_token": "x", "broker": "traderepublic", "asset": "iShares S&P500",
        "isin": "IE00B5BMR087", "quantity": "2", "invested": "100,00",
        "auto_price": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    inv = db.scalars(select(Investment)).one()
    assert inv.yahoo_symbol == "SXR8.DE"
    assert inv.auto_value is True
    assert inv.invested == Decimal("100.00")


def test_add_maps_crypto_by_itself(client):
    c, db = client
    c.post("/investments/position/add", data={
        "csrf_token": "x", "broker": "kraken", "asset": "BTC/EUR",
        "quantity": "0.1", "invested": "100", "auto_price": "on",
    })
    inv = db.scalars(select(Investment)).one()
    assert inv.yahoo_symbol == "BTC-EUR"


def test_a_fund_stays_manual_by_default(client):
    # Without ticking auto price the position stays manual, ISIN or not.
    c, db = client
    c.post("/investments/position/add", data={
        "csrf_token": "x", "broker": "myinvestor", "asset": "Fidelity MSCI World",
        "isin": "IE00BYX5NX33", "invested": "1000", "current_value": "1000",
    })
    inv = db.scalars(select(Investment)).one()
    assert inv.auto_value is False
    assert inv.yahoo_symbol is None
    assert inv.current_value == Decimal("1000")


def test_without_a_symbol_the_typed_value_is_kept(client, monkeypatch):
    monkeypatch.setattr(inv_router, "isin_to_yahoo", lambda isin: None)
    c, db = client
    c.post("/investments/position/add", data={
        "csrf_token": "x", "broker": "myinvestor", "asset": "Unlisted fund",
        "isin": "ES0000000000", "quantity": "5", "invested": "200",
    })
    inv = db.scalars(select(Investment)).one()
    assert inv.yahoo_symbol is None
    assert inv.auto_value is False
    assert inv.current_value == Decimal("200")


def test_editing_the_value_recomputes_the_profit(client):
    c, db = client
    from datetime import date
    inv = Investment(broker="myinvestor", asset="X", quantity=Decimal("1"),
                     invested=Decimal("100"), current_value=Decimal("100"),
                     withdrawn=Decimal("0"), profit=Decimal("0"),
                     valued_on=date.today())
    db.add(inv)
    db.commit()
    c.post(f"/investments/position/{inv.id}/edit", data={
        "csrf_token": "x", "asset": "X", "quantity": "1",
        "invested": "100", "current_value": "130", "yahoo_symbol": "",
    })
    db.refresh(inv)
    assert inv.current_value == Decimal("130")
    assert inv.profit == Decimal("30.00")  # myinvestor: (130+0)-100


def test_delete(client):
    c, db = client
    from datetime import date
    inv = Investment(broker="kraken", asset="BTC/EUR", quantity=Decimal("1"),
                     invested=Decimal("10"), current_value=Decimal("10"),
                     withdrawn=Decimal("0"), profit=Decimal("0"),
                     valued_on=date.today())
    db.add(inv)
    db.commit()
    c.post(f"/investments/position/{inv.id}/delete", data={"csrf_token": "x"})
    assert db.scalars(select(Investment)).all() == []
