from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import os

import yaml

from hailiang_skills.skill_runtime.models import SessionState
from hailiang_skills.skill_runtime.tools import build_tool_specs
from hailiang_skills.workbench.orchestration import orchestrator_snapshot_view
from .contracts import HarnessError


def normalize_skill(value):
    from hailiang_skills.skill_runtime.skill_registry import _registry_skill_id
    return _registry_skill_id(value)


def load_settings():
    path = Path(__file__).resolve().parents[3] / "config" / "runtime.yml"
    return (yaml.safe_load(path.read_text()).get("harness") or {}) if path.exists() else {}


def formal_orchestration_enabled():
    """Switch for running head/tail/recommend in formal (non-debug) sessions.

    On by default; set ``HAILIANG_HARNESS_FORMAL_ORCHESTRATION=false`` to fall
    back to the main-agent delegation flow. Only new runs are affected because
    each run freezes its policy when it is created.
    """
    return os.getenv("HAILIANG_HARNESS_FORMAL_ORCHESTRATION", "true").strip().lower() not in {"0", "false", "off", "no"}


def validate_skill_boundary(independent, experts):
    independent = {normalize_skill(str(s)) for s in independent}
    internal = {normalize_skill(str(s)) for e in experts.values() for s in e["skills"]}
    if independent & internal:
        raise HarnessError("INDEPENDENT_SKILL_OVERLAPS_EXPERT", 422)
    return sorted(independent)


