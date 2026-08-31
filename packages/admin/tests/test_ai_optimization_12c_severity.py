"""tests/test_ai_optimization_12c_severity.py — Phase 12c §C UI rendering.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 10 §C —
hard-break vs soft-dormant visual distinction.

Source-level assertions on ai-optimization.js:
  (a) Hard-break row: role with no credentialed model renders an amber error row
      (available=false, unavailableReason=uncredentialed) — NOT the generic
      "Unavailable" yellow text path.
  (b) Soft-dormant row: role that resolves fine but carries inert non-cred
      fallbacks renders a quiet muted "Dormant fallback" notice.
  (c) Normal case (all-credentialed): neither hard-break nor dormant row appears.
  (d) The hard-break and soft-dormant renderers are mutually exclusive:
      dormant gate checks `available !== false`.
  (e) _aiRoleCredSeverityRows is a shared helper called from both Use-defaults
      and Custom renderRole paths (not duplicated inline).
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


# ── (a) Hard-break rendering ──────────────────────────────────────────────────

def test_hard_break_helper_defined(js: str):
    """_aiRoleCredSeverityRows helper exists in the JS."""
    assert re.search(r"function _aiRoleCredSeverityRows\(", js), \
        "_aiRoleCredSeverityRows helper not found"


def test_hard_break_checks_uncredentialed_reason(js: str):
    """The hard-break branch fires on unavailableReason === 'uncredentialed',
    NOT on a generic !available check (preserves cap_exhausted / unconfigured
    for the generic path)."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    # Must check for the specific reason, not just !available.
    assert "uncredentialed" in body, \
        "hard-break must test for 'uncredentialed' reason"
    assert "No credentialed model" in body or "no credentialed" in body.lower(), \
        "hard-break row must name the problem legibly"
    # Must use the amber/yellow token for prominence.
    assert "var(--yellow)" in body, \
        "hard-break row must use var(--yellow) for prominence"


def test_hard_break_does_not_use_reconcile(js: str):
    """_aiRoleCredSeverityRows must NOT offer Reconcile catalog — that action
    can't help an uncredentialed-provider entry."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    assert "_aiReconcileCatalog" not in body, \
        "_aiRoleCredSeverityRows must not offer Reconcile (can't help)"


# ── (b) Soft-dormant rendering ────────────────────────────────────────────────

def test_soft_dormant_reads_inert_providers(js: str):
    """The soft-dormant branch reads rv.inertProviders from the server payload."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    assert "inertProviders" in body, \
        "soft-dormant must consume the inertProviders field from the server"


def test_soft_dormant_quiet_styling(js: str):
    """The soft-dormant row is muted — uses var(--text3) (not yellow/red)."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    assert "var(--text3)" in body, \
        "soft-dormant row must use var(--text3) for quiet/muted styling"
    assert "Dormant" in body or "dormant" in body, \
        "soft-dormant row must use the 'dormant' label"


def test_soft_dormant_mentions_auto_activate(js: str):
    """The soft-dormant row explains how to activate the entry (add creds)."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    # The row should mention adding credentials or auto-activation.
    assert "auto-activates" in body or "add credentials" in body.lower() or \
        "credentials" in body.lower(), \
        "soft-dormant must explain the remedy (add creds or auto-activates)"


# ── (c) Mutual exclusion ──────────────────────────────────────────────────────

