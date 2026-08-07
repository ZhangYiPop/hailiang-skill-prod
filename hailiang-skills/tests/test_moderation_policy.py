from __future__ import annotations

from pathlib import Path

import pytest

from hailiang_skills.security.models import ModerationBlockedError, ModerationResult
from hailiang_skills.security.moderation_config import ModerationPolicyConfig, load_moderation_policy_config
from hailiang_skills.security.moderation_service import ModerationService


class _CloudProvider:
    available = True
    failure_reason = None

    def __init__(self, labels: list[str]) -> None:
        self.labels = labels

    def check(self, _content: str, *, stage: str, chat_id: str | None = None) -> ModerationResult:
        del stage, chat_id
        return ModerationResult(
            matched=True,
            risk_level="high",
            labels=list(self.labels),
            provider="aliyun",
            mode="cloud",
            request_id="req_security",
        )


class _LocalProvider:
    def check(self, _content: str) -> ModerationResult:
        raise AssertionError("local fallback should not be used")


class _QuarantineStore:
    def __init__(self) -> None:
        self.cases: list[dict] = []

    def create_case(self, **payload):
        self.cases.append(payload)
        return {"case_id": "case_1"}


def _service(labels: list[str], allowed: set[str]) -> tuple[ModerationService, _QuarantineStore]:
    store = _QuarantineStore()
    service = ModerationService(
        lexicon_dir=Path("unused"),
        quarantine_store=store,  # type: ignore[arg-type]
        policy_config=ModerationPolicyConfig(
            allowed_labels_by_provider={"aliyun": frozenset(allowed)},
        ),
        cloud_provider=_CloudProvider(labels),
        local_provider=_LocalProvider(),
    )
    return service, store


@pytest.mark.parametrize("label", ["political_entity", "political_n", "inappropriate_discrimination"])
def test_configured_aliyun_label_passes_without_quarantine(label: str) -> None:
    service, store = _service([label], {"political_entity", "political_n", "inappropriate_discrimination"})

    result = service.check("内部升学资料", stage="output")

    assert result.blocked is False
    assert result.risk_level == "none"
    assert result.labels == [label]
    assert result.raw["policy_override"] == {
        "original_risk_level": "high",
        "allowed_labels": [label],
        "matched_labels": [label],
        "policy_source": "default",
    }
    assert store.cases == []


def test_mixed_allowed_and_unapproved_labels_remain_blocked() -> None:
    service, store = _service(
        ["political_entity", "violence"],
        {"political_entity", "political_n"},
    )

    with pytest.raises(ModerationBlockedError) as error:
        service.check("混合风险内容", stage="output")

    assert error.value.result.risk_level == "high"
    assert store.cases[0]["risk_labels"] == ["political_entity", "violence"]


def test_mixed_education_profile_labels_pass_without_quarantine() -> None:
    service, store = _service(
        ["political_entity", "inappropriate_discrimination"],
        {"political_entity", "political_n", "inappropriate_discrimination"},
    )

    result = service.check("军人子女的升学档案", stage="output")

    assert result.blocked is False
    assert result.raw["policy_override"]["matched_labels"] == [
        "inappropriate_discrimination",
        "political_entity",
    ]
    assert store.cases == []


def test_environment_can_override_aliyun_allowed_labels(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text(
        "security_moderation:\n  allowed_labels:\n    aliyun: [political_entity, political_n]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HAILIANG_SECURITY_ALLOWED_LABELS", "political_n, custom_internal")

    config = load_moderation_policy_config(config_path)

    assert config.allowed_labels("aliyun") == {"political_n", "custom_internal"}
    assert config.source("aliyun") == "HAILIANG_SECURITY_ALLOWED_LABELS"


def test_default_config_allows_education_profile_labels() -> None:
    config = load_moderation_policy_config()

    assert config.allowed_labels("aliyun") >= {
        "political_entity",
        "political_n",
        "inappropriate_discrimination",
    }
