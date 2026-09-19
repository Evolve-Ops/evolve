"""tests/test_jump_to_proposals_routing.py — pin the surface-aware
``jumpToProposals`` routing introduced after the 2026-06-04 surface
flip.

Background: ``renderArbiterProposals`` filters the Self-Improvement
page to ``surface=improvement`` only — everything else (cleanup,
drift, firing, null) routes to Reports → Alerts. But the various
"→ Act" / "see all in Proposals" links from the Security tab,
Dashboard, and Cost Measures page all went through
``jumpToProposals(dimension, botId)`` which unconditionally navigated
to Self-Improvement. The result: clicking a safety-dimension finding
landed on an empty Recommendations queue because every safety
generator carries surface in {cleanup, drift, firing}.

This file pins the routing contract source-side. The tests don't run
the JS — they grep the HTML for the function signatures and call
shapes that encode the surface-routing fix.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_POD_CONFIG_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "pod-config.js"
_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8")


def test_jump_to_proposals_accepts_surface_parameter():
    """The signature must accept a third ``surface`` argument so
    every caller can route based on the proposal's charter surface.
    Without this parameter the function falls back to the historical
    "always go to Self-Improvement" behavior that misroutes
    non-improvement findings to an empty queue."""
    html = _html()
    sig = re.search(
        r"function jumpToProposals\(([^)]*)\)", html,
    )
    assert sig, "jumpToProposals function not found"
    params = [p.strip() for p in sig.group(1).split(",")]
    assert params == ["dimension", "botId", "surface"], (
        f"jumpToProposals signature must be (dimension, botId, surface); "
        f"got ({sig.group(1)})"
    )


def test_jump_to_proposals_routes_non_improvement_to_reports():
    """The function body must contain an early-return branch that
    navigates to ``data-page=reports`` and clicks the ``alerts``
    subtab when ``surface`` is non-empty and not ``improvement``.
    Without that branch, the caller's surface hint has no effect."""
    html = _html()
    fn = re.search(
        r"function jumpToProposals\([^)]*\)\s*\{(.+?)\n\}\n",
        html, re.DOTALL,
    )
    assert fn, "jumpToProposals function body not found"
    body = fn.group(1)
    assert "surface !== 'improvement'" in body or (
        "surface !== \"improvement\"" in body
    ), "Routing condition missing — non-improvement surfaces must branch"
    assert 'data-page="reports"' in body, (
        "Reports-page navigation missing from jumpToProposals"
    )
    assert 'data-subtab="alerts"' in body, (
        "Reports → Alerts subtab click missing — without it, the "
        "user lands on the Reports page's default subtab "
        "(Subscriptions) rather than the Alerts feed"
    )


def test_strip_act_button_passes_per_proposal_surface():
    """The per-proposal ``→ Act`` button in
    ``renderRelatedProposalsStrip`` must pass each proposal's
    ``surface`` to jumpToProposals — otherwise the strip's items
    all route to the same default destination regardless of where
    they live."""
    html = _html()
    fn = re.search(
        r"function renderRelatedProposalsStrip\([^)]*\)\s*\{(.+?)\n\}\n",
        html, re.DOTALL,
    )
    assert fn, "renderRelatedProposalsStrip function body not found"
    body = fn.group(1)
    # The button's onclick must read from the proposal and forward
    # its surface as the third arg. Pin the shape so a future cleanup
    # that drops the surface argument fails loudly.
    assert "p.surface" in body, (
        "per-proposal surface read missing from "
        "renderRelatedProposalsStrip"
    )
    # Pull the function body again with greedy balanced matching so
    # nested template literals like ``${escHtml(dimension)}`` don't
    # confuse the count. Then look for the call shape:
    #   jumpToProposals('${escHtml(dimension)}', '<bot>', '<surface>')
    # which has exactly two commas at the top level of the call.
    # Two commas → three args (dimension, botId, surface). Pre-fix
    # had zero or one comma (single-arg or 2-arg calls).
    #
    # We look for either the literal ``surface)`` token inside a
    # call (which would appear when the template injects p.surface
    # as ``${escHtml(surface)}``) or a third-arg quoted literal that
    # isn't 'improvement' (covering callers that hint to Alerts).
    assert "escHtml(surface)" in body or "'cleanup'" in body, (
        "renderRelatedProposalsStrip must thread per-proposal "
        "surface into jumpToProposals — either via "
        "${escHtml(surface)} (per-proposal button) or a "
        "non-improvement literal (bulk see-all link). Pre-fix, the "
        "strip ignored surface entirely and every click misrouted "
        "to Self-Improvement."
    )


def test_security_tab_per_bot_link_passes_non_improvement_surface():
    """The Security page's per-bot proposal-count link calls
    ``_secJumpToSecurityProposals(botId)``, which used to call
    ``jumpToProposals('safety', botId)`` with no surface — defaulting
    to Self-Improvement. Every safety-dimension generator on this
    pod carries surface in {cleanup, drift, firing}, so the
    fix is to pass a non-improvement surface hint that routes the
    click to Reports → Alerts where these proposals actually live."""
    html = _html()
    fn = re.search(
        r"function _secJumpToSecurityProposals\([^)]*\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_secJumpToSecurityProposals function body not found"
    body = fn.group(1)
    # The surface arg must be present and must not be 'improvement'
    # (which would re-introduce the bug).
    call = re.search(
        r"jumpToProposals\(['\"]safety['\"],\s*botId,\s*['\"]([^'\"]+)['\"]\)",
        body,
    )
    assert call, (
        "_secJumpToSecurityProposals must call jumpToProposals with "
        "a non-empty surface argument"
    )
    assert call.group(1) != "improvement", (
        "Surface hint must not be 'improvement' — that would route "
        "to Self-Improvement, which (after the surface flip) has no "
        "safety-dimension proposals"
    )
