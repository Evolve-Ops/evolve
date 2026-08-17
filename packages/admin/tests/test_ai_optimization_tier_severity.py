"""tests/test_ai_optimization_tier_severity.py — Phase 12c §C UI split.

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §C.

The AI-Optimization freshness renderer must split credential-gap drift by
severity: a tier whose whole chain has no credentialed model (hard break) is
rendered PROMINENTLY at the top (a real "this tier won't route" error), while an
inert non-cred fallback on a routing tier is rendered QUIETLY (muted +
collapsible "dormant" row). Both reuse the §B copy-creds / remove-from-tier
remedies. Source-level assertions on ai-optimization.js.
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


def test_hard_break_split_from_credgap(js: str):
    # The renderer reads the hard_break_tiers side-car and splits the dormant
    # subset out of credGapDrift by tier_severity.
    assert "hard_break_tiers" in js
    assert re.search(r"hardBreakTiers\s*=\s*summary\.hard_break_tiers", js)
    assert re.search(
        r"dormantDrift\s*=\s*credGapDrift\.filter\(\s*d\s*=>\s*d\.tier_severity\s*!==\s*'hard_break'",
        js,
    ), "dormant subset must exclude hard_break-tagged findings"
    # Hard-break panel is built from hardBreakTiers; dormant from dormantDrift.
    assert re.search(r"_aiRenderHardBreak\(\s*hardBreakTiers\s*\)", js)
    assert re.search(r"_aiRenderCatalogCredGap\(\s*dormantDrift\s*\)", js)


def test_hard_break_rendered_before_other_blocks(js: str):
    # In both render branches, the hard-break block comes before the drift /
    # cred-gap blocks (prominent, top-of-panel).
    for m in re.finditer(r"\$\{hardBreakBlock\}", js):
        tail = js[m.end():m.end() + 200]
        assert "${driftBlock}" in tail, "hardBreakBlock must precede driftBlock"
    assert js.count("${hardBreakBlock}") >= 2, "rendered in both branches"


def test_hard_break_renderer_offers_copy_and_remove(js: str):
    m = re.search(
        r"function _aiRenderHardBreak\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiRenderHardBreak not found"
    body = m.group(1)
    assert "needed_providers" in body
    assert "borrow_candidates" in body
    assert "offending_models" in body
    assert "_aiDiversityBorrow" in body, "must reuse the copy-creds flow"
    assert "_aiRemoveFromTier" in body, "must reuse remove-from-tier"
    assert "var(--red)" in body, "hard break uses the red (error) styling family"
    assert "won't route" in body


def test_dormant_renderer_is_muted_and_collapsible(js: str):
    m = re.search(
        r"function _aiRenderCatalogCredGap\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiRenderCatalogCredGap not found"
    body = m.group(1)
    # Collapsible via <details> + the style-guide .expand-icon chevron (NOT a
    # Unicode triangle glyph).
    assert "<details" in body and "</details>" in body
    assert "expand-icon" in body
    assert "dormant" in body.lower()
    # Still offers the §B remedies.
    assert "_aiDiversityBorrow" in body
    assert "_aiRemoveFromTier" in body
    assert "_aiReconcileCatalog" not in body, "dormant must NOT offer Reconcile"


def test_summary_count_includes_pure_hard_breaks(js: str):
    # The collapsed-card badge must count pure-resolution hard breaks (no drift
    # finding) so a judge-with-no-diverse-provider break still nudges the
    # operator to expand — without double-counting the finding-backed ones.
    m = re.search(
        r"function _aiSyncFreshnessSummaryCount\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL
    )
    assert m, "_aiSyncFreshnessSummaryCount not found"
    body = m.group(1)
    assert "hard_break_tiers" in body
    assert "offending_models" in body, "must add only the finding-less hard breaks"
