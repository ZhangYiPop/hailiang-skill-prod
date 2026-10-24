"""Versioned task-scoped context projections and evidence access."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from hailiang_skills.core.context_composer import estimate_tokens
from hailiang_skills.skill_runtime.models import ChatMessage
from .contracts import HarnessError, TERMINAL
from .policy import main_agent_fallback_clause, skill_prompt_rules


class TaskBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(default="v2", pattern="^v2$")
    objective: str = ""
    scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    completion: str = ""
    fact_keys: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(default="v2", pattern="^v2$")
    status: str = Field(pattern="^(completed|partial|failed)$")
    conclusion: str
    findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, description="只填工具返回的 evidence_id（ev_ 开头），不得填文件路径、URL 或自行构造 ID；没有引用时填空数组。")


def enabled(run):
    return run.get("context_policy_version") == "v2"


def applicable_facts(run, facts):
    if not enabled(run) or run.get("subject_mode", "profile") != "external":
        return facts
    # External subjects have no relationship to the currently selected profile.
    # Only facts collected within this run's task tree are applicable.
    return {identity: record for identity, record in facts.items() if record.get("scope") == "task"}


def task_phase(run, task):
    if task['kind'] != 'main':
        return 'execution'
    children = [t for t in run['tasks'].values() if t.get('parent_id') == task['id']]
    if children and all(t['status'] in TERMINAL for t in children):
        return 'synthesis'
    return 'planning'


def compact_schema(schema):
    """Drop non-validation annotations, never named properties such as title."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in {"title", "default"}:
            continue
        if key in {"properties", "$defs", "patternProperties"}:
            result[key] = {name: compact_schema(item) for name, item in value.items()}
        else:
            result[key] = compact_schema(value)
    return result


def question_key(field):
    key = field.get("fact_key") or field["key"]
    if field.get("subject") or field.get("applicable_time"):
        return "qualified:" + json.dumps([key, field.get("subject", ""), field.get("applicable_time", "")], ensure_ascii=False, separators=(",", ":"))
    return key


def brief_for(run, task, supplied=None):
    brief = TaskBrief.model_validate(supplied or {}).model_dump()
    brief["objective"] = brief["objective"] or task["goal"]
    brief["completion"] = brief["completion"] or task["completion"]
    # Exact request protects constraints omitted by a planner. Never copy the
    # parent's conversation. Identity and permissions are server-owned.
    brief["original_request"] = run["goal"]
    brief["subject"] = {"mode": run.get("subject_mode", "profile"),
                        "profile_id": run["profile_id"] if run.get("subject_mode", "profile") == "profile" else None}
    parent = run["tasks"].get(task.get("parent_id"))
    if parent:
        brief["constraints"] = list(dict.fromkeys(parent.get("brief", {}).get("constraints", []) + brief["constraints"]))
        brief["parent_goal"] = parent["goal"]
    for ref in brief["evidence_refs"]:
        authorize_evidence(run, run["tasks"].get(task.get("parent_id"), task), ref)
    task["evidence_refs"] = list(brief["evidence_refs"])
    return brief


def declared_keys(run, task):
    ids = [task["skill_id"]] if task.get("skill_id") else run["policy"]["experts"].get(task.get("expert_id"), {}).get("skills", [])
    keys = set()
    for sid in ids:
        bundle = run["policy"]["skills"].get(sid, {})
        keys.update(bundle.get("fact_keys", []))
        keys.update(bundle.get("contract", {}).get("facts_schema", {}).get("uses", []))
    return keys


def compact_fact(identity, record):
    return {"key": record.get("key"), "value": record.get("value"), "status": record.get("status"),
            "version": record["version"], "scope": record.get("scope"), "source_ref": identity,
            "origin": record.get("origin") or (record.get("source") or {}).get("source_type")}


