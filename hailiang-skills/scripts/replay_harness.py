"""Replay fictional conversations through the same configured model and assets.

Run explicitly: python scripts/replay_harness.py --env-file env.local.sh
Never sources shell scripts or writes credentials to the report.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import tempfile
import time

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.registry import SkillRegistry
from hailiang_skills.harness.contracts import AnswerSubmission
from hailiang_skills.harness.integration import HarnessIntegration
from hailiang_skills.harness.policy import capture_policy
from hailiang_skills.harness.runtime import HarnessRuntime
from hailiang_skills.harness.store import HarnessStore
from hailiang_skills.llm.config import load_llm_config
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path('output/harness-replay.json'))
    parser.add_argument('--timeout', type=int, default=360)
    parser.add_argument('--case', action='append', help='Only run selected case names.')
    args = parser.parse_args()
    config = load_llm_config()
    if args.env_file:
        words = shlex.split(args.env_file.read_text(), comments=True)
        for index, word in enumerate(words[:-1]):
            if word == 'export' and '=' in words[index + 1]:
                key, value = words[index + 1].split('=', 1)
                if key == config.api_key_env and '$' not in value:
                    os.environ.setdefault(key, value)
    if not config.enabled:
        raise SystemExit('Configured model credential is unavailable.')
    os.environ['HAILIANG_SANDBOX_PREWARM_ENABLED'] = 'false'
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), config)
    report = {'model': config.model, 'temperature': config.temperature, 'cases': [], 'fixture': 'fictional'}
    fixtures = [
        ('known_information', '孩子是浙江高二学生。数学最近两次考试都是75分，主要卡在函数题；每周可以练习两小时。请给出三条可执行建议，每条不超过60字。不要重复问年级或省份。'),
        ('parallel_experts', '请分别请学业专家和家庭教育专家分析：高二孩子数学75分，每天因作业与家长争吵，家长晚上只有半小时。最后汇总不超过300字；缺少的非必要信息我暂不补充。'),
        ('multi_round_inputs', '请家庭教育专家帮我制定亲子冲突计划。先向我确认孩子年级和最近冲突具体场景，分两轮提问，每轮一个必要问题，收到两轮回复后再给三条建议。我还没有提供这两项信息。'),
        ('expert_multi_round', '请主 agent 先分派家庭教育专家任务。主 agent 不收集孩子年级或冲突场景；由家庭教育专家在自己的任务内分两轮提问，每轮一个问题，分别了解孩子年级和最近冲突场景，收到两轮回复后制定亲子冲突计划。我尚未提供这两项信息。'),
    ]
    if args.case:
        fixtures = [(name, goal) for name, goal in fixtures if name in args.case]
    with tempfile.TemporaryDirectory(prefix='harness-replay-') as directory:
        store = HarnessStore.local(Path(directory) / 'harness.sqlite3')
        integration = HarnessIntegration(orchestrator, store)
        runtime = HarnessRuntime(store, integration.client, integration.tool)
        runtime.start()
        try:
            for name, goal in fixtures:
                print(f'Replaying {name}', flush=True)
                baseline_context = SessionContext(session_id='baseline_' + name, user_id='fictional-replay:' + name, profile_id='fictional-child:' + name)
                baseline_context.session_meta['expert_team_id'] = capture_policy(orchestrator, baseline_context)['team_id']
                started = time.monotonic()
                try:
                    legacy = orchestrator.handle_message(goal, baseline_context)
                    baseline = {'text': legacy.assistant_message, 'elapsed_s': round(time.monotonic() - started, 2), 'status': 'completed'}
                except Exception as exc:
                    baseline = {'status': 'failed', 'error_type': type(exc).__name__}
                context = SessionContext(session_id='harness_' + name, user_id=baseline_context.user_id, profile_id=baseline_context.profile_id)
                rid = runtime.create(context.session_id, context.user_id, context.profile_id, goal, capture_policy(orchestrator, context))
                started = time.monotonic()
                answered = set()
                while time.monotonic() - started < args.timeout:
                    run = store.get(rid)
                    if run['status'] in {'completed', 'failed', 'cancelled'}:
                        break
                    for request in run['inputs'].values():
                        if request['status'] == 'pending' and request['id'] not in answered:
                            values = {}
                            if name in {'multi_round_inputs', 'expert_multi_round'}:
                                for field in request['fields']:
                                    label = field['label'] + field['key']
                                    value = '高二' if '年级' in label or 'grade' in label else '做作业时被催促，双方提高声音；每晚家长只有半小时陪伴'
                                    if field['input_type'] == 'single_select':
                                        value = next((o['value'] for o in field['options'] if '高二' in o['label']), None)
                                        value = value or next((o['value'] for o in field['options'] if '高中' in o['label']), None)
                                        if value is None:
                                            values = {}
                                            break
                                    elif field['input_type'] == 'multi_select':
                                        value = [field['options'][0]['value']]
                                    elif field['input_type'] in {'number', 'integer'}:
                                        value = 16
                                    values[field['key']] = value
                            runtime.answer(request['id'], AnswerSubmission(user_id=context.user_id, profile_id=context.profile_id,
                                submission_id='replay_' + request['id'], expected_version=request['version'], skipped=not bool(values), answers=values))
                            answered.add(request['id'])
                    time.sleep(.5)
                else:
                    runtime.cancel(rid)
                run = store.get(rid)
                report['cases'].append({'name': name, 'goal': goal, 'legacy': baseline,
                    'harness': {'status': run['status'], 'text': run.get('result'), 'error': run.get('error'), 'elapsed_s': round(time.monotonic() - started, 2),
                        'questions': [{'title': q['title'], 'fields': q['fields'], 'status': q['status'], 'answer': q.get('answer')} for q in run['inputs'].values()],
                        'nodes': runtime.public(run)['nodes'], 'model_calls': sum(t['model_calls'] for t in run['tasks'].values()),
                        'failed_drafts': [trace.get('output') for trace in run['tasks'][run['root_task_id']]['trace'] if trace.get('error')],
                        'tools': [{'name': t['name'], 'status': t['status'], 'error': t.get('result', {}).get('error') if isinstance(t.get('result'), dict) else None} for t in run['tools'].values()]}})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(f"{name}: baseline={baseline['status']}, harness={run['status']}", flush=True)
        finally:
            runtime.close()


if __name__ == '__main__':
    main()
