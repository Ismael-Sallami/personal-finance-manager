"""Bank account CRUD and the consolidated net worth (in-memory DB)."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import require_user
from app.db import Base, get_session
from app.main import app
from app.models import BankAccount, Investment, User
import app.routers.banks as banks_router


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(email="t@t.com", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[require_user] = lambda: user
    monkeypatch.setattr(banks_router, "validate_csrf", lambda *a, **k: True)
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_add_account(client):
    c, db = client
    r = c.post("/banks/add", data={"csrf_token": "x", "name": "Main bank",
                                   "kind": "account", "balance": "1500,50"},
               follow_redirects=False)
    assert r.status_code == 303
    account = db.scalars(select(BankAccount)).one()
    assert account.name == "Main bank"
    assert account.balance == Decimal("1500.50")


def test_edit_and_total(client):
    c, db = client
    c.post("/banks/add", data={"csrf_token": "x", "name": "Main bank", "balance": "1500"})
    c.post("/banks/add", data={"csrf_token": "x", "name": "Cash",
                               "kind": "cash", "balance": "200"})
    total = db.scalar(select(func.sum(BankAccount.balance)))
    assert total == Decimal("1700")
    account = db.scalars(select(BankAccount).where(BankAccount.name == "Main bank")).one()
    c.post(f"/banks/{account.id}/edit", data={"csrf_token": "x", "name": "Main bank",
                                              "kind": "account", "balance": "1600"})
    db.refresh(account)
    assert account.balance == Decimal("1600")


def test_delete(client):
    c, db = client
    c.post("/banks/add", data={"csrf_token": "x", "name": "X", "balance": "10"})
    account = db.scalars(select(BankAccount)).one()
    c.post(f"/banks/{account.id}/delete", data={"csrf_token": "x"})
    assert db.scalars(select(BankAccount)).all() == []


def test_net_worth_adds_investments_and_banks(client):
    c, db = client
    db.add(Investment(broker="myinvestor", asset="Fund", quantity=Decimal("1"),
                      invested=Decimal("1000"), current_value=Decimal("1200"),
                      withdrawn=Decimal("0"), profit=Decimal("200"),
                      valued_on=date.today()))
    db.add(BankAccount(name="Cash", kind="cash", balance=Decimal("300")))
    db.commit()
    from app.services.aggregation import build_dashboard, total_banks
    data = build_dashboard(db)
    assert total_banks(db) == 300.0
    assert data["kpis"]["bank_balance"] == 300.0
    assert data["kpis"]["inv_value"] == 1200.0
    assert data["kpis"]["net_worth"] == 1500.0  # 1200 + 300


def test_snapshot_today_is_an_upsert(client):
    c, db = client
    db.add(BankAccount(name="X", balance=Decimal("100")))
    db.commit()
    from app.models import NetWorthSnapshot
    from app.services.aggregation import snapshot_today
    snapshot_today(db)
    snapshot_today(db)  # second call updates the same row
    snaps = db.scalars(select(NetWorthSnapshot)).all()
    assert len(snaps) == 1
    assert snaps[0].total == Decimal("100.00")
