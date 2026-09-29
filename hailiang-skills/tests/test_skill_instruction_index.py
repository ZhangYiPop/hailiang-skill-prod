from __future__ import annotations

from hailiang_skills.runtime_bridge.skill_instruction_index import build_skill_instruction_index


def test_index_only_keeps_real_direct_reference_paths() -> None:
    index = build_skill_instruction_index(
        """
        # 输出结论
        必须先阅读 references/directions.md，再给出赛事清单。
        不得编造未在资料中的赛事。
        文中示例 references/not-present.md 不应成为依赖。
        """,
        available_reference_paths={"references/directions.md"},
    )

    assert [item.path for item in index.references] == ["references/directions.md"]
    assert "输出结论" in index.references[0].section
    assert any("不得编造" in item for item in index.prohibitions)


def test_index_only_marks_evidence_sensitive_output_for_relevant_turns() -> None:
    index = build_skill_instruction_index(
        "必须使用 references/rules.md 后才能给出结论。",
        available_reference_paths={"references/rules.md"},
    )

    assert not index.is_sensitive_turn(
        draft="好的，我先了解一下你的情况。",
        stage="信息收集",
        next_action="继续提问",
        user_message="孩子今年几年级？",
    )
    assert index.is_sensitive_turn(
        draft="根据你的情况，我给出如下推荐结论。",
        stage="",
        next_action="",
        user_message="请给推荐",
    )


def test_index_retains_explicit_methodology_rules_by_section() -> None:
    index = build_skill_instruction_index(
        """
        ## 尝试盘点阶段
        必须完成尝试盘点后，才能给出方向推荐。
        ## 输出限制
        不得在脚本取数前输出赛事和优势结论。
        """,
        available_reference_paths=set(),
    )

    assert index.has_methodology_gates
    assert any("尝试盘点阶段" in rule for rule in index.methodology_rules)
    assert any("脚本取数" in rule for rule in index.methodology_rules)
