from datetime import date
from decimal import Decimal

from app.models import Investment, Transaction
from app.services.aggregation import build_dashboard, latest_investments


def test_net_worth_is_the_investment_value(db):
    db.add(Investment(
        broker="kraken", asset="BTC/EUR", invested=Decimal("100"),
        current_value=Decimal("150"), withdrawn=Decimal("0"),
        profit=Decimal("50"), valued_on=date(2026, 6, 1),
    ))
    db.add(Investment(
        broker="myinvestor", asset="Fund X", invested=Decimal("400"),
        current_value=Decimal("450"), withdrawn=Decimal("0"),
        profit=Decimal("50"), valued_on=date(2026, 6, 1),
    ))
    db.commit()

    data = build_dashboard(db, 2026, 6)
    # net worth = 150 + 450 = 600
    assert data["kpis"]["net_worth"] == 600.0
    assert data["kpis"]["inv_value"] == 600.0
    assert data["kpis"]["inv_profit"] == 100.0
    assert data["kpis"]["return_pct"] == 20.0  # 100/500
    assert data["has_data"] is True


def test_month_kpis(db):
    db.add(Transaction(date=date(2026, 6, 2), year=2026, month=6,
                       category="Groceries", kind="expense", amount=Decimal("30")))
    db.add(Transaction(date=date(2026, 6, 3), year=2026, month=6,
                       category="Salary", kind="income", amount=Decimal("1000")))
    db.add(Transaction(date=date(2026, 5, 3), year=2026, month=5,
                       category="Other", kind="expense", amount=Decimal("999")))
    db.commit()

    data = build_dashboard(db, 2026, 6)
    assert data["kpis"]["month_expense"] == 30.0
    assert data["kpis"]["month_income"] == 1000.0
    assert data["kpis"]["month_saved"] == 970.0
    # cashflow of the year: June 30, May 999
    assert data["charts"]["cashflow"]["expenses"][5] == 30.0
    assert data["charts"]["cashflow"]["expenses"][4] == 999.0


def test_latest_investments_per_broker(db):
    db.add(Investment(broker="kraken", asset="BTC/EUR", current_value=Decimal("100"),
                      valued_on=date(2026, 5, 1)))
    db.add(Investment(broker="kraken", asset="BTC/EUR", current_value=Decimal("200"),
                      valued_on=date(2026, 6, 1)))
    db.commit()
    invs = latest_investments(db)
    # only the most recent valuation survives
    assert len(invs) == 1
    assert invs[0].current_value == Decimal("200")
