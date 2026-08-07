from __future__ import annotations

import json
import inspect
import re

from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient
from hailiang_skills.skill_runtime.models import (
    ChatMessage,
    RoutingDecision,
    SessionState,
    SkillBundle,
    ToolRoutingCandidate,
)
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger, preview_text


ALLOWED_TOOL_ROUTING_NAMES = frozenset(
    {"web_search", "rag", "subject_requirements", "status_track", "mcp"}
)
ALLOWED_TOOL_ROUTING_KINDS = frozenset({"tool", "script"})


def classify_tool_routing(
    bundle: SkillBundle,
    state: SessionState,
    client: OpenAICompatibleChatClient,
    *,
    logger: RuntimeLogger | None = None,
) -> RoutingDecision:
    latest_user_message = next((item.content for item in reversed(state.messages) if item.role == "user"), "").strip()
    if not latest_user_message:
        return RoutingDecision()

    messages = [
        ChatMessage(role="system", content=build_routing_classifier_prompt(bundle, state)),
        ChatMessage(role="user", content=latest_user_message),
    ]
    try:
        complete_kwargs = {"logger": logger}
        try:
            if "request_purpose" in inspect.signature(client.complete).parameters:
                complete_kwargs["request_purpose"] = "tool_routing_classifier"
        except (TypeError, ValueError):
            pass
        raw_reply = client.complete(messages, **complete_kwargs)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.log("routing.classifier.failed", error=str(exc))
        return RoutingDecision()
    if logger:
        logger.log("routing.classifier.raw_reply", reply_preview=preview_text(raw_reply, limit=500))
    decision = parse_routing_decision(raw_reply)
    if logger:
        logger.log(
            "routing.classifier.decision",
            allow_web_search=decision.allow_web_search,
            candidate_domains=list(decision.candidate_domains),
            query_focus=decision.query_focus,
            reason=decision.reason,
        )
    return RoutingDecision(
        allow_web_search=decision.allow_web_search,
        candidate_domains=decision.candidate_domains,
        reason=decision.reason,
        query_focus=decision.query_focus,
        required=decision.allow_web_search,
        candidates=(
            ToolRoutingCandidate(
                kind="tool",
                name="web_search",
                intent_label="查询实时资料",
                reason=decision.reason,
            ),
        )
        if decision.allow_web_search
        else (),
        source="standalone_classifier",
    )


def build_routing_classifier_prompt(bundle: SkillBundle, state: SessionState) -> str:
    domain_lines: list[str] = []
    registry_domains = bundle.asset_registry.get("domains", {}) if isinstance(bundle.asset_registry, dict) else {}
    if isinstance(registry_domains, dict):
        for domain_name, config in sorted(registry_domains.items()):
            if not isinstance(config, dict):
                continue
            desc = str(config.get("desc") or "").strip() or "(none)"
            supports = ", ".join(str(item) for item in config.get("supports", []) if str(item).strip()) or "(none)"
            fallback = bool(config.get("fallback_to_web_search"))
            domain_lines.append(
                f"- {domain_name}: desc={desc}; supports={supports}; fallback_to_web_search={fallback}"
            )
    history_lines = [
        f"{message.role}: {' '.join(message.content.split())}"
        for message in state.messages[-4:]
    ]
    history_text = "\n".join(history_lines) or "(empty)"
    domains_text = "\n".join(domain_lines) or "(none)"
    skill_name = str(bundle.metadata.get("name") or bundle.root_name or "skill").strip()
    skill_excerpt = " ".join(bundle.skill_markdown.split())[:1200] or "(none)"
    return (
        "You are a lightweight routing classifier inside a local skill runtime.\n"
        "Your job is only to decide whether the latest user message should unlock web_search for this turn.\n"
        "This classifier is skill-aware: use the skill instructions and asset registry as the primary policy source.\n"
        "web_search here means the configured business search API, not the generic mcp bridge tool.\n"
        "Do not make decisions about the mcp tool; mcp is separately controlled by runtime white lists.\n"
        "Be conservative but not literal.\n"
        "You MAY allow web_search when the user is asking for factual lookup, directory/listing, school roster, official policy lookup, or explicitly asks to search online.\n"
        "You MUST keep web_search disabled for normal profile collection, general planning advice, emotional support, or cases already well covered by matched local assets.\n"
        "Treat requests like school lists, school directories, district school names, and institution rosters as eligible factual lookup even if they do not literally contain the word school.\n"
        "Output JSON only in the form: "
        '{"allow_web_search":true|false,"candidate_domains":["school_intro"],"query_focus":"...","reason":"..."}'
        "\n\n"
        f"# Skill Name\n{skill_name}\n\n"
        f"# Skill Excerpt\n{skill_excerpt}\n\n"
        f"# Asset Domains\n{domains_text}\n\n"
        f"# Recent History\n{history_text}\n"
    )


