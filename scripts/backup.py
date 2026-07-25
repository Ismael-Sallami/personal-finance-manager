"""Back up and restore the database from the command line.

    python -m scripts.backup dump                  # writes finance_backup_<date>.json.gz
    python -m scripts.backup dump -o copy.json.gz
    python -m scripts.backup restore copy.json.gz          # dry run, writes nothing
    python -m scripts.backup restore copy.json.gz --write  # actually writes

Restoring DELETES the contents of every table present in the file and inserts
the rows from the backup. That is why `--write` is required: without it the
command only prints what it would do. It always targets the database in
DATABASE_URL, so check where you are pointing before writing.

The login user does not travel in the backup (`password_hash` is not copied).
After restoring into an empty database, recreate it with `python -m scripts.seed`.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Date, DateTime, Numeric, delete, insert

from app.config import settings
from app.db import Base, SessionLocal
from app.services.backup import dump_gzip, export_data, summary

# Importing the models fills Base.metadata; without it there is nothing to dump.
import app.models  # noqa: F401  (intentional side effect)


def _read(path: Path) -> dict:
    raw = path.read_bytes()
    if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _coerce(value, column):
    """Return the value in the type the column expects.

    The backup stores decimals and dates as text so nothing is lost; this undoes
    that conversion.
    """
    if value is None:
        return None
    kind = column.type
    if isinstance(kind, Numeric):
        return Decimal(str(value))
    if isinstance(kind, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(kind, Date):
        return date.fromisoformat(value) if isinstance(value, str) else value
    return value


def cmd_dump(args) -> int:
    db = SessionLocal()
    try:
        content, name = dump_gzip(db)
        data = export_data(db)
    finally:
        db.close()
    target = Path(args.output) if args.output else Path(name)
    target.write_bytes(content)
    print(f"Backup written: {target}  ({len(content)} bytes)")
    print(f"Contents: {summary(data)}")
    return 0


def cmd_restore(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Not found: {path}", file=sys.stderr)
        return 1

    data = _read(path)
    backup_tables = data["tables"]
    print(f"Backup from {data.get('generated', '?')} (engine {data.get('engine', '?')})")
    print(f"Target: {settings.sqlalchemy_url.split('@')[-1]}")

    # sorted_tables goes parents first, which is the safe insert order for FKs.
    order = [t for t in Base.metadata.sorted_tables if t.name in backup_tables]
    for table in order:
        print(f"  {table.name}: {len(backup_tables[table.name])} rows")

    if not args.write:
        print("\nDry run. Nothing written. Repeat with --write to apply it.")
        return 0

    db = SessionLocal()
    try:
        # Delete in reverse order (children before parents) to keep FKs happy.
        for table in reversed(order):
            db.execute(delete(table))
        for table in order:
            rows = backup_tables[table.name]
            if not rows:
                continue
            columns = {c.name: c for c in table.columns}
            values = [
                {k: _coerce(v, columns[k]) for k, v in row.items() if k in columns}
                for row in rows
            ]
            db.execute(insert(table), values)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    print("\nRestore complete.")
    print("If the database was empty, recreate the user: python -m scripts.seed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Database backup and restore.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="dump the database to a .json.gz file")
    p_dump.add_argument("-o", "--output", help="output file path")
    p_dump.set_defaults(func=cmd_dump)

    p_rest = sub.add_parser("restore", help="restore the database from a file")
    p_rest.add_argument("file", help="path to the backup (.json or .json.gz)")
    p_rest.add_argument("--write", action="store_true",
                        help="write for real (without it, only a dry run)")
    p_rest.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
