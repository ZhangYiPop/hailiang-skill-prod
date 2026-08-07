from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hailiang_skills.skill_runtime.models import GeneratedAssetDomain, SessionState, SkillBundle

FALLBACK_MESSAGE = (
    "这部分相关信息还在沉淀整理中，后续版本会更新；"
    "如果你愿意，我先基于已知画像给你一个方向性建议。"
)

DEEP_DIVE_KEYWORDS = (
    "具体",
    "详细",
    "展开",
    "深挖",
    "怎么报",
    "怎么走",
    "流程",
    "规则",
    "政策",
    "分数线",
    "院校",
    "学校",
    "路径",
    "路线",
    "方案",
)

ADMISSION_KEYWORDS = (
    "分流",
    "分数线",
    "特控线",
    "一段线",
    "二段线",
    "梯队",
    "985",
    "211",
    "双一流",
    "本科",
    "专科",
)

MULTIROUTE_KEYWORDS = (
    "路径",
    "路线",
    "综评",
    "强基",
    "少年班",
    "国际",
    "艺体",
    "特长",
    "招飞",
    "专项",
    "3+2",
    "单招",
    "职教",
)


@dataclass(slots=True)
class AssetLookupResult:
    available_domains: tuple[str, ...]
    candidate_domains: tuple[str, ...] = ()
    matched_assets: list[str] = field(default_factory=list)
    unsupported_reason: str = ""
    fallback_required: bool = False
    web_search_allowed: bool = False
    recommended_tool: str = ""
    tool_calling_allowed: bool = True


def lookup_assets(bundle: SkillBundle, state: SessionState) -> AssetLookupResult:
    latest_user_message = next((item.content for item in reversed(state.messages) if item.role == "user"), "")
    available_domains = _selected_generated_domains(bundle)
    registry_domains = _registry_domains(bundle)
    candidate_domains: set[str] = {
        domain_name
        for domain_name, config in registry_domains.items()
        if _message_targets_domain(latest_user_message, config)
    }
    if not latest_user_message:
        return AssetLookupResult(available_domains=available_domains)

    matches: list[str] = []

    local_asset_matches = _lookup_local_assets(bundle, latest_user_message)
    if local_asset_matches:
        matches.extend(local_asset_matches)

    school_match = _lookup_school_intro(_asset_domain(bundle, "school_intro"), latest_user_message)
    if school_match == "__missing__":
        candidate_domains.add("school_intro")
        return _build_unsupported_result(
            bundle,
            available_domains=available_domains,
            candidate_domains=candidate_domains,
            reason="学校介绍资产中暂无该学校的有效详情。",
        )
    if school_match:
        candidate_domains.add("school_intro")
        matches.append(school_match)

    admission_match = _lookup_admission(_asset_domain(bundle, "admission"), latest_user_message)
    if admission_match:
        candidate_domains.add("admission")
        matches.append(admission_match)

    multiroute_match = _lookup_multiroute(_asset_domain(bundle, "multiroute"), latest_user_message)
    if multiroute_match:
        candidate_domains.add("multiroute")
        matches.append(multiroute_match)

    if matches:
        return AssetLookupResult(
            available_domains=available_domains,
            candidate_domains=tuple(sorted(candidate_domains)),
            matched_assets=matches,
            recommended_tool="rag",
        )

    requires_asset = bool(candidate_domains) and any(
        _domain_requires_asset(registry_domains.get(domain_name))
        for domain_name in candidate_domains
    )
    if _looks_like_deep_dive_request(bundle, latest_user_message):
        return _build_unsupported_result(
            bundle,
            available_domains=available_domains,
            candidate_domains=candidate_domains,
            reason=(
                "当前问题需要具体资产支撑，但本地资产库中未命中对应内容。"
                if not candidate_domains
                else _describe_candidate_domain_miss(candidate_domains, registry_domains)
            ),
        )
    if requires_asset:
        return _build_unsupported_result(
            bundle,
            available_domains=available_domains,
            candidate_domains=candidate_domains,
            reason=_describe_candidate_domain_miss(candidate_domains, registry_domains),
        )

    return AssetLookupResult(available_domains=available_domains, recommended_tool="")


