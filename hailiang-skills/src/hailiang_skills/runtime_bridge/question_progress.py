"""Small, Skill-agnostic question/answer progress ledger.

Native Questionnaire already has stable question ids.  Runtime Skills that
collect information in ordinary prose do not, so they need a conservative
platform-level guard as well.  This module deliberately does not try to
understand business stages: it only remembers questions asked by the active
Skill and marks one answered when the user's message contains one of the
question's explicit alternatives (or a high-confidence yes/no answer).

Semantic dedup (v2): when the same question is rephrased — e.g. "你的英语水平怎么样？"
vs "目前英语程度如何？" — the module uses a character-trigram fingerprint and
content-anchor overlap rather than exact-text hashing so semantically equivalent
questions share an identity and are not asked again.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any


_QUESTION_SPLIT = re.compile(r"[?？]")
_MARKDOWN = re.compile(r"[*_`~#]|【|】")
_PUNCT = re.compile(r"[\s，,。；;：:、（）()\[\]{}\"'""''!！?？…]+")
_OPTION_SPLIT = re.compile(r"\s*(?:还是|或者|或)\s*")
_QUESTION_HINT = re.compile(r"(?:还是|或者|是否|有无|吗$|哪一个|哪个|哪些|什么|多少|多久|多长|几次|怎么|如何)")
_YES = {"是", "是的", "对", "对的", "有", "有的", "会", "会的", "都", "全部", "所有", "一样"}
_NO = {"不是", "不是的", "没有", "没有的", "不会", "不对"}

# Character sequence of length 2+ that carries Chinese content meaning.
_CONTENT_ANCHOR = re.compile(r"[一-鿿]{2,}")

# ── semantic question identity (trigram fingerprint) ──────────────────


def _semantic_question_fingerprint(text: str) -> str:
    """Return a semantic fingerprint that is stable under minor rewording.

    Uses character bigrams for fuzzy surface-form matching and Chinese
    bigram anchors for content-bearing overlap, so "英语水平怎么样" and
    "英语程度如何" share the same fingerprint when their content words
    overlap enough.
    """
    norm = _normalize_text(text)
    bigrams = frozenset(norm[i : i + 2] for i in range(max(0, len(norm) - 1)))
    anchors = frozenset(_CONTENT_ANCHOR.findall(norm))
    combined = "|".join(sorted(bigrams)) + "||" + "|".join(sorted(anchors))
    return "f_" + hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _semantic_overlap(q1_norm: str, q2_norm: str) -> float:
    """Return a 0–1 semantic-overlap score between two normalized questions."""
    if not q1_norm or not q2_norm or len(q1_norm) < 3 or len(q2_norm) < 3:
        return 0.0
    # Character unigrams: catches shared content morphemes like "英语" even
    # when the full phrases differ ("英语水平" vs "英语程度" → {英, 语} shared).
    uni1 = set(q1_norm)
    uni2 = set(q2_norm)
    uni_score = len(uni1 & uni2) / max(1, min(len(uni1), len(uni2)))
    # Character bigrams: "英语" is a shared unit even when adjacent words differ.
    bi1 = {q1_norm[i : i + 2] for i in range(len(q1_norm) - 1)}
    bi2 = {q2_norm[i : i + 2] for i in range(len(q2_norm) - 1)}
    bi_score = len(bi1 & bi2) / max(1, min(len(bi1), len(bi2)))
    # Chinese content anchors (pairwise non-overlapping 2-char windows — so
    # "英语水平" yields {"英语", "水平"}, "英语程度" yields {"英语", "程度"}).
    anchors1 = set(re.findall(r"[一-鿿]{2}", q1_norm))
    anchors2 = set(re.findall(r"[一-鿿]{2}", q2_norm))
    anchor_score = (
        len(anchors1 & anchors2) / max(1, min(len(anchors1), len(anchors2)))
        if anchors1 and anchors2
        else 0.0
    )
    # Weight unigrams highest because Chinese synonyms that share no characters
    # ("水平"/"程度", "怎么样"/"如何") are beyond character n-gram reach;
    # the embed path would be needed for those. Bi‑/anchor weighting rewards
    # multi‑character overlap that is a stronger signal of semantic closeness.
    return 0.40 * uni_score + 0.30 * bi_score + 0.30 * anchor_score


_SEMANTIC_DUP_THRESHOLD = 0.28


def _semantic_question_id(text: str) -> str:
    """Generate a question id that groups semantically similar questions.

    Falls back to exact-match id for very short text where the fingerprint
    is not reliable.
    """
    norm = _normalize_text(text)
    if len(norm) < 6:
        return _question_id(text)
    return _semantic_question_fingerprint(text)


def _normalize_text(value: Any) -> str:
    text = _MARKDOWN.sub("", str(value or "")).lower()
    # 的/地/得 are usually grammatical noise in Chinese answers. Removing
    # them lets "所有的科目" match a question's "所有科目".
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
    listed = re.findall(r"(?:^|\n)\s*(?:-\s+|\d+[.、]\s*)([^\n]+)", question)
    if len(listed) >= 2:
        return [_display_text(item, 120) for item in listed[:8]]
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
    # Only terminated questions are questions; explanatory prose containing
    # an "or" is not a pending choice. Keep listed options with their question.
    for raw in _QUESTION_SPLIT.split(str(text or ""))[:-1]:
        if re.search(r"请确认是否由.*专家.*接管", raw):
            continue
        if not re.search(r"(?:^|\n)\s*(?:-\s+|\d+[.、]\s*)", raw):
            raw = re.split(r"[。！!\n]", raw)[-1]
        candidate = _display_text(raw)
        if len(_normalize_text(candidate)) < 4 or not _QUESTION_HINT.search(candidate):
            continue
        question_id = _semantic_question_id(candidate)
        if any(item["question_id"] == question_id for item in result):
            continue
        result.append({
            "question_id": question_id,
            "text": candidate,
            "options": _options(raw),
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
    ordinal = re.match(r"^(?:选|选择|就选)?第([一二三四五六七八12345678])(?:个|项|条|种)?", normalized_message)
    if ordinal and options:
        index = "一二三四五六七八".find(ordinal[1]) if not ordinal[1].isdigit() else int(ordinal[1]) - 1
        return 0 <= index < len(options)
    if any(option and option in normalized_message for option in normalized_options):
        return True
    # Match an explicit predicate even when the answer negates it or omits
    # modifiers. The predicate must still occur in the user's own words.
    for option in normalized_options:
        predicate = re.sub(r"^(?:已经|曾经)?(?:有过)?(?:比较)?", "", option)
        predicate = re.sub(r"(?:比较|系统性|系统)", "", predicate)
        answer = re.sub(r"(?:比较|系统性|系统)", "", normalized_message)
        if len(predicate) >= 4 and predicate in answer:
            return True
    # Natural Chinese answers often omit harmless qualifiers from an option,
    # such as answering "享受画画这个过程" to "单纯享受画画的过程". Treat a
    # close match as an answer while keeping the threshold high enough that
    # unrelated free text does not close a question.
    if any(
        option
        and len(option) >= 6
        and SequenceMatcher(None, normalized_message, option).ratio() >= 0.72
        for option in normalized_options
    ):
        return True
    # "都这样/全部如此" is a common natural-language answer to a binary
    # scope question even when the model's option text is slightly different.
    if any(
        token in normalized_message
        for token in ("都这样", "全部这样", "所有都", "各科都", "每科都", "全部科目", "各科目", "每个科目")
    ):
        if any(token in "".join(normalized_options) for token in ("所有", "全部", "各科", "每科", "都")):
            return True
    # For a yes/no question only accept a short, unambiguous answer; a long
    # sentence containing "是" is not enough evidence by itself.
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
    questions = ledger.get("asked") or []
    if re.match(r"^(?:选|选择|就选)?第[一二三四五六七八12345678]", _normalize_text(user_message)):
        questions = next(([question] for question in reversed(questions)
            if isinstance(question, dict) and question.get("options")
            and str(question.get("question_id")) not in answered_ids), [])
    for question in questions:
        if not isinstance(question, dict):
            continue
        qid = str(question.get("question_id") or "")
        if not qid or qid in answered_ids or not _answer_matches_question(user_message, question):
            continue
        record = {
            "question_id": qid,
            "question": _display_text(question.get("text")),
            "answer": _display_text(user_message, 240),
            "source_turn_id": state.status_flags.get("active_turn_id", ""),
        }
        ordinal = re.match(r"^(?:选|选择|就选)?第([一二三四五六七八12345678])", _normalize_text(user_message))
        options = question.get("options") or []
        if ordinal and options:
            index = "一二三四五六七八".find(ordinal[1]) if not ordinal[1].isdigit() else int(ordinal[1]) - 1
            if 0 <= index < len(options):
                record["selected_option"] = options[index]
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


# ── cross-skill question ledger aggregation ──


def cross_skill_projection(
    state: Any,
    *,
    exclude_skill_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate asked/answered/unresolved questions across ALL skills.

    When multiple skills run in parallel or sequentially within the same
    session, they may ask semantically overlapping questions (e.g. Skill A
    asks "孩子的英语水平怎么样？" and Skill B later asks "英语程度如何？").
    This function provides a merged view so later skills can detect duplicates
    and avoid re-asking.

    ``exclude_skill_id`` omits a specific skill's ledger from the aggregation
    (e.g. when the caller wants "all other skills' data").

    Returns::

        {
            "answered": [...],       # deduped by question_id, across all skills
            "unresolved": [...],     # deduped by question_id
            "skipped_texts": [...],  # merged _skipped_field_texts
            "skill_ids": [...],      # skills that contributed data
            "source_map": {"qid": "skill_id", ...},  # which skill asked each q
        }
    """
    ledgers = state.status_flags.get("runtime_question_ledger", {})
    if not isinstance(ledgers, dict):
        return {"answered": [], "unresolved": [], "skipped_texts": [], "skill_ids": [], "source_map": {}}

    seen_answered: dict[str, dict[str, Any]] = {}
    seen_unresolved: dict[str, dict[str, Any]] = {}
    seen_skipped: dict[str, str] = {}  # fingerprint -> text
    source_map: dict[str, str] = {}
    skill_ids: list[str] = []

    for sid, ledger in ledgers.items():
        if not isinstance(ledger, dict):
            continue
        if exclude_skill_id and canonical_skill_key(sid) == canonical_skill_key(exclude_skill_id):
            continue
        skill_ids.append(sid)
        for item in ledger.get("answered") or []:
            if not isinstance(item, dict):
                continue
            qid = str(item.get("question_id") or "")
            if not qid:
                continue
            if qid not in seen_answered:
                seen_answered[qid] = dict(item)
                source_map[qid] = sid
        for item in ledger.get("asked") or []:
            if not isinstance(item, dict):
                continue
            qid = str(item.get("question_id") or "")
            if not qid or qid in seen_answered:
                continue
            if qid not in seen_unresolved:
                seen_unresolved[qid] = dict(item)
                source_map[qid] = sid
        for pseudo_text in ledger.get("_skipped_field_texts") or []:
            fp = _semantic_question_fingerprint(str(pseudo_text))
            if fp not in seen_skipped:
                seen_skipped[fp] = str(pseudo_text)

    return {
        "answered": list(seen_answered.values())[-24:],
        "unresolved": list(seen_unresolved.values())[-16:],
        "skipped_texts": list(seen_skipped.values()),
        "skill_ids": skill_ids,
        "source_map": source_map,
    }


