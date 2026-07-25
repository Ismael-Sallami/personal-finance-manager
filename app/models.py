"""ORM models. Money is stored as NUMERIC, never float."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, func
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


class Category(Base):
    """Optional catalogue of categories. Transactions store the name directly,
    so a category can be used without registering it first."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # expense | income
    color: Mapped[str | None] = mapped_column(String(9), nullable=True)


class Transaction(Base):
    """One expense or income entry.

    year and month are stored next to the date because every view groups by
    month, and an indexed integer beats extracting parts of a date on both
    Postgres and SQLite.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    category: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # expense | income
    amount: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