def snapshot_root(snapshot):
    """Resolve the tested object's type and key; candidate revisions only store ids."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    root = dict(snapshot.get("root") or {})
    if root.get("object_type") and root.get("object_key"):
        return root
    object_id = root.get("object_id")
    for entry in snapshot.get("entries") or []:
        if object_id and isinstance(entry, dict) and entry.get("object_id") == object_id:
            return {**root, "object_type": entry.get("object_type"), "object_key": entry.get("object_key")}
    return root


def capture_policy(orchestrator, context):
    """Freeze rules, catalogs and tool schemas for this run, not global mutable state."""
    skills, experts, teams = orchestrator.runtime_registry, orchestrator.expert_registry, orchestrator.expert_team_registry
    entries = context.session_meta.get("configuration_snapshot", {}).get("entries")
    if entries:
        from hailiang_skills.workbench.catalog import build_runtime_registries
        skills, experts, teams = build_runtime_registries(entries)
    team_id = context.session_meta.get("expert_team_id")
    if not team_id:
        from hailiang_skills.runtime_bridge.default_expert_team import default_expert_team_id
        team_id = default_expert_team_id()
    team = teams.get(team_id)
    if context.session_meta.get("expert_team_id") and team is None:
        raise HarnessError("EXPERT_TEAM_NOT_AUTHORIZED", 403)
    allowed_experts = team.member_expert_ids if team else tuple(experts.definitions)
    if context.session_meta.get("expert_id"):
        expert_id = context.session_meta["expert_id"]
        if experts.get(expert_id) is None or (context.session_meta.get("expert_team_id") and expert_id not in allowed_experts):
            raise HarnessError("EXPERT_NOT_AUTHORIZED", 403)
        allowed_experts = (expert_id,)
    if context.session_meta.get("harness_experts_disabled"):
        allowed_experts = ()
    expert_data = {}
    for expert_id in allowed_experts:
        e = experts.get(expert_id)
        if e:
            expert_data[expert_id] = {"name": e.name, "brief": e.brief, "rules": e.rules_markdown,
                                     "skills": [normalize_skill(s) for s in e.authorized_skill_ids],
                                     "max_skill_calls": e.max_skill_calls}
    settings = load_settings()
    debug_root = snapshot_root(context.session_meta.get("configuration_snapshot")) if context.session_meta.get("workbench_debug_session_id") else {}
    classifications = {key: {"skills": e.authorized_skill_ids} for key, e in orchestrator.expert_registry.definitions.items()}
    classifications.update({key: {"skills": e.authorized_skill_ids} for key, e in experts.definitions.items()})
    independent = validate_skill_boundary(settings.get("independent_skill_ids", []), classifications)
    if debug_root.get("object_type") == "skill":
        root_skill = normalize_skill(debug_root["object_key"])
        independent = [skill_id for skill_id in independent if skills.get(skill_id)]
        if root_skill not in independent:
            # A candidate Skill is tested through an explicitly scoped test
            # expert, never by granting the main router an internal Skill.
            expert_data["harness_candidate_skill"] = {"name": "候选 Skill 测试专家", "brief": "负责当前候选 Skill 的专项分析与验证",
                "rules": f"你负责候选能力测试。对分派任务调用授权 Skill {root_skill}，收到结果后继续分析并提交。不得扩大授权。",
                "skills": [root_skill], "max_skill_calls": 3}
    skill_data = {}
    for skill_id in set(independent) | {s for e in expert_data.values() for s in e["skills"]}:
        bundle = skills.get(skill_id)
        if bundle is None:
            continue
        bundle = orchestrator._configured_bundle(context, bundle)
        state = SessionState(session_id=context.session_id, active_skill_id=skill_id)
        specs = build_tool_specs(bundle, state)
        # Local Skill document search is enabled only when the Skill grants access
        # to its local references/assets. This is not a separate RAG service.
        from hailiang_skills.skill_runtime.tools import _is_capability_enabled, _get_web_search_http_config
        enabled_specs = [asdict(s) for s in specs if s.enabled]
        if _is_capability_enabled(bundle, "rag") and not any(s["name"] == "rag" for s in enabled_specs):
            enabled_specs.append({"name": "rag", "description": "检索当前 Skill 的本地文档与资料资产", "parameters_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False}, "enabled": True})
        if _is_capability_enabled(bundle, "web_search") and _get_web_search_http_config(bundle) and not any(s["name"] == "web_search" for s in enabled_specs):
            enabled_specs.append({"name": "web_search", "description": "搜索外部资料", "parameters_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False}, "enabled": True})
        skill_data[skill_id] = {"name": bundle.runtime_metadata.name or skill_id, "rules": bundle.skill_markdown,
                               "specs": enabled_specs, "root": str(bundle.root_dir), "metadata": bundle.metadata,
                               "tool_registry": asdict(bundle.tool_registry), "contract": asdict(bundle.contract)}
        from hailiang_skills.skill_runtime.skill_contract import field_uses
        skill_data[skill_id]["fact_keys"] = list(field_uses(bundle.contract))
        from .assets import snapshot_assets
        skill_data[skill_id]["assets"] = snapshot_assets(bundle.root_dir)
    if set(independent) - set(skill_data):
        raise HarnessError("INDEPENDENT_SKILL_UNAVAILABLE")
    for expert in expert_data.values():
        expert["skills"] = [skill_id for skill_id in expert["skills"] if skill_id in skill_data]
    model_config = {}
    resolve_client = getattr(orchestrator, "_runtime_client_for_context", None)
    client = resolve_client(context) if resolve_client else None
    if client is not None and getattr(client, "_config", None) is not None:
        model_config = {key: value for key, value in asdict(client._config).items() if key != "api_key"}
    orchestrator_view = orchestrator_snapshot_view(
        team,
        context.session_meta.get("configuration_snapshot") or {},
        enabled=bool(context.session_meta.get("orchestrator_v2")),
    )
    # An expert team runs the whole formal flow: identity gate, orchestrator
    # head, experts, tail and recommendations. Single experts and Skills keep
    # the light path without orchestration.
    #   * Workbench debug session: the tested object (from the candidate
    #     snapshot) decides — only an expert_team is orchestrated.
    #   * Formal session: the published deployment snapshot carries the team and
    #     the published orchestration rules. A user who picked one specific
    #     expert (session expert_id) bypasses orchestration.
    debug_session = bool(context.session_meta.get("workbench_debug_session_id"))
    tested = snapshot_root(context.session_meta.get("configuration_snapshot")) if debug_session else {}
    root_type = tested.get("object_type")
    direct_expert_id = str(tested.get("object_key") or "") if root_type == "expert" and tested.get("object_key") in expert_data else ""
    if debug_session:
        scope_allows = root_type == "expert_team"
    else:
        scope_allows = formal_orchestration_enabled() and team is not None and not context.session_meta.get("expert_id")
    orchestrated = bool(
        scope_allows
        and not context.session_meta.get("harness_experts_disabled")
        and str(orchestrator_view.get("head_rules") or "").strip()
        and any(item.get("expert_id") in expert_data for item in orchestrator_view.get("members", []))
    )
    if orchestrated:
        orchestrator_view["enabled"] = True
    fallback = str(orchestrator_view.get("fallback_expert_id") or "")
    if fallback not in expert_data and team is not None and getattr(team, "coordinator_expert_id", "") in expert_data:
        # Older team snapshots name the catch-all expert only as coordinator.
        fallback = team.coordinator_expert_id
    if fallback not in expert_data:
        fallback = ""
    return {"experts": expert_data, "skills": skill_data, "independent_skills": independent,
            "harness_fallback_expert_id": fallback,
            "orchestrated": orchestrated,
            # Workbench debug sessions preset identity facts and keep the plain
            # text gate. Real users have nowhere else to supply them, so the
            # gate asks through a form card.
            "identity_gate": "text" if debug_session else "form",
            "direct_expert_id": direct_expert_id,
            "settings": {"context_policy_version": "v2" if os.getenv("HAILIANG_CONTEXT_POLICY", settings.get("context_policy_version", "v1")) == "v2" else "v1",
                         "max_parallel": int(settings.get("max_parallel", 3)), "max_model_calls": int(settings.get("max_model_calls", 24)),
                         "max_search_calls_per_task": int(settings.get("max_search_calls_per_task", 5)),
                         "max_tool_calls": int(settings.get("max_tool_calls", 48)), "context_tokens": int(settings.get("context_tokens", 64000)),
                         "max_tool_evidence_chars": int(settings.get("max_tool_evidence_chars", 8000)),
                         "max_output_tokens": int(settings.get("max_output_tokens", 8192)),
                         "max_run_input_tokens": max(0, int(os.getenv("HAILIANG_HARNESS_MAX_RUN_INPUT_TOKENS", settings.get("max_run_input_tokens", 300000)))),
                         "main_input_reserve_tokens": int(settings.get("main_input_reserve_tokens", 32000)),
                         "max_run_model_calls": int(settings.get("max_run_model_calls", 96)), "max_run_tool_calls": int(settings.get("max_run_tool_calls", 192)), "max_tasks": int(settings.get("max_tasks", 64))},
            "team_id": team_id, "snapshot": context.session_meta.get("configuration_snapshot", {}),
            "model_config": model_config,
            "soul": orchestrator._soul_context(context).get("content", ""),
            "orchestrator": orchestrator_view}


def harness_fallback_expert_id(policy):
    """Expert that must answer when the main agent has no matching specialist."""
    experts = policy.get("experts") or {}
    orchestrator = policy.get("orchestrator") if isinstance(policy.get("orchestrator"), dict) else {}
    expert_id = str(policy.get("harness_fallback_expert_id") or orchestrator.get("fallback_expert_id") or "").strip()
    return expert_id if expert_id in experts else ""


def main_agent_fallback_clause(policy):
    expert_id = harness_fallback_expert_id(policy)
    if not expert_id:
        return ""
    name = (policy.get("experts") or {}).get(expert_id, {}).get("name") or expert_id
    return (
        f"寒暄、致谢、没有实质专业任务，以及对不上任何授权专家的问题，必须调用 delegate_tasks 分给兜底专家「{name}」。"
        "主 agent 不得自己回答这类问题，也不得自称任何专家。"
    )


def allowed_skills(run, task):
    if task["kind"] == "main":
        return run["policy"]["independent_skills"]
    if task["kind"] == "expert":
        return run["policy"]["experts"][task["expert_id"]]["skills"]
    return []


def check_skill(run, task, skill_id):
    skill_id = normalize_skill(skill_id)
    if skill_id not in allowed_skills(run, task) or skill_id not in run["policy"]["skills"]:
        raise HarnessError("SKILL_NOT_AUTHORIZED", 403)
    return skill_id
