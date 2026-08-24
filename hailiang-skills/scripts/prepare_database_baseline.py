#!/usr/bin/env python3
"""Guard the clean multi-profile Alembic baseline against legacy databases.

The 0001_multi_profile_runtime migration is intentionally a new baseline. It
must never be stamped onto, or migrated over, a database whose alembic_version
belongs to the previous schema lineage.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool


EXPECTED_REVISION = "0001_multi_profile_runtime"
DEFAULT_LOCAL_SUFFIX = "_multi_profile_v1"


@dataclass(frozen=True, slots=True)
class DatabaseState:
    revisions: tuple[str, ...]
    tables: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.tables

    @property
    def compatible(self) -> bool:
        return self.empty or self.revisions == (EXPECTED_REVISION,)


def _inspect_database(url: URL) -> DatabaseState:
    engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    text(
                        "SELECT tablename FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'public' ORDER BY tablename"
                    )
                )
            )
            revisions: tuple[str, ...] = ()
            if "alembic_version" in tables:
                revisions = tuple(
                    sorted(
                        str(row[0])
                        for row in connection.execute(text("SELECT version_num FROM alembic_version"))
                    )
                )
            return DatabaseState(revisions=revisions, tables=tables)
    finally:
        engine.dispose()


def _derived_local_url(url: URL, suffix: str) -> URL:
    database = str(url.database or "").strip()
    if not database:
        raise SystemExit("HAILIANG_DATABASE_URL 必须包含数据库名")
    candidate = database if database.endswith(suffix) else f"{database}{suffix}"
    if len(candidate) > 63 or not re.fullmatch(r"[A-Za-z0-9_]+", candidate):
        raise SystemExit(f"无法生成安全的本地数据库名：{candidate}")
    return url.set(database=candidate)


def _ensure_local_database(url: URL) -> None:
    database = str(url.database or "")
    admin_url = url.set(database="postgres")
    engine = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database"),
                {"database": database},
            ).scalar()
            if exists:
                return
            quoted = connection.dialect.identifier_preparer.quote(database)
            connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
            print(f"ℹ️  已创建新的本地多孩子数据库：{database}", file=sys.stderr)
    finally:
        engine.dispose()


def _state_label(state: DatabaseState) -> str:
    if state.empty:
        return "empty"
    if state.revisions:
        return ",".join(state.revisions)
    return "unversioned-nonempty"


def resolve_database_url(raw_url: str, *, mode: str, suffix: str) -> str:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise SystemExit("多孩子生产基线仅支持 PostgreSQL")
    try:
        state = _inspect_database(url)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "3D000":
            raise
        if mode == "strict":
            raise SystemExit(
                f"目标数据库不存在：{url.database}。请由 DBA 创建空数据库后再部署。"
            ) from exc
        _ensure_local_database(url)
        state = _inspect_database(url)
    if state.compatible:
        print(
            f"ℹ️  数据库基线检查通过：{url.database} ({_state_label(state)})",
            file=sys.stderr,
        )
        return url.render_as_string(hide_password=False)

    if mode == "strict":
        raise SystemExit(
            "数据库基线不兼容："
            f"{url.database} 当前 revision={_state_label(state)}，"
            f"本版本要求空数据库或 revision={EXPECTED_REVISION}。"
            "请保留旧库，并为本版本创建一个全新的数据库后修改 HAILIANG_DATABASE_URL。"
        )

    target_url = _derived_local_url(url, suffix)
    _ensure_local_database(target_url)
    target_state = _inspect_database(target_url)
    if not target_state.compatible:
        raise SystemExit(
            f"自动选择的本地数据库 {target_url.database} 也不兼容："
            f"revision={_state_label(target_state)}。请显式指定一个空数据库。"
        )
    print(
        f"⚠️  旧数据库 {url.database} 保持不变；本次切换到 {target_url.database}。",
        file=sys.stderr,
    )
    return target_url.render_as_string(hide_password=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("local-auto", "strict"), default="strict")
    parser.add_argument("--local-suffix", default=DEFAULT_LOCAL_SUFFIX)
    args = parser.parse_args()
    raw_url = os.getenv("HAILIANG_DATABASE_URL", "").strip()
    if not raw_url:
        raise SystemExit("缺少 HAILIANG_DATABASE_URL")
    print(resolve_database_url(raw_url, mode=args.mode, suffix=args.local_suffix))


if __name__ == "__main__":
    main()
