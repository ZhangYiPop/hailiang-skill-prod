"""Clean baseline for the multi-profile long-conversation runtime.

Revision ID: 0001_multi_profile_runtime
Revises:
Create Date: 2026-08-24
"""

from alembic import op

from hailiang_skills.storage.database import Base


revision = "0001_multi_profile_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
