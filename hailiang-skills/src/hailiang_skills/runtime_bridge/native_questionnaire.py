"""Native Skill questions adapted to the stable ``fact_form`` public contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from hailiang_skills.core.fact_service import FactService
from hailiang_skills.runtime_bridge.native_path_options import MULTI_PATH_SKILL_ID, path_catalog
from hailiang_skills.skills.common import extract_explicit_exam_province


_PROVINCES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)
_SCOPE_BY_TARGET = {
    "profile_fact": "profile",
    "user_fact": "shared",
    "shared_fact": "shared",
    "session_fact": "session",
}
_DEFAULT_MAX_FIELDS_PER_FORM = 6
_DEFAULT_RECONCILIATION_CONFIDENCE = 0.85
_DEFAULT_RECONCILIATION_MESSAGES = 8
_DEFAULT_RECONCILIATION_MESSAGE_CHARS = 1200
_DEFAULT_FACT_BINDINGS = {
    "当前年级": ("grade",),
    "高考省份": ("student_province", "province"),
    "预估高考总分": ("score_total", "score_recent_avg"),
    "外语科目": ("foreign_language",),
    "英语水平": ("english_exam_score",),
}


def questionnaire_config(bundle: Any) -> dict[str, Any]:
    value = getattr(bundle, "metadata", {}).get("questionnaire", {})
    return dict(value) if isinstance(value, dict) else {}


def questionnaire_enabled(bundle: Any) -> bool:
    return bool(questionnaire_config(bundle).get("enabled", False))


def build_questionnaire_protocol(bundle: Any, state: Any) -> str:
    if not questionnaire_enabled(bundle):
        return ""
    specs = available_question_specs(bundle, state)
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    if not specs and skill_id != MULTI_PATH_SKILL_ID:
        return ""
    answers = _answers(state, skill_id)
    catalog = [
        {
            "question_id": item["question_id"],
            "label": item["label"],
            "input_type": item["input_type"],
            "options": [option["value"] for option in item["options"]],
            "max_selections": item.get("max_selections"),
            "display_condition": item.get("display_condition", ""),
            "rule": item.get("rule", ""),
        }
        for item in specs
    ]
    protocol = (
        "# Native Questionnaire Protocol\n"
        "Always return JSON only, including when no follow-up is needed: "
        '{"assistant_message":"...","state_patch":{"mode":"...","stage":"..."},'
        '"question_ids":["catalog item"]}. '
        "Use only catalog question_id values and return at most "
        f"{_max_fields_per_form(bundle)} question_ids. Never invent a question type, option, or condition. "
        "Use an empty question_ids array when no follow-up is needed. Do not write follow-up questions "
        "as Markdown or plain text; the runtime renders all selected questions as a form. "
        "For multi_path_planning, when the response is a matching/output conclusion, also put the "
        "canonical path_id values from path_catalog into state_patch.matched_paths; use [] when no "
        "path option should be shown. The runtime, not the model, builds UI cards from those IDs.\n"
        f"session_private_answers={json.dumps(answers, ensure_ascii=False)}\n"
        f"question_catalog={json.dumps(catalog, ensure_ascii=False)}\n"
    )
    if skill_id == MULTI_PATH_SKILL_ID:
        path_items = [
            {"path_id": item.get("path_id"), "path_name": item.get("primary_category")}
            for item in path_catalog()
        ]
        protocol += f"path_catalog={json.dumps(path_items, ensure_ascii=False)}\n"
    return protocol


def question_specs(bundle: Any) -> list[dict[str, Any]]:
    config = questionnaire_config(bundle)
    table_path = str(config.get("rule_table") or "").strip()
    if table_path:
        path = Path(bundle.root_dir) / table_path
        if path.is_file():
            return _parse_rule_table(path.read_text(encoding="utf-8", errors="replace"))
    fields = config.get("fields", [])
    if not isinstance(fields, list):
        return []
    specs = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        spec = _normalize_spec(field)
        if spec:
            specs.append(spec)
    return specs


def available_question_specs(bundle: Any, state: Any) -> list[dict[str, Any]]:
    """Return only the rule-table questions and options valid for this turn."""
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    answers = _answers(state, skill_id)
    tier = _derive_tier(bundle, answers)
    return [
        _materialize_spec(spec, answers, tier)
        for spec in question_specs(bundle)
        if _question_visible(spec, answers, tier)
    ]


def questionnaire_continuation_context(bundle: Any, state: Any) -> dict[str, Any] | None:
    """Return the compact, server-authorized catalog for a collection turn."""
    if not questionnaire_enabled(bundle):
        return None
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    answers = _answers(state, skill_id)
    unanswered = [
        item for item in available_question_specs(bundle, state)
        if str(item["question_id"]) not in answers
    ]
    if not unanswered:
        return None
    skill_state = getattr(state, "skill_facts", {}).get(skill_id, {})
    return {
        "skill_id": skill_id,
        "answers": dict(answers),
        "tier": str(skill_state.get("tier") or _derive_tier(bundle, answers)),
        "max_fields_per_form": _max_fields_per_form(bundle),
        "question_catalog": [
            {
                "question_id": item["question_id"],
                "label": item["label"],
                "input_type": item["input_type"],
                "options": [option["value"] for option in item.get("options", [])],
                "max_selections": item.get("max_selections"),
                "display_condition": item.get("display_condition", ""),
                "rule": item.get("rule", ""),
            }
            for item in unanswered
        ],
        "answer_reconciliation": _answer_reconciliation_context(bundle, state, answers),
    }


def _answer_reconciliation_context(
    bundle: Any,
    state: Any,
    answers: dict[str, Any],
) -> dict[str, Any]:
    """Build one compact, auditable evidence set before the first form."""
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    skill_state = getattr(state, "skill_facts", {}).get(skill_id, {})
    config = questionnaire_config(bundle).get("answer_reuse", {})
    config = config if isinstance(config, dict) else {}
    enabled = bool(config.get("enabled", True))
    if answers or skill_state.get("_questionnaire_reconciliation_completed") or not enabled:
        return {"enabled": False, "fact_sources": [], "message_sources": []}

    bindings = _fact_bindings(config)
    fact_sources: list[dict[str, Any]] = []
    seen_fact_sources: set[tuple[str, str]] = set()
    used_source_ids: set[str] = set()

    def add_fact_source(prefix: str, fact_key: str, value: Any) -> None:
        eligible_ids = [question_id for question_id, keys in bindings.items() if fact_key in keys]
        if not eligible_ids or value in (None, "", [], {}):
            return
        dedupe_key = (fact_key, _stable_value(value))
        if dedupe_key in seen_fact_sources:
            return
        seen_fact_sources.add(dedupe_key)
        base_source_id = f"{prefix}:{fact_key}"
        source_id = base_source_id
        suffix = 2
        while source_id in used_source_ids:
            source_id = f"{base_source_id}:{suffix}"
            suffix += 1
        used_source_ids.add(source_id)
        fact_sources.append({
            "source_id": source_id,
            "source_type": "fact",
            "fact_key": fact_key,
            "value": value,
            "eligible_question_ids": eligible_ids,
        })

    for fact_key, value in dict(getattr(state, "global_facts", {}) or {}).items():
        add_fact_source("runtime_fact", str(fact_key), value)

    memory = getattr(state, "conversation_memory", {})
    memory = memory if isinstance(memory, dict) else {}
    memory_facts = memory.get("facts") if isinstance(memory.get("facts"), dict) else {}
    for fact_key, value in _flatten_memory_facts(memory_facts):
        add_fact_source("memory_fact", fact_key, value)

    raw_messages: list[dict[str, Any]] = []
    for key in ("questionnaire_evidence_messages", "reference_messages", "recent_messages"):
        values = memory.get(key) if isinstance(memory.get(key), list) else []
        raw_messages.extend(item for item in values if isinstance(item, dict))
    raw_messages.extend(
        {"role": item.role, "content": item.content}
        for item in getattr(state, "messages", [])
        if getattr(item, "role", "") == "user"
    )
    message_limit = _positive_int(config.get("max_recent_messages")) or _DEFAULT_RECONCILIATION_MESSAGES
    message_chars = _positive_int(config.get("max_message_chars")) or _DEFAULT_RECONCILIATION_MESSAGE_CHARS
    user_contents: list[str] = []
    for item in raw_messages:
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()[:message_chars]
        if content and content not in user_contents:
            user_contents.append(content)
    user_contents = user_contents[-message_limit:]
    message_sources = [
        {
            "source_id": f"user_message:{index + 1}",
            "source_type": "user_message",
            "content": content,
        }
        for index, content in enumerate(user_contents)
    ]
    return {
        "enabled": True,
        "min_confidence": _confidence(config.get("min_confidence")),
        "fact_sources": fact_sources,
        "message_sources": message_sources,
    }


def _fact_bindings(config: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    bindings = {key: tuple(values) for key, values in _DEFAULT_FACT_BINDINGS.items()}
    configured = config.get("fact_bindings")
    if not isinstance(configured, dict):
        return bindings
    for question_id, raw_keys in configured.items():
        values = raw_keys if isinstance(raw_keys, list) else [raw_keys]
        keys = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        if keys:
            bindings[str(question_id).strip()] = keys
    return bindings


def _flatten_memory_facts(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    if not isinstance(value, dict):
        return [(path[-1], value)] if path else []
    if "value" in value and value.get("value") not in (None, "", [], {}):
        return [(path[-1], value.get("value"))] if path else []
    flattened: list[tuple[str, Any]] = []
    for key, child in value.items():
        flattened.extend(_flatten_memory_facts(child, (*path, str(key))))
    return flattened


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = _DEFAULT_RECONCILIATION_CONFIDENCE
    return min(1.0, max(0.0, parsed))


def _stable_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def resolve_questionnaire_continuation(
    bundle: Any,
    state: Any,
    reply: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Validate a lightweight model decision against the current question catalog."""
    continuation = questionnaire_continuation_context(bundle, state)
    payload = _json_object(reply)
    if continuation is None:
        text = str(payload.get("assistant_message") or "") if isinstance(payload, dict) else ""
        return text.strip(), None, {"valid": True, "collection_complete": True, "fallback_used": False}

    reconciliation = continuation.get("answer_reconciliation", {})
    resolved = _apply_reconciled_answers(bundle, state, payload, reconciliation)
    refreshed = questionnaire_continuation_context(bundle, state)
    if refreshed is None:
        text = str(payload.get("assistant_message") or "").strip() if isinstance(payload, dict) else ""
        return text, None, {
            "valid": True,
            "collection_complete": True,
            "fallback_used": False,
            "resolved_answers": resolved,
            "rejected_resolved_answers": _rejected_resolved_answers(payload, resolved),
            "requested_question_ids": [],
            "selected_question_ids": [],
        }

    catalog_ids = [str(item["question_id"]) for item in refreshed["question_catalog"]]
    allowed_ids = set(catalog_ids)
    requested_ids = _requested_question_ids(payload) if isinstance(payload, dict) else None
    requested_ids = requested_ids if isinstance(requested_ids, list) else []
    collection_complete = bool(payload.get("collection_complete")) if isinstance(payload, dict) else False
    selected_ids = [item for item in requested_ids if item in allowed_ids]
    valid = bool(
        isinstance(payload, dict)
        and isinstance(payload.get("assistant_message"), str)
        and not collection_complete
        and selected_ids
        and len(selected_ids) == len(requested_ids)
    )
    fallback_used = not valid
    if not valid:
        selected_ids = catalog_ids[: int(refreshed["max_fields_per_form"])]
    else:
        selected_ids = selected_ids[: int(refreshed["max_fields_per_form"])]

    specs_by_id = {
        str(item["question_id"]): item
        for item in available_question_specs(bundle, state)
    }
    selected = [specs_by_id[item] for item in selected_ids if item in specs_by_id]
    skill_id = str(refreshed["skill_id"])
    # The prose is streamed before question IDs finish arriving. An invalid
    # selection therefore falls back only at the form layer; keep the same
    # already-visible, sanitized prose when it is present.
    text = (
        str(payload.get("assistant_message") or "").strip()
        if isinstance(payload, dict) and isinstance(payload.get("assistant_message"), str)
        else "请通过下面的表单继续补充关键信息。"
    )
    return text or "请通过下面的表单继续补充关键信息。", _form_block(skill_id, selected), {
        "valid": valid,
        "collection_complete": False,
        "fallback_used": fallback_used,
        "requested_question_ids": requested_ids,
        "selected_question_ids": selected_ids,
        "resolved_answers": resolved,
        "rejected_resolved_answers": _rejected_resolved_answers(payload, resolved),
    }


