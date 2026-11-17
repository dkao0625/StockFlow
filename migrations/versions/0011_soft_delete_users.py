"""soft-delete employee accounts

users
  + is_deleted       (false for every existing account)
  + deleted_at       (empty)
  + deleted_by_id    (empty; which manager deleted the account)
  + deletion_reason  (empty)

Safe for existing data: only ADDS columns. No account is deleted, no
password is reset, and nothing else changes.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_deleted', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('deleted_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('deletion_reason', sa.Text(), nullable=True))
        batch_op.create_foreign_key('fk_users_deleted_by_id', 'users', ['deleted_by_id'], ['id'])


def downgrade():
    # Only safe while no account is soft-deleted (they would become active again).
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_deleted_by_id', type_='foreignkey')
        batch_op.drop_column('deletion_reason')
        batch_op.drop_column('deleted_by_id')
        batch_op.drop_column('deleted_at')
        batch_op.drop_column('is_deleted')
