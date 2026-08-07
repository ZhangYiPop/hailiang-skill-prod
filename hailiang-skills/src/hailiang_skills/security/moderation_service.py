from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hailiang_skills.security.aliyun_provider import AliyunProvider
from hailiang_skills.security.moderation_config import ModerationPolicyConfig, load_moderation_policy_config
from hailiang_skills.security.local_provider import LocalLexiconProvider
from hailiang_skills.security.models import ModerationBlockedError, ModerationResult
from hailiang_skills.security.quarantine_store import QuarantineStore, QuarantineStoreError


class ModerationService:
    def __init__(
        self,
        *,
        lexicon_dir: str | Path,
        quarantine_store: QuarantineStore,
        policy_config: ModerationPolicyConfig | None = None,
        cloud_provider: Any | None = None,
        local_provider: Any | None = None,
    ) -> None:
        self.cloud = cloud_provider or AliyunProvider()
        if local_provider is None:
            from hailiang_skills.security.lexicon_loader import load_lexicon

            local_provider = LocalLexiconProvider(load_lexicon(lexicon_dir))
        self.local = local_provider
        self.quarantine_store = quarantine_store
        self.policy_config = policy_config or load_moderation_policy_config()
        self.logger = logging.getLogger("hailiang.security")

    def check(self, content: str, *, stage: str, trace_id: str = "", session_id: str = "", turn_id: str = "", raise_on_block: bool = True) -> ModerationResult:
        try:
            if self.cloud.available:
                cloud_results = [
                    self.cloud.check(content[index : index + 2000], stage=stage, chat_id=session_id or None)
                    for index in range(0, len(content) or 1, 2000)
                ]
                result = ModerationResult(
                    matched=any(item.matched for item in cloud_results),
                    risk_level=max((item.risk_level for item in cloud_results), key=_risk_rank),
                    labels=sorted({label for item in cloud_results for label in item.labels}),
                    provider="aliyun",
                    mode="cloud",
                    request_id=next((item.request_id for item in cloud_results if item.request_id), None),
                    raw={"windows": [item.raw for item in cloud_results]},
                )
            else:
                raise RuntimeError(self.cloud.failure_reason or "aliyun_content_limit")
        except Exception as exc:
            cloud_failure = str(exc)
            result = self.local.check(content)
            result.failure_reason = cloud_failure
            result.mode = "local_fallback"
        if apply_moderation_policy(result, self.policy_config):
            self.logger.info(
                "moderation_policy_allowed provider=%s labels=%s stage=%s",
                result.provider,
                ",".join(result.labels),
                stage,
            )
        if result.blocked and raise_on_block:
            case_id = self._quarantine(
                content=content,
                stage=stage,
                result=result,
                trace_id=trace_id,
                session_id=session_id,
                turn_id=turn_id,
            )
            raise ModerationBlockedError(result, stage=stage, case_id=case_id)
        return result

    def _quarantine(self, *, content: str, stage: str, result: ModerationResult, trace_id: str, session_id: str, turn_id: str) -> str | None:
        try:
            record = self.quarantine_store.create_case(
                input_content=content if stage == "input" else None,
                output_content=content if stage == "output" else None,
                stream_received_content=content if stage == "stream" else None,
                trace_id=trace_id,
                session_id=session_id,
                turn_id=turn_id,
                stage=stage,
                moderation_mode=result.mode,
                provider=result.provider,
                risk_level=result.risk_level,
                risk_labels=result.labels,
                failure_reason=result.failure_reason,
                lexicon_version=result.lexicon_version,
                provider_request_id=result.request_id,
            )
            return str(record["case_id"])
        except QuarantineStoreError:
            self.logger.exception("security_quarantine_failed; content remains blocked")
            return None


def _risk_rank(level: str) -> int:
    return {"none": 0, "low": 1, "medium": 2, "high": 3}.get(level, 3)


def apply_moderation_policy(result: ModerationResult, config: ModerationPolicyConfig) -> bool:
    """Allow a provider result only when every matched label is explicitly configured."""
    labels = {str(label).strip().lower() for label in result.labels if str(label).strip()}
    allowed = config.allowed_labels(result.provider)
    if not result.blocked or not labels or not labels.issubset(allowed):
        return False
    result.raw.setdefault("policy_override", {})
    result.raw["policy_override"] = {
        "original_risk_level": result.risk_level,
        "matched_labels": sorted(labels),
        "allowed_labels": sorted(labels),
        "policy_source": config.source(result.provider),
    }
    result.matched = False
    result.risk_level = "none"
    return True
