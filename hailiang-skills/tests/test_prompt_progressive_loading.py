from __future__ import annotations

import unittest

from hailiang_skills.skill_runtime.cli import _sanitize_assistant_reply
from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState
from hailiang_skills.skill_runtime.session import build_prompt_assembly
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.core.skill_display import build_runtime_skill_catalog


class PromptProgressiveLoadingTest(unittest.TestCase):
    def test_general_chat_prompt_includes_dynamic_user_skill_catalog(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("general_chat")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_general_catalog",
            active_skill_id="general_chat",
            messages=[ChatMessage(role="user", content="我想了解孩子适合什么方向")],
        )
        catalog = build_runtime_skill_catalog(registry)
        assembly = build_prompt_assembly(bundle, state, skill_catalog=catalog)

        self.assertIn("# Available Skills For General Chat", assembly.core_prompt)
        self.assertIn('"skill_id": "career_plan_entity"', assembly.core_prompt)
        self.assertIn("生涯规划", assembly.core_prompt)
        self.assertIn("routing_examples", assembly.core_prompt)
        self.assertEqual(assembly.core_prompt.count('"skill_id": "general_chat"'), 1)
        self.assertNotIn('"skill_id": "main_planner"', assembly.core_prompt)
        self.assertIn("Runtime Facts 中非空的事实已经由可信上游确认", assembly.core_prompt)

    def test_known_grade_is_in_prompt_and_overrides_first_visit_question_template(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("career_plan_entity")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_known_grade",
            active_skill_id="career_plan_entity",
            stage="collect",
            global_facts={"grade": "高一"},
            messages=[ChatMessage(role="user", content="进入生涯规划顾问")],
        )
        assembly = build_prompt_assembly(bundle, state)

        self.assertIn('"grade": "高一"', assembly.core_prompt)
        self.assertIn("Runtime Facts 已有 grade 时，绝不能再问孩子几年级", assembly.core_prompt)

    def test_specialist_prompt_does_not_include_general_chat_catalog(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("future_explore")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_specialist_catalog",
            active_skill_id="future_explore",
            stage="collect",
            messages=[ChatMessage(role="user", content="想了解专业方向")],
        )
        assembly = build_prompt_assembly(
            bundle,
            state,
            skill_catalog=build_runtime_skill_catalog(registry),
        )

        self.assertNotIn("# Available Skills For General Chat", assembly.core_prompt)

    def test_disabled_skill_is_excluded_from_runtime_prompt_catalog(self) -> None:
        registry = load_local_skill_registry(
            PROJECT_RUNTIME_SKILLS_ROOT,
            enabled_by_id={"interest_explore": False},
        )
        bundle = registry.get("general_chat")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_disabled_catalog",
            active_skill_id="general_chat",
            messages=[ChatMessage(role="user", content="帮我看看方向")],
        )
        assembly = build_prompt_assembly(
            bundle,
            state,
            skill_catalog=build_runtime_skill_catalog(registry),
        )

        self.assertNotIn('"skill_id": "interest_explore"', assembly.core_prompt)

    def test_main_planner_uses_progressive_prompt_loading(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_test",
            active_skill_id="main_planner",
            stage="collect",
            messages=[ChatMessage(role="user", content="浙江高中选科规则是什么？")],
        )

        assembly = build_prompt_assembly(bundle, state)

        self.assertEqual(bundle.runtime_metadata.prompt_loading.strategy, "progressive")
        self.assertEqual(bundle.runtime_metadata.planner.scene_selection.mode, "profile_matrix")
        self.assertEqual(
            bundle.runtime_metadata.planner.scene_selection.matrix_reference,
            "references/06_画像_说明_五型与选择.md",
        )
        self.assertEqual(bundle.runtime_metadata.response_policy.citation_visibility, "hidden")
        self.assertIn("# Skill Metadata", assembly.core_prompt)
        self.assertIn("# Runtime Clock", assembly.core_prompt)
        self.assertIn("china_timezone=Asia/Shanghai", assembly.core_prompt)
        self.assertIn("china_datetime=", assembly.core_prompt)
        self.assertIn("# Skill Instructions", assembly.core_prompt)
        self.assertNotIn("# Reference Files", assembly.core_prompt)
        self.assertIn("# Reference Catalog", assembly.retrieval_prompt)
        self.assertTrue(
            "# Retrieved Knowledge Snippets" in assembly.retrieval_prompt
            or "# Matched Generated Assets" in assembly.retrieval_prompt
        )
        self.assertIn("浙江", assembly.retrieval_prompt)
        self.assertNotIn("references/04_新高考选科规则.md", assembly.retrieval_prompt)
        self.assertNotIn("title=04_新高考选科规则", assembly.retrieval_prompt)
        self.assertTrue(
            "Supporting Snippet" in assembly.retrieval_prompt
            or "Matched Generated Assets" in assembly.retrieval_prompt
        )

    def test_progressive_loading_keeps_reference_body_out_of_core_prompt(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_test",
            active_skill_id="main_planner",
            stage="analyze",
            messages=[ChatMessage(role="user", content="孩子高中，想了解浙江 3+3 选科规则")],
        )

        assembly = build_prompt_assembly(bundle, state)
        reference_body = bundle.references["references/04_新高考选科规则.md"]

        self.assertNotIn(reference_body[:120], assembly.core_prompt)
        self.assertNotIn("04_新高考选科规则.md", assembly.retrieval_prompt)
        self.assertIn("loaded_references=", assembly.retrieval_prompt)
        self.assertIn("高中", assembly.final_prompt)

    def test_child_skill_with_local_assets_builds_catalog_without_global_generated_assets(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("future_explore")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        state = SessionState(
            session_id="sess_child",
            active_skill_id="future_explore",
            stage="collect",
            messages=[ChatMessage(role="user", content="我想了解专业前景方向")],
        )

        assembly = build_prompt_assembly(bundle, state)

        self.assertEqual(bundle.runtime_metadata.assets.generated_domains, ())
        self.assertIn("# Local Asset Catalog", assembly.retrieval_prompt)
        self.assertEqual(assembly.generated_asset_domains, ())

    def test_response_policy_sanitizes_reference_mentions_for_user_visible_reply(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        reply = "结合参考文献02和06的画像规则，详见 references/06_用户画像&规划策略&可探索场景.md。"

        sanitized = _sanitize_assistant_reply(
            reply,
            response_policy=bundle.runtime_metadata.response_policy,
        )

        self.assertNotIn("参考文献02", sanitized)
        self.assertNotIn("06_用户画像&规划策略&可探索场景.md", sanitized)
        self.assertIn("平台内", sanitized)


if __name__ == "__main__":
    unittest.main()
