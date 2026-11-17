"""stock count corrections and other issues

inventory_requests
  + counted_quantity  (stock count corrections: what the employee counted)
  + additional_notes  (optional extra details on any request)
  ~ request_type may also be 'count' or 'issue'
  ~ reason may also be 'Other'
  + rule: a 'count' request must have counted_quantity >= 0

Safe for existing data: two new columns that start empty, two CHECK rules
replaced by WIDER ones, and a new rule that only concerns the new 'count'
type. Nothing is dropped or deleted.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('counted_quantity', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('additional_notes', sa.String(length=500), nullable=True))
        batch_op.drop_constraint('ck_requests_type', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_type', "request_type IN ('adjustment', 'archive', 'restock', 'count', 'issue')"
        )
        batch_op.create_check_constraint(
            'ck_requests_reason',
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive', "
            "'Restock', 'Other')",
        )
        batch_op.create_check_constraint(
            'ck_requests_count_fields',
            "request_type <> 'count' OR (counted_quantity IS NOT NULL AND counted_quantity >= 0)",
        )


def downgrade():
    # Only possible while there are no 'count' or 'issue' requests.
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.drop_constraint('ck_requests_count_fields', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.drop_constraint('ck_requests_type', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_reason',
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive', 'Restock')",
        )
        batch_op.create_check_constraint(
            'ck_requests_type', "request_type IN ('adjustment', 'archive', 'restock')"
        )
        batch_op.drop_column('additional_notes')
        batch_op.drop_column('counted_quantity')
