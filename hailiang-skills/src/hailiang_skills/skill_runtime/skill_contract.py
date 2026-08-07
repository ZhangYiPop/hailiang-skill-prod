from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hailiang_skills.skill_runtime.models import FactsSchema, RouteTarget, SkillContract, StageContract

CONTRACT_FILE_NAME = "runtime_contract.json"


def load_skill_contract(skill_root: str | Path, metadata: dict[str, Any] | None = None) -> SkillContract:
    root = Path(skill_root).expanduser().resolve()
    contract_path = root / CONTRACT_FILE_NAME
    metadata = metadata if isinstance(metadata, dict) else {}
    if not contract_path.is_file():
        return default_skill_contract(root.name, metadata=metadata)
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{contract_path} 顶层必须是 JSON 对象")
    return parse_skill_contract(payload, fallback_name=root.name, metadata=metadata)


def default_skill_contract(root_name: str, *, metadata: dict[str, Any] | None = None) -> SkillContract:
    metadata = metadata if isinstance(metadata, dict) else {}
    skill_id = str(metadata.get("skill_id") or root_name).strip() or root_name
    skill_role = str(metadata.get("entrypoint_role") or metadata.get("skill_role") or "child").strip() or "child"
    return SkillContract(
        skill_id=skill_id,
        skill_role=skill_role,
        stages=(StageContract(id="init", kind="default"),),
        metadata={"generated_default": True},
    )


def parse_skill_contract(
    payload: dict[str, Any],
    *,
    fallback_name: str,
    metadata: dict[str, Any] | None = None,
) -> SkillContract:
    metadata = metadata if isinstance(metadata, dict) else {}
    skill_id = str(payload.get("skill_id") or metadata.get("skill_id") or fallback_name).strip() or fallback_name
    skill_role = str(
        payload.get("skill_role") or metadata.get("entrypoint_role") or metadata.get("skill_role") or "child"
    ).strip() or "child"
    stages_payload = payload.get("stages", [])
    stages = tuple(_parse_stage(item) for item in stages_payload if isinstance(item, dict)) or (
        StageContract(id="init", kind="default"),
    )
    facts_schema = _parse_facts_schema(payload.get("facts"))
    routes_payload = payload.get("routes", [])
    routes = tuple(_parse_route(item) for item in routes_payload if isinstance(item, dict))
    accepts_scenes = tuple(
        str(item).strip()
        for item in payload.get("accepts_scenes", [])
        if str(item).strip()
    ) if isinstance(payload.get("accepts_scenes"), list) else ()
    return SkillContract(
        skill_id=skill_id,
        skill_role=skill_role,
        stages=stages,
        facts_schema=facts_schema,
        routes=routes,
        accepts_scenes=accepts_scenes,
        metadata=dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {},
    )


def get_stage_contract(contract: SkillContract, stage_id: str) -> StageContract | None:
    normalized = str(stage_id or "").strip()
    for item in contract.stages:
        if item.id == normalized:
            return item
    return None


def _parse_stage(payload: dict[str, Any]) -> StageContract:
    stage_id = str(payload.get("id") or "").strip()
    if not stage_id:
        raise ValueError("runtime_contract.json 中的 stage.id 不能为空")
    return StageContract(
        id=stage_id,
        kind=str(payload.get("kind") or "default").strip() or "default",
        required_facts=_string_tuple(payload.get("required_facts")),
        enable_intent_check=bool(payload.get("enable_intent_check", False)),
        enable_satisfaction_check=bool(payload.get("enable_satisfaction_check", False)),
    )


def _parse_route(payload: dict[str, Any]) -> RouteTarget:
    scene = str(payload.get("scene") or "").strip()
    target_skill_id = str(payload.get("target_skill_id") or "").strip()
    if not scene or not target_skill_id:
        raise ValueError("runtime_contract.json 中的 route.scene 与 route.target_skill_id 不能为空")
    return RouteTarget(
        scene=scene,
        target_skill_id=target_skill_id,
        required_global_facts=_string_tuple(payload.get("required_global_facts")),
        required_skill_facts=_string_tuple(payload.get("required_skill_facts")),
    )


def _parse_facts_schema(payload: Any) -> FactsSchema:
    if not isinstance(payload, dict):
        return FactsSchema()
    stage_keys_payload = payload.get("stage", {})
    stage_keys: dict[str, tuple[str, ...]] = {}
    if isinstance(stage_keys_payload, dict):
        for key, value in stage_keys_payload.items():
            normalized = str(key).strip()
            if normalized:
                stage_keys[normalized] = _string_tuple(value)
    exports_payload = payload.get("exports", {})
    if not isinstance(exports_payload, dict):
        exports_payload = {}
    return FactsSchema(
        global_keys=_string_tuple(payload.get("global")),
        skill_keys=_string_tuple(payload.get("skill")),
        stage_keys=stage_keys,
        promote_to_global=_string_tuple(exports_payload.get("promote_to_global")),
        share_with_parent_skill=_string_tuple(exports_payload.get("share_with_parent_skill")),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
