"""Published orchestration rules and team consultation fields.

Platform-agent decisions reflected here:

- a member's routing scope stays on ``routing_brief``
- applicable school stages stay on the team member
- team collaboration hints stay on ``collaboration_rules``
- recommendations are model-emitted ``jump_cards`` validated against catalog enums;
  legacy ``match_terms`` list matching is deprecated and only kept for old revisions
- system skills (orchestrate_head / orchestrate_tail / recommend) are the source of
  rule bodies; ``config/orchestration`` remains a seed/compat mirror
- ``HAILIANG_ORCHESTRATOR_V2`` defaults off and is frozen onto new sessions only
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.runtime_bridge.consultation_policy import (
    ConsultationPolicy,
    OrchestrationConfigError,
    parse_applicable_stages,
    parse_consultation_policy,
)
from hailiang_skills.runtime_bridge.system_skill import (
    SystemSkillError,
    default_system_skills_root,
    load_system_skills_by_purpose,
)


ORCHESTRATOR_V2_ENV = "HAILIANG_ORCHESTRATOR_V2"
SEED_DIR = Path(__file__).resolve().parents[3] / "config" / "orchestration"
SYSTEM_SKILLS_DIR = default_system_skills_root()
RULE_PACKAGE_BODIES = (
    ("orchestrate_head", "head_rules"),
    ("orchestrate_tail", "tail_rules"),
    ("recommend", "recommendation_rules"),
)


def orchestrator_v2_enabled() -> bool:
    return os.getenv(ORCHESTRATOR_V2_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def stamp_new_session(context) -> None:
    """Freeze the gray-release switch onto a session that is being created."""
    meta = context.session_meta
    if "orchestrator_v2" not in meta:
        meta["orchestrator_v2"] = orchestrator_v2_enabled()


def member_keys(locks: list[dict[str, Any]]) -> set[str]:
    return {
        str(lock.get("object_key") or "")
        for lock in locks
        if isinstance(lock, dict) and str(lock.get("object_key") or "")
    }


def validate_expert_team_orchestration(
    payload: dict[str, Any],
    locks: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)``. Legacy revisions without the new keys stay valid."""
    errors: list[str] = []
    warnings: list[str] = []
    keys = member_keys(locks)
    object_ids = {
        str(lock.get("object_id") or "")
        for lock in locks
        if isinstance(lock, dict) and str(lock.get("object_id") or "")
    }
    if "consultation_policy" in payload:
        try:
            parse_consultation_policy(payload.get("consultation_policy"))
        except OrchestrationConfigError as exc:
            errors.append(str(exc))
    if "fallback_expert_id" in payload:
        fallback = str(payload.get("fallback_expert_id") or "").strip()
        if not fallback:
            warnings.append("未配置兜底专家时，编排首不能分发")
        elif fallback not in keys and fallback not in object_ids:
            errors.append("兜底专家必须属于本团且已发布")
    if "collaboration_rules" in payload and payload.get("collaboration_rules") is not None:
        if not isinstance(payload.get("collaboration_rules"), str):
            errors.append("collaboration_rules 必须是字符串")
    members = payload.get("members") if isinstance(payload.get("members"), list) else []
    for item in members:
        if not isinstance(item, dict) or "applicable_stages" not in item:
            continue
        try:
            parse_applicable_stages(item.get("applicable_stages"))
        except OrchestrationConfigError as exc:
            errors.append(str(exc))
            break
    return errors, warnings


