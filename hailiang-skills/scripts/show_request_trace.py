#!/usr/bin/env python3
"""Display local span timings for one trace/request/session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="trace_id、request_id 或 session_id")
    parser.add_argument("--env", choices=("test", "prod"), required=True, help="目标部署环境")
    parser.add_argument("--log-dir", help="覆盖日志根目录，默认 /var/lib/hailiang-skills/<env>/logs")
    args = parser.parse_args()
    root = Path(args.log_dir) / "telemetry" if args.log_dir else Path("/var/lib/hailiang-skills") / args.env / "logs" / "telemetry"
    matches: list[dict] = []
    if not root.exists():
        print(f"没有找到 {root}，请先执行一次请求。")
        return 1
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.query in {
                str(item.get("trace_id") or ""),
                str(item.get("request_id") or ""),
                str(item.get("session_id") or ""),
            }:
                matches.append(item)
    if not matches:
        print(f"没有找到请求：{args.query}")
        return 1
    matches.sort(key=lambda item: str(item.get("start_at") or ""))
    first = matches[0]
    print(f"trace_id={first.get('trace_id')} request_id={first.get('request_id')} session_id={first.get('session_id')}")
    print("start_at\tduration_ms\toutcome\tnode\tskill_id")
    for item in matches:
        print(f"{item.get('start_at','')}\t{item.get('duration_ms','')}\t{item.get('outcome','')}\t{item.get('node','')}\t{item.get('skill_id','')}")
    print(f"span_count={len(matches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
