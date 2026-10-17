"""Versioned orchestration rules for the platform agent.

Revision ID: 0009_orchestration_rules
Revises: 0008_conversation_harness
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0009_orchestration_rules"
down_revision = "0008_conversation_harness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_orchestration_rule_revisions",
        sa.Column("orchestration_revision_id", sa.String(length=80), primary_key=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("revision_no", name="uq_workbench_orchestration_revision_no"),
    )
    op.create_index(
        "ix_workbench_orchestration_rule_revisions_content_hash",
        "workbench_orchestration_rule_revisions",
        ["content_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workbench_orchestration_rule_revisions_content_hash",
        table_name="workbench_orchestration_rule_revisions",
    )
    op.drop_table("workbench_orchestration_rule_revisions")
