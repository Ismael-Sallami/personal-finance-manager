"""Revaluation tests (yfinance mocked, no network)."""
from datetime import date
from decimal import Decimal

from app.models import Investment
from app.services import prices


def test_crypto_symbol():
    assert prices.crypto_symbol("BTC/EUR") == "BTC-EUR"
    assert prices.crypto_symbol("ETH/EUR") == "ETH-EUR"
    assert prices.crypto_symbol("FOO/EUR") is None


def test_refresh_position_computes_the_value(monkeypatch):
    monkeypatch.setattr(prices, "fetch_price", lambda s: Decimal("50000"))
    inv = Investment(
        broker="kraken", asset="BTC/EUR", quantity=Decimal("0.5"),
        invested=Decimal("20000"), current_value=Decimal("0"),
        withdrawn=Decimal("0"), profit=Decimal("0"),
        valued_on=date(2026, 6, 1), auto_value=True,
    )
    changed = prices.refresh_position(inv)
    assert changed is True
    assert inv.yahoo_symbol == "BTC-EUR"
    assert inv.current_price == Decimal("50000")
    assert inv.current_value == Decimal("25000.00")
    assert inv.profit == Decimal("5000.00")  # 25000 - 20000


def test_without_a_symbol_nothing_changes(monkeypatch):
    monkeypatch.setattr(prices, "fetch_price", lambda s: Decimal("99"))
    inv = Investment(
        broker="myinvestor", asset="Unlisted fund", quantity=Decimal("10"),
        invested=Decimal("100"), current_value=Decimal("120"),
        withdrawn=Decimal("0"), profit=Decimal("20"),
        valued_on=date(2026, 6, 1), auto_value=True,
    )
    # myinvestor with no yahoo_symbol is not revalued
    assert prices.refresh_position(inv) is False
    assert inv.current_value == Decimal("120")


def test_an_unavailable_price_changes_nothing(monkeypatch):
    monkeypatch.setattr(prices, "fetch_price", lambda s: None)
    inv = Investment(
        broker="kraken", asset="BTC/EUR", quantity=Decimal("1"),
        invested=Decimal("100"), current_value=Decimal("100"),
        withdrawn=Decimal("0"), profit=Decimal("0"),
        valued_on=date(2026, 6, 1), auto_value=True,
    )
    assert prices.refresh_position(inv) is False


def test_refresh_all(db, monkeypatch):
    monkeypatch.setattr(prices, "fetch_price", lambda s: Decimal("2"))
    db.add(Investment(
        broker="kraken", asset="ETH/EUR", quantity=Decimal("3"),
        invested=Decimal("4"), current_value=Decimal("0"),
        withdrawn=Decimal("0"), profit=Decimal("0"),
        valued_on=date(2026, 6, 1), auto_value=True,
    ))
    db.commit()
    res = prices.refresh_all(db)
    assert res["updated"] == 1
    inv = db.query(Investment).first()
    assert inv.current_value == Decimal("6.00")
