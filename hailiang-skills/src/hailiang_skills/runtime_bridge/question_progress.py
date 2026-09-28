"""Small, Skill-agnostic question/answer progress ledger.

Native Questionnaire already has stable question ids.  Runtime Skills that
collect information in ordinary prose do not, so they need a conservative
platform-level guard as well.  This module deliberately does not try to
understand business stages: it only remembers questions asked by the active
Skill and marks one answered when the user's message contains one of the
question's explicit alternatives (or a high-confidence yes/no answer).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


_QUESTION_SPLIT = re.compile(r"[?？]")
_MARKDOWN = re.compile(r"[*_`~#]|【|】")
_PUNCT = re.compile(r"[\s，,。；;：:、（）()\[\]{}\"'“”‘’!！?？…]+")
_OPTION_SPLIT = re.compile(r"\s*(?:还是|或者|或)\s*")
_QUESTION_HINT = re.compile(r"(?:还是|或者|是否|有无|吗$|哪一个|哪个|哪些|什么|多久|多长|几次|怎么|如何)")
_YES = {"是", "是的", "对", "对的", "有", "有的", "会", "会的", "都", "全部", "所有", "一样"}
_NO = {"不是", "不是的", "没有", "没有的", "不会", "不对"}


def _normalize_text(value: Any) -> str:
    text = _MARKDOWN.sub("", str(value or "")).lower()
    # 的/地/得 are usually grammatical noise in Chinese answers. Removing
    # them lets “所有的科目” match a question's “所有科目”.
    text = re.sub(r"[的地得]", "", text)
    return _PUNCT.sub("", text)


def same_user_message_streak(state: Any, user_message: str | None = None) -> int:
    """Return the number of trailing user turns with the same normalized text.

    A repeated user message is not itself a failure.  People commonly resend a
    question when the previous answer was missed or unclear.  The runtime uses
    this small, conversation-only signal to allow one retry turn while still
    being able to stop an actual repetition loop afterwards.  Assistant turns
    do not break the streak; a different user message does.
    """
    user_items = [
        item for item in (getattr(state, "messages", []) or [])
        if getattr(item, "role", "") == "user"
    ]
    if not user_items:
        return 0
    target = _normalize_text(
        user_message if user_message is not None else getattr(user_items[-1], "content", "")
    )
    if len(target) < 2:
        return 0
    streak = 0
    for item in reversed(user_items):
        if _normalize_text(getattr(item, "content", "")) != target:
            break
        streak += 1
    return streak


def _display_text(value: Any, limit: int = 320) -> str:
    return " ".join(str(value or "").split())[:limit]


def _question_id(text: str) -> str:
    return "q_" + hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()[:16]


def _options(question: str) -> list[str]:
    parts = _OPTION_SPLIT.split(question)
    if len(parts) < 2:
        return []
    left_source = parts[0].rstrip("，,：: ")
    left = re.split(r"[，,：:]", left_source)[-1].strip()
    right = parts[1].strip()
    values = [left, right]
    return [_display_text(item, 120) for item in values if len(_normalize_text(item)) >= 2]


def extract_questions(text: str) -> list[dict[str, Any]]:
    """Extract question-shaped prose without imposing a Skill vocabulary."""
    result: list[dict[str, Any]] = []
    for raw in _QUESTION_SPLIT.split(str(text or "")):
        candidate = _display_text(raw)
        if len(_normalize_text(candidate)) < 4 or not _QUESTION_HINT.search(candidate):
            continue
        question_id = _question_id(candidate)
        if any(item["question_id"] == question_id for item in result):
            continue
        result.append({
            "question_id": question_id,
            "text": candidate,
            "options": _options(candidate),
        })
    return result[:8]


def _answer_matches_question(message: str, question: dict[str, Any]) -> bool:
    if str(message or "").strip().endswith(("?", "？")):
        return False
    normalized_message = _normalize_text(message)
    if not normalized_message:
        return False
    options = [str(item) for item in question.get("options") or []]
    normalized_options = [_normalize_text(item) for item in options]
    if any(option and option in normalized_message for option in normalized_options):
        return True
    # “都这样/全部如此” is a common natural-language answer to a binary
    # scope question even when the model's option text is slightly different.
    if any(
        token in normalized_message
        for token in ("都这样", "全部这样", "所有都", "各科都", "每科都", "全部科目", "各科目", "每个科目")
    ):
        if any(token in "".join(normalized_options) for token in ("所有", "全部", "各科", "每科", "都")):
            return True
    # For a yes/no question only accept a short, unambiguous answer; a long
    # sentence containing “是” is not enough evidence by itself.
    compact = normalized_message.strip()
    if _QUESTION_HINT.search(str(question.get("text") or "")) and not options:
        if compact in _YES or compact in _NO:
            return True
    return False


def _ledger_for_skill(state: Any, skill_id: str) -> dict[str, Any]:
    all_ledgers = state.status_flags.setdefault("runtime_question_ledger", {})
    if not isinstance(all_ledgers, dict):
        all_ledgers = {}
        state.status_flags["runtime_question_ledger"] = all_ledgers
    ledger = all_ledgers.setdefault(skill_id, {})
    if not isinstance(ledger, dict):
        ledger = {}
        all_ledgers[skill_id] = ledger
    ledger.setdefault("asked", [])
    ledger.setdefault("answered", [])
    return ledger


def _bootstrap_last_question(state: Any, skill_id: str, ledger: dict[str, Any]) -> None:
    if ledger.get("asked"):
        return
    for item in reversed(getattr(state, "messages", []) or []):
        if getattr(item, "role", "") != "assistant":
            continue
        questions = extract_questions(getattr(item, "content", ""))
        if questions:
            ledger["asked"] = questions
            return


def reconcile_user_answer(state: Any, skill_id: str, user_message: str) -> dict[str, Any]:
    """Mark explicit answers before the Skill planner sees the next turn."""
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return {"skill_id": "", "answered": [], "unresolved": [], "changed": False}
    ledger = _ledger_for_skill(state, skill_id)
    _bootstrap_last_question(state, skill_id, ledger)
    answered = ledger.get("answered") if isinstance(ledger.get("answered"), list) else []
    answered_ids = {str(item.get("question_id")) for item in answered if isinstance(item, dict)}
    newly_answered: list[dict[str, Any]] = []
    for question in ledger.get("asked") or []:
        if not isinstance(question, dict):
            continue
        qid = str(question.get("question_id") or "")
        if not qid or qid in answered_ids or not _answer_matches_question(user_message, question):
            continue
        record = {
            "question_id": qid,
            "question": _display_text(question.get("text")),
            "answer": _display_text(user_message, 240),
        }
        answered.append(record)
        newly_answered.append(record)
        answered_ids.add(qid)
    ledger["answered"] = answered[-24:]
    unresolved = [
        item for item in ledger.get("asked") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") not in answered_ids
    ]
    ledger["unresolved"] = unresolved[:16]
    ledger["last_reconciled"] = {
        "answered_question_ids": [str(item["question_id"]) for item in newly_answered],
        "user_message_chars": len(str(user_message or "")),
    }
    return {
        "skill_id": skill_id,
        "answered": newly_answered,
        "unresolved": unresolved[:16],
        "changed": bool(newly_answered),
    }


def record_assistant_questions(state: Any, skill_id: str, assistant_message: str) -> dict[str, Any]:
    """Persist questions from the latest assistant response for next turn."""
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return {"skill_id": "", "questions": []}
    ledger = _ledger_for_skill(state, skill_id)
    questions = extract_questions(assistant_message)
    if not questions:
        return {"skill_id": skill_id, "questions": []}
    existing = {
        str(item.get("question_id")): item
        for item in ledger.get("asked") or []
        if isinstance(item, dict) and item.get("question_id")
    }
    new_questions: list[dict[str, Any]] = []
    for question in questions:
        if str(question["question_id"]) not in existing:
            new_questions.append(question)
        existing[str(question["question_id"])] = question
    ledger["asked"] = list(existing.values())[-24:]
    answered_ids = {
        str(item.get("question_id"))
        for item in ledger.get("answered") or []
        if isinstance(item, dict)
    }
    ledger["unresolved"] = [
        item for item in ledger["asked"]
        if str(item.get("question_id")) not in answered_ids
    ][-16:]
    return {"skill_id": skill_id, "questions": new_questions}


def question_ledger_projection(state: Any, skill_id: str) -> dict[str, Any]:
    ledgers = state.status_flags.get("runtime_question_ledger", {})
    ledger = ledgers.get(skill_id, {}) if isinstance(ledgers, dict) else {}
    if not isinstance(ledger, dict):
        return {"answered": [], "unresolved": []}
    return {
        "answered": list(ledger.get("answered") or [])[-12:],
        "unresolved": list(ledger.get("unresolved") or [])[-8:],
    }


def detect_answered_question_repetition(reply: str, state: Any, skill_id: str) -> list[str]:
    """Return answered question ids that are asked again in a draft reply."""
    projection = question_ledger_projection(state, skill_id)
    if not projection["answered"]:
        return []
    reply_questions = extract_questions(reply)
    if not reply_questions:
        return []
    answered_by_id = {
        str(item.get("question_id")): item
        for item in projection["answered"]
        if isinstance(item, dict)
    }
    # Match by normalized question wording first. The fallback compares the
    # alternatives so minor wording changes still cannot reopen a resolved
    # “A or B?” question.
    asked = state.status_flags.get("runtime_question_ledger", {}).get(skill_id, {})
    asked_items = asked.get("asked", []) if isinstance(asked, dict) else []
    asked_by_id = {
        str(item.get("question_id")): item
        for item in asked_items
        if isinstance(item, dict)
    }
    repeated: list[str] = []
    for draft_question in reply_questions:
        draft_norm = _normalize_text(draft_question.get("text"))
        for qid in answered_by_id:
            original = asked_by_id.get(qid, {})
            original_norm = _normalize_text(original.get("text"))
            if original_norm and (original_norm in draft_norm or draft_norm in original_norm):
                repeated.append(qid)
                continue
            original_options = {_normalize_text(item) for item in original.get("options") or []}
            draft_options = {_normalize_text(item) for item in draft_question.get("options") or []}
            if len(original_options.intersection(draft_options)) >= 2:
                repeated.append(qid)
    return list(dict.fromkeys(repeated))
