"""Tests for the paste parser and its safe arithmetic."""
from decimal import Decimal

import pytest

from app.services.expenses_parse import (
    ExprError,
    parse_line,
    parse_text,
    safe_eval,
)


# --- safe_eval ---
def test_add_and_subtract():
    assert safe_eval("15-1+3") == Decimal("17.00")


def test_comma_as_decimal_separator():
    assert safe_eval("8,53+1+26,25") == Decimal("35.78")


def test_multiply_and_parentheses():
    assert safe_eval("(10+5)*2") == Decimal("30.00")
    assert safe_eval("12.5*2") == Decimal("25.00")


def test_divide():
    assert safe_eval("100/4") == Decimal("25.00")


def test_unary_sign():
    assert safe_eval("-5+10") == Decimal("5.00")


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1+", "2**9999", "__import__('os')",
                                 "1;2", "x+1", "10/0"])
def test_invalid_expressions(bad):
    with pytest.raises(ExprError):
        safe_eval(bad)


def test_power_is_blocked():
    # ** is not in the operator allowlist
    with pytest.raises(ExprError):
        safe_eval("2**10")


def test_expression_too_long():
    with pytest.raises(ExprError):
        safe_eval("1+" * 100 + "1")


# --- parse_line ---
def test_line_with_kind_and_category():
    it = parse_line("Expense: Groceries: 15-1+3")
    assert it.kind == "expense"
    assert it.category == "Groceries"
    assert it.amount == Decimal("17.00")


def test_line_income():
    it = parse_line("Income: Salary: 1200")
    assert it.kind == "income"
    assert it.amount == Decimal("1200.00")


def test_line_without_kind_uses_default():
    it = parse_line("Food: 10+5", default_kind="expense")
    assert it.kind == "expense"
    assert it.category == "Food"
    assert it.amount == Decimal("15.00")


def test_invalid_line_returns_none():
    assert parse_line("just text with no colon") is None
    assert parse_line("Cat: abc") is None


# --- parse_text (a whole pasted block) ---
def test_block_with_mixed_bullets():
    txt = """June
- Expense: Groceries: 15-1+3
• Food: 8,53+1+26,25
  * Income: Salary: 1200
– Transport: (10+5)*2
noise that gets skipped
"""
    r = parse_text(txt)
    assert r.month == 6
    assert len(r.items) == 4
    groceries = next(i for i in r.items if i.category == "Groceries")
    assert groceries.amount == Decimal("17.00")
    salary = next(i for i in r.items if i.category == "Salary")
    assert salary.kind == "income" and salary.amount == Decimal("1200.00")
    assert "noise that gets skipped" in r.ignored


def test_without_month_title():
    r = parse_text("- Food: 10")
    assert r.month is None
    assert len(r.items) == 1
