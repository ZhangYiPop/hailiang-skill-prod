"""V3 public run aliases and trusted profile portrait projections."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "0009_harness_v3_client_portraits"
down_revision = "0008_conversation_harness"
branch_labels = None
depends_on = None


def upgrade():
    payload = lambda: sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    op.create_table(
        "harness_client_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_run_id", sa.String(100), nullable=False),
        sa.Column("session_id", sa.String(80), nullable=False, index=True),
        sa.Column("user_id", sa.String(160), nullable=False, index=True),
        sa.Column("profile_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("harness_run_id", sa.String(80), nullable=False, index=True),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.UniqueConstraint("session_id", "user_id", "profile_id", "client_run_id", name="uq_harness_client_run_scope"),
    )
    op.create_table(
        "harness_profile_portraits",
        sa.Column("user_id", sa.String(160), primary_key=True),
        sa.Column("profile_id", sa.String(80), primary_key=True),
        payload(),
        sa.Column("version", sa.Integer(), nullable=False),
    )


def downgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in ("harness_profile_portraits", "harness_client_runs"):
        if name in existing:
            op.drop_table(name)
