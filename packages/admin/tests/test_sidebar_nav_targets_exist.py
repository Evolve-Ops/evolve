"""
Sidebar navigation smoke tests — updated through V2.2-3.

Asserts structural invariants of the sidebar after the 3-bucket IA reorg
(V2.2-1) and page-folding (V2.2-3):

  1. Every data-page="..." on a nav item has a matching id="page-..." div.
  2. The sidebar has exactly 3 nav-sections: Operate, Improve, Settings.
  3. Overview is a nav-item OUTSIDE any nav-section (floats above all buckets).
  4. pageRedirectRegistry is declared; resolveDestination() is wired in nav().
  5. V2.2-3 redirect entries map dissolved pages to their new canonical homes.
  6. The Improve section contains the consolidated Apps nav item (not the
     dissolved capability/gallery/forge nav items that are now subtabs).
  7. The Operate section does not contain the items that moved to Improve.
"""
import re
from pathlib import Path

_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_ROUTER_JS = _WEB / "static" / "js" / "core" / "router.js"


def _load_html() -> str:
    # pageRedirectRegistry + resolveDestination + nav now live in router.js
    # (Phase 2a of the source split). Concat so the existing regex-shape
    # assertions stay valid.
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _ROUTER_JS.read_text(encoding="utf-8")
    )


def test_every_nav_data_page_has_matching_page_element():
    """Each data-page value in the sidebar must have a matching page-<id> div."""
    html = _load_html()

    # All data-page values from nav items
    nav_pages = re.findall(r'<div class="nav-item[^"]*"[^>]*data-page="([^"]+)"', html)
    assert nav_pages, "No nav items with data-page found"

    # All page-level div ids
    page_ids = re.findall(r'id="page-([^"]+)"', html)
    page_id_set = set(page_ids)

    missing = [p for p in nav_pages if p not in page_id_set]
    assert not missing, (
        f"Nav items reference page-ids with no matching <div id=\"page-...\">: {missing}"
    )


def test_sidebar_has_exactly_three_buckets():
    """After V2.2-1 the sidebar must have exactly 3 nav-sections."""
    html = _load_html()
    sections = re.findall(r'<div class="nav-section">([^<]+)</div>', html)
    assert sections == ["Operate", "Improve", "Settings"], (
        f"Expected ['Operate', 'Improve', 'Settings'] but found {sections}"
    )


def test_overview_is_outside_any_nav_section():
    """Overview nav-item must appear before the first nav-section."""
    html = _load_html()
    overview_pos = html.find('data-page="overview"')
    first_section_pos = html.find('<div class="nav-section">')
    assert overview_pos != -1, "Overview nav item not found"
    assert first_section_pos != -1, "No nav-sections found"
    assert overview_pos < first_section_pos, (
        "Overview nav item appears AFTER the first nav-section — it must float above all buckets"
    )


def test_redirect_registry_declared():
    """pageRedirectRegistry must be declared in JS."""
    html = _load_html()
    assert "const pageRedirectRegistry" in html, (
        "pageRedirectRegistry not declared in index.html"
    )


def test_resolve_destination_declared():
    """resolveDestination function must be declared in JS."""
    html = _load_html()
    assert "function resolveDestination(" in html, (
        "resolveDestination function not found in index.html"
    )


def test_nav_calls_resolve_destination():
    """nav() function must call resolveDestination before navigating."""
    html = _load_html()
    # Find the nav function body, bounded by its own closing brace at column 0.
    # (Phase 2a moved nav() into router.js where it's the last declaration in
    # the file, so the original `\nfunction ` lookahead no longer fires.)
    nav_match = re.search(r'function nav\(el\)\s*\{(.+?)\n\}', html, re.DOTALL)
    assert nav_match, "Could not find nav() function body"
    body = nav_match.group(1)
    assert "resolveDestination(" in body, (
        "nav() does not call resolveDestination() — redirect hook not wired in"
    )


# V2.2-3 redirect registry tests
V223_EXPECTED_REDIRECTS = {
    "capabilities": ("apps", "installed"),
    "gallery":      ("apps", "gallery"),
    "forge":        ("apps", "forge-jobs"),
    "monitoring":   ("cost", "sessions"),
    "recovery":     ("maintenance", "recovery"),
    "modules":      ("settings", "modules"),
    "config":       ("settings", "pod-config"),
    "alerts":       ("maintenance", "alerts"),
}


