from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from pathlib import Path

import yaml

from hailiang_skills.skill_runtime.models import IntentRouterConfig, SkillBundle
from hailiang_skills.skill_runtime.skill_contract import CONTRACT_FILE_NAME
from hailiang_skills.skill_runtime.skill_loader import load_skill_bundle_from_directory
from hailiang_skills.core.skill_ids import (
    CAREER_PLAN_SKILL_ID,
    GENERAL_CHAT_SKILL_ID,
    LEGACY_MAIN_PLANNER_SKILL_ID,
    canonical_skill_id,
)

_REGISTRY_SKILL_ALIASES = {
    "admission": "mock_admission",
    "convergence": "multi_path_planning",
}


def _registry_skill_id(skill_id: str) -> str:
    canonical = canonical_skill_id(skill_id)
    return _REGISTRY_SKILL_ALIASES.get(canonical, canonical)


@dataclass(slots=True)
class SkillRegistry:
    bundles: dict[str, SkillBundle] = field(default_factory=dict)
    enabled_by_id: dict[str, bool] = field(default_factory=dict)

    def resolve_skill_id(self, skill_id: str) -> str:
        return _registry_skill_id(skill_id)

    def get(self, skill_id: str) -> SkillBundle | None:
        canonical = _registry_skill_id(skill_id)
        return self.bundles.get(canonical) if self.is_enabled(canonical) else None

    def get_raw(self, skill_id: str) -> SkillBundle | None:
        return self.bundles.get(_registry_skill_id(skill_id))

    def is_enabled(self, skill_id: str) -> bool:
        canonical = _registry_skill_id(skill_id)
        if not canonical or canonical in {GENERAL_CHAT_SKILL_ID}:
            return True
        return self.enabled_by_id.get(canonical, True)

    def get_enabled(self, skill_id: str) -> SkillBundle | None:
        return self.get(skill_id)

    def enabled_bundles(self) -> dict[str, SkillBundle]:
        return {
            skill_id: bundle
            for skill_id, bundle in self.bundles.items()
            if skill_id != LEGACY_MAIN_PLANNER_SKILL_ID and self.is_enabled(skill_id)
        }

    def route_targets(self) -> dict[str, str]:
        return {
            bundle.contract.skill_id: bundle.contract.skill_role
            for skill_id, bundle in self.bundles.items()
            if skill_id != LEGACY_MAIN_PLANNER_SKILL_ID and self.is_enabled(skill_id)
        }


def load_local_skill_registry(
    skills_root: str | Path,
    *,
    enabled_by_id: dict[str, bool] | None = None,
) -> SkillRegistry:
    root = Path(skills_root).expanduser().resolve()
    if not root.is_dir():
        return SkillRegistry(enabled_by_id=dict(enabled_by_id or {}))
    bundles: dict[str, SkillBundle] = {}
    for child in sorted(path for path in root.iterdir() if path.is_dir()):
        if not any(
            path.is_file() and path.name in {"skill.md", "SKILL.md"}
            for path in child.iterdir()
        ):
            continue
        if not (child / CONTRACT_FILE_NAME).is_file():
            continue
        bundle = load_skill_bundle_from_directory(child)
        bundles[bundle.contract.skill_id] = bundle
    _apply_project_intent_router_config(root, bundles)
    # Keep old snapshot/tests readable without exposing the legacy ID in the
    # user-facing registry/catalog.  ``get`` also normalizes this alias.
    career_bundle = bundles.get(CAREER_PLAN_SKILL_ID) or bundles.get(LEGACY_MAIN_PLANNER_SKILL_ID)
    if career_bundle is not None:
        bundles.setdefault(CAREER_PLAN_SKILL_ID, career_bundle)
        bundles.setdefault(LEGACY_MAIN_PLANNER_SKILL_ID, career_bundle)
    normalized_enabled = dict(enabled_by_id or {})
    unknown = sorted(set(normalized_enabled) - set(bundles) - {GENERAL_CHAT_SKILL_ID})
    if unknown:
        logging.getLogger("hailiang.skill_registry").warning(
            "skill_management contains unknown Skill IDs: %s",
            ", ".join(unknown),
        )
    return SkillRegistry(bundles=bundles, enabled_by_id=normalized_enabled)


