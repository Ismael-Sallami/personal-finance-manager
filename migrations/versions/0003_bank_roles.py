"""Bank role per link and reviewed mirror pairs.

- bank_links.role (main | secondary | investment | savings)
- transactions.pair_reviewed

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "role" not in {c["name"] for c in insp.get_columns("bank_links")}:
        with op.batch_alter_table("bank_links") as b:
            b.add_column(sa.Column("role", sa.String(20), nullable=False,
                                   server_default="secondary"))
        # The default investment platforms start with that role.
        op.execute("UPDATE bank_links SET role = 'investment' WHERE lower(bank) LIKE "
                   "'%trade republic%' OR lower(bank) LIKE '%myinvestor%'")
    if "pair_reviewed" not in {c["name"] for c in insp.get_columns("transactions")}:
        with op.batch_alter_table("transactions") as b:
            b.add_column(sa.Column("pair_reviewed", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("transactions") as b:
        b.drop_column("pair_reviewed")
    with op.batch_alter_table("bank_links") as b:
        b.drop_column("role")
