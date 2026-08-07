from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hailiang_skills.api.routes.skill_analytics import build_skill_analytics_router
from hailiang_skills.core.skill_analytics import SkillAnalyticsService, parse_analytics_time


BASE = datetime(2026, 8, 4, 2, tzinfo=UTC)


def _event(event_id: str, event_type: str, payload: dict, *, minute: int = 0) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "session_id": "session-1",
        "created_at": (BASE + timedelta(minutes=minute)).isoformat(),
        "payload": payload,
    }


def test_skill_analytics_distinguishes_multi_skill_exposure_and_channel() -> None:
    events = [
        _event("legacy-routes", "route_suggestions_created", {"route_suggestions": [{"target_skill_id": "a"}, {"target_skill_id": "b"}]}),
        _event("exposure-1", "skill_route_suggestions_exposed", {"skill_ids": ["a", "b", "a"], "run_id": "r1", "dedupe_key": "exposure:r1:m1", "legacy_event_ids": ["legacy-routes"]}),
        _event("transition-a", "skill_transition_requested", {"action": "enter", "to_skill_id": "a", "source": "route_suggestion"}),
        _event("activate-a", "skill_activation_succeeded", {"skill_id": "a", "source": "route_suggestion", "run_id": "r2", "dedupe_key": "activation:r2:a", "transition_event_id": "transition-a"}),
        _event("toolbar-b", "skill_toolbar_clicked", {"skill_id": "b", "run_id": "r3", "dedupe_key": "toolbar:r3:b"}),
        _event("activate-b", "skill_activation_succeeded", {"skill_id": "b", "source": "toolbar", "run_id": "r3", "dedupe_key": "activation:r3:b"}),
    ]
    result = SkillAnalyticsService(lambda _start, _end: events).query(start_at=None, end_at=None)
    rows = {row["skill_id"]: row for row in result["skills"]}

    assert rows["a"]["skill_click_through_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert rows["a"]["chat_recommendation_entry_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert rows["b"]["skill_click_through_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert rows["b"]["chat_recommendation_entry_rate"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert result["aggregate"]["chat_recommendation_entry_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}


def test_skill_analytics_derives_legacy_events_and_filters_time_and_skill() -> None:
    events = [
        _event("legacy-route", "route_suggestions_created", {"route_suggestions": [{"target_skill_id": "legacy"}]}, minute=0),
        _event("legacy-enter", "skill_transition_requested", {"action": "enter", "to_skill_id": "legacy", "source": "route_suggestion"}, minute=1),
        _event("later", "skill_toolbar_clicked", {"skill_id": "other", "run_id": "r2"}, minute=10),
    ]
    service = SkillAnalyticsService(lambda _start, _end: events)
    result = service.query(start_at=BASE, end_at=BASE + timedelta(minutes=2), skill_id="legacy")

    assert result["historical_derived"] is True
    assert [row["skill_id"] for row in result["skills"]] == ["legacy"]
    assert result["skills"][0]["chat_recommendation_entry_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert result["skills"][0]["skill_click_through_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}


def test_skill_analytics_returns_null_rate_without_denominator_and_validates_time() -> None:
    result = SkillAnalyticsService(lambda _start, _end: []).query(start_at=BASE, end_at=BASE, skill_id="empty")
    assert result["skills"][0]["skill_click_through_rate"]["rate"] is None
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_analytics_time("not-a-time", name="start_time")
    with pytest.raises(ValueError, match="timezone"):
        parse_analytics_time("2026-08-04T10:30:00", name="start_time")


def test_skill_analytics_endpoint_validates_times_and_skill_id() -> None:
    class Registry:
        def get(self, skill_id: str):
            return {"skill_id": skill_id} if skill_id == "known" else None

    app = FastAPI()
    app.include_router(build_skill_analytics_router(type("Orchestrator", (), {"runtime_registry": Registry()})()))
    client = TestClient(app)

    assert client.get("/analytics/skills", params={"skill_id": "unknown"}).status_code == 422
    assert client.get("/analytics/skills", params={"skill_id": "known", "start_time": "nope"}).status_code == 422