def _apply_reconciled_answers(
    bundle: Any,
    state: Any,
    payload: dict[str, Any] | None,
    reconciliation: Any,
) -> list[dict[str, Any]]:
    if not isinstance(reconciliation, dict) or not reconciliation.get("enabled"):
        return []
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    skill_state = state.skill_facts.setdefault(skill_id, {})
    # Mark the one-shot operation complete only after its evidence projection
    # was actually available. A transient memory-load miss can retry next turn.
    if reconciliation.get("fact_sources") or reconciliation.get("message_sources"):
        skill_state["_questionnaire_reconciliation_completed"] = True
    requested = payload.get("resolved_answers") if isinstance(payload, dict) else None
    if not isinstance(requested, list):
        return []

    sources = {
        str(item.get("source_id") or ""): item
        for group in ("fact_sources", "message_sources")
        for item in reconciliation.get(group, [])
        if isinstance(item, dict) and str(item.get("source_id") or "")
    }
    specs_by_id = {
        str(item["question_id"]): item
        for item in available_question_specs(bundle, state)
    }
    min_confidence = _confidence(reconciliation.get("min_confidence"))
    accepted: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    answers = skill_state.setdefault("answers", {})
    provenance = skill_state.setdefault("_reused_answer_sources", {})

    for candidate in requested:
        if not isinstance(candidate, dict):
            continue
        question_id = str(candidate.get("question_id") or "").strip()
        source_id = str(candidate.get("source_id") or "").strip()
        spec = specs_by_id.get(question_id)
        source = sources.get(source_id)
        if not spec or not source or question_id in accepted_ids or question_id in answers:
            continue
        if _confidence(candidate.get("confidence")) < min_confidence:
            continue
        value = _normalize_reconciled_value(candidate.get("value"), spec)
        if value is None or not _source_supports_answer(source, candidate, spec, value):
            continue
        answers[question_id] = value
        provenance[question_id] = {
            "source_id": source_id,
            "source_type": source.get("source_type"),
            "confidence": _confidence(candidate.get("confidence")),
        }
        accepted_ids.add(question_id)
        accepted.append({
            "question_id": question_id,
            "value": value,
            "source_id": source_id,
        })

    if accepted:
        skill_state["collected"] = dict(answers)
        derived_tier = _derive_tier(bundle, answers)
        if derived_tier:
            skill_state["tier"] = derived_tier
    return accepted


