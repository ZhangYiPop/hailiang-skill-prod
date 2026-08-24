"""Deterministic, dependency-free MBTI scoring for the native Skill sandbox."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
DIMENSIONS = (("E", "I"), ("S", "N"), ("T", "F"), ("J", "P"))


def _read_json(name: str) -> dict[str, Any]:
    return json.loads((ASSETS_DIR / name).read_text(encoding="utf-8"))


def _answers_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("answers")
    if not isinstance(raw, dict):
        raw = (payload.get("skill_facts") or {}).get("answers") if isinstance(payload.get("skill_facts"), dict) else {}
    return {
        str(key): str(value).upper()
        for key, value in raw.items()
        if str(value).upper() in {"A", "B"}
    } if isinstance(raw, dict) else {}


def score_answers(answers: dict[str, str]) -> dict[str, Any]:
    rules = _read_json("mbti_93_scoring_rules.json").get("rules", [])
    rule_by_number = {int(item["id"]): item for item in rules if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
    scores = {key: 0 for pair in DIMENSIONS for key in pair}
    missing: list[int] = []
    for number in range(1, 94):
        answer = answers.get(f"mbti_{number:03d}") or answers.get(str(number))
        rule = rule_by_number.get(number)
        if answer not in {"A", "B"} or not rule:
            missing.append(number)
            continue
        dimension = rule["optionA"] if answer == "A" else rule["optionB"]
        scores[dimension] += 1
    if missing:
        return {"ok": False, "answered_count": 93 - len(missing), "missing_question_numbers": missing}
    percentages: dict[str, int] = {}
    tendencies: dict[str, float] = {}
    type_code = ""
    for left, right in DIMENSIONS:
        total = scores[left] + scores[right]
        percentages[left] = round(scores[left] / total * 100) if total else 50
        percentages[right] = round(scores[right] / total * 100) if total else 50
        tendencies[f"{left}{right}"] = round((scores[left] - scores[right]) / total * 10, 1) if total else 0.0
        type_code += left if scores[left] >= scores[right] else right
    type_entries = _read_json("mbti_16_types.json").get("types", [])
    profile = next((item for item in type_entries if isinstance(item, dict) and item.get("code") == type_code), {})
    return {
        "ok": True,
        "answered_count": 93,
        "type": type_code,
        "raw_scores": scores,
        "percentages": percentages,
        "tendencies": tendencies,
        "profile": {
            "name": profile.get("name", ""),
            "dominant": profile.get("dominant", ""),
            "auxiliary": profile.get("auxiliary", ""),
            "best_performance": profile.get("bestPerformance", ""),
            "growth_areas": profile.get("growthAreas", ""),
        },
        "disclaimer": "结果仅用于自我探索，不构成心理、医疗、能力或升学诊断。",
    }


def main() -> None:
    payload = json.load(sys.stdin)
    print(json.dumps(score_answers(_answers_from_payload(payload)), ensure_ascii=False))


if __name__ == "__main__":
    main()
