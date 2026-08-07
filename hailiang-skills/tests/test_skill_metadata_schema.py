from __future__ import annotations

import unittest
from pathlib import Path

from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry


class SkillMetadataSchemaTest(unittest.TestCase):
    def test_runtime_native_skills_expose_standardized_metadata(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        expected_native_ids = {
            "main_planner": "career_plan_entity",
            "future_explore": "future_explore",
            "interest_explore": "interest_explore",
            "score_improve": "score_improve",
            "subject_advisor": "subject_advisor",
            "junior_multi_path_planning": "junior_multi_path_planning",
            "mock_admission": "mock_admission",
            "multi_path_planning": "multi_path_planning",
        }

        for skill_id, metadata_skill_id in expected_native_ids.items():
            bundle = registry.get(skill_id)
            self.assertIsNotNone(bundle, skill_id)
            assert bundle is not None

            metadata = bundle.runtime_metadata
            self.assertEqual(metadata.skill_id, metadata_skill_id)
            self.assertEqual(metadata.skill_type, "native")
            self.assertIn(metadata.entrypoint_role, {"entry", "child", "specialist"})
            self.assertTrue(metadata.prompt_loading.strategy)
            self.assertGreaterEqual(metadata.retrieval.top_k, 1)
            self.assertGreaterEqual(metadata.retrieval.snippet_chars, 120)
            if metadata.assets.local_enabled:
                assets_dir = bundle.root_dir / metadata.assets.local_dir
                self.assertTrue(assets_dir.is_dir(), assets_dir.as_posix())

    def test_replacement_skills_are_native_and_reference_complete(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        for skill_id in {"mock_admission", "multi_path_planning"}:
            bundle = registry.get(skill_id)
            self.assertIsNotNone(bundle, skill_id)
            assert bundle is not None
            self.assertEqual(bundle.runtime_metadata.skill_type, "native")
            self.assertTrue(bundle.runtime_metadata.brief)
            self.assertTrue(bundle.runtime_metadata.info)
            self.assertTrue(bundle.metadata["questionnaire"]["enabled"])

        multi_path = registry.get("multi_path_planning")
        assert multi_path is not None
        self.assertEqual(multi_path.skill_file.name, "skill.md")
        self.assertIn("references/skill_question_option_rule_table.md", multi_path.references)
        self.assertIn("唯一的题型、候选项", multi_path.skill_markdown)

    def test_local_assets_directories_exist_for_native_skills(self) -> None:
        skills_root = Path(PROJECT_RUNTIME_SKILLS_ROOT)
        registry = load_local_skill_registry(skills_root)
        for bundle in registry.bundles.values():
            metadata = bundle.runtime_metadata
            if metadata.skill_type != "native" or not metadata.assets.local_enabled:
                continue
            self.assertTrue((bundle.root_dir / metadata.assets.local_dir).exists())

    def test_subject_advisor_contains_business_workflow_contract(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("subject_advisor")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertIn("初高中衔接选科规划子场景", bundle.runtime_metadata.description)
        self.assertIn("1. **识别**", bundle.skill_markdown)
        self.assertIn("初中生（L0/L1）默认不输出最终组合", bundle.skill_markdown)
        self.assertIn("做物理 vs 历史等跨学科优劣判断", bundle.skill_markdown)
        self.assertIn("出方案门槛", bundle.skill_markdown)
        self.assertIn("已完成至少 1 次关键验证问题", bundle.skill_markdown)
        self.assertIn("选科资产查询", bundle.skill_markdown)
        self.assertIn("subject_requirements", bundle.skill_markdown)
        self.assertIn("references/05_选科资产说明.md", bundle.references)
        self.assertIn("想学医应该怎么选科", bundle.runtime_metadata.routing.routing_examples)
        self.assertEqual(bundle.runtime_metadata.routing.school_stage_scope, "junior_senior")
        self.assertIn("diagnose", {stage.id for stage in bundle.contract.stages})
        self.assertIn("school_stage", bundle.contract.facts_schema.skill_keys)
        self.assertIn("conflict_tags", bundle.contract.facts_schema.skill_keys)
        self.assertIn("recommended_plan", bundle.contract.facts_schema.skill_keys)

    def test_interest_explore_contains_talent_judgement_workflow_contract(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("interest_explore")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertIn("特长培养、兴趣班选择", bundle.runtime_metadata.description)
        self.assertIn("Path A：特长方向诊断流程", bundle.skill_markdown)
        self.assertIn("R1：必要信息采集", bundle.skill_markdown)
        self.assertIn("R2：特质标签选择", bundle.skill_markdown)
        self.assertIn("R3：条件采集", bundle.skill_markdown)
        self.assertIn("R4：输出推荐", bundle.skill_markdown)
        self.assertIn("孩子适合学什么兴趣班", bundle.runtime_metadata.routing.routing_examples)
        self.assertEqual(bundle.runtime_metadata.routing.school_stage_scope, "primary_junior")
        self.assertIn("r1_basic_info", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r1_experience", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r2_trait_labels", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r3_conditions", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r4_recommendation", {stage.id for stage in bundle.contract.stages})
        self.assertIn("selected_trait_labels", bundle.contract.facts_schema.skill_keys)
        self.assertIn("recommended_directions", bundle.contract.facts_schema.skill_keys)
        self.assertIn("starter_actions", bundle.contract.facts_schema.skill_keys)
        self.assertIn("observation_plan", bundle.contract.facts_schema.skill_keys)
        self.assertIn("references/references_01_特质标签与方向映射.md", bundle.references)
        self.assertIn("references/references_05_特长培养与升学应用检索规范.md", bundle.references)


if __name__ == "__main__":
    unittest.main()
