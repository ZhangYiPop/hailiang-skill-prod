from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_skill_runtime_core import CoreTraceStep

from hailiang_skills.skill_runtime.models import ChatMessage


@dataclass(slots=True)
class MemoryTurnResult:
    context: dict[str, Any]
    step: CoreTraceStep


class ConversationMemoryStore:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        enabled: bool = True,
        active_window_messages: int = 8,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.enabled = enabled
        self.active_window_messages = min(20, max(1, active_window_messages))
        self._jobs_lock = threading.Lock()
        self._active_jobs: dict[tuple[str, str], str] = {}
        self._file_locks_lock = threading.Lock()
        self._file_locks: dict[tuple[str, str], threading.RLock] = {}

    def prepare_for_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        active_skill_id: str,
        skill_dir: Path | None,
        llm_client: Any | None = None,
        logger: Any | None = None,
        defer_update: bool = False,
    ) -> MemoryTurnResult:
        if not self.enabled:
            memory = self._default_memory(user_id, session_id, active_skill_id)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="skipped",
                    detail="conversation memory disabled by runtime config",
                    payload=self._memory_trace_payload(memory),
                ),
            )

        memory = self._load_memory(user_id, session_id, active_skill_id)
        memory["skill_id"] = active_skill_id
        memory["active_window_messages"] = self.active_window_messages
        facts_schema = self._runtime_contract_facts_schema(skill_dir)
        memory["runtime_contract_hash"] = facts_schema.get("hash")
        memory["runtime_contract_available"] = bool(facts_schema.get("available"))

        complete_count = self._complete_message_count(memory.get("messages") or [])
        processed_count = int(memory.get("summary_updated_through_message_index") or 0)
        summary_target_count = self._summary_target_message_count(complete_count)
        if summary_target_count <= processed_count:
            memory["memory_update_status"] = "idle"
            self._save_memory(user_id, session_id, memory)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="skipped",
                    detail="no completed previous turn needs memory update",
                    payload=self._memory_trace_payload(memory),
                ),
            )

        pending_messages = list(memory.get("messages") or [])[processed_count:summary_target_count]
        if len(pending_messages) < 2:
            memory["memory_update_status"] = "idle"
            self._save_memory(user_id, session_id, memory)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="skipped",
                    detail="previous turn is not complete yet",
                    payload=self._memory_trace_payload(memory),
                ),
            )

        if llm_client is None:
            detail = "memory update skipped because runtime LLM client is unavailable"
            memory["memory_update_status"] = "skipped"
            memory["last_error"] = detail
            memory["last_memory_updated_at"] = datetime.now(UTC).isoformat()
            self._save_memory(user_id, session_id, memory)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="warning",
                    detail=detail,
                    payload=self._memory_trace_payload(memory),
                ),
            )

        if defer_update:
            return self._schedule_memory_update(
                user_id=user_id,
                session_id=session_id,
                active_skill_id=active_skill_id,
                memory=memory,
                pending_messages=pending_messages,
                update_through_message_index=summary_target_count,
                facts_schema=facts_schema,
                llm_client=llm_client,
                logger=logger,
            )

        started = datetime.now(UTC)
        memory["memory_update_status"] = "running"
        memory["last_error"] = None
        self._save_memory(user_id, session_id, memory)
        try:
            result = self._call_memory_llm(
                llm_client=llm_client,
                previous_summary=str(memory.get("conversation_summary") or ""),
                previous_facts=memory.get("conversation_facts") or {},
                pending_messages=pending_messages,
                facts_schema=facts_schema,
                logger=logger,
            )
            memory["conversation_summary"] = self._trim_memory_summary(
                str(result.get("summary") or memory.get("conversation_summary") or "")
            )
            memory["conversation_facts"] = self._merge_memory_facts(
                memory.get("conversation_facts") or {"global": {}, "skill": {}, "stage": {}},
                result.get("facts") or {},
                facts_schema,
                summary_target_count,
            )
            memory["summary_updated_through_message_index"] = summary_target_count
            memory["facts_updated_through_message_index"] = summary_target_count
            memory["memory_update_status"] = "success"
            memory["last_error"] = None
            memory["last_memory_updated_at"] = datetime.now(UTC).isoformat()
            self._save_memory(user_id, session_id, memory)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="success",
                    detail="conversation memory updated from completed previous turns",
                    payload={
                        **self._memory_trace_payload(memory),
                        "pending_messages": len(pending_messages),
                        "duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
                    },
                ),
            )
        except Exception as exc:  # noqa: BLE001
            memory["memory_update_status"] = "error"
            memory["last_error"] = f"{type(exc).__name__}: {exc}"
            memory["last_memory_updated_at"] = datetime.now(UTC).isoformat()
            self._save_memory(user_id, session_id, memory)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="warning",
                    detail="conversation memory update failed; current turn uses previous memory",
                    payload=self._memory_trace_payload(memory),
                ),
            )

    def append_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        active_skill_id: str,
        user_message: str,
        assistant_message: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._default_memory(user_id, session_id, active_skill_id)
        with self._memory_lock(user_id, session_id):
            memory = self._load_memory(user_id, session_id, active_skill_id)
            messages = list(memory.get("messages") or [])
            pair = [
                {"role": "user", "content": user_message, "skill_id": active_skill_id},
                {"role": "assistant", "content": assistant_message, "skill_id": active_skill_id},
            ]
            if len(messages) < 2 or messages[-2:] != pair:
                messages.extend(pair)
            memory["messages"] = messages
            memory["skill_id"] = active_skill_id
            memory["active_window_messages"] = self.active_window_messages
            self._save_memory(user_id, session_id, memory)
            return memory

    def reset_session(self, *, user_id: str, session_id: str, active_skill_id: str = "general_chat") -> dict[str, Any]:
        """Discard persisted prompt memory while keeping session history elsewhere."""
        memory = self._default_memory(user_id, session_id, active_skill_id)
        if self.enabled:
            self._save_memory(user_id, session_id, memory)
        return memory

    def finalize_for_skill_exit(
        self,
        *,
        user_id: str,
        session_id: str,
        active_skill_id: str,
        skill_dir: Path | None,
        llm_client: Any | None = None,
        logger: Any | None = None,
    ) -> MemoryTurnResult:
        """Schedule exit-time compaction without delaying the Skill transition.

        Until the background work completes, raw turns remain available and
        retain their source Skill id. This makes an immediate re-entry safe
        while keeping unrelated Skill history out of role-message context.
        """
        if not self.enabled:
            memory = self._default_memory(user_id, session_id, active_skill_id)
            return MemoryTurnResult(
                context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_exit_finalize",
                    status="skipped",
                    detail="conversation memory disabled by runtime config",
                    payload=self._memory_trace_payload(memory),
                ),
            )

        with self._memory_lock(user_id, session_id):
            memory = self._load_memory(user_id, session_id, active_skill_id)
            messages = list(memory.get("messages") or [])
            complete_count = self._complete_message_count(messages)
            processed_count = min(
                complete_count,
                int(memory.get("summary_updated_through_message_index") or 0),
            )
            pending_messages = messages[processed_count:complete_count]
            facts_schema = self._runtime_contract_facts_schema(skill_dir)
            memory["skill_id"] = active_skill_id
            memory["runtime_contract_hash"] = facts_schema.get("hash")
            memory["runtime_contract_available"] = bool(facts_schema.get("available"))

            if not pending_messages:
                self._save_memory(user_id, session_id, memory)
                return MemoryTurnResult(
                    context=self._memory_context(memory),
                    step=CoreTraceStep(
                        name="memory_exit_finalize",
                        status="skipped",
                        detail="no completed Skill turn needs exit-time memory compaction",
                        payload=self._memory_trace_payload(memory),
                    ),
                )
            if llm_client is None:
                return self._exit_finalize_failure(
                    user_id=user_id,
                    session_id=session_id,
                    memory=memory,
                    detail="memory exit compaction skipped because runtime LLM client is unavailable",
                )
            result = self._schedule_memory_update(
                user_id=user_id,
                session_id=session_id,
                active_skill_id=active_skill_id,
                memory=memory,
                pending_messages=pending_messages,
                update_through_message_index=complete_count,
                facts_schema=facts_schema,
                llm_client=llm_client,
                logger=logger,
            )
            return MemoryTurnResult(
                context=result.context,
                step=CoreTraceStep(
                    name="memory_exit_finalize",
                    status=result.step.status,
                    detail="conversation memory compaction scheduled after Skill exit",
                    payload=result.step.payload,
                ),
            )

    def _exit_finalize_failure(
        self,
        *,
        user_id: str,
        session_id: str,
        memory: dict[str, Any],
        detail: str,
    ) -> MemoryTurnResult:
        """Persist recoverable raw context when exit-time compaction fails."""
        memory["memory_update_status"] = "warning"
        memory["last_error"] = detail
        memory["last_memory_updated_at"] = datetime.now(UTC).isoformat()
        self._save_memory(user_id, session_id, memory)
        return MemoryTurnResult(
            context=self._memory_context(memory),
            step=CoreTraceStep(
                name="memory_exit_finalize",
                status="warning",
                detail=detail,
                payload=self._memory_trace_payload(memory),
            ),
        )

    def _memory_path(self, user_id: str, session_id: str) -> Path:
        return self.runtime_dir / "conversation_memories" / _safe_id(user_id) / f"{_safe_id(session_id)}.json"

    def _default_memory(self, user_id: str, session_id: str, active_skill_id: str) -> dict[str, Any]:
        return {
            "conversation_id": session_id,
            "session_id": session_id,
            "user_id": user_id,
            "skill_id": active_skill_id,
            "active_window_messages": self.active_window_messages,
            "messages": [],
            "conversation_summary": "",
            "conversation_facts": {"global": {}, "skill": {}, "stage": {}},
            "summary_updated_through_message_index": 0,
            "facts_updated_through_message_index": 0,
            "memory_update_status": "idle",
            "memory_update_job_id": None,
            "last_memory_updated_at": None,
            "runtime_contract_hash": None,
            "runtime_contract_available": False,
            "last_error": None,
        }

    def _load_memory(self, user_id: str, session_id: str, active_skill_id: str) -> dict[str, Any]:
        path = self._memory_path(user_id, session_id)
        if not path.is_file():
            return self._default_memory(user_id, session_id, active_skill_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_memory(user_id, session_id, active_skill_id)
        return data if isinstance(data, dict) else self._default_memory(user_id, session_id, active_skill_id)

    def _save_memory(self, user_id: str, session_id: str, memory: dict[str, Any]) -> None:
        path = self._memory_path(user_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")

    def _call_memory_llm(
        self,
        *,
        llm_client: Any,
        previous_summary: str,
        previous_facts: dict[str, Any],
        pending_messages: list[dict[str, Any]],
        facts_schema: dict[str, Any],
        logger: Any | None,
    ) -> dict[str, Any]:
        prompt = (
            "你是海亮升学 Skill Runtime 的对话记忆整理器。请只输出 JSON 对象，不要 Markdown。\n"
            "任务：基于已有摘要、已有 facts 和新增的完整 user/assistant 对话，生成更新后的滚动摘要，"
            "并只抽取 runtime_contract facts schema 允许的事实。\n\n"
            "摘要要求：中文，最多 1200 字；只保留稳定背景、用户目标、已确认结论、未完成事项、重要限制。\n"
            "Facts 要求：只能写入 schema 中出现的 key；没有证据就不要填写；每个 fact 使用 "
            "{\"value\": ..., \"confidence\": 0-1, \"evidence\": \"...\"}。\n\n"
            "返回 JSON schema:\n"
            "{\"summary\":\"...\",\"facts\":{\"global\":{},\"skill\":{},\"stage\":{}}}\n\n"
            "# Existing summary\n"
            f"{previous_summary or '(none)'}\n\n"
            "# Existing facts\n"
            f"{json.dumps(previous_facts or {}, ensure_ascii=False, indent=2)}\n\n"
            "# Allowed facts schema\n"
            f"{json.dumps(facts_schema, ensure_ascii=False, indent=2)}\n\n"
            "# New complete conversation messages\n"
            f"{json.dumps(pending_messages, ensure_ascii=False, indent=2)}"
        )
        messages = [
            ChatMessage(role="system", content="Extract compact conversation memory as strict JSON."),
            ChatMessage(role="user", content=prompt),
        ]
        complete_kwargs: dict[str, Any] = {"logger": logger}
        try:
            if "request_purpose" in inspect.signature(llm_client.complete).parameters:
                complete_kwargs["request_purpose"] = "conversation_memory"
        except (TypeError, ValueError):
            pass
        content = llm_client.complete(messages, **complete_kwargs)
        parsed = _extract_json_object(content)
        if not parsed:
            raise ValueError("memory LLM returned non-JSON content")
        return {
            "summary": str(parsed.get("summary") or previous_summary or ""),
            "facts": parsed.get("facts") if isinstance(parsed.get("facts"), dict) else {},
        }

    def _schedule_memory_update(
        self,
        *,
        user_id: str,
        session_id: str,
        active_skill_id: str,
        memory: dict[str, Any],
        pending_messages: list[dict[str, Any]],
        update_through_message_index: int,
        facts_schema: dict[str, Any],
        llm_client: Any,
        logger: Any | None,
    ) -> MemoryTurnResult:
        job_key = (user_id, session_id)
        with self._jobs_lock:
            existing_job_id = self._active_jobs.get(job_key)
            if existing_job_id:
                memory["memory_update_status"] = "running"
                memory["memory_update_job_id"] = existing_job_id
                return MemoryTurnResult(
                    context=self._memory_context(memory),
                    step=CoreTraceStep(
                        name="memory_update",
                        status="success",
                        detail="conversation memory update already running in background",
                        payload=self._memory_trace_payload(memory),
                    ),
                )
            job_id = f"memory_{uuid4().hex[:12]}"
            self._active_jobs[job_key] = job_id

        memory["memory_update_status"] = "scheduled"
        memory["memory_update_job_id"] = job_id
        with self._memory_lock(user_id, session_id):
            self._save_memory(user_id, session_id, memory)

        def run_update() -> None:
            started = datetime.now(UTC)
            try:
                result = self._call_memory_llm(
                    llm_client=llm_client,
                    previous_summary=str(memory.get("conversation_summary") or ""),
                    previous_facts=memory.get("conversation_facts") or {},
                    pending_messages=pending_messages,
                    facts_schema=facts_schema,
                    logger=logger,
                )
                with self._memory_lock(user_id, session_id):
                    latest = self._load_memory(user_id, session_id, active_skill_id)
                    latest["conversation_summary"] = self._trim_memory_summary(
                        str(result.get("summary") or latest.get("conversation_summary") or "")
                    )
                    latest["conversation_facts"] = self._merge_memory_facts(
                        latest.get("conversation_facts") or {"global": {}, "skill": {}, "stage": {}},
                        result.get("facts") or {},
                        facts_schema,
                        update_through_message_index,
                    )
                    latest["summary_updated_through_message_index"] = max(
                        int(latest.get("summary_updated_through_message_index") or 0),
                        update_through_message_index,
                    )
                    latest["facts_updated_through_message_index"] = max(
                        int(latest.get("facts_updated_through_message_index") or 0),
                        update_through_message_index,
                    )
                    latest["memory_update_status"] = "success"
                    latest["memory_update_job_id"] = None
                    latest["last_error"] = None
                    latest["last_memory_updated_at"] = datetime.now(UTC).isoformat()
                    self._save_memory(user_id, session_id, latest)
                if logger:
                    logger.log(
                        "conversation_memory.background.completed",
                        request_purpose="conversation_memory",
                        job_id=job_id,
                        processed_messages=len(pending_messages),
                        duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                    )
            except Exception as exc:  # noqa: BLE001
                with self._memory_lock(user_id, session_id):
                    latest = self._load_memory(user_id, session_id, active_skill_id)
                    latest["memory_update_status"] = "error"
                    latest["memory_update_job_id"] = None
                    latest["last_error"] = f"{type(exc).__name__}: {exc}"
                    latest["last_memory_updated_at"] = datetime.now(UTC).isoformat()
                    self._save_memory(user_id, session_id, latest)
                if logger:
                    logger.log(
                        "conversation_memory.background.failed",
                        request_purpose="conversation_memory",
                        job_id=job_id,
                        error_type=type(exc).__name__,
                        duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                    )
            finally:
                with self._jobs_lock:
                    if self._active_jobs.get(job_key) == job_id:
                        self._active_jobs.pop(job_key, None)

        threading.Thread(
            target=run_update,
            name=f"conversation-memory-{job_id}",
            daemon=True,
        ).start()
        return MemoryTurnResult(
            context=self._memory_context(memory),
                step=CoreTraceStep(
                    name="memory_update",
                    status="success",
                    detail="conversation memory update scheduled in background",
                payload={**self._memory_trace_payload(memory), "deferred": True},
            ),
        )

    def _memory_lock(self, user_id: str, session_id: str) -> threading.RLock:
        key = (user_id, session_id)
        with self._file_locks_lock:
            return self._file_locks.setdefault(key, threading.RLock())

    def _runtime_contract_facts_schema(self, skill_dir: Path | None) -> dict[str, Any]:
        if not skill_dir:
            return {"available": False, "hash": None, "global": [], "skill": [], "stage": {}, "exports": {}}
        path = skill_dir / "runtime_contract.json"
        if not path.is_file():
            return {"available": False, "hash": None, "global": [], "skill": [], "stage": {}, "exports": {}}
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            contract = json.loads(raw)
        except json.JSONDecodeError:
            return {
                "available": False,
                "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "global": [],
                "skill": [],
                "stage": {},
                "exports": {},
                "error": "runtime_contract.json is not valid JSON",
            }
        facts = contract.get("facts") if isinstance(contract, dict) else {}
        facts = facts if isinstance(facts, dict) else {}
        stage = facts.get("stage") if isinstance(facts.get("stage"), dict) else {}
        return {
            "available": True,
            "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "global": [str(item) for item in facts.get("global", []) if str(item)],
            "skill": [str(item) for item in facts.get("skill", []) if str(item)],
            "stage": {
                str(stage_name): [str(item) for item in keys if str(item)]
                for stage_name, keys in stage.items()
                if isinstance(keys, list)
            },
            "exports": facts.get("exports") if isinstance(facts.get("exports"), dict) else {},
        }

    def _merge_memory_facts(
        self,
        current: dict[str, Any],
        incoming: dict[str, Any],
        facts_schema: dict[str, Any],
        source_index: int,
    ) -> dict[str, Any]:
        merged = {
            "global": dict(current.get("global") or {}),
            "skill": dict(current.get("skill") or {}),
            "stage": dict(current.get("stage") or {}),
        }
        for scope in ("global", "skill"):
            allowed = set(facts_schema.get(scope) or [])
            values = incoming.get(scope) if isinstance(incoming.get(scope), dict) else {}
            for key, value in values.items():
                if key not in allowed:
                    continue
                record = _normalize_fact_record(value, source_index)
                if record is not None:
                    merged[scope][key] = record
        allowed_stage = facts_schema.get("stage") if isinstance(facts_schema.get("stage"), dict) else {}
        incoming_stage = incoming.get("stage") if isinstance(incoming.get("stage"), dict) else {}
        for stage_name, values in incoming_stage.items():
            if not isinstance(values, dict):
                continue
            allowed_keys = set(allowed_stage.get(stage_name) or [])
            if not allowed_keys:
                continue
            stage_bucket = dict(merged["stage"].get(stage_name) or {})
            for key, value in values.items():
                if key not in allowed_keys:
                    continue
                record = _normalize_fact_record(value, source_index)
                if record is not None:
                    stage_bucket[key] = record
            if stage_bucket:
                merged["stage"][stage_name] = stage_bucket
        return merged

    def _memory_context(self, memory: dict[str, Any]) -> dict[str, Any]:
        facts = memory.get("conversation_facts") if isinstance(memory.get("conversation_facts"), dict) else {}
        messages = list(memory.get("messages") or [])
        complete_count = self._complete_message_count(messages)
        summarized_count = min(
            complete_count,
            int(memory.get("summary_updated_through_message_index") or 0),
        )
        recent_messages, reference_messages = _messages_for_prompt(
            messages[summarized_count:complete_count],
            source_skill_id=str(memory.get("skill_id") or ""),
        )
        questionnaire_evidence_messages = _questionnaire_evidence_messages(
            messages[:complete_count],
            source_skill_id=str(memory.get("skill_id") or ""),
        )
        return {
            "summary": str(memory.get("conversation_summary") or "").strip(),
            "facts": _facts_for_prompt(facts),
            # Same-Skill raw history may be supplied as normal role messages.
            # Other Skill history stays reference-only in the system prompt.
            "recent_messages": recent_messages,
            "reference_messages": reference_messages,
            # This compact user-only projection remains available after summary
            # compaction. It is consumed only by first-form answer reconciliation.
            "questionnaire_evidence_messages": questionnaire_evidence_messages,
            "status": self._memory_trace_payload(memory),
        }

    def _memory_trace_payload(self, memory: dict[str, Any]) -> dict[str, Any]:
        messages = memory.get("messages") or []
        complete_count = self._complete_message_count(messages)
        summarized_count = min(
            complete_count,
            int(memory.get("summary_updated_through_message_index") or 0),
        )
        return {
            "active_window_messages": int(memory.get("active_window_messages") or self.active_window_messages),
            "total_messages": len(messages),
            "complete_messages": complete_count,
            "summary_updated_through_message_index": int(memory.get("summary_updated_through_message_index") or 0),
            "facts_updated_through_message_index": int(memory.get("facts_updated_through_message_index") or 0),
            "summary_target_message_index": self._summary_target_message_count(complete_count),
            "raw_context_messages": max(0, complete_count - summarized_count),
            "questionnaire_evidence_messages": len(
                _questionnaire_evidence_messages(
                    messages[:complete_count],
                    source_skill_id=str(memory.get("skill_id") or ""),
                )
            ),
            "facts_count": _count_memory_facts(memory.get("conversation_facts") or {}),
            "memory_update_status": memory.get("memory_update_status") or "idle",
            "memory_update_job_id": memory.get("memory_update_job_id"),
            "last_memory_updated_at": memory.get("last_memory_updated_at"),
            "runtime_contract_hash": memory.get("runtime_contract_hash"),
            "runtime_contract_available": bool(memory.get("runtime_contract_available")),
            "last_error": memory.get("last_error"),
        }

    def _complete_message_count(self, messages: list[Any]) -> int:
        complete = 0
        expected = "user"
        for index, item in enumerate(messages):
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
            if expected == "user" and role == "user":
                expected = "assistant"
            elif expected == "assistant" and role == "assistant":
                complete = index + 1
                expected = "user"
            else:
                break
        return complete

    def _summary_target_message_count(self, complete_count: int) -> int:
        # Messages are persisted as complete user/assistant pairs. Keep the
        # configured active window verbatim and only summarize older pairs.
        target = max(0, int(complete_count) - self.active_window_messages)
        return target - (target % 2)

    def _trim_memory_summary(self, summary: str, limit: int = 1800) -> str:
        cleaned = summary.strip()
        return cleaned if len(cleaned) <= limit else cleaned[-limit:].lstrip()


def supplement_questionnaire_evidence(
    memory_context: dict[str, Any],
    session_messages: list[Any],
    *,
    limit: int = 8,
    max_chars: int = 1200,
) -> dict[str, Any]:
    """Merge persisted user turns into the first-form evidence projection.

    Session history is immediately available during an async memory update, so
    this prevents Skill entry from racing ahead of memory compaction.
    """
    context = dict(memory_context or {})
    existing = context.get("questionnaire_evidence_messages")
    candidates = list(existing) if isinstance(existing, list) else []
    for item in session_messages or []:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
        metadata = item.get("metadata") if isinstance(item, dict) and isinstance(item.get("metadata"), dict) else {}
        if role != "user" or metadata.get("hidden") or metadata.get("synthetic"):
            continue
        if metadata.get("message_type") == "skill_transition_command":
            continue
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
        cleaned = str(content or "").strip()[:max_chars]
        if not cleaned:
            continue
        candidates.append({
            "role": "user",
            "content": cleaned,
            "source_skill_id": str(metadata.get("active_skill_id") or "session_history"),
        })

    deduped: list[dict[str, str]] = []
    seen_contents: set[str] = set()
    for item in reversed(candidates):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content or content in seen_contents:
            continue
        seen_contents.add(content)
        deduped.append({
            "role": "user",
            "content": content[:max_chars],
            "source_skill_id": str(item.get("source_skill_id") or "session_history"),
        })
        if len(deduped) >= limit:
            break
    context["questionnaire_evidence_messages"] = list(reversed(deduped))
    return context


def _facts_for_prompt(facts: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"global": {}, "skill": {}, "stage": {}}
    for scope in ("global", "skill"):
        values = facts.get(scope) if isinstance(facts.get(scope), dict) else {}
        for key, record in values.items():
            compact[scope][key] = record.get("value") if isinstance(record, dict) else record
    stage = facts.get("stage") if isinstance(facts.get("stage"), dict) else {}
    for stage_name, values in stage.items():
        if isinstance(values, dict):
            compact["stage"][stage_name] = {
                key: record.get("value") if isinstance(record, dict) else record
                for key, record in values.items()
            }
    return compact


def _messages_for_prompt(
    messages: list[Any],
    *,
    source_skill_id: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    current: list[dict[str, str]] = []
    reference: list[dict[str, str]] = []
    for item in messages:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
        if role not in {"user", "assistant"}:
            continue
        source_skill = str(item.get("skill_id") or source_skill_id) if isinstance(item, dict) else source_skill_id
        target = current if source_skill == source_skill_id else reference
        record = {"role": str(role), "content": str(content or "")}
        if target is reference:
            record["source_skill_id"] = source_skill
        target.append(record)
    return current, reference


def _questionnaire_evidence_messages(
    messages: list[Any],
    *,
    source_skill_id: str,
    limit: int = 8,
    max_chars: int = 1200,
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for item in messages:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
        if role != "user":
            continue
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
        cleaned = str(content or "").strip()[:max_chars]
        if not cleaned:
            continue
        source_skill = str(item.get("skill_id") or source_skill_id) if isinstance(item, dict) else source_skill_id
        evidence.append({
            "role": "user",
            "content": cleaned,
            "source_skill_id": source_skill,
        })
    return evidence[-limit:]


def _normalize_fact_record(value: Any, source_index: int) -> dict[str, Any] | None:
    if isinstance(value, dict):
        fact_value = value.get("value")
        confidence = value.get("confidence", 0.7)
        evidence = str(value.get("evidence") or "")[:240]
    else:
        fact_value = value
        confidence = 0.6
        evidence = ""
    if fact_value in (None, "", []):
        return None
    try:
        confidence_float = float(confidence)
    except (TypeError, ValueError):
        confidence_float = 0.6
    return {
        "value": fact_value,
        "confidence": min(1.0, max(0.0, confidence_float)),
        "evidence": evidence,
        "source_message_index": source_index,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _count_memory_facts(facts: dict[str, Any]) -> int:
    count = 0
    for scope in ("global", "skill"):
        values = facts.get(scope) if isinstance(facts.get(scope), dict) else {}
        count += len([value for value in values.values() if value not in (None, "", [])])
    stage = facts.get("stage") if isinstance(facts.get("stage"), dict) else {}
    for values in stage.values():
        if isinstance(values, dict):
            count += len([value for value in values.values() if value not in (None, "", [])])
    return count


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"```json\s*", "", str(text or "")).strip()
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for index, char in enumerate(cleaned[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "local"
