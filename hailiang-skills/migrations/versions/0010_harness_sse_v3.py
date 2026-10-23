"""Public projection and transactional SSE outbox."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = '0010_harness_sse_v3'
down_revision = '0009_orchestration_rules'
branch_labels = None
depends_on = None

def upgrade():
    def payload(): return sa.Column('payload', sa.JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
    op.create_table('harness_public_scopes', sa.Column('scope_id', sa.String(64), primary_key=True), sa.Column('cursor', sa.Integer(), nullable=False), payload())
    op.create_table('harness_public_events', sa.Column('scope_id', sa.String(64), primary_key=True), sa.Column('seq', sa.Integer(), primary_key=True), payload())
    op.create_table('harness_public_submissions', sa.Column('key', sa.String(64), primary_key=True), payload())

def downgrade():
    for name in ['harness_public_submissions', 'harness_public_events', 'harness_public_scopes']: op.drop_table(name)
