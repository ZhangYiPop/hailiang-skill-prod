from __future__ import annotations

import pytest

from hailiang_skills.core.deployment import deployment_environment, is_valid_deployment_environment


@pytest.mark.parametrize("value", ["test", "prod", "test-next", "test-release-2026"])
def test_parallel_test_environment_names_are_valid(value: str) -> None:
    assert is_valid_deployment_environment(value)


@pytest.mark.parametrize("value", ["", "development", "prod-next", "test_2", "test-"])
def test_invalid_environment_names_are_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("HAILIANG_DEPLOY_ENV", value)
    with pytest.raises(RuntimeError, match="test, prod, or test-<name>"):
        deployment_environment()