def _normalize_reconciled_value(value: Any, spec: dict[str, Any]) -> Any | None:
    if isinstance(value, list):
        raw = "、".join(str(item).strip() for item in value if str(item).strip())
    else:
        raw = str(value if value is not None else "").strip()
    return _normalize_answer(raw, spec) if raw else None


def _source_supports_answer(
    source: dict[str, Any],
    candidate: dict[str, Any],
    spec: dict[str, Any],
    value: Any,
) -> bool:
    question_id = str(spec.get("question_id") or "")
    if source.get("source_type") == "fact":
        eligible = {str(item) for item in source.get("eligible_question_ids", [])}
        source_value = _normalize_reconciled_value(source.get("value"), spec)
        return question_id in eligible and source_value is not None and source_value == value

    if source.get("source_type") != "user_message":
        return False
    content = str(source.get("content") or "")
    evidence = str(candidate.get("evidence") or "").strip()
    if not evidence or len(evidence) > 240 or evidence not in content:
        return False
    values = value if isinstance(value, list) else [value]
    normalized_values = [str(item).strip() for item in values if str(item).strip()]
    if not normalized_values or any(item not in evidence for item in normalized_values):
        return False
    if question_id == "高考省份" and not extract_explicit_exam_province(content, normalized_values):
        return False
    if any(len(item) <= 1 for item in normalized_values):
        labels = {question_id, str(spec.get("label") or "").strip()}
        if not any(label and label in evidence for label in labels):
            return False
    return True