def test_dormant_gate_checks_available_true(js: str):
    """The soft-dormant branch only fires when the role IS available.
    This ensures hard-break (available=false) can never also render a dormant row."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    # The dormant branch must guard on `available` (truthy).
    # A pattern like `if (available && inert.length` ensures mutual exclusion.
    assert re.search(r"if\s*\(\s*available\s*&&\s*inert", body), \
        "dormant branch must guard on 'available && inert.length'"


# ── (d) Generic unavail path preserved for non-uncredentialed reasons ─────────

def test_generic_unavail_skips_uncredentialed_in_usedefaults(js: str):
    """_aiRenderTiersUseDefaults's generic unavailRow skips the 'uncredentialed'
    reason (it is handled by _aiRoleCredSeverityRows instead)."""
    m = re.search(
        r"function _aiRenderTiersUseDefaults\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRenderTiersUseDefaults not found"
    body = m.group(1)
    # The generic path should have an exclusion for uncredentialed.
    assert "uncredentialed" in body, \
        "_aiRenderTiersUseDefaults must exclude 'uncredentialed' from generic path"
    assert re.search(r"!== ['\"]uncredentialed['\"]", body) or \
        re.search(r"uncredentialed.*!==", body), \
        "generic unavailRow must exclude the 'uncredentialed' reason"


def test_generic_unavail_skips_uncredentialed_in_custom(js: str):
    """_aiRenderTiersCustom's generic unavailRow also skips 'uncredentialed'."""
    m = re.search(
        r"function _aiRenderTiersCustom\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRenderTiersCustom not found"
    body = m.group(1)
    assert "uncredentialed" in body, \
        "_aiRenderTiersCustom must exclude 'uncredentialed' from generic path"
    assert re.search(r"!== ['\"]uncredentialed['\"]", body) or \
        re.search(r"uncredentialed.*!==", body), \
        "generic unavailRow must exclude the 'uncredentialed' reason"


# ── (f) Same-vendor judge advisory (soft preference, 2026-06-19) ──────────────

def test_advisory_branch_in_cred_severity_rows(js: str):
    """_aiRoleCredSeverityRows renders a soft amber advisory for a judge that
    routes but on the same vendor as Standard (advisoryReason ===
    'same_vendor_as_standard') — NOT the red hard-break band."""
    m = re.search(
        r"function _aiRoleCredSeverityRows\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRoleCredSeverityRows not found"
    body = m.group(1)
    assert "same_vendor_as_standard" in body, \
        "advisory branch must test rv.advisoryReason === 'same_vendor_as_standard'"
    assert "advisoryProvider" in body, \
        "advisory row must surface the doubled-up provider (advisoryProvider)"
    # Gated on available (routes fine), and uses amber, not red.
    assert re.search(r"available\s*&&\s*rv\.advisoryReason", body), \
        "advisory branch must guard on 'available && rv.advisoryReason'"
    assert "still routes" in body.lower() or "not required" in body.lower(), \
        "advisory copy must make clear the role still routes / diversity optional"


def test_advisory_tiers_panel_defined(js: str):
    """The freshness card has an _aiRenderAdvisoryTiers panel renderer for the
    same-vendor judge advisory, and it is wired into the render path."""
    assert re.search(r"function _aiRenderAdvisoryTiers\(", js), \
        "_aiRenderAdvisoryTiers panel renderer not found"
    assert "summary.advisory_tiers" in js or "advisory_tiers" in js, \
        "render path must read summary.advisory_tiers"
    assert "advisoryBlock" in js, \
        "the advisory block must be wired into the freshness card output"


def test_advisory_tiers_panel_is_amber_not_red(js: str):
    """The advisory panel uses the amber token, never the red 'won't route'
    copy reserved for genuine hard breaks."""
    m = re.search(
        r"function _aiRenderAdvisoryTiers\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRenderAdvisoryTiers not found"
    body = m.group(1)
    assert "var(--yellow)" in body, "advisory panel must use the amber token"
    assert "won't route" not in body, \
        "advisory panel must NOT use the red hard-break 'won't route' copy"
    assert "same vendor as Standard" in body, \
        "advisory panel must name the same-vendor condition"


# ── (e) Helper called from both render paths ──────────────────────────────────

def test_cred_severity_rows_called_from_usedefaults(js: str):
    """_aiRenderTiersUseDefaults calls _aiRoleCredSeverityRows(rv)."""
    m = re.search(
        r"function _aiRenderTiersUseDefaults\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRenderTiersUseDefaults not found"
    body = m.group(1)
    assert "_aiRoleCredSeverityRows" in body, \
        "_aiRenderTiersUseDefaults must call _aiRoleCredSeverityRows"


def test_cred_severity_rows_called_from_custom(js: str):
    """_aiRenderTiersCustom calls _aiRoleCredSeverityRows(rv)."""
    m = re.search(
        r"function _aiRenderTiersCustom\([^)]*\)\s*\{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert m, "_aiRenderTiersCustom not found"
    body = m.group(1)
    assert "_aiRoleCredSeverityRows" in body, \
        "_aiRenderTiersCustom must call _aiRoleCredSeverityRows"
