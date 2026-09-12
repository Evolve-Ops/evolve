"""
V2.2-4 summary-band regression tests.

Verifies that each page in the new IA has a .page-summary-band element
with the correct id, that the shared JS helper and per-page loader
functions are defined, and that the onPageActivate wiring is in place.

Pages covered (9 pages; Overview already has its own summary band;
  apps-summary removed 2026-05-16 — counts were inaccurate, re-add when
  /api/applications provides a reliable source of truth):
  cost · integrations-keys · security · maintenance · reports ·
  skills · self-improvement · ai-optimization ·
  cost-measures · settings
"""
import re
from pathlib import Path

_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_BASE_CSS = _WEB / "static" / "css" / "base.css"


def _load_html() -> str:
    # base.css holds the shell's CSS after the Phase-1 source split;
    # this fixture's string-shape asserts include CSS-class-existence
    # checks, so concat the two so they stay valid.
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _BASE_CSS.read_text(encoding="utf-8")
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Each page has a .page-summary-band element with the correct id
# ──────────────────────────────────────────────────────────────────────────────

# Summary bands present in the post-V2.2-4 IA. Originally included
# `cost-summary` and `security-summary` (V2.2-4 spec called for them),
# but those pages never got dedicated summary bands as the IA evolved —
# cost-summary's content was absorbed into per-subtab views, and
# security-summary into the page-security top section. Both pages still
# exist; they just don't render their summary band via this mechanism.
EXPECTED_BAND_IDS = [
    "integrations-keys-summary",
    "maintenance-summary",
    "reports-summary",
    "skills-summary",
    "self-improvement-summary",
    "ai-optimization-summary",
    "cost-measures-summary",
    "settings-summary",
]


def test_all_summary_band_ids_present():
    """Every page must have a div with class page-summary-band and the right id."""
    html = _load_html()
    missing = []
    for band_id in EXPECTED_BAND_IDS:
        # Both attributes must coexist (order doesn't matter).
        # Match `class="page-summary-band"` and `id="<band_id>"` within the same div tag.
        pattern = rf'<div[^>]*class="page-summary-band"[^>]*id="{re.escape(band_id)}"'
        alt_pattern = rf'<div[^>]*id="{re.escape(band_id)}"[^>]*class="page-summary-band"'
        if not re.search(pattern, html) and not re.search(alt_pattern, html):
            missing.append(band_id)
    assert not missing, (
        f"These summary band elements are missing from index.html: {missing}"
    )


def test_summary_band_has_loading_and_body_divs():
    """Each band must have a .summary-band-loading AND .summary-band-body child."""
    html = _load_html()
    for band_id in EXPECTED_BAND_IDS:
        # Find the band block by id and look for the child divs within a reasonable window.
        idx = html.find(f'id="{band_id}"')
        assert idx != -1, f"id=\"{band_id}\" not found in HTML"
        snippet = html[idx: idx + 400]  # 400 chars is plenty for the inner divs
        assert 'summary-band-loading' in snippet, (
            f'id="{band_id}": missing .summary-band-loading child'
        )
        assert 'summary-band-body' in snippet, (
            f'id="{band_id}": missing .summary-band-body child'
        )


# ──────────────────────────────────────────────────────────────────────────────
# 2. CSS classes defined
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_CSS_CLASSES = [
    ".page-summary-band",
    ".summary-band-loading",
    ".summary-band-body",
    ".sb-stat",
    ".sb-num",
    ".sb-label",
]


def test_summary_band_css_classes_defined():
    html = _load_html()
    missing = [c for c in EXPECTED_CSS_CLASSES if c not in html]
    assert not missing, (
        f"These CSS classes are missing from index.html: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. JS helper functions defined
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_HELPERS = [
    "function _renderSummaryBand(",
    "function _sbTimeAgo(",
    "function _sbStat(",
]


def test_summary_band_helpers_defined():
    html = _load_html()
    missing = [h for h in EXPECTED_HELPERS if h not in html]
    assert not missing, (
        f"These JS helper functions are missing from index.html: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 4. Per-page loader functions defined
# ──────────────────────────────────────────────────────────────────────────────

# Loader functions present in the post-V2.2-4 IA. cost + security loaders
# were removed when those pages' summary bands were folded into their
# top-section content (see EXPECTED_BAND_IDS for the matching rationale).
EXPECTED_LOADERS = [
    "function loadPageSummary_integrations_keys(",
    "function loadPageSummary_maintenance(",
    "function loadPageSummary_reports(",
    "function loadPageSummary_skills(",
    "function loadPageSummary_self_improvement(",
    "function loadPageSummary_ai_optimization(",
    "function loadPageSummary_cost_measures(",
    "function loadPageSummary_settings(",
]


def test_per_page_loader_functions_defined():
    html = _load_html()
    missing = [f for f in EXPECTED_LOADERS if f not in html]
    assert not missing, (
        f"These per-page summary loader functions are missing: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5. onPageActivate wires each summary loader
# ──────────────────────────────────────────────────────────────────────────────

# Loader wirings present in onPageActivate. cost + security removed when
# those pages' summary bands were folded into top-section content.
EXPECTED_WIRING = [
    "loadPageSummary_integrations_keys()",
    "loadPageSummary_maintenance()",
    "loadPageSummary_reports()",
    "loadPageSummary_skills()",
    "loadPageSummary_self_improvement()",
    "loadPageSummary_ai_optimization()",
    "loadPageSummary_cost_measures()",
    "loadPageSummary_settings()",
]


def test_summary_loaders_wired_into_on_page_activate():
    """Each loader must be called somewhere inside the onPageActivate function body."""
    html = _load_html()
    # Extract the onPageActivate body (from the function keyword to the closing brace).
    match = re.search(r'function onPageActivate\(page\)\s*\{(.+?)\n\}', html, re.DOTALL)
    assert match, "onPageActivate function not found in index.html"
    body = match.group(1)
    missing = [w for w in EXPECTED_WIRING if w not in body]
    assert not missing, (
        f"These summary loaders are not wired into onPageActivate: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6. Each summary band is inside the correct page div (no orphaned bands)
# ──────────────────────────────────────────────────────────────────────────────

# Page → summary-band ID. cost + security were folded into top-section
# content post-V2.2-4 and don't have dedicated summary bands anymore;
# they're omitted here to keep this test in sync with EXPECTED_BAND_IDS.
PAGE_BAND_MAP = {
    "page-integrations-keys": "integrations-keys-summary",
    "page-maintenance":       "maintenance-summary",
    "page-reports":           "reports-summary",
    "page-skills":            "skills-summary",
    "page-self-improvement":  "self-improvement-summary",
    "page-ai-optimization":   "ai-optimization-summary",
    "page-cost-measures":     "cost-measures-summary",
    "page-settings":          "settings-summary",
}


def test_each_summary_band_is_inside_its_page():
    """
    For each page, the summary band id must appear after the page div opens
    and before the next page div or end of file.
    """
    html = _load_html()
    # Split on page div boundaries.
    page_blocks = re.split(r'(?=<div class="page[" ])', html)

    for page_id, band_id in PAGE_BAND_MAP.items():
        # Find the block that contains the page's own id.
        containing_block = None
        for block in page_blocks:
            if f'id="{page_id}"' in block:
                containing_block = block
                break
        assert containing_block is not None, (
            f'No block found for id="{page_id}" — page missing?'
        )
        assert f'id="{band_id}"' in containing_block, (
            f'Summary band id="{band_id}" not found inside the id="{page_id}" block. '
            f'It may be in the wrong page or missing entirely.'
        )


# ──────────────────────────────────────────────────────────────────────────────
# 7. No duplicate summary band ids
# ──────────────────────────────────────────────────────────────────────────────

def test_summary_band_ids_are_unique():
    html = _load_html()
    for band_id in EXPECTED_BAND_IDS:
        count = html.count(f'id="{band_id}"')
        assert count == 1, (
            f'id="{band_id}" appears {count} times — must be unique (getElementById breaks on duplicates)'
        )


# ──────────────────────────────────────────────────────────────────────────────
# 8. Each loader calls _renderSummaryBand with the right page-id argument
# ──────────────────────────────────────────────────────────────────────────────

# Loaders that exist in the post-V2.2-4 IA. cost + security removed when
# their summary bands were folded into top-section content (see
# EXPECTED_BAND_IDS comments).
LOADER_RENDER_MAP = {
    "loadPageSummary_integrations_keys": "integrations-keys",
    "loadPageSummary_maintenance":       "maintenance",
    "loadPageSummary_reports":           "reports",
    "loadPageSummary_skills":            "skills",
    "loadPageSummary_self_improvement":  "self-improvement",
    "loadPageSummary_ai_optimization":   "ai-optimization",
    "loadPageSummary_cost_measures":     "cost-measures",
    "loadPageSummary_settings":          "settings",
}


def test_loaders_call_render_with_correct_page_id():
    """
    Each per-page loader must call _renderSummaryBand('<page-id>', ...) so the
    band renders into the right element.
    """
    html = _load_html()
    for loader_name, expected_page_id in LOADER_RENDER_MAP.items():
        # Find the loader body (from function declaration to next top-level async function)
        start = html.find(f'function {loader_name}(')
        assert start != -1, f"function {loader_name}( not found in HTML"
        snippet = html[start: start + 3000]  # generous window
        render_call = f"_renderSummaryBand('{expected_page_id}'"
        assert render_call in snippet, (
            f"{loader_name}: expected _renderSummaryBand('{expected_page_id}', ...) "
            f"within the function body — not found in first 3000 chars after declaration"
        )
