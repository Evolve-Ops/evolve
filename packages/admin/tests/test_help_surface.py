"""
Help surface smoke tests.

Verifies that the three new Settings-bucket pages and their sidebar entries
exist in the expected form.
"""
import re
from pathlib import Path

INDEX_HTML = (
    Path(__file__).parent.parent
    / "evolve_admin" / "web" / "index.html"
)


def _load_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ── Page divs ───────────────────────────────────────────────────────────────

def test_getting_started_page_exists():
    """page-getting-started div must exist."""
    html = _load_html()
    assert 'id="page-getting-started"' in html, (
        "page-getting-started div not found in index.html"
    )


def test_setup_guide_page_exists():
    """page-setup-guide div must exist."""
    html = _load_html()
    assert 'id="page-setup-guide"' in html, (
        "page-setup-guide div not found in index.html"
    )


def test_help_page_exists():
    """page-help div must exist."""
    html = _load_html()
    assert 'id="page-help"' in html, (
        "page-help div not found in index.html"
    )


# ── Sidebar nav items ────────────────────────────────────────────────────────

def test_sidebar_has_getting_started_nav():
    """Sidebar must have a nav-item for getting-started."""
    html = _load_html()
    assert 'data-page="getting-started"' in html, (
        'Sidebar missing nav-item with data-page="getting-started"'
    )


def test_sidebar_has_setup_guide_nav():
    """Sidebar must have a nav-item for setup-guide."""
    html = _load_html()
    assert 'data-page="setup-guide"' in html, (
        'Sidebar missing nav-item with data-page="setup-guide"'
    )


def test_sidebar_has_help_nav():
    """Sidebar must have a nav-item for help."""
    html = _load_html()
    # The nav-item uses data-page="help"; the page also uses ?-based icon
    assert 'data-page="help"' in html, (
        'Sidebar missing nav-item with data-page="help"'
    )


def test_new_nav_items_in_settings_section():
    """All three new nav items must appear after the Settings nav-section label."""
    html = _load_html()
    settings_section_idx = html.find('<div class="nav-section">Settings</div>')
    assert settings_section_idx != -1, "Settings nav-section not found"
    # Grab the region from "Settings" to the sidebar-footer (where nav items
    # end). Using a sentinel rather than a fixed char count keeps the test
    # robust when new nav items land — e.g. Errors + Feedback (PR #1199)
    # pushed Help past the original 500-char window.
    sidebar_footer_idx = html.find('class="sidebar-footer"', settings_section_idx)
    assert sidebar_footer_idx > settings_section_idx, (
        "sidebar-footer not found after Settings nav-section"
    )
    region = html[settings_section_idx:sidebar_footer_idx]
    assert 'data-page="getting-started"' in region, (
        "getting-started nav-item not found in Settings section"
    )
    assert 'data-page="setup-guide"' in region, (
        "setup-guide nav-item not found in Settings section"
    )
    assert 'data-page="help"' in region, (
        "help nav-item not found in Settings section"
    )


# ── Summary bands ────────────────────────────────────────────────────────────

def test_getting_started_has_summary_band():
    """page-getting-started must have a .page-summary-band element."""
    html = _load_html()
    assert 'id="getting-started-summary"' in html, (
        "getting-started-summary band not found in index.html"
    )


def test_setup_guide_has_summary_band():
    """page-setup-guide must have a .page-summary-band element."""
    html = _load_html()
    assert 'id="setup-guide-summary"' in html, (
        "setup-guide-summary band not found in index.html"
    )


def test_help_has_summary_band():
    """page-help must have a .page-summary-band element."""
    html = _load_html()
    assert 'id="help-summary"' in html, (
        "help-summary band not found in index.html"
    )


# ── onPageActivate wiring ─────────────────────────────────────────────────────

def test_on_page_activate_comment_references_help_pages():
    """onPageActivate must have a comment or guard for the help surface pages."""
    html = _load_html()
    match = re.search(r'function onPageActivate\(page\)\s*\{(.+?)\n\}', html, re.DOTALL)
    assert match, "onPageActivate function not found in index.html"
    body = match.group(1)
    # Static pages don't need dynamic loads, but the function body must
    # contain a note explaining that (so maintainers don't add spurious loaders).
    assert 'getting-started' in body or 'Help surface' in body or 'static pages' in body, (
        "onPageActivate body has no mention of the help surface pages"
    )


# ── _helpToggleDoc helper ─────────────────────────────────────────────────────

def test_help_toggle_doc_function_defined():
    """_helpToggleDoc must be defined — used by accordion rows on Help page."""
    html = _load_html()
    assert 'function _helpToggleDoc(' in html, (
        "_helpToggleDoc function not found in index.html"
    )
