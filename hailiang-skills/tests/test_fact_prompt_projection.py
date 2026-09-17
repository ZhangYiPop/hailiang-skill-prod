from __future__ import annotations

import unittest

from hailiang_skills.core.fact_prompt_projection import (
    build_effective_fact_ledger,
    visible_fact_recap_risk,
)
from hailiang_skills.runtime_bridge.main_planner import _resume_continuity_instruction
from hailiang_skills.skill_runtime.models import SessionState


class FactPromptProjectionTest(unittest.TestCase):
    def test_fact_ledger_removes_memory_copies_but_keeps_memory_only_facts(self) -> None:
        ledger, diagnostics = build_effective_fact_ledger(
            global_facts={"grade": "初二", "city": "杭州", "has_plan": False},
            current_skill_facts={"grade": "初二", "interest": "游泳", "_pending_questionnaire": {"x": 1}},
            memory_facts={
                "global": {"grade": "初二", "city": "杭州", "has_plan": False},
                "current_skill": {"interest": "游泳"},
                "unresolved": {"goal": "了解赛事"},
            },
            skill_progress={"confirmed_facts": {"grade": "初二"}, "pending_topics": ["赛事"], "stage_label": "说明"},
        )

        self.assertEqual(ledger["effective_facts"], {"grade": "初二", "city": "杭州", "has_plan": False})
        self.assertEqual(ledger["current_skill_facts"], {"interest": "游泳"})
        self.assertEqual(ledger["memory_only_facts"], {"unresolved": {"goal": "了解赛事"}})
        self.assertEqual(ledger["skill_progress"], {"pending_topics": ["赛事"], "stage_label": "说明"})
        self.assertEqual(diagnostics["deduplicated_memory_fact_count"], 4)
        self.assertEqual(diagnostics["deduplicated_current_skill_fact_count"], 1)


    def test_fact_recap_risk_only_flags_mechanical_opening_that_repeats_known_value(self) -> None:
        ledger, _ = build_effective_fact_ledger(
            global_facts={"grade": "初二"}, current_skill_facts={}, memory_facts={}
        )

        self.assertTrue(visible_fact_recap_risk("已了解孩子目前初二，下面我来分析。", ledger)["detected"])
        self.assertFalse(visible_fact_recap_risk("游泳方向可以先从训练连续性和比赛层级看起。", ledger)["detected"])

    def test_resume_instruction_keeps_only_unfinished_topics(self) -> None:
        state = SessionState(
            session_id="resume_fact_projection",
            status_flags={"runtime_skill_progress": {"swimming": {"pending_topics": ["赛事选择"]}}},
        )

        instruction = _resume_continuity_instruction(state, "swimming")

        self.assertIn("赛事选择", instruction)
        self.assertIn("Do not recap confirmed facts", instruction)
        empty_instruction = _resume_continuity_instruction(SessionState(session_id="empty"), "swimming")
        self.assertIn("Answer the current request directly", empty_instruction)
        self.assertIn("do not recap confirmed facts", empty_instruction)