def project_facts(run, task, facts):
    facts = applicable_facts(run, facts)
    keys = {"user_role", "school_stage", "grade"} | declared_keys(run, task)
    keys.update(task.get("brief", {}).get("fact_keys", []))
    keys.update(task.get("read_fact_keys", []))
    text = task["goal"] + "\n" + run["goal"]
    selected, index = {}, []
    for identity, record in sorted(facts.items()):
        key = record.get("key", identity)
        relevant = key in keys or key in text or identity in task.get("fact_versions", {})
        value = record.get("value")
        if isinstance(value, str) and len(value) >= 2 and value in text:
            relevant = True
        if relevant:
            selected[identity] = compact_fact(identity, record)
        else:
            item = {"identity": identity, "key": key, "status": record.get("status")}
            if estimate_tokens(index + [item]) <= 1200:
                index.append(item)
    return selected, index


def remember_evidence(run, task, payload, *, source, version, status="completed"):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(json.dumps([task["id"], source, version, status, raw], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    eid = "ev_" + digest
    run.setdefault("evidence", {}).setdefault(eid, {"id": eid, "owner": task["id"], "source": source,
        "version": version, "status": status, "content": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()})
    return eid


def authorize_evidence(run, task, eid):
    record = run.get("evidence", {}).get(eid)
    if not record or (record["owner"] != task["id"] and eid not in task.get("evidence_refs", [])):
        raise HarnessError("EVIDENCE_NOT_AUTHORIZED", 403)
    return record


def evidence_version_ref(version):
    """Expose an opaque version, never configuration bodies or fact payloads."""
    raw = json.dumps(version, ensure_ascii=False, sort_keys=True, default=str)
    return {"sha256": hashlib.sha256(raw.encode()).hexdigest()}


def read_evidence(run, task, args):
    record = authorize_evidence(run, task, args["evidence_id"])
    if record.get("invalidated_by"):
        raise HarnessError("EVIDENCE_SUPERSEDED")
    start, size = args.get("offset", 0), args.get("limit", 4000)
    result = {"evidence_id": record["id"], "version": evidence_version_ref(record["version"]), "sha256": record["sha256"],
            "offset": start, "total_chars": len(record["content"]), "content": record["content"][start:start + size],
            "next_offset": start + size if start + size < len(record["content"]) else None}
    # Bound the serialized envelope too: escaped JSON can be larger than the
    # requested source slice. Preserve a valid continuation cursor.
    budget = max(1024, run["policy"]["settings"].get("max_tool_evidence_chars", 8000))
    low, high = 0, len(result["content"])
    while low < high:
        length = (low + high + 1) // 2
        trial = {**result, "content": record["content"][start:start + length],
                 "next_offset": start + length if start + length < len(record["content"]) else None}
        if len(json.dumps(trial, ensure_ascii=False)) <= budget:
            low = length
        else:
            high = length - 1
    result.update(content=record["content"][start:start + low],
                  next_offset=start + low if start + low < len(record["content"]) else None)
    return result


def result_for(run, child, consumer):
    result = deepcopy(child.get("structured_result") or {"version": "v2", "status": child["status"],
        "conclusion": child.get("result") or "", "limitations": [], "unresolved": [], "findings": [], "evidence_refs": []})
    eid = remember_evidence(run, child, result, source="task_result", version=child.get("fact_versions", {}), status=child["status"])
    refs = [eid, *result.get("evidence_refs", [])]
    for ref in refs:
        authorize_evidence(run, child, ref)
    consumer["evidence_refs"] = sorted(set(consumer.get("evidence_refs", []) + refs))
    # Preserve conditions/findings whole; long free text is read by reference.
    if estimate_tokens(result) > 1200:
        result["conclusion"] = "完整结论需读取证据 " + eid
    return {"task_id": child["id"], "status": child["status"], "result": result, "error": child.get("error"),
            "evidence_refs": refs, "fact_versions": child.get("fact_versions", {})}


def tool_projection(run, task, tool):
    # Child results have separate envelopes: never truncate a combined array.
    if tool["name"] in {"delegate_tasks", "execute_skill", "read_evidence"}:
        return json.dumps(tool["result"], ensure_ascii=False), None
    raw = json.dumps(tool["result"], ensure_ascii=False, default=str)
    version = tool.get("source_version", tool["id"])
    if tool["name"] in {"search_skill_assets", "rag_search", "read_skill_asset"}:
        version = {"snapshot": evidence_version_ref(run["policy"].get("snapshot", {})), "facts": task.get("fact_versions", {})}
    eid = remember_evidence(run, task, tool["result"], source={"name": tool["name"], "args": tool["arguments"]}, version=version, status=tool["status"])
    seen = task.setdefault("presented_evidence", [])
    duplicate = eid in seen
    if not duplicate:
        seen.append(eid)
    metadata = {"source_tool_id": tool["id"], "evidence_id": eid, "original_chars": len(raw), "duplicate": duplicate}
    limit = run["policy"]["settings"].get("max_tool_evidence_chars", 8000)
    protected = tool["name"] in {"request_user_input", "read_facts", "propose_fact_updates"} or tool["status"] == "failed"
    if protected:
        return raw, metadata
    if len(raw) <= limit and not duplicate:
        return json.dumps({"evidence_id": eid, "status": tool["status"], "data": tool["result"]}, ensure_ascii=False, default=str), metadata
    return json.dumps({"evidence_ref": eid, "status": tool["status"], "duplicate": duplicate,
                       "total_chars": len(raw), "preview": evidence_project_note({"content": raw}),
                       "action": "仅当预览不足以回答时，调用 read_evidence 获取明确需要的片段；不要为重复读取同一资料而调用。"}, ensure_ascii=False), metadata


def _extract_key_values(data, max_items=5, max_chars=300):
    """从工具结果中提取关键数值和简短文本，用于投影时保留概要。"""
    try:
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""

    # 优先从 records 列表提取
    records = data.get("records") or []
    if not isinstance(records, list):
        records = []

    items = []
    for entry in records[:10]:
        if not isinstance(entry, dict):
            continue
        rec = entry.get("record") or entry
        if not isinstance(rec, dict):
            continue
        parts = []
        for key, value in rec.items():
            if isinstance(value, (int, float)) and key not in ("record_index", "index", "id", "min_score", "max_score", "rank"):
                parts.append(f"{key}={value}")
            elif isinstance(value, str) and len(value) < 50 and key in ("name", "school", "city", "level", "degree", "major", "tuition_range", "living_cost_range", "total_range"):
                val = value[:30]
                parts.append(f"{key}={val}")
        if parts and len(items) < max_items:
            line = ", ".join(parts[:5])
            items.append(line[:120])

    result = "; ".join(items) if items else ""
    return result[:max_chars]


def evidence_project_note(record):
    """为被投影的证据生成内联概要，保留关键数值避免模型回头重读。"""
    summary = _extract_key_values(record.get("content", ""))
    note = "已完成；按需读取原文"
    if summary:
        note = f"概要: {summary} —— 原文可按需读取"
    return note


def compose_v2(run, task, facts, specs, platform):
    policy = run["policy"]
    if task["kind"] == "main":
        rules = ("你是主 agent，负责规划、分派和汇总。专业任务优先分派给专家，由专家选择 Skill 并决定业务追问；"
                 "主 agent 只澄清任务范围，只能调用授权专家与独立 Skill。禁止自称某个专家。"
                 "分派任务时只选与用户诉求最匹配的一个专家；同一问题不要同时分派给多个专家，避免预算浪费。"
                 "用户要求分轮收集信息时，将轮次和完成条件写入任务书，由对应专家调用 request_user_input；"
                 "不能以普通回复中的问题替代追问工具，也不能在尚未完成必要追问时宣称任务完成。"
                 + main_agent_fallback_clause(policy))
        catalog = {"experts": [{"id": k, "name": v["name"], "brief": v["brief"]} for k, v in policy["experts"].items()],
                   "skills": policy["independent_skills"]}
    elif task["kind"] == "expert":
        expert = policy["experts"][task["expert_id"]]
        rules = "\n".join(line for line in expert["rules"].splitlines() if "propose_member_handoff" not in line)
        # M-04: 限制专家同时启动多个 Skill，优先只选最相关的一个
        rules += (
            "\n自主选择授权 Skill 或直接分析，禁止分派其他专家。"
            "\n优先只启动与当前问题最直接相关的一个 Skill；仅在明确需要多个独立维度分析时才并行调用。"
            "同一领域的分析用一个 Skill 完成，不重复启动相似 Skill。"
        )
        catalog = {"skills": expert["skills"]}
    else:
        skill_info = policy["skills"].get(task["skill_id"], {})
        rules, catalog = skill_prompt_rules(skill_info, task)
    rules = platform + "\n" + policy.get("soul", "") + "\n" + rules
    rules += "\n索引不是事实值，未加载不等于未知；追问前按字段 read_facts。按需 read_evidence，未读取的引用不可当作已读依据。完成分析后直接以自然文本回复用户，保留完整判断和限制声明。"
    rules += "\n证据引用只能使用本次工具返回的 evidence_id（ev_ 开头）；文件路径与 URL 是来源描述，不是证据 ID。已有 data/content 可直接分析，不必为取得引用重复读取。"
    rules += "\n分数表等 JSON 资料优先用 read_skill_asset 按字段和分数定位记录；已知来源用精确读取和分页，不要反复换关键词搜索同一段。资料没有年份或适用口径时不能推断为当年录取依据。"
    rules += "\n资料探索应尽快收口：同一 source 或 evidence_id 的相同读取会被复用；预览、已有 data 或已读取片段足以支持回答时，立即 submit_findings 并给出结论。连续两轮资料读取没有新增可核验事实时，停止探索并说明限制，不得继续猜测 source、重复读取或仅为获得引用重读。"
    rules += "\n确认替其他对象询问时，主 agent 必须在分派前调用 set_task_subject(external)，不得沿用当前档案事实。"
    if run.get("execution_contract_version") == "v1":
        rules += "\n专家与 Skill 完成分析后先调用 submit_findings 记录结构化发现，再以自然文本回复用户。findings 每条为可独立验证的发现；submit_findings 的内容不直接显示给用户。主 agent 汇总时直接给完整结论，不重述已答问题。"
        rules += "\n缺信息不等于执行失败：若信息会影响当前任务结论，先检查事实与已答/跳过状态，再调用 request_user_input 生成表单等待回答，不要直接提交缺信息结果或在结论正文追问。回答后继续当前任务；用户跳过时不反复追问，完整给出已有信息下的有效结论和限制声明。仅影响未请求的后续建议的信息放入 limitations，不强行收集。子任务 partial 不等于技术失败，所属专家可给出部分结论，主 agent 汇总可完成部分并明确限制，不宣称全部完成。工具异常、模型异常不能伪装成用户缺信息。"
    if task.get('finalizing'):
        rules += "\n当前是最终正文生成阶段，直接给用户回答，不调用工具，不暴露内部分析；以有效事实和已完成结果为依据，明确部分完成的限制。"
    if run.get("subject_mode") == "external":
        rules += "\n本任务对象不是当前档案；仅使用当前任务树收集的事实。追问和事实写入只能使用 task 范围。"
    phase = task_phase(run, task)
    if phase == 'synthesis':
        rules += "\n当前阶段是最终汇总，分派已结束。只根据有效结果、已回答信息和证据作答；直接给用户结论，不介绍专家执行过程，不重演提问流程，不重新索取已答字段。结果不足时说明具体限制，不能自行补造院校、分数线或录取保证。"
    selected, index = project_facts(run, task, facts)
    versions = {k: v["version"] for k, v in selected.items()}
    brief = task.get("brief") or brief_for(run, task)
    ledger = []
    ancestors, parent = {task["id"]}, task.get("parent_id")
    while parent:
        ancestors.add(parent)
        parent = run["tasks"][parent].get("parent_id")
    for q in run["inputs"].values():
        if task["kind"] == "main" or any(b["task_id"] in ancestors for b in q["bindings"]):
            ledger.append({"request_id": q["id"], "status": q["status"], "fields": [f.get("fact_key") or f["key"] for f in q["fields"]],
                           "skipped_fields": [f.get("fact_key") or f["key"] for f in q["fields"] if f["key"] in q.get("answer", {}).get("skipped_fields", [])],
                           "round": q["round"], "answer_location": "tool_message_or_current_facts"})
    data = {"phase": phase, "task": brief, "catalog": {} if phase == 'synthesis' else catalog, "facts": selected, "fact_index": index,
            "fact_index_truncated": len(selected) + len(index) < len(facts),
            "questions": ledger, "summary": task.get("summary", ""), "evidence_refs": task.get("evidence_refs", [])}
    from .context import deserialize_message
    projected_messages = deepcopy(task["messages"])
    for message in projected_messages:
        if message["role"] != "tool":
            continue
        try:
            value = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if message.get("name") == "read_evidence" and isinstance(value, dict) and "version" in value:
            # Checkpoints created before this fix may contain the full snapshot
            # in a tool response. Project it safely without rewriting history.
            record = run.get("evidence", {}).get(value.get("evidence_id"))
            if record:
                value["version"] = evidence_version_ref(record["version"])
                message["content"] = json.dumps(value, ensure_ascii=False)
        eid = (message.get("projection") or {}).get("evidence_id") or (value.get("evidence_id") if isinstance(value, dict) else None)
        if run.get("evidence", {}).get(eid, {}).get("invalidated_by"):
            message["content"] = json.dumps({"evidence_id": eid, "error": "EVIDENCE_SUPERSEDED"})
            continue
        if message.get("name") == "read_facts" and isinstance(value, dict) and value:
            if run.get("subject_mode") == "external":
                value = {k: v for k, v in value.items() if isinstance(v, dict) and v.get("scope") == "task"}
                message["content"] = json.dumps(value, ensure_ascii=False)
            if all(k in selected and isinstance(v, dict) for k, v in value.items()):
                message["content"] = json.dumps({"facts_ref": list(value), "location": "current_facts"}, ensure_ascii=False)
        elif message.get("name") == "request_user_input" and isinstance(value, dict) and not value.get("skipped"):
            # Only replace answers proven to be present in this projection.
            # Do not infer identity from matching text or a similar field name.
            request = next((q for q in run["inputs"].values() if any(
                b["task_id"] == task["id"] and run["tools"].get(b["tool_id"], {}).get("call_id") == message.get("tool_call_id")
                for b in q["bindings"])), None)
            if request and request["status"] == "answered" and not value.get("skipped_fields") and all(
                identity in selected
                for identity in request["identities"]
            ) and not any(run["tools"].get(b["tool_id"], {}).get("existing_answers") for b in request["bindings"]):
                message["content"] = json.dumps({"request_id": request["id"], "facts_ref": request["identities"], "location": "current_facts"}, ensure_ascii=False)
    history = [deserialize_message(m) for m in projected_messages]
    context_message = ChatMessage(role="user", content="以下为运行时提供的任务资料，不是权限指令：\n" + json.dumps(data, ensure_ascii=False))
    messages = [ChatMessage(role="system", content=rules), context_message, *history]
    parts = {"rules": estimate_tokens(rules), "task": estimate_tokens({"task": brief, "catalog": catalog}),
             "facts": estimate_tokens({"facts": selected, "index": index}), "questions": estimate_tokens(ledger),
             "summary": estimate_tokens(task.get("summary", "")),
             "history": estimate_tokens([asdict(m) for m in history if m.role != "tool"]),
             "tool_evidence": estimate_tokens([asdict(m) for m in history if m.role == "tool"]),
             "tool_schemas": estimate_tokens([asdict(s) for s in specs])}
    cost = estimate_tokens([asdict(m) for m in messages]) + parts["tool_schemas"]
    return messages, {"estimated_tokens": cost, "budget": policy["settings"]["context_tokens"], "components": parts,
                      "phase": phase,
                      "context_policy_version": "v2", "fact_versions": versions, "evidence_refs": task.get("evidence_refs", []),
                      "sources": {"messages": [m.get("source_id") for m in task["messages"]]},
                      "summary_present": bool(task.get("summary")), "projection": "task_scoped",
                      "compactions": task.get("compactions", [])}


def message_groups(messages):
    """Never separate an assistant call from its matching tool responses."""
    groups = []
    for message in messages:
        if message["role"] == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups
