"""tests/test_surface_routing.py — Phase 2.v2-A: route proposals by
``surface`` field.

Spec: docs/spec-recommendations-rework-2026-06-02.md §"Course
correction 2026-06-04".

Course-corrected Phase 2 ships the routing as a *client-side filter*
on the existing Recommendations + Alerts pages — no new pages, no
sidebar additions. Recommendations shows proposals whose charter
``surface`` is ``improvement`` (or null, the unclassified catchall
during transition). Proposals with ``surface ∈ {firing, drift,
cleanup}`` are sysadmin / hygiene findings and route to the Alerts
page (specifically the maintenance lane, which mirrors to both the
legacy Maintenance Alerts subtab and Reports → Alerts).

The tests pin:

  1. Server: /api/arbiter/proposals attaches ``surface`` to each view.
  2. Client → Recommendations: inbox filter keeps only ``improvement``
     or null. The renderer gets a count of routed-away proposals and
     surfaces a breadcrumb pointing to Alerts.
  3. Client → Alerts: the maintenance lane fetches proposals,
     filters by surface, and renders them coalesced via the existing
     PR #1 coalescer in a "Generator proposals" cluster below the
     signal list.
  4. Activity lane does NOT render proposals (different signal
     category — session-economics — proposals don't belong there).
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"
_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
SERVER_PY = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "server.py"
_ROUTES_ARBITER_PY = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_arbiter.py"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


def _server() -> str:
    # routes_arbiter.py holds the arbiter route implementations extracted
    # from server.py; include it so source-search tests still find the code.
    arbiter_src = _ROUTES_ARBITER_PY.read_text(encoding="utf-8") if _ROUTES_ARBITER_PY.exists() else ""
    return SERVER_PY.read_text(encoding="utf-8") + "\n" + arbiter_src + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


# ── Server: surface map joins the proposals response ────────────────────


def test_server_builds_surface_map_alongside_authority_map():
    """The proposals API already walks the registry to build an
    authority map. Adding a parallel surface_map in the same loop
    avoids a second registry pass."""
    src = _server()
    # The surface_map declaration must be present alongside the
    # authority_map block in api_arbiter_proposals.
    m = re.search(
        r"def api_arbiter_proposals.*?surface_map\s*:\s*dict\[str,\s*str\s*\|\s*None\]\s*=\s*\{\}",
        src, re.DOTALL,
    )
    assert m, (
        "surface_map declaration missing from api_arbiter_proposals — "
        "the proposal view can't surface charter.surface to the client "
        "without it"
    )
    # And the population must happen inside the same loop body that
    # populates authority_map.
    assert re.search(
        r"surface_map\[gid\]\s*=\s*loaded\.charter\.surface",
        src,
    ), "surface_map must read loaded.charter.surface inside the registry loop"


def test_proposal_view_attaches_surface():
    """Each proposal view in the API response must include the
    ``surface`` field so the client routing can read it. The 2026-06-05
    RSI eligibility spec added a per-finding override: the proposal's
    own ``p.surface`` wins over the charter-derived fallback. The
    expression here pins both halves — without the OR clause, the
    override silently doesn't take effect and findings like
    session_token_outlier route back to Recommendations."""
    src = _server()
    # The attach happens in the per-proposal view-building loop after
    # _proposal_to_view(). Both forms must be present in the assignment.
    assert re.search(
        r'view\["surface"\]\s*=\s*p\.surface\s+or\s+surface_map\.get\(p\.generator_id\)',
        src,
    ), (
        "view['surface'] = p.surface or surface_map.get(p.generator_id) "
        "missing — without the override, anomaly findings inside "
        "improvement-charter generators route back to Recommendations. "
        "See docs/spec-rsi-proposal-eligibility-2026-06-05.md."
    )


# ── Client: Recommendations filters to improvement-or-null ──────────────


def test_recommendations_inbox_filters_to_improvement_only():
    """The inbox load filters by charter.surface — keep ONLY proposals
    with surface='improvement'. Slice 1B (2026-06-04) flipped the
    catchall: null/unclassified now routes to Alerts, not
    Recommendations. The motivating finding was that audit_poller's
    app_audit_tier3 emissions (broken-install + missing-cron forge
    findings) carried surface=null and were polluting the
    Recommendations queue — they belong with sysadmin hygiene."""
    html = _html()
    m = re.search(
        r"const _isRecommendable\s*=\s*p\s*=>\s*(.+?);",
        html,
    )
    assert m, "_isRecommendable filter predicate missing"
    body = m.group(1)
    assert "p.surface" in body and "'improvement'" in body, (
        "filter must keep p.surface === 'improvement'"
    )
    # The post-flip rule explicitly rejects null surface. The old
    # `!p.surface ||` catchall clause must be gone.
    assert "!p.surface" not in body, (
        "Slice 1B flipped the catchall — null/undefined surface must "
        "NOT survive the Recommendations filter anymore (it now lands "
        "on Alerts via the inverse rule in _alLoadLane)"
    )


def test_recommendations_renderer_signals_routed_count():
    """When proposals are filtered out by surface, the
    renderArbiterProposals call passes the routed-away count so the
    UI can render a breadcrumb."""
    html = _html()
    # routedToAlerts must be passed; additional opts keys (e.g. observations,
    # added by the Effectiveness-Layer triage) are allowed.
    pattern = (
        r"renderArbiterProposals\(\s*inbox\s*,\s*inProcess\s*,\s*\{\s*routedToAlerts\b[^}]*\}\s*\)"
    )
    assert re.search(pattern, html), (
        "renderArbiterProposals must receive {routedToAlerts, …} so the "
        "page can tell the operator where the sysadmin proposals went"
    )


def test_recommendations_routed_note_dom_present():
    """A small note above the proposals list says 'N routed to
    Alerts' with a link. The dom element id is read by
    renderArbiterProposals; missing element means the renderer
    silently no-ops on the breadcrumb."""
    html = _html()
    assert 'id="arbiter-routed-note"' in html, (
        "arbiter-routed-note container missing — the breadcrumb has "
        "nowhere to render"
    )


def test_recommendations_routed_note_links_to_reports():
    """The breadcrumb link clicks through to the Reports nav item
    (which contains the Alerts subtab). Without the link the
    operator can't navigate to where the proposals went."""
    html = _html()
    # The renderer constructs the link inside renderArbiterProposals.
    fn = re.search(
        r"function renderArbiterProposals\(inbox, inProcess[^)]*\)\s*\{(.+?)\n}",
        html, re.DOTALL,
    )
    assert fn, "renderArbiterProposals function not located"
    body = fn.group(1)
    assert 'routedNoteEl' in body, "routedNoteEl reference missing"
    assert 'data-page=&quot;reports&quot;' in body or 'data-page="reports"' in body, (
        "breadcrumb must link to the Reports nav item — that's where the "
        "Alerts subtab lives in the IA"
    )


