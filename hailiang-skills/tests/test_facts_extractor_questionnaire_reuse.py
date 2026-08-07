from hailiang_skills.skills.facts_extractor import (
    _build_fallback_updates,
    _extract_english_exam_score,
    _extract_foreign_language,
)
from hailiang_skills.skills.common import extract_explicit_exam_province


def test_extracts_foreign_language_and_exam_score_from_natural_intro() -> None:
    text = "我是高一新生，我的选科是英语，英语大考能考 120。"

    assert _extract_foreign_language(text) == "英语"
    assert _extract_english_exam_score(text) == 120
    updates = _build_fallback_updates(
        text,
        known_provinces=[],
        focus_path_ids=[],
        focus_primary_categories=[],
        excluded_path_ids=[],
        excluded_primary_categories=[],
        focus_school_names=[],
    )
    assert updates["foreign_language"] == "英语"
    assert updates["english_exam_score"] == 120


def test_english_exam_score_requires_an_english_score_in_valid_range() -> None:
    assert _extract_english_exam_score("我是高一，英语还不错") is None
    assert _extract_english_exam_score("英语很好，我高1") is None
    assert _extract_english_exam_score("英语大考能考 180 分") is None


def test_exam_province_rejects_birthplace_but_accepts_explicit_and_compact_profiles() -> None:
    provinces = ["北京", "河北", "浙江"]

    assert extract_explicit_exam_province("我是北京的", provinces) is None
    assert extract_explicit_exam_province("我现在住在北京", provinces) is None
    assert extract_explicit_exam_province("我在河北参加高考", provinces) == "河北"
    assert extract_explicit_exam_province("高考省份：北京", provinces) == "北京"
    assert extract_explicit_exam_province("浙江物理类580分", provinces) == "浙江"

    ambiguous_updates = _build_fallback_updates(
        "我是北京的",
        known_provinces=provinces,
        focus_path_ids=[],
        focus_primary_categories=[],
        excluded_path_ids=[],
        excluded_primary_categories=[],
        focus_school_names=[],
    )
    explicit_updates = _build_fallback_updates(
        "高考省份：北京",
        known_provinces=provinces,
        focus_path_ids=[],
        focus_primary_categories=[],
        excluded_path_ids=[],
        excluded_primary_categories=[],
        focus_school_names=[],
    )
    assert ambiguous_updates["student_province"] is None
    assert explicit_updates["student_province"] == "北京"