def _rejected_resolved_answers(
    payload: dict[str, Any] | None,
    accepted: list[dict[str, Any]],
) -> list[str]:
    requested = payload.get("resolved_answers") if isinstance(payload, dict) else None
    if not isinstance(requested, list):
        return []
    accepted_ids = {str(item.get("question_id") or "") for item in accepted}
    return list(dict.fromkeys(
        str(item.get("question_id") or "").strip()
        for item in requested
        if isinstance(item, dict)
        and str(item.get("question_id") or "").strip()
        and str(item.get("question_id") or "").strip() not in accepted_ids
    ))


def decode_questionnaire_reply(bundle: Any, state: Any, reply: str) -> tuple[str, dict[str, Any] | None]:
    payload = _json_object(reply)
    if not isinstance(payload, dict):
        return reply, None
    _apply_state_patch(state, bundle, payload.get("state_patch"))
    text = str(payload.get("assistant_message") or "").strip() or "请补充下面这项信息。"
    requested_ids = _requested_question_ids(payload)
    if requested_ids is None or not requested_ids:
        return text, None
    specs = available_question_specs(bundle, state)
    specs_by_id = {str(item["question_id"]): item for item in specs}
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    answers = _answers(state, skill_id)
    selected: list[dict[str, Any]] = []
    for question_id in requested_ids:
        spec = specs_by_id.get(question_id)
        if spec is None or question_id in answers or spec in selected:
            continue
        selected.append(spec)
        if len(selected) >= _max_fields_per_form(bundle):
            break
    if not selected:
        # Reject model-invented question descriptors and use a deterministic
        # unanswered catalog item instead.
        fallback = next((item for item in specs if item["question_id"] not in answers), None)
        selected = [fallback] if fallback is not None else []
    if not selected:
        return text, None
    return text, _form_block(skill_id, selected)


def questionnaire_reply_is_valid(reply: str) -> bool:
    """Return whether a model reply follows the internal questionnaire envelope."""
    payload = _json_object(reply)
    if not isinstance(payload, dict) or not isinstance(payload.get("assistant_message"), str):
        return False
    return _requested_question_ids(payload) is not None


def deterministic_questionnaire_fallback(
    bundle: Any,
    state: Any,
    *invalid_replies: str,
) -> tuple[str, dict[str, Any] | None]:
    """Recover form-shaped questions mentioned by invalid model output."""
    combined = "\n".join(str(item or "") for item in invalid_replies)
    specs = available_question_specs(bundle, state)
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    answers = _answers(state, skill_id)
    unanswered = [item for item in specs if item["question_id"] not in answers]
    selected = [
        item
        for item in unanswered
        if str(item["question_id"]) in combined or str(item["label"]) in combined
    ]
    if "裸眼视力" in combined:
        selected.extend(
            item
            for item in unanswered
            if "裸眼视力" in str(item["question_id"]) and item not in selected
        )
    selected = selected[:_max_fields_per_form(bundle)]
    if not selected and _looks_like_followup_collection(state, combined):
        selected = unanswered[:1]
    if not selected:
        visible_text = str(invalid_replies[-1] if invalid_replies else "").strip()
        return visible_text, None
    return "请通过下面的表单补充关键信息。", _form_block(skill_id, selected)


