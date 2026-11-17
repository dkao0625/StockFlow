"""account status and richer activity log

users
  + is_active        (true for every existing account)
  + created_at       (filled in from each account's first activity, if any)
activity_logs
  + actor_role       (filled in from the user's role)
  + category         (filled in from the wording of existing entries)
  + quantity_change  (empty for existing entries; their text still says it)
  + target_user_id   (filled in for existing registration entries)
  + indexes on category and created_at (for filtering)

Safe for existing data: only ADDS columns and indexes. The UPDATEs below only
fill the NEW columns of existing rows; no existing value is changed and no
row is deleted.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


# (pattern the old text starts with, category) -- checked in this order.
CATEGORY_PATTERNS = [
    ("Logged in", "login"),
    ("Logged out", "login"),
    ("Registered employee account%", "account"),
    ("Manager account%", "account"),
    ("Added product%", "product_created"),
    ("Bulk edited%", "bulk_edit"),
    ("Edited %", "product_edited"),
    ("Changed stock levels%", "product_edited"),
    ("Stock +%", "stock_increase"),
    ("Stock -%", "stock_decrease"),
    ("Archived product%", "product_archived"),
    ("Submitted request%", "damage_report"),
    ("Requested stock change%", "request_submitted"),
    ("Requested deletion%", "request_submitted"),
    ("Submitted restock request%", "request_submitted"),
    ("Created and authorized restock request%", "request_submitted"),
    ("Approved request%", "request_approved"),
    ("Rejected request%", "request_rejected"),
    ("Received % for restock%", "stock_received"),
    ("Closed restock%", "stock_received"),
]


def upgrade():
    # --- users ----------------------------------------------------------------
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))

    # --- activity_logs ----------------------------------------------------------
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('actor_role', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('category', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('quantity_change', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('target_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_activity_logs_target_user_id', 'users', ['target_user_id'], ['id'])
        batch_op.create_index('ix_activity_logs_category', ['category'], unique=False)
        batch_op.create_index('ix_activity_logs_created_at', ['created_at'], unique=False)

    # --- fill in the new columns for existing rows ----------------------------
    activity = sa.table(
        'activity_logs',
        sa.column('action', sa.String), sa.column('category', sa.String),
        sa.column('actor_id', sa.Integer), sa.column('actor_role', sa.String),
        sa.column('target_user_id', sa.Integer), sa.column('created_at', sa.DateTime),
    )
    users = sa.table(
        'users',
        sa.column('id', sa.Integer), sa.column('role', sa.String), sa.column('created_at', sa.DateTime),
    )

    for pattern, category in CATEGORY_PATTERNS:
        op.execute(
            activity.update()
            .where(activity.c.category.is_(None), activity.c.action.like(pattern))
            .values(category=category)
        )
    op.execute(
        activity.update()
        .where(activity.c.category == 'account', activity.c.target_user_id.is_(None))
        .values(target_user_id=activity.c.actor_id)
    )
    op.execute(
        activity.update()
        .where(activity.c.actor_role.is_(None), activity.c.actor_id.isnot(None))
        .values(
            actor_role=sa.select(users.c.role)
            .where(users.c.id == activity.c.actor_id)
            .scalar_subquery()
        )
    )
    op.execute(
        users.update()
        .where(users.c.created_at.is_(None))
        .values(
            created_at=sa.select(sa.func.min(activity.c.created_at))
            .where(activity.c.actor_id == users.c.id)
            .scalar_subquery()
        )
    )


def downgrade():
    with op.batch_alter_table('activity_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_activity_logs_created_at')
        batch_op.drop_index('ix_activity_logs_category')
        batch_op.drop_constraint('fk_activity_logs_target_user_id', type_='foreignkey')
        batch_op.drop_column('target_user_id')
        batch_op.drop_column('quantity_change')
        batch_op.drop_column('category')
        batch_op.drop_column('actor_role')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('created_at')
        batch_op.drop_column('is_active')
