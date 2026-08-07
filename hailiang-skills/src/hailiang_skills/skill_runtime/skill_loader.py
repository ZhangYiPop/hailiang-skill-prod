from __future__ import annotations

import hashlib
import shutil
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import yaml

from hailiang_skills.skill_runtime.data_loader import (
    default_generated_data_dir,
    load_generated_asset_domains,
    load_generated_asset_registry,
    load_generated_tool_registry,
)
from hailiang_skills.skill_runtime.models import (
    BridgeConfig,
    DebugConfig,
    IntentRouterConfig,
    PlannerConfig,
    PlannerSceneSelectionConfig,
    PromptLoadingConfig,
    RetrievalConfig,
    ResponsePolicyConfig,
    SkillAssetsConfig,
    SkillBundle,
    SkillResource,
    SkillRoutingConfig,
    SkillRuntimeMetadata,
    SkillToolPolicy,
)
from hailiang_skills.skill_runtime.skill_contract import load_skill_contract
from hailiang_skills.skill_runtime.tools import default_tool_registry

IGNORED_PARTS = {"__MACOSX", "__pycache__"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}
IGNORED_PREFIXES = ("._",)


def default_cache_dir() -> Path:
    return Path.cwd() / ".skill_runtime_cache"


def load_skill_bundle(source: str, cache_dir: str | Path | None = None) -> SkillBundle:
    cache_root = Path(cache_dir).expanduser().resolve() if cache_dir else default_cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)
    local_path = Path(source).expanduser().resolve() if not _is_url(source) else None
    if local_path and local_path.is_dir():
        return load_skill_bundle_from_directory(local_path, cache_dir=cache_root)

    archive_path = _resolve_skill_source(source, cache_root)
    _validate_archive_path(archive_path, source)
    extract_dir = cache_root / "extracted" / _sha256_file(archive_path)
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    _extract_archive(archive_path, extract_dir)
    return _build_bundle(source=source, archive_path=archive_path, extract_dir=extract_dir)


def load_skill_bundle_from_directory(skill_dir: str | Path, cache_dir: str | Path | None = None) -> SkillBundle:
    del cache_dir
    root_dir = Path(skill_dir).expanduser().resolve()
    if not root_dir.is_dir():
        raise FileNotFoundError(f"skill 目录不存在: {root_dir}")
    skill_file = _skill_file_for_root(root_dir)
    if skill_file is None:
        raise ValueError(f"无效 skill 目录：未找到 SKILL.md 或 skill.md: {root_dir}")
    return _build_bundle_from_root(
        source=str(root_dir),
        archive_path=root_dir,
        extract_dir=root_dir,
        root_dir=root_dir,
        skill_file=skill_file,
    )


def _resolve_skill_source(source: str, cache_root: Path) -> Path:
    if _is_url(source):
        downloads_dir = cache_root / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urllib.parse.urlparse(source).path).suffix or ".skill"
        local_path = downloads_dir / f"{_sha256_text(source)}{suffix}"
        if local_path.is_file():
            return local_path
        with urllib.request.urlopen(source, timeout=60) as response:
            local_path.write_bytes(response.read())
        return local_path

    local_path = Path(source).expanduser().resolve()
    if not local_path.is_file():
        raise FileNotFoundError(f".skill 文件不存在: {local_path}")
    return local_path


def _build_bundle(source: str, archive_path: Path, extract_dir: Path) -> SkillBundle:
    skill_file, root_dir = _locate_skill_file(extract_dir)
    return _build_bundle_from_root(
        source=source,
        archive_path=archive_path,
        extract_dir=extract_dir,
        root_dir=root_dir,
        skill_file=skill_file,
    )


