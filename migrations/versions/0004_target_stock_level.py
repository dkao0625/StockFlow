"""target stock level

Adds what the Stock Alerts page needs to suggest a reorder quantity:

products
  + target_stock_level  (optional whole number, 0 or more)

Safe for existing data: one new column that may be empty, so every existing
product simply starts with no target. Nothing is dropped or rewritten.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target_stock_level', sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            'ck_products_target_nonnegative',
            'target_stock_level IS NULL OR target_stock_level >= 0',
        )


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_constraint('ck_products_target_nonnegative', type_='check')
        batch_op.drop_column('target_stock_level')
