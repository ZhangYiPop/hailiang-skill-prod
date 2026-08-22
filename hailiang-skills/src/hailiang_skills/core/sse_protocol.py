"""The single, client-facing SSE v2 state protocol.

The runtime still produces granular internal events.  This module is the one
place where those events are accumulated into a stable message presentation
snapshot.  Browser clients never need to understand runtime event names.
"""

from __future__ import annotations

from copy import deepcopy
import json
import time
from typing import Any

from hailiang_skills.core.logging import utc_now_iso
from hailiang_skills.core.status_labels import normalize_status_label


SSE_V2_PROTOCOL = "hailiang.sse.v2"
# Kept as an import-compatible name while callers are migrated.  There is no
# legacy wire protocol any more.
UNIFIED_PROTOCOL = SSE_V2_PROTOCOL
PROTOCOL_NAME = SSE_V2_PROTOCOL


def normalize_protocol(_value: str | None = None) -> str:
    """The chat-stream endpoint is intentionally a v2 hard cut-over."""
    return SSE_V2_PROTOCOL


def empty_risk_state(*, status: str = "idle", stage: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "stage": stage,
        "blocked": False,
        "message": "",
    }


def empty_error_state() -> dict[str, Any]:
    return {
        "code": "",
        "message": "",
        "upstream_detail": "",
        "retryable": False,
        "terminal": False,
    }


def empty_message_state(*, session_id: str, run_id: str) -> dict[str, Any]:
    """Return the fixed-shape state required in *every* v2 SSE frame."""
    return {
        "protocol": SSE_V2_PROTOCOL,
        "session_id": session_id,
        "run_id": run_id,
        "seq": 0,
        "ts": "",
        "elapsed_ms": 0,
        "message_id": None,
        "status": "streaming",
        "assistant": {"content": "", "status": "streaming"},
        "intent": {},
        "form": {},
        "path_options": {},
        "skill_rooms": [],
        "team_handoff": {},
        "expert": {"mode": "none", "team": {}, "active": {}, "transition": {}},
        "skill_transition": {},
        "session": {"active_skill": {}},
        "risk": empty_risk_state(),
        "error": empty_error_state(),
    }


def _as_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _form_from_blocks(blocks: object, *, interaction_states: object = None) -> dict[str, Any]:
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("type") != "fact_form":
            continue
        payload = _as_mapping(block.get("payload"))
        fields: list[dict[str, Any]] = []
        for raw_field in payload.get("fields") if isinstance(payload.get("fields"), list) else []:
            field = _as_mapping(raw_field)
            if not field.get("fact_key"):
                continue
            normalized_field = {
                "fact_key": str(field["fact_key"]),
                "label": str(field.get("label") or field["fact_key"]),
                "input_type": str(field.get("input_type") or "text"),
                "required": bool(field.get("required", True)),
                "placeholder": str(field.get("placeholder") or ""),
                "example": str(field.get("example") or ""),
                "options": field.get("options") if isinstance(field.get("options"), list) else [],
                "submit_mode": str(field.get("submit_mode") or "manual"),
                "scope": str(field.get("scope") or "profile"),
                "value_type": str(field.get("value_type") or "string"),
            }
            # Kept optional so older clients can keep consuming the fixed form
            # shape without knowing about multi-select limits.
            try:
                max_selections = int(field.get("max_selections"))
            except (TypeError, ValueError):
                max_selections = 0
            if max_selections > 0:
                normalized_field["max_selections"] = max_selections
            fields.append(normalized_field)
        if not fields:
            return {}
        form_id = str(payload.get("form_id") or "missing_facts_form")
        interactions = _as_mapping(interaction_states)
        interaction = _as_mapping(interactions.get(f"fact_form:{form_id}"))
        return {
            "form_id": form_id,
            "title": str(payload.get("title") or "补充关键信息"),
            "description": str(payload.get("description") or ""),
            "status": str(interaction.get("status") or "active"),
            "interaction_id": f"fact_form:{form_id}",
            "fields": fields,
        }
    return {}


