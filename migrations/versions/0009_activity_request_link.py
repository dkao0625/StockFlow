"""link activity entries to requests

activity_logs
  + request_id  (optional link to the request an action was about)

Existing entries: the link is filled in ONLY when the entry's text mentions
"#<number>", a request with that number exists, and it is for the same
product as the entry. Anything uncertain is left empty.

Safe for existing data: one new column that may be empty, plus an index.
Only that new column is filled; nothing else is changed or deleted.

Revision ID: 0009
Revises: 0008
"""
import re

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

REQUEST_NUMBER = re.compile(r"#(\d+)")


def upgrade():
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('request_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_activity_logs_request_id', ['request_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_activity_logs_request_id', 'inventory_requests', ['request_id'], ['id']
        )

    connection = op.get_bind()
    request_products = dict(connection.execute(sa.text("SELECT id, product_id FROM inventory_requests")).all())
    entries = connection.execute(sa.text(
        "SELECT id, action, product_id FROM activity_logs WHERE request_id IS NULL AND action LIKE '%#%'"
    )).all()
    for entry_id, action, product_id in entries:
        match = REQUEST_NUMBER.search(action)
        if not match:
            continue
        request_id = int(match.group(1))
        if request_id in request_products and request_products[request_id] == product_id:
            connection.execute(
                sa.text("UPDATE activity_logs SET request_id = :request_id WHERE id = :entry_id"),
                {"request_id": request_id, "entry_id": entry_id},
            )


def downgrade():
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_activity_logs_request_id', type_='foreignkey')
        batch_op.drop_index('ix_activity_logs_request_id')
        batch_op.drop_column('request_id')
