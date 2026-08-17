"""D4 fixture test — PY side of the shared app-id resolution vector.

audit-app-framework-2026-07-02 D4: ``app_integrity_coverage.resolve_app_id``
(PY) and ``appScriptRegistry.appIdOf`` (TS) hand-mirror the priority order
``pkg_id -> id -> spec_id -> instance_id``. A silent drift re-opens the #3387
class (coverage badge never clears). This test and
packages/plugin/tests/appIdResolution.test.mjs consume the SAME JSON vector
(packages/plugin/tests/fixtures/app-id-resolution.json) so the order can only
change on both sides at once. AL-1.4 later collapses the order to ``app_id``
behind this same fixture.

``expected_id: null`` in the fixture means "no id resolves"; the PY resolver's
documented fallback for that case is the empty string.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.app_integrity_coverage import resolve_app_id  # noqa: E402

_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "plugin" / "tests" / "fixtures" / "app-id-resolution.json"
)

_PY_NO_ID_FALLBACK = ""


def _load_cases() -> list[dict]:
    cases = json.loads(_FIXTURE.read_text())["cases"]
    assert isinstance(cases, list) and len(cases) >= 10, "vector must stay substantive"
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names), "case names must be unique"
    return cases


_CASES = _load_cases()


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_resolve_app_id_matches_shared_vector(case: dict) -> None:
    expected = case["expected_id"]
    if expected is None:
        expected = _PY_NO_ID_FALLBACK
    assert resolve_app_id(case["manifest"]) == expected
