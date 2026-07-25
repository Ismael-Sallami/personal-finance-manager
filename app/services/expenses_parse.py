"""Parser for pasted expense/income lists (phone notes or markdown style).

Accepts multi-line text with bullets (-, *, •, ‣, ◦, –, —, ·) and a small
arithmetic expression per line. Valid examples::

    June
    - Expense: Groceries: 15-1+3          -> 17.00
    - Income: Salary: 1200
    • Food: 8.53+1+26.25                  -> default kind (expense)
    Transport: (10+5)*2                   -> 30.00

Line format (case insensitive), fields separated by ':' ::
    [Expense|Income]: Category: <expr>
    Category: <expr>                      (kind = default_kind)

The arithmetic is NOT evaluated with eval(): the expression is parsed with ast
and only numbers and + - * / // % and parentheses are allowed (no powers, no
names, no calls).
"""
from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MONTHS_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Bullet characters stripped from the start of each line.
_BULLET_RE = re.compile(r"^[\s\-\*•‣◦▪·–—]+")
# Digits, basic arithmetic operators, dot/comma and parentheses only.
_EXPR_ALLOWED = re.compile(r"^[0-9.,\s+\-*/%()]+$")

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

MAX_EXPR_LEN = 120


class ExprError(ValueError):
    pass


def safe_eval(expr: str) -> Decimal:
    """Evaluate a simple arithmetic expression safely -> Decimal with 2 places."""
    raw = (expr or "").strip()
    if not raw:
        raise ExprError("empty expression")
    if len(raw) > MAX_EXPR_LEN:
        raise ExprError("expression too long")

    # Accept a comma as decimal separator: "8,53" -> "8.53". This format has no
    # thousands separator, so every comma is decimal.
    norm = raw.replace(",", ".")
    if not _EXPR_ALLOWED.match(norm):
        raise ExprError(f"character not allowed in: {raw!r}")

    try:
        tree = ast.parse(norm, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"invalid syntax: {raw!r}") from e

    value = _eval_node(tree.body)
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation as e:
        raise ExprError(f"invalid result: {raw!r}") from e


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExprError("numbers only")
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ExprError("operator not allowed")  # ** lands here
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
            raise ExprError("division by zero")
        return op(left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = _eval_node(node.operand)
        return val if isinstance(node.op, ast.UAdd) else -val
    raise ExprError("expression not allowed")


@dataclass
class ParsedItem:
    kind: str          # expense | income
    category: str
    amount: Decimal
    note: str | None = None


@dataclass
class ParsedExpenses:
    title: str
    month: int | None
    items: list[ParsedItem]
    ignored: list[str] = field(default_factory=list)


def _month_from_title(title: str) -> int | None:
    clean = re.sub(r"[^a-zA-Z]", "", title).lower()
    for name, num in MONTHS_MAP.items():
        if clean.startswith(name):
            return num
    return None


def _clean_line(line: str) -> str:
    return _BULLET_RE.sub("", line).strip()


def _parse_kind(token: str, default_kind: str) -> str:
    t = token.strip().lower()
    if t in ("income", "incomes", "in", "+"):
        return "income"
    if t in ("expense", "expenses", "out", "-"):
        return "expense"
    return default_kind


def parse_line(line: str, default_kind: str = "expense") -> ParsedItem | None:
    """Parse one line with the bullet already stripped. None if it is not an item."""
    if not line or ":" not in line:
        return None
    parts = [p.strip() for p in line.split(":")]
    # Last part is the expression; everything before it is a label.
    *labels, expr_raw = parts
    if not labels:
        return None

    kind = default_kind
    category: str
    if len(labels) >= 2 and _parse_kind(labels[0], "") in ("expense", "income"):
        kind = _parse_kind(labels[0], default_kind)
        category = ": ".join(labels[1:]).strip()
    else:
        # The first token is a kind only if it matches; otherwise it is the category.
        maybe = _parse_kind(labels[0], "")
        if maybe and len(labels) == 1:
            return None  # "Expense: 15" with no category -> ignore
        category = ": ".join(labels).strip()

    if not category:
        return None
    try:
        amount = safe_eval(expr_raw)
    except ExprError:
        return None
    return ParsedItem(kind=kind, category=category, amount=amount)


def parse_text(text: str, default_kind: str = "expense") -> ParsedExpenses:
    """Parse a whole block. The first line may be the name of the month."""
    lines = list((text or "").splitlines())
    # Look for a month title in the first non-empty line.
    title = ""
    month = None
    start = 0
    for idx, line in enumerate(lines):
        cl = _clean_line(line)
        if not cl:
            continue
        m = _month_from_title(cl)
        if m and ":" not in cl:
            title, month, start = cl, m, idx + 1
        break

    items: list[ParsedItem] = []
    ignored: list[str] = []
    for line in lines[start:]:
        cl = _clean_line(line)
        if not cl:
            continue
        item = parse_line(cl, default_kind)
        if item:
            items.append(item)
        else:
            ignored.append(cl)

    return ParsedExpenses(title=title, month=month, items=items, ignored=ignored)
