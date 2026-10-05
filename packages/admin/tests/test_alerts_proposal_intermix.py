"""tests/test_alerts_proposal_intermix.py — Proposals routing on Reports.

The history of this file mirrors the architecture conversation:

  Pre-2026-06-04: "Generator proposals" section appended under the
                   Reports → Alerts → Firing lane; sectioned-off from
                   Signals with a divider.
  PR #2085 ("Task C / intermix"): dropped the divider but kept the
                   ordering "signals first, proposals second" — half-
                   implemented intermix that operator feedback flagged
                   as still looking like an appended section.
  PR #2132 (subtab): Proposals get their own subtab under Reports →
                   Alerts ("Firing / Proposals / History").
  2026-06-04 promotion (this PR): Proposals promoted from inner subtab
                   to top-level peer of Alerts. Reports navigation is
                   now Subscriptions / Alerts / Proposals / Watchlist.
                   Same _alLoadProposalsTab loader; new outer-tab body
                   container and badge.

Three contracts pinned here:

  1. The Firing lane (``_alLoadLane``) does NOT render proposals — it
     only emits Signal rows. The historic ``proposalsHtml`` concat is
     gone.
  2. Reports has a top-level **Proposals** subtab between Alerts and
     Watchlist, with the same load handler the previous inner subtab
     used. Inner-Alerts shape (PR #2132) is rolled back.
  3. The loader fetches from /api/arbiter/proposals with the surface
     filter and Signal-link dedup that PR #2085 introduced — same
     semantics, different surface (now top-level instead of nested).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_ALERTS_EXTENDED_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "alerts-extended.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _ALERTS_EXTENDED_JS.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """Pull out the body of an async function for source inspection."""
    html = _html()
    m = re.search(
        rf"async function {re.escape(name)}\([^)]*\)\s*\{{(.+?)\n\}}\n",
        html, re.DOTALL,
    )
    assert m, f"async function {name} not found"
    return m.group(1)


# ── Firing lane no longer renders proposals ─────────────────────────────────


def test_firing_lane_does_not_render_proposals():
    """The Firing lane's render path must NOT concat proposal markup
    onto the signal rows. The old shape was

        const html = ... + proposalsHtml

    where ``proposalsHtml`` carried the standalone proposal rows. With
    the tab promotion, proposals live on their own top-level subtab;
    the lane renders only Signals."""
    body = _function_body("_alLoadLane")
    assert "+ proposalsHtml" not in body, (
        "_alLoadLane must not concat proposalsHtml onto the signal "
        "rows — proposals render under the top-level Reports → "
        "Proposals subtab via _alLoadProposalsTab"
    )
    assert "let proposalsHtml" not in body, (
        "_alLoadLane should not declare proposalsHtml — the rendering "
        "moved to _alLoadProposalsTab"
    )


def test_firing_lane_empty_state_keys_on_signals_only():
    """With proposals gone from the Firing lane, the empty-state branch
    must check only ``sigs.length`` — not the historic
    ``!sigs.length && !proposalsHtml`` compound check, which would now
    always evaluate the right-hand side as truthy-undefined."""
    body = _function_body("_alLoadLane")
    assert "!sigs.length && !proposalsHtml" not in body, (
        "Empty-state must not reference proposalsHtml — that variable "
        "no longer exists in the lane scope"
    )
    assert re.search(r"const html\s*=\s*\(!sigs\.length\)", body), (
        "Empty-state branch should switch on (!sigs.length) alone"
    )


# ── Proposals is a top-level peer of Alerts ─────────────────────────────────


def test_proposals_subtab_is_top_level_peer():
    """Reports has Subscriptions / Alerts / Proposals / Watchlist at the
    OUTER subtab level. Proposals must declare data-subtab="proposals"
    at the outer (subTab) level — NOT data-inner="proposals" inside
    the Alerts subtab. The 2026-06-04 promotion moved it up one level
    because Alerts and Proposals have materially different lifecycles
    and action vocabularies."""
    html = _html()
    # Outer subtab — calls subTab(this,'reports','proposals')
    assert re.search(
        r'data-subtab="proposals"[^>]*onclick="subTab\(this,\s*\'reports\',\s*\'proposals\'\)',
        html,
    ), (
        "Proposals must be a top-level Reports subtab (data-subtab='proposals' "
        "wired via subTab()), not nested inside Alerts"
    )


def test_no_proposals_subtab_nested_inside_alerts():
    """Roll back the PR #2132 inner-subtab shape. The Alerts page's
    inner subtab row should only carry Firing + History (Configure was
    relocated to Subscriptions → Thresholds in Phase 2.v2-B; Proposals
    moved up to a peer)."""
    html = _html()
    # The inner subtab declaration is wired via subInner(); the
    # promotion drops it. Check that no inner subtab references
    # proposals.
    assert not re.search(
        r'subInner\(this,\s*\'reports-alerts\',\s*\'proposals\'\)',
        html,
    ), (
        "Proposals must not exist as an inner subtab under Reports → "
        "Alerts — it's a top-level peer now"
    )


def test_proposals_top_level_page_body_present():
    """The top-level Reports → Proposals page body container must
    exist so the loader has a target. Uses the new id
    'reports-proposals-body' (vs the inner shape's
    'reports-alerts-proposals-body')."""
    html = _html()
    assert 'id="reports-proposals-body"' in html, (
        "Top-level Proposals page must declare its body container so "
        "the loader has a target"
    )
    # The page wrapper exists
    assert 'id="reports-proposals"' in html, (
        "Top-level Proposals subtab-page wrapper must exist"
    )


def test_proposals_subtab_carries_actionable_count_badge():
    """The top-level Proposals subtab needs a nav-badge for the queue
    count. Same pattern as the existing Alerts and Subscriptions
    badges."""
    html = _html()
    assert 'id="badge-reports-proposals"' in html, (
        "Top-level Proposals subtab needs id='badge-reports-proposals' "
        "(NOT the inner-shape id 'badge-reports-alerts-proposals')"
    )


def test_proposals_subtab_loader_exists():
    """_alLoadProposalsTab is the entry point for the new subtab. It
    must be defined; otherwise the subtab click throws."""
    html = _html()
    assert "async function _alLoadProposalsTab()" in html, (
        "_alLoadProposalsTab loader missing — clicking the Proposals "
        "subtab would throw ReferenceError"
    )


# ── Loader contract: write to top-level surface ─────────────────────────────


def test_loader_writes_to_top_level_body_container():
    """The loader must write its rendered HTML into the new top-level
    container 'reports-proposals-body', not the inner-subtab id from
    PR #2132."""
    body = _function_body("_alLoadProposalsTab")
    assert "reports-proposals-body" in body, (
        "Loader must write to the top-level Proposals body container"
    )
    assert "reports-alerts-proposals-body" not in body, (
        "Old inner-subtab body id must not appear — clean up the "
        "rename to avoid writing to a non-existent container on the "
        "first render after the promotion"
    )


