"""Backup: the dump, the round trip, and what must never be exported."""
import gzip
import json
from datetime import date
from decimal import Decimal

from app.models import BankAccount, Investment, Transaction, User
from app.security import hash_password
from app.services.backup import dump_gzip, export_data, summary


def _seed(db):
    db.add(User(email="a@b.c", password_hash=hash_password("secret")))
    db.add(Transaction(date=date(2026, 3, 4), year=2026, month=3, kind="expense",
                       category="Groceries", note="shopping", amount=Decimal("40.55")))
    db.add(BankAccount(name="Main bank", balance=Decimal("1234.56")))
    db.add(Investment(asset="MSCI World", broker="myinvestor",
                      invested=Decimal("500.00"), current_value=Decimal("512.30"),
                      valued_on=date(2026, 3, 4)))
    db.commit()


def test_export_contains_the_rows(db):
    _seed(db)
    data = export_data(db)
    assert len(data["tables"]["transactions"]) == 1
    assert len(data["tables"]["bank_accounts"]) == 1
    assert len(data["tables"]["investments"]) == 1


def test_decimals_are_text_so_nothing_is_lost(db):
    _seed(db)
    data = export_data(db)
    assert data["tables"]["bank_accounts"][0]["balance"] == "1234.56"
    assert data["tables"]["transactions"][0]["amount"] == "40.55"


def test_dates_are_iso(db):
    _seed(db)
    data = export_data(db)
    assert data["tables"]["transactions"][0]["date"] == "2026-03-04"


def test_the_password_hash_never_leaves(db):
    _seed(db)
    data = export_data(db)
    user = data["tables"]["users"][0]
    assert "password_hash" not in user
    assert user["email"] == "a@b.c"
    # And not through any other field either: no hash anywhere in the JSON.
    assert "$2b$" not in json.dumps(data)


def test_dump_gzip_is_valid_json_and_dated(db):
    _seed(db)
    content, name = dump_gzip(db)
    assert name == f"finance_backup_{date.today().isoformat()}.json.gz"
    data = json.loads(gzip.decompress(content))
    assert data["tables"]["transactions"][0]["note"] == "shopping"


def test_summary_lists_tables_that_have_rows(db):
    _seed(db)
    text = summary(export_data(db))
    assert "transactions: 1" in text
    assert "categories" not in text  # empty tables are left out


def test_an_empty_database_does_not_break(db):
    data = export_data(db)
    assert summary(data) == "no data"
    content, _ = dump_gzip(db)
    assert json.loads(gzip.decompress(content))["tables"]["transactions"] == []
