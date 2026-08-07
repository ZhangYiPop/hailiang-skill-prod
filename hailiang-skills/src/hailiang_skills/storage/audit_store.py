"""Encrypted full-text audit storage.

Normal logs must only reference the returned audit id and fingerprint.  Payload
decryption is intentionally not exposed through a business API; operators use
the database role and audited operational tooling instead.
"""

from __future__ import annotations

import base64
import os
from datetime import timedelta
from hashlib import sha256
from secrets import token_bytes
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete

from hailiang_skills.core.telemetry import current_telemetry, text_fingerprint
from hailiang_skills.storage.database import AuditPayloadRow, utc_now


class AuditStoreError(RuntimeError):
    pass


class EncryptedAuditStore:
    def __init__(self, session_factory, *, key: str | None = None, key_id: str | None = None) -> None:
        encoded = key or os.getenv("HAILIANG_AUDIT_ENCRYPTION_KEY", "")
        if not encoded:
            raise AuditStoreError("HAILIANG_AUDIT_ENCRYPTION_KEY is required when audit storage is enabled")
        try:
            raw_key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except Exception as exc:  # pragma: no cover - configuration guard
            raise AuditStoreError("audit encryption key must be URL-safe base64") from exc
        if len(raw_key) != 32:
            raise AuditStoreError("audit encryption key must decode to exactly 32 bytes")
        self._aes = AESGCM(raw_key)
        self._session_factory = session_factory
        self._key_id = key_id or os.getenv("HAILIANG_AUDIT_KEY_ID", "primary")
        self._retention_days = int(os.getenv("HAILIANG_AUDIT_RETENTION_DAYS", "90"))

    def write(self, kind: str, content: str, *, session_id: str | None = None) -> dict[str, object]:
        context = current_telemetry()
        audit_id = f"aud_{uuid4().hex}"
        raw = content.encode("utf-8")
        nonce = token_bytes(12)
        # Binding the row identity as AAD prevents ciphertext swapping.
        ciphertext = self._aes.encrypt(nonce, raw, audit_id.encode("utf-8"))
        now = utc_now()
        row = AuditPayloadRow(
            audit_id=audit_id,
            kind=kind,
            request_id=(context.request_id if context else ""),
            session_id=session_id or (context.session_id if context else None),
            content_hash=sha256(raw).hexdigest(),
            content_length=len(content),
            key_id=self._key_id,
            nonce=nonce,
            ciphertext=ciphertext,
            created_at=now,
            expires_at=now + timedelta(days=self._retention_days),
        )
        with self._session_factory.begin() as db:
            db.add(row)
        return {"audit_id": audit_id, **text_fingerprint(content, preview_chars=0)}

    def purge_expired(self) -> int:
        with self._session_factory.begin() as db:
            result = db.execute(delete(AuditPayloadRow).where(AuditPayloadRow.expires_at < utc_now()))
            return int(result.rowcount or 0)