def _path_options_from_blocks(
    blocks: object,
    *,
    interaction_states: object = None,
    message_id: str | None = None,
    latest: bool = True,
) -> dict[str, Any]:
    """Project the legacy path_actions block into the v2 presentation shape."""
    interactions = _as_mapping(interaction_states)
    interaction = _as_mapping(interactions.get("path_actions"))
    status = str(interaction.get("status") or "active")
    actions: list[dict[str, Any]] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("type") != "path_actions":
            continue
        payload = _as_mapping(block.get("payload"))
        raw_actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        for raw_action in raw_actions:
            action = _as_mapping(raw_action)
            path_name = str(action.get("path_name") or action.get("title") or "").strip()
            if not path_name:
                continue
            actions.append(
                {
                    "path_id": str(action.get("path_id") or ""),
                    "title": path_name,
                    "description": str(action.get("description") or ""),
                    "prompt": f"我想了解：{path_name} 路径",
                    "enabled": bool(latest and status == "active"),
                }
            )
        if actions:
            break
    if not actions:
        return {}
    return {
        "status": status,
        "interaction_id": "path_actions",
        "source_message_id": str(message_id or ""),
        "options": actions,
    }


def _skill_rooms_from_suggestions(
    suggestions: object,
    *,
    message_id: str | None = None,
    interaction_states: object = None,
    latest: bool = True,
) -> list[dict[str, Any]]:
    interactions = _as_mapping(interaction_states)
    route_state = _as_mapping(interactions.get("route_suggestions"))
    route_active = route_state.get("status", "active") == "active"
    selected_target = str(route_state.get("selected_target_skill_id") or "")
    rooms: list[dict[str, Any]] = []
    for suggestion in suggestions if isinstance(suggestions, list) else []:
        item = _as_mapping(suggestion)
        skill_id = str(item.get("target_skill_id") or item.get("skill_id") or "").strip()
        if not skill_id:
            continue
        entered = selected_target == skill_id
        rooms.append(
            {
                "skill_id": skill_id,
                "title": str(item.get("agent_label") or item.get("label") or skill_id),
                "brief": str(item.get("brief") or ""),
                "info": str(item.get("info") or ""),
                "description": str(item.get("description") or item.get("reason") or ""),
                "reason": str(item.get("reason") or ""),
                "status": "entered" if entered else "enterable",
                "enabled": bool(latest and route_active and not selected_target),
                "source_message_id": message_id or "",
                "source_interaction_id": "route_suggestions",
            }
        )
    return rooms


def _active_skill(payload: object) -> dict[str, Any]:
    source = _as_mapping(payload)
    skill = _as_mapping(source.get("skill"))
    skill_id = str(
        source.get("active_skill")
        or source.get("to_skill_id")
        or skill.get("skill_id")
        or ""
    ).strip()
    if not skill_id:
        return {}
    return {
        "skill_id": skill_id,
        "title": str(
            source.get("agent_label")
            or source.get("active_skill_label")
            or source.get("scene_name")
            or skill.get("label")
            or skill.get("name")
            or skill_id
        ),
        # skill_context uses the wire names skill_brief/skill_info while
        # skill_transition uses brief/info. Accept both forms so the fixed v2
        # session projection never loses Skill metadata.
        "brief": str(source.get("brief") or source.get("skill_brief") or skill.get("brief") or ""),
        "info": str(
            source.get("info")
            or source.get("skill_info")
            or skill.get("info")
            or skill.get("description")
            or ""
        ),
        "description": str(source.get("description") or skill.get("description") or source.get("skill_info") or ""),
        "scene_name": str(source.get("scene_name") or skill.get("scene_name") or ""),
    }


def _display_label(value: object) -> str:
    """Keep model/runtime diagnostics out of the user-visible reasoning label."""
    return normalize_status_label(value)


