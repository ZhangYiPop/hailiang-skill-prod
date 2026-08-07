from __future__ import annotations

import json
import subprocess
import sys
import unittest

from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.skill_runtime.tools import build_tool_specs, execute_tool_call, make_tool_call_request


class SubjectRequirementAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("subject_advisor")
        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.bundle = bundle
        self.asset_path = bundle.root_dir / "assets" / "subject_selection" / "subject_requirements.json"
        self.lookup_script = bundle.root_dir / "scripts" / "subject_requirements_lookup.py"

    def test_subject_advisor_declares_structured_requirement_asset(self) -> None:
        self.assertTrue(self.asset_path.is_file())
        self.assertTrue(self.lookup_script.is_file())
        self.assertIn("references/05_选科资产说明.md", self.bundle.references)
        self.assertIn("subject_requirements", self.bundle.skill_markdown)
        self.assertIn("《2021 全国选科通用指引》Excel", self.bundle.references["references/05_选科资产说明.md"])

        asset = json.loads(self.asset_path.read_text(encoding="utf-8"))
        self.assertEqual(asset["asset_id"], "subject_selection_requirements_2021_generic")
        self.assertEqual(asset["row_count"], 879)
        self.assertEqual(asset["schema"]["field_semantics"]["minimum_requirement"][:6], "选科最低要求")

        clinical = next(item for item in asset["records"] if item["major_name"] == "临床医学")
        self.assertEqual(clinical["major_category"], "临床医学类")
        self.assertEqual(clinical["minimum_requirement"], "物理+化学")
        self.assertEqual(clinical["required_subject_groups"], [["物理", "化学"]])

    def test_lookup_script_handles_major_career_and_combo_queries(self) -> None:
        medicine = self._run_lookup({"query": "我想读医学，该怎么选科", "limit": 8})
        self.assertEqual(medicine["query_type"], "major_requirement")
        self.assertGreater(medicine["total_matches"], 0)
        self.assertIn("医学", medicine["matched_target"]["directions"])

        programmer = self._run_lookup({"query": "我想当程序员，要选什么专业？对应的选科要求是什么", "limit": 5})
        self.assertEqual(programmer["query_type"], "career_requirement")
        self.assertIn("程序员", programmer["matched_target"]["careers"])
        self.assertTrue(any(item["major_category"] == "计算机类" for item in programmer["results"]))

        physics_only = self._run_lookup({"query": "我只选物理可以去读计算机吗？", "limit": 5})
        self.assertEqual(physics_only["query_type"], "major_requirement")
        self.assertIn("not_eligible", physics_only["compatibility_summary"])
        self.assertTrue(any("化学" in item["missing_subjects"] for item in physics_only["results"]))

        physics_chemistry = self._run_lookup({"query": "物化组合可以报考哪些专业？", "limit": 8})
        self.assertEqual(physics_chemistry["query_type"], "subject_combo_coverage")
        self.assertEqual(physics_chemistry["input"]["selected_subjects"], ["物理", "化学"])
        self.assertGreater(physics_chemistry["eligible_count"], 0)
        self.assertTrue(
            any(item["major_category"] == "计算机类" for item in physics_chemistry["top_major_categories"])
        )

    def test_runtime_exposes_subject_requirements_tool_for_subject_advisor(self) -> None:
        state = SessionState(
            session_id="sess_subject_requirements",
            active_skill_id="subject_advisor",
            stage="analyze",
            messages=[ChatMessage(role="user", content="我只选物理可以去读计算机吗？")],
        )
        specs = build_tool_specs(self.bundle, state)
        tool = next((item for item in specs if item.name == "subject_requirements"), None)
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertTrue(tool.enabled)

        result = execute_tool_call(
            self.bundle,
            state,
            make_tool_call_request("subject_requirements", {"query": "我只选物理可以去读计算机吗？", "limit": 5}),
        )
        self.assertTrue(result.ok, result.error)
        payload = json.loads(result.content)
        self.assertEqual(payload["query_type"], "major_requirement")
        self.assertIn("not_eligible", payload["compatibility_summary"])
        self.assertEqual(result.sources, ("subject_selection/subject_requirements.json",))

    def _run_lookup(self, payload: dict) -> dict:
        completed = subprocess.run(
            [sys.executable, str(self.lookup_script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
