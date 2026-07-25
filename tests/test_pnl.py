from decimal import Decimal

from app.services.pnl import return_pct, total_profit

D = Decimal


def test_myinvestor_simple():
    # invested 100, worth 130, nothing taken out -> +30
    assert total_profit("myinvestor", D("100"), D("0"), D("130")) == D("30.00")


def test_myinvestor_sold_is_not_counted_twice():
    # sold and withdrawn > 50% of invested -> value ignored -> profit = withdrawn - invested
    assert total_profit("myinvestor", D("100"), D("90"), D("90"), sold=True) == D("-10.00")


def test_kraken():
    assert total_profit("kraken", D("175.89"), D("0"), D("200")) == D("24.11")


def test_traderepublic_adds_realised_pnl():
    # (value 50 - cost 40) + realised 5 = 15
    assert total_profit("traderepublic", D("40"), D("0"), D("50"), D("5")) == D("15.00")


def test_return_percentage():
    assert return_pct(D("100"), D("25")) == D("25.00")
    assert return_pct(D("0"), D("25")) == D("0")