def normalize_catalog(raw: Any) -> dict[str, Any]:
    """Normalize recommend catalog shape used by system skills and new seeds."""
    if raw is None:
        return {"schema_version": 1, "items": []}
    if isinstance(raw, list):
        items = raw
        schema_version = 1
    elif isinstance(raw, dict):
        schema_version = int(raw.get("schema_version") or 1)
        items = raw.get("items") if isinstance(raw.get("items"), list) else []
    else:
        raise OrchestrationConfigError("推荐 catalog 必须是对象或数组")
    normalized_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise OrchestrationConfigError(f"catalog 第 {index} 条必须是对象")
        item_id = str(item.get("id") or "").strip()
        intent = str(item.get("intent") or "").strip()
        object_type = str(item.get("object_type") or "").strip()
        if not item_id or not intent or not object_type:
            raise OrchestrationConfigError(
                f"catalog 第 {index} 条必须包含 id、intent、object_type"
            )
        if item_id in seen:
            raise OrchestrationConfigError(f"catalog id 重复: {item_id}")
        seen.add(item_id)
        entry = {
            "id": item_id,
            "title": str(item.get("title") or "").strip(),
            "kind": str(item.get("kind") or "feature").strip() or "feature",
            "when": str(item.get("when") or "").strip(),
            "intent": intent,
            "object_type": object_type,
            "url_or_route": str(item.get("url_or_route") or "").strip(),
        }
        sub_type = str(item.get("sub_type") or "").strip()
        if sub_type:
            entry["sub_type"] = sub_type
        normalized_items.append(entry)
    return {"schema_version": schema_version, "items": normalized_items}


