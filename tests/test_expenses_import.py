"""Text import flow: preview -> save (through TestClient)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import require_user
from app.db import Base, get_session
from app.main import app
from app.models import Transaction, User
import app.routers.expenses as expenses_router


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection: TestClient runs in another thread
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(email="t@t.com", password_hash="x")
    db.add(user)
    db.commit()

    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[require_user] = lambda: user
    # No previous session holds a CSRF token in tests, so accept it.
    monkeypatch.setattr(expenses_router, "validate_csrf", lambda *a, **k: True)

    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_preview_lists_rows(client):
    c, _ = client
    text = "June\n- Expense: Groceries: 8,53+1+26,25\n- Income: Salary: 1200\n- noise with no format"
    r = c.post("/expenses/import/preview",
               data={"text": text, "year": 2026, "month": 6, "csrf_token": "x"})
    assert r.status_code == 200
    assert "Groceries" in r.text
    assert "Salary" in r.text
    assert "noise with no format" in r.text  # skipped line is reported back


def test_save_inserts_only_included_rows(client):
    c, db = client
    data = {
        "csrf_token": "x", "year": 2026, "month": 6, "n": 2,
        "include_0": "on", "kind_0": "expense", "category_0": "Electricity", "amount_0": "60,00",
        # row 1 has no include_1 -> not saved
        "kind_1": "income", "category_1": "Bonus", "amount_1": "100",
    }
    r = c.post("/expenses/import/save", data=data, follow_redirects=False)
    assert r.status_code == 303
    rows = db.scalars(select(Transaction)).all()
    assert len(rows) == 1
    assert rows[0].category == "Electricity"
    assert rows[0].kind == "expense"
    assert rows[0].month == 6 and rows[0].year == 2026
    assert str(rows[0].amount) == "60.00"


def _save(c, **extra):
    data = {"csrf_token": "x", "year": 2026, "month": 6, "n": 1,
            "include_0": "on", "kind_0": "expense", "category_0": "Electricity", "amount_0": "60,00"}
    data.update(extra)
    return c.post("/expenses/import/save", data=data, follow_redirects=False)


def test_append_mode_adds_up(client):
    c, db = client
    _save(c, mode="append")
    _save(c, mode="append")
    rows = db.scalars(select(Transaction)).all()
    assert len(rows) == 2


def test_replace_mode_wipes_that_month_only(client):
    c, db = client
    from datetime import date
    from decimal import Decimal
    # existing row in the same month (e.g. added by hand)
    db.add(Transaction(date=date(2026, 6, 1), year=2026, month=6,
                       category="Old", kind="expense", amount=Decimal("5")))
    # row in another month that must not be touched
    db.add(Transaction(date=date(2026, 5, 1), year=2026, month=5,
                       category="May", kind="expense", amount=Decimal("9")))
    db.commit()

    _save(c, mode="replace")
    rows = db.scalars(select(Transaction).where(Transaction.year == 2026,
                                                Transaction.month == 6)).all()
    assert len(rows) == 1
    assert rows[0].category == "Electricity"  # "Old" is gone
    may = db.scalars(select(Transaction).where(Transaction.month == 5)).all()
    assert len(may) == 1
