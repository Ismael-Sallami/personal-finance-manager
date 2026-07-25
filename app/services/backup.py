"""Database backup as compressed JSON.

Why JSON and not pg_dump: the image is python:3.12-slim and carries no Postgres
client. Dumping through SQLAlchemy adds no dependency, works the same on SQLite
and on Postgres, and the file can be read without any tooling.

The backup is delivered to your Telegram chat on purpose: it ends up outside
the database provider, so if the project is paused or deleted the copy is still
there.

`users.password_hash` is left out: it adds nothing when restoring (the user is
recreated with `python -m scripts.seed`) and it should not travel through a
messaging API.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base

log = logging.getLogger("backup")

# Columns that never leave the server, per table.
EXCLUDED_COLUMNS = {"users": {"password_hash"}}


def _jsonable(value: Any) -> Any:
    """Turn SQLAlchemy types into something json can write."""
    if isinstance(value, Decimal):
        return str(value)  # str, not float: cents must not drift
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def export_data(db: Session) -> dict:
    """Dump every table of the model into a dict.

    It walks Base.metadata, so a new table joins the backup on its own without
    touching this file.
    """
    tables: dict[str, list[dict]] = {}
    for name, table in Base.metadata.tables.items():
        excluded = EXCLUDED_COLUMNS.get(name, set())
        columns = [c for c in table.columns if c.name not in excluded]
        rows = db.execute(select(*columns)).all()
        tables[name] = [
            {c.name: _jsonable(v) for c, v in zip(columns, row)} for row in rows
        ]
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "engine": "sqlite" if settings.is_sqlite else "postgres",
        "tables": tables,
    }


def dump_gzip(db: Session) -> tuple[bytes, str]:
    """Return (gzipped content, file name with today's date)."""
    data = export_data(db)
    raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    name = f"finance_backup_{date.today().isoformat()}.json.gz"
    return gzip.compress(raw, compresslevel=9), name


def summary(data: dict) -> str:
    """One line with the row count per table."""
    parts = [f"{t}: {len(rows)}" for t, rows in sorted(data["tables"].items()) if rows]
    return " · ".join(parts) or "no data"


def backup_to_telegram(db: Session) -> dict:
    """Build the backup and send it as a document to the allowed chats.

    Returns a dict with the outcome so /tasks/backup can report it to the cron.
    It does not raise when Telegram fails: the backup was already built and the
    failure shows up in the response.
    """
    from app.services.bot import send_document_sync

    data = export_data(db)
    raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    content = gzip.compress(raw, compresslevel=9)
    name = f"finance_backup_{date.today().isoformat()}.json.gz"

    targets = settings.allowed_chat_ids
    if not settings.telegram_enabled or not targets:
        return {"sent": False, "reason": "telegram not configured",
                "bytes": len(content), "summary": summary(data)}

    delivered, failures = 0, []
    caption = f"🗄️ Backup {date.today().isoformat()}\n{summary(data)}"
    for chat_id in targets:
        try:
            send_document_sync(chat_id, content, name, caption,
                               mime="application/gzip")
            delivered += 1
        except Exception as exc:
            log.warning("backup not delivered to %s: %s", chat_id, exc)
            failures.append(str(exc)[:120])

    return {"sent": delivered > 0, "targets": delivered, "failures": failures,
            "bytes": len(content), "summary": summary(data)}
