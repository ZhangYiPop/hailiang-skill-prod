"""Formal-runtime executor behind the stable SSE v3 Harness projection.

The public v3 protocol deliberately remains a Harness-shaped, durable
snapshot.  This adapter changes only *who* executes the business turn:
``MainPlannerOrchestrator`` (and its AgentScope/MS-Agent children) rather
than ``HarnessRuntime``.  Keeping this boundary here prevents the product
endpoint from importing Workbench's candidate-session persistence.
"""
from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from hailiang_skills.harness.contracts import TERMINAL


class FormalV3TurnExecutor:
    """Run one persisted V3 turn through the formal conversation runtime."""

    def __init__(self, *, repository, orchestrator, harness_runtime) -> None:
        self.repository = repository
        self.orchestrator = orchestrator
        self.runtime = harness_runtime

    def start(self, run_id: str, *, message: str) -> None:
        thread = threading.Thread(
            target=self._run,
            kwargs={"run_id": run_id, "message": message},
            name=f"formal-v3-{run_id[-12:]}",
            daemon=True,
        )
        thread.start()

    def resume(self, run_id: str, *, message: str) -> None:
        """Resume only a paused formal V3 record.

        A V3 reconnect after ``done_timeout`` is also sent as ``resume`` by
        the product API.  A formal turn may still be running in that case, so
        starting another worker would execute the same user turn twice.
        """
        def apply(run, changes, _db):
            if run.get("execution_runtime") != "formal" or run.get("status") != "waiting_for_user":
                return False
            root = run["tasks"][run["root_task_id"]]
            root.update(status="running", error=None)
            run.update(status="running", outcome=None, phase="resuming", action="正在继续正式专家团分析")
            changes.append({"event": "formal_turn_resumed", "node_id": root["id"]})
            return True
        if self.runtime.store.mutate(run_id, apply):
            self.start(run_id, message=message)

    def answer(self, run_id: str, *, request_id: str, answers: dict[str, Any], text: str | None, skipped: bool) -> bool:
        """Translate the V3 card payload into the formal runtime's text resume.

        The formal runtime remains authoritative for its native form state; V3
        owns only the stable request ID and public card lifecycle.
        """
        message_box: dict[str, Any] = {}

        def apply(run, changes, _db):
            if run.get("execution_runtime") != "formal" or run.get("status") in TERMINAL:
                return False
            request = run.get("inputs", {}).get(request_id)
            if not isinstance(request, dict) or request.get("status") != "pending":
                return False
            values = dict(answers or {})
            handoff = request.get("formal_handoff") if isinstance(request.get("formal_handoff"), dict) else None
            if handoff:
                if skipped:
                    return False
                target = str(values.get("target_expert_id") or "").strip()
                candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
                candidate = next(
                    (item for item in candidates if isinstance(item, dict) and str(item.get("expert_id") or "") == target),
                    None,
                )
                if candidate is None:
                    return False
                message_box.update(
                    handoff=handoff,
                    target_expert_id=target,
                    mention_name=str(candidate.get("mention_name") or candidate.get("name") or target),
                    value=str(handoff.get("original_user_message") or "请根据当前上下文继续协助用户。"),
                )
            else:
                fields = request.get("fields") if isinstance(request.get("fields"), list) else []
                labels = {str(item.get("key") or ""): str(item.get("label") or item.get("key") or "")
                          for item in fields if isinstance(item, dict)}
                lines = []
                if skipped:
                    lines.append("用户跳过了本次补充信息。")
                elif text:
                    lines.append(str(text))
                for key, value in values.items():
                    if value in (None, "", []):
                        continue
                    rendered = "、".join(str(item) for item in value) if isinstance(value, list) else str(value)
                    lines.append(f"{labels.get(str(key), str(key))}: {rendered}")
                message_box["value"] = "补充信息：\n" + ("\n".join(lines) if lines else "用户未提供额外信息。")
            request.update(status="skipped" if skipped else "answered", answer={"answers": values, "text": text, "skipped": skipped})
            root = run["tasks"][run["root_task_id"]]
            root.update(status="running", error=None)
            run.update(status="running", outcome=None, phase="resuming", action="正在继续正式专家团分析")
            changes.append({"event": "formal_input_answered", "node_id": root["id"]})
            return True

        accepted = bool(self.runtime.store.mutate(run_id, apply))
        if accepted:
            if message_box.get("handoff"):
                context = self.repository.get(self.runtime.store.get(run_id)["session_id"])
                handoff = message_box["handoff"]
                context.session_meta.update(
                    active_expert_id=message_box["target_expert_id"],
                    expert_id=message_box["target_expert_id"],
                    expert_selection_source="v3_handoff_card",
                    team_member_switch={
                        "source": "team_handoff",
                        "team_id": str(handoff.get("team_id") or context.session_meta.get("expert_team_id") or ""),
                        "target_expert_id": message_box["target_expert_id"],
                        "mention_name": message_box["mention_name"],
                        "visible_user_message": f"@{message_box['mention_name']}",
                        "source_user_message": message_box["value"],
                        "coordinator_reason": str(handoff.get("reason") or ""),
                        "source_message_id": str(handoff.get("source_message_id") or ""),
                    },
                )
                context.session_meta.pop("pending_team_handoff", None)
                context.session_meta.pop("pending_team_handoff_intent", None)
                self.repository.save(context)
            self.start(run_id, message=message_box["value"])
        return accepted

    def _mutate(self, run_id: str, update) -> None:
        def apply(run, changes, _db):
            if run.get("status") in TERMINAL:
                return
            update(run, changes)
        self.runtime.store.mutate(run_id, apply)

    @staticmethod
    def _latest_assistant(context) -> dict[str, Any]:
        for item in reversed(getattr(context, "messages", []) or []):
            if isinstance(item, dict) and item.get("role") == "assistant":
                return deepcopy(item)
        return {}

    @staticmethod
    def _fact_form(assistant: dict[str, Any]) -> dict[str, Any]:
        for block in assistant.get("blocks") if isinstance(assistant.get("blocks"), list) else []:
            if not isinstance(block, dict) or block.get("type") != "fact_form":
                continue
            payload = block.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("fields"), list):
                return deepcopy(payload)
        return {}

    @staticmethod
    def _team_handoff(assistant: dict[str, Any]) -> dict[str, Any]:
        direct = assistant.get("team_handoff")
        metadata = assistant.get("metadata")
        nested = metadata.get("team_handoff") if isinstance(metadata, dict) else None
        handoff = direct if isinstance(direct, dict) else nested
        if not isinstance(handoff, dict) or not isinstance(handoff.get("candidates"), list):
            return {}
        return deepcopy(handoff)

    def _run(self, *, run_id: str, message: str) -> None:
        run = self.runtime.store.get(run_id)
        try:
            context = self.repository.get(run["session_id"])
        except KeyError:
            self._fail(run_id, "SESSION_NOT_FOUND", "会话不存在，无法恢复正式对话运行时。")
            return

        # Formal runtime already exposes these callbacks for normal streaming
        # and Workbench candidate testing.  The adapter persists compact V3
        # state changes; the public route continues emitting only full frames.
        def status(payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            label = str(payload.get("detail") or payload.get("label") or payload.get("action") or "正在处理")
            self._mutate(run_id, lambda current, changes: (
                current.update(phase=str(payload.get("phase") or current.get("phase") or "running"), action=label),
                changes.append({"event": "formal_status", "node_id": current["root_task_id"]}),
            ))

        def delta(_text: str) -> None:
            # V3 deliberately exposes complete snapshots rather than product
            # message.delta frames.  The formal runtime still streams to its
            # internal callbacks; final content is committed atomically below.
            return None

        context.session_meta["stream_final_reply"] = True
        context.session_meta["status_callback"] = status
        context.session_meta["reply_delta_callback"] = delta
        context.session_meta["formal_v3_run_id"] = run_id
        # The existing formal runtime reads this callback while streaming model
        # output.  It makes the public V3 stop action cooperative without
        # changing the externally visible Harness state machine.
        context.session_meta["stream_cancel_check"] = lambda: (
            (current := self.runtime.store.get(run_id)) is None or current.get("status") == "cancelled"
        )
        try:
            result = self.orchestrator.handle_message(message, context)
            assistant = self._latest_assistant(context)
            content = str(getattr(result, "assistant_message", "") or assistant.get("content") or "")
            # Session repositories persist session_meta as JSON.  Streaming
            # callbacks are request-scoped callables and must never leak into
            # that durable payload.
            for key in ("stream_final_reply", "status_callback", "reply_delta_callback", "formal_v3_run_id", "stream_cancel_check"):
                context.session_meta.pop(key, None)
            self.repository.save(context)
            self._complete(run_id, content, assistant)
        except Exception as exc:  # formal runtime already redacts public errors
            self._fail(run_id, "FORMAL_RUNTIME_FAILED", str(exc)[:500])
        finally:
            for key in ("stream_final_reply", "status_callback", "reply_delta_callback", "formal_v3_run_id", "stream_cancel_check"):
                context.session_meta.pop(key, None)

    def _complete(self, run_id: str, content: str, assistant: dict[str, Any]) -> None:
        def apply(run, changes):
            root = run["tasks"][run["root_task_id"]]
            form = self._fact_form(assistant)
            fields = form.get("fields") if isinstance(form.get("fields"), list) else []
            if fields:
                form_id = str(form.get("form_id") or "formal_input")
                request_id = f"formal_input_{form_id}"
                run.setdefault("inputs", {})[request_id] = {
                    "id": request_id, "title": str(form.get("title") or "补充关键信息"),
                    "fields": deepcopy(fields), "status": "pending", "version": 1, "round": 1,
                    "bindings": [], "owner_task_id": root["id"], "actor": "专家团队",
                }
                root.update(status="waiting_for_user", result=content)
                run.update(status="waiting_for_user", outcome=None, result=content,
                           phase="waiting_for_user", action="等待补充信息", formal_assistant=assistant)
                changes.append({"event": "formal_input_requested", "node_id": root["id"]})
                return
            handoff = self._team_handoff(assistant)
            candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
            if candidates:
                request_id = f"formal_handoff_{str(handoff.get('handoff_id') or 'select')[:64]}"
                options = [
                    {"label": str(item.get("mention_name") or item.get("name") or item.get("expert_id") or ""),
                     "value": str(item.get("expert_id") or "")}
                    for item in candidates if isinstance(item, dict) and item.get("expert_id")
                ]
                run.setdefault("inputs", {})[request_id] = {
                    "id": request_id, "title": "选择接管专家", "status": "pending", "version": 1, "round": 1,
                    "bindings": [], "owner_task_id": root["id"], "actor": "专家团队", "formal_handoff": handoff,
                    "fields": [{"key": "target_expert_id", "label": "请选择接管专家", "input_type": "single_select",
                                "required": True, "options": options}],
                }
                root.update(status="waiting_for_user", result=content)
                run.update(status="waiting_for_user", outcome=None, result=content, phase="waiting_for_user",
                           action="等待选择接管专家", formal_assistant=assistant)
                changes.append({"event": "formal_handoff_requested", "node_id": root["id"]})
                return
            root.update(status="completed", result=content, ended_at=run.get("updated_at"))
            run.update(status="completed", outcome="completed", result=content,
                       phase="completed", action="已完成正式专家团分析", formal_assistant=assistant)
            changes.append({"event": "formal_turn_completed", "node_id": root["id"]})
        self._mutate(run_id, apply)

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        def apply(run, changes):
            root = run["tasks"][run["root_task_id"]]
            root.update(status="failed", error=code, ended_at=run.get("updated_at"))
            run.update(status="failed", outcome="failed", error={"code": code, "message": "正式对话运行失败", "detail": detail},
                       phase="failed", action="正式专家团运行失败")
            changes.append({"event": "formal_turn_failed", "node_id": root["id"]})
        self._mutate(run_id, apply)
