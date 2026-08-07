from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class QuarantineStoreError(RuntimeError):
    """Raised when protected moderation evidence cannot be safely handled."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _safe_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return cleaned[:160] or "unknown"


class QuarantineStore:
    """Encrypted, append-only metadata and protected evidence store.

    Metadata is intentionally plaintext but contains no original content. Payloads
    are AES-256-GCM encrypted. The key must be supplied by the environment or by
    the caller; no key is generated or persisted by this class.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        key: bytes | None = None,
        key_env: str = "HAILIANG_SECURITY_QUARANTINE_KEY",
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_dir = self.root / "index"
        self.payload_dir = self.root / "payloads"
        self.audit_path = self.root / "audit.jsonl"
        self._key = key or self._load_key(key_env)
        if self._key is not None and len(self._key) != 32:
            raise QuarantineStoreError("quarantine key must be exactly 32 bytes for AES-256-GCM")

    @staticmethod
    def _load_key(key_env: str) -> bytes | None:
        raw = os.getenv(key_env, "").strip()
        if not raw:
            return None
        try:
            decoded = base64.urlsafe_b64decode(raw.encode("ascii") + b"=" * (-len(raw) % 4))
        except (ValueError, UnicodeEncodeError):
            decoded = b""
        if len(decoded) == 32:
            return decoded
        try:
            decoded = bytes.fromhex(raw)
        except ValueError:
            decoded = b""
        if len(decoded) == 32:
            return decoded
        raise QuarantineStoreError(f"{key_env} must be base64url or hex AES key")

    @property
    def available(self) -> bool:
        return self._key is not None

    def _require_key(self) -> bytes:
        if self._key is None:
            raise QuarantineStoreError(
                "quarantine encryption key is not configured; refusing to store original content"
            )
        return self._key

    def _prepare_dirs(self) -> None:
        for directory in (self.root, self.index_dir, self.payload_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    def _write_private(self, path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(data)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _append_audit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._prepare_dirs()
        event = {
            "event_id": f"audit_{uuid4().hex[:16]}",
            "event_type": event_type,
            "created_at": _utc_now(),
            "payload": payload,
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        try:
            self.audit_path.chmod(0o600)
        except OSError:
            pass

    def create_case(
        self,
        *,
        input_content: str | None = None,
        output_content: str | None = None,
        stream_received_content: str | None = None,
        matched_text: str | None = None,
        trace_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        stage: str,
        event_type: str = "moderation_blocked",
        moderation_mode: str,
        provider: str,
        risk_level: str,
        risk_labels: Iterable[str] = (),
        failure_reason: str | None = None,
        lexicon_version: str | None = None,
        provider_request_id: str | None = None,
    ) -> dict[str, Any]:
        key = self._require_key()
        self._prepare_dirs()
        case_id = f"sec_{uuid4().hex[:20]}"
        original = {
            "input_content": input_content,
            "output_content": output_content,
            "stream_received_content": stream_received_content,
            "matched_text": matched_text,
        }
        original_json = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        encrypted = nonce + AESGCM(key).encrypt(nonce, original_json, case_id.encode("utf-8"))
        payload_path = self.payload_dir / f"{case_id}.bin"
        payload_path.write_bytes(encrypted)
        try:
            payload_path.chmod(0o600)
        except OSError:
            pass

        all_content = "\n".join(value for value in (input_content, output_content, stream_received_content) if value)
        metadata = {
            "case_id": case_id,
            "trace_id": trace_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "stage": stage,
            "event_type": event_type,
            "moderation_mode": moderation_mode,
            "provider": provider,
            "risk_level": risk_level,
            "risk_labels": sorted({str(label) for label in risk_labels if str(label).strip()}),
            "failure_reason": failure_reason,
            "lexicon_version": lexicon_version,
            "provider_request_id": provider_request_id,
            "content_hash": _sha256(all_content),
            "created_at": _utc_now(),
            "retention_policy": "indefinite",
            "review_status": "pending",
            "payload_available": True,
        }
        self._write_private(
            self.index_dir / f"{case_id}.json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        self._append_audit("security_quarantine_created", {
            "case_id": case_id,
            "content_hash": metadata["content_hash"],
            "stage": stage,
            "risk_level": risk_level,
            "moderation_mode": moderation_mode,
        })
        return metadata

    def list_cases(
        self,
        *,
        status: str | None = None,
        risk_level: str | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.index_dir.glob("sec_*.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status and record.get("review_status") != status:
                continue
            if risk_level and record.get("risk_level") != risk_level:
                continue
            if stage and record.get("stage") != stage:
                continue
            records.append(record)
            if len(records) >= max(1, min(limit, 1000)):
                break
        return records

    def get_case(self, case_id: str) -> dict[str, Any]:
        path = self.index_dir / f"{_safe_id(case_id)}.json"
        if not path.is_file():
            raise KeyError(case_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def record_case_view(self, case_id: str, *, reviewer_id: str) -> None:
        self.get_case(case_id)
        self._append_audit("security_quarantine_viewed", {
            "case_id": case_id,
            "reviewer_id": reviewer_id,
            "view": "metadata",
        })

    def read_payload(self, case_id: str, *, reviewer_id: str) -> dict[str, Any]:
        metadata = self.get_case(case_id)
        if not metadata.get("payload_available"):
            raise KeyError(case_id)
        raw = (self.payload_dir / f"{_safe_id(case_id)}.bin").read_bytes()
        try:
            plaintext = AESGCM(self._require_key()).decrypt(
                raw[:12], raw[12:], _safe_id(case_id).encode("utf-8")
            )
            payload = json.loads(plaintext.decode("utf-8"))
        except (OSError, InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuarantineStoreError("quarantine payload could not be decrypted") from exc
        self._append_audit("security_quarantine_viewed", {
            "case_id": case_id,
            "reviewer_id": reviewer_id,
        })
        return payload

    def export_payload(self, case_id: str, *, reviewer_id: str) -> dict[str, Any]:
        payload = self.read_payload(case_id, reviewer_id=reviewer_id)
        self._append_audit("security_quarantine_exported", {
            "case_id": case_id,
            "reviewer_id": reviewer_id,
        })
        return payload

    def update_review(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        review_status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"pending", "confirmed", "false_positive", "resolved"}
        if review_status not in allowed:
            raise ValueError(f"review_status must be one of {sorted(allowed)}")
        path = self.index_dir / f"{_safe_id(case_id)}.json"
        metadata = self.get_case(case_id)
        metadata["review_status"] = review_status
        metadata["reviewed_by"] = reviewer_id
        metadata["reviewed_at"] = _utc_now()
        if note:
            metadata["review_note_hash"] = _sha256(note)
        self._write_private(path, json.dumps(metadata, ensure_ascii=False, indent=2))
        self._append_audit("security_review_updated", {
            "case_id": case_id,
            "reviewer_id": reviewer_id,
            "review_status": review_status,
            "note_hash": _sha256(note) if note else None,
        })
        return metadata

    def delete_payload(self, case_id: str, *, reviewer_id: str, reason: str) -> dict[str, Any]:
        path = self.payload_dir / f"{_safe_id(case_id)}.bin"
        metadata = self.get_case(case_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        metadata["payload_available"] = False
        metadata["payload_deleted_at"] = _utc_now()
        metadata["payload_deleted_by"] = reviewer_id
        metadata["payload_delete_reason_hash"] = _sha256(reason)
        self._write_private(
            self.index_dir / f"{_safe_id(case_id)}.json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        self._append_audit("security_quarantine_deleted", {
            "case_id": case_id,
            "reviewer_id": reviewer_id,
            "reason_hash": _sha256(reason),
            "content_hash": metadata.get("content_hash"),
        })
        return metadata
