"""Add user display metadata for the internal operator test mode.

Revision ID: 0002_add_operator_user_metadata
Revises: 0001_production_storage
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_add_operator_user_metadata"
down_revision = "0001_production_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "advisor_users",
        sa.Column("user_id", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        if_not_exists=True,
    )
    op.create_index("ix_advisor_users_display_name", "advisor_users", ["display_name"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_advisor_users_display_name", table_name="advisor_users")
    op.drop_table("advisor_users")
