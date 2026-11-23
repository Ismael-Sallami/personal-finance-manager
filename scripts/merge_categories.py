"""Merge duplicate or stray categories into a compact set.

Usage:
    python scripts/merge_categories.py mapping.json            # dry run (default)
    python scripts/merge_categories.py mapping.json --apply    # writes, after a backup

The mapping file (JSON, or YAML if PyYAML is installed) says which old names
become each final one, per kind:

    {"expense": {"Groceries": ["Food", "Supermarket"], "Sport": ["Gym"]},
     "income":  {"Salary": ["Payroll", "Paycheck"]}}

Names are compared ignoring case and accents. Transactions and the learned
category rules are renamed. With --apply a JSON backup of the affected rows is
written first (--backup-dir, default ./backups). Uses DATABASE_URL like the app.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import CategoryRule, Transaction  # noqa: E402


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def load_mapping(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        import yaml  # optional dependency
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict) or not set(data) <= {"expense", "income"}:
        raise SystemExit('The mapping must be {"expense": {...}, "income": {...}}')
    return data


def plan(db, mapping: dict) -> list[tuple[str, Transaction | CategoryRule, str]]:
    """[(kind, row, new_name)] for every row whose category changes."""
    changes = []
    for kind, groups in mapping.items():
        lookup = {norm(old): final for final, olds in groups.items() for old in olds}
        for tx in db.scalars(select(Transaction).where(Transaction.kind == kind)).all():
            final = lookup.get(norm(tx.category))
            if final and final != tx.category:
                changes.append((kind, tx, final))
    all_lookup = {norm(old): final for groups in mapping.values()
                  for final, olds in groups.items() for old in olds}
    for rule in db.scalars(select(CategoryRule)).all():
        final = all_lookup.get(norm(rule.category))
        if final and final != rule.category:
            changes.append(("rule", rule, final))
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mapping", type=Path)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args()

    mapping = load_mapping(args.mapping)
    db = SessionLocal()
    try:
        changes = plan(db, mapping)
        summary: dict[tuple[str, str, str], int] = {}
        for kind, row, final in changes:
            key = (kind, row.category, final)
            summary[key] = summary.get(key, 0) + 1
        for (kind, old, final), n in sorted(summary.items()):
            print(f"{kind:8} {old!r} -> {final!r}: {n} row(s)")
        print(f"{len(changes)} row(s) would change." if not args.apply else "")
        if not args.apply:
            print("Dry run: nothing written. Use --apply to write (a backup is made first).")
            return 0
        args.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = args.backup_dir / f"categories-{datetime.now():%Y%m%d-%H%M%S}.json"
        backup.write_text(json.dumps(
            [{"kind": k, "id": r.id, "old": r.category, "new": f} for k, r, f in changes],
            indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Backup: {backup}")
        for _, row, final in changes:
            row.category = final
        db.commit()
        print(f"Applied: {len(changes)} row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
