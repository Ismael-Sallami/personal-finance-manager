"""Bank sync (Enable Banking).

- transactions: origin, external_id (unique), merchant, account, review
- tables bank_links, category_rules, budgets

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("transactions")}

    with op.batch_alter_table("transactions") as batch:
        if "origin" not in cols:
            batch.add_column(sa.Column("origin", sa.String(20), nullable=False,
                                       server_default="manual"))
        if "external_id" not in cols:
            batch.add_column(sa.Column("external_id", sa.String(128), nullable=True))
            batch.create_unique_constraint("uq_transactions_external_id", ["external_id"])
        if "merchant" not in cols:
            batch.add_column(sa.Column("merchant", sa.String(200), nullable=True))
            batch.create_index("ix_transactions_merchant", ["merchant"])
        if "account" not in cols:
            batch.add_column(sa.Column("account", sa.String(120), nullable=True))
        if "review" not in cols:
            batch.add_column(sa.Column("review", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))

    if "bank_links" not in tables:
        op.create_table(
            "bank_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bank", sa.String(60), nullable=False, index=True),
            sa.Column("session_id", sa.String(64), nullable=False),
            sa.Column("uid", sa.String(64), nullable=False),
            sa.Column("identification_hash", sa.String(128), nullable=False, unique=True),
            sa.Column("iban", sa.String(40), nullable=True),
            sa.Column("valid_until", sa.DateTime(), nullable=False),
            sa.Column("last_sync", sa.Date(), nullable=True),
            sa.Column("expiry_notified", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("bank_account_id", sa.Integer(),
                      sa.ForeignKey("bank_accounts.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    if "category_rules" not in tables:
        op.create_table(
            "category_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("pattern", sa.String(120), nullable=False, unique=True),
            sa.Column("category", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    if "budgets" not in tables:
        op.create_table(
            "budgets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("category", sa.String(120), nullable=False, unique=True),
            sa.Column("monthly_limit", sa.Numeric(14, 2), nullable=False),
            sa.Column("last_alert", sa.String(20), nullable=True),
        )


def downgrade() -> None:
    for t in ("budgets", "category_rules", "bank_links"):
        op.drop_table(t)
    with op.batch_alter_table("transactions") as batch:
        batch.drop_index("ix_transactions_merchant")
        batch.drop_constraint("uq_transactions_external_id", type_="unique")
        for c in ("review", "account", "merchant", "external_id", "origin"):
            batch.drop_column(c)
