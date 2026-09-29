from __future__ import annotations

from hailiang_skills.core.facts_config import get_fact_schema
from hailiang_skills.core.logging import make_event
from hailiang_skills.llm.service import build_context_snapshot, safe_complete_json
from hailiang_skills.skills.assets import load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.schemas.facts import normalize_fact_value
from hailiang_skills.skills.common import build_prompt_record, collect_known_provinces


class FactsExtractorSkill(BaseSkill):
    skill_name = "facts_extractor"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        admission_assets = load_json("assets/generated/admission/province_score_bands.json", [])
        path_catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
        school_catalog = load_json("assets/generated/school_intro/schools.json", [])
        known_provinces = collect_known_provinces(admission_assets)
        _llm_payload = {
            "user_input": user_input,
            "context": build_context_snapshot(context),
            "facts_schema": {
                key: {
                    "label": meta.get("label", key),
                    "value_type": meta.get("value_type", "string"),
                    "allowed_values": meta.get("allowed_values") or [],
                }
                for key, meta in get_fact_schema().items()
                if meta.get("enabled", True)
            },
            "known_provinces": known_provinces,
            "path_catalog_brief": [
                {
                    "path_id": item.get("path_id"),
                    "primary_category": item.get("primary_category"),
                    "required_fact_keys": item.get("required_fact_keys", []),
                }
                for item in path_catalog
            ],
            "school_catalog_brief": [
                {"school_name": item.get("school_name")}
                for item in school_catalog[:200]
            ],
        }
        llm_result = safe_complete_json(self.llm_client, "facts_extractor", _llm_payload)
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "facts_extractor",
            "facts_extractor",
            _llm_payload,
            llm_response=llm_result,
        )

        result_payload = llm_result if isinstance(llm_result, dict) else {}
        context_updates = result_payload.get("context_updates")
        fact_items = context_updates.get("facts") if isinstance(context_updates, dict) else None
        if not isinstance(fact_items, list):
            fact_items = []
        merged_updates = {
            str(item.get("key")): normalize_fact_value(str(item.get("key")), item.get("value"))
            for item in fact_items
            if isinstance(item, dict) and item.get("key")
        }
        explicit_unknown_fact_keys = []
        confidence = result_payload.get("confidence", 0.0)
        reason = result_payload.get("reason", "抽取用户本轮中可写回 context 的事实")

        return SkillResult(
            assistant_message="",
            state_patch={
                "fact_updates": merged_updates,
                "context_updates": context_updates,
                "unknown_fact_keys": explicit_unknown_fact_keys,
                "confidence": confidence,
                "reason": reason,
            },
            events=[
                make_event(
                    "facts_extracted",
                    {
                        "fact_keys": [key for key, value in merged_updates.items() if value not in (None, "", [])],
                        "unknown_fact_keys": explicit_unknown_fact_keys,
                        "confidence": confidence,
                    },
                )
            ],
        )
