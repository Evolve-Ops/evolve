"""tests/test_thresholds_moved.py — Phase 2.v2-B: relocate the
threshold-tuning tab from Reports → Alerts → Configure to Reports →
Subscriptions → Thresholds.

Spec: internal/spec-recommendations-rework-2026-06-02.md §"Course
correction 2026-06-04" — threshold-config breakthrough.

The operator observation that motivated this PR:

> The Configure page actually sets thresholds for SUBSCRIPTIONS, not
> ALERTS. Subscriptions show in the Reports/Subscriptions tab and in
> the Evo bot messaging. Alerts show up in the Reports/Alerts/Firing.
> So the "Configure" tab, under Alerts currently, actually belongs
> under the Subscriptions tab.

The threshold matrix tunes pod_report's anomaly detectors, which is
what becomes part of the daily Pod Report digest — subscription-
shaped configuration. The misplaced tab now sits at Reports →
Subscriptions → Thresholds, alongside the existing Reports →
Subscriptions → Configure (which handles subscribe / unsubscribe).
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ── Old location removed ──────────────────────────────────────────────────


def test_old_alerts_configure_subtab_removed():
    """The "Configure" inner subtab under Reports → Alerts must no
    longer exist — it moved under Subscriptions. The other Alerts
    subtabs (Firing, History) stay."""
    html = _html()
    # The exact subtab declaration that linked Reports → Alerts →
    # Configure must be gone.
    pattern = (
        r'<div\s+class="subtab-inner"\s+data-inner="configure"\s+'
        r"onclick=\"subInner\(this,'reports-alerts','configure'\)"
    )
    assert not re.search(pattern, html), (
        "Reports → Alerts → Configure subtab still present — the move "
        "to Subscriptions → Thresholds didn't take effect"
    )


def test_old_alerts_configure_page_div_removed():
    """The old subtab-inner-page DOM (#reports-alerts-configure) must
    be gone — the threshold matrix only renders inside the new
    location now."""
    html = _html()
    assert 'id="reports-alerts-configure"' not in html, (
        "stale #reports-alerts-configure div still present — the "
        "threshold matrix would mount in the wrong subtab-inner-page"
    )


# ── New location wired ────────────────────────────────────────────────────


def test_thresholds_inner_subtab_present_under_subscriptions():
    """New "Thresholds" inner subtab declared under Reports →
    Subscriptions, alongside Messages + Configure. The handler calls
    _raLoadThresholds() — same function the old location did."""
    html = _html()
    pattern = (
        r'<div\s+class="subtab-inner"\s+data-inner="thresholds"\s+'
        r"onclick=\"subInner\(this,'reports-subscriptions','thresholds'\);"
        r"_raLoadThresholds\(\)\""
    )
    assert re.search(pattern, html), (
        "Reports → Subscriptions → Thresholds subtab missing — operator "
        "has no way to reach the threshold matrix in its new location"
    )


def test_thresholds_inner_page_div_present():
    """New subtab-inner-page #reports-subscriptions-thresholds carries
    the threshold matrix mount point."""
    html = _html()
    assert 'id="reports-subscriptions-thresholds"' in html, (
        "#reports-subscriptions-thresholds subtab-inner-page missing — "
        "_raLoadThresholds() has nowhere to render"
    )
    # The matrix renders into #reports-thresholds-body, which must
    # still be present.
    assert 'id="reports-thresholds-body"' in html, (
        "#reports-thresholds-body mount point disappeared along with "
        "the relocation — _raLoadThresholds finds no targets"
    )


def test_thresholds_inner_page_inside_subscriptions_parent():
    """The inner-page must be a descendant of #reports-subscriptions
    (the outer subtab-page). Otherwise the subTab/subInner toggle
    logic that hides siblings won't find it."""
    html = _html()
    subs_idx = html.find('id="reports-subscriptions"')
    thresh_idx = html.find('id="reports-subscriptions-thresholds"')
    # Find the close of #reports-subscriptions by searching for the
    # next outer subtab-page sibling — reports-alerts.
    alerts_idx = html.find('id="reports-alerts"', subs_idx + 1)
    assert subs_idx > 0, "#reports-subscriptions parent not found"
    assert thresh_idx > 0, "#reports-subscriptions-thresholds not found"
    assert alerts_idx > 0, "#reports-alerts sibling not found"
    assert subs_idx < thresh_idx < alerts_idx, (
        "Thresholds inner-page must sit inside the Reports → "
        f"Subscriptions parent (between {subs_idx} and {alerts_idx}); "
        f"found at {thresh_idx}"
    )


# ── Router updated ────────────────────────────────────────────────────────


def test_router_handles_thresholds_under_subscriptions_group():
    """The subInner dispatcher routes the 'thresholds' name under the
    'reports-subscriptions' group to _raLoadThresholds(). Without
    this, navigating to the new tab calls no loader and the matrix
    never fetches."""
    html = _html()
    m = re.search(
        r"if\s*\(\s*group\s*===\s*['\"]reports-subscriptions['\"]\s*\)\s*\{(.+?)\}",
        html, re.DOTALL,
    )
    assert m, "reports-subscriptions group handler missing in subInner dispatch"
    body = m.group(1)
    assert "name === 'thresholds'" in body, (
        "'thresholds' name not dispatched under reports-subscriptions group"
    )
    assert "_raLoadThresholds" in body, (
        "_raLoadThresholds() not invoked under reports-subscriptions group"
    )


def test_router_no_longer_dispatches_thresholds_under_alerts_group():
    """The stale 'thresholds' handler under the 'reports-alerts'
    group must be removed. Leaving it in would still work
    accidentally, but creates a double-dispatch when the operator
    re-enters via a deep link — and it's a misleading sign of the
    pre-move organization."""
    html = _html()
    m = re.search(
        r"if\s*\(\s*group\s*===\s*['\"]reports-alerts['\"]\s*\)\s*\{(.+?)\}",
        html, re.DOTALL,
    )
    assert m, "reports-alerts group handler missing"
    body = m.group(1)
    # The if-stmt that fires _raLoadThresholds inside the alerts
    # group must be gone (the comment referencing the move is fine).
    bad = re.search(
        r"if\s*\(\s*name\s*===\s*['\"]thresholds['\"]\s*\)\s*\{?\s*_raLoadThresholds",
        body,
    )
    assert not bad, (
        "stale `if (name === 'thresholds') _raLoadThresholds()` still "
        "present in the reports-alerts group handler — should only "
        "live under reports-subscriptions now"
    )
