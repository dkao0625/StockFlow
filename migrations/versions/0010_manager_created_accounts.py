"""manager-created accounts

users
  + full_name             (optional)
  + email                 (optional, UNIQUE, stored lowercase)
  + must_change_password  (false for every existing account)
  + last_login_at         (filled from each account's most recent login entry)

Safe for existing data: only ADDS columns. Existing accounts keep their
passwords and are NOT forced to change them. The UPDATE below only fills the
new last_login_at column. Nothing is dropped or deleted.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column('full_name', sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column('must_change_password', sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint('uq_users_email', ['email'])

    op.execute(
        "UPDATE users SET last_login_at = ("
        "  SELECT MAX(activity_logs.created_at) FROM activity_logs"
        "  WHERE activity_logs.actor_id = users.id AND activity_logs.action = 'Logged in'"
        ") WHERE last_login_at IS NULL"
    )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('uq_users_email', type_='unique')
        batch_op.drop_column('last_login_at')
        batch_op.drop_column('must_change_password')
        batch_op.drop_column('full_name')
        batch_op.drop_column('email')
