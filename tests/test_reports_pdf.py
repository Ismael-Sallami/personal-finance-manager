from datetime import date
from decimal import Decimal

import pytest

from app.models import BankAccount, Transaction
from app.services.reports import charts, generate, pdf


def _tx(db, d, cat, kind, amount, origin="manual", merchant=None):
    db.add(Transaction(date=d, year=d.year, month=d.month, category=cat, kind=kind,
                       amount=Decimal(str(amount)), origin=origin, merchant=merchant))


@pytest.fixture
def data(db):
    for m, base in ((7, 100), (8, 110), (9, 120), (10, 130)):
        _tx(db, date(2026, m, 5), "Leisure", "expense", base, "bank", "Gym")
        _tx(db, date(2026, m, 3), "Salary", "income", 2000)
    _tx(db, date(2026, 10, 9), "Food", "expense", 300, "bank", "Shop")
    _tx(db, date(2026, 10, 12), "Food", "expense", -40, "bank", "Shop")  # refund
    _tx(db, date(2026, 10, 15), "Transfer", "transfer", 500, "bank")
    _tx(db, date(2026, 10, 20), "Investment", "expense", 200)
    for n, k, s in (("Bank", "account", 1500), ("Pot", "savings", 800),
                    ("Wallet", "cash", 50), ("Broker", "investment", 3000)):
        db.add(BankAccount(name=n, kind=k, balance=Decimal(s)))
    db.commit()


def _ok(b):
    assert b.startswith(b"%PDF") and len(b) > 1000


def _all(db):
    _ok(generate.monthly_pdf(db, 2026, 10))
    _ok(generate.monthly_pdf(db, 2026, 1))
    _ok(generate.yearly_pdf(db, 2026))
    _ok(generate.period_pdf(db, 2026, 7, 2026, 10, "Jul - Oct 2026"))
    _ok(generate.detailed_pdf(db, 2026, 7, 2026, 10, "Jul - Oct 2026"))
    _ok(generate.net_worth_pdf(db))


def test_pdfs_empty_db(db):
    _all(db)


def test_pdfs_with_data(db, data):
    _all(db)


def test_highlights_are_three_lines():
    ls = pdf.highlight_lines(900, 2000, [("Food", 500), ("Leisure", 100)],
                             [("Food", 300)], 800, 700)
    assert len(ls) == 3 and "Food" in ls[0] and "saved" in ls[2]
    assert len(pdf.highlight_lines(0, 0, [], [], None, None)) == 3


def test_group_ignores_negatives_and_zero():
    it = charts.group_items([("a", 10), ("b", -5), ("c", 0)] + [(f"x{i}", i + 1) for i in range(10)])
    assert all(v > 0 for _, v, _ in it) and it[-1][0] == "Other"
    assert charts.donut(["a"], [-1]).startswith(b"\x89PNG")
