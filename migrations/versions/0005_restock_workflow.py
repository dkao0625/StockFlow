"""restock workflow

Adds what restock requests and stock receiving need:

inventory_requests
  + received_quantity        (whole number, starts at 0 for every row)
  ~ request_type may also be 'restock'
  ~ reason may also be 'Restock'
  ~ status may also be 'Partially Received' or 'Received'
  + rule: a restock has quantity > 0 and never receives more than that
stock_receipts (new table)
  one row per delivery received against an approved restock request,
  linked to the stock movement it created

Safe for existing data: three CHECK rules are replaced by WIDER ones (every
value allowed before is still allowed), new rules only concern the new
'restock' type, and the new column defaults to 0. Nothing is deleted.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('received_quantity', sa.Integer(), server_default='0', nullable=False)
        )
        # Replace three rules with wider ones.
        batch_op.drop_constraint('ck_requests_status', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.drop_constraint('ck_requests_type', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_status',
            "status IN ('Pending', 'Approved', 'Rejected', 'Partially Received', 'Received')",
        )
        batch_op.create_check_constraint(
            'ck_requests_reason',
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive', 'Restock')",
        )
        batch_op.create_check_constraint(
            'ck_requests_type', "request_type IN ('adjustment', 'archive', 'restock')"
        )
        batch_op.create_check_constraint(
            'ck_requests_restock_fields',
            "request_type <> 'restock' OR (quantity > 0 AND received_quantity <= quantity)",
        )
        batch_op.create_check_constraint(
            'ck_requests_received_nonnegative', 'received_quantity >= 0'
        )

    op.create_table(
        'stock_receipts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('movement_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('received_by_id', sa.Integer(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.CheckConstraint('quantity > 0', name='ck_receipts_quantity_positive'),
        sa.ForeignKeyConstraint(['request_id'], ['inventory_requests.id']),
        sa.ForeignKeyConstraint(['movement_id'], ['stock_movements.id']),
        sa.ForeignKeyConstraint(['received_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('movement_id'),
    )
    op.create_index('ix_stock_receipts_request_id', 'stock_receipts', ['request_id'], unique=False)


def downgrade():
    # Only possible while there are no restock requests (the old rules can't
    # describe them). Receipt history is lost.
    op.drop_index('ix_stock_receipts_request_id', table_name='stock_receipts')
    op.drop_table('stock_receipts')
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.drop_constraint('ck_requests_received_nonnegative', type_='check')
        batch_op.drop_constraint('ck_requests_restock_fields', type_='check')
        batch_op.drop_constraint('ck_requests_type', type_='check')
        batch_op.drop_constraint('ck_requests_reason', type_='check')
        batch_op.drop_constraint('ck_requests_status', type_='check')
        batch_op.create_check_constraint('ck_requests_type', "request_type IN ('adjustment', 'archive')")
        batch_op.create_check_constraint(
            'ck_requests_reason',
            "reason IN ('Damaged', 'Missing', 'Received', 'Found', 'Count correction', 'Archive')",
        )
        batch_op.create_check_constraint(
            'ck_requests_status', "status IN ('Pending', 'Approved', 'Rejected')"
        )
        batch_op.drop_column('received_quantity')