def _build_unsupported_result(
    bundle: SkillBundle,
    *,
    available_domains: tuple[str, ...],
    candidate_domains: set[str],
    reason: str,
) -> AssetLookupResult:
    candidate_tuple = tuple(sorted(candidate_domains))
    return AssetLookupResult(
        available_domains=available_domains,
        candidate_domains=candidate_tuple,
        unsupported_reason=reason,
        fallback_required=True,
        web_search_allowed=_should_allow_web_search(bundle, candidate_tuple),
        recommended_tool="web_search" if _should_allow_web_search(bundle, candidate_tuple) else "",
        tool_calling_allowed=True,
    )


def _selected_generated_domains(bundle: SkillBundle) -> tuple[str, ...]:
    declared = bundle.runtime_metadata.assets.generated_domains
    if declared:
        return tuple(name for name in declared if name in bundle.asset_domains)
    return tuple(sorted(bundle.asset_domains))


def _asset_domain(bundle: SkillBundle, name: str) -> GeneratedAssetDomain | None:
    if name not in _selected_generated_domains(bundle):
        return None
    return bundle.asset_domains.get(name)


def _lookup_local_assets(bundle: SkillBundle, message: str) -> list[str]:
    if not bundle.runtime_metadata.assets.local_enabled or not bundle.local_assets:
        return []
    lowered = message.lower()
    matches: list[str] = []
    for path, content in bundle.local_assets.items():
        path_tokens = [part.lower() for part in path.replace("/", " ").replace("_", " ").split()]
        if any(token and token in lowered for token in path_tokens):
            matches.append(f"[local_asset] {path}\n{content[:800]}")
    return matches[:2]


def _lookup_school_intro(domain: GeneratedAssetDomain | None, message: str) -> str | None:
    if domain is None:
        return None
    schools = _domain_payload_by_stem(domain).get("schools")
    if not isinstance(schools, list):
        return None
    lowered = message.lower()
    for item in schools:
        if not isinstance(item, dict):
            continue
        school_name = str(item.get("school_name") or "").strip()
        if school_name and school_name.lower() in lowered:
            intro = str(item.get("school_intro") or "").strip()
            if not intro or intro == "暂无收录":
                return "__missing__"
            school_url = str(item.get("school_url") or "").strip()
            return (
                f"[school_intro] 学校: {school_name}\n"
                f"简介: {intro}\n"
                f"链接: {school_url or '(none)'}"
            )
    return None


def _lookup_admission(domain: GeneratedAssetDomain | None, message: str) -> str | None:
    if domain is None:
        return None
    payloads = _domain_payload_by_stem(domain)
    province_match = _find_matching_province(payloads.get("province_flow_map"), message)
    tier_match = _find_matching_tier(payloads.get("tier_copywriting"), message)
    if province_match is None and tier_match is None and not _contains_any(message, ADMISSION_KEYWORDS):
        return None

    parts: list[str] = ["[admission]"]
    if province_match is not None:
        province_name, province_payload = province_match
        parts.append(
            f"省份流转: {province_name} -> {province_payload.get('flow_type', '')} / {province_payload.get('notes', '')}"
        )
    if tier_match is not None:
        parts.append(f"院校层级: {tier_match['tier_name']}\n说明: {tier_match['intro']}")
    return "\n".join(parts)


def _lookup_multiroute(domain: GeneratedAssetDomain | None, message: str) -> str | None:
    if domain is None:
        return None
    payloads = _domain_payload_by_stem(domain)
    path_catalog = payloads.get("path_catalog")
    if not isinstance(path_catalog, list):
        return None

    lowered = message.lower()
    for item in path_catalog:
        if not isinstance(item, dict):
            continue
        candidates = [
            str(item.get("primary_category") or ""),
            str(item.get("sheet_group") or ""),
            str(item.get("description") or ""),
            str(item.get("target_users") or ""),
        ]
        if any(candidate and candidate.lower() in lowered for candidate in candidates):
            return _format_multiroute_item(item)

    if _contains_any(message, MULTIROUTE_KEYWORDS):
        for item in path_catalog[:3]:
            if isinstance(item, dict):
                return _format_multiroute_item(item)
    return None