def canonical_skill_key(skill_id: str) -> str:
    """Normalize a skill id for cross-skill comparison."""
    return str(skill_id or "").strip().lower()


def merge_question_ledgers(
    state: Any,
    from_skill_id: str,
    to_skill_id: str,
) -> None:
    """Merge question ledger entries from one skill into another.

    Called when transitioning between skills so the new skill inherits
    the previous skill's answered/skipped question history.  This prevents
    the new skill from re-asking questions the user already answered.
    """
    from_id = canonical_skill_key(from_skill_id)
    to_id = canonical_skill_key(to_skill_id)
    if not from_id or not to_id or from_id == to_id:
        return
    ledgers = state.status_flags.setdefault("runtime_question_ledger", {})
    if not isinstance(ledgers, dict):
        return
    source = ledgers.get(from_skill_id, {})
    target = ledgers.setdefault(to_skill_id, {})
    if not isinstance(source, dict) or not isinstance(target, dict):
        return
    for key in ("asked", "answered", "unresolved"):
        records = {
            str(item.get("question_id")): item
            for item in [
                *list(target.get(key) or []),
                *list(source.get(key) or []),
            ]
            if isinstance(item, dict)
        }
        target[key] = list(records.values())[-24:]
    # Merge skipped fields
    target_texts: list[str] = list(target.get("_skipped_field_texts") or [])
    target_fps = set(target.get("_skipped_field_fingerprints") or [])
    for fp in source.get("_skipped_field_fingerprints") or []:
        if fp not in target_fps:
            target_fps.add(fp)
    for text in source.get("_skipped_field_texts") or []:
        if text not in target_texts:
            target_texts.append(text)
    target["_skipped_field_fingerprints"] = list(target_fps)[-24:]
    target["_skipped_field_texts"] = target_texts[-24:]


