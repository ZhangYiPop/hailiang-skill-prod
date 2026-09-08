"""Persist the current published version independently of release sequence."""
import sqlalchemy as sa
from alembic import op

revision = "0005_current_release"
down_revision = "0004_workbench_debug_context"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workbench_objects")}
    if "current_release_id" not in columns:
        op.add_column("workbench_objects", sa.Column("current_release_id", sa.String(80), nullable=True))
    op.execute(sa.text("""
        UPDATE workbench_objects SET current_release_id = (
            SELECT release_id FROM workbench_releases
            WHERE workbench_releases.object_id = workbench_objects.object_id
              AND archived = false
            ORDER BY release_no DESC LIMIT 1
        ) WHERE current_release_id IS NULL
    """))


def downgrade():
    op.drop_column("workbench_objects", "current_release_id")
