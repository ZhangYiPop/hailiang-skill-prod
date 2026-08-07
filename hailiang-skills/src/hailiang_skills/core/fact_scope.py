from __future__ import annotations

from hailiang_skills.core.facts_config import get_fact_meta


FACT_SCOPE_SHARED = "shared"
FACT_SCOPE_PROFILE = "profile"
FACT_SCOPE_SESSION = "session"


def resolve_fact_scope(fact_key: str, override: str | None = None) -> str:
    legacy_scope_aliases = {
        "user": FACT_SCOPE_SHARED,
    }
    normalized_override = legacy_scope_aliases.get(override or "", override)
    if normalized_override in {FACT_SCOPE_SHARED, FACT_SCOPE_PROFILE, FACT_SCOPE_SESSION}:
        return normalized_override
    if override in {FACT_SCOPE_SHARED, FACT_SCOPE_PROFILE, FACT_SCOPE_SESSION}:
        return override
    meta = get_fact_meta(fact_key)
    scope_policy = legacy_scope_aliases.get(meta.get("scope_policy"), meta.get("scope_policy"))
    if scope_policy in {FACT_SCOPE_SHARED, FACT_SCOPE_PROFILE, FACT_SCOPE_SESSION}:
        return scope_policy
    return FACT_SCOPE_PROFILE
