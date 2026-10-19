"""Paired v1/v2 real-model Harness replay, with frozen assets and fictional inputs.

The automatic report is NOT a quality sign-off. Review each answer and record
constraint/evidence correctness before enabling HAILIANG_CONTEXT_POLICY=v2.
Credentials are read as data (never sourced), and never written into reports.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shlex
import statistics
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


CASES = {
    'known_information': '孩子是浙江高二学生，数学最近两次考试75分，主要卡在函数题；每周能练习两小时。给三条建议，每条不超过60字，不要重复问年级省份。',
    'parallel_experts': '请学业专家和家庭教育专家分别分析：高二孩子数学75分，每天因作业和家长争吵，家长每天只有半小时。汇总不超过300字；非必要信息暂不补充。',
    'multi_round_inputs': '请家庭教育专家制定亲子冲突计划；先分两轮分别了解孩子年级和最近冲突场景，每轮问一个问题，然后给三条建议。',
    'skip': '请家庭教育专家给亲子沟通建议。缺失信息我不知道，请接受跳过，不要换一种说法反复追问，也不要编造孩子情况。',
    'correction': '更正之前的信息：孩子现在高二，数学80分，不是高一或60分。请给三条建议，不要使用旧值。',
    'long_evidence': '请升学专家调用授权 Skill，检索本地 Skill 资料比较普通高考与强基计划，给出证据支持的三条差异和适用限制，不承诺录取。孩子浙江高二，目前成绩尚不明确。',
}


def load_credential(path, key):
    if os.getenv(key) or path is None:
        return
    for word in shlex.split(path.read_text(), comments=True):
        if word.startswith(key + '=') and '$' not in word:
            os.environ[key] = word.split('=', 1)[1]
            return


def summarize(run):
    traces = [trace for task in run['tasks'].values() for trace in task['trace']]
    measured = [t.get('usage', {}).get('input_tokens') for t in traces]
    components = {}
    stages = {}
    for task in run['tasks'].values():
        for trace in task['trace']:
            stage = trace.get('purpose', task['kind'])
            row = stages.setdefault(stage, {'calls': 0, 'estimated_input': 0, 'actual_input': 0, 'output': 0})
            row['calls'] += 1
            row['estimated_input'] += trace.get('diagnostics', {}).get('estimated_tokens', 0)
            row['actual_input'] += trace.get('usage', {}).get('input_tokens') or 0
            row['output'] += trace.get('usage', {}).get('output_tokens') or 0
            for key, value in trace.get('diagnostics', {}).get('components', {}).items():
                components[key] = components.get(key, 0) + value
    questions = [{'title': q['title'], 'fields': q['fields'], 'status': q['status'], 'answer': q.get('answer')} for q in run['inputs'].values()]
    signatures = [q['signature'] for q in run['inputs'].values()]
    return {'status': run['status'], 'error': run.get('error'), 'text': run.get('result', ''),
            'estimated_input': run.get('input_tokens_estimated', 0), 'actual_input': run.get('input_tokens_actual', 0),
            'measurement_complete': bool(traces) and all(isinstance(n, int) for n in measured),
            'model_calls': len(traces), 'stages': stages, 'components': components, 'questions': questions,
            'tasks': [{'kind': t['kind'], 'goal': t['goal'], 'status': t['status'], 'structured_result': t.get('structured_result')} for t in run['tasks'].values()],
            'tools': [{'name': t['name'], 'status': t['status'], 'arguments': t['arguments'], 'error': t.get('error')} for t in run['tools'].values()],
            'duplicate_question_signatures': len(signatures) - len(set(signatures)),
            'quality_review': 'pending', 'constraint_review': 'pending'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--case', action='append', choices=CASES)
    args = parser.parse_args()
    os.environ['HAILIANG_SANDBOX_PREWARM_ENABLED'] = 'false'
    config = load_llm_config()
    load_credential(args.env_file, config.api_key_env)
    if not config.enabled:
        raise SystemExit('Configured model credential unavailable; no replay was performed.')
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), config)
    snapshot_context = SessionContext(session_id='context-replay-snapshot', user_id='context-replay', profile_id=None)
    frozen = capture_policy(orchestrator, snapshot_context)
    fingerprint = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    report = {'model': config.model, 'temperature': config.temperature, 'policy_sha256': fingerprint,
              'fixture': 'fictional', 'repeats': args.repeats, 'cases': [], 'release_gate': 'not_accepted_pending_quality_review'}
    root = Path(__file__).resolve().parents[1]
    sources = sorted((root / 'src/hailiang_skills/harness').glob('*.py')) + [Path(__file__).resolve()]
    report['implementation_sha256'] = hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    save()
    with tempfile.TemporaryDirectory(prefix='harness-context-replay-') as folder:
        for name, goal in CASES.items():
            if args.case and name not in args.case:
                continue
            for repeat in range(args.repeats):
                pair = {'name': name, 'repeat': repeat + 1, 'goal': goal}
                report['cases'].append(pair)
                for version in ('v1', 'v2'):
                    store = HarnessStore.local(Path(folder) / f'{name}-{repeat}-{version}.sqlite3')
                    integration = HarnessIntegration(orchestrator, store)
                    runtime = HarnessRuntime(store, integration.client, integration.tool)
                    policy = deepcopy(frozen)
                    policy['settings']['context_policy_version'] = version
                    history = [{'role': 'user', 'content': '孩子高一，数学60分。'}, {'role': 'assistant', 'content': '已记录，后续可以更正。'}] if name == 'correction' else []
                    seed = {}
                    if name == 'correction':
                        for key, value in [('grade', '高一'), ('score', 60)]:
                            identity = store.fact_identity({'user_id': 'context-replay', 'session_id': 's', 'profile_id': None}, {'id': 'seed'}, 'session', key)
                            seed[identity] = {'key': key, 'scope': 'session', 'value': value, 'status': 'confirmed', 'version': 1}
                    rid = runtime.create('s', 'context-replay', None, goal, policy, seed, history)
                    runtime.start()
                    started = time.monotonic()
                    try:
                        while time.monotonic() - started < args.timeout:
                            run = store.get(rid)
                            if run['status'] in {'completed', 'failed', 'cancelled'}:
                                break
                            for request in run['inputs'].values():
                                if request['status'] != 'pending':
                                    continue
                                values = {}
                                if name == 'multi_round_inputs':
                                    for field in request['fields']:
                                        label = field['label'] + field['key']
                                        is_grade = '年级' in label or 'grade' in label or '学段' in label
                                        if field['input_type'] == 'text':
                                            values[field['key']] = '高二' if is_grade else '催作业时双方提高声音；家长每晚只有半小时'
                                        elif field['input_type'] == 'single_select' and is_grade:
                                            option = next((o for o in field['options'] if '高二' in o['label']), None)
                                            option = option or next((o for o in field['options'] if '高中' in o['label']), None)
                                            if option:
                                                values[field['key']] = option['value']
                                # A form is submitted only if every required field
                                # can be answered from the same frozen fixture.
                                if any(f['required'] and f['key'] not in values for f in request['fields']):
                                    values = {}
                                runtime.answer(request['id'], AnswerSubmission(user_id='context-replay', submission_id=request['id'], expected_version=request['version'], answers=values, skipped=not bool(values)))
                            time.sleep(.25)
                        else:
                            runtime.cancel(rid)
                        pair[version] = summarize(store.get(rid))
                        pair[version]['elapsed_s'] = round(time.monotonic() - started, 2)
                        save()
                        print(f'{name}/{repeat+1}/{version}: {pair[version]["status"]}, input={pair[version]["actual_input"]}', flush=True)
                    finally:
                        runtime.close()
        complete = [p for p in report['cases'] if 'v1' in p and 'v2' in p]
        report['comparison'] = {v: {'median_input': statistics.median([p[v]['actual_input'] for p in complete]) if complete else None,
                                    'completed': sum(p[v]['status'] == 'completed' for p in complete),
                                    'runs': len(complete), 'all_usage_measured': all(p[v]['measurement_complete'] for p in complete)} for v in ('v1', 'v2')}
        save()


if __name__ == '__main__':
    main()
