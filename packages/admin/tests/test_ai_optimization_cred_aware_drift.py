"""tests/test_ai_optimization_cred_aware_drift.py — Phase 12b §B.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §B —
credential-aware catalog drift. A Type-1 (tier_member_missing) drift naming a
model whose provider the bot has NO API key for can't be fixed by "Reconcile
catalog" (the server skips uncredentialed providers, by design). The detector
flags those provider_credentialed=false; the AI-Optimization renderer must
route them to a missing-credentials advisory (copy the key, or remove the tier
entry) instead of the red reconcile banner.

Source-level assertions on the freshness/drift renderer in ai-optimization.js:
the credential-aware split, the new credential-gap renderer, its reuse of the
existing borrow affordance, and the safe tier-write remove path.
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


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


def test_drift_split_on_provider_credentialed(js: str):
    # The renderer must split tierDrift on provider_credentialed: reconcilable
    # findings keep the red Reconcile banner, credential-gap findings go to the
    # new advisory. Both subsets are derived from the same tierDrift list.
    assert "reconcilableDrift" in js
    assert "credGapDrift" in js
    assert "provider_credentialed" in js
    # The reconcile banner renders only the reconcilable subset now.
    assert re.search(
        r"_aiRenderCatalogDrift\(\s*reconcilableDrift\s*\)", js
    ), "reconcile banner must receive only reconcilableDrift"
    # Phase 12c (§C): credGapDrift is split again by tier_severity. The dormant
    # subset (role still routes) feeds the cred-gap renderer; the hard-break
    # subset is folded into the prominent hard-break panel above.
    assert re.search(
        r"dormantDrift\s*=\s*credGapDrift\.filter\(", js
    ), "credGap must be split into a dormantDrift subset"
    assert re.search(
        r"_aiRenderCatalogCredGap\(\s*dormantDrift\s*\)", js
    ), "cred-gap banner must receive the dormant subset"


def test_cred_gap_renderer_offers_copy_and_remove(js: str):
    # _aiRenderCatalogCredGap reuses the existing copy-creds affordance
    # (_aiDiversityBorrow) and offers the remove-from-tier action; it does NOT
    # offer Reconcile (which can't help an uncredentialed-provider drift).
    m = re.search(
        r"function _aiRenderCatalogCredGap\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiRenderCatalogCredGap not found"
    body = m.group(1)
    assert "_aiDiversityBorrow" in body, "must reuse the borrow copy-creds flow"
    assert "_aiRemoveFromTier" in body, "must offer remove-from-tier"
    assert "borrow_candidates" in body, "must read server-attached candidates"
    assert "_aiReconcileCatalog" not in body, "cred-gap must NOT offer Reconcile"


def test_remove_from_tier_uses_safe_tier_write_path(js: str):
    # _aiRemoveFromTier must re-save via the same PUT .../tiers path the editor
    # uses (which re-runs the server reconcile pass), not an inline catalog edit.
    m = re.search(
        r"async function _aiRemoveFromTier\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiRemoveFromTier not found"
    body = m.group(1)
    assert re.search(r"PUT['\"]?\s*,\s*`?/api/admin/config/\$\{botId\}/tiers", body), \
        "must write through the /tiers safe path"
    # Tolerates both string and {id|model} tier entry shapes when filtering.
    assert ".filter(" in body