def test_loader_writes_to_top_level_badge():
    """Badge id matches the new top-level Proposals subtab."""
    body = _function_body("_alLoadProposalsTab")
    assert "badge-reports-proposals" in body, (
        "Loader must update the top-level Proposals badge id"
    )
    assert "badge-reports-alerts-proposals" not in body, (
        "Old inner-subtab badge id must not appear in the loader"
    )


# ── Proposals routing + dedup contract (preserved from PR #2132) ────────────


def test_proposals_loader_fetches_from_arbiter():
    """The loader fetches /api/arbiter/proposals with the actionable
    subdirs included — same data source as the prior shapes."""
    body = _function_body("_alLoadProposalsTab")
    assert "/api/arbiter/proposals" in body, (
        "Loader must fetch from /api/arbiter/proposals"
    )
    assert "pending,snoozed" in body, (
        "Loader should include only the actionable subdirs"
    )


def test_proposals_loader_applies_surface_routing():
    """Only surface ∈ {firing, drift, cleanup} OR null lands here.
    surface=improvement remains on the Recommendations page."""
    body = _function_body("_alLoadProposalsTab")
    assert re.search(
        r"_ALERT_SURFACES\s*=\s*new Set\(\[\s*'firing'\s*,\s*'drift'\s*,\s*'cleanup'\s*\]\)",
        body,
    ), (
        "Surface filter must be {firing, drift, cleanup} — same set the "
        "prior shapes used"
    )
    assert re.search(r"!p\.surface\s*\|\|\s*_ALERT_SURFACES\.has", body), (
        "Null-surface catchall must be preserved so audit_poller's "
        "app_audit_tier3 findings still land on Proposals"
    )


def test_proposals_loader_dedupes_linked_proposals():
    """The loader hides proposals already represented via an
    actively-firing Signal's paired-row Act button. Same dedup rule as
    PR #2085 — preserved across the tab restructure."""
    body = _function_body("_alLoadProposalsTab")
    assert "linkedPropIds" in body, (
        "Loader must compute the linked-proposal id set from Signals' "
        "motivated_proposals_view"
    )
    assert "standaloneProps" in body, (
        "Loader must filter inAlerts -> standaloneProps before "
        "rendering"
    )
    assert re.search(r"_ACTIONABLE\s*=\s*new Set\(\[", body), (
        "Loader must define the actionable-status set so terminal "
        "linked proposals don't accidentally suppress a Proposal row "
        "that should still render"
    )


def test_firing_lane_refreshes_proposals_badge_on_top_level_id():
    """When the operator reloads the Firing lane, the Proposals badge
    should refresh — same as PR #2132 but the existence check must
    use the new top-level id, not the inner-shape id."""
    body = _function_body("_alLoadLane")
    assert "_alLoadProposalsTab" in body, (
        "Firing-lane render path must call _alLoadProposalsTab() at "
        "the end of the maintenance branch so the Proposals badge "
        "stays current"
    )
    assert "reports-proposals-body" in body, (
        "Existence check must reference the top-level body container "
        "id — referencing the obsolete inner-shape id would short-"
        "circuit the refresh and leave the badge stale"
    )


# ── Paired-row Act buttons preserved (PR #2085) ─────────────────────────────


def test_paired_row_act_button_helper_preserved():
    """The promotion must NOT remove the inline paired-row Act button
    from Signal rows on the Firing lane — that's how the operator
    acts on a Signal+Proposal pair without navigating tabs. The
    helper from PR #2085 (_alPairedAct) must remain."""
    html = _html()
    assert "async function _alPairedAct(propId, btn)" in html, (
        "_alPairedAct paired-row helper from PR #2085 must remain — "
        "removing it would force the operator to context-switch to "
        "Reports → Proposals to act on a linked Signal+Proposal pair"
    )
