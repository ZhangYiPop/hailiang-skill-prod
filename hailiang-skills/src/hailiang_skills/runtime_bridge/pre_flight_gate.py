"""Pre-flight fact collection gate.

After the orchestrator decides which experts to dispatch, this gate checks
whether the activated skills' required_facts are all satisfied.  Missing
facts are collected through a single unified input card before experts run,
eliminating duplicate questions across skills.

Design:
  - Layer 1: entry-level required_facts from runtime_contract.json stages.
    Collected once, before any expert runs.
  - Layer 2/3: stage-dependent and intent-dependent facts remain in skill flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Contract discovery ──────────────────────────────────────────────────────

# Primary location: new-specification skill contracts.
_CONTRACTS_DIR = Path(
    "/Users/letty/code/工作/hailiang/测试/1020/新规范"
)

# Fallback: legacy runtime_skills directory relative to this module.
_LEGACY_SKILLS_DIR = Path(__file__).resolve().parents[2] / "runtime_skills"

# Map from contract-local fact keys to facts_schema canonical keys.
# Contracts use varying naming conventions; this normalizes them so the
# facts_schema.yml form-field definitions can be reused.
_FACT_KEY_ALIASES: dict[str, str] = {
    "gaokao_province": "student_province",
    "subject_choice": "subject_group",
    "estimated_score": "score_total",
    "education_budget": "budget_level",
    "foreign_lang": "foreign_language",
}


def _contract_path(skill_id: str) -> Path | None:
    """Return the runtime_contract.json path for *skill_id*, or None."""
    for base in (_CONTRACTS_DIR, _LEGACY_SKILLS_DIR):
        subdir = base / skill_id
        contract = subdir / "runtime_contract.json"
        if contract.is_file():
            return contract
        # The new-spec layout wraps the skill dir in a Chinese-named parent.
        if base == _CONTRACTS_DIR:
            for candidate in base.glob(f"*/{skill_id}/runtime_contract.json"):
                return candidate
    return None


def _load_contract(skill_id: str) -> dict[str, Any] | None:
    path = _contract_path(skill_id)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _normalize_fact_key(contract_key: str) -> str:
    """Map a contract-local fact key to the canonical facts_schema key."""
    return _FACT_KEY_ALIASES.get(contract_key, contract_key)


# ── Public API ──────────────────────────────────────────────────────────────


def collect_required_facts(skill_ids: list[str]) -> dict[str, str]:
    """Return ``{canonical_fact_key: original_contract_key}`` for every
    entry-level required fact across the given skills.

    Only facts listed in a stage's ``required_facts`` (or ``required_fields``)
    are included — stage-dependent and intent-dependent facts are left to the
    skill flow (Layer 2/3).
    """
    result: dict[str, str] = {}
    for sid in set(skill_ids):
        contract = _load_contract(sid)
        if not contract:
            continue
        for stage in contract.get("stages", []):
            required: list[str] = (
                stage.get("required_facts")
                or stage.get("required_fields")
                or []
            )
            for key in required:
                canonical = _normalize_fact_key(key)
                if canonical not in result:
                    result[canonical] = key
    return result


def missing_pre_flight_facts(
    required: dict[str, str], known_facts: dict[str, Any]
) -> list[str]:
    """Return canonical fact keys from *required* whose value is not present
    (or is ``None``) in *known_facts*.
    """
    return [
        k for k in required
        if k not in known_facts or known_facts.get(k) is None
    ]


def build_pre_flight_fields(
    missing_keys: list[str], required_labels: dict[str, str],
    known_facts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build ``request_user_input`` field definitions for missing pre-flight
    facts.

    Pulls field metadata (input_type, allowed_values, placeholder, scope,
    etc.) from ``facts_schema.yml`` via ``facts_config``.  Facts not found in
    the schema fall back to a plain ``text`` field.
    """
    from hailiang_skills.core.facts_config import get_fact_schema
    from hailiang_skills.core.facts_config import get_fact_scope_policy

    schema = get_fact_schema()
    fields: list[dict[str, Any]] = []

    for key in missing_keys:
        meta = schema.get(key, {})
        scope = get_fact_scope_policy(key, "profile")

        field: dict[str, Any] = {
            "key": key,
            "label": meta.get("label", required_labels.get(key, key)),
            "scope": scope,
            "fact_key": key,
        }

        input_type = meta.get("input_type", "text")
        field["input_type"] = input_type
        field["required"] = False

        if meta.get("placeholder"):
            field["placeholder"] = meta["placeholder"]

        allowed = meta.get("allowed_values")
        if allowed:
            values = list(allowed)
            # ── Province-aware subject_group filtering ─────────────────
            if key == "subject_group" and known_facts:
                province = str(known_facts.get("student_province") or "").strip()
                if province:
                    from hailiang_skills.runtime_bridge.pre_flight_gate import _filter_subject_group
                    values = _filter_subject_group(values, province)
            # ── End province-aware filtering ────────────────────────────
            # ── School-stage-aware grade filtering ─────────────────────
            if key == "grade" and known_facts:
                stage = str(known_facts.get("school_stage") or "").strip()
                if stage:
                    from hailiang_skills.runtime_bridge.pre_flight_gate import _filter_grade_by_stage
                    values = _filter_grade_by_stage(values, stage)
            # ── End school-stage-aware filtering ────────────────────────
            field["options"] = [{"label": v, "value": v} for v in values]

        fields.append(field)

    return fields


