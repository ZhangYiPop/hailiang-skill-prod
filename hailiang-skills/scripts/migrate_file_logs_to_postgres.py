#!/usr/bin/env python3
"""Import legacy file-backed sessions, profiles and Facts into PostgreSQL.

Run only after ``alembic upgrade head`` and with HAILIANG_STORAGE_BACKEND=postgres.
The default is non-destructive: rows already present in PostgreSQL are skipped.
Use --dry-run first; --overwrite is reserved for a controlled re-import.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hailiang_skills.core.session_logging import SESSION_LOG_ROOT, USER_LOG_ROOT
from hailiang_skills.storage.database import ProfileRow, SessionRow
from hailiang_skills.storage.event_store import append_events, configure_event_store
from hailiang_skills.storage.factory import build_storage_from_env
from hailiang_skills.storage.repositories.file_session_repo import load_session_context_from_snapshot
from hailiang_skills.storage.repositories.profile_repo import FileBackedProfileRepository
from hailiang_skills.storage.repositories.user_fact_repo import FileBackedUserFactRepository
from hailiang_skills.storage.repositories.postgres_repo import _facts_to_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="only report import counts")
    parser.add_argument("--overwrite", action="store_true", help="replace existing PostgreSQL session/profile rows")
    return parser.parse_args()


def load_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event_id"):
            events.append(payload)
    return events


def main() -> int:
    args = parse_args()
    if os.getenv("HAILIANG_STORAGE_BACKEND", "").lower() != "postgres":
        raise SystemExit("请先设置 HAILIANG_STORAGE_BACKEND=postgres")
    storage = build_storage_from_env()
    if storage.backend != "postgres" or storage.session_factory is None:
        raise SystemExit("PostgreSQL 存储未配置")
    if not storage.ready():
        raise SystemExit("PostgreSQL 不可连接；请检查 HAILIANG_DATABASE_URL")

    configure_event_store(storage.session_factory)
    sessions_created = sessions_skipped = profiles_written = facts_written = events_written = 0
    source_user_facts = FileBackedUserFactRepository()
    source_profiles = FileBackedProfileRepository()

    for user_dir in sorted(USER_LOG_ROOT.glob("*")) if USER_LOG_ROOT.exists() else []:
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        shared_facts = source_user_facts.get(user_id)
        if shared_facts.facts:
            facts_written += 1
            if not args.dry_run:
                storage.user_fact_repository.save(user_id, shared_facts)
        for profile in source_profiles.list_profiles(user_id):
            profile_id = str(profile.get("profile_id") or "")
            if not profile_id:
                continue
            profiles_written += 1
            if args.dry_run:
                continue
            profile_facts = source_profiles.get_profile_facts(user_id, profile_id)
            with storage.session_factory.begin() as db:
                existing = db.get(ProfileRow, profile_id)
                if existing is not None and not args.overwrite:
                    continue
                db.merge(ProfileRow(
                    profile_id=profile_id,
                    user_id=user_id,
                    payload=dict(profile),
                    facts=_facts_to_payload(profile_facts),
                    version=(existing.version + 1 if existing else 1),
                ))

    for session_dir in sorted(SESSION_LOG_ROOT.iterdir()) if SESSION_LOG_ROOT.exists() else []:
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        snapshot_path = session_dir / "snapshot.json"
        if snapshot_path.is_file():
            try:
                context = load_session_context_from_snapshot(session_id)
            except (KeyError, json.JSONDecodeError, OSError) as exc:
                print(f"WARN skip invalid snapshot {snapshot_path}: {exc}")
            else:
                with storage.session_factory() as db:
                    existing = db.get(SessionRow, session_id)
                if existing is not None and not args.overwrite:
                    sessions_skipped += 1
                else:
                    sessions_created += 1
                    if not args.dry_run:
                        if existing is None:
                            storage.session_repository.create(context)
                        else:
                            context.session_meta["_storage_version"] = existing.version
                            storage.session_repository.save(context)
        else:
            print(f"WARN session {session_id} has no snapshot; importing events only")
        events = load_events(session_dir / "events.jsonl")
        events_written += len(events)
        if events and not args.dry_run:
            append_events(session_id, events)

    print(
        "file-log migration complete: "
        f"sessions_imported={sessions_created}, sessions_skipped={sessions_skipped}, "
        f"profiles_seen={profiles_written}, shared_fact_files={facts_written}, events_seen={events_written}, "
        f"dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
