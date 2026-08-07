#!/usr/bin/env python3
"""Search SSE recording and session event logs by session_id/run_id/source_message_id."""

import argparse
import json
from pathlib import Path
DEFAULT_KEYWORDS = [
    "skill_transition_requested",
    "skill_transition",
    "route_suggestion",
    "completed",
    "failed",
    "blocked",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("test", "prod"), required=True, help="目标部署环境")
    parser.add_argument("--log-dir", help="覆盖日志根目录，默认 /var/lib/hailiang-skills/<env>/logs")
    parser.add_argument("--session-id", help="会话 ID，例如 193477649811558401")
    parser.add_argument("--run-id", help="单次请求 run ID，例如 9694...jsonl 对应的文件名")
    parser.add_argument("--source-message-id", help="推荐卡片来源消息 ID，例如 msg_xxx")
    parser.add_argument("--target-skill-id", help="目标 Skill ID，例如 multi_path_planning")
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="额外关键词，可多次传入；不传时会使用常见技能转场关键词",
    )
    parser.add_argument(
        "--show-head",
        type=int,
        default=0,
        help="除搜索结果外，额外打印每个目标文件前 N 行，默认 0",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="打印匹配到的原始整行 JSON，而不是摘要",
    )
    parser.add_argument(
        "--all-lines",
        action="store_true",
        help="不按关键词过滤，直接打印目标文件全部行的摘要",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=200,
        help="最多展示多少条匹配结果，默认 200",
    )
    return parser


def _resolve_project_root():
    return Path(__file__).resolve().parents[1]


def _candidate_files(log_root, session_id, run_id):
    sse_root = log_root / "sse_recording" / "sessions"
    session_log_root = log_root / "sessions"
    files = []

    if session_id and run_id:
        files.append(sse_root / session_id / "sse" / f"{run_id}.jsonl")
    elif run_id:
        files.extend(sorted(sse_root.glob(f"*/sse/{run_id}.jsonl")))

    if session_id:
        files.append(sse_root / session_id / "sse" / "session_stream.jsonl")
        files.append(session_log_root / session_id / "events.jsonl")

    deduped = []
    seen = set()
    for path in files:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _build_terms(args):
    terms = []
    for value in (
        args.session_id,
        args.run_id,
        args.source_message_id,
        args.target_skill_id,
    ):
        if value:
            terms.append(str(value))
    terms.extend(str(item) for item in args.keyword if str(item).strip())
    if not terms and not args.all_lines:
        terms.extend(DEFAULT_KEYWORDS)
    return terms


def _line_matches(line, terms, all_lines=False):
    if all_lines:
        return True
    if not terms:
        return True
    return any(term in line for term in terms)


def _load_json(line):
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _summary_from_record(record):
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    skill_transition = payload.get("skill_transition")
    skill_transition = skill_transition if isinstance(skill_transition, dict) else {}

    parts = [
        f"timestamp={record.get('timestamp') or record.get('timestamp_utc') or ''}",
        f"wire_event={record.get('wire_event') or ''}",
        f"internal_event={record.get('internal_event') or ''}",
        f"seq={record.get('seq') or ''}",
        f"message_id={record.get('message_id') or payload.get('message_id') or ''}",
        f"status={payload.get('status') or ''}",
    ]

    if skill_transition:
        parts.append(
            "skill_transition="
            f"{skill_transition.get('action') or ''}:"
            f"{skill_transition.get('from_skill_id') or ''}->"
            f"{skill_transition.get('to_skill_id') or ''}"
        )

    if isinstance(record.get("payload"), dict) and record.get("internal_event") == "skill_transition_requested":
        transition_payload = record["payload"]
        parts.append(f"source={transition_payload.get('source') or ''}")
        parts.append(f"to_skill_id={transition_payload.get('to_skill_id') or ''}")
        parts.append(f"context_source_message_id={transition_payload.get('context_source_message_id') or ''}")

    assistant = payload.get("assistant")
    assistant = assistant if isinstance(assistant, dict) else {}
    assistant_text = str(assistant.get("content") or "").strip().replace("\n", " ")
    if assistant_text:
        parts.append(f"assistant={assistant_text[:120]}")

    return " | ".join(part for part in parts if part and not part.endswith("="))


def _print_head(path, count):
    if count <= 0 or not path.is_file():
        return
    print(f"\n--- HEAD {count}: {path} ---")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle, start=1):
            if index > count:
                break
            print(f"{index}: {line.rstrip()}")


def _search_file(path, terms, raw=False, all_lines=False, remaining=0):
    if remaining <= 0:
        return 0
    if not path.is_file():
        print(f"[MISS] {path}")
        return 0

    match_count = 0
    print(f"\n=== SEARCH {path} ===")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if match_count >= remaining:
                break
            stripped = line.rstrip("\n")
            if not _line_matches(stripped, terms, all_lines):
                continue
            match_count += 1
            if raw:
                print(f"{line_no}: {stripped}")
                continue
            record = _load_json(stripped)
            if record is None:
                print(f"{line_no}: {stripped[:240]}")
                continue
            print(f"{line_no}: {_summary_from_record(record)}")
    if match_count == 0:
        print("(no matches)")
    return match_count


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.session_id and not args.run_id:
        parser.error("至少提供 --session-id 或 --run-id 之一")

    project_root = _resolve_project_root()
    log_root = Path(args.log_dir) if args.log_dir else Path("/var/lib/hailiang-skills") / args.env / "logs"
    files = _candidate_files(
        log_root,
        str(args.session_id or "").strip(),
        str(args.run_id or "").strip(),
    )
    terms = _build_terms(args)

    print(f"project_root={project_root}")
    print(f"environment={args.env} log_root={log_root}")
    print(f"session_id={args.session_id or ''}")
    print(f"run_id={args.run_id or ''}")
    print(f"source_message_id={args.source_message_id or ''}")
    print(f"target_skill_id={args.target_skill_id or ''}")
    print(f"terms={terms}")

    if not files:
        print("没有推导出可搜索的目标文件。")
        return 1

    print("\n=== TARGET FILES ===")
    for path in files:
        state = "FOUND" if path.is_file() else "MISS"
        print(f"[{state}] {path}")
        _print_head(path, args.show_head)

    total_matches = 0
    remaining = max(int(args.max_matches), 1)
    for path in files:
        found = _search_file(
            path,
            terms=terms,
            raw=bool(args.raw),
            all_lines=bool(args.all_lines),
            remaining=remaining,
        )
        total_matches += found
        remaining -= found
        if remaining <= 0:
            print(f"\n达到 max_matches={args.max_matches}，停止继续输出。")
            break

    print(f"\nmatch_count={total_matches}")
    return 0 if total_matches > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