def _build_bundle_from_root(
    *,
    source: str,
    archive_path: Path,
    extract_dir: Path,
    root_dir: Path,
    skill_file: Path,
) -> SkillBundle:
    root_name = root_dir.name if root_dir != extract_dir else archive_path.stem
    metadata = _parse_frontmatter(_read_frontmatter_text(skill_file))

    scripts: dict[str, Path] = {}
    resources: list[SkillResource] = [
        SkillResource(
            resource_type="skill",
            relative_path=_relative_to_root(skill_file, root_dir),
            file_path=skill_file,
            size_bytes=skill_file.stat().st_size,
        )
    ]

    references_root = root_dir / "references"
    if references_root.exists():
        for file_path in _iter_resource_files(references_root):
            relative_path = _relative_to_root(file_path, root_dir)
            resources.append(
                SkillResource(
                    resource_type="reference",
                    relative_path=relative_path,
                    file_path=file_path,
                    size_bytes=file_path.stat().st_size,
                )
            )

    assets_root = _resolve_local_assets_root(root_dir, metadata)
    if assets_root.exists():
        for file_path in _iter_resource_files(assets_root):
            relative_path = _relative_to_root(file_path, root_dir)
            resources.append(
                SkillResource(
                    resource_type="asset",
                    relative_path=relative_path,
                    file_path=file_path,
                    size_bytes=file_path.stat().st_size,
                )
            )

    for virtual_root, scripts_root in _iter_script_roots(root_dir):
        for file_path in _iter_resource_files(scripts_root):
            relative_path = f"{virtual_root}/{file_path.relative_to(scripts_root).as_posix()}"
            if relative_path in scripts:
                continue
            scripts[relative_path] = file_path
            resources.append(
                SkillResource(
                    resource_type="script",
                    relative_path=relative_path,
                    file_path=file_path,
                    size_bytes=file_path.stat().st_size,
                )
            )

    data_root = default_generated_data_dir()
    contract = load_skill_contract(root_dir, metadata=metadata)
    runtime_metadata = _build_runtime_metadata(metadata, contract)
    if runtime_metadata.assets.local_enabled and not assets_root.is_dir():
        raise ValueError(f"skill 声明启用了本地 assets，但目录不存在: {assets_root}")
    return SkillBundle(
        source=source,
        archive_path=archive_path,
        extracted_dir=extract_dir,
        root_dir=root_dir,
        root_name=root_name,
        metadata=metadata,
        runtime_metadata=runtime_metadata,
        skill_file=skill_file,
        resources=tuple(sorted(resources, key=_resource_sort_key)),
        asset_registry=load_generated_asset_registry(data_root),
        tool_registry=default_tool_registry(load_generated_tool_registry(data_root)),
        contract=contract,
        _skill_markdown_loader=lambda path=skill_file: path.read_text(encoding="utf-8"),
        _references_loader=lambda path=references_root, root=root_dir: _load_reference_contents(path, root),
        _local_assets_loader=lambda path=assets_root, root=root_dir: _load_local_asset_contents(path, root),
        _scripts=scripts,
        _asset_domains_loader=lambda root=data_root: load_generated_asset_domains(root),
    )


def _extract_archive(archive_path: Path, extract_dir: Path) -> None:
    try:
        with ZipFile(archive_path) as archive:
            for info in archive.infolist():
                normalized = _normalize_member_name(info.filename)
                relative_path = _sanitize_member_path(normalized)
                if relative_path is None or info.is_dir():
                    continue

                target_path = extract_dir / relative_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(archive.read(info))
    except BadZipFile as exc:
        raise ValueError(f"无效 .skill 包：不是合法的 zip 压缩文件: {archive_path}") from exc


def _normalize_member_name(name: str) -> str:
    repaired = _repair_cp437_utf8(name)
    return repaired.lstrip("./")


def _sanitize_member_path(name: str) -> Path | None:
    pure_path = PurePosixPath(name)
    parts = [part for part in pure_path.parts if part not in ("", ".")]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        raise ValueError(f".skill 包含非法路径: {name}")
    if any(part in IGNORED_PARTS for part in parts):
        return None
    if parts[-1] in IGNORED_NAMES or any(parts[-1].startswith(prefix) for prefix in IGNORED_PREFIXES):
        return None
    return Path(*parts)


def _validate_archive_path(archive_path: Path, source: str) -> None:
    if archive_path.suffix.lower() != ".skill":
        raise ValueError(f"仅支持 .skill 文件或 URL: {source}")