# ── Client: Alerts maintenance lane renders proposals cluster ───────────


def test_alerts_maintenance_lane_fetches_proposals():
    """The maintenance lane (which mirrors to both Maintenance Alerts
    and Reports → Alerts) must fetch /api/arbiter/proposals during
    its render so the sysadmin proposals appear alongside Signals."""
    html = _html()
    # The fetch is gated on apiFlavor === 'maintenance' so the
    # activity lane (different signal category) doesn't pull
    # proposals in.
    fn = re.search(
        r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_alLoadLane function not located"
    body = fn.group(1)
    assert "apiFlavor === 'maintenance'" in body, (
        "proposals fetch must be gated to the maintenance lane — "
        "activity lane is session-economics signals and proposals "
        "don't belong there"
    )
    assert "/api/arbiter/proposals" in body, (
        "_alLoadLane must fetch /api/arbiter/proposals to render the "
        "proposals cluster"
    )


def test_alerts_lane_filters_proposals_by_surface():
    """Proposals on Alerts are the inverse-routed set: surface in
    {firing, drift, cleanup} OR surface is null/unclassified. Only
    surface=improvement is excluded — those stay on Recommendations.

    Slice 1B (2026-06-04) flipped the null-catchall to Alerts: the
    motivating case is audit_poller's app_audit_tier3 findings, which
    emit without a charter (and therefore carry surface=null). They
    are broken-install / forge-emission hygiene findings, not
    improvements."""
    html = _html()
    fn = re.search(
        r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_alLoadLane function not located"
    body = fn.group(1)
    # The set must include all three "Alerts-bound" surfaces.
    m = re.search(r"_ALERT_SURFACES\s*=\s*new Set\(\[([^\]]+)\]\)", body)
    assert m, "_ALERT_SURFACES Set literal missing"
    contents = m.group(1)
    for required in ("'firing'", "'drift'", "'cleanup'"):
        assert required in contents, (
            f"_ALERT_SURFACES must include {required}"
        )
    # And surface=improvement must NOT be in the set (sanity check —
    # if someone adds it the routing breaks symmetrically).
    assert "'improvement'" not in contents, (
        "surface=improvement must NOT be in _ALERT_SURFACES — those "
        "proposals belong on Recommendations"
    )
    # The post-flip rule includes null in the Alerts set via the
    # `!p.surface ||` clause on the filter. Without it the
    # audit_poller / null-charter proposals would vanish entirely.
    filter_call = re.search(
        r"allProps\.filter\(\s*p\s*=>\s*(.+?)\s*\)",
        body, re.DOTALL,
    )
    assert filter_call, "Alerts-lane proposal filter call not located"
    filter_body = filter_call.group(1)
    assert "!p.surface" in filter_body, (
        "Alerts-lane filter must include null/unclassified surface via "
        "`!p.surface` — Slice 1B catchall flip. Without it the "
        "audit_poller findings disappear from BOTH surfaces."
    )


def test_alerts_lane_uses_proposal_coalescer():
    """The Alerts-rendered proposals reuse PR #1's coalescer so the
    same-root-cause-across-bots collapse works. Without this the
    sysadmin proposals appear as N flat rows instead of one
    expandable group."""
    html = _html()
    fn = re.search(
        r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_alLoadLane function not located"
    body = fn.group(1)
    assert "_propGroupProposals" in body, (
        "Alerts lane must call _propGroupProposals to coalesce the "
        "proposals (matches Recommendations + matches signal-side "
        "_alGroupSignals behavior)"
    )
    assert "_propGroupRow" in body, (
        "Alerts lane must render via _propGroupRow so the group UI "
        "(snooze-all / dismiss-all) is present"
    )


def test_alerts_lane_proposals_cluster_has_section_header():
    """The proposals cluster appears below the signals with a
    'Generator proposals' header, so the operator can tell which
    items are signals from monitors vs. proposals from generators."""
    html = _html()
    fn = re.search(
        r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_alLoadLane function not located"
    body = fn.group(1)
    assert "Generator proposals" in body, (
        "section header 'Generator proposals' missing — the cluster "
        "needs to be labeled or it reads as a continuation of signals"
    )


def test_recommendations_nav_badge_counts_visible_inbox():
    """Slice 1A (2026-06-04 operator feedback): the Recommendations
    nav badge and Overview tile count the surface-filtered Inbox
    (pending only), not the full pending set. The earlier full-pending
    count was a defensive hedge from PR #2048 — operator confirmed it
    just confused them (badge showed 42, Inbox showed 10).

    Pin both halves: (1) the badge/tile read from ``inbox.filter``,
    not the old ``all.filter(p => p.status === 'pending')`` count;
    (2) snoozed items don't inflate the badge — pending only.
    """
    html = _html()
    fn = re.search(
        r"async function loadArbiterProposals\([^)]*\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "loadArbiterProposals function not located"
    body = fn.group(1)
    # Positive pin: the badge counts the surface-filtered inbox,
    # pending only.
    assert re.search(
        r"const pending\s*=\s*inbox\.filter\(p\s*=>\s*p\.status\s*===\s*'pending'\)\.length",
        body,
    ), (
        "nav badge / Overview tile must count "
        "inbox.filter(p => p.status === 'pending').length — the "
        "surface-filtered Inbox is what the operator sees on the page"
    )
    # Negative pin: the old all-pending count must not still be
    # setting the badge.
    assert not re.search(
        r"const pending\s*=\s*all\.filter\(p\s*=>\s*p\.status\s*===\s*'pending'\)\.length",
        body,
    ), (
        "old `const pending = all.filter(p => p.status === 'pending')` "
        "still present — that was the defensive hedge from PR #2048 "
        "and confused operators (badge 42 vs Inbox 10)"
    )


def test_alerts_lane_skips_proposals_fetch_on_activity():
    """Activity lane is session-economics signals. Proposals don't
    apply there. The fetch is gated on apiFlavor === 'maintenance'."""
    html = _html()
    fn = re.search(
        r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_alLoadLane function not located"
    body = fn.group(1)
    # The proposals fetch must follow the apiFlavor === 'maintenance'
    # guard, not precede it. Find the line numbers of both and compare.
    guard_pos = body.find("apiFlavor === 'maintenance'")
    fetch_pos = body.find("/api/arbiter/proposals")
    assert guard_pos != -1, "apiFlavor === 'maintenance' guard missing"
    assert fetch_pos != -1, "proposals fetch missing"
    assert guard_pos < fetch_pos, (
        "proposals fetch must follow the apiFlavor==='maintenance' "
        "guard — activity lane shouldn't pull proposals"
    )
