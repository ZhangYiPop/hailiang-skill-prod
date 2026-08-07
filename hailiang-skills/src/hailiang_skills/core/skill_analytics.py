"""Skill activation funnel aggregation over durable session events."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hailiang_skills.core.skill_ids import canonical_skill_id
from hailiang_skills.core.session_logging import SESSION_LOG_ROOT
from hailiang_skills.storage.event_store import read_events_in_range


def parse_analytics_time(value: str | None, *, name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone, for example +08:00 or Z")
    return parsed.astimezone(UTC)


def _event_time(event: dict[str, Any]) -> datetime | None:
    value = event.get("created_at") or event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _file_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in SESSION_LOG_ROOT.glob("*/events.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if isinstance(event, dict):
                    event.setdefault("session_id", path.parent.name)
                    events.append(event)
        except (OSError, json.JSONDecodeError):
            continue
    return events


def default_event_reader(start_at: datetime | None, end_at: datetime | None) -> list[dict[str, Any]]:
    stored = read_events_in_range(start_at, end_at)
    if stored is not None:
        return stored
    return _file_events()


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


class SkillAnalyticsService:
    def __init__(self, event_reader: Callable[[datetime | None, datetime | None], list[dict[str, Any]]] = default_event_reader):
        self._event_reader = event_reader

    def query(
        self,
        *,
        start_at: datetime | None,
        end_at: datetime | None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        events = self._event_reader(start_at, end_at)
        valid_events = [(event, _event_time(event)) for event in events]
        valid_events = [(event, at) for event, at in valid_events if at is not None]
        effective_start = start_at or (min(at for _, at in valid_events) if valid_events else end_at or datetime.now(UTC))
        effective_end = end_at or datetime.now(UTC)
        if effective_start > effective_end:
            raise ValueError("start_time must be earlier than or equal to end_time")
        scoped = [event for event, at in valid_events if effective_start <= at <= effective_end]

        counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        seen: set[str] = set()
        standard_legacy_ids: set[str] = set()
        historical_derived = False
        for event in scoped:
            if event.get("event_type") in {"skill_route_suggestions_exposed", "skill_activation_succeeded"}:
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                standard_legacy_ids.update(str(value) for value in payload.get("legacy_event_ids", []) if value)
                transition_event_id = payload.get("transition_event_id")
                if transition_event_id:
                    standard_legacy_ids.add(str(transition_event_id))

        def add(kind: str, target: str, event: dict[str, Any], *, suffix: str = "") -> None:
            target = canonical_skill_id(target)
            if not target:
                return
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            base_key = str(payload.get("dedupe_key") or "")
            if not base_key:
                base_key = ":".join((str(event.get("session_id") or ""), str(payload.get("run_id") or event.get("run_id") or ""), target, suffix or str(event.get("event_id") or "")))
            key = f"{kind}:{base_key}:{suffix or target}"
            if key in seen:
                return
            seen.add(key)
            counts[target][kind] += 1

        for event in scoped:
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_id = str(event.get("event_id") or "")
            if event_type == "skill_toolbar_clicked":
                add("toolbar_clicks", str(payload.get("skill_id") or ""), event)
            elif event_type == "skill_route_suggestions_exposed":
                for target in {canonical_skill_id(item) for item in payload.get("skill_ids", [])} - {""}:
                    add("route_suggestion_items", target, event, suffix=target)
                    add("route_suggestion_turns", target, event, suffix=target)
            elif event_type == "skill_activation_succeeded":
                target = str(payload.get("skill_id") or "")
                add("activations", target, event)
                if payload.get("source") == "route_suggestion":
                    add("route_activations", target, event)
            elif event_id not in standard_legacy_ids and event_type == "route_suggestions_created":
                historical_derived = True
                suggestions = payload.get("route_suggestions") if isinstance(payload.get("route_suggestions"), list) else []
                for target in {canonical_skill_id(item.get("target_skill_id")) for item in suggestions if isinstance(item, dict)} - {""}:
                    add("route_suggestion_items", target, event, suffix=target)
                    add("route_suggestion_turns", target, event, suffix=target)
            elif event_id not in standard_legacy_ids and event_type == "skill_transition_requested":
                if payload.get("action") != "enter":
                    continue
                target = str(payload.get("to_skill_id") or "")
                source = str(payload.get("source") or "")
                if source == "toolbar":
                    historical_derived = True
                    add("toolbar_clicks", target, event)
                    add("activations", target, event)
                elif source == "route_suggestion":
                    historical_derived = True
                    add("activations", target, event)
                    add("route_activations", target, event)

        requested = canonical_skill_id(skill_id)
        skill_ids = [requested] if requested else sorted(counts)
        rows = [self._row(target, counts[target]) for target in skill_ids]
        aggregate: dict[str, int] = defaultdict(int)
        for target in skill_ids:
            for key, value in counts[target].items():
                aggregate[key] += value
        return {
            "start_time": effective_start.isoformat(),
            "end_time": effective_end.isoformat(),
            "skill_id": requested or None,
            "historical_derived": historical_derived,
            "skills": rows,
            "aggregate": self._row("all", aggregate),
        }

    @staticmethod
    def _row(skill_id: str, count: dict[str, int]) -> dict[str, Any]:
        toolbar = int(count.get("toolbar_clicks", 0))
        suggested = int(count.get("route_suggestion_items", 0))
        activations = int(count.get("activations", 0))
        route_turns = int(count.get("route_suggestion_turns", 0))
        route_activations = int(count.get("route_activations", 0))
        return {
            "skill_id": skill_id,
            "counts": {
                "toolbar_clicks": toolbar,
                "route_suggestion_items": suggested,
                "route_suggestion_turns": route_turns,
                "activated": activations,
                "route_suggestion_activated": route_activations,
            },
            "skill_click_through_rate": {
                "numerator": activations,
                "denominator": toolbar + suggested,
                "rate": _rate(activations, toolbar + suggested),
            },
            "chat_recommendation_entry_rate": {
                "numerator": route_activations,
                "denominator": route_turns,
                "rate": _rate(route_activations, route_turns),
            },
        }
