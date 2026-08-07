from __future__ import annotations

import io
import json
import unittest
import zipfile

from hailiang_skills.api.routes.chat import build_session_logs_archive
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.session_logging import append_session_events
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


class ChatLogDownloadTest(unittest.TestCase):
    def test_download_session_logs_zip_contains_snapshot_and_events(self) -> None:
        repository = InMemorySessionRepository()
        context = SessionContext(session_id="sess_download_test", user_id="debug-user")
        context.add_message("user", "测试下载日志")
        event = make_event("unit_test_event", {"ok": True})
        context.event_trace.append(event)
        repository.create(context)
        append_session_events(context.session_id, [event])

        repository.save(context)
        archive_bytes = build_session_logs_archive(context.session_id)

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
            self.assertIn("snapshot.json", names)
            self.assertIn("events.jsonl", names)
            snapshot = json.loads(archive.read("snapshot.json").decode("utf-8"))
            events_text = archive.read("events.jsonl").decode("utf-8")

        self.assertEqual(snapshot["session_id"], context.session_id)
        self.assertIn("unit_test_event", events_text)


if __name__ == "__main__":
    unittest.main()