def _locate_skill_file(extract_dir: Path) -> tuple[Path, Path]:
    skill_files = sorted(
        path
        for path in extract_dir.rglob("*")
        if path.is_file()
        and path.name in {"SKILL.md", "skill.md"}
        and _sanitize_member_path(path.relative_to(extract_dir).as_posix()) is not None
    )
    if not skill_files:
        raise ValueError("无效 .skill 包：未找到 SKILL.md 或 skill.md")
    if len(skill_files) > 1:
        joined = ", ".join(path.relative_to(extract_dir).as_posix() for path in skill_files)
        raise ValueError(f"无效 .skill 包：发现多个 SKILL.md: {joined}")

    skill_file = skill_files[0]
    root_dir = skill_file.parent
    for file_path in sorted(path for path in extract_dir.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(extract_dir).as_posix()
        if _sanitize_member_path(relative_path) is None:
            continue
        try:
            file_path.relative_to(root_dir)
        except ValueError as exc:
            raise ValueError(
                f"无效 .skill 包：发现不属于 skill 根目录的文件: {relative_path}"
            ) from exc
    return skill_file, root_dir


def _skill_file_for_root(root_dir: Path) -> Path | None:
    """Prefer lower-case entrypoints while retaining existing Skill packages."""
    candidates = {
        candidate.name: candidate
        for candidate in root_dir.iterdir()
        if candidate.is_file() and candidate.name in {"skill.md", "SKILL.md"}
    }
    return candidates.get("skill.md") or candidates.get("SKILL.md")


def _iter_resource_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and _sanitize_member_path(path.relative_to(root.parent).as_posix()) is not None
    ]


