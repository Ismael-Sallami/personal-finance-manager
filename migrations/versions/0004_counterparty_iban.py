"""IBAN of the counterparty of each movement (destination of transfers).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("transactions")}
    if "counterparty_iban" not in cols:
        with op.batch_alter_table("transactions") as b:
            b.add_column(sa.Column("counterparty_iban", sa.String(40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transactions") as b:
        b.drop_column("counterparty_iban")
