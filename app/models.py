"""ORM models. Money is stored as NUMERIC, never float."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class BrokerImport(Base):
    """One uploaded statement. Keeps the positions it produced together, so a
    bad import can be traced back to the file it came from."""

    __tablename__ = "broker_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(30), index=True)  # myinvestor|traderepublic|kraken
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    investments: Mapped[list["Investment"]] = relationship(
        back_populates="import_", cascade="all, delete-orphan"
    )


class Investment(Base):
    """A valued investment position.

    current_value can update itself: with a yahoo_symbol and a quantity, the
    price job downloads current_price and sets current_value = quantity * price.
    Without a symbol the value is whatever the user typed.
    """

    __tablename__ = "investments"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(30), index=True)
    asset: Mapped[str] = mapped_column(String(200))
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    invested: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    current_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    withdrawn: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    valued_on: Mapped[dt.date] = mapped_column(Date, index=True)

    # --- Automatic revaluation ---
    yahoo_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    current_price: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    price_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    auto_value: Mapped[bool] = mapped_column(Boolean, default=True)

    # Recurring monthly contribution (DCA). 0 = none scheduled.
    monthly_contribution: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_imports.id"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    import_: Mapped["BrokerImport | None"] = relationship(back_populates="investments")


class BankAccount(Base):
    """A bank account, savings pot or cash. Counts towards net worth together
    with the investments. Add as many as you need."""

    __tablename__ = "bank_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="account")  # account | savings | cash
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class NetWorthSnapshot(Base):
    """Net worth on a given day, one row per date. Feeds the evolution chart:
    every time a value changes (an investment or an account) today's row is
    upserted. A scheduled job also writes one every month."""

    __tablename__ = "net_worth_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    investments: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    banks: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
