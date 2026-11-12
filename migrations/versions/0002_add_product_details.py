"""add product details

Adds category, description, unit, location, supplier, unit_cost,
created_at and updated_at to the products table.

Safe for existing data:
  - Only ADDS columns and constraints; nothing is dropped or recreated
    on PostgreSQL.
  - Existing products get sensible defaults automatically:
      category = 'Other', unit = 'Pieces', created_at/updated_at = now,
      and the optional columns stay empty (NULL).

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        # Required columns get a server_default so existing rows are filled in.
        batch_op.add_column(sa.Column('category', sa.String(length=40), server_default='Other', nullable=False))
        batch_op.add_column(sa.Column('unit', sa.String(length=20), server_default='Pieces', nullable=False))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False))

        # Optional columns start empty (NULL).
        batch_op.add_column(sa.Column('description', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column('location', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('supplier', sa.String(length=120), nullable=True))
        # NUMERIC(10, 2): exact decimal, up to 99,999,999.99 -- never a float.
        batch_op.add_column(sa.Column('unit_cost', sa.Numeric(precision=10, scale=2), nullable=True))

        # Database-level rules (autogenerate does not detect these, so they
        # are written by hand). They match the CheckConstraints in models.py.
        batch_op.create_check_constraint('ck_products_unit_cost_nonnegative', 'unit_cost >= 0')
        batch_op.create_check_constraint(
            'ck_products_category',
            "category IN ('Packaging Supplies', 'Clothing', 'Electronics', 'Office Supplies', 'Other')",
        )
        batch_op.create_check_constraint(
            'ck_products_unit',
            "unit IN ('Pieces', 'Boxes', 'Rolls', 'Packs', 'Pairs', 'Other')",
        )


def downgrade():
    # Removes the new columns again. WARNING: this deletes the data stored
    # in them (categories, descriptions, costs, ...). Only for undoing a
    # mistake right after upgrading.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_constraint('ck_products_unit', type_='check')
        batch_op.drop_constraint('ck_products_category', type_='check')
        batch_op.drop_constraint('ck_products_unit_cost_nonnegative', type_='check')
        batch_op.drop_column('unit_cost')
        batch_op.drop_column('supplier')
        batch_op.drop_column('location')
        batch_op.drop_column('description')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')
        batch_op.drop_column('unit')
        batch_op.drop_column('category')
