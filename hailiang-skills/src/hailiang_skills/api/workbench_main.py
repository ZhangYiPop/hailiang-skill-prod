"""Dedicated workbench process entrypoint.

Run with ``uvicorn hailiang_skills.api.workbench_main:app``.  It intentionally
uses the same app factory and runtime image as production so configuration
debugging cannot drift from the live orchestration kernel.
"""

from hailiang_skills.api.main import app


app.title = "hailiang-skills business workbench"