def build_ms_agent_tool_routing_context(bundle: SkillBundle, state: SessionState) -> str:
    """Build a secret-free routing policy/catalog for the combined planner."""
    domain_lines: list[str] = []
    registry_domains = bundle.asset_registry.get("domains", {}) if isinstance(bundle.asset_registry, dict) else {}
    if isinstance(registry_domains, dict):
        for domain_name, config in sorted(registry_domains.items()):
            if not isinstance(config, dict):
                continue
            desc = str(config.get("desc") or "").strip() or "(none)"
            supports = ", ".join(str(item) for item in config.get("supports", []) if str(item).strip()) or "(none)"
            fallback = bool(config.get("fallback_to_web_search"))
            domain_lines.append(
                f"- {domain_name}: desc={desc}; supports={supports}; fallback_to_web_search={fallback}"
            )

    capability_lines = [
        f"- {item.name}: enabled={bool(item.enabled)}; description={item.description}"
        for item in bundle.tool_registry.capabilities
    ]
    mcp_lines: list[str] = []
    tool_settings = bundle.tool_registry.settings.get("tools", {}) if isinstance(bundle.tool_registry.settings, dict) else {}
    mcp_config = tool_settings.get("mcp", {}) if isinstance(tool_settings, dict) else {}
    servers = mcp_config.get("servers", []) if isinstance(mcp_config, dict) else []
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, dict) or not bool(server.get("enabled", False)):
                continue
            allowed_tools = server.get("allowed_tools", [])
            names = [
                str(item.get("name") or "").strip()
                for item in allowed_tools
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ] if isinstance(allowed_tools, list) else []
            mcp_lines.append(
                f"- server={str(server.get('name') or '').strip()}; allowed_tools={','.join(names) or '(none)'}"
            )

    history_lines = [
        f"{message.role}: {' '.join(message.content.split())}"
        for message in state.messages[-4:]
    ]
    return (
        "Decide tool intent as part of the same MS-Agent plan. The model only proposes candidates; "
        "the server performs final authorization.\n"
        "Use web_search for real-time facts, current weather, official-policy lookup, directories, or explicit online search. "
        "Do not use it for profile collection, general planning, emotional support, or content covered by local assets.\n"
        "Use rag for local Skill references/assets, subject_requirements for structured subject-selection requirements, "
        "status_track only when the Skill needs its declared status script, and mcp only for an allowed external capability.\n"
        "Use kind=script only when required_scripts is non-empty. intent_label must be 5-10 Chinese characters, "
        "must not start with 正在, and must describe the actual user-visible operation.\n\n"
        f"# Tool Capabilities\n{chr(10).join(capability_lines) or '(none)'}\n\n"
        f"# Allowed MCP Catalog\n{chr(10).join(mcp_lines) or '(none)'}\n\n"
        f"# Asset Domains\n{chr(10).join(domain_lines) or '(none)'}\n\n"
        f"# Recent History\n{chr(10).join(history_lines) or '(empty)'}"
    )


def parse_ms_agent_tool_routing(value: object) -> RoutingDecision | None:
    if not isinstance(value, dict):
        return None
    required = value.get("required")
    candidates_payload = value.get("candidates")
    allow_web_search = value.get("allow_web_search")
    candidate_domains_payload = value.get("candidate_domains")
    if not isinstance(required, bool) or not isinstance(allow_web_search, bool):
        return None
    if not isinstance(candidates_payload, list) or not isinstance(candidate_domains_payload, list):
        return None

    candidates: list[ToolRoutingCandidate] = []
    for item in candidates_payload:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        if kind not in ALLOWED_TOOL_ROUTING_KINDS:
            return None
        if kind == "tool" and name not in ALLOWED_TOOL_ROUTING_NAMES:
            return None
        if kind == "script" and name != "script":
            return None
        candidates.append(
            ToolRoutingCandidate(
                kind=kind,
                name=name,
                intent_label=str(item.get("intent_label") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            )
        )
    if required != bool(candidates):
        return None
    web_requested = any(item.kind == "tool" and item.name == "web_search" for item in candidates)
    if allow_web_search != web_requested:
        return None
    candidate_domains = tuple(
        str(item).strip()
        for item in candidate_domains_payload
        if isinstance(item, str) and item.strip()
    )
    return RoutingDecision(
        allow_web_search=allow_web_search,
        candidate_domains=candidate_domains,
        reason=str(value.get("reason") or "").strip(),
        query_focus=str(value.get("query_focus") or "").strip(),
        required=required,
        candidates=tuple(candidates),
        source="ms_agent",
    )


def parse_routing_decision(raw_reply: str) -> RoutingDecision:
    payload = parse_json_object(raw_reply)
    if payload is None:
        return RoutingDecision()
    allow_web_search = bool(payload.get("allow_web_search"))
    candidate_domains_payload = payload.get("candidate_domains", [])
    candidate_domains = tuple(
        str(item).strip()
        for item in candidate_domains_payload
        if str(item).strip()
    ) if isinstance(candidate_domains_payload, list) else ()
    return RoutingDecision(
        allow_web_search=allow_web_search,
        candidate_domains=candidate_domains,
        query_focus=str(payload.get("query_focus") or "").strip(),
        reason=str(payload.get("reason") or "").strip(),
    )


def parse_json_object(raw_reply: str) -> dict[str, object] | None:
    stripped = raw_reply.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", stripped)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None
