"""Standalone durable worker: python -m hailiang_skills.harness.worker."""
from __future__ import annotations
import signal
import threading


def main():
    from hailiang_skills.api.main import app
    stopped = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stopped.set())
    runtime = app.state.harness
    runtime.start()
    try:
        stopped.wait()
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
