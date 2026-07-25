"""ORM models. Money is stored as NUMERIC, never float."""
import datetime as dt

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

MONEY = Numeric(14, 2)
QTY = Numeric(20, 8)


class User(Base):
    """The single account that owns the data. Created by scripts/seed.py."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
