from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from pydantic import ValidationError
from jsonschema import validate, ValidationError as SchemaError

from hailiang_skills.skill_runtime.models import ChatMessage, ToolCallRequest, ToolSpec
from .context import compose, serialize_message
from .contracts import AnswerSubmission, HarnessError, InputArguments, TERMINAL, TOOL_TERMINAL, identifier, now, validate_answers
from .policy import allowed_skills, check_skill, normalize_skill
from .wakeup import Wakeup
from .reply import reply_violations, repair_instruction
from .evidence import project_evidence

log = logging.getLogger(__name__)


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def task_node(kind, goal, parent_id=None, expert_id=None, skill_id=None, completion="完成指定任务", depends_on=()):
    return {"id": identifier("task"), "parent_id": parent_id, "kind": kind, "expert_id": expert_id, "skill_id": skill_id,
            "goal": goal, "completion": completion, "depends_on": list(depends_on), "status": "queued", "messages": [{"role": "user", "content": goal}],
            "pending_calls": [], "model_calls": 0, "tool_calls": 0, "skill_calls": 0, "attempt": 0, "trace": [],
            "summary": "", "fact_versions": {}, "created_at": now(), "started_at": None, "ended_at": None, "lease_token": "", "lease_until": ""}


class HarnessRuntime:
    def __init__(self, store, client_factory, tool_executor=None, moderation=None, *, lease_seconds=90):
        self.store, self.client_factory, self.tool_executor = store, client_factory, tool_executor
        self.moderation = moderation
        self.lease_seconds = max(1, lease_seconds)
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="harness")
        self._thread = None
        self.wakeup = Wakeup()
        self.store.wakeup = self.wakeup

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.wakeup.start()
        self._thread = threading.Thread(target=self._loop, name="harness-scheduler", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.wakeup.close()
        if self._thread:
            self._thread.join(timeout=2)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def create(self, session_id, user_id, profile_id, goal, policy, seed_facts=None, history=None):
        if not session_id or len(session_id) > 80 or not user_id or len(user_id) > 160 or profile_id and len(profile_id) > 80:
            raise HarnessError("INVALID_HARNESS_IDENTITY")
        root = task_node("main", goal)
        root["messages"] = list(history or []) + root["messages"]
        run = {"run_id": identifier("run"), "session_id": session_id, "user_id": user_id, "profile_id": profile_id,
               "goal": goal, "status": "running", "root_task_id": root["id"], "tasks": {root["id"]: root},
               "tools": {}, "inputs": {}, "policy": policy, "seed_facts": seed_facts or {}, "created_at": now(), "updated_at": now(), "version": 1}
        self.store.create(run)
        return run["run_id"]

    def specs(self, run, task):
        spec = lambda name, description, schema: ToolSpec(name, description, schema, True)
        result = [spec("read_facts", "读取当前任务适用事实", object_schema({})),
                  spec("request_user_input", "向用户请求必要信息；调用后挂起，答案作为工具结果恢复。", InputArguments.model_json_schema()),
                  spec("propose_fact_updates", "保存有来源的事实变更。user_statement 必须提供用户原文中的逐字证据且值出现在证据中；推断只能 candidate/assumption/derived。", object_schema({
                      "updates": {"type": "array", "minItems": 1, "maxItems": 12, "items": object_schema({
                          "key": {"type": "string"}, "value": {}, "scope": {"enum": ["task", "session", "profile", "user"]},
                          "status": {"enum": ["user_statement", "candidate", "assumption", "derived"]}, "evidence": {"type": "string"}}, ["key", "value", "scope", "status", "evidence"])}}, ["updates"]))]
        if task["kind"] == "main" and run["policy"]["experts"]:
            result.append(spec("delegate_tasks", "拆解为授权专家任务。使用批内唯一 key 和 depends_on，专家自主选择 Skill。", object_schema({
                "tasks": {"type": "array", "minItems": 1, "maxItems": 12, "items": object_schema({
                    "key": {"type": "string", "minLength": 1}, "expert_id": {"enum": list(run["policy"]["experts"])},
                    "goal": {"type": "string", "minLength": 1}, "completion": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}}, ["key", "expert_id", "goal"])}}
                , ["tasks"])))
        if allowed_skills(run, task):
            result.append(spec("execute_skill", "执行自己的授权 Skill，并接收结果继续分析。", object_schema({
                "skill_id": {"enum": allowed_skills(run, task)}, "task": {"type": "string", "minLength": 1}}, ["skill_id", "task"])))
        if task["kind"] == "skill":
            for item in run["policy"]["skills"][task["skill_id"]]["specs"]:
                name = "rag_search" if item["name"] == "rag" else item["name"]
                if name not in {s.name for s in result}:
                    result.append(spec(name, item["description"], item["parameters_schema"]))
        else:
            # Public search uses only data/tools from this actor's authorized skills.
            searchable = list(run["policy"]["skills"].values())
            for name, internal in [("rag_search", "rag"), ("web_search", "web_search")]:
                if any(any(s["name"] == internal for s in bundle["specs"]) for bundle in searchable):
                    result.append(spec(name, "检索当前授权能力中的资料", object_schema({"query": {"type": "string", "minLength": 1}}, ["query"])))
        return result

    def _loop(self):
        futures = set()
        while not self._stop.is_set():
            self.wakeup.wait()
            if self._stop.is_set():
                break
            futures = {f for f in futures if not f.done()}
            if len(futures) >= 6:
                continue
            try:
                for run_id in self.store.runnable_runs():
                    if len(futures) >= 6:
                        break
                    lease = self.claim(run_id)
                    if lease:
                        futures.add(self._pool.submit(self.execute, run_id, *lease))
            except Exception:
                log.exception("harness scheduler iteration failed")

    def claim(self, run_id):
        def apply(run, changes, db):
            if run["status"] in TERMINAL:
                return None
            for task in list(run["tasks"].values()):
                if task["status"] == "completed" and task["fact_versions"]:
                    current = self.facts(run, task, db)
                    if any(identity not in current or current[identity]["version"] != version for identity, version in task["fact_versions"].items()):
                        task.update(status="queued", stale=True, ended_at=None)
                        task["messages"].append({"role": "user", "content": "所用事实版本已变更，请复核并更新结论。"})
                        changes.append({"event": "task_requires_review", "node_id": task["id"]})
                        self._invalidate_consumers(run, task["id"], changes)
            self._settle(run, changes, db)
            running = 0
            for task in run["tasks"].values():
                if task["status"] == "running":
                    if task["lease_until"] <= now():
                        task.update(status="queued", lease_token="", lease_until="")
                        changes.append({"event": "task_recovered", "node_id": task["id"]})
                    else:
                        running += 1
            if running >= run["policy"]["settings"]["max_parallel"]:
                return None
            for task in run["tasks"].values():
                if task["status"] != "queued":
                    continue
                token = identifier("execution")
                task.update(status="running", lease_token=token, lease_until=(datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat(),
                            started_at=task["started_at"] or now(), attempt=task["attempt"] + 1)
                task.setdefault("execution_attempts", []).append({"execution_id": token, "status": "running", "parent_id": task["id"], "started_at": now(), "lease_until": task["lease_until"]})
                changes.append({"event": "task_started", "node_id": task["id"]})
                return task["id"], token
            return None
        return self.store.mutate(run_id, apply)

    def _valid(self, run, task_id, token):
        task = run["tasks"][task_id]
        return run["status"] not in TERMINAL and task["status"] == "running" and task["lease_token"] == token and task["lease_until"] > now()

    def _heartbeat(self, run_id, task_id, token, finished):
        while not finished.wait(min(20, self.lease_seconds / 3)):
            def renew(run, changes, _db):
                if self._valid(run, task_id, token):
                    run["tasks"][task_id]["lease_until"] = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()
                    changes.append({"event": "lease_renewed", "node_id": task_id})
            try:
                self.store.mutate(run_id, renew)
            except Exception:
                log.exception("harness heartbeat failed")

    def execute(self, run_id, task_id, token):
        finished = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(run_id, task_id, token, finished), daemon=True)
        heartbeat.start()
        response = None
        try:
            run = self.store.get(run_id)
            if not self._valid(run, task_id, token):
                return
            task = run["tasks"][task_id]
            if task["pending_calls"]:
                self._execute_tools(run_id, task_id, token)
                return
            specs = self.specs(run, task)
            facts = self.facts(run, task)
            try:
                messages, diagnostics = compose(run, task, facts, specs)
            except HarnessError as exc:
                if exc.code != "HARNESS_CONTEXT_COMPRESSION_REQUIRED":
                    raise
                self._compress(run_id, task_id, token, run, task)
                return
            if task["model_calls"] >= run["policy"]["settings"]["max_model_calls"]:
                raise HarnessError("MODEL_BUDGET_EXCEEDED")
            # Admission, projection and trace are durable before the network call.
            def before(current, changes, _db):
                if not self._valid(current, task_id, token):
                    return False
                live = current["tasks"][task_id]
                if sum(t["model_calls"] for t in current["tasks"].values()) >= current["policy"]["settings"].get("max_run_model_calls", 96):
                    raise HarnessError("RUN_MODEL_BUDGET_EXCEEDED")
                live["model_calls"] += 1
                live["fact_versions"] = diagnostics["fact_versions"]
                live["trace"].append({"execution_id": token, "input": [serialize_message(m) for m in messages], "tools": [asdict(s) for s in specs], "diagnostics": diagnostics})
                changes.append({"event": "model_started", "node_id": task_id})
                return True
            if not self.store.mutate(run_id, before):
                return
            response = self.client_factory(run).complete_with_tools(messages, specs, request_purpose=f"harness_{task['kind']}")
            if not response.tool_calls and not response.final_text.strip():
                raise HarnessError("EMPTY_MODEL_REPLY")
            if self.moderation:
                if response.final_text.strip():
                    self.moderation.check(response.final_text, stage="output", session_id=run["session_id"], turn_id=task_id)
                for call in response.tool_calls:
                    if call.name == "request_user_input":
                        self.moderation.check(json.dumps(call.arguments, ensure_ascii=False), stage="output", session_id=run["session_id"], turn_id=task_id)
            def after(current, changes, db):
                if not self._valid(current, task_id, token):
                    return
                live = current["tasks"][task_id]
                live["trace"][-1]["output"] = asdict(response)
                if live["kind"] == "main" and response.tool_calls and response.final_text.strip():
                    current["phase"] = response.final_text.strip()[:600]
                # A changed dependency while the model was running invalidates this
                # response; retry against current facts instead of publishing stale work.
                fresh = self.facts(current, live, db)
                if {k: v["version"] for k, v in fresh.items()} != live["fact_versions"]:
                    live["status"] = "queued"
                    changes.append({"event": "task_facts_changed", "node_id": task_id})
                    return
                if response.tool_calls:
                    if len({c.id for c in response.tool_calls}) != len(response.tool_calls):
                        raise HarnessError("DUPLICATE_TOOL_CALL_ID")
                    live["messages"].append(serialize_message(ChatMessage(role="assistant", content="", tool_calls=response.tool_calls)))
                    for call in response.tool_calls:
                        node_id = identifier("tool")
                        if any(t["task_id"] == task_id and t["call_id"] == call.id for t in current["tools"].values()):
                            raise HarnessError("REUSED_TOOL_CALL_ID")
                        current["tools"][node_id] = {"id": node_id, "call_id": call.id, "task_id": task_id, "parent_id": task_id,
                            "name": call.name, "arguments": call.arguments, "status": "queued", "attempts": 0, "created_at": now(), "started_at": None, "ended_at": None}
                        live["pending_calls"].append(node_id)
                        changes.append({"event": "tool_created", "node_id": node_id})
                    live["status"] = "queued"
                else:
                    if live["kind"] == "main":
                        violations = reply_violations(current["goal"], response.final_text)
                        if violations:
                            if live.get("reply_validation_attempts", 0) >= 1:
                                raise HarnessError("REPLY_CONSTRAINT_FAILED")
                            live["reply_validation_attempts"] = 1
                            live["messages"].extend([{"role": "assistant", "content": response.final_text}, {"role": "user", "content": "回复未符合明确长度要求：" + "；".join(violations) + "。" + repair_instruction(current["goal"])}])
                            live["status"] = "queued"
                            changes.append({"event": "reply_validation_retry", "node_id": task_id})
                            return
                    unresolved = [t["id"] for t in current["tasks"].values() if t["id"] != task_id and t["status"] not in TERMINAL] if live["kind"] == "main" else []
                    if unresolved:
                        live.update(status="waiting_for_dependency", review_dependencies=unresolved)
                        changes.append({"event": "task_waiting", "node_id": task_id})
                        return
                    live.update(status="completed", result=response.final_text, ended_at=now())
                    if live["kind"] == "main":
                        current.update(status="completed", result=response.final_text)
                    changes.append({"event": "task_completed", "node_id": task_id})
                self._settle(current, changes, db)
            self.store.mutate(run_id, after)
        except Exception as exc:
            log.warning("harness execution failed (%s): %s", task_id, type(exc).__name__)
            self._fail(run_id, task_id, token, getattr(exc, "code", type(exc).__name__), response)
        finally:
            finished.set()

    def facts(self, run, task, db=None):
        result = deepcopy(run["seed_facts"])
        result.update(self.store.fact_records(run, task, db))
        versions = {identity: record["version"] for identity, record in result.items()}
        for record in result.values():
            if record.get("status") in {"derived", "candidate", "assumption"} and any(versions.get(identity) != version for identity, version in record.get("dependencies", {}).items()):
                record.update(status="expired", value=None, invalidated_by="dependency_version_changed")
        return {k: v for k, v in result.items() if v.get("status") in {"confirmed", "candidate", "assumption", "derived", "expired"}}

    def _compress(self, run_id, task_id, token, run, task):
        if len(task["messages"]) < 6 or task["model_calls"] >= run["policy"]["settings"]["max_model_calls"]:
            raise HarnessError("CONTEXT_BUDGET_EXCEEDED")
        # Split only before a user or assistant turn without tool results; retain
        # the entire most recent exchange and all pending calls.
        boundary = len(task["messages"]) - 4
        while boundary > 0 and (task["messages"][boundary]["role"] == "tool" or task["messages"][boundary - 1].get("tool_calls")):
            boundary -= 1
        if boundary < 2:
            raise HarnessError("CONTEXT_BUDGET_EXCEEDED")
        summarizer = deepcopy(task)
        summarizer["messages"] = task["messages"][:boundary] + [{"role": "user", "content": "请压缩上述历史，只输出带来源标识的简要任务摘要。"}]
        messages, diagnostics = compose(run, summarizer, self.facts(run, task), [], purpose="summary")
        def admit(current, changes, _db):
            if not self._valid(current, task_id, token):
                return False
            if sum(t["model_calls"] for t in current["tasks"].values()) >= current["policy"]["settings"].get("max_run_model_calls", 96):
                raise HarnessError("RUN_MODEL_BUDGET_EXCEEDED")
            live = current["tasks"][task_id]
            live["model_calls"] += 1
            live["trace"].append({"execution_id": token, "purpose": "summary", "input": [serialize_message(m) for m in messages], "tools": [], "diagnostics": diagnostics})
            changes.append({"event": "model_started", "node_id": task_id})
            return True
        if not self.store.mutate(run_id, admit):
            return
        summary = self.client_factory(run).complete(messages, request_purpose="harness_context_summary", max_tokens=2000)
        if not summary.strip():
            raise HarnessError("EMPTY_CONTEXT_SUMMARY")
        def save(current, changes, _db):
            if self._valid(current, task_id, token):
                live = current["tasks"][task_id]
                live.setdefault("archived_messages", []).extend(live["messages"][:boundary])
                live["messages"] = live["messages"][boundary:]
                live["trace"][-1]["output"] = {"final_text": summary}
                live.update(summary=summary, status="queued")
                changes.append({"event": "context_compressed", "node_id": task_id})
        self.store.mutate(run_id, save)

    def _execute_tools(self, run_id, task_id, token):
        initial = self.store.get(run_id)
        for node_id in initial["tasks"][task_id]["pending_calls"]:
            external = {}
            def begin(run, changes, db):
                if not self._valid(run, task_id, token):
                    return
                task, tool = run["tasks"][task_id], run["tools"][node_id]
                if tool["status"] in TOOL_TERMINAL or tool["status"] in {"waiting_for_user", "waiting_for_dependency"}:
                    return
                available = {s.name: s for s in self.specs(run, task)}
                try:
                    if tool["name"] not in available:
                        raise HarnessError("TOOL_NOT_AUTHORIZED", 403)
                    if tool["name"] == "execute_skill" and isinstance(tool["arguments"].get("skill_id"), str):
                        tool["arguments"]["skill_id"] = normalize_skill(tool["arguments"]["skill_id"])
                    validate(tool["arguments"], available[tool["name"]].parameters_schema)
                    if task["tool_calls"] >= run["policy"]["settings"]["max_tool_calls"]:
                        raise HarnessError("TOOL_BUDGET_EXCEEDED")
                    if sum(t["tool_calls"] for t in run["tasks"].values()) >= run["policy"]["settings"].get("max_run_tool_calls", 192):
                        raise HarnessError("RUN_TOOL_BUDGET_EXCEEDED")
                    tool.update(status="running", execution_id=token, started_at=tool["started_at"] or now(), attempts=tool["attempts"] + 1)
                    task["tool_calls"] += 1
                    changes.append({"event": "tool_started", "node_id": node_id})
                    handled = self._internal(run, task, tool, changes, db)
                    if not handled:
                        # Unknown side effects are never replayed after a crash.
                        retryable = tool["name"] in {"rag_search", "web_search", "subject_requirements_lookup"}
                        if not retryable and any(other["id"] != tool["id"] and other["task_id"] == task_id and other["name"] == tool["name"] and other["arguments"] == tool["arguments"] and isinstance(other.get("result"), dict) and other["result"].get("error") in {"TOOL_OUTCOME_UNKNOWN", "TOOL_RECONCILIATION_REQUIRED"} for other in run["tools"].values()):
                            raise HarnessError("TOOL_RECONCILIATION_REQUIRED")
                        if tool["attempts"] > 1 and not retryable:
                            raise HarnessError("TOOL_OUTCOME_UNKNOWN")
                        if tool["attempts"] > 3:
                            raise HarnessError("TOOL_RETRIES_EXHAUSTED")
                        external.update(run=deepcopy(run), task=deepcopy(task), tool=deepcopy(tool))
                except (HarnessError, ValidationError, SchemaError, ValueError) as exc:
                    self._tool_result(tool, {"error": getattr(exc, "code", "INVALID_TOOL_ARGUMENTS")}, "failed")
                    changes.append({"event": "tool_failed", "node_id": node_id})
            self.store.mutate(run_id, begin)
            if external:
                try:
                    result = self.tool_executor(external["run"], external["task"], external["tool"]) if self.tool_executor else {"error": "TOOL_UNAVAILABLE"}
                    error = result.get("error") if isinstance(result, dict) else None
                except Exception as exc:
                    result, error = {"error": type(exc).__name__}, type(exc).__name__
                def finish(run, changes, _db):
                    if not self._valid(run, task_id, token):
                        return
                    tool = run["tools"][node_id]
                    if isinstance(result, dict) and "_harness_runtime_state" in result:
                        run["tasks"][task_id]["runtime_state"] = result.pop("_harness_runtime_state")
                    if error and tool["name"] in {"rag_search", "web_search"} and tool["attempts"] < 3:
                        tool.update(status="queued", error=error)
                        changes.append({"event": "tool_retrying", "node_id": node_id})
                    else:
                        self._tool_result(tool, result, "failed" if error else "completed")
                        changes.append({"event": "tool_failed" if error else "tool_completed", "node_id": node_id})
                self.store.mutate(run_id, finish)
        def release(run, changes, db):
            if self._valid(run, task_id, token):
                task = run["tasks"][task_id]
                task["status"] = "waiting_for_dependency"
                task["finished_execution_id"] = token
                task["lease_token"] = ""
                changes.append({"event": "task_waiting", "node_id": task_id})
                self._settle(run, changes, db)
        self.store.mutate(run_id, release)

    @staticmethod
    def _tool_result(tool, result, status="completed"):
        tool.update(status=status, result=result, ended_at=now())

    def _internal(self, run, task, tool, changes, db):
        name, args = tool["name"], tool["arguments"]
        if name == "delegate_tasks":
            if task["kind"] != "main":
                raise HarnessError("DELEGATION_NOT_AUTHORIZED")
            items = args["tasks"]
            if len(run["tasks"]) + len(items) > run["policy"]["settings"].get("max_tasks", 64):
                raise HarnessError("TASK_BUDGET_EXCEEDED")
            keys = {i["key"] for i in items}
            if len(keys) != len(items):
                raise HarnessError("DUPLICATE_TASK_KEY")
            graph = {i["key"]: i.get("depends_on", []) for i in items}
            visited, stack = set(), set()
            def visit(key):
                if key not in graph or key in stack:
                    raise HarnessError("INVALID_TASK_DEPENDENCY")
                if key in visited:
                    return
                stack.add(key)
                for dependency in graph[key]:
                    visit(dependency)
                stack.remove(key)
                visited.add(key)
            for key in graph:
                visit(key)
            existing = {(t.get("expert_id"), t["goal"]) for t in run["tasks"].values() if t["status"] not in TERMINAL}
            if any((i["expert_id"], i["goal"]) in existing for i in items) or len({(i["expert_id"], i["goal"]) for i in items}) != len(items):
                raise HarnessError("DUPLICATE_EXPERT_TASK")
            children = {i["key"]: task_node("expert", i["goal"], task["id"], expert_id=i["expert_id"], completion=i.get("completion", "完成指定任务")) for i in items}
            for item in items:
                child = children[item["key"]]
                child["depends_on"] = [children[k]["id"] for k in item.get("depends_on", [])]
                if child["depends_on"]:
                    child["status"] = "waiting_for_dependency"
                run["tasks"][child["id"]] = child
                changes.append({"event": "task_created", "node_id": child["id"]})
            tool.update(status="waiting_for_dependency", children=[c["id"] for c in children.values()])
            changes.append({"event": "plan_updated", "node_id": task["id"]})
        elif name == "execute_skill":
            skill_id = check_skill(run, task, args["skill_id"])
            if len(run["tasks"]) >= run["policy"]["settings"].get("max_tasks", 64):
                raise HarnessError("TASK_BUDGET_EXCEEDED")
            limit = run["policy"]["experts"].get(task.get("expert_id"), {}).get("max_skill_calls", 8)
            if task["skill_calls"] >= limit:
                raise HarnessError("SKILL_BUDGET_EXCEEDED")
            child = task_node("skill", args["task"], task["id"], expert_id=task.get("expert_id"), skill_id=skill_id)
            run["tasks"][child["id"]] = child
            task["skill_calls"] += 1
            tool.update(status="waiting_for_dependency", children=[child["id"]])
            changes.append({"event": "task_created", "node_id": child["id"]})
        elif name == "request_user_input":
            self._request(run, task, tool, changes, db)
        elif name == "read_facts":
            self._tool_result(tool, self.facts(run, task, db))
            changes.append({"event": "tool_completed", "node_id": tool["id"]})
        elif name == "propose_fact_updates":
            results = []
            sources = [run["goal"]] + [json.dumps(q.get("answer", {}).get("answers", {}), ensure_ascii=False) for q in run["inputs"].values() if q["status"] == "answered"]
            for update in args["updates"]:
                if not update["evidence"].strip():
                    raise HarnessError("FACT_EVIDENCE_REQUIRED")
                if update["status"] == "assumption" and update["scope"] != "task":
                    raise HarnessError("ASSUMPTION_MUST_BE_LOCAL")
                self.store.fact_identity(run, task, update["scope"], update["key"])
                if update["status"] == "user_statement":
                    values = update["value"] if isinstance(update["value"], list) else [update["value"]]
                    if not any(update["evidence"] in source for source in sources) or not all(str(value) in update["evidence"] for value in values):
                        raise HarnessError("USER_FACT_EVIDENCE_INVALID")
            for update in sorted(args["updates"], key=lambda u: (u["scope"], u["key"])):
                if update["status"] == "user_statement":
                    evidence = update["evidence"]
                    identity = self.store.fact_identity(run, task, update["scope"], update["key"])
                    record = self.store.write_fact(db, identity, {**update, "status": "confirmed", "origin": "user_statement", "source": {"run_id": run["run_id"], "evidence": evidence, "tool_call_id": tool["call_id"]}})
                    run["seed_facts"].pop(identity, None)
                    self._invalidate_fact_version(run, identity, record["version"], changes)
                    results.append(record)
                    continue
                # Observations and derived artifacts cannot overwrite a user-confirmed
                # value. Their identities are separate from the canonical fact key.
                identity = self.store.fact_identity(run, task, update["scope"], f"{update['status']}:{update['key']}:{tool['id']}")
                results.append(self.store.write_fact(db, identity, {**update, "source": {"tool_call_id": tool["call_id"], "task_id": task["id"]}, "dependencies": task["fact_versions"]}))
            self._tool_result(tool, results)
            changes.append({"event": "facts_updated", "node_id": task["id"]})
        else:
            return False
        return True

    def _request(self, run, task, tool, changes, db):
        args = InputArguments.model_validate(tool["arguments"])
        fields = [f.model_dump() for f in args.fields]
        facts = self.facts(run, task, db)
        answers = {}
        identities = []
        for field in fields:
            key = field.get("fact_key") or field["key"]
            identity = self.store.fact_identity(run, task, field["scope"], key)
            identities.append(identity)
            record = facts.get(identity)
            if record is None:
                inherited = [r for r in facts.values() if r.get("key") == key and r.get("status") == "confirmed"]
                if inherited and all(r["value"] == inherited[0]["value"] for r in inherited):
                    record = inherited[0]
            if record and record.get("status") == "confirmed":
                answers[field["key"]] = record["value"]
        if len(set(identities)) != len(identities):
            raise HarnessError("DUPLICATE_FACT_FIELD")
        if len(answers) == len(fields) and not args.clarification_reason:
            self._tool_result(tool, {"answers": answers, "source": "existing_facts"})
            changes.append({"event": "tool_completed", "node_id": tool["id"]})
            return
        if answers and not args.clarification_reason:
            tool["existing_answers"] = answers
            remaining = [(field, identity) for field, identity in zip(fields, identities) if field["key"] not in answers]
            fields, identities = map(list, zip(*remaining))
        ancestors = {task["id"]}
        parent = task.get("parent_id")
        while parent:
            ancestors.add(parent)
            parent = run["tasks"][parent].get("parent_id")
        for previous in run["inputs"].values():
            if previous["status"] != "skipped" or args.clarification_reason or not any(b["task_id"] in ancestors for b in previous["bindings"]):
                continue
            if {f.get("fact_key") or f["key"] for f in fields} <= {f.get("fact_key") or f["key"] for f in previous["fields"]}:
                self._tool_result(tool, {"skipped": True, "answers": answers, "source_request_id": previous["id"]}, "skipped")
                changes.append({"event": "tool_completed", "node_id": tool["id"]})
                return
        signature = json.dumps({"fields": sorted([{ "identity": identity, **{k: v for k, v in field.items() if k not in {"key", "label", "fact_key", "scope"}}} for field, identity in zip(fields, identities)], key=lambda f: f["identity"]), "reason": args.clarification_reason}, ensure_ascii=False, sort_keys=True)
        for request in run["inputs"].values():
            if request["signature"] == signature and request["status"] == "skipped":
                self._tool_result(tool, request["answer"], "skipped")
                changes.append({"event": "tool_completed", "node_id": tool["id"]})
                return
            if request["signature"] == signature and request["status"] == "pending":
                local_keys = {identity: field["key"] for field, identity in zip(fields, identities)}
                field_map = {field["key"]: local_keys[identity] for field, identity in zip(request["fields"], request["identities"])}
                request["bindings"].append({"task_id": task["id"], "tool_id": tool["id"], "field_map": field_map})
                tool.update(status="waiting_for_user", input_request_id=request["id"])
                changes.append({"event": "input_requested", "node_id": task["id"]})
                return
        owner = task
        while owner["kind"] == "skill":
            owner = run["tasks"][owner["parent_id"]]
        round_no = 1 + sum(q.get("owner_task_id", q["bindings"][0]["task_id"]) == owner["id"] for q in run["inputs"].values())
        request = {"id": identifier("input"), "title": args.title, "fields": fields, "status": "pending", "version": 1,
                   "round": round_no, "signature": signature, "identities": identities, "created_at": now(),
                   "owner_task_id": owner["id"],
                   "bindings": [{"task_id": task["id"], "tool_id": tool["id"]}], "clarification_reason": args.clarification_reason}
        run["inputs"][request["id"]] = request
        tool.update(status="waiting_for_user", input_request_id=request["id"])
        changes.append({"event": "input_requested", "node_id": task["id"]})

    def answer(self, request_id, submission):
        run_id = self.store.request_run(request_id)
        def apply(run, changes, db):
            if run["user_id"] != submission.user_id or run["profile_id"] != submission.profile_id:
                raise HarnessError("HARNESS_ACCESS_DENIED", 403)
            request = run["inputs"][request_id]
            if request.get("submission_id") == submission.submission_id:
                if request.get("submission") != submission.model_dump():
                    raise HarnessError("SUBMISSION_ID_REUSED", 409)
                return {"request_id": request_id, "status": request["status"], "run_id": run_id}
            if run["status"] in TERMINAL or request["status"] != "pending" or request["version"] != submission.expected_version:
                raise HarnessError("INPUT_REQUEST_INACTIVE", 409)
            answer = validate_answers(request["fields"], submission)
            request.update(status="skipped" if submission.skipped else "answered", answer=answer, submission_id=submission.submission_id,
                           submission=submission.model_dump(), version=request["version"] + 1)
            task = run["tasks"][request["bindings"][0]["task_id"]]
            for field in sorted(request["fields"], key=lambda f: request["identities"][request["fields"].index(f)]):
                if field["key"] not in answer["answers"]:
                    continue
                key = field.get("fact_key") or field["key"]
                identity = self.store.fact_identity(run, task, field["scope"], key)
                record = self.store.write_fact(db, identity, {"key": key, "scope": field["scope"], "value": answer["answers"][field["key"]],
                    "status": "confirmed", "source": {"input_request_id": request_id, "submission_id": submission.submission_id}})
                run["seed_facts"].pop(identity, None)
                self._invalidate_fact_version(run, identity, record["version"], changes)
            for binding in request["bindings"]:
                tool = run["tools"][binding["tool_id"]]
                if run["tasks"][binding["task_id"]]["status"] in {"failed", "cancelled"}:
                    continue
                field_map = binding.get("field_map", {})
                local = {**answer, "answers": {**tool.get("existing_answers", {}), **{field_map.get(key, key): value for key, value in answer["answers"].items()}}}
                self._tool_result(tool, local, "skipped" if submission.skipped else "completed")
                changes.append({"event": "input_answered", "node_id": binding["task_id"]})
            self._settle(run, changes, db)
            return {"request_id": request_id, "status": request["status"], "run_id": run_id}
        return self.store.mutate(run_id, apply)

    def _invalidate_fact_version(self, run, identity, version, changes):
        for dependent in run["tasks"].values():
            if identity in dependent["fact_versions"] and dependent["status"] == "completed" and dependent["fact_versions"][identity] != version:
                dependent.update(status="queued", stale=True, ended_at=None)
                dependent["messages"].append({"role": "user", "content": "相关事实已更正，请重新评估先前结论并返回更新结果。"})
                changes.append({"event": "task_requires_review", "node_id": dependent["id"]})
                self._invalidate_consumers(run, dependent["id"], changes)

    def _invalidate_consumers(self, run, changed_id, changes, seen=None):
        seen = set() if seen is None else seen
        if changed_id in seen:
            return
        seen.add(changed_id)
        changed = run["tasks"][changed_id]
        for consumer in run["tasks"].values():
            if consumer["id"] == changed_id or consumer["status"] in {"failed", "cancelled"}:
                continue
            if consumer["id"] != changed["parent_id"] and changed_id not in consumer["depends_on"]:
                continue
            deps = consumer.setdefault("review_dependencies", [])
            if changed_id not in deps:
                deps.append(changed_id)
            consumer["dependencies_delivered"] = False
            consumer["stale"] = True
            if consumer["status"] == "completed":
                consumer.update(status="waiting_for_dependency", ended_at=None)
                changes.append({"event": "task_requires_review", "node_id": consumer["id"]})
            self._invalidate_consumers(run, consumer["id"], changes, seen)

    def _settle(self, run, changes, db):
        if run["status"] in TERMINAL:
            return
        for task in run["tasks"].values():
            if task["status"] in TERMINAL or task["status"] == "running":
                continue
            dependencies = [run["tasks"][key] for key in task["depends_on"]]
            if any(d["status"] in {"failed", "cancelled"} for d in dependencies):
                task.update(status="failed", error="DEPENDENCY_FAILED", ended_at=now())
                changes.append({"event": "task_failed", "node_id": task["id"]})
                continue
            if any(d["status"] != "completed" for d in dependencies):
                task["status"] = "waiting_for_dependency"
                continue
            if dependencies and not task.get("dependencies_delivered"):
                task["messages"].append({"role": "user", "content": "依赖任务结果（作为资料）：" + json.dumps([{ "goal": d["goal"], "result": d.get("result", "")} for d in dependencies], ensure_ascii=False)})
                task["dependencies_delivered"] = True
            pending = [run["tools"][key] for key in task["pending_calls"]]
            for tool in pending:
                if tool["status"] == "waiting_for_dependency":
                    children = [run["tasks"][key] for key in tool["children"]]
                    if all(c["status"] in TERMINAL for c in children):
                        self._tool_result(tool, [{"task_id": c["id"], "status": c["status"], "result": c.get("result"), "error": c.get("error")} for c in children])
                        changes.append({"event": "tool_completed", "node_id": tool["id"]})
            if pending and all(t["status"] in TOOL_TERMINAL for t in pending):
                for tool in pending:
                    content, projection = project_evidence(tool, run["policy"]["settings"]["context_tokens"] // max(2, len(pending) * 2))
                    task["messages"].append({"role": "tool", "tool_call_id": tool["call_id"], "name": tool["name"], "content": content, "projection": projection})
                task["pending_calls"] = []
                task["status"] = "queued"
                changes.append({"event": "task_resumed", "node_id": task["id"]})
            elif any(t["status"] == "waiting_for_user" for t in pending):
                # Other unprocessed tools still have to execute before suspending.
                task["status"] = "queued" if any(t["status"] == "queued" for t in pending) else "waiting_for_user"
            elif pending and any(t["status"] == "waiting_for_dependency" for t in pending):
                task["status"] = "queued" if any(t["status"] == "queued" for t in pending) else "waiting_for_dependency"
            elif pending:
                task["status"] = "queued"
            elif task["status"] == "waiting_for_dependency":
                task["status"] = "queued"
            review = [run["tasks"][key] for key in task.get("review_dependencies", [])]
            if review and not task["pending_calls"]:
                if any(t["status"] not in TERMINAL for t in review):
                    task["status"] = "waiting_for_dependency"
                else:
                    task["messages"].append({"role": "user", "content": "事实更正后的更新结果（替代此前结果）：" + json.dumps([{ "task_id": t["id"], "status": t["status"], "result": t.get("result"), "error": t.get("error")} for t in review], ensure_ascii=False)})
                    task.update(review_dependencies=[], status="queued", stale=False)
                    changes.append({"event": "task_resumed", "node_id": task["id"]})

    def _fail(self, run_id, task_id, token, error, response=None):
        def apply(run, changes, db):
            if self._valid(run, task_id, token):
                task = run["tasks"][task_id]
                if task["trace"]:
                    task["trace"][-1]["error"] = error
                    if response is not None:
                        task["trace"][-1]["output"] = asdict(response)
                task.update(status="failed", error=error, ended_at=now())
                if task["kind"] == "main":
                    run.update(status="failed", error=error)
                    self._close_descendants(run, changes)
                else:
                    self._close_descendants(run, changes, task_id)
                changes.append({"event": "task_failed", "node_id": task_id})
                self._settle(run, changes, db)
        self.store.mutate(run_id, apply)

    def _close_descendants(self, run, changes, ancestor=None):
        closed = set(run["tasks"]) if ancestor is None else {ancestor}
        if ancestor is not None:
            while True:
                children = {t["id"] for t in run["tasks"].values() if t["parent_id"] in closed}
                if children <= closed:
                    break
                closed.update(children)
        for node in [*run["tasks"].values(), *run["tools"].values()]:
            owner = node.get("task_id", node["id"])
            if owner in closed and node["status"] not in TOOL_TERMINAL:
                node.update(status="cancelled", ended_at=now(), lease_token="")
                changes.append({"event": "node_cancelled", "node_id": node["id"]})
        for request in run["inputs"].values():
            if request["status"] == "pending" and all(b["task_id"] in closed or run["tasks"][b["task_id"]]["status"] in TERMINAL for b in request["bindings"]):
                request.update(status="expired", version=request["version"] + 1)

    def cancel(self, run_id):
        def apply(run, changes, _db):
            if run["status"] in TERMINAL:
                return
            run["status"] = "cancelled"
            self._close_descendants(run, changes)
            changes.append({"event": "run_cancelled", "node_id": run["root_task_id"]})
        self.store.mutate(run_id, apply)

    def public(self, run):
        labels = {
            "delegate_tasks": "分派专家任务",
            "execute_skill": "执行专项分析",
            "request_user_input": "补充信息",
            "rag_search": "检索知识资料",
            "web_search": "搜索外部资料",
            "read_facts": "读取相关信息",
            "propose_fact_updates": "记录观察",
            "subject_requirements": "查询选科要求",
            "subject_requirements_lookup": "查询选科要求",
        }
        nodes = []
        for task in run["tasks"].values():
            actor = "主 agent" if task["kind"] == "main" else run["policy"]["experts"].get(task.get("expert_id"), {}).get("name", "主 agent")
            title = run["policy"]["skills"][task["skill_id"]]["name"] if task["kind"] == "skill" else task["goal"]
            nodes.append({
                "id": task["id"], "parent_id": task["parent_id"], "kind": task["kind"], "title": title, "actor": actor,
                "status": task["status"], "created_at": task.get("created_at"), "started_at": task["started_at"],
                "ended_at": task["ended_at"], "error": task.get("error"), "stale": task.get("stale", False),
            })
        for tool in run["tools"].values():
            nodes.append({
                "id": tool["id"], "parent_id": tool["task_id"], "kind": "tool",
                "title": labels.get(tool["name"], "执行专项工具"), "status": tool["status"],
                "created_at": tool.get("created_at"), "started_at": tool["started_at"], "ended_at": tool["ended_at"],
                "attempts": tool["attempts"],
            })
        # Tasks were previously appended before all tools, so "分派专家任务" appeared
        # under its expert children. Sort siblings by timeline instead.
        nodes.sort(key=lambda n: (n.get("created_at") or n.get("started_at") or "", n["id"]))
        requests = [{k: deepcopy(v) for k, v in q.items() if k in {"id", "title", "fields", "status", "version", "round", "answer", "bindings", "clarification_reason"}} for q in run["inputs"].values()]
        return {"run_id": run["run_id"], "session_id": run["session_id"], "profile_id": run["profile_id"], "goal": run["goal"], "status": run["status"], "version": run["version"], "root_task_id": run["root_task_id"], "nodes": nodes, "inputs": requests,
                "created_at": run["created_at"], "updated_at": run["updated_at"], "phase": run.get("phase", ""), "result": run.get("result", ""), "error": run.get("error"),
                "completed_count": sum(t["status"] == "completed" for t in run["tasks"].values() if t["kind"] != "main"), "task_count": sum(t["kind"] != "main" for t in run["tasks"].values()),
                "waiting_count": sum(q["status"] == "pending" for q in run["inputs"].values())}