def apply_semantic_answers(state: Any, skill_id: str, decisions: Any) -> dict[str, Any]:
    """Accept planner judgments only for known questions with current user evidence."""
    ledger = _ledger_for_skill(state, skill_id)
    latest = next((str(item.content) for item in reversed(state.messages) if item.role == "user"), "")
    asked = {str(item.get("question_id")): item for item in ledger["asked"] if isinstance(item, dict)}
    accepted, rejected = [], []
    for decision in decisions if isinstance(decisions, list) else []:
        if not isinstance(decision, dict):
            continue
        qid = str(decision.get("question_id") or "")
        status = str(decision.get("status") or "")
        evidence = str(decision.get("evidence") or "").strip()
        if qid not in asked or status not in {"answered", "partial", "unanswered", "conflict"} or not evidence or evidence not in latest:
            rejected.append(qid)
            continue
        record = {"question_id": qid, "question": asked[qid]["text"], "status": status,
                  "answer": latest, "evidence": evidence,
                  "source_turn_id": state.status_flags.get("active_turn_id", ""),
                  "missing": str(decision.get("missing") or "")[:240]}
        accepted.append(record)
        if status == "answered":
            ledger["answered"] = [item for item in ledger["answered"] if item.get("question_id") != qid] + [record]
        elif status in {"partial", "conflict"}:
            ledger["answered"] = [item for item in ledger["answered"] if item.get("question_id") != qid]
        asked[qid]["answer_status"] = status
        asked[qid]["answer_evidence"] = record
    answered_ids = {item.get("question_id") for item in ledger["answered"]}
    ledger["unresolved"] = [item for item in ledger["asked"] if item.get("question_id") not in answered_ids][-16:]
    ledger["semantic_answer_diagnostics"] = {"accepted": accepted, "rejected_question_ids": rejected}
    return ledger["semantic_answer_diagnostics"]


