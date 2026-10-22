"""Round expert progress + final reply selection (tail / failed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from hailiang_skills.runtime_bridge.orchestration_tail import (
    run_tail_merge,
    run_tail_merge_stream,
    simple_concat_merge,
)


CallTail = Callable[..., str]


@dataclass(slots=True)
class RoundExpertsState:
    assigned_expert_ids: list[str] = field(default_factory=list)
    running_expert_ids: list[str] = field(default_factory=list)
    completed_expert_ids: list[str] = field(default_factory=list)
    failed_expert_ids: list[str] = field(default_factory=list)
    status: str = "idle"

    def mark_running(self, expert_id: str) -> None:
        expert = str(expert_id or "").strip()
        if not expert:
            return
        if expert not in self.assigned_expert_ids:
            self.assigned_expert_ids.append(expert)
        if expert not in self.running_expert_ids:
            self.running_expert_ids.append(expert)
        self.status = "running"

    def mark_completed(self, expert_id: str) -> None:
        expert = str(expert_id or "").strip()
        if not expert:
            return
        self.running_expert_ids = [item for item in self.running_expert_ids if item != expert]
        if expert not in self.completed_expert_ids:
            self.completed_expert_ids.append(expert)
        self._refresh_status()

    def mark_failed(self, expert_id: str) -> None:
        expert = str(expert_id or "").strip()
        if not expert:
            return
        self.running_expert_ids = [item for item in self.running_expert_ids if item != expert]
        if expert not in self.failed_expert_ids:
            self.failed_expert_ids.append(expert)
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self.running_expert_ids:
            self.status = "running"
            return
        if not self.assigned_expert_ids:
            self.status = "idle"
            return
        if self.completed_expert_ids and not self.failed_expert_ids:
            self.status = "completed"
            return
        if self.failed_expert_ids and not self.completed_expert_ids:
            self.status = "failed"
            return
        if self.completed_expert_ids and self.failed_expert_ids:
            self.status = "partial"
            return
        self.status = "assigned"

    def to_sse(self) -> dict[str, Any]:
        return {
            "assigned_expert_ids": list(self.assigned_expert_ids),
            "running_expert_ids": list(self.running_expert_ids),
            "completed_expert_ids": list(self.completed_expert_ids),
            "failed_expert_ids": list(self.failed_expert_ids),
            "status": self.status,
        }


def _normalize_results(expert_results: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in expert_results or []:
        if not isinstance(item, dict):
            continue
        expert_id = str(item.get("expert_id") or item.get("source_expert_id") or "").strip()
        text = str(item.get("text") or item.get("content") or "").strip()
        status = str(item.get("status") or "").strip()
        if not status:
            status = "completed" if text else "failed"
        rows.append(
            {
                "expert_id": expert_id,
                "mention_name": str(item.get("mention_name") or item.get("name") or expert_id).strip(),
                "text": text,
                "status": status,
            }
        )
    return rows


def select_final_reply(
    expert_results: Any,
    *,
    call_tail: CallTail | None = None,
    client: Any = None,
    tail_rules: str = "",
    user_question: str = "",
    collaboration_rules: str = "",
    stream_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Route successful expert answers through the orchestration tail when available.

    - 0 successes → failed (empty text; platform may show a fixed failure message)
    - ≥1 success + call_tail/client → always run tail (single-expert: baseline then passthrough)
    - ≥1 success without tail deps → passthrough (1) or simple_concat (≥2)
    """
    rows = _normalize_results(expert_results)
    successful = [row for row in rows if row["status"] == "completed" and row["text"]]
    if not successful:
        return {"mode": "failed", "text": "", "source_expert_id": ""}

    single = len(successful) == 1
    source_expert_id = successful[0]["expert_id"] if single else ""
    mode = "direct" if single else "merge"

    if call_tail is not None:
        text = str(
            call_tail(
                expert_results=rows,
                tail_rules=tail_rules,
                user_question=user_question,
                collaboration_rules=collaboration_rules,
                client=client,
            )
            or ""
        ).strip()
        # Empty tail output for a single expert: fall back to raw expert text.
        if not text and single:
            text = successful[0]["text"]
        return {"mode": mode, "text": text, "source_expert_id": source_expert_id}

    if client is not None:
        if stream_callback is not None:
            text = run_tail_merge_stream(
                client,
                tail_rules=tail_rules,
                expert_results=rows,
                user_question=user_question,
                collaboration_rules=collaboration_rules,
                on_delta=stream_callback,
                cancel_check=cancel_check,
            )
        else:
            text = run_tail_merge(
                client,
                tail_rules=tail_rules,
                expert_results=rows,
                user_question=user_question,
                collaboration_rules=collaboration_rules,
            )
        if not text and single:
            text = successful[0]["text"]
        return {"mode": mode, "text": text, "source_expert_id": source_expert_id}

    if single:
        return {
            "mode": "direct",
            "text": successful[0]["text"],
            "source_expert_id": source_expert_id,
        }

    return {
        "mode": "merge",
        "text": simple_concat_merge(rows),
        "source_expert_id": "",
    }
