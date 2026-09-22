from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import secrets
import tempfile
import uuid
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Iterable
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.message_interactions import (
    ACTIVE,
    SELECTED,
    SUBMITTED,
    ensure_message_interactions,
    expire_active_interactions,
    update_interaction,
)
from hailiang_skills.core.sse_protocol import empty_message_state, presentation_from_message
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.schemas.facts import KnownFacts
from hailiang_skills.schemas.questionnaire import validate_questionnaire_config
from hailiang_skills.storage.database import (
    WorkbenchActorRow,
    WorkbenchAssetRow,
    WorkbenchAuditRow,
    WorkbenchDebugSessionRow,
    WorkbenchDeploymentRow,
    WorkbenchEvaluationRunRow,
    WorkbenchEvaluationSuiteRow,
    WorkbenchObjectRow,
    WorkbenchReleaseRow,
    WorkbenchSoulRevisionRow,
    WorkbenchRevisionRow,
    utc_now,
)
from hailiang_skills.workbench.kernel_identity import capability_catalog, kernel_fingerprint
from hailiang_skills.workbench.runtime_overlay import (
    configured_skill_bundle,
    materialize_entry_files,
    skill_bundle_from_entry,
    skill_markdown_with_metadata,
)
from hailiang_skills.runtime_bridge.script_review import review_scripts
from hailiang_skills.runtime_bridge.native_questionnaire import questionnaire_plan_summary
from hailiang_skills.runtime_bridge.agentscope_expert_runtime import AgentScopeRuntimeUnavailable
from hailiang_skills.runtime_bridge.agent_frontmatter import validate_agent_skill_routing


VALID_OBJECT_TYPES = {"skill", "expert", "expert_team"}
EXECUTABLE_SUFFIXES = {".pyc", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".exe", ".dll", ".so", ".dylib"}
SAFE_ROOT_RESOURCE_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".txt"}
MAX_ASSET_BYTES = int(os.getenv("HAILIANG_WORKBENCH_MAX_ASSET_BYTES", str(20 * 1024 * 1024)))
MAX_PACKAGE_BYTES = int(os.getenv("HAILIANG_WORKBENCH_MAX_PACKAGE_BYTES", str(100 * 1024 * 1024)))
# Candidate tests must fail visibly rather than hold a browser SSE connection
# for the much longer general test-model timeout. It remains configurable for
# slow, intentionally long-running test environments.
WORKBENCH_CANDIDATE_LLM_TIMEOUT_S = max(
    int(os.getenv("HAILIANG_WORKBENCH_CANDIDATE_LLM_TIMEOUT_S", "900") or 900),
    1,
)
WORKBENCH_CANDIDATE_LLM_MAX_TOKENS = max(
    int(os.getenv("HAILIANG_WORKBENCH_CANDIDATE_LLM_MAX_TOKENS", "384000") or 384000),
    1,
)


def _script_execution_succeeded(output: dict[str, Any]) -> bool:
    """Normalize both local and MS-Agent sandbox execution result shapes."""
    explicit_ok = output.get("ok")
    if isinstance(explicit_ok, bool):
        return explicit_ok

    for field in ("json_output", "return_value"):
        result = output.get(field)
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return bool(result["ok"])

    exit_code = output.get("exit_code")
    if isinstance(exit_code, bool):
        return False
    if isinstance(exit_code, int):
        return exit_code == 0
    if isinstance(exit_code, str):
        return exit_code.strip().lower() in {"0", "ok", "success", "succeeded", "completed"}
    return False


