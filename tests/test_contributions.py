"""Contribution tests: contribute() and apply_monthly_contributions()."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Investment
from app.services import contributions


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _inv(**kw):
    base = dict(broker="myinvestor", asset="Fund", quantity=Decimal("0"),
                invested=Decimal("0"), current_value=Decimal("0"),
                withdrawn=Decimal("0"), profit=Decimal("0"),
                auto_value=False, monthly_contribution=Decimal("0"),
                valued_on=date.today())
    base.update(kw)
    return Investment(**base)


def test_manual_contribution_adds_to_invested_and_value(db):
    inv = _inv(invested=Decimal("1000"), current_value=Decimal("1100"))
    db.add(inv)
    db.commit()
    contributions.contribute(db, inv, Decimal("100"))
    db.commit()
    assert inv.invested == Decimal("1100")
    assert inv.current_value == Decimal("1200")
    # myinvestor: profit = (value + withdrawn) - invested = 1200 - 1100 = 100
    assert inv.profit == Decimal("100.00")


def test_contribution_with_units(db):
    inv = _inv(invested=Decimal("1000"), current_value=Decimal("1000"),
               quantity=Decimal("10"))
    db.add(inv)
    db.commit()
    contributions.contribute(db, inv, Decimal("100"), units=Decimal("0.5"))
    assert inv.quantity == Decimal("10.5")
    assert inv.invested == Decimal("1100")


def test_auto_valued_position_uses_the_market_price(db, monkeypatch):
    inv = _inv(invested=Decimal("1000"), current_value=Decimal("1000"),
               quantity=Decimal("10"), auto_value=True, yahoo_symbol="X")
    db.add(inv)
    db.commit()

    def fake_refresh(i):
        i.current_value = Decimal("1500")
        i.profit = Decimal("400")
        return True
    monkeypatch.setattr(contributions, "refresh_position", fake_refresh)

    contributions.contribute(db, inv, Decimal("100"))
    assert inv.invested == Decimal("1100")
    assert inv.current_value == Decimal("1500")  # from the price, amount not added


def test_apply_monthly_contributions(db):
    a = _inv(asset="With DCA", invested=Decimal("500"), current_value=Decimal("500"),
             monthly_contribution=Decimal("100"))
    b = _inv(asset="Without DCA", invested=Decimal("500"), current_value=Decimal("500"),
             monthly_contribution=Decimal("0"))
    db.add_all([a, b])
    db.commit()

    n = contributions.apply_monthly_contributions(db)
    assert n == 1
    db.refresh(a)
    db.refresh(b)
    assert a.invested == Decimal("600")  # +100
    assert b.invested == Decimal("500")  # untouched
