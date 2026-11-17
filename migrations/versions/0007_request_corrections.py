"""request corrections and history

inventory_requests
  ~ status may also be 'Needs Correction' (wider CHECK rule)
  + updated_at   (existing requests get their created_at)
request_events (new table)
  one row per step after submission: correction requested, resubmitted,
  approved, rejected, received, closed -- with who, when, note, and the
  quantity before/after a resubmission

Safe for existing data: the status rule is replaced by a WIDER one, the new
column is filled from created_at, and the new table starts empty. Nothing is
dropped or deleted.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.drop_constraint('ck_requests_status', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_status',
            "status IN ('Pending', 'Approved', 'Rejected', 'Partially Received', 'Received', "
            "'Needs Correction')",
        )

    op.execute("UPDATE inventory_requests SET updated_at = created_at WHERE updated_at IS NULL")

    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.alter_column(
            'updated_at', existing_type=sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        )

    op.create_table(
        'request_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Integer(), nullable=False),
        sa.Column('event', sa.String(length=30), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('quantity_before', sa.Integer(), nullable=True),
        sa.Column('quantity_after', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event IN ('correction_requested', 'resubmitted', 'approved', 'rejected', 'received', 'closed')",
            name='ck_request_events_event',
        ),
        sa.ForeignKeyConstraint(['request_id'], ['inventory_requests.id']),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_request_events_request_id', 'request_events', ['request_id'], unique=False)


def downgrade():
    # Only possible while no request is in 'Needs Correction'. History is lost.
    op.drop_index('ix_request_events_request_id', table_name='request_events')
    op.drop_table('request_events')
    with op.batch_alter_table('inventory_requests', schema=None) as batch_op:
        batch_op.drop_constraint('ck_requests_status', type_='check')
        batch_op.create_check_constraint(
            'ck_requests_status',
            "status IN ('Pending', 'Approved', 'Rejected', 'Partially Received', 'Received')",
        )
        batch_op.drop_column('updated_at')
