from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

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


def validate_skill_boundary(independent, experts):
    independent = {normalize_skill(str(s)) for s in independent}
    internal = {normalize_skill(str(s)) for e in experts.values() for s in e["skills"]}
    if independent & internal:
        raise HarnessError("INDEPENDENT_SKILL_OVERLAPS_EXPERT", 422)
    return sorted(independent)


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
    debug_root = context.session_meta.get("configuration_snapshot", {}).get("root", {}) if context.session_meta.get("workbench_debug_session_id") else {}
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
    return {"experts": expert_data, "skills": skill_data, "independent_skills": independent,
            "settings": {"max_parallel": int(settings.get("max_parallel", 3)), "max_model_calls": int(settings.get("max_model_calls", 24)),
                         "max_search_calls_per_task": int(settings.get("max_search_calls_per_task", 5)),
                         "max_tool_calls": int(settings.get("max_tool_calls", 48)), "context_tokens": int(settings.get("context_tokens", 64000)),
                         "max_tool_evidence_chars": int(settings.get("max_tool_evidence_chars", 8000)),
                         "max_output_tokens": int(settings.get("max_output_tokens", 8192)),
                         "max_run_input_tokens": int(settings.get("max_run_input_tokens", 300000)),
                         "main_input_reserve_tokens": int(settings.get("main_input_reserve_tokens", 32000)),
                         "max_run_model_calls": int(settings.get("max_run_model_calls", 96)), "max_run_tool_calls": int(settings.get("max_run_tool_calls", 192)), "max_tasks": int(settings.get("max_tasks", 64))},
            "team_id": team_id, "snapshot": context.session_meta.get("configuration_snapshot", {}),
            "model_config": model_config,
            "soul": orchestrator._soul_context(context).get("content", ""),
            "orchestrator": orchestrator_snapshot_view(
                team,
                context.session_meta.get("configuration_snapshot") or {},
                enabled=bool(context.session_meta.get("orchestrator_v2")),
            )}


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
