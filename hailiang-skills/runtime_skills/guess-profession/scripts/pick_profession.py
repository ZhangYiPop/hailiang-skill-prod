# -*- coding: utf-8 -*-
"""Return the daily profession and the game round for the Guess Profession skill.

The platform executes this file once for every user message and sends a JSON
object through stdin. Only ``messages`` is needed for the round state; no
session id, user id, environment variable, or writable state file is used.

With no stdin (for example, the platform's script smoke test), the script
still returns the daily profession. It cannot infer a game round without a
user message/history, so it reports ``game_started: false`` and ``round: 0``.
"""
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone


HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "..", "professions.json")
SHANGHAI = timezone(timedelta(hours=8))

# Keep this list deliberately strict: a reset must come from an explicit
# command, not from a sentence that merely mentions the word "start".
START_WORDS = {"开始", "start", "来一局", "再来一局", "再来一轮"}
REMIND_ROUNDS = {4, 7, 10}
RESCUE_ROUND = 11


def load_pool():
    """Load and validate the profession list bundled with the skill."""
    with open(POOL_PATH, encoding="utf-8") as file:
        data = json.load(file)
    pool = data if isinstance(data, list) else data.get("professions", [])
    if not isinstance(pool, list) or not pool:
        raise ValueError("professions.json does not contain a non-empty profession list")
    return pool


def date_key(now=None):
    """Return today's China calendar date as YYYY-MM-DD."""
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def pick_daily_profession(today):
    """Pick one uniformly distributed, date-stable profession.

    A fresh process has no memory. Hashing the date creates a reproducible
    pseudo-random draw, which is the only way to keep one profession stable
    for the entire day without persisting a state file.
    """
    digest = hashlib.sha256(f"guess-profession-v010|{today}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest, "big"))
    return rng.choice(load_pool())


def normalize(text):
    if text is None:
        return ""
    value = str(text).strip().lower()
    value = value.strip(" \t\r\n。！？.!?，,~～、；;：:")
    return value.strip("吧啦呀嘛哈哦呢呐阿呗咯哒").strip()


def is_start(text):
    value = normalize(text)
    if value in START_WORDS:
        return True
    # Accept concise variants such as “开始游戏” and “再来一轮吧”, but do not
    # treat a longer chat sentence as a reset command.
    return any(value.startswith(word) and len(value) - len(word) <= 2 for word in START_WORDS)


def extract_text(message):
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return "" if content is None else str(content)


def messages_from(payload):
    """Use history when available; otherwise use the single latest message."""
    messages = payload.get("messages")
    if isinstance(messages, list):
        return messages
    latest = payload.get("latest_user_message") or payload.get("query") or ""
    return [{"role": "user", "content": latest}] if latest else []


def compute_round(messages):
    """Return rounds after the most recent explicit reset, or None if idle."""
    last_start = -1
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user" and is_start(extract_text(message)):
            last_start = index
    if last_start < 0:
        return None
    return sum(
        1
        for message in messages[last_start + 1 :]
        if isinstance(message, dict) and message.get("role") == "user"
    )


def phase_for(round_no, started):
    if not started:
        return "idle"
    if round_no >= RESCUE_ROUND:
        return "rescue"
    if round_no in REMIND_ROUNDS:
        return "remind"
    return "play"


def build_result(payload=None):
    """Build the JSON result; also usable by function-style script runners."""
    payload = payload if isinstance(payload, dict) else {}
    today = date_key()
    round_no = compute_round(messages_from(payload))
    started = round_no is not None
    return {
        "ok": True,
        "date_key": today,
        "current_profession": pick_daily_profession(today),
        "round": round_no if started else 0,
        "phase": phase_for(round_no if started else 0, started),
        "game_started": started,
        "selection_method": "date_stable_pseudorandom",
    }


def main(payload=None):
    """Return a result for function runners; print it when run as a script."""
    if payload is not None:
        return build_result(payload)

    raw = ""
    try:
        raw = sys.stdin.read().strip()
    except (OSError, ValueError):
        pass
    if not raw:
        return build_result({})
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "code": "invalid_json", "message": f"无法解析输入 JSON：{exc}"}
    return build_result(parsed)


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)
