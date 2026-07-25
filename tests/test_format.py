"""Number formatting: money and the "no fake 0%" rule."""
from app.format import money, pct


def test_money_groups_thousands():
    assert money(1234.5).startswith("1,234.50")
    assert money(None) == "-"


def test_whole_percentages():
    assert pct(45.0) == "45%"          # >= 0.5 -> plain integer
    assert pct(0) == "0%"              # a real zero stays 0%


def test_small_value_is_not_a_fake_zero():
    assert pct(0.4) == "0.400%"        # would round to 0% -> 3 decimals
    assert pct(0.012) == "0.012%"


def test_explicit_decimals():
    assert pct(12.34, decimals=2) == "12.34%"
    assert pct(0.004, decimals=2) == "0.004%"  # 0.00% would be a fake zero


def test_invalid_value_does_not_break():
    assert pct(None) == "0%"
    assert pct("abc") == "abc"
