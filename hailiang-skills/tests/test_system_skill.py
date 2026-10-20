from pathlib import Path

import pytest

from hailiang_skills.runtime_bridge.system_skill import (
    SystemSkillError,
    load_system_skills_by_purpose,
    validate_system_skill_package,
)


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_ROOT = ROOT / "runtime_system_skills"


def test_default_system_skills_load():
    by_purpose = load_system_skills_by_purpose(SYSTEM_ROOT)
    assert set(by_purpose) == {"orchestrate_head", "orchestrate_tail", "recommend"}
    assert by_purpose["recommend"].catalog is not None
    assert by_purpose["recommend"].catalog["items"]
    assert "职责" in by_purpose["orchestrate_head"].body
    assert "不按 skill 归并" in by_purpose["orchestrate_head"].body
    assert "逐条保留原问题" in by_purpose["orchestrate_head"].body
    assert by_purpose["orchestrate_head"].frontmatter["skill_type"] == "system"


def test_reject_extra_frontmatter_and_forbidden_paths(tmp_path: Path):
    package = tmp_path / "bad_head"
    package.mkdir()
    (package / "SKILL.md").write_text(
        """---
skill_type: system
purpose: orchestrate_head
skill_id: bad_head
name: 坏包
version: "1.0"
description: 不允许
---

## 1. 职责
x
## 2. 可用输入
x
## 3. 判断规则
x
## 4. 输出要求
x
## 5. 禁止
x
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemSkillError) as frontmatter_exc:
        validate_system_skill_package(package, expected_purpose="orchestrate_head")
    assert frontmatter_exc.value.code == "SYSTEM_SKILL_FRONTMATTER_KEY"

    (package / "SKILL.md").write_text(
        """---
skill_type: system
purpose: orchestrate_head
skill_id: bad_head
name: 坏包
version: "1.0"
---

## 1. 职责
x
## 2. 可用输入
x
## 3. 判断规则
x
## 4. 输出要求
x
## 5. 禁止
x
""",
        encoding="utf-8",
    )
    (package / "runtime_contract.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemSkillError) as path_exc:
        validate_system_skill_package(package, expected_purpose="orchestrate_head")
    assert path_exc.value.code == "SYSTEM_SKILL_FORBIDDEN_PATH"


def test_recommend_requires_catalog_only_assets(tmp_path: Path):
    package = tmp_path / "recommend_bad"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        (SYSTEM_ROOT / "recommend_default" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(SystemSkillError) as missing:
        validate_system_skill_package(package, expected_purpose="recommend")
    assert missing.value.code == "SYSTEM_SKILL_CATALOG_MISSING"

    (assets / "catalog.json").write_text('{"schema_version":1,"items":[]}', encoding="utf-8")
    with pytest.raises(SystemSkillError) as empty:
        validate_system_skill_package(package, expected_purpose="recommend")
    assert empty.value.code == "SYSTEM_SKILL_CATALOG_EMPTY"

    good = SYSTEM_ROOT / "recommend_default"
    definition = validate_system_skill_package(good, expected_purpose="recommend")
    assert definition.skill_id == "recommend_default"