def _format_multiroute_item(item: dict[str, Any]) -> str:
    return (
        "[multiroute]\n"
        f"路径: {item.get('primary_category', '')}\n"
        f"说明: {item.get('description', '')}\n"
        f"适用对象: {item.get('target_users', '')}\n"
        f"流程: {item.get('process_flow', '')}"
    )


def _find_matching_province(payload: Any, message: str) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    for province_payload in payload.values():
        if not isinstance(province_payload, dict):
            continue
        provinces = province_payload.get("provinces")
        if not isinstance(provinces, list):
            continue
        for province in provinces:
            if isinstance(province, str) and province in message:
                return province, province_payload
    return None


def _find_matching_tier(payload: Any, message: str) -> dict[str, Any] | None:
    if not isinstance(payload, list):
        return None
    for item in payload:
        if not isinstance(item, dict):
            continue
        tier_name = str(item.get("tier_name") or "").strip()
        if tier_name and tier_name in message:
            return item
    return None


def _domain_payload_by_stem(domain: GeneratedAssetDomain) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for file in domain.files:
        stem = file.file_name[:-5] if file.file_name.endswith(".json") else file.file_name
        result[stem] = file.payload
    return result


def _contains_any(message: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in message for keyword in keywords)


def _looks_like_deep_dive_request(bundle: SkillBundle, message: str) -> bool:
    registry_keywords = _registry_deep_dive_keywords(bundle)
    return _contains_any(message, tuple(registry_keywords)) if registry_keywords else _contains_any(message, DEEP_DIVE_KEYWORDS)


def _registry_domains(bundle: SkillBundle) -> dict[str, dict[str, Any]]:
    domains = bundle.asset_registry.get("domains") if isinstance(bundle.asset_registry, dict) else None
    if not isinstance(domains, dict):
        return {}
    return {str(key): value for key, value in domains.items() if isinstance(value, dict)}


def _global_policy(bundle: SkillBundle) -> dict[str, Any]:
    policy = bundle.asset_registry.get("global_policy") if isinstance(bundle.asset_registry, dict) else None
    return policy if isinstance(policy, dict) else {}


def _registry_deep_dive_keywords(bundle: SkillBundle) -> list[str]:
    keywords = _global_policy(bundle).get("concrete_request_keywords")
    if not isinstance(keywords, list):
        return list(DEEP_DIVE_KEYWORDS)
    return [str(item).strip() for item in keywords if str(item).strip()]


def _message_targets_domain(message: str, config: dict[str, Any]) -> bool:
    keywords = _config_keywords(config)
    if not keywords:
        return False
    lowered = message.lower()
    return any(keyword and keyword.lower() in lowered for keyword in keywords)


def _config_keywords(config: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    for field_name in ("intent_keywords", "supports"):
        field_value = config.get(field_name)
        if isinstance(field_value, list):
            keywords.extend(str(item).strip() for item in field_value if str(item).strip())
    return keywords


def _domain_requires_asset(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return False
    return bool(config.get("disallow_freeform"))


def _should_allow_web_search(bundle: SkillBundle, candidate_domains: tuple[str, ...]) -> bool:
    registry_domains = _registry_domains(bundle)
    if candidate_domains:
        return any(
            bool(registry_domains.get(domain_name, {}).get("fallback_to_web_search"))
            for domain_name in candidate_domains
        )
    return bool(_global_policy(bundle).get("fallback_to_web_search_for_unknown_concrete_requests"))


def _describe_candidate_domain_miss(
    candidate_domains: set[str],
    registry_domains: dict[str, dict[str, Any]],
) -> str:
    if not candidate_domains:
        return "当前问题需要具体资产支撑，但本地资产库中未命中对应内容。"

    labels: list[str] = []
    for domain_name in sorted(candidate_domains):
        config = registry_domains.get(domain_name, {})
        desc = str(config.get("desc") or "").strip()
        labels.append(f"{domain_name}({desc})" if desc else domain_name)
    joined = "、".join(labels)
    return f"当前问题命中了以下资产边界，但本地资产库中未命中对应内容: {joined}。"
