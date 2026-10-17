from pathlib import Path

import pytest

from hailiang_skills.runtime_bridge.jump_cards import (
    JumpCardCatalog,
    JumpCardsError,
    parse_and_validate_jump_cards,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = JumpCardCatalog.load(
    ROOT / "runtime_system_skills" / "recommend_default" / "assets" / "catalog.json"
)


def test_valid_single_assessment_card():
    payload = parse_and_validate_jump_cards(
        {
            "jump_cards": [{
                "intent": "assessment",
                "object_type": "single_assessment",
                "sub_type": "career_interest",
                "reason": "可以先做职业兴趣测评",
            }]
        },
        catalog=CATALOG,
    )
    assert len(payload.jump_cards) == 1
    assert payload.jump_cards[0].sub_type == "career_interest"


def test_empty_jump_cards_ok():
    payload = parse_and_validate_jump_cards({"jump_cards": []}, catalog=CATALOG)
    assert payload.jump_cards == []


def test_filter_and_enum_validation():
    payload = parse_and_validate_jump_cards(
        {
            "jump_cards": [{
                "intent": "product",
                "object_type": "product_list",
                "filter": {"region": "浙江", "grade": "高三", "category": "升学服务"},
                "reason": "按你说的条件看商品",
            }]
        },
        catalog=CATALOG,
    )
    assert payload.jump_cards[0].filter.region == "浙江"

    with pytest.raises(JumpCardsError) as enum_exc:
        parse_and_validate_jump_cards(
            {"jump_cards": [{"intent": "assessment", "object_type": "not_in_catalog"}]},
            catalog=CATALOG,
        )
    assert enum_exc.value.code == "JUMP_CARDS_ENUM"

    with pytest.raises(JumpCardsError) as subtype_exc:
        parse_and_validate_jump_cards(
            {"jump_cards": [{"intent": "assessment", "object_type": "single_assessment"}]},
            catalog=CATALOG,
        )
    assert subtype_exc.value.code == "JUMP_CARDS_SUB_TYPE_REQUIRED"


def test_rejects_more_than_one_card_and_extra_fields():
    with pytest.raises(JumpCardsError) as max_exc:
        parse_and_validate_jump_cards(
            {
                "jump_cards": [
                    {"intent": "course", "object_type": "live"},
                    {"intent": "theme", "object_type": "college_explore"},
                ]
            },
            catalog=CATALOG,
        )
    assert max_exc.value.code == "JUMP_CARDS_MAX"

    with pytest.raises(JumpCardsError):
        parse_and_validate_jump_cards(
            {
                "jump_cards": [{
                    "intent": "course",
                    "object_type": "live",
                    "route": "/live",
                }]
            },
            catalog=CATALOG,
        )
