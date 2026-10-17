from __future__ import annotations

import json

from hailiang_skills.runtime_bridge.recommendation_extractor import (
    build_recommend_prompt,
    extract_jump_cards,
)


CATALOG = {
    "items": [
        {
            "intent": "open",
            "object_type": "skill",
            "sub_type": "multi_path",
            "title": "升学路径",
            "description": "了解升学路径",
        },
        {
            "intent": "open",
            "object_type": "skill",
            "sub_type": "subject",
            "title": "选科建议",
            "description": "了解选科",
        },
    ]
}


class ScriptedClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        return self.text


def test_build_recommend_prompt_includes_catalog_and_forbids_state_lookup():
    prompt = build_recommend_prompt(
        recommendation_rules="RECOMMEND",
        catalog=CATALOG,
        user_question="想了解路径",
        assistant_reply="这是专家回答",
    )
    assert prompt[0].role == "system"
    assert "RECOMMEND" in prompt[0].content
    assert "不要查询会话状态" in prompt[0].content
    assert "multi_path" in prompt[0].content
    assert "想了解路径" in prompt[0].content


def test_extract_jump_cards_filters_by_catalog():
    raw = json.dumps(
        {
            "jump_cards": [
                {
                    "intent": "open",
                    "object_type": "skill",
                    "sub_type": "multi_path",
                    "filter": {},
                    "title": "",
                },
                {
                    "intent": "open",
                    "object_type": "skill",
                    "sub_type": "unknown",
                    "filter": {},
                },
            ]
        },
        ensure_ascii=False,
    )
    client = ScriptedClient(raw)
    cards = extract_jump_cards(
        client,
        CATALOG,
        recommendation_rules="r",
        user_question="路径",
        assistant_reply="回答",
        max_cards=3,
    )
    assert client.calls == 1
    assert len(cards) == 1
    assert cards[0]["sub_type"] == "multi_path"
    assert cards[0]["title"] == "升学路径"


def test_extract_jump_cards_empty_catalog_skips_model():
    client = ScriptedClient('{"jump_cards":[]}')
    assert extract_jump_cards(client, {"items": []}) == []
    assert client.calls == 0


def test_extract_jump_cards_returns_empty_on_invalid_model_output():
    client = ScriptedClient("not-json")
    assert extract_jump_cards(client, CATALOG) == []
