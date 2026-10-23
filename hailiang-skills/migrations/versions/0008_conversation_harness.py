"""Durable conversation harness; schema is frozen independently of ORM models."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "0008_conversation_harness"
down_revision = "0007_revision_summary"
branch_labels = None
depends_on = None


TABLES = ['harness_runs', 'harness_tasks', 'harness_tool_calls', 'harness_input_requests',
          'harness_checkpoints', 'harness_jobs', 'harness_events', 'harness_execution_attempts', 'harness_facts']


def upgrade():
    payload = lambda: sa.Column('payload', sa.JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
    column = lambda name, length=80, **kw: sa.Column(name, sa.String(length), **kw)
    op.create_table('harness_runs', column('run_id', primary_key=True), column('session_id', nullable=False, index=True),
        column('user_id', 160, nullable=False), column('profile_id'), column('active_session', unique=True), payload(), sa.Column('version', sa.Integer(), nullable=False))
    op.create_table('harness_tasks', column('task_id', primary_key=True), column('run_id', nullable=False, index=True), column('parent_id'), column('status', 40, nullable=False, index=True), payload())
    op.create_table('harness_tool_calls', column('node_id', 160, primary_key=True), column('run_id', nullable=False, index=True), column('task_id', nullable=False, index=True), column('status', 40, nullable=False), payload())
    op.create_table('harness_input_requests', column('request_id', primary_key=True), column('run_id', nullable=False, index=True), column('status', 40, nullable=False), payload())
    op.create_table('harness_checkpoints', column('task_id', primary_key=True), column('run_id', nullable=False, index=True), sa.Column('version', sa.Integer(), nullable=False), payload())
    op.create_table('harness_jobs', column('task_id', primary_key=True), column('run_id', nullable=False, index=True), column('status', 40, nullable=False, index=True), column('lease_token', nullable=False), column('lease_until', 40, nullable=False), payload())
    op.create_table('harness_events', sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True), column('session_id', nullable=False, index=True), column('run_id', nullable=False, index=True), column('event', 60, nullable=False), payload())
    op.create_table('harness_execution_attempts', column('execution_id', primary_key=True), column('run_id', nullable=False, index=True), column('task_id', nullable=False, index=True), column('status', 40, nullable=False), payload())
    op.create_table('harness_facts', sa.Column('identity', sa.Text(), primary_key=True), payload(), sa.Column('version', sa.Integer(), nullable=False))


def downgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(TABLES):
        if name in existing:
            op.drop_table(name)
