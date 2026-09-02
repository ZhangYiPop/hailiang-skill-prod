"""Add immutable business workbench versioning and deployment tables.

Revision ID: 0002_business_workbench
Revises: 0001_multi_profile_runtime
Create Date: 2026-08-24
"""

from alembic import op

from hailiang_skills.storage.database import Base


revision = "0002_business_workbench"
down_revision = "0001_multi_profile_runtime"
branch_labels = None
depends_on = None


TABLE_NAMES = (
    "workbench_actors",
    "workbench_objects",
    "workbench_revisions",
    "workbench_revision_assets",
    "workbench_releases",
    "workbench_debug_sessions",
    "workbench_evaluation_suites",
    "workbench_evaluation_runs",
    "workbench_deployments",
    "workbench_audit_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLE_NAMES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLE_NAMES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
