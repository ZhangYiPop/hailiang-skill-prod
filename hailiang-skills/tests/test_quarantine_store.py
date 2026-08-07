from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hailiang_skills.security.quarantine_store import QuarantineStore, QuarantineStoreError


def _key() -> bytes:
    return b"0123456789abcdef0123456789abcdef"


def test_triggered_content_is_encrypted_and_retrievable(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "security_quarantine", key=_key())
    record = store.create_case(
        input_content="用户违规输入",
        output_content="模型违规输出",
        stage="output",
        moderation_mode="cloud",
        provider="aliyun",
        risk_level="high",
        risk_labels=["political"],
        trace_id="trace-1",
        session_id="session-1",
        turn_id="turn-1",
    )

    payload_path = tmp_path / "security_quarantine" / "payloads" / f"{record['case_id']}.bin"
    assert payload_path.read_bytes() != json.dumps({"input_content": "用户违规输入"}).encode()
    assert store.read_payload(record["case_id"], reviewer_id="reviewer-1") == {
        "input_content": "用户违规输入",
        "output_content": "模型违规输出",
        "stream_received_content": None,
        "matched_text": None,
    }
    assert store.get_case(record["case_id"])["content_hash"].startswith("sha256:")


def test_metadata_has_no_original_content_and_review_delete_is_audited(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "security_quarantine", key=_key())
    record = store.create_case(
        input_content="secret input",
        stage="input",
        moderation_mode="local_fallback",
        provider="local",
        risk_level="medium",
    )
    index_text = (tmp_path / "security_quarantine" / "index" / f"{record['case_id']}.json").read_text()
    assert "secret input" not in index_text

    store.update_review(record["case_id"], reviewer_id="reviewer-1", review_status="confirmed")
    deleted = store.delete_payload(record["case_id"], reviewer_id="reviewer-1", reason="retention request")
    assert deleted["payload_available"] is False
    assert store.get_case(record["case_id"])["content_hash"].startswith("sha256:")
    with pytest.raises(KeyError):
        store.read_payload(record["case_id"], reviewer_id="reviewer-1")

    audit = (tmp_path / "security_quarantine" / "audit.jsonl").read_text()
    assert "security_quarantine_created" in audit
    assert "security_review_updated" in audit
    assert "security_quarantine_deleted" in audit
    assert "secret input" not in audit


def test_missing_key_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HAILIANG_SECURITY_QUARANTINE_KEY", raising=False)
    store = QuarantineStore(tmp_path / "security_quarantine", key=None)
    assert store.available is False
    with pytest.raises(QuarantineStoreError):
        store.create_case(
            input_content="must not be dropped",
            stage="input",
            moderation_mode="cloud",
            provider="aliyun",
            risk_level="high",
        )


def test_environment_key_accepts_urlsafe_base64(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEST_QUARANTINE_KEY", base64.urlsafe_b64encode(_key()).decode())
    store = QuarantineStore(tmp_path, key_env="TEST_QUARANTINE_KEY")
    assert store.available is True
