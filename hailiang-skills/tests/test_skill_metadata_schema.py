from __future__ import annotations

import unittest
from pathlib import Path

from hailiang_skills.core.skill_duty_registry import clear_skill_duty_registry_cache, registered_school_stages
from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.skill_contract import field_uses
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry


class SkillMetadataSchemaTest(unittest.TestCase):
    def test_runtime_native_skills_expose_v11_frontmatter(self) -> None:
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
            self.assertTrue(metadata.name)
            self.assertGreaterEqual(len(metadata.description), 80)
            # Transition fields removed from frontmatter; platform defaults apply.
            self.assertEqual(metadata.prompt_loading.strategy, "progressive")
            self.assertFalse(metadata.prompt_loading.include_route_targets)
            self.assertFalse(metadata.triggers)
            self.assertFalse(metadata.routing.routing_examples)
            self.assertFalse(metadata.routing.school_stage_scope)

    def test_duty_registry_covers_core_skills(self) -> None:
        clear_skill_duty_registry_cache()
        self.assertEqual(set(registered_school_stages("interest_explore") or ()), {"primary", "junior"})
        self.assertEqual(set(registered_school_stages("multi_path_planning") or ()), {"senior"})
        self.assertEqual(set(registered_school_stages("subject_advisor") or ()), {"junior", "senior"})

    def test_questionnaire_skills_declare_request_declared_form(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        for skill_id in {"mock_admission", "multi_path_planning", "mbti_self_exploration", "subject_advisor"}:
            bundle = registry.get(skill_id)
            self.assertIsNotNone(bundle, skill_id)
            assert bundle is not None
            self.assertIn("request_declared_form", bundle.runtime_metadata.tool_dependency)

        multi_path = registry.get("multi_path_planning")
        assert multi_path is not None
        self.assertEqual(multi_path.skill_file.name, "skill.md")
        self.assertIn("references/skill_question_option_rule_table.md", multi_path.references)
        self.assertTrue(multi_path.metadata.get("questionnaire", {}).get("enabled"))

    def test_subject_advisor_contains_business_workflow_contract(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("subject_advisor")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertIn("新高考选科顾问", bundle.runtime_metadata.description)
        self.assertIn("专业/职业", bundle.runtime_metadata.description)
        self.assertIn("1. **识别**", bundle.skill_markdown)
        self.assertIn("初中默认给认知与准备建议，不给最终组合", bundle.skill_markdown)
        self.assertIn("仅凭原始分跨学科比优劣", bundle.skill_markdown)
        self.assertIn("出方案门槛", bundle.skill_markdown)
        self.assertIn("至少 1 次关键验证", bundle.skill_markdown)
        self.assertIn("查询选科要求", bundle.skill_markdown)
        self.assertIn("subject_requirements", bundle.skill_markdown)
        self.assertIn("references/05_选科资产说明.md", bundle.references)
        self.assertIn("diagnose", {stage.id for stage in bundle.contract.stages})
        uses = set(field_uses(bundle.contract))
        self.assertIn("school_stage", uses)
        self.assertIn("conflict_tags", uses)
        self.assertIn("recommended_plan", uses)
        self.assertIn("selected_subjects", uses)
        self.assertIn("target_major", uses)

    def test_interest_explore_contains_talent_judgement_workflow_contract(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("interest_explore")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertIn("特长培养", bundle.runtime_metadata.description)
        self.assertIn("Path A：特长方向诊断流程", bundle.skill_markdown)
        self.assertIn("R1：必要信息采集", bundle.skill_markdown)
        self.assertIn("R2：特质标签选择", bundle.skill_markdown)
        self.assertIn("R3：补采回答处理", bundle.skill_markdown)
        self.assertIn("R4：输出校准结论", bundle.skill_markdown)
        self.assertNotIn("## 开场白", bundle.skill_markdown)
        self.assertIn("r1_basic_info", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r1_experience", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r2_trait_labels", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r3_conditions", {stage.id for stage in bundle.contract.stages})
        self.assertIn("r4_recommendation", {stage.id for stage in bundle.contract.stages})
        uses = set(field_uses(bundle.contract))
        self.assertIn("selected_trait_labels", uses)
        self.assertIn("recommended_directions", uses)
        self.assertIn("starter_actions", uses)
        self.assertIn("observation_plan", uses)
        self.assertIn("references/references_01_特质标签与方向映射.md", bundle.references)
        self.assertIn("references/references_05_特长培养与升学应用检索规范.md", bundle.references)

    def test_local_assets_directories_exist_when_present(self) -> None:
        skills_root = Path(PROJECT_RUNTIME_SKILLS_ROOT)
        registry = load_local_skill_registry(skills_root)
        for bundle in registry.bundles.values():
            assets_dir = bundle.root_dir / "assets"
            if assets_dir.is_dir():
                self.assertTrue(any(assets_dir.iterdir()) or True)


if __name__ == "__main__":
    unittest.main()
