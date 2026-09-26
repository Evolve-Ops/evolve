"""D4 fixture test — PY side of the shared app-id resolution vector.

audit-app-framework-2026-07-02 D4: the PY and TS app-id resolvers used to
hand-mirror the priority order ``pkg_id -> id -> spec_id -> instance_id``, and
a silent drift between the copies re-opens the #3387 class (coverage badge
never clears). AL-1.4a collapsed each side onto ONE implementation —
``applications/app_identity.resolve_app_id`` here, ``apps/appIdentity.appIdOf``
there — and put the canonical ``app_id`` at the head of the order. This test
and packages/plugin/tests/appIdResolution.test.mjs consume the SAME JSON vector
(packages/plugin/tests/fixtures/app-id-resolution.json) so the order can still
only change on both sides at once.

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

from evolve_admin.applications import app_identity  # noqa: E402
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


def test_coverage_reader_reexports_the_one_resolver() -> None:
    """The documented import site must BE the shared resolver, not a copy.

    ``app_integrity_coverage.resolve_app_id`` is where the coverage reader (and
    this test's original import) reaches for the answer. AL-1.4a made it a
    re-export; if someone re-inlines a second implementation there, the D4
    drift class is back even though this file's fixture cases still pass.
    """
    assert resolve_app_id is app_identity.resolve_app_id


def test_draft_id_never_resolves_as_an_app_id() -> None:
    """A discovered draft's id must not leak through the app-id side.

    Design §3: a draft may be merged, renamed or dropped freely, so its id is
    explicitly unstable and must never appear in attribution, access or
    sharing. ``draft_id_of`` reads it; ``resolve_app_id`` must not.
    """
    draft = {"draft_id": "draft-9f2c1a"}
    assert app_identity.draft_id_of(draft) == "draft-9f2c1a"
    assert app_identity.resolve_app_id(draft) == ""
    assert app_identity.draft_id_of({"pkg_id": "app-a"}) == ""