def presentation_from_message(message: dict[str, Any], *, latest: bool) -> dict[str, Any]:
    """Normalize persisted legacy messages into the v2 presentation model."""
    existing = _as_mapping(message.get("presentation"))
    if existing:
        presentation = deepcopy(existing)
        presentation["error"] = {
            **empty_error_state(),
            **_as_mapping(presentation.get("error")),
        }
        presentation["skill_rooms"] = [
            {**_as_mapping(room), "enabled": bool(latest and _as_mapping(room).get("enabled"))}
            for room in presentation.get("skill_rooms", [])
            if isinstance(room, dict)
        ]
        path_options = _as_mapping(presentation.get("path_options"))
        if path_options:
            path_options["status"] = str(path_options.get("status") or "active")
            path_options["options"] = [
                {
                    **_as_mapping(option),
                    "enabled": bool(latest and _as_mapping(option).get("enabled")),
                }
                for option in path_options.get("options", [])
                if isinstance(option, dict)
            ]
        else:
            metadata = _as_mapping(message.get("metadata"))
            blocks = message.get("blocks") if isinstance(message.get("blocks"), list) else metadata.get("blocks", [])
            interaction_states = message.get("interaction_states") or metadata.get("interaction_states") or {}
            path_options = _path_options_from_blocks(
                blocks,
                interaction_states=interaction_states,
                message_id=str(message.get("message_id") or ""),
                latest=latest,
            )
        presentation["path_options"] = path_options
        return presentation

    metadata = _as_mapping(message.get("metadata"))
    blocks = message.get("blocks") if isinstance(message.get("blocks"), list) else metadata.get("blocks", [])
    interaction_states = message.get("interaction_states") or metadata.get("interaction_states") or {}
    route_suggestions = message.get("route_suggestions") or metadata.get("route_suggestions") or []
    return {
        "assistant": {
            "content": str(message.get("content") or ""),
            "status": "stopped" if (message.get("generation_status") or metadata.get("generation_status")) == "cancelled" else "completed",
        },
        "intent": {},
        "form": _form_from_blocks(blocks, interaction_states=interaction_states),
        "path_options": _path_options_from_blocks(
            blocks,
            interaction_states=interaction_states,
            message_id=str(message.get("message_id") or ""),
            latest=latest,
        ),
        "skill_rooms": _skill_rooms_from_suggestions(
            route_suggestions,
            message_id=str(message.get("message_id") or ""),
            interaction_states=interaction_states,
            latest=latest,
        ),
        "skill_transition": _as_mapping(message.get("skill_transition") or metadata.get("skill_transition")),
        "session": {"active_skill": _active_skill({
            "active_skill": message.get("skill_id") or metadata.get("skill_id"),
            "active_skill_label": message.get("skill_name") or metadata.get("skill_name"),
            "scene_name": message.get("scene_name") or metadata.get("scene_name"),
        })},
        "risk": empty_risk_state(),
        "error": _as_mapping(existing.get("error")) or empty_error_state(),
    }