def stage_questionnaire_form(state: Any, bundle: Any, block: dict[str, Any] | None) -> None:
    if not block:
        return
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    payload = block.get("payload") or {}
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    state.skill_facts.setdefault(skill_id, {})["_pending_questionnaire"] = {
        "form_id": payload.get("form_id"),
        "fields": [_pending_field(field) for field in fields if isinstance(field, dict)],
    }
    state.status_flags["native_questionnaire_form"] = block


def attach_staged_questionnaire_form(context: Any, state: Any) -> dict[str, Any] | None:
    block = state.status_flags.pop("native_questionnaire_form", None)
    if not isinstance(block, dict) or not context.messages or context.messages[-1].get("role") != "assistant":
        return None
    message = context.messages[-1]
    blocks = message.setdefault("blocks", [])
    if not isinstance(blocks, list):
        blocks = []
        message["blocks"] = blocks
    blocks.append(block)
    message.setdefault("metadata", {})["blocks"] = blocks
    context._ensure_message_ids()
    return block


def consume_pending_questionnaire_answer(state: Any, context: Any, bundle: Any, user_message: str) -> dict[str, Any] | None:
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    skill_state = state.skill_facts.setdefault(skill_id, {})
    pending = skill_state.get("_pending_questionnaire")
    if not isinstance(pending, dict):
        return None
    fields = _pending_fields(pending)
    accepted: list[tuple[dict[str, Any], Any]] = []
    remaining: list[dict[str, Any]] = []
    for field in fields:
        raw_value = _answer_from_message(
            user_message,
            str(field.get("label") or ""),
            allow_unlabelled=len(fields) == 1,
        )
        question_id = str(field.get("question_id") or "")
        if not raw_value and question_id and question_id != str(field.get("label") or ""):
            raw_value = _answer_from_message(
                user_message,
                question_id,
                allow_unlabelled=False,
            )
        if not raw_value:
            remaining.append(field)
            continue
        value = _normalize_answer(raw_value, field)
        if value is None:
            remaining.append(field)
            continue
        accepted.append((field, value))
    if not accepted:
        if fields:
            skill_state["last_questionnaire_error"] = "答案不符合当前题目的格式或选择约束。"
        return None
    answers = skill_state.setdefault("answers", {})
    promotions: list[dict[str, Any]] = []
    for field, value in accepted:
        question_id = str(field.get("question_id") or "")
        answers[question_id] = value
        promotions.extend(_promote(bundle, question_id, value, context, state, skill_id))
    skill_state["collected"] = dict(skill_state["answers"])
    derived_tier = _derive_tier(bundle, skill_state["answers"])
    if derived_tier:
        skill_state["tier"] = derived_tier
    if remaining:
        pending["fields"] = remaining
        skill_state["last_questionnaire_error"] = "部分答案缺失或不符合选择约束。"
    else:
        skill_state.pop("_pending_questionnaire", None)
        skill_state.pop("last_questionnaire_error", None)
    question_ids = [str(field.get("question_id") or "") for field, _value in accepted]
    values = {str(field.get("question_id") or ""): value for field, value in accepted}
    return {
        "question_id": question_ids[0],
        "question_ids": question_ids,
        "value": values[question_ids[0]],
        "values": values,
        "promotions": promotions,
    }


