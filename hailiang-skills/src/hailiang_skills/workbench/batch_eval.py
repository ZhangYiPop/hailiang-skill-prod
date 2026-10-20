"""Run persona-backed batch conversations against a workbench revision.

Each case uses the same debug-session snapshot as the manual test console:
the selected revision (and optional Soul revision) is frozen before the first
turn. Follow-up forms are answered from the persona. Free-text clarifications
start another round in the same harness session.
"""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
from threading import RLock, Semaphore, Thread
from typing import Any

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.deployment import deployment_environment
from hailiang_skills.harness.contracts import AnswerSubmission, HarnessError
from hailiang_skills.harness.policy import capture_policy, snapshot_root
from hailiang_skills.harness.store import HarnessStore
from hailiang_skills.llm.config import UnknownTesterModelError
from hailiang_skills.skill_runtime.models import ChatMessage
from hailiang_skills.skills.common import extract_grade
from hailiang_skills.workbench import WorkbenchError
from hailiang_skills.workbench.persona import answer_fields, parse_persona

TERMINAL = {"completed", "failed", "cancelled"}
# Fixed at the harness execution-pool size. The workbench control is locked
# until pool sizes can be raised without affecting live traffic.
BATCH_CONCURRENCY = 6


class BatchEvalService:
    def __init__(self, workbench, repository, fact_service, orchestrator, runtime):
        self.workbench = workbench
        self.repository = repository
        self.fact_service = fact_service
        self.orchestrator = orchestrator
        self.runtime = runtime
        self._jobs: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, Thread] = {}
        self._lock = RLock()

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if any(job["status"] == "running" for job in self._jobs.values()):
                raise WorkbenchError("已有批量测评在运行，请等它结束或停止后再开始", code="BATCH_EVAL_BUSY")
            job_id = f"batch_{uuid.uuid4().hex[:12]}"
            model_choice, resolved_model = resolve_batch_model(self.orchestrator, payload.get("model") or "flash")
            cases = []
            for index, item in enumerate(payload["cases"], start=1):
                cases.append({
                    "index": index,
                    "scene": str(item.get("scene") or "").strip(),
                    "opening": str(item["opening"]).strip(),
                    "persona": str(item["persona"]).strip(),
                    "goal": str(item.get("goal") or "").strip(),
                    "status": "pending",
                    "rounds": 0,
                    "question_count": 0,
                    "stop_reason": "",
                    "elapsed_s": 0,
                    "experts": [],
                    "result": "",
                    "nodes": [],
                    "transcript": [],
                    "live_phase": "",
                    "error": "",
                })
            job = {
                "job_id": job_id,
                "status": "running",
                "revision_id": payload["revision_id"],
                "soul_revision_id": payload.get("soul_revision_id") or None,
                "actor_id": payload["actor_id"],
                "persona_mode": payload.get("persona_mode") or "simulator",
                "speaker_role": payload.get("speaker_role") if payload.get("speaker_role") in {"家长", "学生"} else "家长",
                "model": model_choice,
                "resolved_model": resolved_model,
                "preset_identity": bool(payload.get("preset_identity", True)),
                "max_rounds": int(payload.get("max_rounds") or 4),
                "concurrency": _bounded_concurrency(payload.get("concurrency")),
                "round_timeout_s": int(payload.get("round_timeout_s") or 180),
                "created_at": time.time(),
                "updated_at": time.time(),
                "total": len(cases),
                "finished": 0,
                "current_index": 0,
                "error": "",
                "cancel": False,
                "cases": cases,
            }
            self._jobs[job_id] = job
            thread = Thread(target=self._execute, args=(job_id,), name=f"batch-eval-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise WorkbenchError("批量测评不存在", code="BATCH_EVAL_NOT_FOUND")
            return _public(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise WorkbenchError("批量测评不存在", code="BATCH_EVAL_NOT_FOUND")
            job["cancel"] = True
            job["updated_at"] = time.time()
        return self.get(job_id)

    def export_csv(self, job_id: str) -> str:
        job = self.get(job_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "序号", "场景", "首轮对话", "画像", "会话目标", "状态", "轮数", "追问次数",
            "结束原因", "命中专家", "耗时秒", "节点过程", "对话记录", "最终回复", "错误",
        ])
        for case in job["cases"]:
            writer.writerow([
                case["index"],
                case["scene"],
                case["opening"],
                case["persona"],
                case["goal"],
                case["status"],
                case["rounds"],
                case["question_count"],
                case["stop_reason"],
                "、".join(case["experts"]),
                case["elapsed_s"],
                " > ".join(f"第{node['round']}轮 {node['actor']} {node['title']}({node['status']})" for node in case["nodes"]),
                "\n".join(f"{row['role']}: {row['content']}" for row in case["transcript"]),
                case["result"],
                case["error"],
            ])
        return buffer.getvalue()

    def _execute(self, job_id: str) -> None:
        job = self._jobs[job_id]
        limit = max(1, min(int(job["concurrency"]), len(job["cases"]) or 1))
        gate = Semaphore(limit)
        threads: list[Thread] = []

        def run(case: dict[str, Any]) -> None:
            try:
                self._execute_case(job, case)
            finally:
                gate.release()

        try:
            for case in job["cases"]:
                gate.acquire()
                if job["cancel"]:
                    case["status"] = "cancelled"
                    case["stop_reason"] = "已停止"
                    self._note_finished(job)
                    gate.release()
                    continue
                thread = Thread(
                    target=run,
                    args=(case,),
                    name=f"batch-case-{job_id}-{case['index']}",
                    daemon=True,
                )
                threads.append(thread)
                thread.start()
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            for thread in threads:
                thread.join()
        if job["status"] == "running":
            job["status"] = "cancelled" if job["cancel"] else "completed"
        job["updated_at"] = time.time()

    def _execute_case(self, job: dict[str, Any], case: dict[str, Any]) -> None:
        if job["cancel"]:
            case["status"] = "cancelled"
            case["stop_reason"] = "已停止"
            self._note_finished(job)
            return
        with self._lock:
            case["status"] = "running"
            job["current_index"] = case["index"]
            job["updated_at"] = time.time()
        try:
            self._run_case(job, case)
        except Exception as exc:  # one case must not stop the batch
            case["status"] = "failed"
            case["error"] = f"{type(exc).__name__}: {exc}"
            case["stop_reason"] = "运行失败"
        self._note_finished(job)

    def _note_finished(self, job: dict[str, Any]) -> None:
        with self._lock:
            job["finished"] += 1
            job["updated_at"] = time.time()

    def _run_case(self, job: dict[str, Any], case: dict[str, Any]) -> None:
        started = time.monotonic()
        persona = parse_persona(case["persona"], case["opening"], user_role=job["speaker_role"])
        debug = self.workbench.create_revision_test_session(
            job["revision_id"],
            soul_revision_id=job["soul_revision_id"],
            actor_id=job["actor_id"],
        )
        debug_id = debug["debug_session_id"]
        session_id = f"h{debug_id}"
        user_id = f"workbench-batch:{job['actor_id']}"[:160]
        facts = persona.seed_facts(
            preset_identity=job["preset_identity"],
            seed_profile=job["persona_mode"] == "seed",
        )
        utterance = case["opening"]
        last_result = ""
        for round_no in range(1, job["max_rounds"] + 1):
            if job["cancel"]:
                case["status"] = "cancelled"
                case["stop_reason"] = "已停止"
                break
            case["rounds"] = round_no
            case["live_phase"] = "starting"
            nodes_before_round = list(case.get("nodes") or [])
            run_id = self._start_round(session_id, user_id, debug_id, utterance, facts if round_no == 1 else {}, job["resolved_model"])
            case["transcript"].append({"role": "user", "content": utterance, "source": "首轮" if round_no == 1 else "模拟用户"})
            public = self._wait_round(job, case, persona, run_id, user_id, nodes_before_round)
            last_result = str(public.get("result") or "")
            case["result"] = last_result
            case["transcript"].append({"role": "assistant", "content": last_result or f"（{public.get('status')}）"})
            case["nodes"] = nodes_before_round + _nodes(public, round_no)
            case["live_phase"] = str(public.get("phase") or "")
            case["experts"] = _experts(case["experts"], public)
            if public.get("status") == "failed":
                case["status"] = "failed"
                case["error"] = str(public.get("error") or "运行失败")
                case["stop_reason"] = "运行失败"
                break
            if public.get("status") == "cancelled":
                case["status"] = "cancelled"
                case["stop_reason"] = "已停止"
                break
            decision = self._decide(persona, case["goal"], last_result, round_no, job["max_rounds"], self._model_client(job))
            if decision["kind"] != "answer":
                case["status"] = "completed"
                case["stop_reason"] = decision["reason"]
                break
            utterance = decision["utterance"]
        else:
            case["status"] = "completed"
            case["stop_reason"] = "达到最大轮数"
        if not case["status"] or case["status"] == "running":
            case["status"] = "completed"
        case["elapsed_s"] = round(time.monotonic() - started, 1)

    def _start_round(self, session_id: str, user_id: str, debug_id: str, goal: str, facts: dict[str, str], model: str) -> str:
        data = ContextData(user_id=user_id, facts=dict(facts), grade=facts.get("grade") or None)
        context, created = open_or_resume_session(
            self.repository,
            self.fact_service,
            session_id=session_id,
            data=data,
            context_scope="unbound",
            allow_context_seed=True,
        )
        if created:
            snapshot = self.workbench.debug_configuration_snapshot(debug_id)
            context.session_meta["configuration_snapshot"] = snapshot
            context.session_meta["workbench_debug_session_id"] = debug_id
            root = snapshot_root(snapshot)
            if root.get("object_type") == "expert_team":
                context.session_meta["expert_team_id"] = root.get("object_key")
        elif not context.session_meta.get("workbench_debug_session_id"):
            raise WorkbenchError("调试快照没有绑定到会话", code="BATCH_EVAL_SNAPSHOT_MISSING")
        context.session_meta["requested_llm_model"] = model
        policy = capture_policy(self.orchestrator, context)
        if not context.title:
            context.title = goal[:40]
        self.repository.save(context)
        dummy = {"session_id": session_id, "user_id": user_id, "profile_id": None}
        seed = {}
        for scope, records in [("user", context.shared_facts), ("profile", context.profile_facts), ("session", context.session_facts)]:
            for key, record in records.facts.items():
                identity = HarnessStore.fact_identity(dummy, {"id": "seed"}, scope, key)
                seed[identity] = {
                    "key": key,
                    "value": record.value,
                    "scope": scope,
                    "status": record.status,
                    "version": 1,
                    "source": record.model_dump(),
                }
        explicit_grade = extract_grade(goal)
        if explicit_grade:
            identity = HarnessStore.fact_identity(dummy, {"id": "seed"}, "session", "grade")
            seed[identity] = {
                "key": "grade",
                "value": explicit_grade,
                "scope": "session",
                "status": "confirmed",
                "version": 1,
                "source": {"source_type": "user_input", "source_id": f"{session_id}:goal", "evidence": explicit_grade},
            }
        history = []
        for previous in sorted(self.runtime.store.runs(session_id), key=lambda item: item["created_at"])[-10:]:
            if previous.get("profile_id") is not None:
                continue
            history.append({"role": "user", "content": previous["goal"]})
            if previous.get("result"):
                history.append({"role": "assistant", "content": previous["result"]})
        return self.runtime.create(session_id, user_id, None, goal, policy, seed, history)

    def _wait_round(
        self,
        job,
        case,
        persona,
        run_id: str,
        user_id: str,
        baseline_nodes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        answered: set[str] = set()
        deadline = time.monotonic() + job["round_timeout_s"]
        round_no = int(case.get("rounds") or 1)
        prior_nodes = list(baseline_nodes if baseline_nodes is not None else case.get("nodes") or [])
        while time.monotonic() < deadline:
            if job["cancel"]:
                self.runtime.cancel(run_id)
                public = self.runtime.public(self.runtime.store.get(run_id))
                public["status"] = "cancelled"
                return public
            run = self.runtime.store.get(run_id)
            # Publish live task-tree progress so the workbench poll can show
            # more than "running" while a long round is still open.
            public_live = self.runtime.public(run)
            case["live_phase"] = str(public_live.get("phase") or "")
            case["nodes"] = prior_nodes + _nodes(public_live, round_no)
            case["experts"] = _experts(case.get("experts") or [], public_live)
            job["updated_at"] = time.time()
            for request in run["inputs"].values():
                if request["status"] != "pending" or request["id"] in answered:
                    continue
                answers, sources = answer_fields(request["fields"], persona)
                skipped = not answers
                try:
                    self.runtime.answer(request["id"], AnswerSubmission(
                        user_id=user_id,
                        profile_id=None,
                        submission_id=f"batch_{request['id']}"[:100],
                        expected_version=request["version"],
                        skipped=skipped,
                        answers={} if skipped else answers,
                    ))
                except HarnessError:
                    if not skipped:
                        self.runtime.answer(request["id"], AnswerSubmission(
                            user_id=user_id,
                            profile_id=None,
                            submission_id=f"batch_{request['id']}_skip"[:100],
                            expected_version=request["version"],
                            skipped=True,
                            answers={},
                        ))
                    skipped = True
                    answers = {}
                answered.add(request["id"])
                case["question_count"] += 1
                case["transcript"].append({
                    "role": "assistant",
                    "content": _question_text(request),
                })
                case["transcript"].append({
                    "role": "user",
                    "content": "不清楚，画像里没有这项。" if skipped else _answer_text(request, answers),
                    "source": "画像缺失" if skipped else "、".join(sources.values()),
                })
            run = self.runtime.store.get(run_id)
            if run["status"] in TERMINAL:
                return self.runtime.public(run)
            time.sleep(0.4)
        self.runtime.cancel(run_id)
        public = self.runtime.public(self.runtime.store.get(run_id))
        public["status"] = "failed"
        public["error"] = public.get("error") or "本轮超时"
        return public

    def _model_client(self, job: dict[str, Any]):
        model = str(job.get("resolved_model") or "")
        factory = getattr(self.orchestrator, "_runtime_client_for_context", None)
        if model and callable(factory):
            return factory(_ModelScope(model))
        return getattr(self.orchestrator, "runtime_client", None)

    def _decide(self, persona, session_goal: str, assistant_text: str, round_no: int, max_rounds: int, client=None) -> dict[str, str]:
        if round_no >= max_rounds:
            return {"kind": "done", "utterance": "", "reason": "达到最大轮数"}
        if client is None:
            client = getattr(self.orchestrator, "runtime_client", None)
        enabled = bool(getattr(getattr(self.orchestrator, "llm_config", None), "enabled", False))
        if client is not None and enabled and assistant_text.strip():
            try:
                raw = client.complete(
                    [
                        ChatMessage(role="system", content=_SIMULATOR_RULES),
                        ChatMessage(role="user", content=json.dumps({
                            "发言身份": persona.user_role,
                            "画像": persona.raw,
                            "会话目标": session_goal,
                            "助手本轮回复": assistant_text[:4000],
                        }, ensure_ascii=False)),
                    ],
                    request_purpose="batch_eval_simulator",
                    max_tokens=400,
                )
                parsed = _parse_decision(raw)
                if parsed is not None:
                    return parsed
            except Exception:
                pass
        return _heuristic(persona, assistant_text, round_no)


def _bounded_concurrency(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = BATCH_CONCURRENCY
    return min(BATCH_CONCURRENCY, max(1, parsed))


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "revision_id": job["revision_id"],
        "soul_revision_id": job["soul_revision_id"],
        "persona_mode": job["persona_mode"],
        "speaker_role": job["speaker_role"],
        "model": job["model"],
        "preset_identity": job["preset_identity"],
        "max_rounds": job["max_rounds"],
        "concurrency": job.get("concurrency") or BATCH_CONCURRENCY,
        "total": job["total"],
        "finished": job["finished"],
        "current_index": job["current_index"],
        "error": job["error"],
        "cases": [
            {
                "index": case["index"],
                "scene": case["scene"],
                "opening": case["opening"],
                "persona": case["persona"],
                "goal": case["goal"],
                "status": case["status"],
                "rounds": case["rounds"],
                "question_count": case["question_count"],
                "stop_reason": case["stop_reason"],
                "elapsed_s": case["elapsed_s"],
                "experts": list(case["experts"]),
                "result": case["result"],
                "nodes": list(case["nodes"]),
                "transcript": list(case["transcript"]),
                "live_phase": case.get("live_phase") or "",
                "error": case["error"],
            }
            for case in job["cases"]
        ],
    }


def _nodes(public: dict[str, Any], round_no: int) -> list[dict[str, Any]]:
    nodes = []
    for node in public.get("nodes") or []:
        nodes.append({
            "round": round_no,
            "kind": node.get("kind") or "",
            "title": node.get("title") or "",
            "actor": node.get("actor") or "",
            "status": node.get("status") or "",
        })
    return nodes


def _experts(current: list[str], public: dict[str, Any]) -> list[str]:
    found = list(current)
    for node in public.get("nodes") or []:
        if node.get("kind") == "expert":
            name = str(node.get("actor") or "")
            if name and name not in found:
                found.append(name)
    for subtask in (public.get("orchestration") or {}).get("subtasks") or []:
        expert_id = str(subtask.get("target_expert_id") or "")
        if expert_id and expert_id not in found:
            found.append(expert_id)
    return found


def _question_text(request: dict[str, Any]) -> str:
    labels = "、".join(str(item.get("label") or item.get("key") or "") for item in request.get("fields") or [])
    title = str(request.get("title") or "补充信息")
    return f"{title}：{labels}" if labels else title


def _answer_text(request: dict[str, Any], answers: dict) -> str:
    labels = {item.get("key"): item.get("label") or item.get("key") for item in request.get("fields") or []}
    return "；".join(f"{labels.get(key, key)}：{value}" for key, value in answers.items())


_SIMULATOR_RULES = """你在扮演评测里的真实用户，根据给定画像回答助手。
只输出一个 JSON 对象，不要解释：
{"kind":"answer"|"done","utterance":"","reason":""}
规则：
- 画像描述的是孩子。发言身份为家长时用家长口吻谈孩子；为学生时用孩子本人的口吻，把画像里的情况说成自己的。
- 助手还在追问、澄清或要求补充信息时，kind 用 answer。utterance 只回答当前问题，事实必须来自画像；画像没有就说「不清楚」。
- 助手已经给出针对会话目标的结论，或没有继续提问时，kind 用 done，utterance 留空。
- 会话目标为空时，拿到一次具体建议就 done。
- 不要主动把整份画像倒出去，不要编造画像里没有的分数、城市或意向。"""


class _ModelScope:
    def __init__(self, model: str):
        self.session_meta = {"requested_llm_model": model}
        self.user_id = ""


def resolve_batch_model(orchestrator, requested: str) -> tuple[str, str]:
    """Map a debug-console model id to the configured model name.

    The choice stays visible even when this environment keeps the default model.
    """
    choice = str(requested or "").strip() or "flash"
    resolver = getattr(getattr(orchestrator, "llm_config", None), "resolve_tester_model", None)
    if not callable(resolver):
        return choice, ""
    try:
        return choice, str(resolver(choice, environment=deployment_environment()) or "")
    except UnknownTesterModelError as exc:
        raise WorkbenchError("不在调试台可选模型里", code="UNKNOWN_MODEL") from exc


def _parse_decision(raw: str) -> dict[str, str] | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    kind = str(payload.get("kind") or "")
    if kind not in {"answer", "done"}:
        return None
    utterance = str(payload.get("utterance") or "").strip()
    if kind == "answer" and not utterance:
        return None
    reason = str(payload.get("reason") or "").strip() or ("继续回答追问" if kind == "answer" else "模拟用户判定结束")
    return {"kind": kind, "utterance": utterance, "reason": reason}


def _heuristic(persona, assistant_text: str, round_no: int) -> dict[str, str]:
    text = assistant_text or ""
    asking = any(token in text for token in ("？", "?", "请补充", "请告诉", "哪一", "是否"))
    if asking and round_no == 1:
        return {"kind": "answer", "utterance": persona.brief_reply(), "reason": "按画像回答追问"}
    if asking:
        return {"kind": "answer", "utterance": "这些我暂时不清楚，请按已经了解的情况继续给建议。", "reason": "画像里没有更多信息"}
    return {"kind": "done", "utterance": "", "reason": "回复没有继续提问"}

