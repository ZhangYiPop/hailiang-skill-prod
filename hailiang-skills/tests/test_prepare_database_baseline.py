import importlib.util
import sys
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "prepare_database_baseline",
    Path(__file__).resolve().parents[1] / "scripts" / "prepare_database_baseline.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

BASELINE_REVISION = _MODULE.BASELINE_REVISION
DatabaseState = _MODULE.DatabaseState
compatible_revisions = _MODULE.compatible_revisions


def test_current_alembic_head_is_accepted_by_database_guard() -> None:
    revisions = compatible_revisions()

    assert BASELINE_REVISION in revisions
    assert "0002_business_workbench" in revisions
    assert DatabaseState(revisions=("0002_business_workbench",), tables=("alembic_version",)).compatible


def test_unknown_or_unversioned_nonempty_database_remains_incompatible() -> None:
    assert not DatabaseState(revisions=("legacy_revision",), tables=("alembic_version",)).compatible
    assert not DatabaseState(revisions=(), tables=("legacy_table",)).compatible
    assert DatabaseState(revisions=(), tables=()).compatible