def _parse_rule_table(content: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for line in content.splitlines():
        compact = line.replace("|", "").replace(" ", "").strip()
        if not line.lstrip().startswith("|") or "字段" in line or set(compact) <= {":", "-"}:
            continue
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        question_id, label, kind, value_type, rule = cells[:5]
        if not question_id or not label:
            continue
        input_type = "multi_select" if "多选" in kind else "single_select" if "单选" in kind else "text"
        maximum = re.search(r"上限\s*(\d+)", kind)
        options = _options(rule)
        if "行政区划组件" in rule:
            options = [{"label": item, "value": item} for item in _PROVINCES]
        specs.append({
            "question_id": question_id,
            "label": label,
            "input_type": input_type,
            "value_type": _value_type(value_type),
            "options": options,
            "max_selections": int(maximum.group(1)) if maximum else None,
            "decimal_places": _decimal_places(rule),
            "display_condition": _display_condition(question_id, rule),
            "option_conditions": _option_conditions(rule),
            "rule": rule,
        })
    if any(item["question_id"] == "升学诉求" for item in specs):
        # The source row explicitly defines the secondary choices.  Expose it
        # as a distinct, conditionally displayed field rather than asking the
        # model to invent a non-canonical question.
        specs.append({
            "question_id": "稳就业方向",
            "label": "稳就业方向",
            "input_type": "multi_select",
            "value_type": "string",
            "options": [{"label": item, "value": item} for item in ("军警", "师范", "农科", "医学", "飞行员")],
            "max_selections": None,
            "decimal_places": None,
            "display_condition": "当升学诉求包含稳就业时显示；选项由升学诉求规则行的二级选项定义",
            "option_conditions": {},
            "rule": "来源：升学诉求行的“稳就业下二级选项为军警、师范、农科、医学、飞行员”。",
        })
    return specs


def _apply_state_patch(state: Any, bundle: Any, patch: Any) -> None:
    """Keep native state explicit without allowing a model to mutate Facts."""
    if not isinstance(patch, dict):
        return
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    skill_state = state.skill_facts.setdefault(skill_id, {})
    for key in ("mode", "collected", "tier", "target_path", "matched_paths"):
        if key in patch and isinstance(patch[key], (str, int, float, bool, list, dict)):
            skill_state[key] = patch[key]
    stage = str(patch.get("stage") or "").strip()
    allowed_stages = {item.id for item in bundle.contract.stages}
    if stage and stage in allowed_stages:
        state.stage = stage
        skill_state["stage"] = stage


def _requested_question_ids(payload: dict[str, Any]) -> list[str] | None:
    raw_ids = payload.get("question_ids")
    if isinstance(raw_ids, list):
        return list(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
    legacy_question = payload.get("question")
    if isinstance(legacy_question, dict):
        question_id = str(legacy_question.get("question_id") or "").strip()
        return [question_id] if question_id else []
    return None


def _max_fields_per_form(bundle: Any) -> int:
    return _positive_int(questionnaire_config(bundle).get("max_fields_per_form")) or _DEFAULT_MAX_FIELDS_PER_FORM


def _looks_like_followup_collection(state: Any, text: str) -> bool:
    skill_id = str(getattr(state, "active_skill_id", ""))
    skill_state = getattr(state, "skill_facts", {}).get(skill_id, {})
    mode = str(skill_state.get("mode") or "") if isinstance(skill_state, dict) else ""
    if mode in {"recommend", "match_single"}:
        return True
    latest_user_message = next(
        (item.content for item in reversed(getattr(state, "messages", [])) if getattr(item, "role", "") == "user"),
        "",
    )
    signal_text = f"{latest_user_message}\n{text}"
    return any(token in signal_text for token in ("匹配", "适合", "能不能", "测一下", "补充", "请选择", "请告诉"))


def _pending_field(field: dict[str, Any]) -> dict[str, Any]:
    return {
        key: field.get(key)
        for key in (
            "question_id",
            "fact_key",
            "label",
            "input_type",
            "options",
            "max_selections",
            "value_type",
            "decimal_places",
        )
    }


def _pending_fields(pending: dict[str, Any]) -> list[dict[str, Any]]:
    fields = pending.get("fields")
    if isinstance(fields, list):
        return [dict(field) for field in fields if isinstance(field, dict)]
    # Read sessions created by the original single-question implementation.
    return [_pending_field(pending)] if pending.get("question_id") else []


def _normalize_spec(field: dict[str, Any]) -> dict[str, Any] | None:
    question_id = str(field.get("question_id") or field.get("id") or "").strip()
    label = str(field.get("label") or question_id).strip()
    if not question_id or not label:
        return None
    raw_options = field.get("options", [])
    options = []
    if isinstance(raw_options, list):
        for option in raw_options:
            if isinstance(option, dict):
                value = str(option.get("value") or option.get("label") or "").strip()
                text = str(option.get("label") or value).strip()
            else:
                value = text = str(option).strip()
            if value:
                options.append({"label": text, "value": value})
    return {
        "question_id": question_id,
        "label": label,
        "input_type": str(field.get("input_type") or "text"),
        "value_type": _value_type(str(field.get("value_type") or "string")),
        "options": options,
        "max_selections": _positive_int(field.get("max_selections")),
        "decimal_places": _positive_int(field.get("decimal_places")),
        "display_condition": str(field.get("display_condition") or ""),
        "option_conditions": field.get("option_conditions") if isinstance(field.get("option_conditions"), dict) else {},
        "rule": str(field.get("rule") or ""),
    }


def _options(rule: str) -> list[dict[str, str]]:
    match = re.search(r"选项：([^<\n]+)", rule)
    if not match:
        return []
    option_text = re.sub(r"，[^，]*(?:仅在|只在|仅).*", "", match.group(1))
    items = []
    for raw_item in re.split(r"[、/]", option_text):
        item = raw_item.strip().rstrip("，,").strip()
        item = re.sub(r"[（(]仅限[^）)]*[）)]$", "", item).strip()
        items.append(item)
    return [{"label": item, "value": item} for item in items if item]


def _display_condition(question_id: str, rule: str) -> str:
    if question_id == "升学诉求":
        return "根据当前分数档显示：本科/一段线及以上、专科/二段线以上且本科/一段线以下；低于专科/二段线时不显示"
    if question_id == "学考成绩":
        return "高考省份为浙江，且成绩高于专科/二段线时显示"
    if question_id == "竞赛情况":
        return "成绩高于特控线时显示"
    return rule if "当" in rule or "仅在" in rule else ""


def _option_conditions(rule: str) -> dict[str, dict[str, str]]:
    if "技术仅在高考省份为浙江时出现" in rule or "技术（仅限浙江）" in rule:
        return {"技术": {"高考省份": "浙江"}}
    return {}


def _decimal_places(rule: str) -> int | None:
    match = re.search(r"保留\s*(\d+)\s*位小数", rule)
    return int(match.group(1)) if match else None


def _materialize_spec(spec: dict[str, Any], answers: dict[str, Any], tier: str) -> dict[str, Any]:
    result = dict(spec)
    option_conditions = spec.get("option_conditions", {})
    result["options"] = [
        dict(option)
        for option in spec.get("options", [])
        if not isinstance(option_conditions.get(str(option.get("value"))), dict)
        or all(answers.get(key) == expected for key, expected in option_conditions[str(option.get("value"))].items())
    ]
    if spec.get("question_id") == "升学诉求":
        if tier in {"特控线上", "一段本科线上"}:
            result["options"] = [{"label": item, "value": item} for item in ("冲院校", "稳就业", "无明确需求")]
        elif tier == "二段专科线上本科线下":
            result["options"] = [{"label": item, "value": item} for item in ("冲本科", "保院校", "无明确需求")]
    return result


def _question_visible(spec: dict[str, Any], answers: dict[str, Any], tier: str) -> bool:
    question_id = spec.get("question_id")
    if question_id == "升学诉求":
        return tier in {"特控线上", "一段本科线上", "二段专科线上本科线下"}
    if question_id == "稳就业方向":
        values = answers.get("升学诉求", [])
        values = values if isinstance(values, list) else [values]
        return "稳就业" in values
    if question_id == "学考成绩":
        return answers.get("高考省份") == "浙江" and tier in {
            "特控线上", "一段本科线上", "二段专科线上本科线下",
        }
    if question_id == "竞赛情况":
        return tier == "特控线上"
    return True


def _derive_tier(bundle: Any, answers: dict[str, Any]) -> str:
    """Calculate the table's score band from local province-line data only."""
    try:
        score = int(answers.get("预估高考总分"))
    except (TypeError, ValueError):
        return ""
    province = str(answers.get("高考省份") or "").strip()
    if not province:
        return ""
    path = Path(bundle.root_dir) / "references/province_lines_2026.md"
    if not path.is_file():
        return ""
    section = ""
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "3+3 省份" in raw_line:
            section = "3+3"
        elif "3+1+2 省份" in raw_line:
            section = "3+1+2"
        elif "文理（老高考）" in raw_line:
            section = "legacy"
        if not raw_line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
        if not cells or cells[0] != province:
            continue
        numbers = [_leading_int(value) for value in cells[1:]]
        if section == "3+3" and len(numbers) >= 4 and all(item is not None for item in numbers[:4]):
            return _score_tier(score, numbers[1], numbers[2], numbers[3])
        if section == "3+1+2" and len(numbers) >= 5:
            subject = answers.get("选科科目", [])
            subject_values = subject if isinstance(subject, list) else [subject]
            is_history = "历史" in subject_values
            tl, bl = (numbers[1], numbers[3]) if is_history else (numbers[0], numbers[2])
            if tl is not None and bl is not None and numbers[4] is not None:
                return _score_tier(score, tl, bl, numbers[4])
        if section == "legacy" and len(numbers) >= 5:
            subject = answers.get("选科科目", [])
            subject_values = subject if isinstance(subject, list) else [subject]
            is_liberal_arts = "历史" in subject_values or "文科" in subject_values
            tl, bl = (numbers[0], numbers[2]) if is_liberal_arts else (numbers[1], numbers[3])
            if tl is not None and bl is not None and numbers[4] is not None:
                return _score_tier(score, tl, bl, numbers[4])
    return ""


def _leading_int(value: str) -> int | None:
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _score_tier(score: int, special_line: int, bachelor_line: int, college_line: int) -> str:
    if score >= special_line:
        return "特控线上"
    if score >= bachelor_line:
        return "一段本科线上"
    if score >= college_line:
        return "二段专科线上本科线下"
    return "二段线下"


def _form_field(skill_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(spec["question_id"].encode("utf-8")).hexdigest()[:12]
    field: dict[str, Any] = {
        "fact_key": f"native_question.{skill_id}.{digest}",
        "question_id": spec["question_id"],
        "label": spec["label"],
        "input_type": spec["input_type"],
        "required": True,
        "placeholder": "请输入" if spec["input_type"] == "text" else "请选择",
        "example": "",
        "options": spec["options"],
        "submit_mode": "auto" if spec["input_type"] == "single_select" else "manual",
        "scope": "skill_session",
        "value_type": spec["value_type"],
    }
    if spec.get("max_selections"):
        field["max_selections"] = spec["max_selections"]
    if spec.get("decimal_places") is not None:
        field["decimal_places"] = spec["decimal_places"]
    return field


def _form_block(skill_id: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
    question_key = "\0".join(str(spec["question_id"]) for spec in specs)
    digest = hashlib.sha256(question_key.encode("utf-8")).hexdigest()[:12]
    return {
        "type": "fact_form",
        "payload": {
            "form_id": f"native_question:{skill_id}:{digest}",
            "title": "补充关键信息",
            "fields": [_form_field(skill_id, spec) for spec in specs],
        },
    }


def _answer_from_message(message: str, label: str, *, allow_unlabelled: bool) -> str:
    match = re.search(rf"{re.escape(label)}\s*[：:]\s*([^；;\n]+)", str(message or ""))
    if match:
        return match.group(1).strip()
    return str(message or "").strip() if allow_unlabelled else ""


def _normalize_answer(raw: str, pending: dict[str, Any]) -> Any | None:
    option_values = {
        str(option.get("value")).strip()
        for option in pending.get("options", [])
        if isinstance(option, dict) and str(option.get("value") or "").strip()
    }
    normalized_raw = str(raw or "").strip()
    if pending.get("input_type") == "multi_select":
        values = list(dict.fromkeys(item.strip() for item in re.split(r"[、,，/]", normalized_raw) if item.strip()))
        if not values or (option_values and any(item not in option_values for item in values)):
            return None
        maximum = _positive_int(pending.get("max_selections"))
        return values if not maximum or len(values) <= maximum else None
    if pending.get("input_type") == "single_select" and option_values and normalized_raw not in option_values:
        return None
    try:
        if pending.get("value_type") == "integer":
            return int(normalized_raw) if re.fullmatch(r"[+-]?\d+", normalized_raw) else None
        if pending.get("value_type") == "number":
            normalized = normalized_raw
            decimal_places = _positive_int(pending.get("decimal_places"))
            if decimal_places is not None:
                pattern = rf"[+-]?\d+(?:\.\d{{1,{decimal_places}}})?"
                if not re.fullmatch(pattern, normalized):
                    return None
            return float(normalized)
    except ValueError:
        return None
    return normalized_raw


def flush_deferred_questionnaire_promotions(state: Any, context: Any, bundle: Any) -> list[dict[str, Any]]:
    """Write mappings configured for Skill completion, not for form submit."""
    skill_id = str(getattr(state, "active_skill_id", "") or bundle.contract.skill_id)
    skill_state = state.skill_facts.setdefault(skill_id, {})
    pending = skill_state.pop("_deferred_questionnaire_promotions", [])
    if not isinstance(pending, list):
        return []
    promotions: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        promotions.extend(
            _write_promotion(
                bundle,
                str(item.get("question_id") or ""),
                item.get("value"),
                context,
                skill_id,
            )
        )
    return promotions


def _promote(
    bundle: Any,
    question_id: str,
    value: Any,
    context: Any,
    state: Any,
    skill_id: str,
) -> list[dict[str, Any]]:
    persistence = questionnaire_config(bundle).get("persistence", {})
    mappings = persistence.get("mappings", {}) if isinstance(persistence, dict) else {}
    mapping = mappings.get(question_id) if isinstance(mappings, dict) else None
    if not isinstance(mapping, dict):
        return []
    timing = str(mapping.get("submit_timing") or mapping.get("write_timing") or "on_submit")
    if timing == "on_completion":
        state.skill_facts.setdefault(skill_id, {}).setdefault("_deferred_questionnaire_promotions", []).append(
            {"question_id": question_id, "value": value}
        )
        return []
    if timing != "on_submit":
        return []
    return _write_promotion(bundle, question_id, value, context, skill_id)


def _write_promotion(
    bundle: Any,
    question_id: str,
    value: Any,
    context: Any,
    skill_id: str,
) -> list[dict[str, Any]]:
    persistence = questionnaire_config(bundle).get("persistence", {})
    mappings = persistence.get("mappings", {}) if isinstance(persistence, dict) else {}
    mapping = mappings.get(question_id) if isinstance(mappings, dict) else None
    if not isinstance(mapping, dict):
        return []
    scope = _SCOPE_BY_TARGET.get(str(mapping.get("target") or ""))
    fact_key = str(mapping.get("fact_key") or "")
    if not scope or not fact_key:
        return []
    normalized = FactService.validate_configured_update(
        fact_key,
        _fact_value(value, str(mapping.get("value_type") or "string")),
        declared_value_type=str(mapping.get("value_type") or "") or None,
    )
    if normalized is None:
        return []
    record = context.update_fact(
        fact_key,
        normalized,
        source_skill=skill_id,
        source_type="native_questionnaire",
        source_id=question_id,
        source_label=str(getattr(bundle.runtime_metadata, "name", "") or skill_id),
        scope=scope,
    )
    return [{"fact_key": fact_key, "scope": record.scope, "value": record.value}]


def _answers(state: Any, skill_id: str) -> dict[str, Any]:
    value = getattr(state, "skill_facts", {}).get(skill_id, {}).get("answers", {})
    return dict(value) if isinstance(value, dict) else {}


def _json_object(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _fact_value(value: Any, value_type: str) -> Any | None:
    try:
        if value_type == "integer":
            return int(value)
        if value_type == "number":
            return float(value)
        if value_type == "string_list":
            return value if isinstance(value, list) else [str(value)]
        return str(value)
    except (TypeError, ValueError):
        return None


def _value_type(value: str) -> str:
    value = str(value or "").lower()
    if value in {"整数", "integer", "int"}:
        return "integer"
    if value in {"小数", "number", "decimal", "float"}:
        return "number"
    return "string"


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
