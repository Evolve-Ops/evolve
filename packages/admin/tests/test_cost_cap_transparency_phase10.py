"""tests/test_cost_cap_transparency_phase10.py — Phase 10 of the 2026-06
cost-cap normalization (spec: internal/spec-cost-caps-2026-06-05.md).

Pins the user-facing transparency rules:
- POD_CONDUCT.md carries rule 12 (cost cap transparency) in the prompt
  summary and a §13 section explaining the per-tier banner behavior.
- The catalog events for tier_downgrade + L2 breaker (Phase 6) are still
  registered with the right severity + producer.
- The evo tools registered in Phase 9 are still resolvable from the
  registry — they're how evo reads remediation state to decide whether
  to prepend a banner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_DIR = _REPO_ROOT / "packages" / "admin"
_ANALYZER_DIR = _REPO_ROOT / "packages" / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── POD_CONDUCT.md contains the transparency rule ────────────────────────


@pytest.fixture
def conduct_text() -> str:
    path = _REPO_ROOT / "docs" / "system" / "POD_CONDUCT.md"
    return path.read_text()


def test_pod_conduct_summary_block_lists_cost_cap_transparency(conduct_text):
    """The prompt summary block at the top of POD_CONDUCT.md must include
    the cost-cap transparency rule so it lands in every session preamble."""
    assert "12. Cost cap transparency" in conduct_text, (
        "rule 12 missing from the prompt summary block"
    )
    # B7 facade syntax — the conduct doc teaches the advertised call, not
    # the canonical registry name (which is dispatch-only since the cut).
    assert 'pod_state(query="cost_remediation_status")' in conduct_text, (
        "summary block must name the read tool evo calls to check state"
    )


def test_pod_conduct_has_dedicated_section_for_cost_cap_transparency(conduct_text):
    """The detailed section §13 explains the four tiers + banner copy."""
    assert "## 13. Cost Cap Transparency" in conduct_text
    # Each remediation tier should be named so the LLM knows the full ladder.
    for tier in ("tier_downgrade", "l1_breaker", "l2_breaker", "per_session_cap"):
        assert tier in conduct_text, (
            f"§13 must name the {tier!r} tier"
        )
    # Banner exemplars — at least the three remediation cases.
    assert "Heads up: I'm on a tier-3 model today" in conduct_text
    assert "heartbeat is paused" in conduct_text
    assert "shutdown mode" in conduct_text


# ─── Phase 6 catalog events still wired ───────────────────────────────────


def test_tier_downgrade_catalog_event_registered():
    from evolve_admin.alerts.catalog import CATALOG as EVENTS
    keys = {e.key for e in EVENTS}
    assert "cost.tier_downgrade_active" in keys


def test_l2_breaker_catalog_event_registered():
    # Key renamed to ``cost.gateway_stopped`` in Phase 6 — gitleaks'
    # generic-api-key entropy filter false-positived on identifiers
    # containing digits (``cost.l2_*``). Digit-free names pass clean.
    from evolve_admin.alerts.catalog import CATALOG as EVENTS
    keys = {e.key for e in EVENTS}
    assert "cost.gateway_stopped" in keys


def test_l2_breaker_catalog_event_is_safety_critical():
    """L2 stops the gateway entirely — operators need to see it now."""
    from evolve_admin.alerts.catalog import CATALOG as EVENTS
    l2 = next(e for e in EVENTS if e.key == "cost.gateway_stopped")
    assert l2.is_safety_critical is True


# ─── Phase 9 evo tools resolvable (banner read path) ─────────────────────


def test_remediation_status_tool_resolvable():
    from evolve_admin.evo.tools import all_tools
    names = [t.name for t in all_tools()]
    assert "pod_state.cost_remediation_status" in names, (
        "evo can't read remediation state to decide whether to prepend a banner"
    )


def test_cost_caps_tool_resolvable():
    from evolve_admin.evo.tools import all_tools
    names = [t.name for t in all_tools()]
    assert "pod_state.cost_caps" in names