# ── cross-channel dedup: request_user_input skipped fields → prose ledger ──


def record_skipped_fields_from_tool(state: Any, skill_id: str, tool_result: dict[str, Any]) -> list[str]:
    """Record skipped form fields from a request_user_input tool call into the prose ledger.

    When a user skips fields in a structured form (e.g. "english_level", "target_major"),
    this function stores their semantic fingerprints so that the prose-based
    detect_answered_question_repetition can catch a later natural-language re-ask like
    "你的英语水平怎么样？".

    Returns the list of skipped field keys that were recorded (new ones only).
    """
    skill_id = str(skill_id or "").strip()
    if not skill_id:
        return []
    skipped = tool_result.get("skipped_fields") if isinstance(tool_result, dict) else None
    if not skipped:
        return []
    ledger = _ledger_for_skill(state, skill_id)
    existing = set(ledger.get("_skipped_field_fingerprints") or [])
    new_entries: list[str] = []
    # Store the pseudo-question texts alongside fingerprints so the
    # cross-channel check in detect_answered_question_repetition can
    # use semantic overlap instead of exact fingerprint matching.
    pseudo_texts: list[str] = ledger.get("_skipped_field_texts") or []
    for item in skipped:
        key = str(item.get("key") if isinstance(item, dict) else item)
        label = str(item.get("label", key)) if isinstance(item, dict) else key
        pseudo = label
        fp = _semantic_question_fingerprint(pseudo)
        if fp not in existing:
            existing.add(fp)
            pseudo_texts.append(pseudo)
            new_entries.append(key)
    ledger["_skipped_field_fingerprints"] = list(existing)[-24:]
    ledger["_skipped_field_texts"] = pseudo_texts[-24:]
    if new_entries:
        # Also record a synthetic "answered" entry so that question_ledger_projection
        # surfaces it and the prompt guardrail "do not ask them again" covers it.
        for item in skipped:
            key = str(item.get("key") if isinstance(item, dict) else item)
            label = str(item.get("label", key)) if isinstance(item, dict) else key
            pseudo = label
            fp = _semantic_question_fingerprint(pseudo)
            answered = ledger.get("answered") if isinstance(ledger.get("answered"), list) else []
            if not any(a.get("question_id") == fp for a in answered):
                answered.append({
                    "question_id": fp,
                    "question": pseudo,
                    "answer": "(已跳过)",
                    "source": "request_user_input_skipped",
                })
            ledger["answered"] = answered[-24:]
    return new_entries


