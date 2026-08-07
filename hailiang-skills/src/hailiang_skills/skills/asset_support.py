from __future__ import annotations

from typing import Any

from hailiang_skills.core.facts_config import get_fact_label
from hailiang_skills.skills.assets import feature_flag_enabled, load_json


ENABLE_SCORE_BAND_EXPOSURE_RULES = "HAILIANG_ENABLE_SCORE_BAND_EXPOSURE_RULES"


ASSET_REGISTRY = {
    "common_summary": [
        {
            "path": "assets/generated/multiroute/path_catalog.json",
            "manifest_path": "assets/generated/multiroute/asset_manifest.json",
            "count_key": "path_catalog",
            "title": "多元路径目录",
            "supports": ["路径介绍", "路径特色", "规则文本", "适用对象", "流程说明"],
        },
        {
            "path": "assets/generated/multiroute/path_reason_templates.json",
            "manifest_path": "assets/generated/multiroute/asset_manifest.json",
            "count_key": "reason_templates",
            "title": "路径理由模板",
            "supports": ["推荐依据", "风险提示"],
        },
        {
            "path": "assets/generated/multiroute/action_timeline_templates.json",
            "manifest_path": "assets/generated/multiroute/asset_manifest.json",
            "count_key": "action_timeline",
            "title": "行动时间线",
            "supports": ["阶段时间线", "下一步行动建议"],
        },
    ],
    "admission": [
        {
            "path": "assets/generated/admission/province_score_bands.json",
            "manifest_path": "assets/generated/admission/asset_manifest.json",
            "count_key": "province_score_bands",
            "title": "模拟升学分数档",
            "supports": ["省份匹配", "选科匹配", "分数档命中", "代表院校", "推荐路径"],
        },
        {
            "path": "assets/generated/admission/tier_copywriting.json",
            "manifest_path": "assets/generated/admission/asset_manifest.json",
            "count_key": "tier_copywriting",
            "title": "学校层次说明",
            "supports": ["层次解释"],
        },
        {
            "path": "assets/generated/admission/province_flow_map.json",
            "manifest_path": "assets/generated/admission/asset_manifest.json",
            "count_key": "flow_map",
            "title": "省份流程映射",
            "supports": ["省份流程映射"],
        },
    ],
    "convergence": [
        {
            "path": "assets/generated/multiroute/province_score_lines.json",
            "manifest_path": "assets/generated/multiroute/asset_manifest.json",
            "count_key": "province_score_lines",
            "title": "省份分数线",
            "supports": ["分数段换算", "省份线差判断"],
        },
        {
            "path": "assets/generated/multiroute/score_band_exposure_rules.json",
            "manifest_path": "assets/generated/multiroute/asset_manifest.json",
            "count_key": "score_band_rules",
            "title": "分段露出规则",
            "supports": ["分数段露出控制"],
            "enabled_by_flag": ENABLE_SCORE_BAND_EXPOSURE_RULES,
            "default_enabled": False,
        },
    ],
    "school_intro": [
        {
            "path": "assets/generated/school_intro/schools.json",
            "manifest_path": "assets/generated/school_intro/asset_manifest.json",
            "count_key": "schools",
            "title": "学校介绍目录",
            "supports": ["学校名称", "学校简介", "学校链接"],
        }
    ],
}


def format_fact_label(slot: str) -> str:
    return get_fact_label(slot)


def format_missing_slot_prompts(missing_slots: list[str]) -> list[str]:
    return [f"下一步优先补充：{format_fact_label(slot)}" for slot in missing_slots[:3]]


def _manifest_count(manifest_path: str, count_key: str) -> int | None:
    manifest = load_json(manifest_path, {})
    return (manifest.get("counts") or {}).get(count_key)


def _build_registry_entries(skill_name: str) -> list[dict[str, Any]]:
    entries = [*ASSET_REGISTRY.get("common_summary", [])]
    entries.extend(ASSET_REGISTRY.get(skill_name, []))
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        flag_name = entry.get("enabled_by_flag")
        enabled = True
        if flag_name:
            enabled = feature_flag_enabled(flag_name, default=entry.get("default_enabled", False))
        normalized.append(
            {
                **entry,
                "count": _manifest_count(entry["manifest_path"], entry["count_key"]),
                "enabled": enabled,
            }
        )
    return normalized


def build_asset_support(
    skill_name: str,
    *,
    candidate_paths: list[dict] | None = None,
    matched_items_brief: list[dict] | None = None,
    recommended_path_timelines: list[dict] | None = None,
    targets: list[dict] | None = None,
) -> dict[str, Any]:
    registry_entries = _build_registry_entries(skill_name)
    available_assets = [
        {
            "path": entry["path"],
            "title": entry["title"],
            "count": entry.get("count"),
            "supports": entry.get("supports", []),
            "enabled": entry.get("enabled", True),
        }
        for entry in registry_entries
    ]
    supported_dimensions = sorted(
        {
            dimension
            for entry in registry_entries
            if entry.get("enabled", True)
            for dimension in entry.get("supports", [])
        }
    )

    dynamic_supported: list[str] = []
    dynamic_unavailable: list[str] = []
    candidate_paths = candidate_paths or []
    matched_items_brief = matched_items_brief or []
    recommended_path_timelines = recommended_path_timelines or []
    targets = targets or []

    if skill_name in {"convergence", "path_drilldown", "terminate_or_recommend"}:
        if any(item.get("rule_variants") or item.get("rule_text_raw") for item in candidate_paths):
            dynamic_supported.append("路径规则判断")
        else:
            dynamic_unavailable.append("路径规则判断")
        if any(item.get("action_timeline") for item in candidate_paths):
            dynamic_supported.append("行动建议")
        else:
            dynamic_unavailable.append("行动建议")
        if any(item.get("risk_hint") for item in candidate_paths):
            dynamic_supported.append("风险提示")
        else:
            dynamic_unavailable.append("风险提示")

    if skill_name == "admission":
        if matched_items_brief:
            dynamic_supported.append("命中院校/推荐路径")
        else:
            dynamic_unavailable.append("命中院校/推荐路径")
        if recommended_path_timelines and any(item.get("timeline") for item in recommended_path_timelines):
            dynamic_supported.append("推荐路径时间线")
        else:
            dynamic_unavailable.append("推荐路径时间线")

    if skill_name == "school_intro":
        if any(item.get("school_intro") for item in targets):
            dynamic_supported.append("学校简介")
        else:
            dynamic_unavailable.append("学校简介")
        if any(item.get("school_url") for item in targets):
            dynamic_supported.append("学校链接")
        else:
            dynamic_unavailable.append("学校链接")

    if dynamic_supported:
        supported_dimensions = sorted(set([*supported_dimensions, *dynamic_supported]))

    return {
        "available_assets": available_assets,
        "supported_dimensions": supported_dimensions,
        "dynamic_supported_dimensions": dynamic_supported,
        "dynamic_unavailable_dimensions": sorted(set(dynamic_unavailable)),
        "policy": {
            "must_ground_on_assets": True,
            "fallback_message": "该维度的信息正在整理中，后续版本会提供详细的分析。",
            "base_rule_first_missing_facts": skill_name in {"convergence", "path_drilldown"},
        },
    }
