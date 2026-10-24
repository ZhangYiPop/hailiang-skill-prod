"""Local visual console for checking one agent turn.

It calls the same MainPlannerOrchestrator.handle_message path as the product
API, then returns the new event trace, the reply, and any form that was staged.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SKILLS_SRC = ROOT.parent / "hailiang-skills" / "src"
if str(SKILLS_SRC) not in sys.path:
    sys.path.insert(0, str(SKILLS_SRC))

HOST = os.environ.get("DEBUG_CONSOLE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEBUG_CONSOLE_PORT", "8765"))
MAX_TEXT = 1600

_LOCK = threading.Lock()
_ORCHESTRATOR = None
_ORCHESTRATOR_ERROR = ""
_SESSIONS: dict[str, Any] = {}


KEY_HINTS = (
    "expert",
    "questionnaire",
    "form",
    "skill",
    "route",
    "handoff",
    "script",
    "reply_progress",
    "ms_agent",
    "intent",
    "fact",
    "path",
)

EVENT_LABELS = {
    "expert_form_requested": "专家请求提问表单",
    "expert_skill_handoff_confirmed": "专家确认进入 Skill",
    "expert_skill_executed": "专家已执行 Skill",
    "expert_direct_response": "专家直接回答",
    "expert_completed": "专家本轮结束",
    "expert_decision_unavailable": "专家决策失败",
    "expert_agent_reply_discarded": "丢弃了框架失败文案",
    "expert_skill_unavailable_fallback": "目标 Skill 不可用，专家兜底",
    "native_questionnaire_response": "问卷回复已生成",
    "native_questionnaire_form_created": "提问表单已挂到消息",
    "native_questionnaire_answered": "已收下表单答案",
    "team_member_completed": "专家团成员本轮结束",
    "team_handoff_preempted_skill_execution": "转交卡片打断了本轮 Skill",
    "reply_progress_evaluated": "回复进度检查",
    "script_result_presented": "脚本结果已展示",
    "ms_agent_runtime": "Skill 运行时状态",
    "question_answer_reconciled": "从用户原话对上了已有答案",
    "conversation_memory_filtered": "这轮回复没有写入长期记忆",
}


def _orchestrator():
    global _ORCHESTRATOR, _ORCHESTRATOR_ERROR
    if _ORCHESTRATOR is not None:
        return _ORCHESTRATOR
    if _ORCHESTRATOR_ERROR:
        raise RuntimeError(_ORCHESTRATOR_ERROR)
    try:
        from hailiang_skills.core.registry import SkillRegistry
        from hailiang_skills.llm.client import LLMClient
        from hailiang_skills.llm.config import load_llm_config
        from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator
        from hailiang_skills.skills.admission import AdmissionSkill
        from hailiang_skills.skills.chat import ChatSkill
        from hailiang_skills.skills.convergence import ConvergenceSkill
        from hailiang_skills.skills.facts_extractor import FactsExtractorSkill
        from hailiang_skills.skills.logging_skill import LoggingSkill
        from hailiang_skills.skills.path_drilldown import PathDrillDownSkill
        from hailiang_skills.skills.planner import PlannerSkill
        from hailiang_skills.skills.router import RouterSkill
        from hailiang_skills.skills.school_intro import SchoolIntroSkill
        from hailiang_skills.skills.terminate_or_recommend import TerminateOrRecommendSkill

        llm_config = load_llm_config()
        llm_client = LLMClient(llm_config)
        registry = SkillRegistry()
        for skill in (
            RouterSkill(llm_client),
            FactsExtractorSkill(llm_client),
            PlannerSkill(llm_client),
            ChatSkill(llm_client),
            AdmissionSkill(llm_client),
            ConvergenceSkill(llm_client),
            PathDrillDownSkill(llm_client),
            SchoolIntroSkill(llm_client),
            TerminateOrRecommendSkill(llm_client),
            LoggingSkill(),
        ):
            registry.register(skill)
        _ORCHESTRATOR = MainPlannerOrchestrator(registry, llm_config)
        return _ORCHESTRATOR
    except Exception as exc:
        _ORCHESTRATOR_ERROR = f"{type(exc).__name__}: {exc}"
        raise


def _shrink(value: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"…（已截断，原长 {len(value)}）"
    if isinstance(value, list):
        items = [_shrink(item, limit) for item in value[:40]]
        if len(value) > 40:
            items.append(f"…还有 {len(value) - 40} 项")
        return items
    if isinstance(value, dict):
        return {str(key): _shrink(item, limit) for key, item in list(value.items())[:40]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _shrink(str(value), limit)


def _catalog() -> dict[str, Any]:
    orchestrator = _orchestrator()
    teams = []
    for team in sorted(orchestrator.expert_team_registry.definitions.values(), key=lambda item: item.team_id):
        teams.append({
            "team_id": team.team_id,
            "name": team.name,
            "coordinator_expert_id": team.coordinator_expert_id,
            "fallback_expert_id": team.fallback_expert_id,
            "members": [
                {"expert_id": member.expert_id, "mention_name": member.mention_name}
                for member in team.members
            ],
        })
    experts = []
    for definition in sorted(orchestrator.expert_registry.definitions.values(), key=lambda item: item.agent_id):
        experts.append({
            "expert_id": definition.agent_id,
            "name": definition.name,
            "skill_ids": list(definition.authorized_skill_ids),
        })
    from hailiang_skills.runtime_bridge.default_expert_team import default_expert_team_id

    return {
        "default_team_id": default_expert_team_id(),
        "teams": teams,
        "experts": experts,
    }


def _apply_entry(context, orchestrator, body: dict[str, Any]) -> None:
    from hailiang_skills.runtime_bridge.default_expert_team import initialize_default_expert_team

    mode = str(body.get("mode") or "team")
    meta = context.session_meta
    if not meta.get("expert_team_id") and not meta.get("expert_id"):
        initialize_default_expert_team(context, orchestrator, force=True)
    if mode == "legacy":
        for key in ("expert_team_id", "expert_id", "active_expert_id"):
            meta.pop(key, None)
        meta.pop("pending_team_handoff", None)
        return
    if mode == "expert":
        expert_id = str(body.get("expert_id") or "").strip()
        if orchestrator.expert_registry.get(expert_id) is None:
            raise ValueError(f"专家不存在: {expert_id}")
        meta.pop("expert_team_id", None)
        meta.pop("pending_team_handoff", None)
        meta["expert_id"] = expert_id
        meta["active_expert_id"] = expert_id
        return
    team_id = str(body.get("team_id") or "").strip()
    team = orchestrator.expert_team_registry.get(team_id)
    if team is None:
        raise ValueError(f"专家团不存在: {team_id}")
    expert_id = str(body.get("expert_id") or "").strip()
    if expert_id not in team.member_expert_ids:
        expert_id = team.coordinator_expert_id or team.fallback_expert_id or (team.members[0].expert_id if team.members else "")
    meta["expert_team_id"] = team.team_id
    meta["expert_id"] = expert_id
    meta["active_expert_id"] = expert_id


def _forms(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    forms = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        blocks = message.get("blocks") or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "fact_form":
                payload = block.get("payload") if isinstance(block.get("payload"), dict) else {}
                forms.append(_shrink(payload, 4000))
    return forms


def _detail(payload: dict[str, Any]) -> str:
    keys = (
        "expert_id", "skill_id", "active_skill", "question_ids", "form_id",
        "form_emitted", "form_source", "fallback_used", "reason", "status",
        "mode", "execution_mode", "error", "no_progress_reason",
    )
    parts = []
    for key in keys:
        if key not in payload or payload[key] in ("", None, [], {}):
            continue
        parts.append(f"{key}={payload[key]}")
    return "；".join(parts)


def _ask(body: dict[str, Any]) -> dict[str, Any]:
    from hailiang_skills.core.context import SessionContext

    message = str(body.get("message") or "").strip()
    if not message:
        raise ValueError("请先写一句要问的话")
    orchestrator = _orchestrator()
    session_id = str(body.get("session_id") or "").strip()
    if body.get("reset") or not session_id or session_id not in _SESSIONS:
        session_id = f"debug_{uuid4().hex[:10]}"
        _SESSIONS[session_id] = SessionContext(session_id=session_id, user_id="debug_console")
    context = _SESSIONS[session_id]
    _apply_entry(context, orchestrator, body)
    before_events = len(context.event_trace)
    before_messages = len(context.messages)
    started = perf_counter()
    result = orchestrator.handle_message(message, context)
    elapsed_ms = int((perf_counter() - started) * 1000)
    new_events = []
    for event in context.event_trace[before_events:]:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "unknown")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        new_events.append({
            "event_type": event_type,
            "label": EVENT_LABELS.get(event_type, event_type),
            "key": any(hint in event_type for hint in KEY_HINTS),
            "detail": _detail(payload),
            "payload": _shrink(payload),
            "timestamp": event.get("timestamp") or "",
        })
    expert_id = str(context.session_meta.get("active_expert_id") or context.session_meta.get("expert_id") or "")
    expert = orchestrator.expert_registry.get(expert_id)
    return {
        "ok": True,
        "session_id": session_id,
        "elapsed_ms": elapsed_ms,
        "reply": str(getattr(result, "assistant_message", "") or ""),
        "state_patch": _shrink(getattr(result, "state_patch", {}) or {}),
        "active_skill": str(context.interaction_state.get("active_skill") or ""),
        "expert_id": expert_id,
        "expert_name": expert.name if expert is not None else "",
        "team_id": str(context.session_meta.get("expert_team_id") or ""),
        "forms": _forms(context.messages[before_messages:]),
        "events": new_events,
        "event_count": len(new_events),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        line = "%s - %s\n" % (self.address_string(), fmt % args)
        try:
            with (ROOT / "server.log").open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            sys.stderr.write(line)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            page = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path == "/api/health":
            self._send(200, {"ok": True})
            return
        if path == "/api/catalog":
            try:
                with _LOCK:
                    self._send(200, {"ok": True, **_catalog()})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(exc), "trace": traceback.format_exc()})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": str(exc)})
            return
        if path == "/api/reset":
            session_id = str(body.get("session_id") or "")
            _SESSIONS.pop(session_id, None)
            self._send(200, {"ok": True})
            return
        if path != "/api/ask":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            with _LOCK:
                self._send(200, _ask(body))
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"逻辑调试台 http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