def normalize_recommendations(raw: Any) -> list[dict[str, Any]]:
    """Legacy recommendation list.

    Deprecated: new revisions should publish catalog.json via the recommend
    system skill. This helper still accepts the old match_terms shape so frozen
    session snapshots keep loading.
    """
    if raw is None:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        # Allow callers that already migrated to catalog shape.
        return normalize_catalog(raw)["items"]
    if not isinstance(raw, list):
        raise OrchestrationConfigError("推荐清单必须是数组")
    if raw and all(isinstance(item, dict) and ("intent" in item or "object_type" in item) for item in raw):
        return normalize_catalog(raw)["items"]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise OrchestrationConfigError(f"推荐清单第 {index} 条必须是对象")
        recommendation_id = str(item.get("recommendation_id") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not recommendation_id or not title:
            raise OrchestrationConfigError(f"推荐清单第 {index} 条必须包含 recommendation_id 和 title")
        if recommendation_id in seen:
            raise OrchestrationConfigError(f"推荐清单 id 重复: {recommendation_id}")
        seen.add(recommendation_id)
        # Deprecated local keyword matching — retained for old published revisions only.
        terms = item.get("match_terms") if isinstance(item.get("match_terms"), list) else []
        match_terms = [str(term).strip() for term in terms if str(term).strip()]
        entry = {
            "recommendation_id": recommendation_id,
            "title": title,
            "summary": str(item.get("summary") or "").strip(),
            "match_terms": match_terms,
            "deprecated_match_terms": True,
        }
        items.append(entry)
    return items


def normalize_rule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OrchestrationConfigError("编排规则必须是对象")
    catalog = payload.get("recommend_catalog")
    if catalog is None and isinstance(payload.get("recommendations"), dict):
        catalog = payload.get("recommendations")
    recommendations_raw = payload.get("recommendations")
    if isinstance(recommendations_raw, dict):
        recommendations = normalize_catalog(recommendations_raw)["items"]
        catalog = recommendations_raw if catalog is None else catalog
    else:
        recommendations = normalize_recommendations(recommendations_raw or [])
    result = {
        "head_rules": str(payload.get("head_rules") or "").strip(),
        "tail_rules": str(payload.get("tail_rules") or "").strip(),
        "recommendation_rules": str(payload.get("recommendation_rules") or "").strip(),
        "recommendations": recommendations,
    }
    if catalog is not None:
        result["recommend_catalog"] = normalize_catalog(catalog)
    system_skills = payload.get("system_skills")
    if isinstance(system_skills, dict) and system_skills:
        result["system_skills"] = system_skills
    return result


def load_system_skill_seed() -> dict[str, Any]:
    """Load rule bodies and catalog from runtime_system_skills packages."""
    try:
        by_purpose = load_system_skills_by_purpose(SYSTEM_SKILLS_DIR)
    except SystemSkillError:
        return {}
    head = by_purpose.get("orchestrate_head")
    tail = by_purpose.get("orchestrate_tail")
    recommend = by_purpose.get("recommend")
    payload: dict[str, Any] = {
        "head_rules": head.body if head else "",
        "tail_rules": tail.body if tail else "",
        "recommendation_rules": recommend.body if recommend else "",
        "recommendations": [],
        "system_skills": {},
    }
    if recommend and recommend.catalog is not None:
        payload["recommend_catalog"] = normalize_catalog(recommend.catalog)
        payload["recommendations"] = payload["recommend_catalog"]["items"]
    for purpose, definition in by_purpose.items():
        payload["system_skills"][purpose] = definition.as_snapshot()
    return payload


def _stored_rule_text(text: str) -> tuple[str, str]:
    """Split an optional SKILL.md header from a saved rule body.

    Publishing the whole file keeps the version header in the text box. Comparison
    uses the body under that header, and reads ``version`` from it when the
    revision did not store a package version separately.
    """
    raw = str(text or "").strip()
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", raw
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return "", raw
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return "", raw
    if not isinstance(loaded, dict):
        return "", raw
    body = "\n".join(lines[end + 1 :]).strip()
    return str(loaded.get("version") or "").strip(), body


def rule_package_status(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Compare one stored revision with the rule packages currently on disk.

    ``current`` means the stored package version and body both match the file.
    ``body_current`` means the text matches but this revision never recorded a version.
    ``stale`` means the file has moved on. ``unknown`` means the package could not be read.
    """
    stored = payload if isinstance(payload, dict) else {}
    try:
        by_purpose = load_system_skills_by_purpose(SYSTEM_SKILLS_DIR)
    except SystemSkillError:
        by_purpose = {}
    skills = stored.get("system_skills") if isinstance(stored.get("system_skills"), dict) else {}
    status: dict[str, dict[str, Any]] = {}
    for purpose, body_key in RULE_PACKAGE_BODIES:
        definition = by_purpose.get(purpose)
        skill = skills.get(purpose) if isinstance(skills.get(purpose), dict) else {}
        header_version, comparable_body = _stored_rule_text(str(stored.get(body_key) or ""))
        version = str(skill.get("version") or "").strip() or header_version
        source_version = str(getattr(definition, "version", "") or "").strip()
        source_body = str(getattr(definition, "body", "") or "").strip()
        body_matches = bool(source_body) and comparable_body == source_body
        if definition is None:
            state = "unknown"
        elif version and source_version and version == source_version and body_matches:
            state = "current"
        elif not version and body_matches:
            state = "body_current"
        else:
            state = "stale"
        status[purpose] = {
            "version": version,
            "source_version": source_version,
            "body_matches": body_matches,
            "state": state,
        }
    return status


def load_seed_rules() -> dict[str, Any]:
    def read(name: str) -> str:
        path = SEED_DIR / name
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    system_seed = load_system_skill_seed()
    recommendations: list[Any] | dict[str, Any] = []
    catalog_path = SEED_DIR / "recommendations.json"
    if catalog_path.is_file():
        loaded = json.loads(catalog_path.read_text(encoding="utf-8") or "[]")
        recommendations = loaded if isinstance(loaded, (list, dict)) else []
    if system_seed.get("recommend_catalog") and not recommendations:
        recommendations = system_seed["recommend_catalog"]
    payload = normalize_rule_payload({
        "head_rules": system_seed.get("head_rules") or read("head_rules.md"),
        "tail_rules": system_seed.get("tail_rules") or read("tail_rules.md"),
        "recommendation_rules": system_seed.get("recommendation_rules") or read("recommendation_rules.md"),
        "recommendations": recommendations,
        "recommend_catalog": system_seed.get("recommend_catalog"),
    })
    if system_seed.get("system_skills"):
        payload["system_skills"] = system_seed["system_skills"]
    return payload


def preserve_orchestrator_binding(previous: dict[str, Any], nxt: dict[str, Any]) -> dict[str, Any]:
    """Keep an existing session on the rules it started with.

    Deployment sync may still refresh the rest of the catalog. Team consultation
    fields and the three orchestration documents stay on the snapshot captured
    when the session was created.
    """
    updated = copy.deepcopy(nxt)
    if "orchestration_rules" in previous:
        updated["orchestration_rules"] = copy.deepcopy(previous.get("orchestration_rules"))
    else:
        updated.pop("orchestration_rules", None)
    previous_entries = previous.get("entries") if isinstance(previous.get("entries"), list) else []
    next_entries = updated.get("entries") if isinstance(updated.get("entries"), list) else []
    previous_teams = {
        str(item.get("object_key") or ""): item
        for item in previous_entries
        if isinstance(item, dict) and item.get("object_type") == "expert_team"
    }
    for entry in next_entries:
        if not isinstance(entry, dict) or entry.get("object_type") != "expert_team":
            continue
        previous_team = previous_teams.get(str(entry.get("object_key") or ""))
        if not isinstance(previous_team, dict):
            continue
        payload = entry.setdefault("payload", {})
        if not isinstance(payload, dict):
            continue
        previous_payload = previous_team.get("payload") if isinstance(previous_team.get("payload"), dict) else {}
        for key in ("consultation_policy", "fallback_expert_id", "collaboration_rules"):
            if key in previous_payload:
                payload[key] = copy.deepcopy(previous_payload[key])
            else:
                payload.pop(key, None)
        previous_members = {
            str(item.get("expert_id") or ""): item
            for item in previous_payload.get("members", [])
            if isinstance(previous_payload.get("members"), list) and isinstance(item, dict)
        }
        members = payload.get("members")
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            old = previous_members.get(str(member.get("expert_id") or ""))
            if isinstance(old, dict) and "applicable_stages" in old:
                member["applicable_stages"] = list(old.get("applicable_stages") or [])
            else:
                member.pop("applicable_stages", None)
    return updated


def orchestrator_snapshot_view(team, snapshot: dict[str, Any] | None, *, enabled: bool, expert_registry=None) -> dict[str, Any]:
    """The slice ``capture_policy`` freezes for one run."""
    rules = {}
    if isinstance(snapshot, dict) and isinstance(snapshot.get("orchestration_rules"), dict):
        rules = snapshot["orchestration_rules"]
    policy = team.consultation_policy if team is not None and getattr(team, "consultation_policy", None) else ConsultationPolicy()
    members = []
    if team is not None:
        for member in team.members:
            # Expert's own brief from agent.yaml is the authoritative description
            # of what this expert handles.  Team-level routing_brief is an optional
            # override kept for backward compatibility.
            expert_brief = ""
            if expert_registry is not None:
                expert = expert_registry.get(member.expert_id)
                if expert is not None:
                    expert_brief = str(getattr(expert, "brief", "") or "").strip()
            routing_brief = str(member.routing_brief or "").strip() or expert_brief
            members.append({
                "expert_id": member.expert_id,
                "mention_name": member.mention_name,
                "routing_brief": routing_brief,
                "applicable_stages": list(getattr(member, "applicable_stages", ()) or ()),
            })
    system_skills = rules.get("system_skills") if isinstance(rules.get("system_skills"), dict) else {}
    if not system_skills:
        system_skills = {
            purpose: {
                "purpose": purpose,
                "body": str(rules.get(body_key) or ""),
                "catalog": rules.get("recommend_catalog") if purpose == "recommend" else None,
            }
            for purpose, body_key in (
                ("orchestrate_head", "head_rules"),
                ("orchestrate_tail", "tail_rules"),
                ("recommend", "recommendation_rules"),
            )
            if str(rules.get(body_key) or "").strip() or (
                purpose == "recommend" and isinstance(rules.get("recommend_catalog"), dict)
            )
        }
        # Drop empty synthetic entries.
        system_skills = {
            key: value
            for key, value in system_skills.items()
            if str(value.get("body") or "").strip() or value.get("catalog")
        }
    recommend_catalog = rules.get("recommend_catalog")
    if recommend_catalog is None and isinstance(system_skills.get("recommend"), dict):
        recommend_catalog = system_skills["recommend"].get("catalog")
    return {
        "enabled": bool(enabled),
        "consultation_policy": policy.as_dict(),
        "fallback_expert_id": str(getattr(team, "fallback_expert_id", "") or "") if team is not None else "",
        "collaboration_rules": str(getattr(team, "collaboration_rules", "") or "") if team is not None else "",
        "members": members,
        "head_rules": str(rules.get("head_rules") or ""),
        "tail_rules": str(rules.get("tail_rules") or ""),
        "recommendation_rules": str(rules.get("recommendation_rules") or ""),
        "recommendations": list(rules.get("recommendations") or []),
        "recommend_catalog": recommend_catalog if isinstance(recommend_catalog, dict) else None,
        "system_skills": system_skills,
    }
