"""Transactional public projections. No prompt, trace or raw tool arguments."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer
from hailiang_skills.storage.database import Base, _json_type
from .contracts import now, TERMINAL

PROTOCOL = 'hailiang.sse.v3'

class PublicScope(Base):
    __tablename__ = 'harness_public_scopes'
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(_json_type())

class PublicEvent(Base):
    __tablename__ = 'harness_public_events'
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(_json_type())

class Submission(Base):
    __tablename__ = 'harness_public_submissions'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(_json_type())

TABLES = [PublicScope.__table__, PublicEvent.__table__, Submission.__table__]

def scope_key(session_id, user_id, profile_id):
    return hashlib.sha256(json.dumps([session_id, user_id, profile_id], ensure_ascii=False).encode()).hexdigest()

def lock_scope(db, session_id, user_id, profile_id):
    key = scope_key(session_id, user_id, profile_id)
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
    db.execute(insert(PublicScope).values(scope_id=key, cursor=0, payload={
        'protocol': PROTOCOL, 'session_id': session_id, 'profile_id': profile_id,
        'runtime_mode': 'harness_v1', 'runs': {}, 'nodes': {}, 'input_requests': {}, 'input_groups': {}, 'messages': {}, '_records': {}}
    ).on_conflict_do_nothing(index_elements=['scope_id']))
    scope = db.scalar(select(PublicScope).where(PublicScope.scope_id == key).with_for_update())
    if '_records' not in scope.payload:
        payload = deepcopy(scope.payload)
        payload['_records'] = {}
        scope.payload = payload
    elif 'input_groups' not in scope.payload:
        payload = deepcopy(scope.payload)
        payload['input_groups'] = {}
        scope.payload = payload
    return scope

def emit(db, scope, run_id, kind, entity):
    scope.cursor += 1
    value = {'protocol': PROTOCOL, 'event_id': f'{scope.scope_id}:{scope.cursor}', 'seq': scope.cursor,
             'session_id': scope.payload['session_id'], 'profile_id': scope.payload['profile_id'],
             'run_id': run_id, 'type': kind, 'entity_id': entity['id'],
             'entity_version': entity['version'], 'timestamp': now(), 'data': deepcopy(entity)}
    db.add(PublicEvent(scope_id=scope.scope_id, seq=scope.cursor, payload=value))

def emit_full_state(db, scope, run_id, *, event_type='state', reason='updated', extra=None):
    """Persist a V2-shaped full public snapshot for the V3 wire adapter."""
    scope.cursor += 1
    state = snapshot(scope)
    current = next((item for item in state['runs'] if item['id'] == run_id), {})
    payload = {
        'protocol': PROTOCOL, 'event_id': f'{scope.scope_id}:{scope.cursor}',
        'seq': scope.cursor, 'session_id': scope.payload['session_id'],
        'profile_id': scope.payload['profile_id'], 'run_id': run_id,
        'status': current.get('status', 'running'), 'outcome': current.get('outcome'),
        'reason': reason, 'timestamp': now(), 'snapshot': state,
    }
    if extra:
        payload.update(deepcopy(extra))
    db.add(PublicEvent(scope_id=scope.scope_id, seq=scope.cursor, payload={**payload, 'type': event_type}))

def upsert(db, scope, bucket, kind, run_id, entity):
    payload = deepcopy(scope.payload)
    old = payload[bucket].get(entity['id'])
    body = {k: v for k, v in entity.items() if k != 'version'}
    if old and body == {k: v for k, v in old.items() if k != 'version'}:
        return
    entity = {**body, 'version': (old or {}).get('version', 0) + 1}
    payload[bucket][entity['id']] = entity
    scope.payload = payload
    emit(db, scope, run_id, kind, entity)
    # Fine-grained events remain in the outbox for traceability. Consumers use
    # this immutable, full snapshot event instead, like SSE v2 state frames.
    reason = 'dispatch_completed' if bucket == 'runs' and entity.get('dispatch') and not (old or {}).get('dispatch') else 'updated'
    emit_full_state(db, scope, run_id, reason=reason)

def safe_error(error):
    return error if isinstance(error, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', error) else 'TASK_EXECUTION_FAILED'

def project(db, run):
    from .runtime import HarnessRuntime
    scope = lock_scope(db, run['session_id'], run['user_id'], run['profile_id'])
    public = HarnessRuntime.public(run)
    rid = run['run_id']
    nodes, inputs, groups = public.pop('nodes'), public.pop('inputs'), public.pop('input_groups', [])
    # Explicit queue projection keeps card scheduling out of BFF/frontend code.
    ordered_inputs = sorted(inputs, key=lambda item: (item.get('created_at', ''), item['id']))
    pending_inputs = [item['id'] for item in ordered_inputs if item.get('status') == 'pending']
    queued_inputs = [item['id'] for item in ordered_inputs if item.get('status') == 'queued']
    active_group = next((group for group in groups if group.get('status') == 'active'), None)
    queued_groups = [group for group in groups if group.get('status') == 'queued']
    public['input_queue'] = {
        'active_group_id': active_group.get('id') if active_group else None,
        'queued_group_ids': [group['id'] for group in queued_groups],
        'active_request_id': (active_group or {}).get('active_request_id') or (pending_inputs[0] if pending_inputs else None),
        'active_group_total': (active_group or {}).get('total_count', 0),
        'active_group_completed': (active_group or {}).get('completed_count', 0),
        'queued_request_ids': queued_inputs,
        'pending_count': len(pending_inputs),
        'queued_count': len(queued_inputs),
    }
    public.pop('version', None)
    # Technical traces and provider error strings never enter the public stream.
    public['error'] = {'code': safe_error(run.get('error')), 'message': '任务未能完成'} if run.get('error') else None
    if run.get('output_blocked'):
        public['error'] = {'code': 'CONTENT_BLOCKED', 'message': '该回复未通过内容检查，请调整问题后重试。'}
        public['result'] = public['error']['message']
    active = run['status'] not in TERMINAL
    runnable = any(t['status'] in {'running', 'queued'} for t in run['tasks'].values())
    public['status'] = 'running' if active and runnable else ('waiting_for_user' if active and public['waiting_count'] else run['status'])
    public['id'] = rid
    end = max((t.get('ended_at') or run['updated_at'] for t in run['tasks'].values()), default=run['updated_at'])
    public['elapsed_ms'] = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(run['created_at'])).total_seconds() * 1000))
    public['allowed_actions'] = ['start', 'cancel', 'answer'] if active else ['start']
    if active:
        public['outcome'] = None
    elif run['status'] in {'failed', 'cancelled'}:
        public['outcome'] = run['status']
    else:
        public['outcome'] = ('partial' if any(
            t.get('structured_result', {}).get('status') in {'partial', 'failed'}
            for t in run['tasks'].values()
        ) else 'completed')
    if run.get('cancellation_reason'):
        public['cancellation_reason'] = run['cancellation_reason']
    if run.get('superseded_by_run_id'):
        public['superseded_by_run_id'] = run['superseded_by_run_id']
    upsert(db, scope, 'runs', 'run.updated', rid, public)
    for node in nodes:
        node['run_id'] = rid
        if node.get('error'):
            node['error'] = safe_error(node['error'])
        task = run['tasks'].get(node['id'], {})
        node['outcome'] = task.get('structured_result', {}).get('status')
        upsert(db, scope, 'nodes', 'node.updated', rid, node)
    for request in inputs:
        request['request_version'] = request.pop('version')
        request['run_id'] = rid
        upsert(db, scope, 'input_requests', 'input_request.updated', rid, request)
    for group in groups:
        group['run_id'] = rid
        upsert(db, scope, 'input_groups', 'input_group.updated', rid, group)
    if run.get('phase'):
        upsert(db, scope, 'messages', 'message.updated', rid, {'id': rid + ':phase', 'run_id': rid,
            'kind': 'phase', 'content': run['phase'], 'status': 'completed', 'revision': 1})
    mid = rid + ':final'
    previous = scope.payload['messages'].get(mid)
    if run.get('result') or previous or run['status'] in TERMINAL:
        content = run.get('result') or (previous or {}).get('content', '')
        status = 'completed' if run['status'] == 'completed' else ('interrupted' if not active else 'streaming')
        if run.get('output_blocked'):
            content, status = '', 'blocked'
        upsert(db, scope, 'messages', 'message.updated', rid, {'id': mid, 'run_id': rid, 'kind': 'final',
            'content': content, 'status': status, 'revision': (previous or {}).get('revision', 1)})
    if run['status'] in TERMINAL and rid not in scope.payload.get('_records', {}):
        payload = deepcopy(scope.payload)
        final_message = payload['messages'].get(mid, {'id': mid, 'content': '', 'status': 'interrupted', 'revision': 1, 'version': 0})
        run_entity = payload['runs'][rid]
        record = {
            'protocol': PROTOCOL, 'record_version': 1, 'id': rid, 'run_id': rid,
            'session_id': run['session_id'], 'profile_id': run['profile_id'],
            'user_message': {'content': run['goal'], 'created_at': run['created_at']},
            'assistant_message': {k: final_message.get(k) for k in ('id', 'content', 'status', 'revision', 'version')},
            'execution': {'status': run['status'], 'outcome': run_entity.get('outcome'),
                          'completed_at': run['updated_at'], 'cancellation_reason': run.get('cancellation_reason'),
                          'superseded_by_run_id': run.get('superseded_by_run_id')},
            'agent_summary': [
                {k: node.get(k) for k in ('id', 'parent_id', 'kind', 'actor', 'expert_id', 'title', 'status', 'outcome', 'brief', 'phase', 'action', 'result')}
                for node in payload['nodes'].values() if node.get('run_id') == rid and node.get('kind') in {'main', 'expert', 'skill'}
            ],
            'dispatch': run_entity.get('dispatch'),
            'synthesis_status': run_entity.get('synthesis_status'),
            'recommendations_status': run_entity.get('recommendations_status'),
            'round_experts': run_entity.get('round_experts') or {
                'assigned_expert_ids': [], 'running_expert_ids': [], 'completed_expert_ids': [],
                'failed_expert_ids': [], 'status': 'idle',
            },
            'jump_cards': list(run_entity.get('jump_cards') or []),
            'input_summary': [
                {'id': item.get('id'), 'actor': item.get('actor'), 'round': item.get('round'),
                 'status': item.get('status'), 'answer': item.get('answer')}
                for item in payload['input_requests'].values() if item.get('run_id') == rid
            ],
        }
        payload['_records'][rid] = record
        scope.payload = payload
        finalization = {**run_entity, 'record_version': 1,
             'record_url': f'/api/v3/sessions/{run["session_id"]}/harness/runs/{rid}/record',
             'final_message_id': final_message['id'], 'final_message_version': final_message.get('version', 0)}
        emit(db, scope, rid, 'run.finalized', finalization)
        # ``done`` is the V2-compatible terminal signal consumed by BFFs.
        emit_full_state(db, scope, rid, event_type='done', reason='completed', extra={
            'record_version': 1, 'record_url': finalization['record_url'],
            'final_message_id': finalization['final_message_id'],
            'final_message_version': finalization['final_message_version'],
        })
    return scope

def snapshot(scope):
    public = {key: deepcopy(value) for key, value in scope.payload.items() if not key.startswith('_')}
    return {**public, 'cursor': scope.cursor,
            **{key: list(deepcopy(scope.payload.get(key, {})).values()) for key in ['runs', 'nodes', 'input_requests', 'input_groups', 'messages']}}
