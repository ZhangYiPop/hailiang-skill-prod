"""Persist candidate debug runtime context between turns.

Revision ID: 0004_workbench_debug_context
Revises: 0003_workbench_soul_revisions
"""

import sqlalchemy as sa
from alembic import op


revision = "0004_workbench_debug_context"
down_revision = "0003_workbench_soul_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0002 creates the workbench tables from the current SQLAlchemy
    # metadata. On a fresh database that metadata can already contain this
    # newer column, so make the additive migration safe for both schemas.
    existing_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("workbench_debug_sessions")
    }
    if "runtime_context" not in existing_columns:
        op.add_column(
            "workbench_debug_sessions",
            sa.Column("runtime_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    existing_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("workbench_debug_sessions")
    }
    if "runtime_context" in existing_columns:
        op.drop_column("workbench_debug_sessions", "runtime_context")
