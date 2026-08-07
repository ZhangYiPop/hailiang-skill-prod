from pathlib import Path
import os
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SHARED_RUNTIME_CORE = Path(
    os.getenv(
        "AGENT_SKILL_RUNTIME_CORE_PATH",
        str(WORKSPACE_ROOT / "agent_skill_runtime_core"),
    )
)


def ensure_skill_runtime_importable() -> None:
    if SHARED_RUNTIME_CORE.exists() and str(SHARED_RUNTIME_CORE) not in sys.path:
        sys.path.insert(0, str(SHARED_RUNTIME_CORE))
    import hailiang_skills.skill_runtime  # noqa: F401
