"""
V2.2-3 page-folding regression tests.

Verifies that the seven page-dissolution operations produced correct HTML
structure:

  1. page-apps has 4 subtabs: apps, discovered, gallery, activity
  2. page-cost has a sessions subtab (monitoring folded in)
  3. page-maintenance has alerts/alert-history/thresholds/recovery subtabs
  4. page-settings has modules and pod-config subtabs
  5. page-reports exists with subscriptions/schedule/tracked subtabs
  6. Every dissolved page has a redirect stub (page-<id> div still exists)
  7. pageRedirectRegistry has an entry for each dissolved page
  8. The correct subtab IDs exist for each new page so nav() redirect lands
  9. No duplicate element IDs (critical: getElementById must be unambiguous)

NOTE: The UI has continued to evolve past V2.2-3 — alerts/alert-history/
thresholds moved from page-maintenance to page-reports, "tracked" was
renamed to "watchlist", "schedule" was absorbed into per-subscription
config, and AL-1.8a replaced the Apps page's Installed / Create /
Forge Jobs subtabs with the lifecycle (apps / discovered / gallery /
activity — docs/design-apps-surface-2026-08-16.md §3). Tests covering those specific subtabs are now @pytest.mark.skip
because they pin a structure that no longer exists. The redirect map
and ID-uniqueness checks were updated to the post-V2.2-3 layout.
"""
import re
from pathlib import Path

import pytest

_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_ROUTER_JS = _WEB / "static" / "js" / "core" / "router.js"


def _load_html() -> str:
    # pageRedirectRegistry now lives in router.js (Phase 2a of the
    # source split). Concat so the existing regex-shape assertions
    # stay valid.
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _ROUTER_JS.read_text(encoding="utf-8")
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Apps page structure
# ──────────────────────────────────────────────────────────────────────────────

def test_apps_page_exists():
    html = _load_html()
    assert 'id="page-apps"' in html, "page-apps div not found"


def test_apps_page_has_apps_subtab():
    """AL-1.8a: the pod-first Apps list is what "installed" became — one row
    per app across the pod rather than a per-bot grid."""
    html = _load_html()
    # The subtab button must have data-subtab="apps" inside page-apps
    apps_match = re.search(
        r'id="page-apps"(.+?)(?=<div class="page"|$)',
        html, re.DOTALL
    )
    assert apps_match, "page-apps block not found"
    block = apps_match.group(1)
    assert 'data-subtab="apps"' in block, (
        'Apps page missing "apps" subtab button'
    )
    assert 'id="apps-apps"' in block, (
        'Apps page missing subtab-page div id="apps-apps"'
    )
    # And the retired ones stay retired — a re-add would resurrect the
    # bot-first surface the pod-first page replaced.
    assert 'data-subtab="installed"' not in block, 'retired "installed" subtab is back'
    assert 'data-subtab="create"' not in block, 'retired "create" subtab is back'


