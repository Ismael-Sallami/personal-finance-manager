"""Parser tests against small synthetic statements built in the test itself."""
from decimal import Decimal

from app.services.parsers import kraken, myinvestor
from app.services.parsers.dispatch import BROKERS, parse

KRAKEN_CSV = b"""txid,pair,time,type,ordertype,price,cost,fee,vol
1,BTC/EUR,2026-01-02,buy,market,40000,400.00,0.4,0.01
2,BTC/EUR,2026-02-02,buy,market,45000,450.00,0.4,0.01
3,BTC/EUR,2026-03-02,sell,market,50000,250.00,0.2,0.005
4,ADA/USD,2026-03-02,buy,market,0.5,50.00,0.1,100
"""

# MyInvestor exports latin1, ';' separated, with European decimals. Fund names
# come in upper case; anything else is a cash movement.
MYINVESTOR_CSV = (
    "Fecha;Concepto;Importe\n"
    "01/02/2026;VANGUARD GLOBAL STOCK @ 12,34;-1.000,00\n"
    "01/03/2026;VANGUARD GLOBAL STOCK @ 13,00;-500,00\n"
    "01/04/2026;Transferencia recibida;250,00\n"
    "01/05/2026;AMUNDI MSCI WORLD @ 9,10;-200,00\n"
    "01/06/2026;AMUNDI MSCI WORLD @ 9,90;220,00\n"
).encode("latin1")


def test_kraken_nets_buys_and_sells():
    r = kraken.parse(KRAKEN_CSV)
    assert r.broker == "kraken"
    btc = next(p for p in r.positions if p.asset == "BTC/EUR")
    # 400 + 450 - 250 committed, 0.015 coins left
    assert btc.invested == Decimal("600.00")
    assert btc.quantity == Decimal("0.01500000")


def test_kraken_keeps_usd_pairs():
    r = kraken.parse(KRAKEN_CSV)
    assert any(p.asset == "ADA/USD" for p in r.positions)


def test_kraken_reports_a_broken_file():
    r = kraken.parse(b"not a csv at all")
    assert r.positions == []
    assert r.warnings


def test_myinvestor_groups_by_fund():
    r = myinvestor.parse(MYINVESTOR_CSV)
    funds = {p.asset: p for p in r.positions}
    assert funds["VANGUARD GLOBAL STOCK"].invested == Decimal("1500.00")
    assert funds["AMUNDI MSCI WORLD"].withdrawn == Decimal("220.00")


def test_myinvestor_skips_cash_movements():
    r = myinvestor.parse(MYINVESTOR_CSV)
    assert not any("Transferencia" in p.asset for p in r.positions)


def test_myinvestor_flags_a_closed_fund():
    # 200 in, 220 out -> the fund looks sold
    r = myinvestor.parse(MYINVESTOR_CSV)
    amundi = next(p for p in r.positions if p.asset == "AMUNDI MSCI WORLD")
    assert amundi.looks_sold


def test_unknown_broker_does_not_raise():
    r = parse("not-a-broker", b"")
    assert r.positions == []
    assert r.warnings and "Unknown broker" in r.warnings[0]
    assert "kraken" in BROKERS
