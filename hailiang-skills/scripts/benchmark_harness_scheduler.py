"""Measure scheduler scan cost against a disposable, empty PostgreSQL database.

Set HAILIANG_BENCHMARK_DATABASE_URL to a dedicated database named
``harness_perf``. This script creates test data and never clears existing data.
It uses synthetic tasks and no model calls; it is not a production benchmark.
"""
from __future__ import annotations

import os
import statistics
import time
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

from hailiang_skills.harness.models import TABLES
from hailiang_skills.harness.runtime import HarnessRuntime
from hailiang_skills.harness.store import HarnessStore


def main():
    url = os.environ['HAILIANG_BENCHMARK_DATABASE_URL']
    if make_url(url).database != 'harness_perf':
        raise SystemExit('Only a disposable database named harness_perf is permitted')
    engine = create_engine(url, pool_pre_ping=True)
    for table in TABLES:
        table.create(engine, checkfirst=True)
    queries = [0]
    @event.listens_for(engine, 'before_cursor_execute')
    def count_sql(*_args):
        queries[0] += 1
    runtime = HarnessRuntime(HarnessStore(engine), lambda _: None)
    policy = {'experts': {}, 'skills': {}, 'independent_skills': [],
              'settings': {'max_parallel': 3, 'max_model_calls': 24, 'max_tool_calls': 48}, 'soul': ''}
    prefix = uuid4().hex
    for i in range(50):
        rid = runtime.create(f'bench-{prefix}-{i}', 'bench-user', 'bench-profile', '模拟问题', policy,
                             wire_protocol='hailiang.sse.v3')
        def waiting(run, changes, _db):
            root = run['tasks'][run['root_task_id']]
            root['status'] = 'waiting_for_user'
            changes.append({'event': 'benchmark_waiting', 'node_id': root['id']})
        runtime.store.mutate(rid, waiting)
    scans, counts = [], []
    for _ in range(5):
        start, first_count = time.perf_counter(), queries[0]
        run_ids = runtime.store.runnable_runs()
        for rid in run_ids:
            runtime.claim(rid)
        scans.append((time.perf_counter() - start) * 1000)
        counts.append(queries[0] - first_count)
    print(f'active_waiting_runs={len(run_ids)} scans=5 median_scan_ms={statistics.median(scans):.1f} '
          f'max_scan_ms={max(scans):.1f} median_sql_statements={statistics.median(counts):.0f}')
    scans, counts = [], []
    for _ in range(5):
        start, first_count = time.perf_counter(), queries[0]
        candidates = runtime.store.likely_runnable_runs()
        for rid in candidates:
            runtime.claim(rid)
        scans.append((time.perf_counter() - start) * 1000)
        counts.append(queries[0] - first_count)
    print(f'candidate_runs={len(candidates)} scans=5 median_scan_ms={statistics.median(scans):.1f} '
          f'max_scan_ms={max(scans):.1f} median_sql_statements={statistics.median(counts):.0f}')
    new_run = runtime.create(f'bench-{prefix}-new', 'bench-user', 'bench-profile', '新任务', policy,
                             wire_protocol='hailiang.sse.v3')
    started = time.perf_counter()
    candidates = runtime.store.likely_runnable_runs()
    assert new_run in candidates
    assert runtime.claim(new_run) is not None
    print(f'new_run_candidate_and_claim_ms={(time.perf_counter() - started) * 1000:.1f}')
    runtime.close()
    engine.dispose()


if __name__ == '__main__':
    main()