def _apply_project_intent_router_config(root: Path, bundles: dict[str, SkillBundle]) -> None:
    config = _load_project_intent_router_config(root)
    if config is None:
        return
    bundle = bundles.get(CAREER_PLAN_SKILL_ID) or bundles.get(LEGACY_MAIN_PLANNER_SKILL_ID)
    if bundle is None:
        return
    metadata = bundle.runtime_metadata
    bundle.runtime_metadata = replace(
        metadata,
        planner=replace(metadata.planner, intent_router=config),
        raw={
            **metadata.raw,
            "planner": {
                **(metadata.raw.get("planner", {}) if isinstance(metadata.raw.get("planner"), dict) else {}),
                "intent_router": _intent_router_config_to_dict(config),
            },
        },
    )


def _load_project_intent_router_config(root: Path) -> IntentRouterConfig | None:
    path = root.parent / "config" / "intent_router.yml"
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logging.getLogger("hailiang.skill_registry").warning(
            "failed to load intent router config %s: %s",
            path.as_posix(),
            exc,
        )
        return None
    payload = raw.get("intent_router") if isinstance(raw, dict) else {}
    if not isinstance(payload, dict):
        logging.getLogger("hailiang.skill_registry").warning(
            "ignored invalid intent router config %s: missing intent_router object",
            path.as_posix(),
        )
        return None
    return _build_intent_router_config(payload)


def _build_intent_router_config(payload: dict) -> IntentRouterConfig:
    route_suggestions = payload.get("route_suggestions")
    route_suggestions = route_suggestions if isinstance(route_suggestions, dict) else {}
    return IntentRouterConfig(
        unclear_intent_patterns=_string_tuple(payload.get("unclear_intent_patterns")),
        enable_embedding=bool(payload.get("enable_embedding", True)),
        embedding_model=str(payload.get("embedding_model") or "text-embedding-v4").strip() or "text-embedding-v4",
        embedding_timeout_s=max(int(payload.get("embedding_timeout_s", 8) or 8), 1),
        direct_threshold=float(payload.get("direct_threshold", 0.72) or 0.72),
        ambiguity_margin=float(payload.get("ambiguity_margin", 0.08) or 0.08),
        # The nested route setting is canonical; the old top-level key is a
        # backwards-compatible fallback for older project configurations.
        general_chat_choice_threshold=_confidence(
            route_suggestions.get("card_threshold", payload.get("general_chat_choice_threshold", 0.72)),
            0.72,
        ),
        general_chat_choice_max_candidates=max(
            int(payload.get("general_chat_choice_max_candidates", 3) or 3),
            1,
        ),
        route_suggestion_min_confidence=_confidence(route_suggestions.get("min_confidence", 0.72), 0.72),
        route_suggestion_min_confidence_without_context=_confidence(
            route_suggestions.get("min_confidence_without_context", 0.72), 0.72
        ),
        route_suggestion_card_threshold=_confidence(route_suggestions.get("card_threshold", 0.72), 0.72),
        specialist_switch_threshold=_confidence(
            route_suggestions.get("specialist_switch_threshold", 0.92), 0.92
        ),
        enable_llm_fallback=bool(payload.get("enable_llm_fallback", False)),
        long_profile_message_min_chars=max(
            int(payload.get("long_profile_message_min_chars", 80) or 80),
            20,
        ),
        long_profile_signal_threshold=max(
            int(payload.get("long_profile_signal_threshold", 3) or 3),
            1,
        ),
    )


def _intent_router_config_to_dict(config: IntentRouterConfig) -> dict[str, object]:
    return {
        "unclear_intent_patterns": list(config.unclear_intent_patterns),
        "enable_embedding": config.enable_embedding,
        "embedding_model": config.embedding_model,
        "embedding_timeout_s": config.embedding_timeout_s,
        "direct_threshold": config.direct_threshold,
        "ambiguity_margin": config.ambiguity_margin,
        "general_chat_choice_threshold": config.general_chat_choice_threshold,
        "general_chat_choice_max_candidates": config.general_chat_choice_max_candidates,
        "route_suggestions": {
            "min_confidence": config.route_suggestion_min_confidence,
            "min_confidence_without_context": config.route_suggestion_min_confidence_without_context,
            "card_threshold": config.route_suggestion_card_threshold,
            "specialist_switch_threshold": config.specialist_switch_threshold,
        },
        "enable_llm_fallback": config.enable_llm_fallback,
        "long_profile_message_min_chars": config.long_profile_message_min_chars,
        "long_profile_signal_threshold": config.long_profile_signal_threshold,
    }


def _confidence(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 0.0), 1.0)


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        normalized = value.strip()
        return (normalized,) if normalized else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()
