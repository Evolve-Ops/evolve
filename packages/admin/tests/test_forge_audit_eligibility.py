"""Test for forge_engine._derive_audit_eligibility.

The heuristic decides whether forge marks a freshly-built app as
audit-eligible. Pure-data manifests (.md only, or data/state layer) get
audit_eligible=False; anything with real code gets True.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.forge_engine import _derive_audit_eligibility  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


def _mk(files=None) -> ApplicationManifest:
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    m.files = list(files or [])
    return m


def test_empty_files_is_ineligible() -> None:
    """No files = nothing to audit."""
    assert _derive_audit_eligibility(_mk(files=[])) is False


def test_python_code_is_eligible() -> None:
    """A real script makes the app worth auditing."""
    assert _derive_audit_eligibility(_mk(files=[
        {"path": "scripts/journal.py", "layer": "skill"},
    ])) is True


def test_only_markdown_docs_ineligible() -> None:
    """A manifest that ships only .md docs is a reference card, not code."""
    assert _derive_audit_eligibility(_mk(files=[
        {"path": "docs/README.md", "layer": "reference"},
        {"path": "docs/NOTES.md", "layer": "reference"},
    ])) is False


def test_data_only_ineligible() -> None:
    """Manifests that ship only data/state files are containers, not apps."""
    assert _derive_audit_eligibility(_mk(files=[
        {"path": "data/seed.json", "layer": "data"},
        {"path": "state/cursor.json", "layer": "state"},
    ])) is False


def test_trivial_data_only_ineligible() -> None:
    """A trivial cron with only data files and no .py code."""
    m = _mk(files=[{"path": "config/greet.txt", "layer": "data"}])
    assert _derive_audit_eligibility(m) is False


def test_code_plus_data_still_eligible() -> None:
    """Mixed code + data → eligible (code drives the decision)."""
    m = _mk(files=[
        {"path": "scripts/x.py", "layer": "skill"},
        {"path": "config/data.json", "layer": "data"},
    ])
    assert _derive_audit_eligibility(m) is True


def test_mixed_code_and_docs_eligible() -> None:
    """Common case: scripts + a README → eligible."""
    assert _derive_audit_eligibility(_mk(files=[
        {"path": "scripts/journal.py", "layer": "skill"},
        {"path": "docs/README.md", "layer": "reference"},
        {"path": "data/journal.jsonl", "layer": "data"},
    ])) is True


def test_shell_script_eligible() -> None:
    """`.sh` counts as code."""
    assert _derive_audit_eligibility(_mk(files=[
        {"path": "scripts/sweep.sh", "layer": "skill"},
    ])) is True