def test_v223_redirect_registry_has_all_folded_pages():
    """V2.2-3 must populate pageRedirectRegistry for all 8 dissolved pages."""
    html = _load_html()
    for old_id, (new_page, subtab) in V223_EXPECTED_REDIRECTS.items():
        assert f'"{old_id}"' in html, (
            f'pageRedirectRegistry missing entry for "{old_id}"'
        )
        # Verify the entry points to the correct destination page
        assert new_page in html, f'Destination page "{new_page}" not found in HTML'


def test_v223_dissolved_pages_have_stub_divs():
    """Every dissolved page must still have a page-<id> div (as redirect stub)."""
    html = _load_html()
    page_ids = set(re.findall(r'id="page-([^"]+)"', html))
    for old_id in V223_EXPECTED_REDIRECTS:
        assert old_id in page_ids, (
            f'Dissolved page "{old_id}" has no page-{old_id} stub div. '
            f'Old deeplinks would 404.'
        )


def test_improve_bucket_contains_apps_not_dissolved_items():
    """V2.2-3: Improve section must have Apps nav item; capabilities/gallery/forge/monitoring
    are dissolved into subtabs and must NOT appear as standalone nav items in the sidebar."""
    html = _load_html()
    improve_match = re.search(
        r'<div class="nav-section">Improve</div>(.+?)<div class="nav-section">Settings</div>',
        html, re.DOTALL
    )
    assert improve_match, "Could not find Improve section bounded by Settings"
    improve_block = improve_match.group(1)

    # Apps (consolidated) must be in Improve
    assert 'data-page="apps"' in improve_block, (
        'Apps nav item not found in Improve section'
    )
    # self-improvement, ai-optimization, cost-measures must still be there
    for page_id in ["skills", "self-improvement", "ai-optimization", "cost-measures"]:
        assert f'data-page="{page_id}"' in improve_block, (
            f'Page "{page_id}" not found in Improve section'
        )

    # Dissolved pages must NOT appear as separate nav items in the sidebar
    for dissolved in ["capabilities", "gallery", "forge", "monitoring"]:
        assert f'data-page="{dissolved}"' not in improve_block, (
            f'Dissolved page "{dissolved}" still appears as a nav item in Improve — '
            f'it should be a subtab inside Apps (or Usage), not a standalone nav item'
        )


def test_operate_bucket_does_not_contain_extend_items():
    """ai-optimization and cost-measures must NOT be in the Operate section."""
    html = _load_html()
    operate_match = re.search(
        r'<div class="nav-section">Operate</div>(.+?)<div class="nav-section">Improve</div>',
        html, re.DOTALL
    )
    assert operate_match, "Could not find Operate section bounded by Improve"
    operate_block = operate_match.group(1)

    should_not_be_in_operate = ["ai-optimization", "cost-measures",
                                 "skills", "capabilities", "gallery", "forge"]
    for page_id in should_not_be_in_operate:
        assert f'data-page="{page_id}"' not in operate_block, (
            f'Page "{page_id}" found in Operate section — should have moved to Improve'
        )


def test_settings_bucket_has_single_settings_item():
    """V2.2-3: Settings bucket must have exactly the 'settings' nav item (modules+config merged)."""
    html = _load_html()
    settings_match = re.search(
        r'<div class="nav-section">Settings</div>(.+?)</div>\s*$',
        html, re.DOTALL
    )
    # Just verify settings nav item exists and modules/config don't appear as sidebar items
    assert 'data-page="settings"' in html, "Settings nav item not found"
    # Find the Settings section block — from Settings nav-section to end of sidebar
    settings_section_pos = html.find('<div class="nav-section">Settings</div>')
    sidebar_footer_pos = html.find('<div class="sidebar-footer">')
    assert settings_section_pos != -1, "Settings nav-section not found"
    assert sidebar_footer_pos != -1, "sidebar-footer not found"
    settings_block = html[settings_section_pos:sidebar_footer_pos]
    assert 'data-page="modules"' not in settings_block, (
        'Modules still appears as a standalone nav item — should be a tab inside Settings'
    )
    assert 'data-page="config"' not in settings_block, (
        'Config still appears as a standalone nav item — should be a tab inside Settings'
    )
