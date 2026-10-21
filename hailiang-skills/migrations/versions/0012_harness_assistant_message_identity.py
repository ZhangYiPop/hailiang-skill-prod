"""Stable public assistant-message identity for resumable V3 Harness tasks."""
import sqlalchemy as sa
from alembic import op

revision = "0012_harness_msg_identity"
down_revision = "0011_harness_v3_client_portraits"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("harness_runs")}
    if "assistant_message_id" not in columns:
        op.add_column("harness_runs", sa.Column("assistant_message_id", sa.String(80), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_runs")}
    # Existing V3 tasks retain a deterministic legacy bubble key so that a
    # deployed BFF can resume them immediately after this migration.
    bind.execute(sa.text(
        "UPDATE harness_runs SET assistant_message_id = 'msg_harness_' || run_id "
        "WHERE assistant_message_id IS NULL"
    ))
    if "ix_harness_runs_assistant_message_id" not in indexes:
        op.create_index("ix_harness_runs_assistant_message_id", "harness_runs", ["assistant_message_id"], unique=True)


def downgrade():
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_runs")}
    if "ix_harness_runs_assistant_message_id" in indexes:
        op.drop_index("ix_harness_runs_assistant_message_id", table_name="harness_runs")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("harness_runs")}
    if "assistant_message_id" in columns:
        op.drop_column("harness_runs", "assistant_message_id")
