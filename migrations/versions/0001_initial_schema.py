"""Initial schema: users, categories, transactions, broker imports,
investments, bank accounts and net worth snapshots.

Revision ID: 0001
Revises: 
Create Date: 2026-07-25 21:37:59.313778
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('bank_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('balance', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bank_accounts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bank_accounts_name'), ['name'], unique=False)

    op.create_table('broker_imports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('broker', sa.String(length=30), nullable=False),
    sa.Column('imported_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('source_file', sa.String(length=255), nullable=True),
    sa.Column('raw_meta', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('broker_imports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_broker_imports_broker'), ['broker'], unique=False)

    op.create_table('categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('color', sa.String(length=9), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_categories_name'), ['name'], unique=False)

    op.create_table('net_worth_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('investments', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('banks', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('net_worth_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_net_worth_snapshots_date'), ['date'], unique=True)

    op.create_table('transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('year', sa.Integer(), nullable=False),
    sa.Column('month', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transactions_category'), ['category'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_month'), ['month'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_year'), ['year'], unique=False)

    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_email'), ['email'], unique=True)

    op.create_table('investments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('broker', sa.String(length=30), nullable=False),
    sa.Column('asset', sa.String(length=200), nullable=False),
    sa.Column('isin', sa.String(length=20), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('invested', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('current_value', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('withdrawn', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('profit', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('valued_on', sa.Date(), nullable=False),
    sa.Column('yahoo_symbol', sa.String(length=40), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('current_price', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('price_updated_at', sa.DateTime(), nullable=True),
    sa.Column('auto_value', sa.Boolean(), nullable=False),
    sa.Column('monthly_contribution', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('import_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['import_id'], ['broker_imports.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('investments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_investments_broker'), ['broker'], unique=False)
        batch_op.create_index(batch_op.f('ix_investments_valued_on'), ['valued_on'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('investments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_investments_valued_on'))
        batch_op.drop_index(batch_op.f('ix_investments_broker'))

    op.drop_table('investments')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_email'))

    op.drop_table('users')
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transactions_year'))
        batch_op.drop_index(batch_op.f('ix_transactions_month'))
        batch_op.drop_index(batch_op.f('ix_transactions_date'))
        batch_op.drop_index(batch_op.f('ix_transactions_category'))

    op.drop_table('transactions')
    with op.batch_alter_table('net_worth_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_net_worth_snapshots_date'))

    op.drop_table('net_worth_snapshots')
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_categories_name'))

    op.drop_table('categories')
    with op.batch_alter_table('broker_imports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_broker_imports_broker'))

    op.drop_table('broker_imports')
    with op.batch_alter_table('bank_accounts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bank_accounts_name'))

    op.drop_table('bank_accounts')
