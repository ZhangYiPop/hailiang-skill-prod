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
from hailiang_skills.workbench.runtime_overlay import configured_skill_bundle, materialize_entry_files
from hailiang_skills.runtime_bridge.script_review import review_scripts


VALID_OBJECT_TYPES = {"skill", "expert", "expert_team"}
EXECUTABLE_SUFFIXES = {".pyc", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".exe", ".dll", ".so", ".dylib"}
MAX_ASSET_BYTES = int(os.getenv("HAILIANG_WORKBENCH_MAX_ASSET_BYTES", str(20 * 1024 * 1024)))
MAX_PACKAGE_BYTES = int(os.getenv("HAILIANG_WORKBENCH_MAX_PACKAGE_BYTES", str(100 * 1024 * 1024)))


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
            deleted_keys = db.scalars(
                select(WorkbenchAuditRow).where(WorkbenchAuditRow.action == "object.deleted")
            )
            if any(
                str((event.payload or {}).get("object_type") or "") == object_type
                and str((event.payload or {}).get("object_key") or "") == key
                for event in deleted_keys
            ):
                raise WorkbenchConflict(
                    "该对象 ID 曾被永久删除，不能用同一 ID 恢复；请使用新的对象 ID",
                    code="DELETED_OBJECT_KEY_RESERVED",
                )
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
        raw_key = str(metadata.get("skill_id") or metadata.get("id") or PurePosixPath(source_path).parent.name or "imported_skill")
        object_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in raw_key).strip("_") or "imported_skill"
        name = str(metadata.get("name") or object_key)
        runtime_contract = self._standard_runtime_contract(files)
        assets = []
        blocked: list[str] = []
        for path, content in files.items():
            if path == source_path or not (path.startswith("references/") or path.startswith("scripts/") or path.startswith("assets/")):
                continue
            if path.startswith("scripts/") and PurePosixPath(path).suffix.lower() not in {".py", ".txt"}:
                blocked.append(f"不支持的脚本文件：{path}")
                continue
            target_path = f"references/{path}" if path.startswith("assets/") else path
            media_type = "text/x-python" if path.endswith(".py") else "application/json" if path.endswith(".json") else "text/markdown"
            assets.append({"relative_path": target_path, "media_type": media_type, "content_base64": base64.b64encode(content).decode("ascii")})
        decoded = self._decode_assets(assets, object_type=object_type) if not blocked else []
        script_errors = self._script_validation_errors(decoded) if object_type == "skill" else []
        questionnaire = runtime_contract.get("questionnaire") if isinstance(runtime_contract.get("questionnaire"), dict) else {}
        if not questionnaire:
            questionnaire = self._questions_asset_to_questionnaire(files)
            if questionnaire:
                runtime_contract = {**runtime_contract, "questionnaire": questionnaire}
        payload = (
            {"prompt_markdown": source, "runtime_contract": runtime_contract, "capability_ids": []}
            if object_type == "skill"
            else {"rules_markdown": body or source, "budget": {"max_iters": 4, "max_skill_calls": 3}}
        )
        ai_hint = self._standard_conversion_ai_hint(source, object_type)
        if ai_hint.get("name"):
            name = str(ai_hint["name"])[:256]
        if ai_hint.get("description"):
            metadata["description"] = str(ai_hint["description"])[:2000]
        warnings = [*blocked, *script_errors]
        warnings.extend(str(item) for item in ai_hint.get("review_items", []) if str(item))
        if questionnaire and not isinstance(questionnaire.get("fields"), list):
            warnings.append("问卷定义无法映射为平台表单字段，请人工补充。")
        return {
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
                result.append(self._object_dict(row, latest_revision, latest_release))
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
            return {
                **self._object_dict(row, revisions[0].revision_no if revisions else 0, releases[0].release_no if releases else 0),
                "revisions": [self._revision_dict(item) for item in revisions],
                "releases": [self._release_dict(item, row) for item in releases],
            }

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
                "permanent": True,
            }
            db.delete(obj)
            self._audit(db, actor_id, "object.deleted", obj.object_type, object_id, summary)
            db.commit()
            self._uninstall_runtime_object(obj.object_type, obj.object_key)
            return summary

    def save_revision(
        self,
        object_id: str,
        *,
        base_revision_id: str | None,
        payload: dict[str, Any],
        dependency_locks: list[dict[str, Any]] | None,
        assets: list[dict[str, Any]] | None,
        actor_id: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkbenchError("payload 必须是对象")
        with self.session_factory() as db:
            obj = self._require_object(db, object_id)
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
            decoded_assets = self._decode_assets(assets or [], object_type=obj.object_type)
            validation = self._validate_payload(obj.object_type, payload, canonical_locks)
            if obj.object_type == "skill":
                script_errors = self._script_validation_errors(decoded_assets)
                if script_errors:
                    validation = {
                        **validation,
                        "valid": False,
                        "errors": [*validation.get("errors", []), *script_errors],
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
            self._audit(db, actor_id, "revision.saved", obj.object_type, revision.revision_id, {"revision_no": revision_no})
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise WorkbenchConflict(
                    "配置已被其他修订更新，请刷新后重试",
                    code="REVISION_BASE_CONFLICT",
                    details={"submitted_base_revision_id": base_revision_id},
                ) from exc
            return self._revision_dict(revision)

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
            # Candidate messages use exactly the same interaction lifecycle as
            # formal chat. A new free-text turn makes a previous form/card
            # stale, while a structured form or handoff action consumes the
            # active interaction instead.
            if not form_submission and not team_handoff_selection and not expert_selection:
                expire_active_interactions(context.messages)
            event_count_before = len(context.event_trace)
            turn_started = datetime.now().timestamp()
            candidate_stream_generation: str | None = None
            candidate_stream_sequence = 0
            stream_content = ""
            stream_handoff: dict[str, Any] | None = None

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

                context.session_meta["reply_delta_callback"] = reply_delta_callback
                context.session_meta["reasoning_delta_callback"] = reasoning_delta_callback
                context.session_meta["status_callback"] = status_callback
                context.session_meta["team_handoff_callback"] = team_handoff_callback
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
                    "message_kind": "team_handoff_selection" if team_handoff_selection else "manual_expert_selection" if expert_selection else "fact_form_submission" if form_submission else "user_message",
                    "root": self._candidate_root_debug(snapshot),
                },
            )
            try:
                assistant_message = self._execute_snapshot_message(snapshot, message, context)
            except Exception:
                if candidate_stream_generation:
                    with self._active_candidate_streams_lock:
                        self._active_candidate_streams.pop(debug_session_id, None)
                raise
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
                    "stream_cancel_check",
                ):
                    context.session_meta.pop(key, None)
                if context.session_meta.get("active_stream_generation") == candidate_stream_generation:
                    context.session_meta.pop("active_stream_generation", None)
                if context.session_meta.get("cancelled_stream_generation") == candidate_stream_generation:
                    context.session_meta.pop("cancelled_stream_generation", None)
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
        handoff["status"] = "selected"
        handoff["selected_target_expert_id"] = target_expert_id
        update_interaction(source, "team_handoff", status=SELECTED, selected_target_skill_id=target_expert_id)
        metadata = source.setdefault("metadata", {})
        if isinstance(metadata, dict) and isinstance(metadata.get("team_handoff"), dict):
            metadata["team_handoff"].update({"status": "selected", "selected_target_expert_id": target_expert_id})
        from_expert_id = str(context.session_meta.get("active_expert_id") or "")
        context.session_meta["active_expert_id"] = target_expert_id
        context.session_meta["expert_id"] = target_expert_id
        context.session_meta["expert_selection_source"] = "candidate_handoff_card"
        context.session_meta["_candidate_branch_version"] = int(
            context.session_meta.get("_candidate_branch_version") or 1
        ) + 1
        context.session_meta.pop("pending_team_handoff", None)
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
        context.session_meta["team_handoff_visible_user_message"] = f"@{mention_name}"
        # The Runtime writes this as a display/audit event.  It is deliberately
        # excluded from later model history; the target expert receives the
        # original question and structured handoff context below instead.
        context.session_meta["team_handoff_visible_user_message_type"] = "team_handoff_confirmation"
        context.session_meta["team_handoff_visible_user_message_metadata"] = {
            "source_message_id": str(selection.get("source_message_id") or ""),
            "target_expert_id": target_expert_id,
            "expert_team_id": team_id,
            "source": "team_handoff",
        }
        # Match the formal handoff path: the authoritative, server-validated
        # selection is passed structurally to the Expert Runtime.  Do not put
        # internal object IDs into a synthetic user message; it both diverges
        # from formal behaviour and can trigger an irrelevant local lexicon
        # match during degraded moderation.
        context.session_meta["team_member_switch"] = {
            "source": "team_handoff",
            "team_id": team_id,
            "from_expert_id": from_expert_id,
            "target_expert_id": target_expert_id,
            "mention_name": mention_name,
            "visible_user_message": f"@{mention_name}",
            "source_user_message": source_user_message,
            "coordinator_reason": str(handoff.get("reason") or ""),
            "source_message_id": str(selection.get("source_message_id") or ""),
        }
        return source_user_message or "请根据当前上下文继续协助用户。"

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
        for message in context.messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if any(
                key.startswith("fact_form:") and state.get("status") == ACTIVE
                for key, state in ensure_message_interactions(message).items()
                if isinstance(state, dict)
            ):
                raise WorkbenchConflict("请先完成或放弃当前表单，再切换专家", code="REVISION_TEST_EXPERT_BLOCKED_BY_FORM")
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
        expire_active_interactions(context.messages)
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
        registry = getattr(self.orchestrator, "expert_registry", None)
        definition = registry.get(expert_id) if expert_id and registry is not None else None
        team_registry = getattr(self.orchestrator, "expert_team_registry", None)
        team = team_registry.get(team_id) if team_id and team_registry is not None else None
        mode = "team" if team is not None else "single" if definition is not None else "none"
        member = team.member_for_expert(expert_id) if team is not None else None
        expert = {
            "mode": mode,
            "team": {
                "team_id": team.team_id,
                "name": team.name,
                "coordinator_expert_id": team.coordinator_expert_id,
            } if team is not None else {},
            "active": {
                "expert_id": expert_id,
                "name": str(getattr(definition, "name", "") or expert_id),
                "mention_name": str(getattr(member, "mention_name", "") or ""),
                "is_coordinator": bool(team is not None and expert_id == team.coordinator_expert_id),
            } if expert_id else {},
            "activation": {
                "source": "candidate_snapshot",
                "is_default": bool(team is not None and expert_id == team.coordinator_expert_id),
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
            if coordinator:
                context.session_meta["expert_id"] = coordinator
                context.session_meta["active_expert_id"] = coordinator
                context.skill_states.setdefault("agent_runtime", {})["expert_id"] = coordinator

    def _execute_snapshot_message(self, snapshot: dict[str, Any], message: str, context: SessionContext) -> str:
        root_entry = self._snapshot_root_entry(snapshot)
        if root_entry.get("object_type") == "skill":
            return self._execute_candidate_skill(root_entry, message, context)
        allowed = self._candidate_allowed_skill_ids(snapshot, root_entry, context)
        active_skill = str(context.interaction_state.get("active_skill") or "")
        # Once an Agent has entered an authorized Skill, continue that Skill
        # directly. Re-running the Agent for every answer resets its route and
        # turns a questionnaire answer such as "A" back into a fresh request.
        # An explicit task/Skill change is the exception: send the turn back
        # to the Expert so it can choose another *locked* Skill.  The old
        # shortcut made an instruction such as "用另一个技能来解答" invisible
        # to the Expert and incorrectly forced the current Skill to answer.
        redispatch = active_skill in allowed and self._candidate_requests_redispatch(message)
        if active_skill in allowed and not redispatch:
            semantic_target = self._candidate_semantic_redispatch(
                message,
                context,
                current_skill_id=active_skill,
                allowed_skill_ids=allowed,
            )
            if semantic_target:
                target_entry = next(
                    (item for item in snapshot.get("entries", []) if isinstance(item, dict) and item.get("object_type") == "skill" and item.get("object_key") == semantic_target),
                    None,
                )
                if target_entry is None:
                    raise WorkbenchError("候选快照缺少 Agent 选择的 Skill", code="CANDIDATE_SKILL_SNAPSHOT_MISSING")
                context.interaction_state["active_skill"] = semantic_target
                self._record_candidate_turn_event(
                    context,
                    "candidate_skill_semantic_redispatch",
                    {"from_skill_id": active_skill, "to_skill_id": semantic_target},
                )
                return self._execute_candidate_skill(target_entry, message, context)
        if active_skill in allowed and not redispatch:
            target_entry = next(
                (item for item in snapshot.get("entries", []) if isinstance(item, dict) and item.get("object_type") == "skill" and item.get("object_key") == active_skill),
                None,
            )
            if target_entry is None:
                raise WorkbenchError("候选快照缺少当前 Skill", code="CANDIDATE_SKILL_SNAPSHOT_MISSING")
            return self._execute_candidate_skill(target_entry, message, context)
        if redispatch:
            self._record_candidate_turn_event(
                context,
                "candidate_skill_redispatch_requested",
                {"from_skill_id": active_skill, "reason": "user_explicit_skill_or_task_change"},
            )
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
    def _candidate_requests_redispatch(message: str) -> bool:
        """Conservative, language-facing exit signal for a candidate Skill.

        A short answer such as ``A`` must remain in a questionnaire.  We only
        bypass continuation when the tester has clearly asked to change the
        current task/Skill.  The Expert still makes the actual selection and
        can only choose its immutable dependency locks.
        """
        normalized = "".join(str(message or "").lower().split())
        if not normalized:
            return False
        markers = (
            "换个skill", "换一个skill", "切换skill", "换技能", "换一个技能",
            "切换技能", "另一个技能", "其他技能", "改用", "换个能力",
            "退出当前", "停止当前", "不做测试了", "先不做", "换个问题",
        )
        return any(marker in normalized for marker in markers)

    def _candidate_semantic_redispatch(
        self,
        message: str,
        context: SessionContext,
        *,
        current_skill_id: str,
        allowed_skill_ids: set[str],
    ) -> str | None:
        """Return a different locked Skill only after the Expert chose it.

        Short option-like messages are questionnaire continuations and never
        incur a routing probe.  The production AgentScope Expert performs the
        semantic judgment; unavailable/degraded runtimes safely retain the
        current candidate Skill instead of guessing from keywords.
        """
        if len(str(message or "").strip()) < 8:
            return None
        expert_runtime = getattr(self.orchestrator, "expert_runtime", None)
        chooser = getattr(expert_runtime, "select_candidate_skill_switch", None)
        if not callable(chooser):
            return None
        selected = str(chooser(message, context, current_skill_id=current_skill_id) or "")
        if not selected or selected == current_skill_id:
            return None
        if selected not in allowed_skill_ids:
            self._record_candidate_turn_event(
                context,
                "candidate_skill_semantic_redispatch_rejected",
                {"from_skill_id": current_skill_id, "to_skill_id": selected, "reason": "not_locked_by_current_expert"},
            )
            return None
        return selected

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
        script_runs: list[dict[str, Any]] = []
        for event in turn_events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type in {"reference_context", "retrieval_context"}:
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
            if event_type == "ms_agent_runtime" and str(payload.get("step") or "") == "script_execution":
                step_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                for output in step_payload.get("outputs") if isinstance(step_payload.get("outputs"), list) else []:
                    if not isinstance(output, dict):
                        continue
                    script_runs.append({
                        "path": str(output.get("script") or ""),
                        "status": "success" if output.get("ok") else "failed",
                        "input": output.get("stdin_payload") or output.get("args") or {},
                        "output": output.get("json_output") or output.get("return_value") or output.get("stdout") or "",
                        "error": str(output.get("error") or output.get("stderr") or ""),
                        "duration_ms": output.get("duration_ms"),
                    })

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

        return {
            "root": self._entry_debug(root),
            "expert_team": self._entry_debug(by_key.get(team_id)),
            "expert": self._entry_debug(by_key.get(expert_id)),
            "skill": self._entry_debug(skill_entry),
            "references": {
                "used": reference_uses,
                "available_but_not_used": [
                    item for item in references_available
                    if item["path"] not in {str(used.get("path") or "") for used in reference_uses}
                ],
            },
            "scripts": script_runs,
            "elapsed_ms": elapsed_ms,
        }

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
                return self._release_dict(existing, obj)
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
            return self._release_dict(row, obj)

    def archive_release(self, release_id: str, *, actor_id: str) -> dict[str, Any]:
        with self.session_factory() as db:
            release = self._require_release(db, release_id)
            release.archived = True
            self._audit(db, actor_id, "release.archived", "release", release_id, {})
            db.commit()
            return {"release_id": release_id, "archived": True}

    def list_releases(self, *, object_type: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = select(WorkbenchReleaseRow, WorkbenchObjectRow).join(
                WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchReleaseRow.object_id
            )
            if object_type:
                query = query.where(WorkbenchObjectRow.object_type == object_type)
            if not include_archived:
                query = query.where(WorkbenchReleaseRow.archived.is_(False), WorkbenchObjectRow.archived.is_(False))
            return [self._release_dict(release, obj) for release, obj in db.execute(query.order_by(WorkbenchReleaseRow.published_at.desc()))]

    def export_release(self, release_id: str, *, actor_id: str) -> tuple[bytes, dict[str, Any]]:
        with self.session_factory() as db:
            root_release = self._require_release(db, release_id)
            entries = self._release_closure(db, root_release)
            package_id = _id("pkg")
            manifest_objects: list[dict[str, Any]] = []
            files: dict[str, bytes] = {}
            for release, obj, revision in entries:
                prefix = f"objects/{obj.object_type}/{obj.object_key}/v{release.release_no}"
                object_payload = {
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "object_key": obj.object_key,
                    "name": obj.name,
                    "description": obj.description,
                    "release_id": release.release_id,
                    "release_no": release.release_no,
                    "revision_id": revision.revision_id,
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
                    "content_hash": release.content_hash,
                    "dependency_locks": release.dependency_locks,
                    "files": object_files,
                })
            root_obj = self._require_object(db, root_release.object_id)
            manifest = {
                "schema_version": 1,
                "package_id": package_id,
                "root": {
                    "object_id": root_obj.object_id,
                    "object_type": root_obj.object_type,
                    "object_key": root_obj.object_key,
                    "release_id": root_release.release_id,
                    "release_no": root_release.release_no,
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
            self._audit(db, actor_id, "package.exported", "release", release_id, {"package_id": package_id, "package_sha256": hashlib.sha256(archive).hexdigest()})
            db.commit()
            return archive, manifest

    def import_package(self, package_bytes: bytes, *, environment: str, actor_id: str) -> dict[str, Any]:
        if not package_bytes or len(package_bytes) > MAX_PACKAGE_BYTES:
            raise WorkbenchError("配置包为空或超过大小限制", code="PACKAGE_TOO_LARGE")
        files = self._read_zip(package_bytes)
        if "manifest.json" not in files:
            raise WorkbenchError("配置包缺少 manifest.json", code="INVALID_PACKAGE")
        try:
            manifest = json.loads(files["manifest.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError("manifest.json 无法解析", code="INVALID_PACKAGE") from exc
        if manifest.get("schema_version") != 1:
            raise WorkbenchError("不支持的配置包 Schema", code="PACKAGE_SCHEMA_MISMATCH")
        claimed_hash = str(manifest.get("manifest_hash") or "")
        unsigned = dict(manifest)
        unsigned.pop("manifest_hash", None)
        if not claimed_hash or _hash(unsigned) != claimed_hash:
            raise WorkbenchError("配置包清单哈希校验失败", code="PACKAGE_HASH_MISMATCH")
        if manifest.get("kernel_fingerprint") != self.kernel_fingerprint:
            raise WorkbenchError("调试与生产内核版本不一致", code="KERNEL_INCOMPATIBLE")
        manifest_objects = manifest.get("objects", [])
        if not isinstance(manifest_objects, list) or not manifest_objects:
            raise WorkbenchError("配置包对象清单不能为空", code="INVALID_PACKAGE")
        object_release_ids = {str(item.get("release_id") or "") for item in manifest_objects if isinstance(item, dict)}
        object_hashes = {
            str(item.get("release_id") or ""): str(item.get("content_hash") or "")
            for item in manifest_objects
            if isinstance(item, dict)
        }
        declared_paths = {"manifest.json"}
        known_capabilities = {item["capability_id"] for item in capability_catalog(self.orchestrator)}
        for item in manifest_objects:
            if not isinstance(item, dict):
                raise WorkbenchError("配置包对象清单不合法", code="INVALID_PACKAGE")
            for lock in item.get("dependency_locks", []):
                dependency_release_id = str(lock.get("release_id") or "")
                if dependency_release_id not in object_release_ids:
                    raise WorkbenchError("配置包缺少依赖版本", code="PACKAGE_DEPENDENCY_MISSING")
                if str(lock.get("content_hash") or "") != object_hashes[dependency_release_id]:
                    raise WorkbenchError("依赖锁内容哈希不匹配", code="PACKAGE_DEPENDENCY_MISMATCH")
            object_files: list[tuple[str, bytes]] = []
            declared_contents: list[tuple[str, bytes]] = []
            for declared in item.get("files", []):
                path = _safe_relative_path(str(declared.get("path") or ""))
                declared_paths.add(path)
                content = files.get(path)
                if content is None or hashlib.sha256(content).hexdigest() != declared.get("sha256"):
                    raise WorkbenchError("配置包文件缺失或哈希不匹配", code="PACKAGE_FILE_HASH_MISMATCH")
                if path.endswith("/object.json"):
                    object_files.append((path, content))
                declared_contents.append((path, content))
                if PurePosixPath(path).suffix.lower() in EXECUTABLE_SUFFIXES:
                    raise WorkbenchError("配置包不得携带可执行代码", code="EXECUTABLE_CONTENT_FORBIDDEN")
            if len(object_files) != 1:
                raise WorkbenchError("每个对象必须且只能包含一个 object.json", code="INVALID_PACKAGE")
            try:
                declaration = json.loads(object_files[0][1])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkbenchError("对象声明无法解析", code="INVALID_PACKAGE") from exc
            for field in ("object_id", "object_type", "object_key", "release_id", "release_no", "content_hash", "dependency_locks"):
                if declaration.get(field) != item.get(field):
                    raise WorkbenchError("对象声明与 manifest 不一致", code="PACKAGE_OBJECT_MISMATCH")
            if declaration.get("object_type") not in VALID_OBJECT_TYPES:
                raise WorkbenchError("对象声明类型不合法", code="INVALID_PACKAGE")
            prefix = object_files[0][0].rsplit("/object.json", 1)[0] + "/"
            script_sources: list[tuple[str, str, bytes]] = []
            for path, content in declared_contents:
                relative_path = path[len(prefix):] if path.startswith(prefix) else path
                if PurePosixPath(relative_path).suffix.lower() == ".py":
                    if declaration.get("object_type") != "skill" or not relative_path.startswith("scripts/"):
                        raise WorkbenchError("Python 脚本只能位于 Skill 的 scripts/ 目录", code="EXECUTABLE_CONTENT_FORBIDDEN")
                    script_sources.append((relative_path, "text/x-python", content))
                elif relative_path == "scripts/requirements.txt":
                    script_sources.append((relative_path, "text/plain", content))
            script_errors = self._script_validation_errors(script_sources)
            if script_errors:
                raise WorkbenchError(
                    "Python 脚本安全校验未通过",
                    code="SCRIPT_REVIEW_FAILED",
                    details={"errors": script_errors},
                )
            unknown = sorted(set((declaration.get("payload") or {}).get("capability_ids") or []) - known_capabilities)
            if unknown:
                raise WorkbenchError(
                    f"配置包引用未知底层能力: {', '.join(unknown)}",
                    code="UNKNOWN_CAPABILITY",
                )
        if set(files) != declared_paths:
            raise WorkbenchError("配置包包含未在 manifest 声明的文件", code="UNDECLARED_PACKAGE_FILE")
        package_hash = hashlib.sha256(package_bytes).hexdigest()
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
            root_release_id = str((manifest.get("root") or {}).get("release_id") or "")
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
                active_rows[0] if active_rows else None,
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
            root_object_id = str((target.manifest.get("root") or {}).get("object_id") or "")
            active_rows = list(
                db.scalars(
                    select(WorkbenchDeploymentRow).where(
                        WorkbenchDeploymentRow.environment == target.environment,
                        WorkbenchDeploymentRow.status == "active",
                    )
                )
            )
            previous = next(
                (item for item in active_rows if str((item.manifest.get("root") or {}).get("object_id") or "") == root_object_id),
                None,
            )
            if previous is not None and previous.deployment_id != target.deployment_id:
                previous.status = "superseded"
                target.previous_deployment_id = previous.deployment_id
            target.status = "active"
            target.activated_by = actor_id
            target.activated_at = utc_now()
            if target.package_bytes:
                try:
                    files = self._read_zip(bytes(target.package_bytes))
                    entries = [self._package_object_entry(item, files) for item in target.manifest.get("objects", [])]
                    for entry in sorted(entries, key=lambda item: {"skill": 0, "expert": 1, "expert_team": 2}.get(item.get("object_type"), 9)):
                        self._install_runtime_entry(entry)
                except (WorkbenchError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
            self._audit(db, actor_id, "deployment.activated", "deployment", target.deployment_id, {"previous_deployment_id": target.previous_deployment_id})
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
            self._audit(db, actor_id, "deployment.rolled_back", "deployment", current.deployment_id, {"restored_deployment_id": previous.deployment_id})
            db.commit()
            return self._deployment_dict(previous)

    def bootstrap_from_runtime(self) -> dict[str, int]:
        """Import the filesystem configuration once as immutable baseline releases."""
        if self.orchestrator is None:
            return {"objects": 0, "releases": 0}
        with self.session_factory() as db:
            existing = db.scalar(select(func.count()).select_from(WorkbenchObjectRow)) or 0
            deleted = db.scalar(
                select(func.count()).select_from(WorkbenchAuditRow).where(WorkbenchAuditRow.action == "object.deleted")
            ) or 0
            if existing or deleted:
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
                "coordinator_expert_id": coordinator_object_id,
                "members": [
                    {"expert_id": item.expert_id, "mention_name": item.mention_name, "routing_brief": item.routing_brief}
                    for item in team.members
                ],
            }
            self._bootstrap_object("expert_team", team.team_id, team.name, payload, locks)
            created += 1
        return {"objects": created, "releases": created}

    def install_runtime_catalog(self) -> None:
        """Install the latest released declarations into the shared runtime registries."""
        if self.orchestrator is None:
            return
        with self.session_factory() as db:
            entries: list[dict[str, Any]] = []
            for obj in db.scalars(select(WorkbenchObjectRow).where(WorkbenchObjectRow.archived.is_(False))):
                release = db.scalar(
                    select(WorkbenchReleaseRow)
                    .where(WorkbenchReleaseRow.object_id == obj.object_id, WorkbenchReleaseRow.archived.is_(False))
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
        self._apply_deletion_tombstones()

    def _apply_deletion_tombstones(self) -> None:
        if self.orchestrator is None:
            return
        with self.session_factory() as db:
            events = db.scalars(
                select(WorkbenchAuditRow)
                .where(WorkbenchAuditRow.action == "object.deleted")
                .order_by(WorkbenchAuditRow.created_at)
            )
            deleted = [
                (
                    str((event.payload or {}).get("object_type") or event.target_type),
                    str((event.payload or {}).get("object_key") or ""),
                )
                for event in events
            ]
        for object_type, object_key in deleted:
            if object_key:
                self._uninstall_runtime_object(object_type, object_key)

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
        revisions = db.execute(
            select(WorkbenchRevisionRow, WorkbenchObjectRow)
            .join(WorkbenchObjectRow, WorkbenchObjectRow.object_id == WorkbenchRevisionRow.object_id)
            .where(WorkbenchRevisionRow.object_id != obj.object_id)
        )
        for revision, owner in revisions:
            locks = revision.dependency_locks or []
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
                    "revision_no": revision.revision_no,
                    "message": f"{owner.name} 的 r{revision.revision_no} 正在引用该对象",
                })
        deployments = db.scalars(
            select(WorkbenchDeploymentRow).where(WorkbenchDeploymentRow.status.in_(("staged", "active")))
        )
        for deployment in deployments:
            if any(
                isinstance(item, dict) and str(item.get("object_id") or "") == obj.object_id
                for item in (deployment.manifest or {}).get("objects", [])
            ):
                blockers.append({
                    "kind": "deployment",
                    "deployment_id": deployment.deployment_id,
                    "environment": deployment.environment,
                    "status": deployment.status,
                    "message": f"{deployment.environment} 环境存在 {deployment.status} 部署",
                })
        return blockers

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
            bundle = registry.get_raw(object_key)
            if bundle is None:
                template = registry.get_raw("general_chat") or self.orchestrator.main_bundle
                if template is None:
                    return
                bundle = copy.copy(template)
                bundle.contract = replace(template.contract, skill_id=object_key, skill_role="child")
                bundle.runtime_metadata = replace(
                    template.runtime_metadata,
                    skill_id=object_key,
                    name=str(entry.get("name") or object_key),
                    version=f"v{int(entry.get('release_no') or 0)}",
                    raw=dict(payload.get("configuration") or {}),
                )
                bundle.root_name = object_key
                bundle._scripts = {}
                registry.bundles[object_key] = bundle
            bundle = configured_skill_bundle(bundle, entry)
            registry.bundles[object_key] = bundle
            return
        if object_type == "expert":
            from hailiang_skills.runtime_bridge.expert_bundle import ExpertDefinition, LockedSkill

            budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
            definition = ExpertDefinition(
                agent_id=object_key,
                name=str(entry.get("name") or object_key),
                rules_markdown=str(payload.get("rules_markdown") or ""),
                skills=tuple(LockedSkill(str(item.get("object_key") or ""), f"v{int(item.get('release_no') or 0)}") for item in locks),
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
        seen: set[str] = set()
        for requested in requested_locks:
            release_id = str(requested.get("release_id") or "").strip()
            if not release_id or release_id in seen:
                raise WorkbenchError("依赖版本不能为空或重复")
            release = self._require_release(db, release_id)
            obj = self._require_object(db, release.object_id)
            if obj.object_type != expected_type or release.archived or obj.archived:
                raise WorkbenchError(f"{object_type} 只能引用未归档的已发布 {expected_type} 版本")
            seen.add(release_id)
            locks.append({
                "object_id": obj.object_id,
                "object_type": obj.object_type,
                "object_key": obj.object_key,
                "release_id": release.release_id,
                "release_no": release.release_no,
                "content_hash": release.content_hash,
            })
        return locks

    def _validate_payload(self, object_type: str, payload: dict[str, Any], locks: list[dict[str, Any]]) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if object_type == "skill":
            if not str(payload.get("prompt_markdown") or "").strip():
                errors.append("Skill Prompt 不能为空")
            known = {item["capability_id"] for item in capability_catalog(self.orchestrator)}
            unknown = sorted(set(payload.get("capability_ids") or []) - known)
            if unknown:
                errors.append(f"存在未知底层能力: {', '.join(unknown)}")
        elif object_type == "expert":
            if not str(payload.get("rules_markdown") or "").strip():
                errors.append("专家规则不能为空")
            if len(locks) < 1:
                errors.append("专家必须引用至少一个已发布 Skill 版本")
            budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
            for key, maximum in (("max_iters", 4), ("max_skill_calls", 3)):
                value = int(budget.get(key, maximum) or maximum)
                if value < 1 or value > maximum:
                    errors.append(f"{key} 必须在 1-{maximum} 之间")
        elif object_type == "expert_team":
            if len(locks) < 2:
                errors.append("专家团必须引用至少两个已发布专家版本")
            coordinator = str(payload.get("coordinator_expert_id") or "")
            if not coordinator or coordinator not in {item["object_id"] for item in locks}:
                errors.append("主协调专家必须属于专家团成员")
            if not str(payload.get("rules_markdown") or "").strip():
                warnings.append("建议补充专家团协同规则")
        return {"valid": not errors, "errors": errors, "warnings": warnings, "checked_at": _iso(utc_now())}

    def _decode_assets(
        self,
        assets: list[dict[str, Any]],
        *,
        object_type: str,
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

        def visit(release: WorkbenchReleaseRow) -> None:
            if release.release_id in visited:
                return
            visited.add(release.release_id)
            obj = self._require_object(db, release.object_id)
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
            files[f"{prefix}/SKILL.md"] = str(payload.get("prompt_markdown") or "").encode("utf-8")
            files[f"{prefix}/runtime_contract.json"] = json.dumps(payload.get("runtime_contract") or {}, ensure_ascii=False, indent=2).encode("utf-8")
        elif obj.object_type == "expert":
            files[f"{prefix}/AGENT.md"] = str(payload.get("rules_markdown") or "").encode("utf-8")
            files[f"{prefix}/agent.yaml"] = yaml.safe_dump({
                "schema_version": 1, "id": obj.object_key, "name": obj.name, "topology": "single_expert",
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

    @staticmethod
    def _package_object_entry(item: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
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
        return materialize_entry_files(entry)

    @staticmethod
    def _read_zip(package_bytes: bytes) -> dict[str, bytes]:
        try:
            with ZipFile(io.BytesIO(package_bytes)) as archive:
                files: dict[str, bytes] = {}
                total = 0
                for info in archive.infolist():
                    path = _safe_relative_path(info.filename)
                    if path in files:
                        raise WorkbenchError("配置包包含重复文件", code="DUPLICATE_PACKAGE_FILE")
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > MAX_PACKAGE_BYTES:
                        raise WorkbenchError("配置包解压后超过大小限制", code="PACKAGE_TOO_LARGE")
                    files[path] = archive.read(info)
                return files
        except BadZipFile as exc:
            raise WorkbenchError("不是合法的 ZIP 配置包", code="INVALID_PACKAGE") from exc

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
            "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def _revision_dict(row) -> dict[str, Any]:
        return {
            "revision_id": row.revision_id, "object_id": row.object_id, "revision_no": row.revision_no,
            "base_revision_id": row.base_revision_id, "payload": row.payload,
            "dependency_locks": row.dependency_locks, "validation": row.validation,
            "content_hash": row.content_hash, "created_by": row.created_by, "created_at": _iso(row.created_at),
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
    def _release_dict(row, obj) -> dict[str, Any]:
        return {
            "release_id": row.release_id, "object_id": row.object_id, "object_type": obj.object_type,
            "object_key": obj.object_key, "name": obj.name, "revision_id": row.revision_id,
            "release_no": row.release_no, "version": f"v{row.release_no}", "dependency_locks": row.dependency_locks,
            "content_hash": row.content_hash, "verification": row.verification, "archived": row.archived,
            "published_by": row.published_by, "published_at": _iso(row.published_at),
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
        return {
            "deployment_id": row.deployment_id, "environment": row.environment,
            "root_release_id": row.root_release_id, "package_hash": row.package_hash,
            "manifest": row.manifest, "status": row.status,
            "previous_deployment_id": row.previous_deployment_id, "imported_by": row.imported_by,
            "imported_at": _iso(row.imported_at), "activated_by": row.activated_by,
            "activated_at": _iso(row.activated_at),
        }