class SseEnvelopeBuilder:
    """Accumulate internal runtime events into one v2 ``event: state`` stream."""

    def __init__(self, *, run_id: str, session_id: str, protocol: str | None = None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.protocol = SSE_V2_PROTOCOL
        self.started = time.perf_counter()
        self.seq = 0
        self.state = empty_message_state(session_id=session_id, run_id=run_id)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Continue a control response (for example stop) from its last state."""
        restored = deepcopy(snapshot)
        if restored.get("protocol") != SSE_V2_PROTOCOL:
            return
        if restored.get("session_id") != self.session_id or restored.get("run_id") != self.run_id:
            return
        self.state = restored
        self.seq = int(restored.get("seq") or 0)

    def presentation(self) -> dict[str, Any]:
        return {
            key: deepcopy(self.state[key])
            for key in ("assistant", "intent", "form", "path_options", "skill_rooms", "team_handoff", "expert", "skill_transition", "session", "risk", "error")
        }

    def encode(self, event: str, data: dict[str, Any]) -> str | None:
        raw_sse, _ = self.encode_with_metadata(event, data)
        return raw_sse

    def encode_with_metadata(self, event: str, data: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        changed = self._apply(event, data)
        # A transition-only run may not alter the empty state until its next
        # event.  It still needs an initial fixed-shape frame.
        if self.seq == 0:
            changed = True
        if not changed:
            return None, {
                "wire_event": "state",
                "payload": self.snapshot(),
                "protocol": self.protocol,
                "seq": self.seq,
                "ts": self.state["ts"],
                "message_id": self.state["message_id"],
            }
        self.seq += 1
        self.state["seq"] = self.seq
        self.state["ts"] = utc_now_iso()
        self.state["elapsed_ms"] = int((time.perf_counter() - self.started) * 1000)
        payload = self.snapshot()
        raw_sse = f"event: state\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return raw_sse, {
            "wire_event": "state",
            "payload": payload,
            "protocol": self.protocol,
            "seq": self.seq,
            "ts": payload["ts"],
            "message_id": payload["message_id"],
        }

    def _set(self, key: str, value: Any) -> bool:
        if self.state.get(key) == value:
            return False
        self.state[key] = deepcopy(value)
        return True

    def _apply(self, event: str, data: dict[str, Any]) -> bool:
        changed = False
        if event == "run_started":
            changed |= self._set("status", "streaming")
            changed |= self._set("assistant", {"content": "", "status": "streaming"})
            risk_stage = str(data.get("risk_stage") or "")
            if risk_stage:
                changed |= self._set("risk", empty_risk_state(status="checking", stage=risk_stage))
            changed |= self._set("error", empty_error_state())
            return changed

        if event in {"synthetic_progress", "skill_status"}:
            stage = str(data.get("stage") or "thinking")
            label = _display_label(data.get("label"))
            if not label:
                return False
            intent = _as_mapping(self.state.get("intent"))
            # ``progress_completed`` is emitted immediately before the first
            # visible reply delta. Runtime/heartbeat status events can already
            # be queued at that point; they must not make a completed intent
            # timeline appear to start thinking again while the answer streams.
            if intent.get("status") == "completed":
                return False
            steps = [dict(item) for item in intent.get("steps", []) if isinstance(item, dict)]
            for item in steps:
                if item.get("status") == "active":
                    item["status"] = "completed"
            next_step = {
                "id": stage,
                "label": label,
                "status": "active",
                "detail": str(data.get("detail") or ""),
            }
            index = next((i for i, item in enumerate(steps) if item.get("id") == stage), -1)
            # Keep the first public label for a stable stage id. Later runtime
            # events can enrich its detail, but completion must not display a
            # different step name from the one shown at startup.
            if index >= 0:
                next_step["label"] = str(steps[index].get("label") or label)
            duplicate_index = next(
                (i for i, item in enumerate(steps) if item.get("label") == next_step["label"]),
                -1,
            )
            if duplicate_index >= 0 and duplicate_index != index:
                return False
            if index >= 0:
                steps[index] = next_step
            else:
                steps.append(next_step)
            changed |= self._set("intent", {"status": "streaming", "steps": steps})
            return changed

        if event == "progress_completed":
            intent = _as_mapping(self.state.get("intent"))
            if not intent:
                return False
            steps = [
                {**item, "status": "completed" if item.get("status") == "active" else item.get("status", "completed")}
                for item in intent.get("steps", [])
                if isinstance(item, dict)
            ]
            return self._set("intent", {"status": "completed", "steps": steps})

        if event == "final_text_delta":
            assistant = _as_mapping(self.state.get("assistant"))
            return self._set(
                "assistant",
                {"content": str(assistant.get("content") or "") + str(data.get("delta") or ""), "status": "streaming"},
            )

        if event == "main_content_end":
            assistant = _as_mapping(self.state.get("assistant"))
            changed |= self._set(
                "assistant",
                {"content": str(data.get("assistant_message") or assistant.get("content") or ""), "status": "streaming"},
            )
            if data.get("message_id"):
                changed |= self._set("message_id", str(data["message_id"]))
            return changed

        if event in {"skill_context", "skill_intro"}:
            active = _active_skill(data)
            if active:
                changed |= self._set("session", {"active_skill": active})
            return changed

        if event == "team_handoff":
            return self._set("team_handoff", _as_mapping(data))

        if event == "expert_context":
            return self._set("expert", {
                "mode": str(data.get("mode") or "none"),
                "team": _as_mapping(data.get("team")),
                "active": _as_mapping(data.get("active")),
                "transition": _as_mapping(data.get("transition")),
            })

        if event == "skill_transition":
            transition = {
                "action": str(data.get("action") or ""),
                "from_skill_id": str(data.get("from_skill_id") or ""),
                "to_skill_id": str(data.get("to_skill_id") or "general_chat"),
                "source": str(data.get("source") or ""),
            }
            if isinstance(data.get("skill"), dict):
                transition["skill"] = deepcopy(data["skill"])
            changed |= self._set("skill_transition", transition)
            changed |= self._set("session", {"active_skill": _active_skill(data) or {
                "skill_id": transition["to_skill_id"], "title": transition["to_skill_id"], "description": "", "scene_name": ""
            }})
            if transition["action"] == "exit":
                changed |= self._set(
                    "assistant",
                    {"content": "已为你退出 AI 咨询室，如有问题可以继续提问。", "status": "streaming"},
                )
            return changed

        if event == "skill_action":
            blocks = data.get("message_blocks") if isinstance(data.get("message_blocks"), list) else []
            changed |= self._set("form", _form_from_blocks(blocks))
            changed |= self._set(
                "path_options",
                _path_options_from_blocks(
                    blocks,
                    message_id=self.state.get("message_id"),
                ),
            )
            changed |= self._set(
                "skill_rooms",
                _skill_rooms_from_suggestions(data.get("route_suggestions"), message_id=self.state.get("message_id")),
            )
            if "team_handoff" in data:
                changed |= self._set("team_handoff", _as_mapping(data.get("team_handoff")))
            active = _active_skill(data)
            if active:
                changed |= self._set("session", {"active_skill": active})
            return changed

        if event == "final_message":
            if data.get("message_id"):
                changed |= self._set("message_id", str(data["message_id"]))
            assistant = _as_mapping(self.state.get("assistant"))
            changed |= self._set(
                "assistant",
                {"content": str(data.get("assistant_message") or assistant.get("content") or ""), "status": "streaming"},
            )
            blocks = data.get("message_blocks") if isinstance(data.get("message_blocks"), list) else []
            changed |= self._set(
                "path_options",
                _path_options_from_blocks(
                    blocks,
                    message_id=data.get("message_id") or self.state.get("message_id"),
                ),
            )
            if "team_handoff" in data:
                changed |= self._set("team_handoff", _as_mapping(data.get("team_handoff")))
            return changed

        if event == "security":
            status = str(data.get("status") or "idle")
            stage = str(data.get("stage") or "")
            risk = {
                "status": status,
                "stage": stage,
                "blocked": status == "blocked" or bool(data.get("blocked")),
                "message": str(data.get("message") or ""),
            }
            return self._set("risk", risk)

        if event == "moderation_blocked":
            changed |= self._set("status", "blocked")
            changed |= self._set("assistant", {"content": "", "status": "blocked"})
            changed |= self._set("form", {})
            changed |= self._set("path_options", {})
            changed |= self._set("skill_rooms", [])
            changed |= self._set("team_handoff", {})
            changed |= self._set("risk", {
                "status": "blocked",
                "stage": str(data.get("stage") or ""),
                "blocked": True,
                "message": "该内容当前无法继续处理，请调整后重新输入。",
            })
            return changed

        if event == "run_cancelled":
            assistant = _as_mapping(self.state.get("assistant"))
            changed |= self._set("status", "stopped")
            changed |= self._set("assistant", {"content": str(assistant.get("content") or ""), "status": "stopped"})
            return changed

        if event == "run_superseded":
            changed |= self._set("status", "superseded")
            changed |= self._set("assistant", {"content": "", "status": "superseded"})
            changed |= self._set("form", {})
            changed |= self._set("path_options", {})
            changed |= self._set("skill_rooms", [])
            changed |= self._set("team_handoff", {})
            return changed

        if event in {"model_error", "run_failed"}:
            error = {
                **empty_error_state(),
                **_as_mapping(data.get("error")),
            }
            if not error["message"]:
                error["message"] = str(data.get("message") or "模型运行异常，请稍后重试。")
            if event == "run_failed":
                error["terminal"] = True
            changed |= self._set("error", error)
            if not bool(error.get("terminal")):
                return changed
            changed |= self._set("status", "failed")
            assistant = _as_mapping(self.state.get("assistant"))
            changed |= self._set("assistant", {"content": str(assistant.get("content") or ""), "status": "failed"})
            return changed

        if event == "run_completed":
            raw_status = str(data.get("status") or "completed")
            status = {"cancelled": "stopped"}.get(raw_status, raw_status)
            if status not in {"completed", "stopped", "superseded", "blocked", "failed"}:
                status = "completed"
            changed |= self._set("status", status)
            assistant = _as_mapping(self.state.get("assistant"))
            changed |= self._set("assistant", {"content": str(assistant.get("content") or ""), "status": status})
            if isinstance(data.get("error"), dict):
                changed |= self._set("error", {**empty_error_state(), **_as_mapping(data.get("error"))})
            if data.get("message_id"):
                changed |= self._set("message_id", str(data["message_id"]))
            return changed

        # Blocks, facts and lifecycle diagnostics are persisted server-side but
        # intentionally have no independent wire representation in v2.
        return False
