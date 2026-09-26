"""tests/test_ai_optimization_default_catalog_coverage.py — Phase 6b §D.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 3.D — the per-bot
catalog (OC's runtime allowlist) lacks default-role models, so a role resolving
to a code-default model (max → claude-fable-5) dies silently at the OC layer
with no advisory. The fix surfaces a ``role_resolves_outside_catalog`` drift
finding wired into the existing catalog-drift advisory + one-click add.

Source-level assertions that the freshness/drift renderer in ai-optimization.js
treats the new finding kind the same as ``tier_member_missing`` (same red drift
banner + Reconcile button), and that the stale "registry recommends" panel copy
is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)
_INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


def test_role_resolves_outside_catalog_kind_wired_into_drift(js: str):
    # The drift filter must include the new kind so the role finding renders
    # in the same red banner as tier_member_missing.
    m = re.search(
        r"tierDrift\s*=\s*driftFindings\.filter\((.*?)\);", js, re.DOTALL
    )
    assert m, "tierDrift filter not found"
    flt = m.group(1)
    assert "tier_member_missing" in flt
    assert "role_resolves_outside_catalog" in flt


def test_drift_renderer_handles_role_kind_phrasing(js: str):
    # _aiRenderCatalogDrift branches phrasing for the role kind (resolved
    # model not in catalog → pulls die at the OpenClaw layer).
    m = re.search(
        r"function _aiRenderCatalogDrift\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiRenderCatalogDrift not found"
    body = m.group(1)
    assert "role_resolves_outside_catalog" in body
    assert "resolved model" in body
    # Still offers the one-click catalog reconcile affordance for the bot.
    assert "_aiReconcileCatalog" in body


def test_stale_registry_recommends_copy_retired(html: str):
    # The obsolete "older model than the registry recommends" copy is replaced
    # by discovery+defaults+catalog-coverage language (§D.2).
    assert "registry recommends" not in html
    # New copy describes catalog coverage of every role's resolved model.
    # Window widened in Phase 10c: the freshness card is now a collapsible
    # <details>/<summary> (Addendum 7 item 7), so the description copy sits a
    # little deeper past the summary header markup.
    region = html[html.find("Model Freshness"):][:2800]
    assert "catalog" in region.lower()
    assert "resolved model" in region.lower()
