import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.models import CategoryRule, Transaction

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import merge_categories as mc  # noqa: E402


def _add(db, cat, kind="expense"):
    db.add(Transaction(date=date(2026, 11, 1), year=2026, month=11, category=cat, kind=kind,
                       amount=Decimal("5")))


def test_plan_renames_rows_and_rules_ignoring_case_and_accents(db):
    _add(db, "food")
    _add(db, "Supermarket")
    _add(db, "Groceries")
    _add(db, "Payroll", "income")
    _add(db, "Food", "income")  # other kind: not touched by the expense mapping
    db.add(CategoryRule(pattern="lidl", category="Food"))
    db.commit()
    mapping = {"expense": {"Groceries": ["Food", "Supermarket"]},
               "income": {"Salary": ["payroll"]}}
    changes = mc.plan(db, mapping)
    assert sorted((k, f) for k, _, f in changes) == [
        ("expense", "Groceries"), ("expense", "Groceries"),
        ("income", "Salary"), ("rule", "Groceries")]
    for _, row, final in changes:
        row.category = final
    db.commit()
    cats = {(t.kind, t.category) for t in db.scalars(select(Transaction))}
    assert cats == {("expense", "Groceries"), ("income", "Salary"), ("income", "Food")}


def test_load_mapping_validates_shape(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"expense": {"A": ["b"]}}))
    assert mc.load_mapping(f) == {"expense": {"A": ["b"]}}
    f.write_text(json.dumps({"other": {}}))
    try:
        mc.load_mapping(f)
    except SystemExit:
        pass
    else:
        raise AssertionError("an invalid mapping must be rejected")
