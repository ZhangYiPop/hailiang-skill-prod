"""Create production session, Facts, event and encrypted-audit storage.

Revision ID: 0001_production_storage
Revises:
Create Date: 2026-07-19
"""

from alembic import op

from hailiang_skills.storage.database import Base

revision = "0001_production_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())

