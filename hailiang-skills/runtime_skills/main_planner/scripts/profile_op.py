from __future__ import annotations

import json
import sys


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"ok": False, "error": "empty stdin payload"}, ensure_ascii=False))
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"invalid stdin json: {exc.msg}"},
                ensure_ascii=False,
            )
        )
        return
    print(json.dumps({"ok": True, "payload": payload}, ensure_ascii=False))


if __name__ == "__main__":
    main()