def detect_answered_question_repetition(
    reply: str,
    state: Any,
    skill_id: str,
    *,
    cross_skill: bool = False,
) -> list[str]:
    """Return answered question ids that are asked again in a draft reply.

    When ``cross_skill`` is True, checks ALL skills' ledgers — not just
    ``skill_id``.  This catches cases where parallel skills ask semantically
    overlapping questions and later ones should not re-ask what was already
    answered by an earlier skill (even if skipped/empty).
    """
    if cross_skill:
        return _detect_repetition_cross_skill(reply, state, skill_id)
    return _detect_repetition_scoped(reply, state, skill_id)


def _detect_repetition_scoped(reply: str, state: Any, skill_id: str) -> list[str]:
    """Check repetition within the single-skill ledger (original behavior)."""
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
        matched = False
        for qid in answered_by_id:
            original = asked_by_id.get(qid, {})
            original_norm = _normalize_text(original.get("text"))
            # Strategy 1: text substring containment
            if original_norm and (original_norm in draft_norm or draft_norm in original_norm):
                repeated.append(qid)
                matched = True
                continue
            # Strategy 2: option-set overlap (≥2 shared)
            original_options = {_normalize_text(item) for item in original.get("options") or []}
            draft_options = {_normalize_text(item) for item in draft_question.get("options") or []}
            if len(original_options.intersection(draft_options)) >= 2:
                repeated.append(qid)
                matched = True
                continue
            # Strategy 3: semantic trigram overlap
            if (
                original_norm
                and draft_norm
                and _semantic_overlap(original_norm, draft_norm) >= _SEMANTIC_DUP_THRESHOLD
            ):
                repeated.append(qid)
                matched = True
        if not matched:
            skipped_texts = asked.get("_skipped_field_texts") or []
            for pseudo in skipped_texts:
                if (
                    draft_norm
                    and _semantic_overlap(_normalize_text(pseudo), draft_norm) >= _SEMANTIC_DUP_THRESHOLD
                ):
                    repeated.append(draft_question.get("question_id", _semantic_question_fingerprint(pseudo)))
                    break
    return list(dict.fromkeys(repeated))


