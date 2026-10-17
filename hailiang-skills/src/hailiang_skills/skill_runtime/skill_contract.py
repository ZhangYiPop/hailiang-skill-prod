from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from hailiang_skills.skill_runtime.models import FactsSchema, RouteTarget, SkillContract, StageContract

CONTRACT_FILE_NAME = "runtime_contract.json"
PACKAGE_FIELDS_FILE = "assets/fields.json"


def load_skill_contract(skill_root: str | Path, metadata: dict[str, Any] | None = None) -> SkillContract:
    root = Path(skill_root).expanduser().resolve()
    contract_path = root / CONTRACT_FILE_NAME
    metadata = metadata if isinstance(metadata, dict) else {}
    package_fields = _load_package_fields(root)
    if not contract_path.is_file():
        contract = default_skill_contract(root.name, metadata=metadata)
        return replace(contract, package_fields=package_fields)
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{contract_path} 顶层必须是 JSON 对象")
    contract = parse_skill_contract(payload, fallback_name=root.name, metadata=metadata)
    return replace(contract, package_fields=package_fields)


def default_skill_contract(root_name: str, *, metadata: dict[str, Any] | None = None) -> SkillContract:
    metadata = metadata if isinstance(metadata, dict) else {}
    return replace(
        parse_skill_contract({}, fallback_name=root_name, metadata=metadata),
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
    stages_payload = payload.get("stages", metadata.get("stages", []))
    stages = tuple(_parse_stage(item) for item in stages_payload if isinstance(item, dict)) or (
        StageContract(id="init", kind="default"),
    )
    # P7: prefer top-level fields.{uses,outputs}; fall back to legacy facts.*
    fields_payload = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    facts_schema = _parse_facts_schema(payload.get("facts"), fields_payload=fields_payload)
    routes_payload = payload.get("routes", metadata.get("routes", []))
    routes = tuple(_parse_route(item) for item in routes_payload if isinstance(item, dict))
    accepts_scenes = tuple(
        str(item).strip()
        for item in payload.get("accepts_scenes", metadata.get("accepts_scenes", []))
        if str(item).strip()
    ) if isinstance(payload.get("accepts_scenes", metadata.get("accepts_scenes", [])), list) else ()
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


def field_uses(contract: SkillContract) -> tuple[str, ...]:
    """Declared fields this skill consumes (P6/P7)."""
    uses = tuple(contract.facts_schema.uses or ())
    if uses:
        return uses
    # Transition: merge legacy skill + global keys.
    return tuple(
        dict.fromkeys(
            [
                *contract.facts_schema.skill_keys,
                *contract.facts_schema.global_keys,
            ]
        )
    )


def _parse_stage(payload: dict[str, Any]) -> StageContract:
    stage_id = str(payload.get("id") or "").strip()
    if not stage_id:
        raise ValueError("runtime_contract.json 中的 stage.id 不能为空")
    required_fields = _string_tuple(payload.get("required_fields"))
    required_facts = _string_tuple(payload.get("required_facts"))
    # P7: required_fields is canonical; required_facts remains as transition alias.
    merged_required = required_fields or required_facts
    return StageContract(
        id=stage_id,
        kind=str(payload.get("kind") or "default").strip() or "default",
        required_facts=merged_required,
        required_fields=merged_required,
        enable_intent_check=bool(payload.get("enable_intent_check", False)),
        enable_satisfaction_check=bool(payload.get("enable_satisfaction_check", False)),
    )


def _parse_route(payload: dict[str, Any]) -> RouteTarget:
    target_skill_id = str(payload.get("target_skill_id") or "").strip()
    scene = str(payload.get("scene") or "").strip() or target_skill_id
    if not target_skill_id:
        raise ValueError("runtime_contract.json 中的 route.target_skill_id 不能为空")
    if not scene:
        raise ValueError("runtime_contract.json 中的 route 需要 scene 或 target_skill_id")
    return RouteTarget(
        scene=scene,
        target_skill_id=target_skill_id,
        required_global_facts=_string_tuple(payload.get("required_global_facts")),
        required_skill_facts=_string_tuple(payload.get("required_skill_facts")),
        pass_fields=_string_tuple(payload.get("pass_fields")),
    )


def _parse_facts_schema(payload: Any, *, fields_payload: dict[str, Any] | None = None) -> FactsSchema:
    fields_payload = fields_payload if isinstance(fields_payload, dict) else {}
    uses = _string_tuple(fields_payload.get("uses"))
    outputs = _string_tuple(fields_payload.get("outputs"))
    if not isinstance(payload, dict):
        return FactsSchema(uses=uses, outputs=outputs)
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
    skill_keys = _string_tuple(payload.get("skill"))
    share = _string_tuple(exports_payload.get("share_with_parent_skill"))
    # Transition mapping from the spec: facts.skill ↔ fields.uses, share_with_parent ↔ outputs.
    if not uses and skill_keys:
        uses = skill_keys
    if not outputs and share:
        outputs = share
    return FactsSchema(
        global_keys=_string_tuple(payload.get("global")),
        skill_keys=skill_keys or uses,
        stage_keys=stage_keys,
        promote_to_global=_string_tuple(exports_payload.get("promote_to_global")),
        share_with_parent_skill=share or outputs,
        uses=uses,
        outputs=outputs,
    )


def _load_package_fields(root: Path) -> dict[str, Any]:
    """P6: load package-local field table from assets/fields.json when present."""
    path = root / PACKAGE_FIELDS_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    fields = payload.get("fields")
    if isinstance(fields, dict):
        return {str(key): value for key, value in fields.items() if str(key).strip()}
    return {}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
