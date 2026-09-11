"""Add durable profile archive and short-term memory checkpoint tables."""

from alembic import op
from hailiang_skills.storage.database import Base


# Alembic's baseline creates ``alembic_version.version_num`` as VARCHAR(32).
# Keep revision IDs within that compatibility limit.
revision = "0006_profile_memory_context"
down_revision = "0005_current_release"
branch_labels = None
depends_on = None


TABLE_NAMES = ("profile_memories", "conversation_memory_checkpoints")


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLE_NAMES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLE_NAMES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