def _detect_repetition_cross_skill(reply: str, state: Any, current_skill_id: str) -> list[str]:
    """Check repetition across ALL skills' ledgers.

    When Skill B runs after Skill A, this function checks Skill B's draft
    reply against ALL skills' answered questions and skipped fields.  A
    match from any skill blocks the re-ask, even if the answer was empty
    or skipped — the fact that it was already presented is enough.
    """
    cross = cross_skill_projection(state, exclude_skill_id=current_skill_id)
    own_projection = question_ledger_projection(state, current_skill_id)

    # Combine: own answers first (higher priority), then cross-skill answers
    all_answered_by_id: dict[str, dict[str, Any]] = {}
    for item in cross.get("answered") or []:
        if isinstance(item, dict) and item.get("question_id"):
            all_answered_by_id[str(item["question_id"])] = dict(item)
    for item in own_projection.get("answered") or []:
        if isinstance(item, dict) and item.get("question_id"):
            all_answered_by_id[str(item["question_id"])] = dict(item)

    if not all_answered_by_id:
        return []

    reply_questions = extract_questions(reply)
    if not reply_questions:
        return []

    # Build a lookup of original question texts across all skills
    all_ledgers = state.status_flags.get("runtime_question_ledger", {})
    all_asked_by_id: dict[str, dict[str, Any]] = {}
    for sid, ledger in (all_ledgers.items() if isinstance(all_ledgers, dict) else []):
        if not isinstance(ledger, dict):
            continue
        for item in ledger.get("asked") or []:
            if isinstance(item, dict) and item.get("question_id"):
                qid = str(item["question_id"])
                if qid not in all_asked_by_id:
                    all_asked_by_id[qid] = dict(item)

    # Merge skipped field texts across all skills
    all_skipped: list[str] = list(cross.get("skipped_texts") or [])
    own_ledger = (all_ledgers.get(current_skill_id, {}) if isinstance(all_ledgers, dict) else {})
    own_skipped = (own_ledger.get("_skipped_field_texts") or []) if isinstance(own_ledger, dict) else []
    for text in own_skipped:
        if text not in all_skipped:
            all_skipped.append(text)

    repeated: list[str] = []
    for draft_question in reply_questions:
        draft_norm = _normalize_text(draft_question.get("text"))
        matched = False
        for qid, answer_record in all_answered_by_id.items():
            original = all_asked_by_id.get(qid, {})
            original_norm = _normalize_text(
                original.get("text") or answer_record.get("question") or ""
            )
            # Strategy 1: text substring containment
            if original_norm and (original_norm in draft_norm or draft_norm in original_norm):
                repeated.append(qid)
                matched = True
                continue
            # Strategy 2: option-set overlap (≥2 shared)
            original_options = {_normalize_text(item) for item in original.get("options") or []}
            draft_options = {_normalize_text(item) for item in draft_question.get("options") or []}
            if len(original_options.intersection(draft_options)) >= 2:
                repeated.append(qid)
                matched = True
                continue
            # Strategy 3: semantic trigram overlap
            if (
                original_norm
                and draft_norm
                and _semantic_overlap(original_norm, draft_norm) >= _SEMANTIC_DUP_THRESHOLD
            ):
                repeated.append(qid)
                matched = True
        # Cross-check against skipped field texts
        if not matched:
            for pseudo in all_skipped:
                if (
                    draft_norm
                    and _semantic_overlap(_normalize_text(pseudo), draft_norm) >= _SEMANTIC_DUP_THRESHOLD
                ):
                    repeated.append(
                        draft_question.get("question_id", _semantic_question_fingerprint(pseudo))
                    )
                    break
    return list(dict.fromkeys(repeated))
