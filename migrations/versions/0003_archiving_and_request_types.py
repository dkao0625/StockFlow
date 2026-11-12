"""archiving and request types

Adds what bulk editing, bulk stock adjustment, and bulk delete need:

products
  + is_archived, archived_at, archived_by_id   (soft delete / archiving)
inventory_requests
  + request_type  ('adjustment' or 'archive')  -- existing rows become 'adjustment'
  + direction     ('increase' or 'decrease')   -- existing rows become 'decrease'
  ~ quantity may be empty (archive requests have no quantity)
  ~ more reasons allowed: Received, Found, Count correction, Archive
activity_logs
  + product_id    (so each product can show its own change history)

Safe for existing data: nothing is dropped except two CHECK rules that are
replaced by wider ones, and every existing request already satisfies the
new rules (they are all Damaged/Missing decreases with quantity > 0).

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # --- products: archiving -------------------------------------------------
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_archived', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('archived_by_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_products_archived_by_id', 'users', ['archived_by_id'], ['id'])

    # --- inventory_requests: request types -----------------------------------
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_type', sa.String(length=20), server_default='adjustment', nullable=False))
        batch_op.add_column(sa.Column('direction', sa.String(length=10), nullable=True))

    # Every request that exists today is a Damaged/Missing report, i.e. a
    # stock DECREASE. Fill that in so they satisfy the new rule below.
    op.execute("UPDATE inventory_requests SET direction = 'decrease' WHERE direction IS NULL")

    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.alter_column('quantity', existing_type=sa.Integer(), nullable=True)
        # Replace two old rules with wider ones.
        batch_op.drop_constraint('ck_requests_quantity_positive', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_reason',
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive')",
        )
        batch_op.create_check_constraint('ck_requests_type', "request_type IN ('adjustment', 'archive')")
        batch_op.create_check_constraint('ck_requests_direction', "direction IN ('increase', 'decrease')")
        batch_op.create_check_constraint(
            'ck_requests_adjustment_fields',
            "request_type <> 'adjustment' OR (quantity > 0 AND direction IS NOT NULL)",
        )

    # --- activity_logs: link to a product --------------------------------------
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('product_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_activity_logs_product_id', ['product_id'], unique=False)
        batch_op.create_foreign_key('fk_activity_logs_product_id', 'products', ['product_id'], ['id'])


def downgrade():
    # Only possible while there are no archive requests or stock increases
    # (the old rules can't describe them). The new columns' data is lost.
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_activity_logs_product_id', type_='foreignkey')
        batch_op.drop_index('ix_activity_logs_product_id')
        batch_op.drop_column('product_id')

    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.drop_constraint('ck_requests_adjustment_fields', type_='check')
        batch_op.drop_constraint('ck_requests_direction', type_='check')
        batch_op.drop_constraint('ck_requests_type', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.create_check_constraint('ck_requests_reason', "reason IN ('Damaged', 'Missing')")
        batch_op.create_check_constraint('ck_requests_quantity_positive', 'quantity > 0')
        batch_op.alter_column('quantity', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('direction')
        batch_op.drop_column('request_type')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_constraint('fk_products_archived_by_id', type_='foreignkey')
        batch_op.drop_column('archived_by_id')
        batch_op.drop_column('archived_at')
        batch_op.drop_column('is_archived')