def _iter_script_roots(root_dir: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    for directory_name in ("script", "scripts"):
        candidate = root_dir / directory_name
        if candidate.exists():
            candidates.append(("script", candidate))
    return candidates


def _load_reference_contents(references_root: Path, root_dir: Path) -> dict[str, str]:
    if not references_root.exists():
        return {}
    references: dict[str, str] = {}
    for file_path in _iter_resource_files(references_root):
        relative_path = _relative_to_root(file_path, root_dir)
        references[relative_path] = file_path.read_text(encoding="utf-8", errors="replace")
    return references


def _load_local_asset_contents(assets_root: Path, root_dir: Path) -> dict[str, str]:
    if not assets_root.exists():
        return {}
    assets: dict[str, str] = {}
    for file_path in _iter_resource_files(assets_root):
        relative_path = _relative_to_root(file_path, root_dir)
        assets[relative_path] = file_path.read_text(encoding="utf-8", errors="replace")
    return assets


def _read_frontmatter_text(skill_file: Path) -> str:
    with skill_file.open(encoding="utf-8") as f:
        first_line = f.readline()
        if first_line.strip() != "---":
            return ""
        lines = [first_line.rstrip("\n")]
        for line in f:
            lines.append(line.rstrip("\n"))
            if line.strip() == "---":
                break
    return "\n".join(lines)


def _relative_to_root(path: Path, root_dir: Path) -> str:
    return path.relative_to(root_dir).as_posix()


def _resource_sort_key(resource: SkillResource) -> tuple[int, str]:
    order = {"skill": 0, "reference": 1, "asset": 2, "script": 3}
    return order.get(resource.resource_type, 99), resource.relative_path


def _repair_cp437_utf8(name: str) -> str:
    try:
        repaired = name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if "\ufffd" in repaired:
        return name
    return repaired


def _parse_frontmatter(markdown_text: str) -> dict[str, object]:
    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for payload in yaml.safe_load_all(markdown_text):
        if isinstance(payload, dict):
            return payload
    return {}


def _resolve_local_assets_root(root_dir: Path, metadata: dict[str, object]) -> Path:
    assets_config = metadata.get("assets")
    if isinstance(assets_config, dict):
        local_dir = str(assets_config.get("local_dir") or "").strip()
        if local_dir:
            return (root_dir / local_dir).resolve()
    return root_dir / "assets"


def _build_runtime_metadata(metadata: dict[str, object], contract) -> SkillRuntimeMetadata:
    prompt_loading_raw = metadata.get("prompt_loading")
    prompt_loading = prompt_loading_raw if isinstance(prompt_loading_raw, dict) else {}
    retrieval_raw = metadata.get("retrieval")
    retrieval = retrieval_raw if isinstance(retrieval_raw, dict) else {}
    assets_raw = metadata.get("assets")
    assets = assets_raw if isinstance(assets_raw, dict) else {}
    tool_policy_raw = metadata.get("tool_policy")
    tool_policy = tool_policy_raw if isinstance(tool_policy_raw, dict) else {}
    debug_raw = metadata.get("debug")
    debug = debug_raw if isinstance(debug_raw, dict) else {}
    response_policy_raw = metadata.get("response_policy")
    response_policy = response_policy_raw if isinstance(response_policy_raw, dict) else {}
    bridge_raw = metadata.get("bridge")
    bridge = bridge_raw if isinstance(bridge_raw, dict) else {}
    routing_raw = metadata.get("routing")
    routing = routing_raw if isinstance(routing_raw, dict) else {}
    planner_raw = metadata.get("planner")
    planner = planner_raw if isinstance(planner_raw, dict) else {}
    scene_selection_raw = planner.get("scene_selection")
    scene_selection = scene_selection_raw if isinstance(scene_selection_raw, dict) else {}
    intent_router_raw = planner.get("intent_router")
    intent_router = intent_router_raw if isinstance(intent_router_raw, dict) else {}

    return SkillRuntimeMetadata(
        name=str(metadata.get("name") or "").strip(),
        description=str(metadata.get("description") or "").strip(),
        brief=str(metadata.get("brief") or "").strip(),
        info=str(metadata.get("info") or "").strip(),
        skill_id=str(metadata.get("skill_id") or contract.skill_id or "").strip(),
        version=str(metadata.get("version") or "").strip(),
        author=str(metadata.get("author") or "").strip(),
        tags=_string_tuple(metadata.get("tags")),
        skill_type=str(metadata.get("skill_type") or "native").strip() or "native",
        entrypoint_role=str(
            metadata.get("entrypoint_role") or metadata.get("skill_role") or contract.skill_role or "child"
        ).strip() or "child",
        accepts_scenes=_string_tuple(metadata.get("accepts_scenes")) or contract.accepts_scenes,
        triggers=_string_tuple(metadata.get("triggers")),
        prompt_loading=PromptLoadingConfig(
            strategy=str(prompt_loading.get("strategy") or "legacy").strip() or "legacy",
            include_skill_markdown=str(prompt_loading.get("include_skill_markdown") or "full").strip() or "full",
            include_session_state=bool(prompt_loading.get("include_session_state", True)),
            include_tool_capabilities=bool(prompt_loading.get("include_tool_capabilities", True)),
            include_route_targets=bool(prompt_loading.get("include_route_targets", True)),
            include_references=str(prompt_loading.get("include_references") or "full").strip() or "full",
            include_local_assets=str(prompt_loading.get("include_local_assets") or "summary").strip() or "summary",
            include_generated_assets=str(prompt_loading.get("include_generated_assets") or "summary").strip()
            or "summary",
        ),
        retrieval=RetrievalConfig(
            enabled=bool(retrieval.get("enabled", False)),
            supplemental_enabled=bool(retrieval.get("supplemental_enabled", False)),
            sources=_string_tuple(retrieval.get("sources")),
            top_k=max(int(retrieval.get("top_k", 3) or 3), 1),
            snippet_chars=max(int(retrieval.get("snippet_chars", 600) or 600), 120),
            include_catalog=bool(retrieval.get("include_catalog", True)),
        ),
        assets=SkillAssetsConfig(
            local_enabled=bool(assets.get("local_enabled", False)),
            local_dir=str(assets.get("local_dir") or "assets").strip() or "assets",
            local_prompt_policy=str(assets.get("local_prompt_policy") or "summary").strip() or "summary",
            generated_domains=_string_tuple(assets.get("generated_domains")),
            generated_prompt_policy=str(assets.get("generated_prompt_policy") or "summary").strip() or "summary",
        ),
        tool_policy=SkillToolPolicy(
            allow_tool_call_first=bool(tool_policy.get("allow_tool_call_first", True)),
            allow_direct_answer=bool(tool_policy.get("allow_direct_answer", True)),
            max_tool_calls=max(int(tool_policy.get("max_tool_calls", 0) or 0), 0),
        ),
        debug=DebugConfig(
            record_prompt_assembly=bool(debug.get("record_prompt_assembly", True)),
            record_retrieval_details=bool(debug.get("record_retrieval_details", True)),
        ),
        response_policy=ResponsePolicyConfig(
            citation_visibility=str(response_policy.get("citation_visibility") or "hidden").strip() or "hidden",
            mention_source_category=bool(response_policy.get("mention_source_category", True)),
            allow_file_name_mentions=bool(response_policy.get("allow_file_name_mentions", False)),
            allow_reference_id_mentions=bool(response_policy.get("allow_reference_id_mentions", False)),
            sanitize_output=bool(response_policy.get("sanitize_output", True)),
        ),
        bridge=BridgeConfig(
            target_skill=str(bridge.get("target_skill") or metadata.get("hailiang_skill") or "").strip(),
            scenario=str(bridge.get("scenario") or "").strip(),
            phase=str(bridge.get("phase") or "").strip(),
        ),
        routing=SkillRoutingConfig(
            scene_name=str(routing.get("scene_name") or "").strip(),
            intent_clarity=str(routing.get("intent_clarity") or "").strip(),
            routing_examples=_string_tuple(routing.get("routing_examples")),
            slot_facts=_string_tuple(routing.get("slot_facts")),
            school_stage_scope=str(routing.get("school_stage_scope") or "").strip(),
        ),
        planner=PlannerConfig(
            scene_selection=PlannerSceneSelectionConfig(
                mode=str(scene_selection.get("mode") or "").strip(),
                matrix_reference=str(scene_selection.get("matrix_reference") or "").strip(),
                match_fields=_string_tuple(scene_selection.get("match_fields")),
                fallback=str(scene_selection.get("fallback") or "keyword").strip() or "keyword",
                enable_implicit_routing=bool(scene_selection.get("enable_implicit_routing", False)),
            ),
            intent_router=IntentRouterConfig(
                unclear_intent_patterns=_string_tuple(intent_router.get("unclear_intent_patterns")),
                enable_embedding=bool(intent_router.get("enable_embedding", True)),
                embedding_model=str(intent_router.get("embedding_model") or "text-embedding-v4").strip()
                or "text-embedding-v4",
                embedding_timeout_s=max(int(intent_router.get("embedding_timeout_s", 8) or 8), 1),
                direct_threshold=float(intent_router.get("direct_threshold", 0.72) or 0.72),
                ambiguity_margin=float(intent_router.get("ambiguity_margin", 0.08) or 0.08),
                general_chat_choice_threshold=_confidence(
                    _route_suggestion_value(
                        intent_router,
                        "card_threshold",
                        intent_router.get("general_chat_choice_threshold", 0.72),
                    ),
                    0.72,
                ),
                general_chat_choice_max_candidates=max(
                    int(intent_router.get("general_chat_choice_max_candidates", 3) or 3),
                    1,
                ),
                route_suggestion_min_confidence=_confidence(
                    _route_suggestion_value(intent_router, "min_confidence", 0.72), 0.72
                ),
                route_suggestion_min_confidence_without_context=_confidence(
                    _route_suggestion_value(intent_router, "min_confidence_without_context", 0.72), 0.72
                ),
                route_suggestion_card_threshold=_confidence(
                    _route_suggestion_value(intent_router, "card_threshold", 0.72), 0.72
                ),
                specialist_switch_threshold=_confidence(
                    _route_suggestion_value(intent_router, "specialist_switch_threshold", 0.92), 0.92
                ),
                enable_llm_fallback=bool(intent_router.get("enable_llm_fallback", False)),
                long_profile_message_min_chars=max(
                    int(intent_router.get("long_profile_message_min_chars", 80) or 80),
                    20,
                ),
                long_profile_signal_threshold=max(
                    int(intent_router.get("long_profile_signal_threshold", 3) or 3),
                    1,
                ),
            ),
        ),
        raw=dict(metadata),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        normalized = value.strip()
        return (normalized,) if normalized else ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _route_suggestion_value(payload: dict, key: str, default: object) -> object:
    nested = payload.get("route_suggestions")
    return nested.get(key, default) if isinstance(nested, dict) else default


def _confidence(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 0.0), 1.0)


def _is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