def _questionnaire_asset_errors(payload: dict[str, Any], assets: list[tuple[str, str, bytes]]) -> list[str]:
    metadata = payload.get("configuration") or payload.get("source_metadata") or {}
    questionnaire = metadata.get("questionnaire") if isinstance(metadata, dict) else {}
    contract = payload.get("runtime_contract") if isinstance(payload.get("runtime_contract"), dict) else {}
    if not isinstance(questionnaire, dict) or not questionnaire:
        # Read compatibility for revisions created before questionnaire
        # configuration moved out of runtime_contract.json.
        questionnaire = contract.get("questionnaire") if isinstance(contract.get("questionnaire"), dict) else {}
    config_path = str(questionnaire.get("config_path") or "").strip()
    decision_script = questionnaire.get("decision_script") if isinstance(questionnaire, dict) else None
    errors: list[str] = []
    if isinstance(decision_script, dict) and decision_script.get("enabled"):
        entrypoint = str(decision_script.get("entrypoint") or "").strip()
        function = str(decision_script.get("function") or "decide").strip()
        asset_paths = {path for path, _media_type, _content in assets}
        if not entrypoint or not entrypoint.endswith(".py"):
            errors.append("questionnaire.decision_script.entrypoint 必须是 Python 文件")
        elif entrypoint not in asset_paths:
            errors.append(f"questionnaire.decision_script 指向的文件不存在：{entrypoint}")
        if not function:
            errors.append("questionnaire.decision_script.function 不能为空")
    if not config_path:
        return errors
    asset = next((content for path, _media_type, content in assets if path == config_path), None)
    if asset is None:
        return [*errors, f"questionnaire.config_path 指向的文件不存在：{config_path}"]
    try:
        config = json.loads(asset.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [*errors, f"问卷配置不是合法 UTF-8 JSON：{config_path}"]
    return [*errors, *(f"{config_path}: {error}" for error in validate_questionnaire_config(config))]


class WorkbenchError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "WORKBENCH_INVALID_REQUEST",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class WorkbenchConflict(WorkbenchError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _normalize_brief(value: object) -> str:
    return " ".join(str(value or "").split())


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkbenchError("附件路径不合法", code="INVALID_ASSET_PATH")
    return path.as_posix()


class WorkbenchService:
    """Transaction boundary for immutable business configuration management."""

    def __init__(self, session_factory, *, orchestrator: Any | None = None):
        self.session_factory = session_factory
        self.orchestrator = orchestrator
        # Candidate conversations deliberately do not use the formal session
        # repository or its turn coordinator.  This small in-process registry
        # is only the live cancellation bridge for a workbench SSE response;
        # durable transcript/state continue to live on the debug session row.
        self._active_candidate_streams: dict[str, dict[str, Any]] = {}
        self._active_candidate_streams_lock = RLock()
        # A candidate overlay temporarily occupies a Runtime skill id while
        # the formal executor resolves it. Serialize this short-lived mount so
        # concurrent candidate turns cannot observe or restore each other's
        # bundle.
        self._candidate_runtime_mount_lock = RLock()

    @property
    def kernel_fingerprint(self) -> str:
        return kernel_fingerprint(self.orchestrator)

    def kernel_info(self) -> dict[str, Any]:
        capabilities = capability_catalog(self.orchestrator)
        return {
            "fingerprint": self.kernel_fingerprint,
            "runtime_contract_schema": 1,
            "capabilities": capabilities,
            "max_asset_bytes": MAX_ASSET_BYTES,
            "max_package_bytes": MAX_PACKAGE_BYTES,
        }

    def register_actor(self, display_name: str, device_token: str | None = None) -> dict[str, Any]:
        name = str(display_name or "").strip()
        if not 2 <= len(name) <= 80:
            raise WorkbenchError("用户名长度必须为 2-80 个字符", code="INVALID_ACTOR_NAME")
        token = str(device_token or "").strip() or secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.session_factory() as db:
            actor = db.scalar(select(WorkbenchActorRow).where(WorkbenchActorRow.device_token_hash == token_hash))
            if actor is None:
                actor = WorkbenchActorRow(actor_id=_id("actor"), display_name=name, device_token_hash=token_hash)
                db.add(actor)
            else:
                actor.display_name = name
                actor.last_seen_at = utc_now()
            db.commit()
            db.refresh(actor)
            return {"actor_id": actor.actor_id, "display_name": actor.display_name, "device_token": token}

    def bootstrap_soul_from_runtime(self) -> dict[str, Any] | None:
        path = getattr(getattr(self.orchestrator, "runtime_bridge_config", None), "soul_path", None)
        if path is None or not Path(path).is_file():
            return None
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        with self.session_factory() as db:
            existing = db.scalar(select(WorkbenchSoulRevisionRow).order_by(WorkbenchSoulRevisionRow.revision_no.desc()).limit(1))
            if existing is not None:
                return self._soul_revision_dict(existing)
            row = WorkbenchSoulRevisionRow(soul_revision_id=_id("soul"), revision_no=1, content=content, content_hash=hashlib.sha256(content.encode()).hexdigest(), created_by="system")
            db.add(row)
            self._audit(db, "system", "soul_revision.imported", "soul_revision", row.soul_revision_id, {"revision_no": 1})
            db.commit()
            return self._soul_revision_dict(row)

    def list_soul_revisions(self) -> list[dict[str, Any]]:
        self.bootstrap_soul_from_runtime()
        with self.session_factory() as db:
            return [self._soul_revision_dict(row) for row in db.scalars(select(WorkbenchSoulRevisionRow).order_by(WorkbenchSoulRevisionRow.revision_no.desc()))]

    def save_soul_revision(self, content: str, *, actor_id: str) -> dict[str, Any]:
        value = str(content or "").strip()
        if not value:
            raise WorkbenchError("Soul 内容不能为空", code="SOUL_CONTENT_REQUIRED")
        with self.session_factory() as db:
            latest = db.scalar(select(WorkbenchSoulRevisionRow).order_by(WorkbenchSoulRevisionRow.revision_no.desc()).limit(1))
            row = WorkbenchSoulRevisionRow(soul_revision_id=_id("soul"), revision_no=(latest.revision_no if latest else 0) + 1, content=value, content_hash=hashlib.sha256(value.encode()).hexdigest(), created_by=actor_id)
            db.add(row)
            self._audit(db, actor_id, "soul_revision.saved", "soul_revision", row.soul_revision_id, {"revision_no": row.revision_no})
            db.commit()
            return self._soul_revision_dict(row)

    def create_object(
        self,
        *,
        object_type: str,
        object_key: str,
        name: str,
        description: str = "",
        actor_id: str,
    ) -> dict[str, Any]:
        if object_type not in VALID_OBJECT_TYPES:
            raise WorkbenchError("object_type 仅支持 skill、expert、expert_team")
        key = str(object_key or "").strip()
        label = str(name or "").strip()
        if not key or not label:
            raise WorkbenchError("对象标识和名称不能为空")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key):
            raise WorkbenchError("对象标识只能包含字母、数字、下划线和连字符")
        with self.session_factory() as db:
            duplicate = db.scalar(
                select(WorkbenchObjectRow).where(
                    WorkbenchObjectRow.object_type == object_type,
                    WorkbenchObjectRow.object_key == key,
                )
            )
            if duplicate is not None:
                raise WorkbenchConflict("同类型对象标识已存在", code="OBJECT_KEY_CONFLICT")
            row = WorkbenchObjectRow(
                object_id=_id("obj"),
                object_type=object_type,
                object_key=key,
                name=label,
                description=str(description or "").strip(),
                created_by=actor_id,
            )
            db.add(row)
            self._audit(db, actor_id, "object.created", object_type, row.object_id, {"object_key": key})
            db.commit()
            return self._object_dict(row, latest_revision_no=0, latest_release_no=0)

    def preview_standard_skill_package(self, package_bytes: bytes, *, actor_id: str) -> dict[str, Any]:
        """Parse an Agent Skills ZIP without executing its code or creating revisions."""
        files = self._normalize_standard_package_files(self._read_zip(package_bytes))
        source_path = next((path for path in files if PurePosixPath(path).name in {"SKILL.md", "AGENT.md", "TEAM.md"}), "")
        if not source_path:
            raise WorkbenchError("标准包缺少 SKILL.md、AGENT.md 或 TEAM.md", code="STANDARD_SKILL_ENTRY_REQUIRED")
        try:
            source = files[source_path].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkbenchError("标准入口文件必须是 UTF-8 文本", code="STANDARD_SKILL_TEXT_REQUIRED") from exc
        metadata, body = self._standard_frontmatter(source)
        leaf = PurePosixPath(source_path).name
        object_type = "expert_team" if leaf == "TEAM.md" else "expert" if leaf == "AGENT.md" else "skill"
        descriptor: dict[str, Any] = {}
        if object_type in {"expert", "expert_team"}:
            descriptor_name = "agent.yaml" if object_type == "expert" else "team.yaml"
            descriptor_path = PurePosixPath(source_path).with_name(descriptor_name).as_posix()
            if descriptor_path in files:
                try:
                    parsed = yaml.safe_load(files[descriptor_path])
                    descriptor = parsed if isinstance(parsed, dict) else {}
                except (UnicodeDecodeError, yaml.YAMLError):
                    descriptor = {}
        raw_key = str(
            metadata.get("skill_id")
            or metadata.get("agent_id")
            or metadata.get("expert_team_id")
            or metadata.get("team_id")
            or metadata.get("id")
            or descriptor.get("agent_id")
            or descriptor.get("expert_team_id")
            or descriptor.get("team_id")
            or descriptor.get("id")
            or PurePosixPath(source_path).parent.name
            or "imported_skill"
        )
        object_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in raw_key).strip("_") or "imported_skill"
        name = str(metadata.get("name") or descriptor.get("name") or object_key)
        runtime_contract = self._standard_runtime_contract(files)
        assets = []
        blocked: list[str] = []
        reserved_root_files = {
            "SKILL.md", "skill.md", "runtime_contract.json",
            "AGENT.md", "agent.yaml", "TEAM.md", "team.yaml",
            "skills.lock.json", "experts.lock.json", "object.json",
        }
        for path, content in files.items():
            package_path = PurePosixPath(path)
            safe_root_resource = (
                len(package_path.parts) == 1
                and package_path.name not in reserved_root_files
                and package_path.suffix.lower() in SAFE_ROOT_RESOURCE_SUFFIXES
            )
            if path == source_path or not (
                path.startswith("references/")
                or path.startswith("scripts/")
                or path.startswith("assets/")
                or safe_root_resource
            ):
                continue
            if path.startswith("scripts/") and PurePosixPath(path).suffix.lower() not in {".py", ".txt"}:
                blocked.append(f"不支持的脚本文件：{path}")
                continue
            # Preserve the package layout.  assets/ and scripts/ are runtime
            # directories, not reference documents; changing their prefix
            # makes config_path and script discovery fail after import.
            target_path = path
            media_type = "text/x-python" if path.endswith(".py") else "application/json" if path.endswith(".json") else "text/markdown"
            assets.append({"relative_path": target_path, "media_type": media_type, "content_base64": base64.b64encode(content).decode("ascii")})
        decoded = self._decode_assets(assets, object_type=object_type) if not blocked else []
        script_errors = self._script_validation_errors(decoded) if object_type == "skill" else []
        questionnaire = metadata.get("questionnaire") if isinstance(metadata.get("questionnaire"), dict) else {}
        if not questionnaire and isinstance(runtime_contract.get("questionnaire"), dict):
            # Legacy package compatibility only; new packages declare this in SKILL.md.
            questionnaire = copy.deepcopy(runtime_contract["questionnaire"])
        if not questionnaire:
            questionnaire = self._questions_asset_to_questionnaire(files)
            if questionnaire:
                metadata["questionnaire"] = copy.deepcopy(questionnaire)
        payload = (
            {
                "prompt_markdown": source,
                "configuration": copy.deepcopy(metadata),
                "runtime_contract": runtime_contract,
                "capability_ids": [],
            }
            if object_type == "skill"
            else {
                "rules_markdown": body or source,
                "brief": _normalize_brief(
                    descriptor.get("brief")
                    or metadata.get("brief")
                    or metadata.get("description")
                    or metadata.get("desc")
                    or name
                ),
                "budget": {"max_iters": 4, "max_skill_calls": 3},
            }
        )
        ai_hint = self._standard_conversion_ai_hint(source, object_type)
        if ai_hint.get("name"):
            name = str(ai_hint["name"])[:256]
        if ai_hint.get("description"):
            metadata["description"] = str(ai_hint["description"])[:2000]
        warnings = [*blocked, *script_errors]
        warnings.extend(str(item) for item in ai_hint.get("review_items", []) if str(item))
        if questionnaire and not any(key in questionnaire for key in ("fields", "config_path", "config_json")):
            warnings.append("问卷定义无法映射为平台表单字段，请人工补充。")
        return {
            "root": self._entry_debug(root),
            "source": {"entry_path": source_path, "files": sorted(files), "metadata": metadata},
            "draft": {"object_type": object_type, "object_key": object_key, "name": name, "description": str(metadata.get("description") or ""), "payload": payload, "assets": assets, "dependency_locks": []},
            "form_preview": questionnaire.get("fields", []) if isinstance(questionnaire.get("fields"), list) else [],
            "warnings": warnings,
            "ai_assistance": {"status": ai_hint.get("status", "deterministic_fallback"), "message": ai_hint.get("message", "已生成受约束的转换草稿；可在确认前编辑。")},
        }

    def _standard_conversion_ai_hint(self, source: str, object_type: str) -> dict[str, Any]:
        if self.orchestrator is None:
            return {"status": "deterministic_fallback", "message": "未配置转换模型，已使用确定性映射。"}
        try:
            from hailiang_skills.skill_runtime.models import ChatMessage

            context = SessionContext(session_id=f"conversion_{uuid.uuid4().hex[:16]}", user_id="workbench-conversion", profile_id="workbench-conversion")
            client = self.orchestrator._runtime_client_for_context(context)
            if client is None:
                return {"status": "deterministic_fallback", "message": "转换模型不可用，已使用确定性映射。"}
            prompt = (
                "将以下标准 Agent Skill 文本概括为平台导入草稿的辅助信息。"
                "只输出 JSON：{name:string,description:string,review_items:string[]}。"
                "不得生成工具、权限、脚本、表单字段或发布指令。\n\n" + source[:60_000]
            )
            raw = str(client.complete([ChatMessage(role="system", content=prompt)]) or "")
            candidate = json.loads(raw)
            if not isinstance(candidate, dict):
                raise ValueError("AI 输出不是对象")
            return {"status": "ai_assisted", "message": "已使用 AI 生成说明与人工复核项。", **candidate}
        except Exception:
            return {"status": "deterministic_fallback", "message": "AI 增强不可用，已使用确定性映射。"}

    def commit_standard_skill_conversion(self, draft: dict[str, Any], *, actor_id: str, target_object_id: str | None = None) -> dict[str, Any]:
        if not isinstance(draft, dict):
            raise WorkbenchError("转换草稿不合法", code="CONVERSION_DRAFT_INVALID")
        if target_object_id:
            detail = self.get_object(target_object_id)
            if detail["object_type"] != draft.get("object_type"):
                raise WorkbenchError("转换草稿类型与目标对象不一致", code="CONVERSION_TARGET_TYPE_MISMATCH")
            object_id = target_object_id
            base_revision_id = (detail.get("revisions") or [{}])[0].get("revision_id")
        else:
            created = self.create_object(
                object_type=str(draft.get("object_type") or "skill"),
                object_key=str(draft.get("object_key") or ""),
                name=str(draft.get("name") or ""),
                description=str(draft.get("description") or ""),
                actor_id=actor_id,
            )
            object_id = created["object_id"]
            base_revision_id = None
        revision = self.save_revision(
            object_id,
            base_revision_id=base_revision_id,
            payload=dict(draft.get("payload") or {}),
            dependency_locks=list(draft.get("dependency_locks") or []),
            assets=list(draft.get("assets") or []),
            actor_id=actor_id,
        )
        return {"object_id": object_id, "revision": revision}

    @staticmethod
    def _standard_frontmatter(source: str) -> tuple[dict[str, Any], str]:
        if not source.startswith("---"):
            return {}, source
        parts = source.split("---", 2)
        if len(parts) < 3:
            return {}, source
        try:
            metadata = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata if isinstance(metadata, dict) else {}, parts[2].lstrip("\n")

    @staticmethod
    def _standard_runtime_contract(files: dict[str, bytes]) -> dict[str, Any]:
        for path in ("runtime_contract.json", "runtime_contract.yaml", "runtime_contract.yml"):
            if path not in files:
                continue
            try:
                value = json.loads(files[path]) if path.endswith(".json") else yaml.safe_load(files[path])
                return value if isinstance(value, dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError):
                return {}
        return {}

    @staticmethod
    def _normalize_standard_package_files(files: dict[str, bytes]) -> dict[str, bytes]:
        clean = {
            path: content
            for path, content in files.items()
            if not path.startswith("__MACOSX/")
            and not any(part in {".DS_Store", "__MACOSX"} or part.startswith("._") for part in PurePosixPath(path).parts)
        }
        entries = [path for path in clean if PurePosixPath(path).name in {"SKILL.md", "AGENT.md", "TEAM.md"}]
        if len(entries) != 1:
            return clean
        parent = PurePosixPath(entries[0]).parent
        if str(parent) in {"", "."}:
            return clean
        prefix = f"{parent.as_posix()}/"
        return {
            path[len(prefix):]: content
            for path, content in clean.items()
            if path.startswith(prefix)
        }

    @staticmethod
    def _questions_asset_to_questionnaire(files: dict[str, bytes]) -> dict[str, Any]:
        for path, content in files.items():
            if not path.startswith("assets/") or not path.endswith(".json"):
                continue
            try:
                questions = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(questions, list) or not all(isinstance(item, dict) and item.get("title") and isinstance(item.get("options"), list) for item in questions):
                continue
            fields = []
            for item in questions:
                question_id = str(item.get("id") or len(fields) + 1)
                fields.append({
                    "question_id": f"question_{question_id}",
                    "fact_key": f"question_{question_id}",
                    "label": str(item.get("title")),
                    "input_type": "single_select",
                    "required": True,
                    "options": [
                        {"label": str(option.get("text") or option.get("label") or ""), "value": f"{question_id}:{option.get('dimension') or index}"}
                        for index, option in enumerate(item.get("options") or [])
                        if isinstance(option, dict)
                    ],
                })
            return {"enabled": True, "fields": fields, "source": path, "max_fields_per_form": 1}
        return {}

    def list_objects(self, *, object_type: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = select(WorkbenchObjectRow)
            if object_type:
                if object_type not in VALID_OBJECT_TYPES:
                    raise WorkbenchError("未知对象类型")
                query = query.where(WorkbenchObjectRow.object_type == object_type)
            if not include_archived:
                query = query.where(WorkbenchObjectRow.archived.is_(False))
            rows = list(db.scalars(query.order_by(WorkbenchObjectRow.updated_at.desc())))
            result = []
            for row in rows:
                latest_revision = db.scalar(
                    select(func.max(WorkbenchRevisionRow.revision_no)).where(WorkbenchRevisionRow.object_id == row.object_id)
                ) or 0
                latest_release = db.scalar(
                    select(func.max(WorkbenchReleaseRow.release_no)).where(WorkbenchReleaseRow.object_id == row.object_id)
                ) or 0
                item = self._object_dict(row, latest_revision, latest_release)
                latest_payload = db.scalar(
                    select(WorkbenchRevisionRow.payload)
                    .where(WorkbenchRevisionRow.object_id == row.object_id)
                    .order_by(WorkbenchRevisionRow.revision_no.desc())
                    .limit(1)
                )
                if row.object_type in {"expert", "expert_team"}:
                    item["brief"] = _normalize_brief((latest_payload or {}).get("brief")) if isinstance(latest_payload, dict) else ""
                result.append(item)
            return result

    def get_object(self, object_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            row = self._require_object(db, object_id)
            revisions = list(
                db.scalars(
                    select(WorkbenchRevisionRow)
                    .where(WorkbenchRevisionRow.object_id == object_id)
                    .order_by(WorkbenchRevisionRow.revision_no.desc())
                )
            )
            releases = list(
                db.scalars(
                    select(WorkbenchReleaseRow)
                    .where(WorkbenchReleaseRow.object_id == object_id)
                    .order_by(WorkbenchReleaseRow.release_no.desc())
                )
            )
            actor_names = self._actor_display_names(
                db,
                [*(item.created_by for item in revisions), *(item.published_by for item in releases)],
            )
            detail = {
                **self._object_dict(row, revisions[0].revision_no if revisions else 0, releases[0].release_no if releases else 0),
                "revisions": [self._revision_dict(item, actor_names) for item in revisions],
                "releases": [self._release_dict(item, row, actor_names) for item in releases],
            }
            if row.object_type in {"expert", "expert_team"}:
                detail["brief"] = _normalize_brief((revisions[0].payload or {}).get("brief")) if revisions else ""
            return detail

    def list_revision_assets(self, revision_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            self._require_revision(db, revision_id)
            return self._revision_files(db, revision_id)

    def archive_object(self, object_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            row = self._require_object(db, object_id)
            row.archived = True
            row.updated_at = utc_now()
            self._audit(db, actor_id, "object.archived", row.object_type, row.object_id, {})
            db.commit()
            return {"object_id": object_id, "archived": True}

    def unarchive_object(self, object_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            row = self._require_object(db, object_id)
            row.archived = False
            row.updated_at = utc_now()
            self._audit(db, actor_id, "object.unarchived", row.object_type, row.object_id, {})
            db.commit()
            return {"object_id": object_id, "archived": False}

    def delete_object(
        self,
        object_id: str,
        *,
        confirmation_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Permanently delete an unreferenced business object and its local history."""
        with self.session_factory() as db:
            obj = self._require_object(db, object_id)
            if str(confirmation_name or "") != obj.name:
                raise WorkbenchError(
                    "确认名称与对象名称不一致",
                    code="DELETE_CONFIRMATION_MISMATCH",
                )
            blockers = self._deletion_blockers(db, obj)
            if blockers:
                raise WorkbenchConflict(
                    "对象仍被其他版本或部署引用，不能永久删除",
                    code="OBJECT_DELETE_BLOCKED",
                    details={"blockers": blockers},
                )

            revision_ids = list(
                db.scalars(select(WorkbenchRevisionRow.revision_id).where(WorkbenchRevisionRow.object_id == object_id))
            )
            suite_ids = list(
                db.scalars(select(WorkbenchEvaluationSuiteRow.suite_id).where(WorkbenchEvaluationSuiteRow.object_id == object_id))
            )
            release_count = db.scalar(
                select(func.count()).select_from(WorkbenchReleaseRow).where(WorkbenchReleaseRow.object_id == object_id)
            ) or 0
            asset_count = 0
            if revision_ids:
                asset_count = db.scalar(
                    select(func.count()).select_from(WorkbenchAssetRow).where(WorkbenchAssetRow.revision_id.in_(revision_ids))
                ) or 0
                db.execute(delete(WorkbenchEvaluationRunRow).where(WorkbenchEvaluationRunRow.revision_id.in_(revision_ids)))
                db.execute(delete(WorkbenchDebugSessionRow).where(WorkbenchDebugSessionRow.revision_id.in_(revision_ids)))
                db.execute(delete(WorkbenchReleaseRow).where(WorkbenchReleaseRow.object_id == object_id))
                db.execute(delete(WorkbenchAssetRow).where(WorkbenchAssetRow.revision_id.in_(revision_ids)))
                db.execute(delete(WorkbenchRevisionRow).where(WorkbenchRevisionRow.object_id == object_id))
            if suite_ids:
                db.execute(delete(WorkbenchEvaluationRunRow).where(WorkbenchEvaluationRunRow.suite_id.in_(suite_ids)))
                db.execute(delete(WorkbenchEvaluationSuiteRow).where(WorkbenchEvaluationSuiteRow.object_id == object_id))

            summary = {
                "object_id": object_id,
                "object_type": obj.object_type,
                "object_key": obj.object_key,
                "name": obj.name,
                "revision_count": len(revision_ids),
                "release_count": int(release_count),
                "asset_count": int(asset_count),
                "reference_policy": "latest_unarchived_release_only",
                "permanent": True,
            }
            db.delete(obj)
            self._audit(db, actor_id, "object.deleted", obj.object_type, object_id, summary)
            db.commit()
            # Production conversations bind their deployment ZIP snapshot when
            # they start.  Do not tear down the shared code shell here: an
            # active, immutable deployment may still be resolving that snapshot.
            return summary

    def migrate_object_id(
        self,
        object_id: str,
        *,
        new_object_key: str,
        confirmation_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Create a safe, unpublished successor for an object whose public ID changes.

        IDs participate in release packages and deployment snapshots.  Updating the
        object row in place would silently rewrite that history, so an ID change is
        deliberately modelled as a new object plus a copied candidate revision.
        """
        with self.session_factory() as db:
            source = self._require_object(db, object_id)
            if str(confirmation_name or "") != source.name:
                raise WorkbenchError("确认名称与对象名称不一致", code="ID_MIGRATION_CONFIRMATION_MISMATCH")
            key = self._validate_available_object_key(db, source.object_type, new_object_key)
            latest = db.scalar(
                select(WorkbenchRevisionRow)
                .where(WorkbenchRevisionRow.object_id == source.object_id)
                .order_by(WorkbenchRevisionRow.revision_no.desc())
                .limit(1)
            )
            if latest is None:
                raise WorkbenchError("对象尚无可复制的修订，请先保存配置", code="ID_MIGRATION_REVISION_REQUIRED")

            successor = WorkbenchObjectRow(
                object_id=_id("obj"), object_type=source.object_type,
                object_key=key, name=source.name, description=source.description,
                created_by=actor_id,
            )
            db.add(successor)
            db.flush()
            revision = self._copy_candidate_revision(
                db, source_revision=latest, target=successor,
                payload=copy.deepcopy(latest.payload),
                requested_locks=[{"release_id": item["release_id"]} for item in (latest.dependency_locks or [])],
                actor_id=actor_id,
                base_revision_id=None,
            )
            source_release = db.scalar(select(WorkbenchReleaseRow).where(WorkbenchReleaseRow.revision_id == latest.revision_id))
            downstream = self._reference_migration_candidates(db, source_release.release_id) if source_release else []
            result = {
                "source": self._object_dict(source, latest.revision_no, self._latest_release_no(db, source.object_id)),
                "successor": self._object_dict(successor, revision.revision_no, 0),
                "revision": self._revision_dict(revision),
                "source_release_id": source_release.release_id if source_release else None,
                "downstream": downstream,
            }
            self._audit(db, actor_id, "object.id_migrated", source.object_type, successor.object_id, {
                "source_object_id": source.object_id, "source_object_key": source.object_key,
                "new_object_key": key, "source_revision_id": latest.revision_id,
            })
            db.commit()
            return result

    def migrate_references(
        self,
        *,
        from_release_id: str,
        to_release_id: str,
        confirmation_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Create the next layer of candidate revisions for a released replacement."""
        with self.session_factory() as db:
            old_release = self._require_release(db, from_release_id)
            new_release = self._require_release(db, to_release_id)
            old_object = self._require_object(db, old_release.object_id)
            new_object = self._require_object(db, new_release.object_id)
            if str(confirmation_name or "") != old_object.name:
                raise WorkbenchError("确认名称与对象名称不一致", code="REFERENCE_MIGRATION_CONFIRMATION_MISMATCH")
            if old_object.object_type != new_object.object_type or old_release.archived or new_release.archived:
                raise WorkbenchError("替换版本必须是同类型且未归档的已发布对象", code="REFERENCE_MIGRATION_RELEASE_INVALID")

            created: list[dict[str, Any]] = []
            for source_revision, owner in self._reference_migration_candidates(db, old_release.release_id, include_rows=True):
                replacement_locks = [
                    {"release_id": new_release.release_id} if item.get("release_id") == old_release.release_id
                    else {"release_id": item["release_id"]}
                    for item in (source_revision.dependency_locks or [])
                ]
                payload = copy.deepcopy(source_revision.payload)
                if owner.object_type == "expert_team":
                    members = payload.get("members")
                    if isinstance(members, list):
                        for member in members:
                            if isinstance(member, dict) and str(member.get("expert_id") or "") == old_object.object_key:
                                member["expert_id"] = new_object.object_key
                    if str(payload.get("coordinator_expert_id") or "") == old_object.object_id:
                        payload["coordinator_expert_id"] = new_object.object_id
                revision = self._copy_candidate_revision(
                    db, source_revision=source_revision, target=owner, payload=payload,
                    requested_locks=replacement_locks, actor_id=actor_id,
                    base_revision_id=source_revision.revision_id,
                )
                created.append({"object": self._object_dict(owner, revision.revision_no, self._latest_release_no(db, owner.object_id)), "revision": self._revision_dict(revision)})
            self._audit(db, actor_id, "object.references_migrated", old_object.object_type, old_release.release_id, {
                "from_release_id": old_release.release_id, "to_release_id": new_release.release_id,
                "created_revision_count": len(created),
            })
            db.commit()
            return {"from_release_id": old_release.release_id, "to_release_id": new_release.release_id, "created": created}

    def save_revision(
        self,
        object_id: str,
        *,
        base_revision_id: str | None,
        payload: dict[str, Any],
        dependency_locks: list[dict[str, Any]] | None,
        assets: list[dict[str, Any]] | None,
        actor_id: str,
        change_summary: str = "",
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkbenchError("payload 必须是对象")
        with self.session_factory() as db:
            obj = self._require_object(db, object_id)
            if obj.object_type in {"expert", "expert_team"}:
                payload = {**payload, "brief": _normalize_brief(payload.get("brief"))}
            if obj.object_type == "skill":
                payload = {**payload, "_workbench_managed_files": True}
            latest = db.scalar(
                select(WorkbenchRevisionRow)
                .where(WorkbenchRevisionRow.object_id == object_id)
                .order_by(WorkbenchRevisionRow.revision_no.desc())
                .limit(1)
            )
            expected = latest.revision_id if latest else None
            if expected != (base_revision_id or None):
                changes: list[dict[str, Any]] = []
                if latest is not None:
                    self._diff("payload", latest.payload, payload, changes)
                raise WorkbenchConflict(
                    "配置已被其他修订更新，请比较最新版本后重试",
                    code="REVISION_BASE_CONFLICT",
                    details={
                        "submitted_base_revision_id": base_revision_id,
                        "latest_revision_id": expected,
                        "latest_revision_no": latest.revision_no if latest else 0,
                        "changes": changes,
                    },
                )
            canonical_locks = self._validate_dependencies(db, obj.object_type, payload, dependency_locks or [])
            if obj.object_type == "expert":
                routing_errors = validate_agent_skill_routing(
                    str(payload.get("rules_markdown") or ""),
                    {str(item.get("object_key") or "") for item in canonical_locks if str(item.get("object_key") or "")},
                )
                if routing_errors:
                    raise WorkbenchError(
                        "；".join(routing_errors),
                        code="AGENT_SKILL_ROUTING_INVALID",
                        details={"errors": routing_errors},
                    )
            decoded_assets = self._decode_assets(assets or [], object_type=obj.object_type)
            validation = self._validate_payload(obj.object_type, payload, canonical_locks)
            if obj.object_type == "skill":
                questionnaire_errors = _questionnaire_asset_errors(payload, decoded_assets)
                script_errors = self._script_validation_errors(decoded_assets)
                if questionnaire_errors or script_errors:
                    validation = {
                        **validation,
                        "valid": False,
                        "errors": [*validation.get("errors", []), *questionnaire_errors, *script_errors],
                    }
            revision_no = (latest.revision_no if latest else 0) + 1
            content_hash = _hash({
                "payload": payload,
                "dependency_locks": canonical_locks,
                "assets": [
                    {"relative_path": item[0], "media_type": item[1], "size_bytes": len(item[2]), "content_hash": hashlib.sha256(item[2]).hexdigest()}
                    for item in decoded_assets
                ],
            })
            revision = WorkbenchRevisionRow(
                revision_id=_id("rev"),
                object_id=object_id,
                revision_no=revision_no,
                base_revision_id=base_revision_id,
                change_summary=str(change_summary or "").strip(),
                payload=payload,
                dependency_locks=canonical_locks,
                validation=validation,
                content_hash=content_hash,
                created_by=actor_id,
            )
            db.add(revision)
            # Assets only point at a revision ID instead of declaring an ORM
            # relationship.  Flush the parent explicitly before adding assets,
            # otherwise PostgreSQL is allowed to issue the asset INSERT first
            # and rejects a perfectly valid save with a foreign-key error.
            # (SQLite's foreign keys are commonly disabled in local tests, so
            # this ordering bug was previously hidden there.)
            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                raise WorkbenchConflict(
                    "配置已被其他修订更新，请刷新后重试",
                    code="REVISION_BASE_CONFLICT",
                    details={"submitted_base_revision_id": base_revision_id},
                ) from exc
            for relative_path, media_type, content in decoded_assets:
                db.add(WorkbenchAssetRow(
                    asset_id=_id("asset"),
                    revision_id=revision.revision_id,
                    relative_path=relative_path,
                    media_type=media_type,
                    size_bytes=len(content),
                    content_hash=hashlib.sha256(content).hexdigest(),
                    content=content,
                ))
            obj.updated_at = utc_now()
            self._audit(db, actor_id, "revision.saved", obj.object_type, revision.revision_id, {"revision_no": revision_no, "change_summary": revision.change_summary})
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise WorkbenchConflict(
                    "配置已被其他修订更新，请刷新后重试",
                    code="REVISION_BASE_CONFLICT",
                    details={"submitted_base_revision_id": base_revision_id},
                ) from exc
            return self._revision_dict(revision, self._actor_display_names(db, [revision.created_by]))

    def compare_revisions(self, left_revision_id: str, right_revision_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            left = self._require_revision(db, left_revision_id)
            right = self._require_revision(db, right_revision_id)
            if left.object_id != right.object_id:
                raise WorkbenchError("只能比较同一对象的修订")
            changes: list[dict[str, Any]] = []
            self._diff("payload", left.payload, right.payload, changes)
            self._diff("dependencies", left.dependency_locks, right.dependency_locks, changes)
            return {"left": self._revision_dict(left), "right": self._revision_dict(right), "changes": changes}

    def create_debug_session(
        self,
        revision_id: str,
        *,
        baseline_release_id: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.session_factory() as db:
            revision = self._require_revision(db, revision_id)
            snapshot = self._snapshot_for_revision(db, revision)
            row = WorkbenchDebugSessionRow(
                debug_session_id=_id("dbg"),
                revision_id=revision_id,
                baseline_release_id=baseline_release_id,
                snapshot=snapshot,
                created_by=actor_id,
            )
            db.add(row)
            self._audit(db, actor_id, "debug.started", "revision", revision_id, {"debug_session_id": row.debug_session_id})
            db.commit()
            return self._debug_dict(row)

    def create_revision_test_session(self, revision_id: str, *, soul_revision_id: str | None = None, actor_id: str) -> dict[str, Any]:
        """Start an in-workbench, candidate-revision test session.

        This is intentionally separate from the full conversation test console:
        it always executes the requested immutable revision snapshot in the
        workbench itself, including unpublished Skills, Experts and Teams.
        """
        with self.session_factory() as db:
            revision = self._require_revision(db, revision_id)
            snapshot = self._snapshot_for_revision(db, revision, soul_revision_id=soul_revision_id)
            row = WorkbenchDebugSessionRow(
                debug_session_id=_id("dbg"),
                revision_id=revision_id,
                baseline_release_id=None,
                snapshot=snapshot,
                created_by=actor_id,
            )
            db.add(row)
            self._audit(
                db,
                actor_id,
                "revision_test.started",
                "revision",
                revision_id,
                {"revision_test_id": row.debug_session_id},
            )
            db.commit()
            return self._debug_dict(row)

    def run_revision_test_turn(
        self,
        debug_session_id: str,
        *,
        user_message: str,
        form_submission: dict[str, Any] | None = None,
        team_handoff_selection: dict[str, Any] | None = None,
        expert_selection: dict[str, Any] | None = None,
        actor_id: str,
        on_event=None,
    ) -> dict[str, Any]:
        """Run one manual candidate test turn without creating a chat-console session."""
        if self.orchestrator is None:
            raise WorkbenchError("当前环境未配置候选修订执行内核", code="REVISION_TEST_RUNTIME_UNAVAILABLE")
        with self.session_factory() as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                raise WorkbenchError("修订测试会话不存在", code="DEBUG_SESSION_NOT_FOUND")
            if row.status != "active":
                raise WorkbenchConflict("修订测试会话已经结束", code="DEBUG_SESSION_COMPLETED")
            snapshot = copy.deepcopy(row.snapshot or {})
            context = self._restore_revision_test_context(row, snapshot)
            # Capture every structured interaction transition before the
            # normal input adapter below so evidence retains form and card
            # state changes for this turn.
            event_count_before = len(context.event_trace)
            stream_handoff: dict[str, Any] | None = None
            # A team handoff card is an explicit authorization control. Free
            # text is allowed while it is visible, but it must stay with the
            # current coordinator; only the structured card selection below
            # may change the active expert. Keep the card active so a later
            # click remains possible.
            text_handoff_mode = ""
            if not form_submission and not team_handoff_selection and not expert_selection:
                pending_handoff = context.session_meta.get("pending_team_handoff")
                active_handoff = isinstance(pending_handoff, dict) and str(pending_handoff.get("status") or "active") == "active"
                if not active_handoff:
                    for candidate_message in reversed(context.messages):
                        if not isinstance(candidate_message, dict) or candidate_message.get("role") != "assistant":
                            continue
                        candidate_handoff = candidate_message.get("team_handoff")
                        if not isinstance(candidate_handoff, dict):
                            metadata = candidate_message.get("metadata") if isinstance(candidate_message.get("metadata"), dict) else {}
                            candidate_handoff = metadata.get("team_handoff")
                        interaction = ensure_message_interactions(candidate_message).get("team_handoff")
                        if (
                            isinstance(candidate_handoff, dict)
                            and str(candidate_handoff.get("status") or "active") == "active"
                            and isinstance(interaction, dict)
                            and interaction.get("status") == ACTIVE
                        ):
                            active_handoff = True
                            break
                if active_handoff:
                    text_handoff_mode = "team_handoff_text_continued"
                expire_active_interactions(context.messages, preserve_kinds={"team_handoff"})
            turn_started = datetime.now().timestamp()
            candidate_stream_generation: str | None = None
            candidate_stream_sequence = 0
            stream_content = ""

            def emit_candidate_state(
                status: str = "streaming",
                *,
                assistant_record: dict[str, Any] | None = None,
                error: dict[str, Any] | None = None,
            ) -> None:
                nonlocal candidate_stream_sequence
                if callable(on_event) and candidate_stream_generation:
                    state = self._candidate_sse_state(
                        context,
                        run_id=candidate_stream_generation,
                        status=status,
                        assistant_content=stream_content,
                        assistant_record=assistant_record,
                        team_handoff=stream_handoff,
                        error=error,
                    )
                    with self._active_candidate_streams_lock:
                        candidate_stream_sequence += 1
                        state["seq"] = candidate_stream_sequence
                        state["ts"] = _iso(utc_now()) or ""
                        state["elapsed_ms"] = round((datetime.now().timestamp() - turn_started) * 1000, 2)
                        active = self._active_candidate_streams.get(debug_session_id)
                        if isinstance(active, dict) and active.get("run_id") == candidate_stream_generation:
                            active["last_state"] = copy.deepcopy(state)
                    on_event("state", state)

            if callable(on_event):
                # The formal StreamingRunner assigns a generation before the
                # Runtime opens the upstream model stream.  Candidate tests
                # call the Runtime directly, so they must establish the same
                # invariant themselves.  Without it, both markers are empty
                # and the Runtime's cancellation fallback evaluates
                # ``cancelled_generation == active_generation`` as true,
                # silently cancelling the request before its first token.
                candidate_stream_generation = _id("candidate_stream")
                context.session_meta["active_stream_generation"] = candidate_stream_generation
                context.session_meta["workbench_candidate_llm_timeout_s"] = WORKBENCH_CANDIDATE_LLM_TIMEOUT_S
                context.session_meta["workbench_candidate_llm_max_tokens"] = WORKBENCH_CANDIDATE_LLM_MAX_TOKENS
                context.session_meta.pop("cancelled_stream_generation", None)
                context.session_meta["stream_cancel_check"] = lambda: (
                    context.session_meta.get("cancelled_stream_generation") == candidate_stream_generation
                )
                context.session_meta["stream_final_reply"] = True
                def reply_delta_callback(text: str) -> None:
                    nonlocal stream_content
                    stream_content += str(text or "")
                    on_event("reply_delta", {"delta": str(text or "")})
                    emit_candidate_state()

                def reasoning_delta_callback(text: str) -> None:
                    on_event("reasoning_delta", {"delta": str(text or "")})

                def status_callback(payload: Any) -> None:
                    on_event("status", payload if isinstance(payload, dict) else {})
                    emit_candidate_state()

                def team_handoff_callback(payload: Any) -> None:
                    nonlocal stream_handoff
                    stream_handoff = copy.deepcopy(payload) if isinstance(payload, dict) else None
                    on_event("team_handoff", payload if isinstance(payload, dict) else {})
                    emit_candidate_state()

                def questionnaire_callback(payload: Any) -> None:
                    on_event("questionnaire_plan", payload if isinstance(payload, dict) else {})
                    emit_candidate_state()

                context.session_meta["reply_delta_callback"] = reply_delta_callback
                context.session_meta["reasoning_delta_callback"] = reasoning_delta_callback
                context.session_meta["status_callback"] = status_callback
                context.session_meta["team_handoff_callback"] = team_handoff_callback
                context.session_meta["questionnaire_callback"] = questionnaire_callback
            message = self._revision_test_input_message(
                context,
                user_message,
                form_submission,
                team_handoff_selection,
                expert_selection,
            )
            if not message:
                raise WorkbenchError("测试问题或表单答案不能为空", code="REVISION_TEST_INPUT_REQUIRED")
            if candidate_stream_generation:
                with self._active_candidate_streams_lock:
                    self._active_candidate_streams[debug_session_id] = {
                        "run_id": candidate_stream_generation,
                        "context": context,
                        "emit_state": emit_candidate_state,
                        "last_state": None,
                    }
            emit_candidate_state()
            self._record_candidate_turn_event(
                context,
                "candidate_turn_started",
                {
                    "message_kind": text_handoff_mode or ("team_handoff_selection" if team_handoff_selection else "manual_expert_selection" if expert_selection else "fact_form_submission" if form_submission else "user_message"),
                    "root": self._candidate_root_debug(snapshot),
                },
            )
            candidate_runtime_mounts = self._mount_candidate_snapshot_skills(snapshot, context)
            try:
                assistant_message = self._execute_snapshot_message(snapshot, message, context)
                if text_handoff_mode == "team_handoff_text_continued":
                    self._record_candidate_turn_event(context, "team_handoff_text_continued", {
                        "reason": "free_text_does_not_authorize_expert_switch",
                        "switch_requires": "team_handoff_card_click",
                        "active_expert_id": str(context.session_meta.get("active_expert_id") or ""),
                    })
            except Exception as exc:
                # A candidate test is a diagnostic artifact.  Previously an
                # Expert routing failure was converted to the public generic
                # error before this transient context was persisted, so the
                # exported JSON contained neither the failed route decision
                # nor the underlying safe error category.
                failure_payload = {
                    "exception_type": type(exc).__name__,
                    "reason": str(exc)[:800],
                    "stage": "candidate_runtime_execution",
                }
                self._record_candidate_turn_event(context, "candidate_turn_failed", failure_payload)
                failed_events = copy.deepcopy(context.event_trace[event_count_before:])
                row.runtime_context = self._revision_test_context_payload(context)
                trace = list(row.trace or [])
                trace.append({
                    "turn": len([item for item in context.messages if isinstance(item, dict) and item.get("role") == "user"]),
                    "events": failed_events,
                    "debug": self._candidate_turn_debug(
                        snapshot, context, failed_events,
                        elapsed_ms=round((datetime.now().timestamp() - turn_started) * 1000, 2),
                    ),
                    "error": failure_payload,
                })
                row.trace = trace
                self._audit(
                    db, actor_id, "revision_test.turn_failed", "debug_session", debug_session_id,
                    {"revision_id": row.revision_id, "exception_type": failure_payload["exception_type"]},
                )
                db.commit()
                if candidate_stream_generation:
                    with self._active_candidate_streams_lock:
                        active = self._active_candidate_streams.get(debug_session_id)
                        if isinstance(active, dict) and active.get("run_id") == candidate_stream_generation:
                            self._active_candidate_streams.pop(debug_session_id, None)
                if self._is_model_timeout(exc):
                    raise WorkbenchError(
                        "候选修订测试的模型响应超时，请稍后重试。",
                        code="MODEL_TIMEOUT",
                        details={"timeout_s": WORKBENCH_CANDIDATE_LLM_TIMEOUT_S},
                    ) from exc
                if isinstance(exc, AgentScopeRuntimeUnavailable):
                    raise WorkbenchError(
                        "专家决策暂不可用，请稍后重试。",
                        code="EXPERT_DECISION_UNAVAILABLE",
                        details={"reason": str(exc)[:800]},
                    ) from exc
                raise
            finally:
                self._restore_candidate_snapshot_skills(candidate_runtime_mounts)
            was_stopped = bool(
                candidate_stream_generation
                and context.session_meta.get("cancelled_stream_generation") == candidate_stream_generation
            )
            if candidate_stream_generation:
                # Callback and cancellation state are request-scoped.  They
                # must not become durable candidate conversation state and
                # accidentally affect a later non-streaming turn.
                for key in (
                    "stream_final_reply",
                    "reply_delta_callback",
                    "reasoning_delta_callback",
                    "status_callback",
                    "team_handoff_callback",
                    "questionnaire_callback",
                    "stream_cancel_check",
                    "workbench_candidate_llm_timeout_s",
                    "workbench_candidate_llm_max_tokens",
                ):
                    context.session_meta.pop(key, None)
                if context.session_meta.get("active_stream_generation") == candidate_stream_generation:
                    context.session_meta.pop("active_stream_generation", None)
                if context.session_meta.get("cancelled_stream_generation") == candidate_stream_generation:
                    context.session_meta.pop("cancelled_stream_generation", None)
            # A stopped browser is allowed to start a new candidate turn
            # immediately. If that happened while a non-interruptible model
            # call was unwinding, the old worker must never overwrite the
            # newer turn's transcript or remove its live-stream registration.
            superseded_after_stop = False
            if candidate_stream_generation and was_stopped:
                with self._active_candidate_streams_lock:
                    active = self._active_candidate_streams.get(debug_session_id)
                    superseded_after_stop = not isinstance(active, dict) or active.get("run_id") != candidate_stream_generation
            if superseded_after_stop:
                return {
                    "assistant_message": assistant_message,
                    "trace": [],
                    "turn_debug": {},
                    "form": None,
                    "assistant_blocks": [],
                    "assistant_message_record": {},
                    "debug_session": self._debug_dict(row),
                }
            form = self._latest_fact_form(context)
            assistant_blocks = self._latest_assistant_blocks(context)
            assistant_projection = self._latest_assistant_projection(
                context,
                fallback_content=assistant_message,
            )
            if was_stopped:
                presentation = assistant_projection.get("presentation")
                if not isinstance(presentation, dict):
                    presentation = {}
                    assistant_projection["presentation"] = presentation
                presentation["assistant"] = {
                    **(presentation.get("assistant") if isinstance(presentation.get("assistant"), dict) else {}),
                    "content": str(assistant_projection.get("content") or assistant_message or ""),
                    "status": "stopped",
                }
                assistant_projection["generation_status"] = "cancelled"
            user_projection = self._latest_user_projection(context, fallback_content=message)
            transcript = [
                *self._refresh_candidate_transcript_interactions(row.transcript or [], context),
                user_projection,
                assistant_projection,
            ]
            row.transcript = transcript
            row.runtime_context = self._revision_test_context_payload(context)
            # `context.event_trace` is session-level runtime state.  Persist a
            # sliced, self-contained trace for every turn so the evidence can
            # be audited without duplicating earlier turns or guessing which
            # references/scripts belonged to the current user action.
            turn_events = copy.deepcopy(context.event_trace[event_count_before:])
            turn_debug = self._candidate_turn_debug(
                snapshot,
                context,
                turn_events,
                elapsed_ms=round((datetime.now().timestamp() - turn_started) * 1000, 2),
            )
            trace = list(row.trace or [])
            trace.append({
                "turn": len([item for item in transcript if item.get("role") == "user"]),
                "events": turn_events,
                "debug": turn_debug,
            })
            row.trace = trace
            self._audit(
                db,
                actor_id,
                "revision_test.turn_recorded",
                "debug_session",
                debug_session_id,
                {"revision_id": row.revision_id},
            )
            db.commit()
            if callable(on_event):
                emit_candidate_state("stopped" if was_stopped else "completed", assistant_record=assistant_projection)
                on_event("message", {"assistant_message": assistant_projection})
                on_event("blocks", {"blocks": assistant_blocks, "form": form})
            if candidate_stream_generation:
                with self._active_candidate_streams_lock:
                    active = self._active_candidate_streams.get(debug_session_id)
                    if isinstance(active, dict) and active.get("run_id") == candidate_stream_generation:
                        self._active_candidate_streams.pop(debug_session_id, None)
            return {
                "assistant_message": assistant_message,
                "trace": turn_events,
                "turn_debug": turn_debug,
                "form": form,
                "assistant_blocks": assistant_blocks,
                "assistant_message_record": assistant_projection,
                "debug_session": self._debug_dict(row),
            }

    @staticmethod
    def _is_model_timeout(exc: BaseException) -> bool:
        """Classify transport timeouts without exposing upstream diagnostics."""
        text = f"{type(exc).__name__}: {exc}".lower()
        return "timeout" in text or "timed out" in text

    def stop_revision_test_turn(
        self,
        debug_session_id: str,
        *,
        run_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """Request cancellation for a live candidate SSE turn.

        The original stream remains responsible for persisting its partial
        transcript and emitting its final ``state``/``done`` pair.  This
        endpoint merely sets the request-scoped Runtime cancellation marker
        and immediately emits a stopped snapshot, matching formal chat's
        user-visible stop semantics without coupling candidates to production
        sessions.
        """
        expected_run_id = str(run_id or "").strip()
        if not expected_run_id:
            raise WorkbenchError("缺少候选测试运行 ID", code="REVISION_TEST_RUN_ID_REQUIRED")
        with self._active_candidate_streams_lock:
            active = self._active_candidate_streams.get(debug_session_id)
            if not isinstance(active, dict) or active.get("run_id") != expected_run_id:
                raise WorkbenchConflict("候选修订当前没有该运行中的回复", code="REVISION_TEST_RUN_NOT_ACTIVE")
            context = active.get("context")
            if not isinstance(context, SessionContext):
                raise WorkbenchConflict("候选修订当前没有该运行中的回复", code="REVISION_TEST_RUN_NOT_ACTIVE")
            context.session_meta["cancelled_stream_generation"] = expected_run_id
            emit_state = active.get("emit_state")
            last_state = copy.deepcopy(active.get("last_state")) if isinstance(active.get("last_state"), dict) else None
        if callable(emit_state):
            emit_state("stopped")
        with self._active_candidate_streams_lock:
            current = self._active_candidate_streams.get(debug_session_id)
            if isinstance(current, dict) and current.get("run_id") == expected_run_id:
                last_state = copy.deepcopy(current.get("last_state")) if isinstance(current.get("last_state"), dict) else last_state
        return {
            "run_id": expected_run_id,
            "status": "stopped",
            "state": last_state,
        }

    @staticmethod
    def _revision_test_input_message(
        context,
        user_message: str,
        form_submission: dict[str, Any] | None,
        team_handoff_selection: dict[str, Any] | None = None,
        expert_selection: dict[str, Any] | None = None,
    ) -> str:
        if team_handoff_selection:
            return WorkbenchService._apply_candidate_team_handoff(context, team_handoff_selection)
        if expert_selection:
            return WorkbenchService._apply_candidate_manual_expert_selection(
                context,
                expert_selection,
                user_message,
            )
        if not form_submission:
            return str(user_message or "").strip()
        source_message_id = str(form_submission.get("source_message_id") or "")
        source = WorkbenchService._candidate_interaction_source(
            context,
            source_message_id=source_message_id,
            interaction_prefix="fact_form:",
        )
        current = WorkbenchService._fact_form_from_message(source)
        payload = current.get("payload") if isinstance(current, dict) and isinstance(current.get("payload"), dict) else {}
        form_id = str(form_submission.get("form_id") or "")
        if not form_id or form_id != str(payload.get("form_id") or ""):
            raise WorkbenchError("表单已失效，请以最新回复为准", code="REVISION_TEST_FORM_STALE")
        interaction_id = f"fact_form:{form_id}"
        state = ensure_message_interactions(source).get(interaction_id, {})
        if state.get("status") != ACTIVE:
            raise WorkbenchError("表单已失效，请以最新回复为准", code="REVISION_TEST_FORM_STALE")
        values = form_submission.get("values") if isinstance(form_submission.get("values"), dict) else {}
        lines = []
        missing_required: list[str] = []
        for field in payload.get("fields") if isinstance(payload.get("fields"), list) else []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("fact_key") or "")
            value = values.get(key)
            if value in (None, "", []):
                if field.get("required", True):
                    missing_required.append(str(field.get("label") or key))
                continue
            rendered = "、".join(str(item) for item in value) if isinstance(value, list) else str(value)
            lines.append(f"{field.get('label') or key}: {rendered}")
        if missing_required:
            raise WorkbenchError(
                f"请先填写必填项：{'、'.join(missing_required)}",
                code="REVISION_TEST_FORM_REQUIRED",
            )
        update_interaction(source, interaction_id, status=SUBMITTED, submitted_fact_keys=[
            str(key) for key, value in values.items() if value not in (None, "", [])
        ])
        return "\n".join(lines).strip()

    @staticmethod
    def _apply_candidate_team_handoff(context: SessionContext, selection: dict[str, Any]) -> str:
        """Confirm a team handoff without touching a formal user session."""
        handoff_id = str(selection.get("handoff_id") or "")
        target_expert_id = str(selection.get("target_expert_id") or "")
        source = WorkbenchService._candidate_interaction_source(
            context,
            source_message_id=str(selection.get("source_message_id") or ""),
            interaction_prefix="team_handoff",
            missing_code="REVISION_TEST_HANDOFF_NOT_FOUND",
        )
        handoff = source.get("team_handoff")
        if not isinstance(handoff, dict) or str(handoff.get("handoff_id") or "") != handoff_id:
            raise WorkbenchError("专家转交已失效，请以最新回复为准", code="REVISION_TEST_HANDOFF_STALE")
        if str(handoff.get("status") or "active") != "active":
            raise WorkbenchError("该专家转交已经处理", code="REVISION_TEST_HANDOFF_STALE")
        if ensure_message_interactions(source).get("team_handoff", {}).get("status") != ACTIVE:
            raise WorkbenchError("该专家转交已经处理", code="REVISION_TEST_HANDOFF_STALE")
        candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
        candidate = next(
            (item for item in candidates if isinstance(item, dict) and str(item.get("expert_id") or "") == target_expert_id),
            None,
        )
        if candidate is None:
            raise WorkbenchError("目标专家不在本次转交候选中", code="REVISION_TEST_HANDOFF_FORBIDDEN")
        team_id = str(context.session_meta.get("expert_team_id") or "")
        if not team_id or team_id != str(handoff.get("team_id") or ""):
            raise WorkbenchError("专家转交不属于当前候选专家团", code="REVISION_TEST_HANDOFF_FORBIDDEN")
        requested_team_id = str(selection.get("team_id") or "")
        if requested_team_id and requested_team_id != team_id:
            raise WorkbenchError("专家转交不属于当前候选专家团", code="REVISION_TEST_HANDOFF_FORBIDDEN")
        snapshot = context.session_meta.get("configuration_snapshot")
        entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        team_entry = next(
            (item for item in entries if isinstance(item, dict) and item.get("object_type") == "expert_team" and str(item.get("object_key") or "") == team_id),
            None,
        )
        allowed_experts = {
            str(item.get("object_key") or "")
            for item in (team_entry or {}).get("dependency_locks", [])
            if isinstance(item, dict) and item.get("object_type") == "expert"
        }
        if target_expert_id not in allowed_experts:
            raise WorkbenchError("目标专家未锁定在当前候选专家团中", code="REVISION_TEST_HANDOFF_FORBIDDEN")
        from_expert_id = str(context.session_meta.get("active_expert_id") or "")
        context.abandon_active_interactions_for_expert_change(
            reason="candidate_confirm_team_handoff",
            from_expert_id=from_expert_id,
            target_expert_id=target_expert_id,
        )
        handoff["status"] = "selected"
        handoff["selected_target_expert_id"] = target_expert_id
        update_interaction(source, "team_handoff", status=SELECTED, selected_target_skill_id=target_expert_id)
        metadata = source.setdefault("metadata", {})
        if isinstance(metadata, dict) and isinstance(metadata.get("team_handoff"), dict):
            metadata["team_handoff"].update({"status": "selected", "selected_target_expert_id": target_expert_id})
        context.session_meta["active_expert_id"] = target_expert_id
        context.session_meta["expert_id"] = target_expert_id
        context.session_meta["expert_selection_source"] = "candidate_handoff_card"
        context.session_meta["_candidate_branch_version"] = int(
            context.session_meta.get("_candidate_branch_version") or 1
        ) + 1
        context.session_meta.pop("pending_team_handoff", None)
        context.session_meta.pop("pending_team_handoff_intent", None)
        context.session_meta.pop("expert_requested_skill_id", None)
        context.interaction_state["active_skill"] = ""
        runtime_state = context.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state["active_skill_id"] = ""
        source_user_message = ""
        source_index = context.messages.index(source)
        for item in reversed(context.messages[:source_index]):
            if isinstance(item, dict) and item.get("role") == "user":
                source_user_message = str(item.get("content") or "").strip()
                break
        mention_name = str(candidate.get("mention_name") or candidate.get("name") or target_expert_id)
        text_confirmation = str(selection.get("text_confirmation") or "").strip()
        source_kind = "team_handoff_ack" if text_confirmation else "team_handoff"
        context.session_meta["team_handoff_visible_user_message"] = text_confirmation or f"@{mention_name}"
        # The Runtime writes this as a display/audit event.  It is deliberately
        # excluded from later model history; the target expert receives the
        # original question and structured handoff context below instead.
        context.session_meta["team_handoff_visible_user_message_type"] = "team_handoff_confirmation"
        context.session_meta["team_handoff_visible_user_message_metadata"] = {
            "source_message_id": str(selection.get("source_message_id") or ""),
            "target_expert_id": target_expert_id,
            "expert_team_id": team_id,
            "source": source_kind,
        }
        # Match the formal handoff path: the authoritative, server-validated
        # selection is passed structurally to the Expert Runtime.  Do not put
        # internal object IDs into a synthetic user message; it both diverges
        # from formal behaviour and can trigger an irrelevant local lexicon
        # match during degraded moderation.
        context.session_meta["team_member_switch"] = {
            "source": source_kind,
            "team_id": team_id,
            "from_expert_id": from_expert_id,
            "target_expert_id": target_expert_id,
            "mention_name": mention_name,
            "visible_user_message": text_confirmation or f"@{mention_name}",
            "source_user_message": source_user_message,
            "coordinator_reason": str(handoff.get("reason") or ""),
            "source_message_id": str(selection.get("source_message_id") or ""),
        }
        return source_user_message or "请根据当前上下文继续协助用户。"

    @staticmethod
    def _apply_candidate_recovered_team_handoff(
        context: SessionContext, handoff: dict[str, Any], candidate: dict[str, Any], acknowledgement: str,
    ) -> None:
        """Apply a persisted intent when no presentation card was written."""
        team_id = str(context.session_meta.get("expert_team_id") or "")
        target_expert_id = str(candidate.get("expert_id") or "")
        snapshot = context.session_meta.get("configuration_snapshot")
        entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        team_entry = next((item for item in entries if isinstance(item, dict) and item.get("object_type") == "expert_team" and str(item.get("object_key") or "") == team_id), None)
        allowed = {str(item.get("object_key") or "") for item in (team_entry or {}).get("dependency_locks", []) if isinstance(item, dict) and item.get("object_type") == "expert"}
        if not team_id or str(handoff.get("team_id") or "") != team_id or target_expert_id not in allowed:
            raise WorkbenchError("待恢复的专家转交已失效", code="REVISION_TEST_HANDOFF_STALE")
        from_expert_id = str(context.session_meta.get("active_expert_id") or "")
        if from_expert_id != target_expert_id:
            context.abandon_active_interactions_for_expert_change(
                reason="candidate_confirm_recovered_team_handoff",
                from_expert_id=from_expert_id,
                target_expert_id=target_expert_id,
            )
        mention_name = str(candidate.get("mention_name") or candidate.get("name") or target_expert_id)
        context.session_meta.update({"active_expert_id": target_expert_id, "expert_id": target_expert_id, "expert_selection_source": "candidate_handoff_text_confirmation"})
        context.session_meta["_candidate_branch_version"] = int(
            context.session_meta.get("_candidate_branch_version") or 1
        ) + 1
        context.session_meta.pop("pending_team_handoff", None)
        context.session_meta.pop("pending_team_handoff_intent", None)
        context.session_meta["team_handoff_visible_user_message"] = acknowledgement
        context.session_meta["team_handoff_visible_user_message_type"] = "team_handoff_confirmation"
        context.session_meta["team_handoff_visible_user_message_metadata"] = {"source": "team_handoff_ack_recovered", "target_expert_id": target_expert_id, "expert_team_id": team_id}
        context.session_meta["team_member_switch"] = {
            "source": "team_handoff_ack_recovered", "team_id": team_id,
            "from_expert_id": from_expert_id, "target_expert_id": target_expert_id,
            "mention_name": mention_name, "visible_user_message": acknowledgement,
            "source_user_message": str(handoff.get("original_user_message") or ""),
            "coordinator_reason": str(handoff.get("reason") or ""),
            "source_message_id": str(handoff.get("source_message_id") or ""),
        }

    @staticmethod
    def _apply_candidate_manual_expert_selection(
        context: SessionContext,
        selection: dict[str, Any],
        user_message: str,
    ) -> str:
        """Apply a formal-style ``@专家`` toolbar selection to a candidate team.

        The target is always resolved from the immutable candidate snapshot;
        clients may only choose a member already locked by that team.  This is
        a session-local routing action, not a child/Profile operation.
        """
        content = str(user_message or "").strip()
        if not content:
            raise WorkbenchError("请选择专家后输入测试问题", code="REVISION_TEST_EXPERT_CONTENT_REQUIRED")
        target_expert_id = str(selection.get("target_expert_id") or "").strip()
        if not target_expert_id:
            raise WorkbenchError("请选择要 @ 的专家", code="REVISION_TEST_EXPERT_REQUIRED")
        snapshot = context.session_meta.get("configuration_snapshot")
        entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        team_id = str(context.session_meta.get("expert_team_id") or "")
        team_entry = next(
            (
                item
                for item in entries
                if isinstance(item, dict)
                and item.get("object_type") == "expert_team"
                and str(item.get("object_key") or "") == team_id
            ),
            None,
        )
        if team_entry is None:
            raise WorkbenchError("只有候选专家团测试可手动 @ 团内专家", code="REVISION_TEST_EXPERT_SELECTION_UNAVAILABLE")
        locks = team_entry.get("dependency_locks") if isinstance(team_entry.get("dependency_locks"), list) else []
        allowed_experts = {
            str(item.get("object_key") or "")
            for item in locks
            if isinstance(item, dict) and item.get("object_type") == "expert"
        }
        if target_expert_id not in allowed_experts:
            raise WorkbenchError("目标专家不属于当前候选专家团", code="REVISION_TEST_EXPERT_FORBIDDEN")
        payload = team_entry.get("payload") if isinstance(team_entry.get("payload"), dict) else {}
        members = payload.get("members") if isinstance(payload.get("members"), list) else []
        configured_member = next(
            (item for item in members if isinstance(item, dict) and str(item.get("expert_id") or "") == target_expert_id),
            {},
        )
        expert_entry = next(
            (
                item
                for item in entries
                if isinstance(item, dict)
                and item.get("object_type") == "expert"
                and str(item.get("object_key") or "") == target_expert_id
            ),
            {},
        )
        mention_name = str(
            configured_member.get("mention_name")
            or configured_member.get("name")
            or expert_entry.get("name")
            or target_expert_id
        ).strip()
        from_expert_id = str(context.session_meta.get("active_expert_id") or "")
        if from_expert_id != target_expert_id:
            context.abandon_active_interactions_for_expert_change(
                reason="candidate_manual_expert_selection",
                from_expert_id=from_expert_id,
                target_expert_id=target_expert_id,
            )
        context.session_meta["active_expert_id"] = target_expert_id
        context.session_meta["expert_id"] = target_expert_id
        context.session_meta["expert_selection_source"] = "candidate_manual_at"
        context.session_meta["_candidate_branch_version"] = int(
            context.session_meta.get("_candidate_branch_version") or 1
        ) + 1
        context.session_meta.pop("pending_team_handoff", None)
        context.session_meta.pop("expert_requested_skill_id", None)
        context.session_meta["team_member_switch"] = {
            "source": "toolbar",
            "team_id": team_id,
            "from_expert_id": from_expert_id,
            "target_expert_id": target_expert_id,
            "mention_name": mention_name,
            "content": content,
            "visible_user_message": f"@{mention_name} {content}",
        }
        context.interaction_state["active_skill"] = ""
        runtime_state = context.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state["active_skill_id"] = ""
        return content

    def _restore_revision_test_context(self, row: WorkbenchDebugSessionRow, snapshot: dict[str, Any]) -> SessionContext:
        context = SessionContext(
            session_id=f"revision_test_{row.debug_session_id}",
            # Candidate tests keep Runtime context and memory per debug
            # session, but never identify as a formal user or one of that
            # user's children.  The anonymous identity is deliberately
            # derived from this immutable debug-session id.
            user_id=f"workbench-candidate-{row.debug_session_id}",
            profile_id=None,
            profile_name=None,
        )
        saved = row.runtime_context if isinstance(row.runtime_context, dict) else {}
        context.messages = list(saved.get("messages") or [])
        context.known_facts = KnownFacts.model_validate(saved.get("known_facts") or {})
        context.shared_facts = KnownFacts.model_validate(saved.get("shared_facts") or {})
        context.profile_facts = KnownFacts.model_validate(saved.get("profile_facts") or {})
        context.session_facts = KnownFacts.model_validate(saved.get("session_facts") or {})
        context.skill_states = dict(saved.get("skill_states") or {})
        context.candidate_paths = list(saved.get("candidate_paths") or [])
        context.interaction_state = dict(saved.get("interaction_state") or {})
        context.risk_signals = list(saved.get("risk_signals") or [])
        context.event_trace = list(saved.get("event_trace") or [])
        context.last_fact_changes = list(saved.get("last_fact_changes") or [])
        context.session_meta.update(dict(saved.get("session_meta") or {}))
        context.session_meta.setdefault("_candidate_branch_version", 1)
        self._configure_snapshot_context(context, snapshot)
        return context

    @staticmethod
    def _revision_test_context_payload(context: SessionContext) -> dict[str, Any]:
        return {
            "messages": copy.deepcopy(context.messages),
            "known_facts": context.known_facts.model_dump(mode="json"),
            "shared_facts": context.shared_facts.model_dump(mode="json"),
            "profile_facts": context.profile_facts.model_dump(mode="json"),
            "session_facts": context.session_facts.model_dump(mode="json"),
            "skill_states": copy.deepcopy(context.skill_states),
            "candidate_paths": copy.deepcopy(context.candidate_paths),
            "interaction_state": copy.deepcopy(context.interaction_state),
            "risk_signals": copy.deepcopy(context.risk_signals),
            "event_trace": copy.deepcopy(context.event_trace[-200:]),
            "last_fact_changes": copy.deepcopy(context.last_fact_changes),
            "session_meta": {
                key: copy.deepcopy(value)
                for key, value in context.session_meta.items()
                if key not in {"configuration_snapshot", "soul_context"} and not callable(value)
            },
        }

    def _candidate_expert_state(self, context: SessionContext) -> tuple[dict[str, Any], dict[str, Any]]:
        """Project an anonymous candidate Runtime state into the public v2 shape."""
        meta = context.session_meta if isinstance(context.session_meta, dict) else {}
        team_id = str(meta.get("expert_team_id") or "").strip()
        expert_id = str(meta.get("active_expert_id") or meta.get("expert_id") or "").strip()
        snapshot = meta.get("configuration_snapshot") if isinstance(meta.get("configuration_snapshot"), dict) else {}
        entries = snapshot.get("entries") if isinstance(snapshot.get("entries"), list) else []

        def snapshot_entry(object_type: str, object_key: str) -> dict[str, Any]:
            return next(
                (
                    item for item in entries
                    if isinstance(item, dict)
                    and item.get("object_type") == object_type
                    and str(item.get("object_key") or "") == object_key
                ),
                {},
            )

        expert_entry = snapshot_entry("expert", expert_id)
        team_entry = snapshot_entry("expert_team", team_id)
        registry = getattr(self.orchestrator, "expert_registry", None)
        definition = registry.get(expert_id) if expert_id and registry is not None else None
        team_registry = getattr(self.orchestrator, "expert_team_registry", None)
        team = team_registry.get(team_id) if team_id and team_registry is not None else None
        # Candidate revisions may not exist in the deployed registries. Their
        # immutable snapshot is the public source of names, so never expose a
        # technical ID merely because the draft has not been published.
        mode = "team" if team_id else "single" if expert_id else "none"
        member = team.member_for_expert(expert_id) if team is not None else None
        expert_name = str(expert_entry.get("name") or getattr(definition, "name", "") or expert_id)
        team_name = str(team_entry.get("name") or getattr(team, "name", "") or team_id)
        expert = {
            "mode": mode,
            "team": {
                "team_id": team_id,
                "name": team_name,
                "coordinator_expert_id": str(
                    (team_entry.get("payload") or {}).get("coordinator_expert_id")
                    or getattr(team, "coordinator_expert_id", "")
                    or ""
                ),
            } if team_id else {},
            "active": {
                "expert_id": expert_id,
                "name": expert_name,
                "mention_name": str(getattr(member, "mention_name", "") or ""),
                "is_coordinator": bool(
                    team_id
                    and expert_id == str(
                        (team_entry.get("payload") or {}).get("coordinator_expert_id")
                        or getattr(team, "coordinator_expert_id", "")
                        or ""
                    )
                ),
            } if expert_id else {},
            "activation": {
                "source": "candidate_snapshot",
                "is_default": bool(team_id and expert_id == str(getattr(team, "coordinator_expert_id", "") or "")),
                "selection_source": str(meta.get("expert_selection_source") or ""),
            } if mode != "none" else {},
            "transition": {},
        }
        expert_context = {
            "expert_team_id": team_id or None,
            "expert_id": expert_id or None,
            "branch_version": int(meta.get("_candidate_branch_version") or 1),
        }
        return expert, expert_context

    def _candidate_sse_state(
        self,
        context: SessionContext,
        *,
        run_id: str,
        status: str,
        assistant_content: str = "",
        assistant_record: dict[str, Any] | None = None,
        team_handoff: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the fixed SSE v2 presentation shape for a candidate turn."""
        state = empty_message_state(session_id=context.session_id, run_id=run_id)
        expert, expert_context = self._candidate_expert_state(context)
        record = assistant_record if isinstance(assistant_record, dict) else {}
        presentation = record.get("presentation") if isinstance(record.get("presentation"), dict) else {}
        active_skill = build_skill_display(
            context,
            runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
        )
        questionnaire_plan: dict[str, Any] = {}
        runtime_state = context.skill_states.get("runtime_state") if isinstance(context.skill_states, dict) else None
        skill_id = str(active_skill.get("skill_id") or "")
        registry = getattr(self.orchestrator, "runtime_registry", None)
        bundle = registry.get(skill_id) if registry is not None and skill_id else None
        if bundle is not None and runtime_state is not None:
            questionnaire_plan = questionnaire_plan_summary(bundle, runtime_state)
        state.update({
            "profile_id": None,
            "profile_name": None,
            "context_scope": "unbound",
            "context_label": "未绑定孩子",
            "context_switched": False,
            "context_notice": {},
            "branch_version": expert_context["branch_version"],
            "profile_context_status": "unbound",
            "status": status,
            "message_id": str(record.get("message_id") or "") or None,
            "assistant": {
                "content": str(record.get("content") or assistant_content or ""),
                "status": status,
            },
            "intent": copy.deepcopy(presentation.get("intent") or {}),
            "form": copy.deepcopy(presentation.get("form") or {}),
            "path_options": copy.deepcopy(presentation.get("path_options") or {}),
            "skill_rooms": copy.deepcopy(presentation.get("skill_rooms") or []),
            "team_handoff": copy.deepcopy(team_handoff or presentation.get("team_handoff") or {}),
            "expert": expert,
            "expert_context": expert_context,
            "skill_transition": copy.deepcopy(presentation.get("skill_transition") or {}),
            "session": copy.deepcopy(presentation.get("session") or {
                "active_skill": {
                    "skill_id": active_skill.get("skill_id"),
                    "title": active_skill.get("active_skill_label"),
                    "brief": active_skill.get("brief", ""),
                    "info": active_skill.get("info", ""),
                    "description": active_skill.get("description", ""),
                    "scene_name": active_skill.get("scene_name", ""),
                },
            }),
            "risk": copy.deepcopy(presentation.get("risk") or state["risk"]),
            "error": copy.deepcopy(error or presentation.get("error") or state["error"]),
            "questionnaire": questionnaire_plan,
        })
        return state

    @staticmethod
    def _latest_fact_form(context) -> dict[str, Any] | None:
        for message in reversed(getattr(context, "messages", []) or []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for block in message.get("blocks") or []:
                if isinstance(block, dict) and block.get("type") == "fact_form":
                    return copy.deepcopy(block)
        return None

    @staticmethod
    def _fact_form_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
        for block in message.get("blocks") or []:
            if isinstance(block, dict) and block.get("type") == "fact_form":
                return copy.deepcopy(block)
        return None

    @staticmethod
    def _candidate_interaction_source(
        context: SessionContext,
        *,
        source_message_id: str,
        interaction_prefix: str,
        missing_code: str = "REVISION_TEST_FORM_STALE",
    ) -> dict[str, Any]:
        """Resolve an actionable candidate message exactly like formal chat.

        Old candidate transcripts did not store message ids; they deliberately
        cannot acquire an action retroactively. New candidate turns always
        persist the Runtime message id and interaction state.
        """
        if not source_message_id:
            raise WorkbenchError("交互已失效，请以最新回复为准", code=missing_code)
        source = next(
            (
                item for item in context.messages
                if isinstance(item, dict)
                and item.get("role") == "assistant"
                and str(item.get("message_id") or "") == source_message_id
            ),
            None,
        )
        if source is None:
            raise WorkbenchError("交互已失效，请以最新回复为准", code=missing_code)
        interactions = ensure_message_interactions(source)
        if interaction_prefix == "fact_form:":
            if not any(key.startswith(interaction_prefix) and value.get("status") == ACTIVE for key, value in interactions.items() if isinstance(value, dict)):
                raise WorkbenchError("表单已失效，请以最新回复为准", code="REVISION_TEST_FORM_STALE")
        elif not isinstance(interactions.get(interaction_prefix), dict):
            raise WorkbenchError("没有可确认的专家转交", code=missing_code)
        return source

    @staticmethod
    def _latest_assistant_blocks(context) -> list[dict[str, Any]]:
        for message in reversed(getattr(context, "messages", []) or []):
            if isinstance(message, dict) and message.get("role") == "assistant":
                blocks = copy.deepcopy([item for item in message.get("blocks") or [] if isinstance(item, dict)])
                handoff = message.get("team_handoff")
                if not isinstance(handoff, dict):
                    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                    handoff = metadata.get("team_handoff")
                if (
                    isinstance(handoff, dict)
                    and isinstance(handoff.get("candidates"), list)
                    and not any(item.get("type") == "team_handoff" for item in blocks)
                ):
                    blocks.append({"type": "team_handoff", "payload": copy.deepcopy(handoff)})
                return blocks
        return []

    @staticmethod
    def _latest_assistant_projection(context: SessionContext, *, fallback_content: str) -> dict[str, Any]:
        """Persist the public Runtime message envelope in candidate evidence."""
        for message in reversed(getattr(context, "messages", []) or []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            return WorkbenchService._assistant_message_projection(message, latest=True, fallback_content=fallback_content)
        return {
            "role": "assistant",
            "content": fallback_content,
            "message_id": "",
            "message_type": "",
            "blocks": [],
            "team_handoff": None,
            "interaction_states": {},
            "presentation": {},
            "created_at": _iso(utc_now()),
        }

    @staticmethod
    def _assistant_message_projection(
        message: dict[str, Any],
        *,
        latest: bool,
        fallback_content: str = "",
    ) -> dict[str, Any]:
        ensure_message_interactions(message)
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        handoff = message.get("team_handoff") if isinstance(message.get("team_handoff"), dict) else metadata.get("team_handoff")
        blocks = copy.deepcopy([item for item in message.get("blocks") or [] if isinstance(item, dict)])
        if (
            isinstance(handoff, dict)
            and isinstance(handoff.get("candidates"), list)
            and not any(item.get("type") == "team_handoff" for item in blocks)
        ):
            blocks.append({"type": "team_handoff", "payload": copy.deepcopy(handoff)})
        return {
            "role": "assistant",
            "content": str(message.get("content") or fallback_content or ""),
            "message_id": str(message.get("message_id") or ""),
            "message_type": str(message.get("message_type") or metadata.get("message_type") or ""),
            "blocks": blocks,
            "team_handoff": copy.deepcopy(handoff) if isinstance(handoff, dict) else None,
            "interaction_states": copy.deepcopy(message.get("interaction_states") or metadata.get("interaction_states") or {}),
            "presentation": presentation_from_message(message, latest=latest),
            "created_at": str(message.get("created_at") or _iso(utc_now()) or ""),
        }

    @staticmethod
    def _refresh_candidate_transcript_interactions(
        transcript: list[dict[str, Any]],
        context: SessionContext,
    ) -> list[dict[str, Any]]:
        by_message_id = {
            str(message.get("message_id") or ""): message
            for message in context.messages
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("message_id")
        }
        refreshed: list[dict[str, Any]] = []
        for item in transcript:
            message_id = str(item.get("message_id") or "") if isinstance(item, dict) else ""
            source = by_message_id.get(message_id)
            refreshed.append(
                WorkbenchService._assistant_message_projection(source, latest=False)
                if source is not None
                else copy.deepcopy(item)
            )
        return refreshed

    @staticmethod
    def _latest_user_projection(context: SessionContext, *, fallback_content: str) -> dict[str, Any]:
        for message in reversed(getattr(context, "messages", []) or []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            return {
                "role": "user",
                "content": str(message.get("content") or fallback_content or ""),
                "message_id": str(message.get("message_id") or ""),
                "message_type": str(message.get("message_type") or metadata.get("message_type") or ""),
                "metadata": copy.deepcopy(metadata),
                "blocks": [],
                "interaction_states": {},
                "created_at": str(message.get("created_at") or _iso(utc_now()) or ""),
            }
        return {
            "role": "user",
            "content": fallback_content,
            "message_id": "",
            "message_type": "",
            "metadata": {},
            "blocks": [],
            "interaction_states": {},
            "created_at": _iso(utc_now()),
        }

    def create_formal_chat_session(
        self,
        *,
        object_id: str,
        revision_id: str,
        release_id: str,
        soul_revision_id: str | None = None,
        actor_id: str,
        team_only: bool = False,
    ) -> dict[str, Any]:
        """Create the long-chat bridge for one explicit published configuration."""
        with self.session_factory() as db:
            revision = self._require_revision(db, revision_id)
            obj = self._require_object(db, object_id)
            if revision.object_id != obj.object_id:
                raise WorkbenchError("修订不属于当前调试对象", code="REVISION_OBJECT_MISMATCH")
            allowed = {"expert_team"} if team_only else {"expert", "expert_team"}
            if obj.object_type not in allowed:
                raise WorkbenchError("长对话测试台仅用于已发布的专家或专家团版本", code="FORMAL_CHAT_REQUIRES_AGENT")
            release = self._require_revision_release(db, revision, release_id)
            if release.archived:
                raise WorkbenchError("已归档发布版本不能进入长对话测试台", code="FORMAL_CHAT_RELEASE_ARCHIVED")
            row = WorkbenchDebugSessionRow(
                debug_session_id=_id("dbg"),
                revision_id=revision_id,
                baseline_release_id=release.release_id,
                snapshot=self._snapshot_for_revision(db, revision, soul_revision_id=soul_revision_id),
                created_by=actor_id,
            )
            db.add(row)
            self._audit(
                db,
                actor_id,
                "formal_chat.started",
                "release",
                release.release_id,
                {"debug_session_id": row.debug_session_id, "revision_id": revision_id},
            )
            db.commit()
            return self._debug_dict(row)

    def create_formal_team_chat_session(self, revision_id: str, *, actor_id: str) -> dict[str, Any]:
        """Compatibility entrypoint for existing Expert Team callers."""
        with self.session_factory() as db:
            revision = self._require_revision(db, revision_id)
            obj = self._require_object(db, revision.object_id)
            release = db.scalar(select(WorkbenchReleaseRow).where(WorkbenchReleaseRow.revision_id == revision_id))
            if release is None:
                raise WorkbenchError("请先发布该专家团修订后再进入长对话测试台", code="FORMAL_TEAM_CHAT_REQUIRES_RELEASE")
            object_id = obj.object_id
            release_id = release.release_id
        return self.create_formal_chat_session(
            object_id=object_id,
            revision_id=revision_id,
            release_id=release_id,
            soul_revision_id=None,
            actor_id=actor_id,
            team_only=True,
        )

    def debug_configuration_snapshot(self, debug_session_id: str) -> dict[str, Any]:
        """Resolve the immutable candidate snapshot used by the shared SSE chat runtime."""
        with self.session_factory() as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                raise WorkbenchError("调试会话不存在", code="DEBUG_SESSION_NOT_FOUND")
            return copy.deepcopy(row.snapshot or {})

    def append_debug_turn(
        self,
        debug_session_id: str,
        *,
        user_message: str,
        assistant_message: str,
        trace: list[dict[str, Any]] | None,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.session_factory() as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                raise WorkbenchError("调试会话不存在", code="DEBUG_SESSION_NOT_FOUND")
            if row.status != "active":
                raise WorkbenchConflict("调试会话已经结束", code="DEBUG_SESSION_COMPLETED")
            transcript = list(row.transcript or [])
            transcript.extend([
                {"role": "user", "content": str(user_message), "created_at": _iso(utc_now())},
                {"role": "assistant", "content": str(assistant_message), "created_at": _iso(utc_now())},
            ])
            row.transcript = transcript
            row.trace = [*(row.trace or []), *(trace or [])]
            self._audit(db, actor_id, "debug.turn_recorded", "debug_session", debug_session_id, {})
            db.commit()
            return self._debug_dict(row)

    def complete_debug_session(self, debug_session_id: str, *, conclusion: str, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                raise WorkbenchError("调试会话不存在", code="DEBUG_SESSION_NOT_FOUND")
            row.status = "completed"
            row.conclusion = str(conclusion or "").strip()
            row.completed_at = utc_now()
            self._audit(db, actor_id, "debug.completed", "revision", row.revision_id, {"debug_session_id": debug_session_id})
            db.commit()
            return self._debug_dict(row)

    def list_debug_sessions(
        self,
        *,
        object_id: str | None = None,
        revision_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = (
                select(WorkbenchDebugSessionRow, WorkbenchRevisionRow, WorkbenchObjectRow, WorkbenchReleaseRow)
                .join(WorkbenchRevisionRow, WorkbenchRevisionRow.revision_id == WorkbenchDebugSessionRow.revision_id)
                .join(WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchRevisionRow.object_id)
                .outerjoin(WorkbenchReleaseRow, WorkbenchReleaseRow.revision_id == WorkbenchDebugSessionRow.revision_id)
            )
            if object_id:
                query = query.where(WorkbenchObjectRow.object_id == object_id)
            if revision_id:
                query = query.where(WorkbenchDebugSessionRow.revision_id == revision_id)
            if status:
                query = query.where(WorkbenchDebugSessionRow.status == status)
            rows = db.execute(query.order_by(WorkbenchDebugSessionRow.created_at.desc()).limit(limit)).all()
            return [
                {
                    **self._debug_dict(debug),
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "object_key": obj.object_key,
                    "object_name": obj.name,
                    "revision_no": revision.revision_no,
                    "release_id": release.release_id if release else None,
                    "release_version": f"v{release.release_no}" if release else None,
                }
                for debug, revision, obj, release in rows
            ]

    def create_evaluation_suite(
        self,
        *,
        object_id: str,
        name: str,
        cases: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        if not name.strip() or not cases:
            raise WorkbenchError("用例集名称和至少一个用例不能为空")
        with self.session_factory() as db:
            self._require_object(db, object_id)
            row = WorkbenchEvaluationSuiteRow(
                suite_id=_id("suite"), object_id=object_id, name=name.strip(), cases=cases, created_by=actor_id
            )
            db.add(row)
            self._audit(db, actor_id, "evaluation_suite.created", "object", object_id, {"suite_id": row.suite_id})
            db.commit()
            return self._suite_dict(row)

    def list_evaluation_suites(self, object_id: str | None = None) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = select(WorkbenchEvaluationSuiteRow).where(WorkbenchEvaluationSuiteRow.archived.is_(False))
            if object_id:
                query = query.where(WorkbenchEvaluationSuiteRow.object_id == object_id)
            return [self._suite_dict(row) for row in db.scalars(query.order_by(WorkbenchEvaluationSuiteRow.updated_at.desc()))]

    def create_evaluation_run(
        self,
        *,
        suite_id: str,
        revision_id: str,
        baseline_release_id: str | None,
        soul_revision_id: str | None = None,
        results: list[dict[str, Any]] | None,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.session_factory() as db:
            suite = db.get(WorkbenchEvaluationSuiteRow, suite_id)
            if suite is None:
                raise WorkbenchError("用例集不存在", code="EVALUATION_SUITE_NOT_FOUND")
            revision = self._require_revision(db, revision_id)
            if revision.object_id != suite.object_id:
                raise WorkbenchError("修订与用例集不属于同一对象")
            if baseline_release_id:
                self._require_revision_release(db, revision, baseline_release_id)
            snapshot = self._snapshot_for_revision(db, revision, soul_revision_id=soul_revision_id)
            generated_results = results
            if generated_results is None and self.orchestrator is not None:
                generated_results = self._execute_cases(suite.cases, snapshot)
            row = WorkbenchEvaluationRunRow(
                run_id=_id("eval"),
                suite_id=suite_id,
                revision_id=revision_id,
                baseline_release_id=baseline_release_id,
                snapshot=snapshot,
                results=generated_results or [{"case_id": item.get("case_id"), "status": "pending"} for item in suite.cases],
                status="completed" if generated_results is not None else "pending",
                completed_at=utc_now() if generated_results is not None else None,
                created_by=actor_id,
            )
            db.add(row)
            self._audit(db, actor_id, "evaluation.started", "revision", revision_id, {"run_id": row.run_id})
            db.commit()
            return self._evaluation_run_dict(row)

    def _execute_cases(self, cases: Iterable[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, case in enumerate(cases):
            case_id = str(case.get("case_id") or f"case_{index + 1}")
            context = SessionContext(
                session_id=f"eval_{uuid.uuid4().hex[:16]}",
                user_id="workbench-evaluation",
                profile_id=f"profile_{case_id}",
                profile_name="评测用户",
            )
            self._configure_snapshot_context(context, snapshot)
            raw_turns = case.get("turns")
            turns = raw_turns if isinstance(raw_turns, list) else [{"role": "user", "content": case.get("input") or ""}]
            started = datetime.now().timestamp()
            assistant_messages: list[str] = []
            error = ""
            try:
                for turn in turns:
                    if not isinstance(turn, dict) or turn.get("role", "user") != "user":
                        continue
                    message = str(turn.get("content") or "").strip()
                    if not message:
                        continue
                    reply = self._execute_snapshot_message(snapshot, message, context)
                    assistant_messages.append(reply)
            except Exception as exc:  # Each case is isolated; one failure must not stop the suite.
                error = f"{type(exc).__name__}: {exc}"
            facts = {
                key: record.value
                for key, record in context.known_facts.facts.items()
            }
            results.append({
                "case_id": case_id,
                "name": str(case.get("name") or case_id),
                "status": "failed" if error else "completed",
                "assistant_messages": assistant_messages,
                "active_skill": context.interaction_state.get("active_skill"),
                "facts": facts,
                "trace": context.event_trace,
                "elapsed_ms": round((datetime.now().timestamp() - started) * 1000, 2),
                "error": error,
            })
        return results

    @staticmethod
    def _snapshot_root_entry(snapshot: dict[str, Any]) -> dict[str, Any]:
        root = snapshot.get("root") or {}
        return next(
            (item for item in snapshot.get("entries", []) if item.get("object_id") == root.get("object_id")),
            {},
        )

    def _configure_snapshot_context(self, context: SessionContext, snapshot: dict[str, Any]) -> None:
        context.session_meta["configuration_snapshot"] = snapshot
        context.session_meta["soul_context"] = copy.deepcopy(snapshot.get("soul_context") or {"enabled": False, "content": ""})
        root_entry = self._snapshot_root_entry(snapshot)
        if root_entry.get("object_type") == "skill":
            context.session_meta["workbench_candidate_skill_id"] = root_entry.get("object_key")
            context.interaction_state["active_skill"] = root_entry.get("object_key")
        elif root_entry.get("object_type") == "expert":
            context.session_meta["expert_id"] = root_entry.get("object_key")
            context.session_meta["active_expert_id"] = root_entry.get("object_key")
            context.skill_states.setdefault("agent_runtime", {})["expert_id"] = root_entry.get("object_key")
        elif root_entry.get("object_type") == "expert_team":
            context.session_meta["expert_team_id"] = root_entry.get("object_key")
            coordinator_object_id = str((root_entry.get("payload") or {}).get("coordinator_expert_id") or "")
            coordinator = next(
                (item.get("object_key") for item in root_entry.get("dependency_locks", []) if item.get("object_id") == coordinator_object_id),
                None,
            )
            allowed_members = {
                str(item.get("object_key") or "")
                for item in root_entry.get("dependency_locks", [])
                if isinstance(item, dict) and item.get("object_type") == "expert"
            }
            persisted_expert = str(
                context.session_meta.get("active_expert_id")
                or context.session_meta.get("expert_id")
                or ""
            ).strip()
            # A candidate team starts with its coordinator, but this helper
            # also runs when a subsequent test turn restores persisted state.
            # Do not reset a member chosen through a validated handoff card or
            # the @ toolbar on every restore.  Keep the immutable snapshot as
            # the authority: an absent or no-longer-locked member falls back
            # to the coordinator.
            selected_expert = persisted_expert if persisted_expert in allowed_members else coordinator
            if selected_expert:
                context.session_meta["expert_id"] = selected_expert
                context.session_meta["active_expert_id"] = selected_expert
                context.skill_states.setdefault("agent_runtime", {})["expert_id"] = selected_expert

    def _execute_snapshot_message(self, snapshot: dict[str, Any], message: str, context: SessionContext) -> str:
        root_entry = self._snapshot_root_entry(snapshot)
        if root_entry.get("object_type") == "skill":
            return self._execute_candidate_skill(root_entry, message, context)
        allowed = self._candidate_allowed_skill_ids(snapshot, root_entry, context)
        # Candidate conversations use the same Expert decision boundary as
        # production on every turn.  In particular, a one-Skill Expert still
        # receives AGENT.md and may answer directly instead of invoking it.
        # The Expert is also responsible for deciding whether a short form
        # answer resumes the current Skill or a new message enters another
        # locked Skill.
        message_count_before = len(context.messages)
        event_count_before = len(context.event_trace)
        output = self.orchestrator.handle_message(message, context)
        requested_skill = next(
            (
                str((event.get("payload") or {}).get("skill_id") or "")
                for event in reversed(context.event_trace[event_count_before:])
                if isinstance(event, dict) and event.get("event_type") == "expert_skill_executed"
            ),
            "",
        )
        if requested_skill:
            if requested_skill not in allowed:
                raise WorkbenchError("Agent 请求了未锁定的 Skill", code="CANDIDATE_SKILL_NOT_LOCKED", details={"skill_id": requested_skill})
            # Candidate tests never require a user confirmation click.  The
            # Agent already made an authorized selection. Execute the locked
            # snapshot entry directly instead of asking the general planner to
            # route the same turn again (which can fall back to general_chat).
            target_entry = next(
                (item for item in snapshot.get("entries", []) if isinstance(item, dict) and item.get("object_type") == "skill" and item.get("object_key") == requested_skill),
                None,
            )
            if target_entry is None:
                raise WorkbenchError("候选快照缺少 Agent 选择的 Skill", code="CANDIDATE_SKILL_SNAPSHOT_MISSING")
            context.interaction_state["active_skill"] = requested_skill
            # The formal Expert runtime already invokes its legacy/native
            # executor after an authorized `execute_skill` call. Replaying a
            # lightweight candidate completion here used to create a second,
            # incompatible assistant turn and discarded its form/handoff
            # message metadata. Only test doubles without a Runtime message
            # fall back to the candidate skill executor.
            if any(
                isinstance(item, dict) and item.get("role") == "assistant"
                for item in context.messages[message_count_before:]
            ):
                return str(getattr(output, "assistant_message", "") or self._latest_assistant_content(context))
            return self._execute_candidate_skill(target_entry, message, context)
        return str(getattr(output, "assistant_message", "") or "")

    @staticmethod
    def _latest_assistant_content(context: SessionContext) -> str:
        for item in reversed(getattr(context, "messages", []) or []):
            if isinstance(item, dict) and item.get("role") == "assistant":
                return str(item.get("content") or "")
        return ""


    @staticmethod
    def _candidate_allowed_skill_ids(snapshot: dict[str, Any], root_entry: dict[str, Any], context: SessionContext) -> set[str]:
        entries = snapshot.get("entries") if isinstance(snapshot.get("entries"), list) else []
        active_expert = str(context.session_meta.get("active_expert_id") or context.session_meta.get("expert_id") or root_entry.get("object_key") or "")
        expert_entry = next((item for item in entries if isinstance(item, dict) and item.get("object_type") == "expert" and item.get("object_key") == active_expert), root_entry)
        locks = expert_entry.get("dependency_locks") if isinstance(expert_entry, dict) else []
        return {str(item.get("object_key") or "") for item in locks if isinstance(item, dict) and item.get("object_type") == "skill"}

    @staticmethod
    def _record_candidate_turn_event(context: SessionContext, event_type: str, payload: dict[str, Any]) -> None:
        """Record workbench-only details using the same redaction as runtime events."""
        context.event_trace.append(make_event(event_type, payload))

    @staticmethod
    def _entry_debug(entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        return {
            "object_id": str(entry.get("object_id") or ""),
            "object_type": str(entry.get("object_type") or ""),
            "object_key": str(entry.get("object_key") or ""),
            "name": str(entry.get("name") or entry.get("object_key") or ""),
            "revision_id": str(entry.get("revision_id") or ""),
            "revision_no": entry.get("revision_no"),
            "release_id": str(entry.get("release_id") or "") or None,
        }

    def _candidate_root_debug(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self._entry_debug(self._snapshot_root_entry(snapshot)) or {}

    def _candidate_turn_debug(
        self,
        snapshot: dict[str, Any],
        context: SessionContext,
        turn_events: list[dict[str, Any]],
        *,
        elapsed_ms: float,
    ) -> dict[str, Any]:
        """Make an explicit, evidence-safe per-turn execution summary.

        Runtime events remain available verbatim below this summary.  The
        normalized fields let a business tester see exactly what was selected
        and distinguish an actually-used resource from one merely present in
        the immutable candidate snapshot.
        """
        entries = [item for item in snapshot.get("entries", []) if isinstance(item, dict)]
        by_key = {str(item.get("object_key") or ""): item for item in entries}
        root = self._snapshot_root_entry(snapshot)
        team_id = str(context.session_meta.get("expert_team_id") or "")
        expert_id = str(context.session_meta.get("active_expert_id") or context.session_meta.get("expert_id") or "")
        skill_id = str(context.interaction_state.get("active_skill") or context.session_meta.get("workbench_candidate_skill_id") or "")
        skill_entry = by_key.get(skill_id)

        references_available: list[dict[str, Any]] = []
        scripts_available: list[str] = []
        if skill_entry:
            for file in skill_entry.get("files") if isinstance(skill_entry.get("files"), list) else []:
                if not isinstance(file, dict):
                    continue
                path = str(file.get("relative_path") or "")
                if path.startswith("references/") or path.startswith("assets/"):
                    references_available.append({
                        "path": path,
                        "media_type": str(file.get("media_type") or "application/octet-stream"),
                        "content_hash": str(file.get("content_hash") or ""),
                    })
                elif path.startswith("scripts/") and path.endswith(".py"):
                    scripts_available.append(path)

        reference_uses: list[dict[str, Any]] = []
        required_references: set[str] = set()
        selected_references: set[str] = set()
        skill_progress: dict[str, Any] = {}
        script_runs: list[dict[str, Any]] = []
        expert_routing: dict[str, Any] = {}
        execution_error: dict[str, Any] = {}
        timing: dict[str, Any] = {
            "planner_ms": None,
            "sandbox_prepare_ms": None,
            "worker_acquire_ms": None,
            "worker_startup_ms": None,
            "process_spawn_ms": None,
            "asset_load_ms": None,
            "script_compute_ms": None,
            "stdout_parse_ms": None,
            "result_cache_lookup_ms": None,
            "final_generation_ms": None,
            "first_token_ms": None,
        }
        for event in turn_events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "expert_skill_route_selected":
                expert_routing = {
                    "mode": str(payload.get("mode") or ""),
                    "skill_id": str(payload.get("skill_id") or "") or None,
                    "candidate_skill_ids": list(payload.get("candidate_skill_ids") or []),
                    "confidence": payload.get("confidence"),
                    "reason": str(payload.get("reason") or ""),
                    "capability_catalog_version": str(payload.get("capability_catalog_version") or ""),
                    "execute_skill_registered": bool(payload.get("execute_skill_registered")),
                    "route_candidate_source": str(payload.get("route_candidate_source") or ""),
                    "full_skill_inspected": bool(payload.get("full_skill_inspected")),
                    "scope_decision": str(payload.get("scope_decision") or "uncertain"),
                    "skill_scope_basis": str(payload.get("skill_scope_basis") or ""),
                    "direct_reply_preserved": bool(payload.get("direct_reply_preserved")),
                    "direct_reply_block_reason": str(payload.get("direct_reply_block_reason") or ""),
                    "forced_skill_execution": bool(payload.get("forced_skill_execution")),
                }
            elif event_type == "expert_direct_reply_blocked":
                expert_routing["direct_reply_blocked"] = {
                    "candidate_skill_id": str(payload.get("candidate_skill_id") or ""),
                    "reason": str(payload.get("reason") or ""),
                }
            elif event_type == "expert_skill_scope_inspection_requested":
                expert_routing["scope_inspection_requested"] = {
                    "candidate_skill_id": str(payload.get("candidate_skill_id") or ""),
                    "route_candidate_source": str(payload.get("route_candidate_source") or ""),
                    "reason": str(payload.get("reason") or ""),
                }
            elif event_type == "expert_direct_reply_preserved":
                expert_routing["direct_reply_preserved"] = {
                    "candidate_skill_id": str(payload.get("candidate_skill_id") or ""),
                    "route_candidate_source": str(payload.get("route_candidate_source") or ""),
                    "full_skill_inspected": bool(payload.get("full_skill_inspected")),
                    "scope_decision": str(payload.get("scope_decision") or "uncertain"),
                    "skill_scope_basis": str(payload.get("skill_scope_basis") or ""),
                    "reason": str(payload.get("reason") or ""),
                }
            elif event_type in {"expert_decision_unavailable", "candidate_turn_failed"}:
                execution_error = {
                    "event_type": event_type,
                    "reason": str(payload.get("reason") or "")[:800],
                    "exception_type": str(payload.get("exception_type") or ""),
                }
            if event_type == "runtime_skill_progress_updated":
                skill_progress = {
                    "stage_label": str(payload.get("stage_label") or ""),
                    "confirmed_fact_keys": list(payload.get("confirmed_fact_keys") or []),
                    "resolved_topics": list(payload.get("resolved_topics") or []),
                    "pending_topics": list(payload.get("pending_topics") or []),
                    "next_action": str(payload.get("next_action") or ""),
                }
            if event_type == "runtime_llm_timing":
                phase = str(payload.get("phase") or "")
                duration = payload.get("duration_ms")
                if phase and duration is not None:
                    timing["final_generation_ms" if "final" in phase or "response" in phase else phase] = duration
                if payload.get("first_token_ms") is not None:
                    timing["first_token_ms"] = payload.get("first_token_ms")
            if event_type == "native_questionnaire_response" and payload.get("duration_ms") is not None:
                timing["final_generation_ms"] = payload.get("duration_ms")
                timing["first_token_ms"] = payload.get("duration_ms")
            if event_type in {
                "reference_context",
                "retrieval_context",
                "ms_agent_plan_summary",
                "reference_preflight",
                "reference_response_evidence",
                "reference_evidence_unavailable",
                "reference_compliance_degraded",
            }:
                for path in payload.get("required_references") if isinstance(payload.get("required_references"), list) else []:
                    if str(path).strip():
                        required_references.add(str(path).strip())
                for path in payload.get("selected_references") if isinstance(payload.get("selected_references"), list) else []:
                    if str(path).strip():
                        selected_references.add(str(path).strip())
                plan_payload = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
                for path in plan_payload.get("required_references") if isinstance(plan_payload.get("required_references"), list) else []:
                    if str(path).strip():
                        required_references.add(str(path).strip())
                for path in plan_payload.get("selected_references") if isinstance(plan_payload.get("selected_references"), list) else []:
                    if str(path).strip():
                        selected_references.add(str(path).strip())
                for item in payload.get("items") if isinstance(payload.get("items"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("source_path") or item.get("path") or "")
                    if path:
                        reference_uses.append({
                            "path": path,
                            "title": str(item.get("title") or ""),
                            "source_type": str(item.get("source_type") or "reference"),
                            "snippet": str(item.get("snippet") or ""),
                        })
                for path in payload.get("loaded_references") if isinstance(payload.get("loaded_references"), list) else []:
                    if str(path).strip():
                        selected_references.add(str(path).strip())
            if event_type == "ms_agent_runtime":
                step_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                for path in step_payload.get("required_references") if isinstance(step_payload.get("required_references"), list) else []:
                    if str(path).strip():
                        required_references.add(str(path).strip())
                for path in step_payload.get("selected_references") if isinstance(step_payload.get("selected_references"), list) else []:
                    if str(path).strip():
                        selected_references.add(str(path).strip())
            if event_type == "ms_agent_runtime" and str(payload.get("step") or "") == "script_execution":
                step_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                for output in step_payload.get("outputs") if isinstance(step_payload.get("outputs"), list) else []:
                    if not isinstance(output, dict):
                        continue
                    script_runs.append({
                        "skill_id": str(payload.get("skill_id") or skill_id),
                        "path": str(output.get("script") or ""),
                        "status": "success" if _script_execution_succeeded(output) else "failed",
                        "execution_mode": str(output.get("execution_mode") or step_payload.get("execution_mode") or ""),
                        "planner_selected": True,
                        "planner_reason": str(step_payload.get("reason") or ""),
                        "stdin_payload": output.get("stdin_payload"),
                        "args": output.get("args"),
                        "stdout": output.get("stdout"),
                        "stderr": output.get("stderr"),
                        "json_output": output.get("json_output"),
                        "return_value": output.get("return_value"),
                        "exit_code": output.get("exit_code"),
                        "error": str(output.get("error") or ""),
                        "duration_ms": output.get("duration_ms"),
                        "cache_hit": bool(output.get("cache_hit")),
                        "cache_key_hash": str(output.get("cache_key_hash") or ""),
                        "result_injected_into_prompt": True,
                    })

            if event_type == "ms_agent_runtime":
                step = str(payload.get("step") or "")
                step_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                if step == "skill_plan" and step_payload.get("duration_ms") is not None:
                    timing["planner_ms"] = step_payload.get("duration_ms")
                if step == "sandbox_prepare" and step_payload.get("duration_ms") is not None:
                    timing["sandbox_prepare_ms"] = step_payload.get("duration_ms")
                if step == "sandbox_startup" and step_payload.get("duration_ms") is not None:
                    timing["worker_startup_ms"] = step_payload.get("duration_ms")
                if step == "script_execution":
                    outputs = step_payload.get("outputs") if isinstance(step_payload.get("outputs"), list) else []
                    durations = [item.get("duration_ms") for item in outputs if isinstance(item, dict) and item.get("duration_ms") is not None]
                    if durations:
                        timing["script_compute_ms"] = sum(durations)
                    parse_durations = [item.get("stdout_parse_ms") for item in outputs if isinstance(item, dict) and item.get("stdout_parse_ms") is not None]
                    if parse_durations:
                        timing["stdout_parse_ms"] = sum(parse_durations)
                    if step_payload.get("cache_lookup_duration_ms") is not None:
                        timing["result_cache_lookup_ms"] = step_payload.get("cache_lookup_duration_ms")
                    if step_payload.get("worker_acquire_ms") is not None:
                        timing["worker_acquire_ms"] = step_payload.get("worker_acquire_ms")
                    if step_payload.get("execution_mode"):
                        timing["execution_mode"] = step_payload.get("execution_mode")
                    timing["worker_reused"] = bool(step_payload.get("worker_reused"))

        # Direct preview executions intentionally do not run arbitrary package
        # scripts. Keep that fact visible in evidence rather than presenting a
        # configured script as though it had been executed.
        if not script_runs:
            script_runs = [
                {
                    "path": path,
                    "status": "not_executed",
                    "input": None,
                    "output": None,
                    "error": "本轮候选运行未调用该脚本。",
                    "duration_ms": None,
                }
                for path in scripts_available
            ]

        timing["total_ms"] = elapsed_ms
        return {
            "root": self._entry_debug(root),
            "expert_team": self._entry_debug(by_key.get(team_id)),
            "expert": self._entry_debug(by_key.get(expert_id)),
            "skill": self._entry_debug(skill_entry),
            "references": {
                "required_references": sorted(required_references),
                "selected_references": sorted(selected_references),
                "used": reference_uses,
                "available_but_not_used": [
                    item for item in references_available
                    if item["path"] not in {str(used.get("path") or "") for used in reference_uses}
                ],
            },
            "skill_progress": skill_progress,
            "expert_routing": expert_routing,
            "execution_error": execution_error,
            "scripts": script_runs,
            "timing": timing,
            "elapsed_ms": elapsed_ms,
        }

    def _mount_candidate_snapshot_skills(self, snapshot: dict[str, Any], context: SessionContext) -> dict[str, Any] | None:
        """Expose candidate Skills before Expert routing, then restore them.

        The old candidate overlay was mounted only after an Expert had chosen
        a Skill. Consequently unpublished dependency locks were authorised but
        absent from the capability directory used to make that choice.
        """
        runtime_registry = getattr(self.orchestrator, "runtime_registry", None)
        if runtime_registry is None:
            return None
        entries = [
            item for item in snapshot.get("entries", [])
            if isinstance(item, dict) and item.get("object_type") == "skill" and str(item.get("object_key") or "")
        ]
        if not entries:
            return None
        template = runtime_registry.get_raw("general_chat") or getattr(self.orchestrator, "main_bundle", None)
        if template is None:
            return None
        self._candidate_runtime_mount_lock.acquire()
        previous: dict[str, Any] = {}
        mounted: list[str] = []
        try:
            for entry in entries:
                skill_id = str(entry.get("object_key") or "")
                previous[skill_id] = runtime_registry.bundles.get(skill_id)
                bundle = copy.copy(configured_skill_bundle(template, entry))
                bundle.contract = replace(bundle.contract, skill_id=skill_id)
                bundle.runtime_metadata = replace(
                    bundle.runtime_metadata,
                    skill_id=skill_id,
                    name=str(entry.get("name") or skill_id),
                    version=f"candidate-{str(entry.get('revision_id') or '')[:12]}",
                )
                runtime_registry.bundles[skill_id] = bundle
                mounted.append(skill_id)
                self._record_candidate_turn_event(context, "candidate_runtime_temporary_mount", {
                    "skill_id": skill_id,
                    "revision_id": entry.get("revision_id"),
                    "mode": "expert_routing_overlay",
                })
        except Exception:
            for skill_id in mounted:
                original = previous.get(skill_id)
                if original is None:
                    runtime_registry.bundles.pop(skill_id, None)
                else:
                    runtime_registry.bundles[skill_id] = original
            self._candidate_runtime_mount_lock.release()
            raise
        return {"registry": runtime_registry, "previous": previous, "mounted": mounted}

    def _restore_candidate_snapshot_skills(self, mounts: dict[str, Any] | None) -> None:
        if not isinstance(mounts, dict):
            return
        runtime_registry = mounts.get("registry")
        previous = mounts.get("previous") if isinstance(mounts.get("previous"), dict) else {}
        mounted = mounts.get("mounted") if isinstance(mounts.get("mounted"), list) else []
        try:
            for skill_id in mounted:
                original = previous.get(skill_id)
                if original is None:
                    runtime_registry.bundles.pop(skill_id, None)
                else:
                    runtime_registry.bundles[skill_id] = original
        finally:
            self._candidate_runtime_mount_lock.release()

    def _execute_candidate_skill(self, entry: dict[str, Any], message: str, context: SessionContext) -> str:
        """Execute a candidate Skill through the formal Runtime whenever possible.

        The snapshot overlay is read by ``MainPlannerOrchestrator`` and swaps
        only the business files/prompt. The state machine, questionnaire,
        script sandbox and message construction remain the production code.
        Unpublished candidates are temporarily mounted over the shared
        Runtime registry for the duration of this call, so they use exactly
        the same executor without becoming visible to formal conversations.
        """
        skill_id = str(entry.get("object_key") or "")
        runtime_registry = getattr(self.orchestrator, "runtime_registry", None)
        bundle = runtime_registry.get(skill_id) if runtime_registry is not None and skill_id else None
        formal_executor = getattr(self.orchestrator, "_handle_message_legacy", None)
        temporary_mount = False
        previous_bundle = None
        registry_key = skill_id
        runtime_mount_lock = None
        if bundle is None and runtime_registry is not None and callable(formal_executor):
            # Candidate revisions must be testable before their first release.
            # Reuse the main/general bundle as the immutable code shell and
            # overlay the candidate's prompt, references, contract and scripts.
            template = runtime_registry.get_raw("general_chat") or getattr(self.orchestrator, "main_bundle", None)
            if template is not None and skill_id:
                candidate_bundle = configured_skill_bundle(template, entry)
                candidate_bundle = copy.copy(candidate_bundle)
                candidate_bundle.contract = replace(candidate_bundle.contract, skill_id=skill_id)
                candidate_bundle.runtime_metadata = replace(
                    candidate_bundle.runtime_metadata,
                    skill_id=skill_id,
                    name=str(entry.get("name") or skill_id),
                    version=f"candidate-{str(entry.get('revision_id') or '')[:12]}",
                )
                runtime_mount_lock = self._candidate_runtime_mount_lock
                runtime_mount_lock.acquire()
                previous_bundle = runtime_registry.bundles.get(registry_key)
                runtime_registry.bundles[registry_key] = candidate_bundle
                temporary_mount = True
                bundle = runtime_registry.get(skill_id)
                self._record_candidate_turn_event(
                    context,
                    "candidate_runtime_temporary_mount",
                    {"skill_id": skill_id, "revision_id": entry.get("revision_id"), "mode": "candidate_overlay"},
                )
        if bundle is not None and callable(formal_executor):
            context.interaction_state["active_skill"] = skill_id
            runtime_state = context.skill_states.setdefault("skill_runtime", {})
            if isinstance(runtime_state, dict):
                runtime_state["active_skill_id"] = skill_id
            self._record_candidate_turn_event(
                context,
                "candidate_runtime_shared_executor",
                {"skill_id": skill_id, "mode": "formal_runtime"},
            )
            try:
                result = formal_executor(message, context)
                return str(getattr(result, "assistant_message", "") or self._latest_assistant_content(context))
            finally:
                if temporary_mount:
                    if previous_bundle is None:
                        runtime_registry.bundles.pop(registry_key, None)
                    else:
                        runtime_registry.bundles[registry_key] = previous_bundle
                    runtime_mount_lock.release()
        self._record_candidate_turn_event(
            context,
            "candidate_runtime_fallback",
            {"skill_id": skill_id, "reason": "runtime_skill_not_installed"},
        )
        return self._execute_skill_preview_fallback(entry, message, context)

    def _execute_skill_preview_fallback(self, entry: dict[str, Any], message: str, context: SessionContext) -> str:
        from hailiang_skills.skill_runtime.models import ChatMessage

        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        prompt = str(payload.get("prompt_markdown") or "").strip()
        client = self.orchestrator._runtime_client_for_context(context)
        if client is None:
            raise RuntimeError("模型客户端不可用")
        revision_id = str(entry.get("revision_id") or "")
        system_prompt = (
            "你正在执行业务工作台中选定的不可变候选 Skill 修订。"
            f"当前 Skill：{entry.get('object_key') or ''}；修订：{revision_id}。\n"
            "下方 SKILL.md 是本次测试唯一且强制执行的系统配置，不是用户提供的参考资料。"
            "必须遵循其中的角色、指令和输出要求；不得改用生产版本、默认规则或其他修订。\n\n"
            "<candidate-skill-markdown>\n"
            f"{prompt}\n"
            "</candidate-skill-markdown>"
        )
        history = [
            ChatMessage(role=str(item.get("role") or "user"), content=str(item.get("content") or ""))
            for item in (getattr(context, "messages", []) or [])
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and str(item.get("content") or "").strip()
        ]
        response = client.complete([
            ChatMessage(role="system", content=system_prompt),
            *history,
            ChatMessage(role="user", content=message),
        ])
        context.add_message("user", message)
        context.add_message("assistant", str(response or ""), metadata={"skill_id": entry.get("object_key")})
        context.interaction_state["active_skill"] = entry.get("object_key")
        return str(response or "")

    def complete_evaluation_run(
        self,
        run_id: str,
        *,
        results: list[dict[str, Any]],
        manual_result: str,
        manual_notes: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if manual_result not in {"accepted", "rejected"}:
            raise WorkbenchError("manual_result 仅支持 accepted 或 rejected")
        with self.session_factory() as db:
            row = db.get(WorkbenchEvaluationRunRow, run_id)
            if row is None:
                raise WorkbenchError("评测运行不存在", code="EVALUATION_RUN_NOT_FOUND")
            row.results = results
            row.status = "completed"
            row.manual_result = manual_result
            row.manual_notes = str(manual_notes or "").strip()
            row.completed_at = utc_now()
            self._audit(db, actor_id, "evaluation.completed", "revision", row.revision_id, {"run_id": run_id, "result": manual_result})
            db.commit()
            return self._evaluation_run_dict(row)

    def list_evaluation_runs(
        self,
        *,
        object_id: str | None = None,
        revision_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = (
                select(WorkbenchEvaluationRunRow, WorkbenchEvaluationSuiteRow, WorkbenchRevisionRow, WorkbenchObjectRow, WorkbenchReleaseRow)
                .join(WorkbenchEvaluationSuiteRow, WorkbenchEvaluationSuiteRow.suite_id == WorkbenchEvaluationRunRow.suite_id)
                .join(WorkbenchRevisionRow, WorkbenchRevisionRow.revision_id == WorkbenchEvaluationRunRow.revision_id)
                .join(WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchRevisionRow.object_id)
                .outerjoin(WorkbenchReleaseRow, WorkbenchReleaseRow.revision_id == WorkbenchEvaluationRunRow.revision_id)
            )
            if object_id:
                query = query.where(WorkbenchObjectRow.object_id == object_id)
            if revision_id:
                query = query.where(WorkbenchEvaluationRunRow.revision_id == revision_id)
            if status:
                query = query.where(WorkbenchEvaluationRunRow.status == status)
            rows = db.execute(query.order_by(WorkbenchEvaluationRunRow.created_at.desc()).limit(limit)).all()
            return [
                {
                    **self._evaluation_run_dict(run),
                    "suite_name": suite.name,
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "object_key": obj.object_key,
                    "object_name": obj.name,
                    "revision_no": revision.revision_no,
                    "release_id": release.release_id if release else None,
                    "release_version": f"v{release.release_no}" if release else None,
                }
                for run, suite, revision, obj, release in rows
            ]

    def publish_revision(
        self,
        revision_id: str,
        *,
        evidence_id: str,
        manual_confirmation: bool,
        confirmation_notes: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not manual_confirmation:
            raise WorkbenchError("发布前必须人工确认调试结果", code="MANUAL_CONFIRMATION_REQUIRED")
        with self.session_factory() as db:
            revision = self._require_revision(db, revision_id)
            obj = self._require_object(db, revision.object_id)
            existing = db.scalar(select(WorkbenchReleaseRow).where(WorkbenchReleaseRow.revision_id == revision_id))
            if existing is not None:
                return self._release_dict(existing, obj, self._actor_display_names(db, [existing.published_by]))
            if not revision.validation.get("valid", False):
                raise WorkbenchError(
                    "修订校验未通过，不能发布",
                    code="REVISION_VALIDATION_FAILED",
                    details={"errors": list(revision.validation.get("errors") or []), "warnings": list(revision.validation.get("warnings") or [])},
                )
            evidence = self._require_evidence(db, revision_id, evidence_id)
            release_no = (db.scalar(select(func.max(WorkbenchReleaseRow.release_no)).where(WorkbenchReleaseRow.object_id == obj.object_id)) or 0) + 1
            verification = {
                "evidence_id": evidence_id,
                "evidence_type": evidence,
                "manual_confirmation": True,
                "notes": str(confirmation_notes or "").strip(),
                "confirmed_by": actor_id,
                "confirmed_at": _iso(utc_now()),
            }
            row = WorkbenchReleaseRow(
                release_id=_id("rel"),
                object_id=obj.object_id,
                revision_id=revision_id,
                release_no=release_no,
                dependency_locks=list(revision.dependency_locks or []),
                content_hash=revision.content_hash,
                verification=verification,
                published_by=actor_id,
            )
            db.add(row)
            obj.current_release_id = row.release_id
            self._install_runtime_entry({
                "object_id": obj.object_id,
                "object_type": obj.object_type,
                "object_key": obj.object_key,
                "name": obj.name,
                "release_id": row.release_id,
                "release_no": row.release_no,
                "payload": revision.payload,
                "dependency_locks": row.dependency_locks,
                "content_hash": revision.content_hash,
                "files": self._revision_files(db, revision.revision_id),
            })
            self._audit(db, actor_id, "release.published", obj.object_type, row.release_id, {"release_no": release_no})
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise WorkbenchConflict(
                    "发布序号已被并发占用，请刷新后重试",
                    code="RELEASE_SEQUENCE_CONFLICT",
                ) from exc
            return self._release_dict(row, obj, self._actor_display_names(db, [row.published_by]))

    def archive_release(self, release_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            release = self._require_release(db, release_id)
            obj = self._require_object(db, release.object_id)
            if obj.current_release_id == release_id:
                raise WorkbenchConflict("请先切换当前发布版本，再归档此版本", code="CURRENT_RELEASE_ARCHIVE_FORBIDDEN")
            release.archived = True
            self._audit(db, actor_id, "release.archived", "release", release_id, {})
            db.commit()
            return {"release_id": release_id, "archived": True}

    def select_current_release(self, release_id: str, *, expected_current_release_id: str | None, actor_id: str) -> dict[str, Any]:
        """Move the publication pointer without rewriting any release or dependency."""
        with self.session_factory() as db:
            release = self._require_release(db, release_id)
            obj = db.scalar(select(WorkbenchObjectRow).where(
                WorkbenchObjectRow.object_id == release.object_id
            ).with_for_update())
            if obj is None or obj.archived or release.archived:
                raise WorkbenchConflict("不能选择已归档对象或发布版本", code="RELEASE_ARCHIVED")
            if obj.current_release_id != expected_current_release_id:
                raise WorkbenchConflict("当前发布版本已变化，请刷新后重试", code="CURRENT_RELEASE_CONFLICT")
            # Historical dependencies may have been deleted from the workbench.
            # Refuse a rollback which cannot reconstruct its complete closure.
            self._release_closure(db, release)
            previous = obj.current_release_id
            obj.current_release_id = release_id
            self._audit(db, actor_id, "release.current_changed", obj.object_type, obj.object_id,
                        {"from_release_id": previous, "to_release_id": release_id})
            runtime_entry = self._runtime_entry_for_release(db, release_id)
            result = self._release_dict(release, obj, self._actor_display_names(db, [release.published_by]))
            db.commit()
        self._install_runtime_entry(runtime_entry)
        return result

    def draft_from_release(self, release_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            release = self._require_release(db, release_id)
            obj = db.scalar(select(WorkbenchObjectRow).where(
                WorkbenchObjectRow.object_id == release.object_id
            ).with_for_update())
            if obj is None or obj.archived:
                raise WorkbenchConflict("对象已归档", code="OBJECT_ARCHIVED")
            source = self._require_revision(db, release.revision_id)
            latest = db.scalar(select(WorkbenchRevisionRow).where(
                WorkbenchRevisionRow.object_id == obj.object_id
            ).order_by(WorkbenchRevisionRow.revision_no.desc()).limit(1))
            revision = self._copy_candidate_revision(
                db, source_revision=source, target=obj, payload=copy.deepcopy(source.payload),
                requested_locks=copy.deepcopy(release.dependency_locks), actor_id=actor_id,
                base_revision_id=latest.revision_id if latest else None,
            )
            db.commit()
            return self._revision_dict(revision, self._actor_display_names(db, [revision.created_by]))

    def list_releases(self, *, object_type: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = select(WorkbenchReleaseRow, WorkbenchObjectRow, WorkbenchRevisionRow).join(
                WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchReleaseRow.object_id
            ).join(WorkbenchRevisionRow, WorkbenchRevisionRow.revision_id == WorkbenchReleaseRow.revision_id)
            if object_type:
                query = query.where(WorkbenchObjectRow.object_type == object_type)
            if not include_archived:
                query = query.where(WorkbenchReleaseRow.archived.is_(False), WorkbenchObjectRow.archived.is_(False))
            rows = list(db.execute(query.order_by(WorkbenchReleaseRow.published_at.desc())))
            actor_names = self._actor_display_names(db, [release.published_by for release, _obj, _revision in rows])
            result = []
            for release, obj, revision in rows:
                item = self._release_dict(release, obj, actor_names)
                item["revision_no"] = revision.revision_no
                item["revision_change_summary"] = str(getattr(revision, "change_summary", "") or "")
                result.append(item)
            return result

    def export_configuration(self, *, release_id: str | None, revision_id: str | None, actor_id: str) -> tuple[bytes, dict[str, Any]]:
        if bool(release_id) == bool(revision_id):
            raise WorkbenchError("必须且只能指定一个正式版本或修订版本", code="EXPORT_TARGET_REQUIRED")
        with self.session_factory() as db:
            package_kind = "release"
            if release_id:
                root_release = self._require_release(db, release_id)
                entries = self._release_closure(db, root_release)
            else:
                package_kind = "candidate_revision"
                revision = self._require_revision(db, str(revision_id))
                root_obj = self._require_object(db, revision.object_id)
                root_release = SimpleNamespace(
                    release_id=f"revision:{revision.revision_id}", object_id=root_obj.object_id,
                    release_no=None, revision_id=revision.revision_id, dependency_locks=revision.dependency_locks,
                    content_hash=revision.content_hash, published_by="", published_at=None,
                )
                entries = [(root_release, root_obj, revision)]
                seen = {root_release.release_id}
                for lock in revision.dependency_locks or []:
                    for dependency in self._release_closure(db, self._require_release(db, str(lock["release_id"]))) :
                        if dependency[0].release_id not in seen:
                            entries.append(dependency)
                            seen.add(dependency[0].release_id)
            actor_names = self._actor_display_names(
                db,
                [
                    actor
                    for release, _obj, revision in entries
                    for actor in (revision.created_by, release.published_by)
                ],
            )
            package_id = _id("pkg")
            manifest_objects: list[dict[str, Any]] = []
            files: dict[str, bytes] = {}
            for release, obj, revision in entries:
                version_label = f"v{release.release_no}" if release.release_no else f"r{revision.revision_no}"
                prefix = f"objects/{obj.object_type}/{obj.object_key}/{version_label}"
                object_payload = {
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "object_key": obj.object_key,
                    "name": obj.name,
                    "description": obj.description,
                    "release_id": release.release_id,
                    "release_no": release.release_no,
                    "revision_id": revision.revision_id,
                    "revision_no": revision.revision_no,
                    "change_summary": str(getattr(revision, "change_summary", "") or ""),
                    "revision_created_by": revision.created_by,
                    "revision_created_by_display_name": actor_names.get(revision.created_by, revision.created_by),
                    "revision_created_at": _iso(revision.created_at),
                    "release_published_by": release.published_by,
                    "release_published_by_display_name": actor_names.get(release.published_by, release.published_by),
                    "release_published_at": _iso(release.published_at),
                    "content_hash": release.content_hash,
                    "payload": revision.payload,
                    "dependency_locks": release.dependency_locks,
                }
                object_path = f"{prefix}/object.json"
                files[object_path] = json.dumps(object_payload, ensure_ascii=False, indent=2).encode("utf-8")
                self._add_compatibility_files(files, prefix, obj, revision, release)
                for asset in self._revision_files(db, revision.revision_id):
                    files[f"{prefix}/{_safe_relative_path(asset['relative_path'])}"] = base64.b64decode(
                        asset["content_base64"], validate=True
                    )
                object_files = [
                    {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
                    for path, content in sorted(files.items())
                    if path.startswith(f"{prefix}/")
                ]
                manifest_objects.append({
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "object_key": obj.object_key,
                    "release_id": release.release_id,
                    "release_no": release.release_no,
                    "revision_id": revision.revision_id,
                    "revision_no": revision.revision_no,
                    "change_summary": str(getattr(revision, "change_summary", "") or ""),
                    "revision_created_by": revision.created_by,
                    "revision_created_by_display_name": actor_names.get(revision.created_by, revision.created_by),
                    "revision_created_at": _iso(revision.created_at),
                    "release_published_by": release.published_by,
                    "release_published_by_display_name": actor_names.get(release.published_by, release.published_by),
                    "release_published_at": _iso(release.published_at),
                    "content_hash": release.content_hash,
                    "dependency_locks": release.dependency_locks,
                    "files": object_files,
                })
            root_obj = self._require_object(db, root_release.object_id)
            manifest = {
                "schema_version": 2,
                "package_kind": package_kind,
                "package_id": package_id,
                "root": {
                    "object_id": root_obj.object_id,
                    "object_type": root_obj.object_type,
                    "object_key": root_obj.object_key,
                    "release_id": root_release.release_id,
                    "release_no": root_release.release_no,
                    "revision_id": root_release.revision_id,
                    "revision_no": entries[0][2].revision_no,
                    "change_summary": str(getattr(entries[0][2], "change_summary", "") or ""),
                },
                "objects": manifest_objects,
                "kernel_fingerprint": self.kernel_fingerprint,
                "exported_by": actor_id,
                "exported_at": _iso(utc_now()),
            }
            manifest["manifest_hash"] = _hash(manifest)
            files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            archive = self._zip(files)
            if len(archive) > MAX_PACKAGE_BYTES:
                raise WorkbenchError("配置包超过大小限制", code="PACKAGE_TOO_LARGE")
            self._audit(db, actor_id, "package.exported", package_kind, release_id or revision_id, {"package_id": package_id, "package_sha256": hashlib.sha256(archive).hexdigest()})
            db.commit()
            return archive, manifest

    def export_release(self, release_id: str, *, actor_id: str) -> tuple[bytes, dict[str, Any]]:
        return self.export_configuration(release_id=release_id, revision_id=None, actor_id=actor_id)

    def import_package(self, package_bytes: bytes, *, environment: str, actor_id: str) -> dict[str, Any]:
        # Staging is an intake step.  Defer Python directory/layout and script
        # safety review until activation, while retaining ZIP/manifest/hash and
        # executable-file checks here.
        validated = self._validated_package_contents(package_bytes, defer_script_validation=True)
        manifest = copy.deepcopy(validated["manifest"])
        root_release_id = str((manifest.get("root") or {}).get("release_id") or "")
        root_entry = next((item for item in validated["entries"] if str(item.get("release_id") or "") == root_release_id), {})
        if isinstance(manifest.get("root"), dict) and isinstance(root_entry, dict):
            # Deployment metadata is separate from the signed ZIP manifest and
            # makes the immutable root name available to the operations UI.
            manifest["root"]["name"] = str(root_entry.get("name") or manifest["root"].get("object_key") or "")
        package_hash = validated["package_hash"]
        object_release_ids = validated["object_release_ids"]
        with self.session_factory() as db:
            existing = db.scalar(
                select(WorkbenchDeploymentRow).where(
                    WorkbenchDeploymentRow.environment == environment,
                    WorkbenchDeploymentRow.package_hash == package_hash,
                )
            )
            if existing is not None:
                return self._deployment_dict(existing)
            incoming_hashes = {
                str(item.get("release_id") or ""): str(item.get("content_hash") or "")
                for item in manifest.get("objects", [])
                if isinstance(item, dict)
            }
            for installed in db.scalars(select(WorkbenchDeploymentRow)):
                for item in (installed.manifest or {}).get("objects", []):
                    release_id = str(item.get("release_id") or "") if isinstance(item, dict) else ""
                    if release_id in incoming_hashes and incoming_hashes[release_id] != str(item.get("content_hash") or ""):
                        raise WorkbenchConflict(
                            "相同 release_id 已存在但内容哈希不同",
                            code="RELEASE_CONTENT_CONFLICT",
                        )
            if root_release_id not in object_release_ids:
                raise WorkbenchError("根发布版本不在配置包对象中", code="INVALID_PACKAGE")
            deployment = WorkbenchDeploymentRow(
                deployment_id=_id("deploy"),
                environment=str(environment or "prod"),
                root_release_id=root_release_id,
                package_hash=package_hash,
                manifest=manifest,
                package_bytes=package_bytes,
                status="staged",
                imported_by=actor_id,
            )
            db.add(deployment)
            self._audit(db, actor_id, "deployment.imported", "deployment", deployment.deployment_id, {"environment": environment})
            db.commit()
            return self._deployment_dict(deployment)

    def import_object_package(self, package_bytes: bytes, *, actor_id: str) -> dict[str, Any]:
        validated = self._validated_package_contents(
            package_bytes,
            materialize_entries=False,
            defer_script_validation=True,
        )
        manifest = validated["manifest"]
        package_hash = validated["package_hash"]
        root_source_release_id = str((manifest.get("root") or {}).get("release_id") or "")
        ordered_entries = sorted(
            validated["entries"],
            key=lambda item: {"skill": 0, "expert": 1, "expert_team": 2}.get(str(item.get("object_type") or ""), 9),
        )
        source_release_map: dict[str, dict[str, Any]] = {}
        imported_entries: list[dict[str, Any]] = []
        summary = {
            "root": manifest.get("root") or {},
            "package_hash": package_hash,
            "created_objects": 0,
            "created_revisions": 0,
            "created_releases": 0,
            "reused_objects": 0,
            "reused_revisions": 0,
            "reused_releases": 0,
            "unarchived_objects": 0,
            "entries": imported_entries,
        }
        runtime_entries: list[dict[str, Any]] = []
        with self.session_factory() as db:
            for entry in ordered_entries:
                source_release_id = str(entry.get("release_id") or "")
                local_locks: list[dict[str, Any]] = []
                for lock in entry.get("dependency_locks", []) if isinstance(entry.get("dependency_locks"), list) else []:
                    dependency = source_release_map.get(str(lock.get("release_id") or ""))
                    if dependency is None:
                        raise WorkbenchError("配置包缺少可导入的依赖闭包", code="PACKAGE_DEPENDENCY_MISSING")
                    local_locks.append({
                        "object_id": dependency["object_id"],
                        "object_type": dependency["object_type"],
                        "object_key": dependency["object_key"],
                        "release_id": dependency["release_id"],
                        "release_no": dependency["release_no"],
                        "content_hash": dependency["content_hash"],
                    })
                result = self._import_workbench_entry(
                    db,
                    entry,
                    local_dependency_locks=local_locks,
                    package_hash=package_hash,
                    actor_id=actor_id,
                    defer_script_validation=True,
                )
                # A package is an exchange artifact, never an implicit
                # publication. Keep the root as a candidate revision so the
                # receiving environment must run its own tests and publish
                # decision. Dependency releases remain available for the
                # root Agent/Team's immutable locks.
                if source_release_id == root_source_release_id and result["created_releases"]:
                    local_release_id = str(result["release"]["release_id"])
                    local_release = self._require_release(db, local_release_id)
                    local_object = self._require_object(db, local_release.object_id)
                    if local_object.current_release_id == local_release_id:
                        local_object.current_release_id = None
                    db.delete(local_release)
                    result["created_releases"] = 0
                    result["runtime_entry"] = None
                    result["entry"]["local_release_id"] = None
                    result["entry"]["local_release_no"] = None
                    result["entry"]["status"] = "created_candidate_revision"
                source_release_map[source_release_id] = result["release"]
                imported_entries.append(result["entry"])
                runtime_entry = result.get("runtime_entry")
                if isinstance(runtime_entry, dict):
                    runtime_entries.append(runtime_entry)
                for key in (
                    "created_objects", "created_revisions", "created_releases",
                    "reused_objects", "reused_revisions", "reused_releases", "unarchived_objects",
                ):
                    summary[key] += int(result[key])
            self._audit(
                db,
                actor_id,
                "package.imported_to_workbench",
                "package",
                package_hash,
                {
                    "root": manifest.get("root") or {},
                    "created_objects": summary["created_objects"],
                    "created_revisions": summary["created_revisions"],
                    "created_releases": summary["created_releases"],
                    "reused_objects": summary["reused_objects"],
                    "reused_revisions": summary["reused_revisions"],
                    "reused_releases": summary["reused_releases"],
                    "unarchived_objects": summary["unarchived_objects"],
                },
            )
            db.commit()
        for entry in runtime_entries:
            self._install_runtime_entry(entry)
        return summary

    def list_deployments(self, environment: str = "prod") -> list[dict[str, Any]]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(WorkbenchDeploymentRow)
                .where(WorkbenchDeploymentRow.environment == environment)
                .order_by(WorkbenchDeploymentRow.imported_at.desc())
            )
            return [self._deployment_dict(row) for row in rows]

    def active_deployment_snapshot(self, environment: str = "prod") -> dict[str, Any] | None:
        with self.session_factory() as db:
            active_rows = list(db.scalars(
                select(WorkbenchDeploymentRow)
                .where(
                    WorkbenchDeploymentRow.environment == environment,
                    WorkbenchDeploymentRow.status == "active",
                )
                .order_by(WorkbenchDeploymentRow.activated_at.desc())
            ))
            row = next(
                (item for item in active_rows if str((item.manifest.get("root") or {}).get("object_type") or "") == "expert_team"),
                None,
            )
            if row is None:
                return None
            entries: list[dict[str, Any]] = []
            if row.package_bytes:
                try:
                    files = self._read_zip(bytes(row.package_bytes))
                    for item in row.manifest.get("objects", []):
                        entries.append(self._package_object_entry(item, files))
                except (WorkbenchError, UnicodeDecodeError, json.JSONDecodeError):
                    entries = []
            return {
                "deployment_id": row.deployment_id,
                "environment": row.environment,
                "root_release_id": row.root_release_id,
                "package_hash": row.package_hash,
                "kernel_fingerprint": row.manifest.get("kernel_fingerprint"),
                "root": row.manifest.get("root"),
                "deployment": {
                    "deployment_id": row.deployment_id,
                    "environment": row.environment,
                    "activated_by": row.activated_by,
                    "activated_at": _iso(row.activated_at),
                },
                "entries": entries or row.manifest.get("objects", []),
                "bound_at": _iso(utc_now()),
            }

    def list_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(WorkbenchAuditRow)
                .order_by(WorkbenchAuditRow.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
            )
            return [
                {
                    "event_id": row.event_id,
                    "actor_id": row.actor_id,
                    "action": row.action,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "payload": row.payload,
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]

    def activate_deployment(self, deployment_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            target = db.get(WorkbenchDeploymentRow, deployment_id)
            if target is None:
                raise WorkbenchError("部署记录不存在", code="DEPLOYMENT_NOT_FOUND")
            # Re-run package integrity/dependency/script checks immediately
            # before runtime installation.  File placement is intentionally
            # not an activation gate: imported packages may retain legacy
            # ``script/`` or other relative Python paths, while the runtime
            # loader only executes explicitly supported Skill script assets.
            self._validated_package_contents(
                target.package_bytes,
                enforce_python_location=False,
            )
            root_team_id = self._deployment_root_team_id(target, required=False)
            root_object_id = str((target.manifest.get("root") or {}).get("object_id") or "")
            active_rows = list(
                db.scalars(
                    select(WorkbenchDeploymentRow).where(
                        WorkbenchDeploymentRow.environment == target.environment,
                        WorkbenchDeploymentRow.status == "active",
                    )
                )
            )
            eligible_active = [
                item for item in active_rows
                if item.deployment_id != target.deployment_id
                and (
                    self._deployment_root_team_id(item, required=False) is not None
                    if root_team_id is not None
                    else str((item.manifest.get("root") or {}).get("object_id") or "") == root_object_id
                )
            ]
            previous = eligible_active[0] if eligible_active else None
            if previous is not None:
                previous.status = "superseded"
                target.previous_deployment_id = previous.deployment_id
            target.status = "active"
            target.activated_by = actor_id
            target.activated_at = utc_now()
            self._install_deployment_runtime(target)
            self._audit(db, actor_id, "deployment.activated", "deployment", target.deployment_id, {
                "previous_deployment_id": target.previous_deployment_id,
                "expert_team_id": root_team_id,
            })
            db.commit()
            return self._deployment_dict(target)

    def deactivate_deployment(self, deployment_id: str, *, expert_team_id: str, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            target = db.get(WorkbenchDeploymentRow, deployment_id)
            if target is None:
                raise WorkbenchError("部署记录不存在", code="DEPLOYMENT_NOT_FOUND")
            root_team_id = self._deployment_root_team_id(target)
            if target.status != "active":
                raise WorkbenchConflict("只能下线当前活跃的生产专家团", code="DEPLOYMENT_NOT_ACTIVE")
            if str(expert_team_id or "") != root_team_id:
                raise WorkbenchError("确认的 expert_team_id 与部署不一致", code="DEPLOYMENT_CONFIRMATION_MISMATCH")
            target.status = "deactivated"
            self._audit(db, actor_id, "deployment.deactivated", "deployment", target.deployment_id, {"expert_team_id": root_team_id})
            db.commit()
            return self._deployment_dict(target)

    def restore_deployment(self, deployment_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            target = db.get(WorkbenchDeploymentRow, deployment_id)
            if target is None:
                raise WorkbenchError("部署记录不存在", code="DEPLOYMENT_NOT_FOUND")
            root_team_id = self._deployment_root_team_id(target, required=False)
            if target.status not in {"superseded", "rolled_back", "deactivated"}:
                raise WorkbenchConflict("只能恢复历史生产专家团部署", code="DEPLOYMENT_RESTORE_FORBIDDEN")
            active_rows = list(db.scalars(select(WorkbenchDeploymentRow).where(
                WorkbenchDeploymentRow.environment == target.environment,
                WorkbenchDeploymentRow.status == "active",
            )))
            root_object_id = str((target.manifest.get("root") or {}).get("object_id") or "")
            previous = next((item for item in active_rows if item.deployment_id != target.deployment_id and (
                self._deployment_root_team_id(item, required=False) is not None
                if root_team_id is not None
                else str((item.manifest.get("root") or {}).get("object_id") or "") == root_object_id
            )), None)
            if previous is not None:
                previous.status = "superseded"
                target.previous_deployment_id = previous.deployment_id
            target.status = "active"
            target.activated_by = actor_id
            target.activated_at = utc_now()
            self._install_deployment_runtime(target)
            self._audit(db, actor_id, "deployment.restored", "deployment", target.deployment_id, {
                "previous_deployment_id": target.previous_deployment_id,
                "expert_team_id": root_team_id,
            })
            db.commit()
            return self._deployment_dict(target)

    def rollback_deployment(self, deployment_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            current = db.get(WorkbenchDeploymentRow, deployment_id)
            if current is None or current.status != "active":
                raise WorkbenchError("只能回滚当前激活部署", code="DEPLOYMENT_NOT_ACTIVE")
            previous = db.get(WorkbenchDeploymentRow, current.previous_deployment_id) if current.previous_deployment_id else None
            if previous is None:
                raise WorkbenchError("没有可回滚的历史部署", code="ROLLBACK_TARGET_NOT_FOUND")
            current.status = "rolled_back"
            previous.status = "active"
            previous.activated_by = actor_id
            previous.activated_at = utc_now()
            self._install_deployment_runtime(previous)
            self._audit(db, actor_id, "deployment.rolled_back", "deployment", current.deployment_id, {"restored_deployment_id": previous.deployment_id})
            db.commit()
            return self._deployment_dict(previous)

    def bootstrap_from_runtime(self) -> dict[str, int]:
        """Import the filesystem configuration once as immutable baseline releases."""
        if self.orchestrator is None:
            return {"objects": 0, "releases": 0}
        with self.session_factory() as db:
            existing = db.scalar(select(func.count()).select_from(WorkbenchObjectRow)) or 0
            if existing:
                return {"objects": 0, "releases": 0}
        skill_release_by_key: dict[str, dict[str, Any]] = {}
        expert_release_by_key: dict[str, dict[str, Any]] = {}
        created = 0
        runtime_registry = getattr(self.orchestrator, "runtime_registry", None)
        seen: set[str] = set()
        for bundle in getattr(runtime_registry, "bundles", {}).values():
            skill_key = str(bundle.contract.skill_id or "").strip()
            if not skill_key or skill_key in seen:
                continue
            seen.add(skill_key)
            payload = {
                "prompt_markdown": bundle.skill_markdown,
                "runtime_contract": self._json_file(bundle.root_dir / "runtime_contract.json"),
                "configuration": dict(bundle.runtime_metadata.raw or {}),
                "source_metadata": copy.deepcopy(bundle.metadata),
                "capability_ids": [f"{skill_key}:{path}" for path in sorted(bundle.scripts)],
                "source_version": str(bundle.runtime_metadata.version or bundle.metadata.get("version") or "0.0.0"),
                "_workbench_managed_files": True,
            }
            baseline_assets = []
            for resource in bundle.resources:
                if resource.resource_type not in {"reference", "asset", "script"}:
                    continue
                if not self._is_publishable_asset_path(resource.relative_path):
                    continue
                try:
                    relative_path = resource.relative_path
                    if resource.resource_type == "script" and relative_path.startswith("script/"):
                        relative_path = f"scripts/{relative_path[len('script/'):]}"
                    baseline_assets.append((relative_path, "application/octet-stream", resource.file_path.read_bytes()))
                except OSError:
                    continue
            released = self._bootstrap_object(
                "skill", skill_key, bundle.runtime_metadata.name or skill_key, payload, [], baseline_assets
            )
            skill_release_by_key[skill_key] = released
            created += 1
        experts = getattr(getattr(self.orchestrator, "expert_registry", None), "definitions", {})
        for expert in experts.values():
            locks = [skill_release_by_key[item.skill_id] for item in expert.skills if item.skill_id in skill_release_by_key]
            payload = {
                "rules_markdown": expert.rules_markdown,
                "brief": expert.brief,
                "budget": {"max_iters": expert.max_iters, "max_skill_calls": expert.max_skill_calls},
                "capabilities": list(expert.capabilities),
            }
            released = self._bootstrap_object("expert", expert.agent_id, expert.name, payload, locks)
            expert_release_by_key[expert.agent_id] = released
            created += 1
        teams = getattr(getattr(self.orchestrator, "expert_team_registry", None), "definitions", {})
        for team in teams.values():
            locks = [expert_release_by_key[item.expert_id] for item in team.members if item.expert_id in expert_release_by_key]
            coordinator_object_id = next((item["object_id"] for item in locks if item["object_key"] == team.coordinator_expert_id), "")
            payload = {
                "rules_markdown": team.rules_markdown,
                "brief": team.brief,
                "coordinator_expert_id": coordinator_object_id,
                "members": [
                    {"expert_id": item.expert_id, "mention_name": item.mention_name, "routing_brief": item.routing_brief}
                    for item in team.members
                ],
            }
            self._bootstrap_object("expert_team", team.team_id, team.name, payload, locks)
            created += 1
        return {"objects": created, "releases": created}

    def preview_runtime_migration(self, *, skills_only: bool = False) -> dict[str, Any]:
        """Inventory every business file before the one-time filesystem migration."""
        entries = self._runtime_migration_entries()
        if skills_only:
            entries = [entry for entry in entries if entry["object_type"] == "skill"]
        objects = []
        for entry in entries:
            files = entry.get("files") if isinstance(entry.get("files"), list) else []
            objects.append({
                "object_type": entry["object_type"],
                "object_key": entry["object_key"],
                "name": entry["name"],
                "dependency_keys": [item["object_key"] for item in entry.get("dependency_locks", [])],
                "file_count": len(files),
                "files": [item["relative_path"] for item in files],
                "content_hash": entry["content_hash"],
            })
        return {
            "mode": "preview",
            "source": "filesystem",
            "scope": "runtime_skills" if skills_only else "full_catalog",
            "object_count": len(objects),
            "file_count": sum(item["file_count"] for item in objects),
            "objects": objects,
            "catalog_hash": _hash([item["content_hash"] for item in objects]),
            "ignored_patterns": ["__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.tmp", "*.zip"],
        }

    def migrate_runtime_catalog(
        self,
        *,
        actor_id: str,
        make_current: bool = False,
        skills_only: bool = False,
    ) -> dict[str, Any]:
        """Idempotently copy the complete filesystem catalog into immutable DB releases.

        Importing and selecting a publication pointer are intentionally separate.
        ``make_current`` is an explicit operator choice, never a boot side effect.
        """
        entries = self._runtime_migration_entries()
        if skills_only:
            entries = [entry for entry in entries if entry["object_type"] == "skill"]
        package_hash = _hash({"source": "filesystem", "entries": [item["content_hash"] for item in entries]})
        source_release_map: dict[str, dict[str, Any]] = {}
        summary: dict[str, Any] = {
            "mode": "execute",
            "scope": "runtime_skills" if skills_only else "full_catalog",
            "package_hash": package_hash,
            "make_current": make_current,
            "created_objects": 0,
            "created_revisions": 0,
            "created_releases": 0,
            "reused_objects": 0,
            "reused_revisions": 0,
            "reused_releases": 0,
            "entries": [],
        }
        runtime_entries: list[dict[str, Any]] = []
        with self.session_factory() as db:
            for entry in entries:
                local_locks = []
                for lock in entry.get("dependency_locks", []):
                    dependency = source_release_map.get(str(lock["release_id"]))
                    if dependency is None:
                        raise WorkbenchError("文件目录存在缺失依赖", code="MIGRATION_DEPENDENCY_MISSING", details={"dependency": lock})
                    local_locks.append({key: dependency[key] for key in (
                        "object_id", "object_type", "object_key", "release_id", "release_no", "content_hash",
                    )})
                result = self._import_workbench_entry(
                    db,
                    entry,
                    local_dependency_locks=local_locks,
                    package_hash=package_hash,
                    actor_id=actor_id,
                    select_new_as_current=make_current,
                )
                source_release_map[str(entry["release_id"])] = result["release"]
                if make_current:
                    obj = self._require_object(db, result["release"]["object_id"])
                    obj.current_release_id = result["release"]["release_id"]
                    runtime_entry = self._runtime_entry_for_release(db, result["release"]["release_id"])
                    runtime_entries.append(runtime_entry)
                summary["entries"].append(result["entry"])
                for key in (
                    "created_objects", "created_revisions", "created_releases",
                    "reused_objects", "reused_revisions", "reused_releases",
                ):
                    summary[key] += int(result[key])
            self._audit(db, actor_id, "runtime_catalog.migrated", "catalog", package_hash, {
                key: summary[key] for key in summary if key not in {"entries"}
            })
            db.commit()
        for entry in runtime_entries:
            self._install_runtime_entry(entry)
        return summary

    def _runtime_migration_entries(self) -> list[dict[str, Any]]:
        if self.orchestrator is None:
            raise WorkbenchError("运行时目录不可用", code="RUNTIME_CATALOG_UNAVAILABLE")
        entries: list[dict[str, Any]] = []
        source_ids: dict[tuple[str, str], str] = {}
        seen: set[str] = set()
        for bundle in getattr(getattr(self.orchestrator, "runtime_registry", None), "bundles", {}).values():
            key = str(bundle.contract.skill_id or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            files = self._directory_files(Path(bundle.root_dir), reserved={"SKILL.md", "skill.md", "runtime_contract.json"})
            payload = {
                "prompt_markdown": bundle.skill_markdown,
                "runtime_contract": self._json_file(Path(bundle.root_dir) / "runtime_contract.json"),
                "configuration": copy.deepcopy(bundle.runtime_metadata.raw or {}),
                "source_metadata": copy.deepcopy(bundle.metadata),
                "capability_ids": [f"{key}:{path}" for path in sorted(bundle.scripts)],
                "source_version": str(bundle.runtime_metadata.version or bundle.metadata.get("version") or "0.0.0"),
                "_workbench_managed_files": True,
            }
            source_id = f"filesystem:skill:{key}"
            source_ids[("skill", key)] = source_id
            entries.append(self._filesystem_entry("skill", key, bundle.runtime_metadata.name or key, payload, [], files, source_id))
        for expert in getattr(getattr(self.orchestrator, "expert_registry", None), "definitions", {}).values():
            locks = [self._filesystem_lock("skill", item.skill_id, source_ids) for item in expert.skills]
            payload = {
                "rules_markdown": expert.rules_markdown,
                "brief": expert.brief,
                "budget": {"max_iters": expert.max_iters, "max_skill_calls": expert.max_skill_calls},
                "capabilities": list(expert.capabilities),
            }
            source_id = f"filesystem:expert:{expert.agent_id}"
            source_ids[("expert", expert.agent_id)] = source_id
            entries.append(self._filesystem_entry("expert", expert.agent_id, expert.name, payload, locks, [], source_id))
        for team in getattr(getattr(self.orchestrator, "expert_team_registry", None), "definitions", {}).values():
            locks = [self._filesystem_lock("expert", item.expert_id, source_ids) for item in team.members]
            coordinator_lock = next((item for item in locks if item["object_key"] == team.coordinator_expert_id), None)
            payload = {
                "rules_markdown": team.rules_markdown,
                "brief": team.brief,
                "coordinator_expert_id": str((coordinator_lock or {}).get("object_id") or team.coordinator_expert_id),
                "members": [
                    {"expert_id": item.expert_id, "mention_name": item.mention_name, "routing_brief": item.routing_brief}
                    for item in team.members
                ],
            }
            source_id = f"filesystem:expert_team:{team.team_id}"
            source_ids[("expert_team", team.team_id)] = source_id
            entries.append(self._filesystem_entry("expert_team", team.team_id, team.name, payload, locks, [], source_id))
        return entries

    def _filesystem_entry(self, object_type, object_key, name, payload, locks, files, source_id):
        content_hash = _hash({"payload": payload, "dependency_locks": locks, "files": [
            {key: item[key] for key in ("relative_path", "size_bytes", "content_hash")} for item in files
        ]})
        return {
            "object_type": object_type, "object_key": object_key, "name": name,
            "release_id": source_id, "release_no": 1, "payload": payload,
            "dependency_locks": locks, "files": files, "content_hash": content_hash,
        }

    @staticmethod
    def _filesystem_lock(object_type: str, object_key: str, source_ids: dict[tuple[str, str], str]) -> dict[str, Any]:
        source_id = source_ids.get((object_type, object_key))
        if source_id is None:
            raise WorkbenchError("文件目录引用了不存在的对象", code="MIGRATION_DEPENDENCY_MISSING", details={
                "object_type": object_type, "object_key": object_key,
            })
        return {"object_id": f"filesystem:{object_type}:{object_key}", "object_type": object_type,
                "object_key": object_key, "release_id": source_id, "release_no": 1, "content_hash": "filesystem"}

    def _directory_files(self, root: Path, *, reserved: set[str]) -> list[dict[str, Any]]:
        if not root.is_dir():
            return []
        result = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root)
            if relative.as_posix() in reserved or self._ignored_migration_file(relative):
                continue
            content = path.read_bytes()
            result.append({
                "relative_path": relative.as_posix(),
                "media_type": self._media_type(relative),
                "size_bytes": len(content),
                "content_hash": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            })
        return result

    @staticmethod
    def _ignored_migration_file(path: Path) -> bool:
        return (
            any(part in {"__pycache__", ".git", ".pytest_cache"} for part in path.parts)
            or path.name in {".DS_Store", "Thumbs.db"}
            or path.suffix.lower() in {".pyc", ".pyo", ".tmp", ".swp", ".zip"}
        )

    @staticmethod
    def _media_type(path: Path) -> str:
        return {
            ".json": "application/json", ".yaml": "application/yaml", ".yml": "application/yaml",
            ".md": "text/markdown", ".txt": "text/plain", ".py": "text/x-python", ".csv": "text/csv",
        }.get(path.suffix.lower(), "application/octet-stream")

    def _runtime_entry_for_release(self, db, release_id: str) -> dict[str, Any]:
        release = self._require_release(db, release_id)
        obj = self._require_object(db, release.object_id)
        revision = self._require_revision(db, release.revision_id)
        return {
            "object_id": obj.object_id, "object_type": obj.object_type, "object_key": obj.object_key,
            "name": obj.name, "release_id": release.release_id, "release_no": release.release_no,
            "payload": revision.payload, "dependency_locks": release.dependency_locks,
            "content_hash": revision.content_hash, "files": self._revision_files(db, revision.revision_id),
        }

    def install_runtime_catalog(self) -> None:
        """Install the latest released declarations into the shared runtime registries."""
        if self.orchestrator is None:
            return
        with self.session_factory() as db:
            entries: list[dict[str, Any]] = []
            for obj in db.scalars(select(WorkbenchObjectRow).where(WorkbenchObjectRow.archived.is_(False))):
                release = db.scalar(
                    select(WorkbenchReleaseRow)
                    .where(WorkbenchReleaseRow.release_id == obj.current_release_id, WorkbenchReleaseRow.archived.is_(False))
                    .order_by(WorkbenchReleaseRow.release_no.desc())
                    .limit(1)
                )
                if release is None:
                    continue
                revision = self._require_revision(db, release.revision_id)
                entries.append({
                    "object_id": obj.object_id, "object_type": obj.object_type, "object_key": obj.object_key,
                    "name": obj.name, "release_id": release.release_id, "release_no": release.release_no,
                    "payload": revision.payload, "dependency_locks": release.dependency_locks,
                    "content_hash": revision.content_hash, "files": self._revision_files(db, revision.revision_id),
                })
        for entry in sorted(entries, key=lambda item: {"skill": 0, "expert": 1, "expert_team": 2}.get(item["object_type"], 9)):
            self._install_runtime_entry(entry)

    def install_active_deployment_runtime(self, environment: str = "prod") -> None:
        """Restore the active immutable ZIP after a process restart."""
        if self.orchestrator is None:
            return
        with self.session_factory() as db:
            deployment = db.scalar(
                select(WorkbenchDeploymentRow)
                .where(
                    WorkbenchDeploymentRow.environment == environment,
                    WorkbenchDeploymentRow.status == "active",
                )
                .order_by(WorkbenchDeploymentRow.activated_at.desc())
                .limit(1)
            )
            if deployment is not None:
                self._install_deployment_runtime(deployment)

    def _install_deployment_runtime(self, deployment: WorkbenchDeploymentRow) -> None:
        """Mount an immutable deployment archive for the formal runtime shell."""
        if not deployment.package_bytes:
            return
        try:
            files = self._read_zip(bytes(deployment.package_bytes))
            entries = [self._package_object_entry(item, files) for item in deployment.manifest.get("objects", [])]
            for entry in sorted(entries, key=lambda item: {"skill": 0, "expert": 1, "expert_team": 2}.get(item.get("object_type"), 9)):
                self._install_runtime_entry(entry)
            # A process may have booted from the filesystem fallback before
            # the first production deployment.  Once an expert-team package
            # is activated, make its coordinator the runtime default as well;
            # new sessions and health checks then observe the same package
            # without requiring a second process restart.
            root = deployment.manifest.get("root") if isinstance(deployment.manifest, dict) else None
            if isinstance(root, dict) and root.get("object_type") == "expert_team" and self.orchestrator is not None:
                team_id = str(root.get("object_key") or "")
                team = self.orchestrator.expert_team_registry.get(team_id)
                if team is not None and getattr(self.orchestrator, "expert_runtime", None) is not None:
                    self.orchestrator.expert_runtime.default_expert_id = team.coordinator_expert_id
        except (WorkbenchError, UnicodeDecodeError, json.JSONDecodeError):
            # The archive was fully validated at import; keep activation status
            # durable even if a runtime shell cannot currently be mounted.
            return

    def _uninstall_runtime_object(self, object_type: str, object_key: str) -> None:
        if self.orchestrator is None:
            return
        if object_type == "skill":
            registry = getattr(self.orchestrator, "runtime_registry", None)
            if registry is not None:
                registry.bundles.pop(object_key, None)
                enabled_by_id = getattr(registry, "enabled_by_id", None)
                if isinstance(enabled_by_id, dict):
                    enabled_by_id.pop(object_key, None)
            return
        if object_type == "expert":
            for registry in (
                getattr(self.orchestrator, "expert_registry", None),
                getattr(getattr(self.orchestrator, "expert_runtime", None), "expert_registry", None),
            ):
                if registry is not None and isinstance(getattr(registry, "definitions", None), dict):
                    registry.definitions.pop(object_key, None)
            return
        if object_type == "expert_team":
            for registry in (
                getattr(self.orchestrator, "expert_team_registry", None),
                getattr(getattr(self.orchestrator, "expert_runtime", None), "team_registry", None),
            ):
                if registry is not None and isinstance(getattr(registry, "definitions", None), dict):
                    registry.definitions.pop(object_key, None)

    def _deletion_blockers(self, db, obj: WorkbenchObjectRow) -> list[dict[str, Any]]:
        release_ids = set(
            db.scalars(select(WorkbenchReleaseRow.release_id).where(WorkbenchReleaseRow.object_id == obj.object_id))
        )
        blockers: list[dict[str, Any]] = []
        owners = db.scalars(
            select(WorkbenchObjectRow).where(
                WorkbenchObjectRow.object_id != obj.object_id,
                WorkbenchObjectRow.archived.is_(False),
            )
        )
        for owner in owners:
            # Only the current published configuration is live.  Drafts and
            # superseded releases are retained as history, not delete blockers.
            release = db.scalar(
                select(WorkbenchReleaseRow)
                .where(
                    WorkbenchReleaseRow.release_id == owner.current_release_id,
                    WorkbenchReleaseRow.archived.is_(False),
                )
                .order_by(WorkbenchReleaseRow.release_no.desc())
                .limit(1)
            )
            if release is None:
                continue
            locks = release.dependency_locks or []
            if any(
                str(lock.get("object_id") or "") == obj.object_id
                or str(lock.get("release_id") or "") in release_ids
                for lock in locks
                if isinstance(lock, dict)
            ):
                blockers.append({
                    "kind": "revision",
                    "object_id": owner.object_id,
                    "object_type": owner.object_type,
                    "object_key": owner.object_key,
                    "name": owner.name,
                    "release_id": release.release_id,
                    "release_no": release.release_no,
                    "message": f"{owner.name} 的当前发布版本 v{release.release_no} 正在引用该对象",
                })
        return blockers

    def _validate_available_object_key(self, db, object_type: str, raw_key: str) -> str:
        key = str(raw_key or "").strip()
        if not key:
            raise WorkbenchError("新的对象 ID 不能为空", code="ID_MIGRATION_KEY_REQUIRED")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key):
            raise WorkbenchError("对象标识只能包含字母、数字、下划线和连字符", code="ID_MIGRATION_KEY_INVALID")
        existing = db.scalar(select(WorkbenchObjectRow).where(
            WorkbenchObjectRow.object_type == object_type,
            WorkbenchObjectRow.object_key == key,
        ))
        if existing is not None:
            raise WorkbenchConflict("同类型对象标识已存在", code="OBJECT_KEY_CONFLICT")
        return key

    @staticmethod
    def _latest_release_no(db, object_id: str) -> int:
        return int(db.scalar(select(func.max(WorkbenchReleaseRow.release_no)).where(
            WorkbenchReleaseRow.object_id == object_id
        )) or 0)

    def _copy_candidate_revision(
        self,
        db,
        *,
        source_revision: WorkbenchRevisionRow,
        target: WorkbenchObjectRow,
        payload: dict[str, Any],
        requested_locks: list[dict[str, Any]],
        actor_id: str,
        base_revision_id: str | None,
    ) -> WorkbenchRevisionRow:
        """Persist a new candidate revision inside an existing transaction."""
        if target.object_type == "skill":
            payload = {**payload, "_workbench_managed_files": True}
        canonical_locks = self._validate_dependencies(db, target.object_type, payload, requested_locks)
        source_assets = list(db.scalars(select(WorkbenchAssetRow).where(
            WorkbenchAssetRow.revision_id == source_revision.revision_id
        ).order_by(WorkbenchAssetRow.relative_path)))
        files = [(item.relative_path, item.media_type, bytes(item.content)) for item in source_assets]
        validation = self._validate_payload(target.object_type, payload, canonical_locks)
        if target.object_type == "skill":
            script_errors = self._script_validation_errors(files)
            if script_errors:
                validation = {**validation, "valid": False, "errors": [*validation.get("errors", []), *script_errors]}
        latest = db.scalar(select(WorkbenchRevisionRow).where(
            WorkbenchRevisionRow.object_id == target.object_id
        ).order_by(WorkbenchRevisionRow.revision_no.desc()).limit(1))
        expected_base = latest.revision_id if latest else None
        if expected_base != base_revision_id:
            raise WorkbenchConflict("配置已被其他修订更新，请刷新后重试", code="REVISION_BASE_CONFLICT")
        revision = WorkbenchRevisionRow(
            revision_id=_id("rev"), object_id=target.object_id,
            revision_no=(latest.revision_no if latest else 0) + 1,
            base_revision_id=base_revision_id, payload=payload,
            dependency_locks=canonical_locks, validation=validation,
            content_hash=_hash({
                "payload": payload, "dependency_locks": canonical_locks,
                "assets": [{"relative_path": path, "media_type": media_type, "size_bytes": len(content), "content_hash": hashlib.sha256(content).hexdigest()} for path, media_type, content in files],
            }), created_by=actor_id,
        )
        db.add(revision)
        db.flush()
        for path, media_type, content in files:
            db.add(WorkbenchAssetRow(
                asset_id=_id("asset"), revision_id=revision.revision_id,
                relative_path=path, media_type=media_type, size_bytes=len(content),
                content_hash=hashlib.sha256(content).hexdigest(), content=content,
            ))
        target.updated_at = utc_now()
        self._audit(db, actor_id, "revision.saved", target.object_type, revision.revision_id, {"revision_no": revision.revision_no, "migration": True})
        return revision

    def _reference_migration_candidates(self, db, from_release_id: str, *, include_rows: bool = False):
        """Only the current revision of each direct owner can receive a new draft."""
        rows = db.execute(
            select(WorkbenchRevisionRow, WorkbenchObjectRow)
            .join(WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchRevisionRow.object_id)
            .order_by(WorkbenchRevisionRow.object_id, WorkbenchRevisionRow.revision_no.desc())
        )
        candidates: list[tuple[WorkbenchRevisionRow, WorkbenchObjectRow]] = []
        seen: set[str] = set()
        for revision, owner in rows:
            if owner.object_id in seen:
                continue
            seen.add(owner.object_id)
            if any(isinstance(lock, dict) and lock.get("release_id") == from_release_id for lock in (revision.dependency_locks or [])):
                candidates.append((revision, owner))
        if include_rows:
            return candidates
        return [{
            "object_id": owner.object_id, "object_type": owner.object_type,
            "object_key": owner.object_key, "name": owner.name,
            "revision_id": revision.revision_id, "revision_no": revision.revision_no,
        } for revision, owner in candidates]

    def _install_runtime_entry(self, entry: dict[str, Any]) -> None:
        if self.orchestrator is None:
            return
        object_type = str(entry.get("object_type") or "")
        object_key = str(entry.get("object_key") or "")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        locks = entry.get("dependency_locks") if isinstance(entry.get("dependency_locks"), list) else []
        if not object_key:
            return
        if object_type == "skill":
            registry = self.orchestrator.runtime_registry
            registry.bundles[object_key] = skill_bundle_from_entry(entry)
            return
        if object_type == "expert":
            from hailiang_skills.runtime_bridge.expert_bundle import ExpertDefinition, LockedSkill

            budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
            definition = ExpertDefinition(
                agent_id=object_key,
                name=str(entry.get("name") or object_key),
                rules_markdown=str(payload.get("rules_markdown") or ""),
                skills=tuple(LockedSkill(str(item.get("object_key") or ""), f"v{int(item.get('release_no') or 0)}") for item in locks),
                brief=str(payload.get("brief") or ""),
                max_iters=max(1, min(int(budget.get("max_iters", 4)), 4)),
                max_skill_calls=max(1, min(int(budget.get("max_skill_calls", 3)), 3)),
                capabilities=tuple(str(item) for item in payload.get("capabilities", []) if str(item)) or (
                    "execute_skill", "request_declared_form", "read_effective_facts",
                ),
            )
            self.orchestrator.expert_registry.definitions[object_key] = definition
            self.orchestrator.expert_runtime.expert_registry.definitions[object_key] = definition
            return
        if object_type == "expert_team":
            from hailiang_skills.runtime_bridge.expert_team_bundle import ExpertTeamDefinition, ExpertTeamMember

            coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
            coordinator = next((str(item.get("object_key") or "") for item in locks if str(item.get("object_id") or "") == coordinator_object_id), "")
            member_payloads = payload.get("members") if isinstance(payload.get("members"), list) else []
            by_key = {str(item.get("expert_id") or ""): item for item in member_payloads if isinstance(item, dict)}
            members = tuple(
                ExpertTeamMember(
                    expert_id=str(item.get("object_key") or ""),
                    mention_name=str((by_key.get(str(item.get("object_key") or "")) or {}).get("mention_name") or item.get("object_key") or ""),
                    routing_brief=str((by_key.get(str(item.get("object_key") or "")) or {}).get("routing_brief") or ""),
                )
                for item in locks if str(item.get("object_key") or "")
            )
            if coordinator and members:
                definition = ExpertTeamDefinition(
                    team_id=object_key,
                    name=str(entry.get("name") or object_key),
                    rules_markdown=str(payload.get("rules_markdown") or ""),
                    coordinator_expert_id=coordinator,
                    members=members,
                    brief=str(payload.get("brief") or ""),
                )
                self.orchestrator.expert_team_registry.definitions[object_key] = definition
                self.orchestrator.expert_runtime.team_registry.definitions[object_key] = definition

    def _bootstrap_object(
        self,
        object_type: str,
        object_key: str,
        name: str,
        payload: dict[str, Any],
        dependencies: list[dict[str, Any]],
        assets: list[tuple[str, str, bytes]] | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as db:
            obj = WorkbenchObjectRow(
                object_id=_id("obj"), object_type=object_type, object_key=object_key, name=name, created_by="system"
            )
            db.add(obj)
            db.flush()
            locks = [
                {key: item[key] for key in ("object_id", "object_type", "object_key", "release_id", "release_no", "content_hash")}
                for item in dependencies
            ]
            validation = self._validate_payload(object_type, payload, locks)
            asset_items = assets or []
            revision = WorkbenchRevisionRow(
                revision_id=_id("rev"), object_id=obj.object_id, revision_no=1, payload=payload,
                dependency_locks=locks, validation=validation, content_hash=_hash({
                    "payload": payload,
                    "dependency_locks": locks,
                    "assets": [
                        {"relative_path": path, "media_type": media_type, "size_bytes": len(content), "content_hash": hashlib.sha256(content).hexdigest()}
                        for path, media_type, content in asset_items
                    ],
                }), created_by="system"
            )
            db.add(revision)
            db.flush()
            for path, media_type, content in asset_items:
                db.add(WorkbenchAssetRow(
                    asset_id=_id("asset"), revision_id=revision.revision_id,
                    relative_path=_safe_relative_path(path), media_type=media_type,
                    size_bytes=len(content), content_hash=hashlib.sha256(content).hexdigest(), content=content,
                ))
            release = WorkbenchReleaseRow(
                release_id=_id("rel"), object_id=obj.object_id, revision_id=revision.revision_id, release_no=1,
                dependency_locks=locks, content_hash=revision.content_hash,
                verification={"evidence_type": "filesystem_baseline", "manual_confirmation": True}, published_by="system"
            )
            db.add(release)
            obj.current_release_id = release.release_id
            db.commit()
            return self._release_dict(release, obj)

    def _validate_dependencies(
        self,
        db,
        object_type: str,
        payload: dict[str, Any],
        requested_locks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        expected_type = {"skill": None, "expert": "skill", "expert_team": "expert"}[object_type]
        if expected_type is None and requested_locks:
            raise WorkbenchError("Skill 不能依赖其他业务对象")
        locks: list[dict[str, Any]] = []
        seen_release_ids: set[str] = set()
        seen_object_ids: set[str] = set()
        for requested in requested_locks:
            release_id = str(requested.get("release_id") or "").strip()
            if not release_id or release_id in seen_release_ids:
                raise WorkbenchError("依赖版本不能为空或重复")
            release = self._require_release(db, release_id)
            obj = self._require_object(db, release.object_id)
            if obj.object_type != expected_type or release.archived or obj.archived:
                raise WorkbenchError(f"{object_type} 只能引用未归档的已发布 {expected_type} 版本")
            if obj.object_id in seen_object_ids:
                raise WorkbenchError(
                    "同一依赖对象只能锁定一个发布版本",
                    code="DEPENDENCY_OBJECT_VERSION_CONFLICT",
                    details={"object_id": obj.object_id, "object_key": obj.object_key},
                )
            seen_release_ids.add(release_id)
            seen_object_ids.add(obj.object_id)
            locks.append({
                "object_id": obj.object_id,
                "object_type": obj.object_type,
                "object_key": obj.object_key,
                "release_id": release.release_id,
                "release_no": release.release_no,
                "content_hash": release.content_hash,
            })
        return locks

    @staticmethod
    def _validate_unique_dependency_objects(locks: list[dict[str, Any]]) -> None:
        """Reject imported packages that lock multiple versions of one object."""
        seen_object_ids: set[str] = set()
        for lock in locks:
            object_id = str(lock.get("object_id") or "").strip()
            if not object_id:
                raise WorkbenchError("导入包中的依赖对象不能为空", code="PACKAGE_DEPENDENCY_INVALID")
            if object_id in seen_object_ids:
                raise WorkbenchError(
                    "导入包中同一依赖对象锁定了多个发布版本",
                    code="DEPENDENCY_OBJECT_VERSION_CONFLICT",
                    details={"object_id": object_id, "object_key": str(lock.get("object_key") or "")},
                )
            seen_object_ids.add(object_id)

    def _validate_payload(
        self,
        object_type: str,
        payload: dict[str, Any],
        locks: list[dict[str, Any]],
        *,
        known_capabilities: set[str] | None = None,
        allow_legacy_brief: bool = False,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if object_type == "skill":
            if not str(payload.get("prompt_markdown") or "").strip():
                errors.append("Skill Prompt 不能为空")
            runtime_contract = payload.get("runtime_contract") if isinstance(payload.get("runtime_contract"), dict) else {}
            legacy_contract_keys = sorted(set(runtime_contract) - {"facts"})
            if legacy_contract_keys:
                warnings.append(
                    "runtime_contract.json 中的历史字段仍可兼容读取，但新 Skill 只应保留 facts："
                    + ", ".join(legacy_contract_keys)
                )
            metadata = payload.get("configuration") or payload.get("source_metadata") or {}
            questionnaire = metadata.get("questionnaire") if isinstance(metadata, dict) else {}
            if not isinstance(questionnaire, dict) or not questionnaire:
                questionnaire = runtime_contract.get("questionnaire") if isinstance(runtime_contract.get("questionnaire"), dict) else {}
            config_json = questionnaire.get("config_json")
            if config_json is not None:
                errors.extend(validate_questionnaire_config(config_json))
            known = known_capabilities if known_capabilities is not None else {
                item["capability_id"] for item in capability_catalog(self.orchestrator)
            }
            unknown = sorted(set(payload.get("capability_ids") or []) - known)
            if unknown:
                errors.append(f"存在未知底层能力: {', '.join(unknown)}")
        elif object_type == "expert":
            brief = _normalize_brief(payload.get("brief"))
            if not brief:
                (warnings if allow_legacy_brief else errors).append("专家 Brief 不能为空" if not allow_legacy_brief else "历史专家包缺少 Brief，请补齐后再发布")
            elif len(brief) > 120:
                errors.append("专家 Brief 不能超过 120 字")
            if not str(payload.get("rules_markdown") or "").strip():
                errors.append("专家规则不能为空")
            if len(locks) < 1:
                errors.append("专家必须引用至少一个已发布 Skill 版本")
            errors.extend(validate_agent_skill_routing(
                str(payload.get("rules_markdown") or ""),
                {str(item.get("object_key") or "") for item in locks if str(item.get("object_key") or "")},
            ))
            budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
            for key, maximum in (("max_iters", 4), ("max_skill_calls", 3)):
                value = int(budget.get(key, maximum) or maximum)
                if value < 1 or value > maximum:
                    errors.append(f"{key} 必须在 1-{maximum} 之间")
        elif object_type == "expert_team":
            brief = _normalize_brief(payload.get("brief"))
            if not brief:
                (warnings if allow_legacy_brief else errors).append("专家团 Brief 不能为空" if not allow_legacy_brief else "历史专家团包缺少 Brief，请补齐后再发布")
            elif len(brief) > 120:
                errors.append("专家团 Brief 不能超过 120 字")
            if len(locks) < 2:
                errors.append("专家团必须引用至少两个已发布专家版本")
            coordinator = str(payload.get("coordinator_expert_id") or "")
            if not coordinator or coordinator not in {item["object_id"] for item in locks}:
                errors.append("主协调专家必须属于专家团成员")
            members = payload.get("members") if isinstance(payload.get("members"), list) else []
            # Legacy revisions did not persist member presentation fields.
            # They remain readable; every current workbench save writes the
            # complete form below and thereafter receives strict validation.
            if members:
                locked_keys = {item["object_key"] for item in locks}
                member_keys = [str(item.get("expert_id") or "").strip() for item in members if isinstance(item, dict)]
                if set(member_keys) != locked_keys or len(member_keys) != len(locked_keys):
                    errors.append("专家团成员配置必须与锁定的专家版本一一对应")
                mentions = [
                    str(item.get("mention_name") or "").strip()
                    for item in members
                    if isinstance(item, dict)
                ]
                if not all(mentions) or len(set(mentions)) != len(mentions):
                    errors.append("成员展示名称不能为空且必须在专家团内唯一")
            if not str(payload.get("rules_markdown") or "").strip():
                warnings.append("建议补充专家团协同规则")
        return {"valid": not errors, "errors": errors, "warnings": warnings, "checked_at": _iso(utc_now())}

    def _decode_assets(
        self,
        assets: list[dict[str, Any]],
        *,
        object_type: str,
        allow_misplaced_python: bool = False,
    ) -> list[tuple[str, str, bytes]]:
        result: list[tuple[str, str, bytes]] = []
        seen: set[str] = set()
        for item in assets:
            path = _safe_relative_path(str(item.get("relative_path") or ""))
            reserved_names = {"SKILL.md", "skill.md", "runtime_contract.json", "AGENT.md", "agent.yaml", "TEAM.md", "team.yaml", "skills.lock.json", "experts.lock.json", "object.json"}
            if path in seen or PurePosixPath(path).name in reserved_names:
                raise WorkbenchError("附件路径重复或使用了保留文件名", code="INVALID_ASSET_PATH")
            if path.startswith("scripts/"):
                if object_type != "skill":
                    raise WorkbenchError("只有 Skill 可以配置 Python 脚本", code="INVALID_ASSET_PATH")
                if PurePosixPath(path).suffix.lower() not in {".py", ".txt"}:
                    raise WorkbenchError("scripts/ 仅支持 .py 和 requirements.txt", code="INVALID_ASSET_PATH")
                if PurePosixPath(path).suffix.lower() == ".txt" and PurePosixPath(path).name != "requirements.txt":
                    raise WorkbenchError("scripts/ 下只允许 requirements.txt 文本文件", code="INVALID_ASSET_PATH")
            elif PurePosixPath(path).suffix.lower() == ".py" and not allow_misplaced_python:
                raise WorkbenchError("Python 脚本只能位于 Skill 的 scripts/ 目录", code="INVALID_ASSET_PATH")
            if PurePosixPath(path).suffix.lower() in EXECUTABLE_SUFFIXES:
                raise WorkbenchError("资料附件不得包含可执行代码", code="EXECUTABLE_CONTENT_FORBIDDEN")
            try:
                content = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
            except ValueError as exc:
                raise WorkbenchError("附件内容不是合法 Base64", code="INVALID_ASSET_CONTENT") from exc
            if len(content) > MAX_ASSET_BYTES:
                raise WorkbenchError("单个附件超过大小限制", code="ASSET_TOO_LARGE")
            seen.add(path)
            result.append((path, str(item.get("media_type") or "application/octet-stream"), content))
        return result

    @staticmethod
    def _script_validation_errors(files: list[tuple[str, str, bytes]]) -> list[str]:
        script_files = [item for item in files if item[0].startswith("scripts/")]
        if not script_files:
            return []
        with tempfile.TemporaryDirectory(prefix="hailiang-workbench-review-") as temp_dir:
            root = Path(temp_dir)
            for relative_path, _media_type, content in script_files:
                target = root.joinpath(*PurePosixPath(relative_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return [item.detail for item in review_scripts(root) if item.status == "error"]

    @staticmethod
    def _asset_dict(row: WorkbenchAssetRow, *, include_content: bool = False) -> dict[str, Any]:
        result = {
            "asset_id": row.asset_id,
            "revision_id": row.revision_id,
            "relative_path": row.relative_path,
            "media_type": row.media_type,
            "size_bytes": row.size_bytes,
            "content_hash": row.content_hash,
        }
        if include_content:
            result["content_base64"] = base64.b64encode(bytes(row.content)).decode("ascii")
        return result

    def _revision_files(self, db, revision_id: str) -> list[dict[str, Any]]:
        result = [
            self._asset_dict(row, include_content=True)
            for row in db.scalars(
                select(WorkbenchAssetRow)
                .where(WorkbenchAssetRow.revision_id == revision_id)
                .order_by(WorkbenchAssetRow.relative_path)
            )
            if self._is_publishable_asset_path(row.relative_path)
        ]
        existing_paths = {item["relative_path"] for item in result}
        if self.orchestrator is None:
            return result
        revision = self._require_revision(db, revision_id)
        obj = self._require_object(db, revision.object_id)
        if obj.object_type != "skill" or bool((revision.payload or {}).get("_workbench_managed_files")):
            return result
        bundle = self.orchestrator.runtime_registry.get_raw(obj.object_key)
        for resource in getattr(bundle, "resources", ()) if bundle is not None else ():
            if resource.resource_type not in {"reference", "asset", "script"}:
                continue
            relative_path = resource.relative_path
            if resource.resource_type == "script" and relative_path.startswith("script/"):
                relative_path = f"scripts/{relative_path[len('script/'):]}"
            if relative_path in existing_paths or not self._is_publishable_asset_path(relative_path):
                continue
            try:
                content = resource.file_path.read_bytes()
            except OSError:
                continue
            result.append({
                "relative_path": relative_path,
                "media_type": "text/x-python" if relative_path.endswith(".py") else "application/octet-stream",
                "size_bytes": len(content),
                "content_hash": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
                "inherited_from_runtime": True,
            })
            existing_paths.add(relative_path)
        return sorted(result, key=lambda item: item["relative_path"])

    @staticmethod
    def _is_publishable_asset_path(relative_path: str) -> bool:
        path = PurePosixPath(str(relative_path or ""))
        return "__pycache__" not in path.parts and path.suffix.lower() != ".pyc"

    def _snapshot_for_revision(self, db, revision: WorkbenchRevisionRow, *, soul_revision_id: str | None = None) -> dict[str, Any]:
        obj = self._require_object(db, revision.object_id)
        entries = [{
            "object_id": obj.object_id,
            "object_type": obj.object_type,
            "object_key": obj.object_key,
            "name": obj.name,
            "revision_id": revision.revision_id,
            "revision_no": revision.revision_no,
            "content_hash": revision.content_hash,
            "payload": revision.payload,
            "dependency_locks": revision.dependency_locks,
            "files": self._revision_files(db, revision.revision_id),
        }]
        visited: set[str] = set()
        for lock in revision.dependency_locks or []:
            self._append_release_snapshot(db, str(lock["release_id"]), entries, visited)
        root = {"kind": "revision", "object_id": obj.object_id, "revision_id": revision.revision_id}
        snapshot = {"root": root, "entries": entries, "kernel_fingerprint": self.kernel_fingerprint}
        if soul_revision_id:
            soul = self._require_soul_revision(db, soul_revision_id)
            snapshot["soul_context"] = self._soul_revision_dict(soul)
        else:
            snapshot["soul_context"] = {"enabled": False, "content": "", "content_hash": None}
        snapshot["snapshot_hash"] = _hash(snapshot)
        snapshot["entries"] = [materialize_entry_files(item) for item in entries]
        return snapshot

    def _append_release_snapshot(self, db, release_id: str, entries: list[dict[str, Any]], visited: set[str]) -> None:
        if release_id in visited:
            return
        visited.add(release_id)
        release = self._require_release(db, release_id)
        revision = self._require_revision(db, release.revision_id)
        obj = self._require_object(db, release.object_id)
        entries.append({
            "object_id": obj.object_id,
            "object_type": obj.object_type,
            "object_key": obj.object_key,
            "name": obj.name,
            "release_id": release.release_id,
            "release_no": release.release_no,
            "content_hash": release.content_hash,
            "payload": revision.payload,
            "dependency_locks": release.dependency_locks,
            "files": self._revision_files(db, revision.revision_id),
        })
        for lock in release.dependency_locks or []:
            self._append_release_snapshot(db, str(lock["release_id"]), entries, visited)

    def _release_closure(self, db, root: WorkbenchReleaseRow) -> list[tuple[Any, Any, Any]]:
        result: list[tuple[Any, Any, Any]] = []
        visited: set[str] = set()
        identities: dict[tuple[str, str], str] = {}

        def visit(release: WorkbenchReleaseRow) -> None:
            if release.release_id in visited:
                return
            visited.add(release.release_id)
            obj = self._require_object(db, release.object_id)
            identity = (obj.object_type, obj.object_key)
            if identity in identities and identities[identity] != release.release_id:
                raise WorkbenchConflict("依赖闭包含同 ID 的不同版本，请先统一上游依赖", code="DEPENDENCY_VERSION_CONFLICT")
            identities[identity] = release.release_id
            revision = self._require_revision(db, release.revision_id)
            result.append((release, obj, revision))
            for lock in release.dependency_locks or []:
                visit(self._require_release(db, str(lock["release_id"])))

        visit(root)
        return result

    def _require_evidence(self, db, revision_id: str, evidence_id: str) -> str:
        debug = db.get(WorkbenchDebugSessionRow, evidence_id)
        if debug is not None and debug.revision_id == revision_id and debug.status == "completed":
            return "debug_session"
        evaluation = db.get(WorkbenchEvaluationRunRow, evidence_id)
        if (
            evaluation is not None
            and evaluation.revision_id == revision_id
            and evaluation.status == "completed"
            and evaluation.manual_result == "accepted"
        ):
            return "evaluation_run"
        raise WorkbenchError("未找到已完成且通过人工确认的调试证据", code="RELEASE_EVIDENCE_REQUIRED")

    def _add_compatibility_files(self, files, prefix, obj, revision, release) -> None:
        payload = revision.payload
        if obj.object_type == "skill":
            metadata = payload.get("source_metadata") or payload.get("configuration") or {}
            files[f"{prefix}/SKILL.md"] = skill_markdown_with_metadata(
                str(payload.get("prompt_markdown") or ""),
                metadata if isinstance(metadata, dict) else {},
            ).encode("utf-8")
            files[f"{prefix}/runtime_contract.json"] = json.dumps(payload.get("runtime_contract") or {}, ensure_ascii=False, indent=2).encode("utf-8")
        elif obj.object_type == "expert":
            files[f"{prefix}/AGENT.md"] = str(payload.get("rules_markdown") or "").encode("utf-8")
            files[f"{prefix}/agent.yaml"] = yaml.safe_dump({
                "schema_version": 1, "id": obj.object_key, "name": obj.name, "topology": "single_expert",
                "brief": str(payload.get("brief") or ""),
                "rule_file": "AGENT.md", "skills": [item["object_key"] for item in release.dependency_locks],
                "budget": payload.get("budget") or {"max_iters": 4, "max_skill_calls": 3},
                "capabilities": payload.get("capabilities") or [],
            }, allow_unicode=True, sort_keys=False).encode("utf-8")
            files[f"{prefix}/skills.lock.json"] = json.dumps({"schema_version": 1, "skills": [
                {"skill_id": item["object_key"], "release_id": item["release_id"], "version": f"v{item['release_no']}", "content_hash": item["content_hash"]}
                for item in release.dependency_locks
            ]}, ensure_ascii=False, indent=2).encode("utf-8")
        elif obj.object_type == "expert_team":
            files[f"{prefix}/TEAM.md"] = str(payload.get("rules_markdown") or "").encode("utf-8")
            coordinator = next((item["object_key"] for item in release.dependency_locks if item["object_id"] == payload.get("coordinator_expert_id")), "")
            files[f"{prefix}/team.yaml"] = yaml.safe_dump({
                "schema_version": 1, "id": obj.object_key, "name": obj.name, "topology": "team", "rule_file": "TEAM.md",
                "brief": str(payload.get("brief") or ""),
                "coordinator_expert_id": coordinator,
                "members": payload.get("members") or [{"expert_id": item["object_key"]} for item in release.dependency_locks],
            }, allow_unicode=True, sort_keys=False).encode("utf-8")
            files[f"{prefix}/experts.lock.json"] = json.dumps({"schema_version": 1, "experts": [
                {"expert_id": item["object_key"], "release_id": item["release_id"], "version": f"v{item['release_no']}", "content_hash": item["content_hash"]}
                for item in release.dependency_locks
            ]}, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _zip(files: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for path, content in sorted(files.items()):
                info = ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, content)
        return output.getvalue()

    def _validated_package_contents(
        self,
        package_bytes: bytes,
        *,
        materialize_entries: bool = True,
        defer_script_validation: bool = False,
        enforce_python_location: bool = True,
    ) -> dict[str, Any]:
        """Validate package integrity while optionally tolerating legacy .py paths.

        ``enforce_python_location`` is deliberately independent from script
        safety review: deployment intake/activation may preserve a legacy
        layout, but only files under the supported ``scripts/`` runtime root
        are considered executable script assets.
        """
        if not package_bytes or len(package_bytes) > MAX_PACKAGE_BYTES:
            raise WorkbenchError("配置包为空或超过大小限制", code="PACKAGE_TOO_LARGE")
        files = self._read_zip(package_bytes)
        if "manifest.json" not in files:
            raise WorkbenchError("配置包缺少 manifest.json", code="INVALID_PACKAGE")
        try:
            manifest = json.loads(files["manifest.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError("manifest.json 无法解析", code="INVALID_PACKAGE") from exc
        if not isinstance(manifest, dict):
            raise WorkbenchError("manifest.json 必须是对象", code="INVALID_PACKAGE")
        if manifest.get("schema_version") not in {1, 2}:
            raise WorkbenchError("不支持的配置包 Schema", code="PACKAGE_SCHEMA_MISMATCH")
        claimed_hash = str(manifest.get("manifest_hash") or "")
        unsigned = dict(manifest)
        unsigned.pop("manifest_hash", None)
        if not claimed_hash or _hash(unsigned) != claimed_hash:
            raise WorkbenchError("配置包清单哈希校验失败", code="PACKAGE_HASH_MISMATCH")
        # The fingerprint records the exporter runtime for diagnosis and audit, but
        # it is intentionally not a cross-environment compatibility gate.  It
        # includes the release label and hashes for the whole runtime catalog, so
        # an otherwise runnable package could be rejected merely because either
        # environment has an unrelated runtime update.  Actual compatibility is
        # verified below from the package's declared schema, dependency closure,
        # file hashes, capability requirements, and script review.
        manifest_objects = manifest.get("objects", [])
        if not isinstance(manifest_objects, list) or not manifest_objects:
            raise WorkbenchError("配置包对象清单不能为空", code="INVALID_PACKAGE")
        object_release_ids = {str(item.get("release_id") or "") for item in manifest_objects if isinstance(item, dict)}
        if "" in object_release_ids or len(object_release_ids) != len(manifest_objects):
            raise WorkbenchError("配置包对象发布版本不能为空且不能重复", code="INVALID_PACKAGE")
        root = manifest.get("root")
        root_release_id = str(root.get("release_id") or "") if isinstance(root, dict) else ""
        root_item = next(
            (
                item for item in manifest_objects
                if isinstance(item, dict) and str(item.get("release_id") or "") == root_release_id
            ),
            None,
        )
        if root_item is None or not isinstance(root, dict):
            raise WorkbenchError("根发布版本不在配置包对象中", code="INVALID_PACKAGE")
        if any(
            str(root.get(field) or "") != str(root_item.get(field) or "")
            for field in ("object_type", "object_key")
        ):
            raise WorkbenchError("根对象声明与配置包不一致", code="PACKAGE_OBJECT_MISMATCH")
        object_hashes = {
            str(item.get("release_id") or ""): str(item.get("content_hash") or "")
            for item in manifest_objects
            if isinstance(item, dict)
        }
        declared_paths = {"manifest.json"}
        for item in manifest_objects:
            if not isinstance(item, dict):
                raise WorkbenchError("配置包对象清单不合法", code="INVALID_PACKAGE")
            dependency_locks = item.get("dependency_locks", [])
            if not isinstance(dependency_locks, list) or not all(isinstance(lock, dict) for lock in dependency_locks):
                raise WorkbenchError("配置包依赖锁格式不合法", code="INVALID_PACKAGE")
            for lock in dependency_locks:
                dependency_release_id = str(lock.get("release_id") or "")
                if dependency_release_id not in object_release_ids:
                    raise WorkbenchError("配置包缺少依赖版本", code="PACKAGE_DEPENDENCY_MISSING")
                if str(lock.get("content_hash") or "") != object_hashes[dependency_release_id]:
                    raise WorkbenchError("依赖锁内容哈希不匹配", code="PACKAGE_DEPENDENCY_MISMATCH")
            object_files: list[tuple[str, bytes]] = []
            declared_contents: list[tuple[str, bytes]] = []
            declared_files = item.get("files", [])
            if not isinstance(declared_files, list) or not all(isinstance(declared, dict) for declared in declared_files):
                raise WorkbenchError("配置包文件清单格式不合法", code="INVALID_PACKAGE")
            for declared in declared_files:
                path = _safe_relative_path(str(declared.get("path") or ""))
                declared_paths.add(path)
                content = files.get(path)
                if content is None or hashlib.sha256(content).hexdigest() != declared.get("sha256"):
                    reason = "文件缺失" if content is None else "文件哈希不匹配"
                    raise WorkbenchError(
                        f"配置包文件{reason}: {path}",
                        code="PACKAGE_FILE_HASH_MISMATCH",
                        details={"path": path, "reason": reason},
                    )
                if path.endswith("/object.json"):
                    object_files.append((path, content))
                declared_contents.append((path, content))
                # Python is allowed only after the object declaration has been
                # inspected below, where it is restricted to Skill/scripts and
                # submitted to the script safety review.  Shell and native
                # executables are never valid package assets.
                if PurePosixPath(path).suffix.lower() in (EXECUTABLE_SUFFIXES - {".py"}):
                    raise WorkbenchError("配置包不得携带可执行代码", code="EXECUTABLE_CONTENT_FORBIDDEN")
            if len(object_files) != 1:
                raise WorkbenchError("每个对象必须且只能包含一个 object.json", code="INVALID_PACKAGE")
            try:
                declaration = json.loads(object_files[0][1])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkbenchError("对象声明无法解析", code="INVALID_PACKAGE") from exc
            if not isinstance(declaration, dict):
                raise WorkbenchError("对象声明必须是对象", code="INVALID_PACKAGE")
            for field in ("object_id", "object_type", "object_key", "release_id", "release_no", "content_hash", "dependency_locks"):
                if declaration.get(field) != item.get(field):
                    raise WorkbenchError("对象声明与 manifest 不一致", code="PACKAGE_OBJECT_MISMATCH")
            if declaration.get("object_type") not in VALID_OBJECT_TYPES:
                raise WorkbenchError("对象声明类型不合法", code="INVALID_PACKAGE")
            if not isinstance(declaration.get("payload"), dict):
                raise WorkbenchError("对象声明 payload 必须是对象", code="INVALID_PACKAGE")
            prefix = object_files[0][0].rsplit("/object.json", 1)[0] + "/"
            script_sources: list[tuple[str, str, bytes]] = []
            for path, content in declared_contents:
                relative_path = path[len(prefix):] if path.startswith(prefix) else path
                if PurePosixPath(relative_path).suffix.lower() == ".py":
                    if enforce_python_location and not defer_script_validation and (
                        declaration.get("object_type") != "skill" or not relative_path.startswith("scripts/")
                    ):
                        raise WorkbenchError("Python 脚本只能位于 Skill 的 scripts/ 目录", code="EXECUTABLE_CONTENT_FORBIDDEN")
                    if relative_path.startswith("scripts/"):
                        script_sources.append((relative_path, "text/x-python", content))
                elif relative_path == "scripts/requirements.txt":
                    script_sources.append((relative_path, "text/plain", content))
            script_errors = [] if defer_script_validation else self._script_validation_errors(script_sources)
            if script_errors:
                raise WorkbenchError(
                    "Python 脚本安全校验未通过",
                    code="SCRIPT_REVIEW_FAILED",
                    details={"errors": script_errors},
                )
            # Skill scripts travel inside the package. Verify their capability
            # declarations against those bundled scripts, not against the
            # receiver's whole runtime catalog: unrelated local changes must
            # not make a complete reviewed package non-portable. ``script/``
            # remains accepted for packages created by the older exporter.
            object_key = str(declaration.get("object_key") or "")
            bundled_capabilities = {
                f"{object_key}:{relative_path}"
                for relative_path, _media_type, _content in script_sources
            }
            bundled_capabilities.update(
                capability.replace(":scripts/", ":script/", 1)
                for capability in list(bundled_capabilities)
            )
            unknown = sorted(
                set((declaration.get("payload") or {}).get("capability_ids") or []) - bundled_capabilities
            )
            if unknown:
                raise WorkbenchError(
                    f"配置包声明的能力未随包提供: {', '.join(unknown)}",
                    code="UNKNOWN_CAPABILITY",
                )
        if set(files) != declared_paths:
            raise WorkbenchError("配置包包含未在 manifest 声明的文件", code="UNDECLARED_PACKAGE_FILE")
        return {
            "manifest": manifest,
            "files": files,
            "package_hash": hashlib.sha256(package_bytes).hexdigest(),
            "object_release_ids": object_release_ids,
            "entries": [
                self._package_object_entry(item, files, materialize=materialize_entries)
                for item in manifest_objects
            ],
        }

    def _package_object_entry(self, item: dict[str, Any], files: dict[str, bytes], *, materialize: bool = True) -> dict[str, Any]:
        object_file = next(
            (
                str(declared.get("path") or "")
                for declared in item.get("files", [])
                if str(declared.get("path") or "").endswith("/object.json")
            ),
            "",
        )
        if not object_file or object_file not in files:
            raise WorkbenchError("配置包对象声明缺失", code="INVALID_PACKAGE")
        entry = json.loads(files[object_file])
        prefix = object_file.rsplit("/object.json", 1)[0] + "/"
        entry["files"] = [
            {
                "relative_path": path[len(prefix):],
                "media_type": "text/x-python" if path.endswith(".py") else "application/octet-stream",
                "size_bytes": len(content),
                "content_hash": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            for path, content in sorted(files.items())
            if path.startswith(prefix)
            and path != object_file
            and PurePosixPath(path).name not in {"SKILL.md", "AGENT.md", "TEAM.md", "runtime_contract.json", "agent.yaml", "team.yaml", "skills.lock.json", "experts.lock.json"}
        ]
        return materialize_entry_files(entry) if materialize else entry

    @staticmethod
    def _entry_content_hash(
        payload: dict[str, Any],
        locks: list[dict[str, Any]],
        files: list[tuple[str, str, bytes]],
    ) -> str:
        return _hash({
            "payload": payload,
            "dependency_locks": locks,
            "assets": [
                {
                    "relative_path": relative_path,
                    "media_type": media_type,
                    "size_bytes": len(content),
                    "content_hash": hashlib.sha256(content).hexdigest(),
                }
                for relative_path, media_type, content in files
            ],
        })

    def _create_imported_release(
        self,
        db,
        *,
        obj: WorkbenchObjectRow,
        revision: WorkbenchRevisionRow,
        locks: list[dict[str, Any]],
        package_hash: str,
        source_release_id: str,
        source_release_no: int,
        actor_id: str,
    ) -> WorkbenchReleaseRow:
        next_release_no = (
            db.scalar(select(func.max(WorkbenchReleaseRow.release_no)).where(WorkbenchReleaseRow.object_id == obj.object_id)) or 0
        ) + 1
        release = WorkbenchReleaseRow(
            release_id=_id("rel"),
            object_id=obj.object_id,
            revision_id=revision.revision_id,
            release_no=next_release_no,
            dependency_locks=locks,
            content_hash=revision.content_hash,
            verification={
                "evidence_type": "package_import",
                "manual_confirmation": True,
                "source_package_hash": package_hash,
                "source_release_id": source_release_id,
                "source_release_no": source_release_no,
                "imported_by": actor_id,
                "imported_at": _iso(utc_now()),
            },
            published_by=actor_id,
        )
        db.add(release)
        return release

    def _import_workbench_entry(
        self,
        db,
        entry: dict[str, Any],
        *,
        local_dependency_locks: list[dict[str, Any]],
        package_hash: str,
        actor_id: str,
        select_new_as_current: bool = True,
        defer_script_validation: bool = False,
    ) -> dict[str, Any]:
        self._validate_unique_dependency_objects(local_dependency_locks)
        object_type = str(entry.get("object_type") or "")
        object_key = str(entry.get("object_key") or "")
        payload = dict(entry.get("payload") or {})
        legacy_missing_brief = object_type in {"expert", "expert_team"} and "brief" not in payload
        if object_type == "expert_team":
            source_locks = entry.get("dependency_locks") if isinstance(entry.get("dependency_locks"), list) else []
            source_to_local_object_ids = {
                str(source.get("object_id") or ""): str(local.get("object_id") or "")
                for source, local in zip(source_locks, local_dependency_locks)
                if str(source.get("object_id") or "") and str(local.get("object_id") or "")
            }
            coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
            if coordinator_object_id in source_to_local_object_ids:
                payload["coordinator_expert_id"] = source_to_local_object_ids[coordinator_object_id]
        if object_type == "skill":
            payload["_workbench_managed_files"] = True
        obj = db.scalar(
            select(WorkbenchObjectRow).where(
                WorkbenchObjectRow.object_type == object_type,
                WorkbenchObjectRow.object_key == object_key,
            )
        )
        created_object = 0
        reused_object = 0
        unarchived_object = 0
        if obj is None:
            obj = WorkbenchObjectRow(
                object_id=_id("obj"),
                object_type=object_type,
                object_key=object_key,
                name=str(entry.get("name") or object_key),
                description=str(entry.get("description") or ""),
                created_by=actor_id,
            )
            db.add(obj)
            db.flush()
            created_object = 1
        else:
            reused_object = 1
            # A recursive package is an explicit request to make its complete
            # dependency closure editable again. Reusing an archived object
            # without unarchiving it leaves the team locks pointing at objects
            # that the default workbench library hides.
            if obj.archived:
                obj.archived = False
                obj.updated_at = utc_now()
                unarchived_object = 1
                self._audit(
                    db,
                    actor_id,
                    "object.unarchived",
                    object_type,
                    obj.object_id,
                    {"reason": "recursive_package_import", "object_key": object_key},
                )
        decoded_assets = self._decode_assets(
            entry.get("files") if isinstance(entry.get("files"), list) else [],
            object_type=object_type,
            allow_misplaced_python=defer_script_validation,
        )
        imported_capabilities: set[str] | None = None
        if object_type == "skill":
            imported_capabilities = {
                f"{object_key}:{relative_path}"
                for relative_path, _media_type, _content in decoded_assets
                if PurePosixPath(relative_path).suffix.lower() == ".py"
                and relative_path.startswith("scripts/")
            }
            imported_capabilities.update(
                capability.replace(":scripts/", ":script/", 1)
                for capability in list(imported_capabilities)
            )
        validation = self._validate_payload(
            object_type,
            payload,
            local_dependency_locks,
            known_capabilities=imported_capabilities,
            allow_legacy_brief=legacy_missing_brief,
        )
        if object_type == "skill":
            questionnaire_errors = _questionnaire_asset_errors(payload, decoded_assets)
            script_errors = [] if defer_script_validation else self._script_validation_errors(decoded_assets)
            if questionnaire_errors or script_errors:
                validation = {
                    **validation,
                    "valid": False,
                    "errors": [*validation.get("errors", []), *questionnaire_errors, *script_errors],
                }
            if defer_script_validation and any(
                PurePosixPath(path).suffix.lower() == ".py" and not path.startswith("scripts/")
                for path, _media_type, _content in decoded_assets
            ):
                validation = {
                    **validation,
                    "warnings": [
                        *list(validation.get("warnings") or []),
                        "检测到非 scripts/ 目录中的 Python 文件，导入时保留原路径；运行时不会自动执行该文件",
                    ],
                }
        if not validation.get("valid", False) and not str(entry.get("release_id") or "").startswith("revision:"):
            raise WorkbenchError(
                "导入包中的对象校验未通过",
                code="REVISION_VALIDATION_FAILED",
                details={
                    "object_type": object_type,
                    "object_key": object_key,
                    "errors": list(validation.get("errors") or []),
                    "warnings": list(validation.get("warnings") or []),
                },
            )
        content_hash = self._entry_content_hash(payload, local_dependency_locks, decoded_assets)
        revision = db.scalar(
            select(WorkbenchRevisionRow)
            .where(
                WorkbenchRevisionRow.object_id == obj.object_id,
                WorkbenchRevisionRow.content_hash == content_hash,
            )
            .order_by(WorkbenchRevisionRow.revision_no.desc())
            .limit(1)
        )
        created_revision = 0
        reused_revision = 0
        if revision is None:
            latest = db.scalar(
                select(WorkbenchRevisionRow)
                .where(WorkbenchRevisionRow.object_id == obj.object_id)
                .order_by(WorkbenchRevisionRow.revision_no.desc())
                .limit(1)
            )
            revision = WorkbenchRevisionRow(
                revision_id=_id("rev"),
                object_id=obj.object_id,
                revision_no=(latest.revision_no if latest else 0) + 1,
                base_revision_id=latest.revision_id if latest else None,
                change_summary=str(entry.get("change_summary") or "").strip(),
                payload=payload,
                dependency_locks=local_dependency_locks,
                validation=validation,
                content_hash=content_hash,
                created_by=actor_id,
            )
            db.add(revision)
            db.flush()
            for relative_path, media_type, content in decoded_assets:
                db.add(WorkbenchAssetRow(
                    asset_id=_id("asset"),
                    revision_id=revision.revision_id,
                    relative_path=relative_path,
                    media_type=media_type,
                    size_bytes=len(content),
                    content_hash=hashlib.sha256(content).hexdigest(),
                    content=content,
                ))
            obj.updated_at = utc_now()
            created_revision = 1
            self._audit(
                db,
                actor_id,
                "revision.imported",
                object_type,
                revision.revision_id,
                {"object_key": object_key, "source_release_id": entry.get("release_id")},
            )
        else:
            reused_revision = 1
        release = db.scalar(select(WorkbenchReleaseRow).where(WorkbenchReleaseRow.revision_id == revision.revision_id))
        created_release = 0
        reused_release = 0
        entry_status = "reused_release"
        runtime_entry: dict[str, Any] | None = None
        if release is None:
            release = self._create_imported_release(
                db,
                obj=obj,
                revision=revision,
                locks=local_dependency_locks,
                package_hash=package_hash,
                source_release_id=str(entry.get("release_id") or ""),
                source_release_no=int(entry.get("release_no") or 0),
                actor_id=actor_id,
            )
            created_release = 1
            entry_status = "created" if created_object else "created_revision_and_release" if created_revision else "reused_revision_and_created_release"
            self._audit(
                db,
                actor_id,
                "release.imported",
                object_type,
                release.release_id,
                {"object_key": object_key, "source_release_id": entry.get("release_id")},
            )
            runtime_entry = {
                "object_id": obj.object_id,
                "object_type": object_type,
                "object_key": obj.object_key,
                "name": obj.name,
                "release_id": release.release_id,
                "release_no": release.release_no,
                "payload": revision.payload,
                "dependency_locks": release.dependency_locks,
                "content_hash": revision.content_hash,
                "files": self._revision_files(db, revision.revision_id),
            }
        else:
            reused_release = 1
        selected_as_current = select_new_as_current and obj.current_release_id is None
        if selected_as_current:
            obj.current_release_id = release.release_id
            runtime_entry = self._runtime_entry_for_release(db, release.release_id)
        self._audit(
            db,
            actor_id,
            "object.imported",
            object_type,
            obj.object_id,
            {
                "object_key": object_key,
                "source_release_id": entry.get("release_id"),
                "status": entry_status,
                "selected_as_current": selected_as_current,
            },
        )
        return {
            "created_objects": created_object,
            "created_revisions": created_revision,
            "created_releases": created_release,
            "reused_objects": reused_object,
            "reused_revisions": reused_revision,
            "reused_releases": reused_release,
            "unarchived_objects": unarchived_object,
            "runtime_entry": runtime_entry,
            "release": {
                "object_id": obj.object_id,
                "object_type": object_type,
                "object_key": obj.object_key,
                "release_id": release.release_id,
                "release_no": release.release_no,
                "content_hash": release.content_hash,
                "revision_id": revision.revision_id,
            },
            "entry": {
                "object_id": obj.object_id,
                "object_type": object_type,
                "object_key": obj.object_key,
                "name": obj.name,
                "source_release_id": entry.get("release_id"),
                "source_release_no": entry.get("release_no"),
                "local_revision_id": revision.revision_id,
                "local_revision_no": revision.revision_no,
                "local_release_id": release.release_id,
                "local_release_no": release.release_no,
                "status": entry_status,
                "unarchived": bool(unarchived_object),
            },
        }

    @staticmethod
    def _read_zip(package_bytes: bytes) -> dict[str, bytes]:
        try:
            with ZipFile(io.BytesIO(package_bytes)) as archive:
                raw_files: dict[str, bytes] = {}
                total = 0
                for info in archive.infolist():
                    path = WorkbenchService._repair_zip_filename(info.filename)
                    path = _safe_relative_path(path)
                    # macOS adds Finder metadata when a directory is zipped
                    # from Finder.  It is not part of the signed package and
                    # must not make an otherwise valid archive fail the
                    # manifest/declared-files check.
                    if path == "__MACOSX" or path.startswith("__MACOSX/") or PurePosixPath(path).name == ".DS_Store":
                        continue
                    if path in raw_files:
                        raise WorkbenchError("配置包包含重复文件", code="DUPLICATE_PACKAGE_FILE")
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > MAX_PACKAGE_BYTES:
                        raise WorkbenchError("配置包解压后超过大小限制", code="PACKAGE_TOO_LARGE")
                    raw_files[path] = archive.read(info)

                # Finder and common ZIP tools preserve the selected folder as
                # a single top-level directory.  Exported manifests, however,
                # intentionally use package-relative paths.  Normalize exactly
                # one such wrapper directory while rejecting mixed layouts.
                files = raw_files
                if "manifest.json" not in files:
                    candidates = [
                        path for path in files
                        if path.endswith("/manifest.json")
                        and path.count("/") >= 1
                    ]
                    if len(candidates) == 1:
                        manifest_path = candidates[0]
                        prefix = manifest_path[: -len("manifest.json")]
                        if prefix and all(path.startswith(prefix) for path in files):
                            files = {
                                path[len(prefix):]: content
                                for path, content in files.items()
                            }
                return files
        except BadZipFile as exc:
            raise WorkbenchError("不是合法的 ZIP 配置包", code="INVALID_PACKAGE") from exc

    @staticmethod
    def _repair_zip_filename(filename: str) -> str:
        """Repair the reversible UTF-8-as-CP437 filename corruption.

        Some macOS ZIP tools write UTF-8 bytes without the UTF-8 flag. Python
        then exposes names such as ``τ║ó...``. If the round-trip is lossless,
        recover the intended UTF-8 name; valid Unicode names remain unchanged.
        """
        try:
            repaired = filename.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return filename
        return repaired if repaired != filename else filename

    @staticmethod
    def _json_file(path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _diff(path: str, left: Any, right: Any, changes: list[dict[str, Any]]) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                WorkbenchService._diff(f"{path}.{key}", left.get(key), right.get(key), changes)
            return
        if left != right:
            changes.append({"path": path, "before": left, "after": right})

    @staticmethod
    def _audit(db, actor_id: str, action: str, target_type: str, target_id: str, payload: dict[str, Any]) -> None:
        db.add(WorkbenchAuditRow(
            event_id=_id("audit"), actor_id=actor_id or "anonymous", action=action,
            target_type=target_type, target_id=target_id, payload=payload,
        ))

    @staticmethod
    def _require_object(db, object_id: str) -> WorkbenchObjectRow:
        row = db.get(WorkbenchObjectRow, object_id)
        if row is None:
            raise WorkbenchError("业务对象不存在", code="OBJECT_NOT_FOUND")
        return row

    @staticmethod
    def _require_revision(db, revision_id: str) -> WorkbenchRevisionRow:
        row = db.get(WorkbenchRevisionRow, revision_id)
        if row is None:
            raise WorkbenchError("修订不存在", code="REVISION_NOT_FOUND")
        return row

    @staticmethod
    def _require_release(db, release_id: str) -> WorkbenchReleaseRow:
        row = db.get(WorkbenchReleaseRow, release_id)
        if row is None:
            raise WorkbenchError("发布版本不存在", code="RELEASE_NOT_FOUND")
        return row

    @staticmethod
    def _require_revision_release(db, revision: WorkbenchRevisionRow, release_id: str) -> WorkbenchReleaseRow:
        release = WorkbenchService._require_release(db, release_id)
        if release.object_id != revision.object_id or release.revision_id != revision.revision_id:
            raise WorkbenchError("发布版本不属于当前修订", code="RELEASE_REVISION_MISMATCH")
        return release

    @staticmethod
    def _object_dict(row, latest_revision_no: int, latest_release_no: int) -> dict[str, Any]:
        return {
            "object_id": row.object_id, "object_type": row.object_type, "object_key": row.object_key,
            "name": row.name, "description": row.description, "archived": row.archived,
            "latest_revision_no": latest_revision_no, "latest_release_no": latest_release_no,
            "current_release_id": row.current_release_id,
            "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def _actor_display_names(db, actor_ids: Iterable[str]) -> dict[str, str]:
        ids = {str(actor_id or "").strip() for actor_id in actor_ids if str(actor_id or "").strip()}
        if not ids:
            return {}
        return {
            row.actor_id: row.display_name
            for row in db.scalars(select(WorkbenchActorRow).where(WorkbenchActorRow.actor_id.in_(ids)))
            if str(row.display_name or "").strip()
        }

    @staticmethod
    def _revision_dict(row, actor_names: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "revision_id": row.revision_id, "object_id": row.object_id, "revision_no": row.revision_no,
            "base_revision_id": row.base_revision_id, "change_summary": str(getattr(row, "change_summary", "") or ""), "payload": row.payload,
            "dependency_locks": row.dependency_locks, "validation": row.validation,
            "content_hash": row.content_hash, "created_by": row.created_by,
            "created_by_display_name": (actor_names or {}).get(row.created_by, row.created_by),
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def _soul_revision_dict(row) -> dict[str, Any]:
        return {
            "soul_revision_id": row.soul_revision_id,
            "revision_no": row.revision_no,
            "content": row.content,
            "content_hash": row.content_hash,
            "enabled": True,
            "created_by": row.created_by,
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def _require_soul_revision(db, soul_revision_id: str) -> WorkbenchSoulRevisionRow:
        row = db.get(WorkbenchSoulRevisionRow, soul_revision_id)
        if row is None:
            raise WorkbenchError("Soul 版本不存在", code="SOUL_REVISION_NOT_FOUND")
        return row

    @staticmethod
    def _release_dict(row, obj, actor_names: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "is_current": obj.current_release_id == row.release_id,
            "release_id": row.release_id, "object_id": row.object_id, "object_type": obj.object_type,
            "object_key": obj.object_key, "name": obj.name, "revision_id": row.revision_id,
            "release_no": row.release_no, "version": f"v{row.release_no}", "dependency_locks": row.dependency_locks,
            "content_hash": row.content_hash, "verification": row.verification, "archived": row.archived,
            "published_by": row.published_by,
            "published_by_display_name": (actor_names or {}).get(row.published_by, row.published_by),
            "published_at": _iso(row.published_at),
        }

    @staticmethod
    def _debug_dict(row) -> dict[str, Any]:
        return {
            "debug_session_id": row.debug_session_id, "revision_id": row.revision_id,
            "baseline_release_id": row.baseline_release_id, "snapshot": row.snapshot,
            "transcript": row.transcript, "trace": row.trace, "status": row.status,
            "conclusion": row.conclusion, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "completed_at": _iso(row.completed_at),
        }

    @staticmethod
    def _suite_dict(row) -> dict[str, Any]:
        return {
            "suite_id": row.suite_id, "object_id": row.object_id, "name": row.name,
            "cases": row.cases, "archived": row.archived, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def _evaluation_run_dict(row) -> dict[str, Any]:
        return {
            "run_id": row.run_id, "suite_id": row.suite_id, "revision_id": row.revision_id,
            "baseline_release_id": row.baseline_release_id, "snapshot": row.snapshot,
            "results": row.results, "status": row.status, "manual_result": row.manual_result,
            "manual_notes": row.manual_notes, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "completed_at": _iso(row.completed_at),
        }

    @staticmethod
    def _deployment_dict(row) -> dict[str, Any]:
        root = row.manifest.get("root") if isinstance(row.manifest, dict) else {}
        return {
            "deployment_id": row.deployment_id, "environment": row.environment,
            "root_release_id": row.root_release_id, "package_hash": row.package_hash,
            "manifest": row.manifest, "status": row.status,
            "expert_team_id": str((root or {}).get("object_key") or "") if (root or {}).get("object_type") == "expert_team" else None,
            "expert_team_name": str((root or {}).get("name") or "") if (root or {}).get("object_type") == "expert_team" else None,
            "previous_deployment_id": row.previous_deployment_id, "imported_by": row.imported_by,
            "imported_at": _iso(row.imported_at), "activated_by": row.activated_by,
            "activated_at": _iso(row.activated_at),
        }

    @staticmethod
    def _deployment_root_team_id(deployment: WorkbenchDeploymentRow, *, required: bool = True) -> str | None:
        root = deployment.manifest.get("root") if isinstance(deployment.manifest, dict) else {}
        team_id = str((root or {}).get("object_key") or "")
        if (root or {}).get("object_type") == "expert_team" and team_id:
            return team_id
        if required:
            raise WorkbenchError("生产部署根对象必须是专家团", code="DEPLOYMENT_ROOT_TEAM_REQUIRED")
        return None
