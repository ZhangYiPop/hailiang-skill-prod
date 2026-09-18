"""Compact, non-repetitive fact projections for model prompts.

The runtime intentionally keeps several durable views of a conversation
(effective facts, Skill state, rolling memory and progress).  They are useful
for recovery, but passing every copy to a model encourages it to narrate the
same fact back to the user.  This module only changes the prompt projection;
it never mutates durable state or routing inputs.
"""

from __future__ import annotations

import json
import re
from typing import Any


_PRIVATE_SKILL_FACT_PREFIX = "_"
_PROGRESS_FACT_KEYS = {"confirmed_facts"}
_RECAPPING_PREFIX = re.compile(
    r"(?:^\s*(?:好的[，,。！!\s]*)?(?:已(?:经)?了解|我(?:已)?了解|收到|确认(?:了)?|"
    r"根据您(?:刚才)?(?:说|提供|描述)|您(?:刚才)?提到|孩子(?:目前)?是)|"
    r"(?:根据|基于|结合)(?:您|你)(?:目前|已)?提供的信息)",
)


def build_effective_fact_ledger(
    *,
    global_facts: dict[str, Any] | None,
    current_skill_facts: dict[str, Any] | None,
    memory_facts: dict[str, Any] | None,
    skill_progress: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a canonical prompt ledger plus compact, safe diagnostics."""
    global_values = _public_mapping(global_facts)
    # Effective facts are the authoritative business ledger.  Keep a Skill
    # value only when it contributes something the effective ledger does not
    # already say; a conflicting value is retained for the model to resolve.
    current_values, pruned_current = _prune_duplicates(
        _public_mapping(current_skill_facts),
        _flatten_leaf_values(global_values),
    )
    progress = _public_mapping(skill_progress, excluded_keys=_PROGRESS_FACT_KEYS)
    canonical = _flatten_leaf_values({"global": global_values, "current_skill": current_values})
    memory_only, pruned = _prune_duplicates(_public_mapping(memory_facts), canonical)
    ledger = {
        "effective_facts": global_values,
        "current_skill_facts": current_values,
        "memory_only_facts": memory_only,
        "skill_progress": progress,
    }
    dedupe_sources: list[str] = []
    if pruned_current:
        dedupe_sources.append("current_skill_facts")
    if pruned:
        dedupe_sources.append("memory_facts")
    diagnostics = {
        "effective_fact_count": len(_flatten_leaf_values({"global": global_values, "current_skill": current_values})),
        "memory_only_fact_count": len(_flatten_leaf_values(memory_only)),
        "deduplicated_current_skill_fact_count": pruned_current,
        "deduplicated_memory_fact_count": pruned,
        "dedupe_sources": dedupe_sources,
    }
    return ledger, diagnostics


def visible_fact_recap_risk(reply: str, ledger: dict[str, Any]) -> dict[str, Any]:
    """Detect, but never rewrite, likely mechanical fact-recap openings."""
    text = str(reply or "").strip()
    if not text or not _RECAPPING_PREFIX.search(text):
        return {"detected": False, "matched_fact_keys": []}
    head = text[:180]
    matched: list[str] = []
    for key, value in _flatten_leaf_values(ledger).items():
        rendered = str(value).strip()
        if len(rendered) >= 2 and rendered in head:
            matched.append(key)
    return {"detected": bool(matched), "matched_fact_keys": matched[:6]}


def response_style_instruction() -> str:
    """Shared final-answer policy, deliberately independent of business Skills."""
    return (
        "【事实呈现规则（平台最高优先级）】已确认事实仅供内部推理，默认不要在面向用户的正文中重念、"
        "确认或用作过渡开场。直接回答、分析或提出下一步。禁止无新增价值的“已了解/根据您刚才说/"
        "确认孩子是……”式前缀。仅当用户主动要求回顾、当前信息存在冲突或时效不明需要核对、"
        "或解释结论确实需要引用不超过两项直接依据时，才提及事实。用户本轮新提供的信息可用一句"
        "承接，但该句必须带来新的判断、行动或必要的最小追问，不能只是改写用户原话。"
    )


def _public_mapping(value: dict[str, Any] | None, *, excluded_keys: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded_keys or set()
    if not isinstance(value, dict):
        return {}
    return {
        str(key): child
        for key, child in value.items()
        if not str(key).startswith(_PRIVATE_SKILL_FACT_PREFIX) and str(key) not in excluded
    }


def _flatten_leaf_values(value: Any, path: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {".".join(path): value} if path and value not in (None, "", [], {}) else {}
    flattened: dict[str, Any] = {}
    for key, child in value.items():
        flattened.update(_flatten_leaf_values(child, (*path, str(key))))
    return flattened


def _prune_duplicates(value: Any, canonical: dict[str, Any], path: tuple[str, ...] = ()) -> tuple[Any, int]:
    if not isinstance(value, dict):
        leaf_key = path[-1] if path else ""
        duplicate = any(key.rsplit(".", 1)[-1] == leaf_key and _same_value(item, value) for key, item in canonical.items())
        return (None, 1) if duplicate else (value, 0)
    kept: dict[str, Any] = {}
    pruned = 0
    for key, child in value.items():
        result, count = _prune_duplicates(child, canonical, (*path, str(key)))
        pruned += count
        # False and 0 are valid business facts (for example whether the user
        # has confirmed an option).  Do not accidentally lose them while
        # removing a duplicate branch.
        if result is not None and result != {}:
            kept[str(key)] = result
    return kept, pruned


def _same_value(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return left == right
