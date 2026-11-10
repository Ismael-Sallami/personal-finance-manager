"""Insights: pace, comparison, merchants, subscriptions, unusual spending, budgets."""
from datetime import date
from decimal import Decimal

from app.models import Budget, Transaction
from app.services import insights as ins


def tx(db, day, amount, cat="Groceries", merchant="MERCADONA", kind="expense", origin="bank"):
    t = Transaction(date=day, year=day.year, month=day.month, category=cat,
                    kind=kind, amount=Decimal(str(amount)), merchant=merchant,
                    origin=origin)
    db.add(t)
    db.commit()
    return t


def test_pace_projects_and_excludes_investing(db):
    tx(db, date(2026, 11, 3), 100)
    tx(db, date(2026, 11, 8), 50)
    tx(db, date(2026, 11, 9), 500, cat="Investment")
    tx(db, date(2026, 10, 1), 900, origin="manual")
    r = ins.pace(db, date(2026, 11, 10))
    assert r.spent == Decimal("150")
    assert r.projection == Decimal("450.00")  # 150 / 10 * 30
    assert r.average == Decimal("900.00")


def test_comparison_same_day(db):
    tx(db, date(2026, 11, 5), 80, cat="Restaurants")
    tx(db, date(2026, 10, 5), 40, cat="Restaurants")
    tx(db, date(2026, 10, 20), 999, cat="Restaurants")  # after day 10: not counted
    rows = ins.comparison(db, date(2026, 11, 10))
    assert rows[0] == ("Restaurants", Decimal("80"), Decimal("40"))


def test_top_merchants(db):
    tx(db, date(2026, 11, 1), 10, merchant="A")
    tx(db, date(2026, 11, 2), 30, merchant="B")
    tx(db, date(2026, 11, 3), 15, merchant="A")
    assert ins.top_merchants(db, 2026, 11)[0] == ("B", Decimal("30"), 1)
    assert ins.top_merchants(db, 2026, 11)[1] == ("A", Decimal("25"), 2)


def test_is_subscription():
    d = date
    ok = [(d(2026, 8, 3), Decimal("12.99")), (d(2026, 9, 3), Decimal("12.99")),
          (d(2026, 10, 3), Decimal("13.49"))]
    assert ins.is_subscription(ok)
    assert not ins.is_subscription(ok[:2])
    irregular = [(d(2026, 8, 3), Decimal("10")), (d(2026, 8, 10), Decimal("10")),
                 (d(2026, 9, 3), Decimal("10"))]
    assert not ins.is_subscription(irregular)


def test_subscriptions_and_price_rise_alert(db):
    for month in (8, 9, 10):
        tx(db, date(2026, month, 3), 12.99, cat="Leisure", merchant="NETFLIX")
    new = tx(db, date(2026, 11, 3), 14.99, cat="Leisure", merchant="NETFLIX")
    subs = ins.subscriptions(db, date(2026, 11, 10))
    assert [s.merchant for s in subs] == ["NETFLIX"]
    assert "went up" in ins.subscription_alert(db, new)


def test_new_subscription_alert(db):
    tx(db, date(2026, 9, 3), 5, cat="Leisure", merchant="SPOTIFY")
    tx(db, date(2026, 10, 3), 5, cat="Leisure", merchant="SPOTIFY")
    t = tx(db, date(2026, 11, 3), 5, cat="Leisure", merchant="SPOTIFY")
    assert "New subscription" in ins.subscription_alert(db, t)


def test_unusual_alert(db):
    for day in (1, 2, 3):
        tx(db, date(2026, 11, day), 20)
    high = tx(db, date(2026, 11, 4), 200)
    assert "High spending" in ins.unusual_alert(db, high)
    new = tx(db, date(2026, 11, 5), 150, cat="Shopping", merchant="NEW SHOP")
    assert "First charge" in ins.unusual_alert(db, new)
    assert ins.unusual_alert(db, tx(db, date(2026, 11, 6), 21)) is None


def test_budget_alerts_once_per_threshold(db):
    db.add(Budget(category="Groceries", monthly_limit=Decimal("100")))
    db.commit()
    tx(db, date(2026, 11, 2), 85)
    today = date(2026, 11, 10)
    assert len(ins.budget_alerts(db, today)) == 1          # 80 %
    assert ins.budget_alerts(db, today) == []              # no repeat
    tx(db, date(2026, 11, 3), 20)
    assert "🔴" in ins.budget_alerts(db, today)[0]          # 100 %
    assert ins.budget_alerts(db, today) == []
