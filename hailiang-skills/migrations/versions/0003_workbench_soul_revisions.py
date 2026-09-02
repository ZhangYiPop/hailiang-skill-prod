"""Add versioned Soul configurations for workbench testing.

Revision ID: 0003_workbench_soul_revisions
Revises: 0002_business_workbench
"""

from alembic import op

from hailiang_skills.storage.database import Base


revision = "0003_workbench_soul_revisions"
down_revision = "0002_business_workbench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["workbench_soul_revisions"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["workbench_soul_revisions"].drop(op.get_bind(), checkfirst=True)
