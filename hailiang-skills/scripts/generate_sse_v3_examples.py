"""Generate fictional wire fixtures through the actual runtime (no model/network).
Run PYTHONPATH=src .venv/bin/python scripts/generate_sse_v3_examples.py.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from time import sleep, perf_counter
from hailiang_skills.harness.runtime import HarnessRuntime
from hailiang_skills.harness.store import HarnessStore
from hailiang_skills.harness.contracts import AnswerSubmission
from hailiang_skills.skill_runtime.models import AssistantTurnResult, ToolCallRequest

def call(name, args, key):
    return AssistantTurnResult(tool_calls=(ToolCallRequest(key, name, args),))

class Demo:
    def __init__(self, legacy=False): self.counts = {}; self.legacy = legacy; self.stream_calls = 0
    def complete_with_tools(self, messages, specs, request_purpose, **kw):
        data = json.loads(messages[1].content.split('\n',1)[1])
        goal = data['task']['objective']
        key = (request_purpose, goal)
        count = self.counts.get(key,0); self.counts[key] = count+1
        if request_purpose == 'harness_main':
            if count == 0: return call('delegate_tasks', {'tasks':[{'key':'a','expert_id':'a','goal':'升学建议'},{'key':'b','expert_id':'b','goal':'兴趣建议'}]},'delegate')
            if self.legacy:
                sleep(.33)
                return AssistantTurnResult(final_text='根据已提供的年级，可以先探索兴趣。😀省份与兴趣本次跳过，因此仅提供通用建议。')
            return call('finalize_response',{},'finalize')
        if request_purpose == 'harness_expert' and goal == '升学建议' and count == 0:
            return call('execute_skill',{'skill_id':'internal','task':'分析地区限制'},'skill')
        if (request_purpose == 'harness_skill' or goal == '兴趣建议') and count == 0:
            fields = [{'key':'grade','label':'年级','scope':'profile'}, {'key':'province','label':'省份','scope':'profile'}] if request_purpose == 'harness_skill' else [{'key':'interest','label':'兴趣','scope':'profile'}]
            return call('request_user_input',{'title':'补充信息','fields':fields},'ask')
        if request_purpose == 'harness_skill' and count == 1:
            return call('request_user_input',{'title':'第二轮目标确认','fields':[{'key':'goal_detail','label':'希望进一步了解的目标','scope':'profile'}]},'ask-round-2')
        return call('submit_result',{'status':'partial','conclusion':'可给通用建议','limitations':['用户本次跳过部分信息'],'unresolved':['具体地区建议待补充']},'result')
    def stream_complete(self,*args,**kw):
        self.stream_calls += 1
        for text in ['根据已提供的年级，','可以先探索兴趣。😀','省份与兴趣本次跳过，因此仅提供通用建议。']:
            sleep(.11)
            yield SimpleNamespace(content_delta=text)

def generate(legacy=False):
    out = Path(__file__).resolve().parents[1] / 'guides/sse_v3'
    policy = {'experts':{e:{'name':f'专家 {e}','brief':'规划','rules':'依据已知信息分析','skills':['internal'] if e=='a' else [],'max_skill_calls':3} for e in ['a','b']},
              'skills':{'internal':{'name':'地区建议','rules':'需要省份时追问','specs':[]}},'independent_skills':[],
              'settings':{'context_policy_version':'v2','max_parallel':3,'max_model_calls':24,'max_tool_calls':48,'context_tokens':64000},'soul':''}
    with TemporaryDirectory() as tmp:
        client = Demo(legacy)
        started = perf_counter()
        runtime = HarnessRuntime(HarnessStore.local(Path(tmp)/'test.sqlite'),lambda _:client)
        rid = runtime.create('demo-session','demo-user','demo-profile','为孩子规划',policy,wire_protocol=None if legacy else 'hailiang.sse.v3')
        for _ in range(80):
            lease = runtime.claim(rid)
            if lease: runtime.execute(rid,*lease)
            else:
                run = runtime.store.get(rid)
                pending = next((q for q in run['inputs'].values() if q['status']=='pending'),None)
                if pending:
                    answers = {'grade':'高二'} if any(f['key']=='grade' for f in pending['fields']) else {}
                    runtime.answer(pending['id'],AnswerSubmission(user_id='demo-user',profile_id='demo-profile',submission_id=pending['id'],expected_version=pending['version'],answers=answers))
                elif run['status'] in {'completed','failed','cancelled'}: break
        run = runtime.store.get(rid)
        if run['status'] != 'completed': raise RuntimeError(run.get('error',run['status']))
        measured = {'model_calls': sum(t['model_calls'] for t in run['tasks'].values()), 'main_calls': run['tasks'][run['root_task_id']]['model_calls'],
                    'elapsed_ms': round((perf_counter()-started)*1000, 2), 'stream_calls': client.stream_calls}
        if legacy:
            runtime.close()
            return measured
        events = runtime.store.public_events('demo-session','demo-user','demo-profile',0)
        finalized = [event for event in events if event['type'] == 'run.finalized']
        if len(finalized) != 1 or finalized[0] != events[-1]:
            raise RuntimeError('run.finalized must be the final persistence event')
        out.joinpath('parallel-partial-stream.events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2)+'\n')
        out.joinpath('parallel-partial-stream.sse').write_text(''.join(f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e,ensure_ascii=False)}\n\n" for e in events))
        out.joinpath('snapshot.json').write_text(json.dumps(runtime.store.public_snapshot('demo-session','demo-user','demo-profile'),ensure_ascii=False,indent=2)+'\n')
        runtime.close()
        return measured
if __name__ == '__main__':
    old, new = generate(True), generate(False)
    report = {'source': 'deterministic_runtime_fixture_not_provider_benchmark', 'baseline': old, 'v3': new,
              'extra_model_calls': new['model_calls']-old['model_calls'],
              'limitations': '同一虚构多专家场景、固定正文和模拟生成延迟；仅量化协议调用及本地执行耗时，不代表真实模型 token 或业务性能。'}
    (Path(__file__).resolve().parents[1]/'guides/sse_v3/measurement.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
