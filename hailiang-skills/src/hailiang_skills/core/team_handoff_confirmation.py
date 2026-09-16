"""Pure, conservative recognition for text confirmations of team handoffs.

The client deliberately sends a normal ``chat`` turn for these messages.  It
must not have to infer card state or expert ids; that remains authoritative on
the server.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4


_ACKNOWLEDGEMENTS = {
    "继续", "请继续", "好的", "好", "可以", "行", "没问题", "就按这个来",
    "按这个来", "麻烦你", "麻烦了", "拜托了", "同意", "同意的", "确认", "请帮我转交", "好的请继续",
}
_NEGATIVE_OR_UNCERTAIN = ("不", "先不用", "等等", "等一下", "考虑", "再想想", "但是", "可是", "还是")


def is_conservative_handoff_acknowledgement(text: str) -> bool:
    """Return true only for a short, unambiguous acknowledgement.

    Exact matching intentionally rejects messages which add a new question or
    any hesitation/negation.  This makes a mistaken automatic transfer much
    less likely than a missed convenience transfer.
    """
    normalized = re.sub(r"[\s\u3000,，。.!！?？、;；:：'\"“”‘’（）()\-—_]+", "", str(text or "").strip())
    if not normalized or len(normalized) > 24:
        return False
    if any(token in normalized for token in _NEGATIVE_OR_UNCERTAIN):
        return False
    return normalized in _ACKNOWLEDGEMENTS


def active_handoff_decision(context: Any, *, team_id: str, text: str) -> dict[str, Any]:
    """Classify an acknowledgement against an attached card or saved intent.

    ``kind`` is one of ``none``, ``single_card``, ``single_recovery`` or
    ``multiple``.  This is intentionally structural: it never tries to infer
    an expert from assistant prose.
    """
    if not is_conservative_handoff_acknowledgement(text):
        return {"kind": "none"}
    for message in reversed(getattr(context, "messages", []) or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        handoff = message.get("team_handoff")
        if not isinstance(handoff, dict):
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            handoff = metadata.get("team_handoff")
        interactions = message.get("interactions") if isinstance(message.get("interactions"), dict) else {}
        interaction = interactions.get("team_handoff") if isinstance(interactions, dict) else None
        if not isinstance(handoff, dict) or str(handoff.get("team_id") or "") != team_id:
            continue
        if str(handoff.get("status") or "active") != "active" or not isinstance(interaction, dict) or interaction.get("status") != "active":
            continue
        candidates = [item for item in handoff.get("candidates", []) if isinstance(item, dict) and item.get("expert_id")]
        return {"kind": "single_card" if len(candidates) == 1 else "multiple", "handoff": handoff, "source": message, "candidates": candidates}
    meta = getattr(context, "session_meta", {}) if isinstance(getattr(context, "session_meta", {}), dict) else {}
    intent = meta.get("pending_team_handoff_intent") or meta.get("pending_team_handoff")
    if not isinstance(intent, dict) or str(intent.get("team_id") or "") != team_id or str(intent.get("status") or "active") != "active":
        return {"kind": "none"}
    candidates = [item for item in intent.get("candidates", []) if isinstance(item, dict) and item.get("expert_id")]
    return {"kind": "single_recovery" if len(candidates) == 1 else "multiple", "handoff": intent, "source": None, "candidates": candidates}


def block_text_handoff_confirmation(context: Any, decision: dict[str, Any]) -> dict[str, Any] | None:
    """Expire the acknowledged card and issue a fresh card without switching.

    Text acknowledgements are deliberately non-authoritative.  The returned
    card is a new interaction so a delayed click on the old card cannot mutate
    the active expert.
    """
    handoff = decision.get("handoff") if isinstance(decision, dict) else None
    source = decision.get("source") if isinstance(decision, dict) else None
    if not isinstance(handoff, dict):
        return None
    if isinstance(source, dict):
        interactions = source.get("interactions")
        interaction = interactions.get("team_handoff") if isinstance(interactions, dict) else None
        if isinstance(interaction, dict):
            interaction["status"] = "expired"
        source_handoff = source.get("team_handoff")
        if isinstance(source_handoff, dict):
            source_handoff["status"] = "expired"
        metadata = source.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("team_handoff"), dict):
            metadata["team_handoff"]["status"] = "expired"
    fresh = dict(handoff)
    fresh["previous_handoff_id"] = str(handoff.get("handoff_id") or "")
    fresh["handoff_id"] = f"handoff_{uuid4().hex[:16]}"
    fresh["interaction_id"] = "team_handoff"
    fresh["source_message_id"] = None
    fresh["status"] = "active"
    fresh["confirmation_mode"] = "card_only"
    fresh["text_confirmation_action"] = "blocked"
    fresh.pop("selected_target_expert_id", None)
    fresh["presentation_status"] = "pending"
    context.session_meta["pending_team_handoff"] = fresh
    context.session_meta["pending_team_handoff_intent"] = fresh
    return fresh