def resolve_skill_ids(
    target_expert_ids: list[str], policy: dict[str, Any]
) -> list[str]:
    """Given a list of ``target_expert_id`` values from the orchestration plan,
    return the set of skill_ids those experts are authorized to run.
    """
    skill_ids: list[str] = []
    experts = policy.get("experts", {})
    for eid in set(target_expert_ids):
        expert = experts.get(eid) or {}
        skills = expert.get("skills") or []
        skill_ids.extend(str(s) for s in skills)
    return list(set(skill_ids))


# ── Province-scheme subject_group filtering ──────────────────────────────

# 高考改革方案：3+3 / 3+1+2 / traditional
_PROVINCE_SCHEME: dict[str, str] = {
    "北京": "three_three", "天津": "three_three", "上海": "three_three",
    "浙江": "three_three", "山东": "three_three", "海南": "three_three",
}

# 3+3 省份专属：可以不选物理也不选历史的组合
_THREE_THREE_ONLY = frozenset({
    "化学+生物+政治", "化学+生物+历史", "化学+生物+地理",
    "化学+政治+历史", "化学+政治+地理", "化学+历史+地理",
    "生物+政治+历史", "生物+政治+地理", "生物+历史+地理",
    "政治+历史+地理", "物理+历史+地理", "物理+历史+政治",
    "物理+历史+生物", "物理+历史+化学",
})


def _filter_subject_group(all_values: list[str], province: str) -> list[str]:
    """Filter subject_group options based on province's gaokao reform scheme.

    3+3 provinces see all 20 combinations.
    3+1+2 provinces only see combinations that include exactly one of 物理/历史.
    """
    scheme = _PROVINCE_SCHEME.get(province, "three_one_two")
    if scheme == "three_three":
        return list(all_values)
    # 3+1+2: must include exactly one of 物理 or 历史
    return [v for v in all_values if v not in _THREE_THREE_ONLY]


# ── School-stage-aware grade filtering ────────────────────────────────────

# 年级按学段分组：预置信息已知学段时，只展示对应学段的年级选项。
_STAGE_GRADE_MAP: dict[str, list[str]] = {
    "小学": ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级"],
    "初中": ["初一", "初二", "初三"],
    "高中": ["高一", "高二", "高三"],
}


def _filter_grade_by_stage(all_values: list[str], stage: str) -> list[str]:
    """Filter grade options to only those belonging to *stage*.

    When school_stage is known (e.g. "高中"), only show grades for that stage.
    Unknown stages return the full list unchanged.
    """
    filtered = _STAGE_GRADE_MAP.get(stage)
    if filtered is None:
        return list(all_values)
    return [v for v in all_values if v in filtered]