def test_apps_page_has_discovered_subtab():
    html = _load_html()
    apps_match = re.search(r'id="page-apps"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert apps_match
    block = apps_match.group(1)
    assert 'data-subtab="discovered"' in block, 'Apps page missing "discovered" subtab'
    assert 'id="apps-discovered"' in block, 'Apps page missing id="apps-discovered"'


def test_apps_page_has_gallery_subtab():
    html = _load_html()
    apps_match = re.search(r'id="page-apps"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert apps_match
    block = apps_match.group(1)
    assert 'data-subtab="gallery"' in block, 'Apps page missing "gallery" subtab'
    assert 'id="apps-gallery"' in block, 'Apps page missing id="apps-gallery"'


def test_apps_page_has_activity_subtab():
    """AL-1.8a: Activity replaced the Forge Jobs subtab and absorbed its
    table + approval panel, so the forge surface must still be on the page."""
    html = _load_html()
    apps_match = re.search(r'id="page-apps"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert apps_match
    block = apps_match.group(1)
    assert 'data-subtab="activity"' in block, 'Apps page missing "activity" subtab'
    assert 'id="apps-activity"' in block, 'Apps page missing id="apps-activity"'
    assert 'id="forge-jobs-table"' in block, (
        'the forge job table did not survive the move into Activity'
    )
    assert 'id="forge-approval-panel"' in block, (
        'the forge approval panel did not survive the move into Activity'
    )


def test_apps_page_no_reliability_subtab():
    """The Apps "Testing"/reliability subtab was removed — it fed off the dead
    /api/pod/reliability endpoint (deleted with the app-test surface, #2480) and
    flooded the admin console with 404s. Guard against it being re-added."""
    html = _load_html()
    apps_match = re.search(r'id="page-apps"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert apps_match
    block = apps_match.group(1)
    assert 'data-subtab="reliability"' not in block, 'Dead "reliability" subtab is back'
    assert 'id="apps-reliability"' not in block, 'Dead id="apps-reliability" is back'


def test_apps_load_reliability_removed():
    """appsLoadReliability() and the /api/pod/reliability fetch it wrapped were
    removed as dead code — the backend route no longer exists, so any call only
    produces a 404. Guard against re-adding either."""
    html = _load_html()
    assert 'function appsLoadReliability(' not in html, (
        'appsLoadReliability was re-added — its backend route /api/pod/reliability '
        'no longer exists, so it only produces 404s'
    )
    assert '/api/pod/reliability' not in html, (
        '/api/pod/reliability fetch re-added — the route was deleted in #2480'
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Usage page — Sessions subtab
# ──────────────────────────────────────────────────────────────────────────────

def test_cost_page_has_sessions_subtab():
    html = _load_html()
    cost_match = re.search(r'id="page-cost"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert cost_match, "page-cost block not found"
    block = cost_match.group(1)
    assert 'data-subtab="sessions"' in block, 'Usage page missing "sessions" subtab button'
    assert 'id="cost-sessions"' in block, 'Usage page missing id="cost-sessions"'


def test_cost_page_has_spend_subtab():
    html = _load_html()
    cost_match = re.search(r'id="page-cost"(.+?)(?=<div class="page"|$)', html, re.DOTALL)
    assert cost_match
    block = cost_match.group(1)
    assert 'data-subtab="spend"' in block, 'Usage page missing "spend" subtab (original content)'
    assert 'id="cost-spend"' in block, 'Usage page missing id="cost-spend"'


# ──────────────────────────────────────────────────────────────────────────────
# 3. Maintenance page — alerts/history/thresholds/recovery subtabs
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Alerts moved from page-maintenance to page-reports after V2.2-3. "
                  "See test_reports_page_has_alerts_subtab for the new home.")
def test_maintenance_page_has_alerts_subtab():
    pass


@pytest.mark.skip(reason="alert-history was absorbed into the reports/alerts unified view "
                  "after V2.2-3 — no longer a standalone subtab.")
def test_maintenance_page_has_alert_history_subtab():
    pass


@pytest.mark.skip(reason="Thresholds tab merged into per-subscription configuration in "
                  "the post-V2.2-3 IA — no standalone subtab.")
def test_maintenance_page_has_thresholds_subtab():
    pass


def test_reports_page_has_alerts_subtab():
    """Alerts subtab is on page-reports post-V2.2-3."""
    html = _load_html()
    reports_match = re.search(
        r'id="page-reports"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert reports_match, "page-reports block not found"
    block = reports_match.group(1)
    assert 'data-subtab="alerts"' in block, 'Reports page missing "alerts" subtab'


def test_maintenance_page_has_recovery_subtab():
    html = _load_html()
    maint_match = re.search(
        r'id="page-maintenance"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert maint_match
    block = maint_match.group(1)
    assert 'data-subtab="recovery"' in block, 'Maintenance missing "recovery" subtab'
    assert 'id="maintenance-recovery"' in block, 'Maintenance missing id="maintenance-recovery"'


# ──────────────────────────────────────────────────────────────────────────────
# 4. Settings page — modules + pod-config subtabs
# ──────────────────────────────────────────────────────────────────────────────

def test_settings_page_exists():
    html = _load_html()
    assert 'id="page-settings"' in html, "page-settings div not found"


def test_settings_page_has_modules_subtab():
    html = _load_html()
    settings_match = re.search(
        r'id="page-settings"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert settings_match, "page-settings block not found"
    block = settings_match.group(1)
    assert 'data-subtab="modules"' in block, 'Settings page missing "modules" subtab'
    assert 'id="settings-modules"' in block, 'Settings page missing id="settings-modules"'


def test_settings_page_has_pod_config_subtab():
    html = _load_html()
    settings_match = re.search(
        r'id="page-settings"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert settings_match
    block = settings_match.group(1)
    assert 'data-subtab="pod-config"' in block, 'Settings page missing "pod-config" subtab'
    assert 'id="settings-pod-config"' in block, 'Settings page missing id="settings-pod-config"'


def test_settings_page_has_bots_subtab():
    """The 2026-06-01 restructure promoted the formerly-nested Pod
    Config → Bot subtab to a top-level Settings → Bots sibling so chip
    deep-links (e.g. the pushback chip's "Open this bot's settings"
    remediation) can land on the per-bot view without three-level
    nesting. Both the subtab affordance and the subtab-page wrapper
    must be present.
    """
    html = _load_html()
    settings_match = re.search(
        r'id="page-settings"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert settings_match, "page-settings block not found"
    block = settings_match.group(1)
    assert 'data-subtab="bots"' in block, 'Settings page missing "bots" subtab'
    assert 'id="settings-bots"' in block, 'Settings page missing id="settings-bots"'


def test_settings_pod_config_has_pod_context_card():
    """Pod Config holds the pod-wide configuration cards. Pod Context
    (the read-only system-state card — hostname, admin URL, etc.) must
    still render here; it was migrated up from the retired
    #config-identity sub-tab during the 2026-05-29 Users-page split.

    The old inner Network/Bot subtabs were retired in the 2026-06-01
    restructure (Bot promoted to top-level Bots sibling; Network
    cards promoted to direct children of Pod Config). #config-network
    and #config-bot must NOT exist on the page anymore.
    """
    html = _load_html()
    pod_config_match = re.search(
        r'id="settings-pod-config"(.+?)<!-- /settings-pod-config -->',
        html, re.DOTALL
    )
    assert pod_config_match, "settings-pod-config block not found"
    block = pod_config_match.group(1)
    # Pod Context card must still render — it's pod-wide system state.
    assert 'id="identity-pod-context-card"' in block, (
        'Pod Context card should still render in Pod Config '
        '(migrated up from #config-identity).'
    )
    # Retired inner subtabs must not reappear.
    for stale in ("config-network", "config-bot", "config-identity"):
        assert f'id="{stale}"' not in block, (
            f'#{stale} should be removed in the 2026-06-01 restructure; '
            "the inner subtab structure inside Pod Config has been flattened."
        )


# ──────────────────────────────────────────────────────────────────────────────
# 5. Reports page — subscriptions/schedule/tracked subtabs
# ──────────────────────────────────────────────────────────────────────────────

def test_reports_page_exists():
    html = _load_html()
    assert 'id="page-reports"' in html, "page-reports div not found"


def test_reports_page_has_subscriptions_subtab():
    html = _load_html()
    reports_match = re.search(
        r'id="page-reports"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert reports_match, "page-reports block not found"
    block = reports_match.group(1)
    assert 'data-subtab="subscriptions"' in block, 'Reports missing "subscriptions" subtab'
    assert 'id="reports-subscriptions"' in block, 'Reports missing id="reports-subscriptions"'


@pytest.mark.skip(reason="Schedule was absorbed into per-subscription config after V2.2-3 — "
                  "no standalone subtab on page-reports.")
def test_reports_page_has_schedule_subtab():
    pass


def test_reports_page_has_watchlist_subtab():
    """V2.2-3's 'tracked' subtab was renamed to 'watchlist' in the post-V2.2-3 IA."""
    html = _load_html()
    reports_match = re.search(
        r'id="page-reports"(.+?)(?=<div class="page"|<!-- ══|$)',
        html, re.DOTALL
    )
    assert reports_match
    block = reports_match.group(1)
    assert 'data-subtab="watchlist"' in block, 'Reports missing "watchlist" subtab'
    assert 'id="reports-watchlist"' in block, 'Reports missing id="reports-watchlist"'


# ──────────────────────────────────────────────────────────────────────────────
# 6. Dissolved pages have redirect stub divs
# ──────────────────────────────────────────────────────────────────────────────

DISSOLVED_PAGES = [
    "capabilities", "gallery", "forge",  # → apps subtabs
    "monitoring",                          # → cost·sessions
    "recovery",                            # → maintenance·recovery
    "modules", "config",                   # → settings subtabs
    "alerts",                              # → maintenance·alerts
]


def test_all_dissolved_pages_have_stub_divs():
    """Each dissolved page must have a page-<id> div so getElementById never returns null."""
    html = _load_html()
    page_ids = set(re.findall(r'id="page-([^"]+)"', html))
    missing = [p for p in DISSOLVED_PAGES if p not in page_ids]
    assert not missing, (
        f"These dissolved pages have no stub div — old nav links would break: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 7. pageRedirectRegistry entries
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_REDIRECT_MAP = {
    # AL-1.8a: "installed" became the pod-first Apps list and "forge-jobs"
    # became Activity, so the old deeplinks land on the tab that inherited
    # their content.
    "capabilities": ("apps",        "apps"),
    "gallery":      ("apps",        "gallery"),
    "forge":        ("apps",        "activity"),
    "monitoring":   ("cost",        "sessions"),
    "recovery":     ("maintenance", "recovery"),
    "modules":      ("settings",    "modules"),
    "config":       ("settings",    "pod-config"),
    # alerts moved from maintenance to reports in the post-V2.2-3 IA
    "alerts":       ("reports",     "alerts"),
}


def test_redirect_registry_has_all_v223_entries():
    """pageRedirectRegistry must have an entry for each dissolved page."""
    html = _load_html()
    for old_id, (new_page, subtab) in EXPECTED_REDIRECT_MAP.items():
        assert f'"{old_id}"' in html, (
            f'pageRedirectRegistry missing key "{old_id}"'
        )
        assert new_page in html, (
            f'Destination page "{new_page}" referenced by "{old_id}" redirect not found'
        )
        assert subtab in html, (
            f'Destination subtab "{subtab}" referenced by "{old_id}" redirect not found'
        )


# ──────────────────────────────────────────────────────────────────────────────
# 8. Subtab ID contract — nav() redirect uses data-subtab to click the tab
# ──────────────────────────────────────────────────────────────────────────────

def test_nav_redirect_subtab_targets_exist():
    """
    For each redirect entry, the destination page must contain a subtab element
    with the matching data-subtab attribute. nav() does:
      document.querySelector(`#page-${dest.page} .subtab[data-subtab="${dest.subtab}"]`)
    If that element is absent, the redirect silently fails to activate the right tab.
    """
    html = _load_html()
    for old_id, (new_page, subtab) in EXPECTED_REDIRECT_MAP.items():
        page_block_match = re.search(
            rf'id="page-{re.escape(new_page)}"(.+?)(?=<div class="page"|<!-- ══|$)',
            html, re.DOTALL
        )
        assert page_block_match, (
            f'Destination page "page-{new_page}" block not found in HTML'
        )
        block = page_block_match.group(1)
        assert f'data-subtab="{subtab}"' in block, (
            f'Redirect for "{old_id}" → {new_page}·{subtab}: '
            f'data-subtab="{subtab}" not found in #page-{new_page}. '
            f'nav() redirect would fail to activate the correct tab.'
        )


# ──────────────────────────────────────────────────────────────────────────────
# 9. No duplicate element IDs (spot-check critical IDs)
# ──────────────────────────────────────────────────────────────────────────────

UNIQUE_IDS = [
    # Subtab-page IDs that must appear exactly once
    "apps-apps", "apps-discovered", "apps-gallery", "apps-activity",
    "cost-sessions", "cost-spend",
    # post-V2.2-3: alerts/alert-history/thresholds moved off maintenance;
    # tracked was renamed to watchlist; schedule was absorbed into
    # per-subscription config. Updated list reflects the current IA.
    "maintenance-recovery",
    "settings-modules", "settings-pod-config",
    "reports-subscriptions", "reports-alerts", "reports-watchlist",
    # Key DOM singletons
    "modules-grid", "al-subs-body",
]


def test_critical_ids_are_unique():
    """Each critical element ID must appear exactly once in the HTML."""
    html = _load_html()
    for eid in UNIQUE_IDS:
        count = html.count(f'id="{eid}"')
        assert count == 1, (
            f'Element id="{eid}" appears {count} times — duplicate IDs break getElementById'
        )
