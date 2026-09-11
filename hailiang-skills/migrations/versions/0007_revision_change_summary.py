"""Add an optional business-facing description to every workbench revision."""

import sqlalchemy as sa
from alembic import op


revision = "0007_revision_summary"
down_revision = "0006_profile_memory_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("workbench_revisions")}
    if "change_summary" not in columns:
        op.add_column(
            "workbench_revisions",
            sa.Column("change_summary", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("workbench_revisions", "change_summary")
