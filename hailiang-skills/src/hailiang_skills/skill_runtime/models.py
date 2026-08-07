from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True, frozen=True)
class SkillResource:
    resource_type: str
    relative_path: str
    file_path: Path
    size_bytes: int
    content: str | None = None


@dataclass(slots=True, frozen=True)
class GeneratedAssetFile:
    domain: str
    file_name: str
    relative_path: str
    file_path: Path
    payload: Any


@dataclass(slots=True)
class GeneratedAssetDomain:
    name: str
    root_dir: Path
    files: tuple[GeneratedAssetFile, ...] = ()
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolCapability:
    name: str
    description: str
    enabled: bool = False


@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    enabled: bool = False


@dataclass(slots=True, frozen=True)
class SkillCatalogEntry:
    """Small, user-facing Skill summary safe to expose to general_chat."""

    skill_id: str
    name: str
    brief: str = ""
    description: str = ""
    scene_name: str = ""
    routing_examples: tuple[str, ...] = ()
    school_stage_scope: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "brief": self.brief,
            "description": self.description,
            "scene_name": self.scene_name,
            "routing_examples": list(self.routing_examples),
            "school_stage_scope": self.school_stage_scope,
        }


@dataclass(slots=True, frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolCallResult:
    id: str
    name: str
    ok: bool
    content: str
    error: str = ""
    sources: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ToolRoutingCandidate:
    kind: str
    name: str
    intent_label: str = ""
    reason: str = ""


@dataclass(slots=True, frozen=True)
class RoutingDecision:
    allow_web_search: bool = False
    candidate_domains: tuple[str, ...] = ()
    reason: str = ""
    query_focus: str = ""
    required: bool = False
    candidates: tuple[ToolRoutingCandidate, ...] = ()
    source: str = ""


@dataclass(slots=True, frozen=True)
class StageContract:
    id: str
    kind: str = "default"
    required_facts: tuple[str, ...] = ()
    enable_intent_check: bool = False
    enable_satisfaction_check: bool = False


@dataclass(slots=True, frozen=True)
class RouteTarget:
    scene: str
    target_skill_id: str
    required_global_facts: tuple[str, ...] = ()
    required_skill_facts: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class FactsSchema:
    global_keys: tuple[str, ...] = ()
    skill_keys: tuple[str, ...] = ()
    stage_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    promote_to_global: tuple[str, ...] = ()
    share_with_parent_skill: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillContract:
    skill_id: str
    skill_role: str = "child"
    stages: tuple[StageContract, ...] = ()
    facts_schema: FactsSchema = field(default_factory=FactsSchema)
    routes: tuple[RouteTarget, ...] = ()
    accepts_scenes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RouteDecision:
    should_route: bool = False
    target_skill_id: str = ""
    scene: str = ""
    reason: str = ""
    missing_global_facts: tuple[str, ...] = ()
    missing_skill_facts: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RuntimeStateSnapshot:
    active_skill_id: str = ""
    stage: str = "init"
    global_facts: dict[str, Any] = field(default_factory=dict)
    skill_facts: dict[str, Any] = field(default_factory=dict)
    stage_facts: dict[str, Any] = field(default_factory=dict)
    status_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolRegistry:
    capabilities: tuple[ToolCapability, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class PromptLoadingConfig:
    strategy: str = "legacy"
    include_skill_markdown: str = "full"
    include_session_state: bool = True
    include_tool_capabilities: bool = True
    include_route_targets: bool = True
    include_references: str = "full"
    include_local_assets: str = "summary"
    include_generated_assets: str = "summary"


@dataclass(slots=True, frozen=True)
class RetrievalConfig:
    enabled: bool = False
    supplemental_enabled: bool = False
    sources: tuple[str, ...] = ()
    top_k: int = 3
    snippet_chars: int = 600
    include_catalog: bool = True


@dataclass(slots=True, frozen=True)
class SkillAssetsConfig:
    local_enabled: bool = False
    local_dir: str = "assets"
    local_prompt_policy: str = "summary"
    generated_domains: tuple[str, ...] = ()
    generated_prompt_policy: str = "summary"


@dataclass(slots=True, frozen=True)
class SkillToolPolicy:
    allow_tool_call_first: bool = True
    allow_direct_answer: bool = True
    max_tool_calls: int = 0


@dataclass(slots=True, frozen=True)
class DebugConfig:
    record_prompt_assembly: bool = True
    record_retrieval_details: bool = True


@dataclass(slots=True, frozen=True)
class ResponsePolicyConfig:
    citation_visibility: str = "hidden"
    mention_source_category: bool = True
    allow_file_name_mentions: bool = False
    allow_reference_id_mentions: bool = False
    sanitize_output: bool = True


@dataclass(slots=True, frozen=True)
class BridgeConfig:
    target_skill: str = ""
    scenario: str = ""
    phase: str = ""


@dataclass(slots=True, frozen=True)
class SkillRoutingConfig:
    scene_name: str = ""
    intent_clarity: str = ""
    routing_examples: tuple[str, ...] = ()
    slot_facts: tuple[str, ...] = ()
    school_stage_scope: str = ""


@dataclass(slots=True, frozen=True)
class IntentRouterConfig:
    unclear_intent_patterns: tuple[str, ...] = ()
    enable_embedding: bool = True
    embedding_model: str = "text-embedding-v4"
    embedding_timeout_s: int = 8
    direct_threshold: float = 0.72
    ambiguity_margin: float = 0.08
    general_chat_choice_threshold: float = 0.72
    general_chat_choice_max_candidates: int = 3
    route_suggestion_min_confidence: float = 0.72
    route_suggestion_min_confidence_without_context: float = 0.72
    route_suggestion_card_threshold: float = 0.72
    specialist_switch_threshold: float = 0.92
    enable_llm_fallback: bool = False
    long_profile_message_min_chars: int = 80
    long_profile_signal_threshold: int = 3


@dataclass(slots=True, frozen=True)
class PlannerSceneSelectionConfig:
    mode: str = ""
    matrix_reference: str = ""
    match_fields: tuple[str, ...] = ()
    fallback: str = "keyword"
    enable_implicit_routing: bool = False


@dataclass(slots=True, frozen=True)
class PlannerConfig:
    scene_selection: PlannerSceneSelectionConfig = field(default_factory=PlannerSceneSelectionConfig)
    intent_router: IntentRouterConfig = field(default_factory=IntentRouterConfig)


@dataclass(slots=True, frozen=True)
class SkillRuntimeMetadata:
    name: str = ""
    description: str = ""
    brief: str = ""
    info: str = ""
    skill_id: str = ""
    version: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()
    skill_type: str = "native"
    entrypoint_role: str = "child"
    accepts_scenes: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    prompt_loading: PromptLoadingConfig = field(default_factory=PromptLoadingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    assets: SkillAssetsConfig = field(default_factory=SkillAssetsConfig)
    tool_policy: SkillToolPolicy = field(default_factory=SkillToolPolicy)
    debug: DebugConfig = field(default_factory=DebugConfig)
    response_policy: ResponsePolicyConfig = field(default_factory=ResponsePolicyConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    routing: SkillRoutingConfig = field(default_factory=SkillRoutingConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RetrievedContextItem:
    source_type: str
    source_path: str
    title: str
    snippet: str
    score: int


@dataclass(slots=True, frozen=True)
class PromptAssembly:
    core_prompt: str
    retrieval_prompt: str
    tool_results_prompt: str
    final_prompt: str
    reference_strategy: str
    retrieved_items: tuple[RetrievedContextItem, ...] = ()
    generated_asset_domains: tuple[str, ...] = ()
    local_asset_paths: tuple[str, ...] = ()
    assembled_from: tuple[str, ...] = ()


@dataclass(slots=True)
class SkillBundle:
    source: str
    archive_path: Path
    extracted_dir: Path
    root_dir: Path
    root_name: str
    metadata: dict[str, Any]
    skill_file: Path
    runtime_metadata: SkillRuntimeMetadata = field(default_factory=SkillRuntimeMetadata)
    resources: tuple[SkillResource, ...] = ()
    asset_registry: dict[str, Any] = field(default_factory=dict)
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    contract: SkillContract = field(default_factory=lambda: SkillContract(skill_id=""))
    _skill_markdown: str | None = None
    _skill_markdown_loader: Callable[[], str] | None = None
    _references: dict[str, str] | None = None
    _references_loader: Callable[[], dict[str, str]] | None = None
    _local_assets: dict[str, str] | None = None
    _local_assets_loader: Callable[[], dict[str, str]] | None = None
    _scripts: dict[str, Path] = field(default_factory=dict)
    _asset_domains: dict[str, GeneratedAssetDomain] | None = None
    _asset_domains_loader: Callable[[], dict[str, GeneratedAssetDomain]] | None = None

    @property
    def skill_markdown(self) -> str:
        if self._skill_markdown is None:
            self._skill_markdown = self._skill_markdown_loader() if self._skill_markdown_loader else ""
        return self._skill_markdown

    @property
    def references(self) -> dict[str, str]:
        if self._references is None:
            self._references = self._references_loader() if self._references_loader else {}
        return self._references

    @property
    def scripts(self) -> dict[str, Path]:
        return self._scripts

    @property
    def local_assets(self) -> dict[str, str]:
        if self._local_assets is None:
            self._local_assets = self._local_assets_loader() if self._local_assets_loader else {}
        return self._local_assets

    @property
    def local_asset_index(self) -> tuple[str, ...]:
        return tuple(sorted(self.local_assets))

    @property
    def asset_domains(self) -> dict[str, GeneratedAssetDomain]:
        if self._asset_domains is None:
            self._asset_domains = self._asset_domains_loader() if self._asset_domains_loader else {}
        return self._asset_domains


@dataclass(slots=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    api_key: str
    timeout_s: int
    temperature: float
    max_tokens: int
    enable_thinking: bool = False
    return_reasoning: bool = False


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str
    tool_call_id: str = ""
    name: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(slots=True)
class SessionState:
    session_id: str
    stage: str = "init"
    collected_info: dict[str, Any] = field(default_factory=dict)
    active_skill_id: str = ""
    global_facts: dict[str, Any] = field(default_factory=dict)
    skill_facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    stage_facts: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    status_flags: dict[str, Any] = field(default_factory=dict)
    route_history: list[dict[str, Any]] = field(default_factory=list)
    messages: list[ChatMessage] = field(default_factory=list)
    soul_context: dict[str, Any] = field(default_factory=dict)
    conversation_memory: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AssistantTurnResult:
    final_text: str = ""
    tool_mode: str = "none"
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(slots=True, frozen=True)
class ChatStreamChunk:
    content_delta: str = ""
    reasoning_delta: str = ""
