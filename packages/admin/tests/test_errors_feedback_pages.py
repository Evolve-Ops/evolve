"""
Phase 1D — Errors + Feedback page smoke tests.

Verifies:
  1. Sidebar has data-page="errors" and data-page="feedback" in the Settings section.
  2. page-errors and page-feedback divs exist.
  3. Errors page has a summary band.
  4. onPageActivate handles both new pages (loadErrors / loadFeedback calls).
  5. The top-right error popup banner is deprecated (hidden, not rendered visibly).
  6. The errlog-btn in the utility bar is hidden (moved to sidebar page).
  7. /api/errors endpoint returns expected JSON shape with dedup grouping.
"""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

INDEX_HTML = (
    Path(__file__).parent.parent
    / "evolve_admin" / "web" / "index.html"
)


def _load_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ── Sidebar structure ────────────────────────────────────────────────────────

def test_sidebar_has_errors_nav_item():
    """Settings section must include a nav item for the Errors page."""
    html = _load_html()
    settings_pos = html.find('<div class="nav-section">Settings</div>')
    sidebar_footer_pos = html.find('<div class="sidebar-footer">')
    assert settings_pos != -1, "Settings nav-section not found"
    assert sidebar_footer_pos != -1, "sidebar-footer not found"
    settings_block = html[settings_pos:sidebar_footer_pos]
    assert 'data-page="errors"' in settings_block, (
        'nav item with data-page="errors" not found in Settings section'
    )


def test_sidebar_has_feedback_nav_item():
    """Settings section must include a nav item for the Feedback page."""
    html = _load_html()
    settings_pos = html.find('<div class="nav-section">Settings</div>')
    sidebar_footer_pos = html.find('<div class="sidebar-footer">')
    assert settings_pos != -1
    assert sidebar_footer_pos != -1
    settings_block = html[settings_pos:sidebar_footer_pos]
    assert 'data-page="feedback"' in settings_block, (
        'nav item with data-page="feedback" not found in Settings section'
    )


def test_sidebar_errors_badge_element():
    """badge-errors span must exist (for the unreviewed count badge)."""
    html = _load_html()
    assert 'id="badge-errors"' in html, "badge-errors element not found"


# ── Page divs ────────────────────────────────────────────────────────────────

def test_page_errors_div_exists():
    """page-errors div must exist as a page-level element."""
    html = _load_html()
    assert 'id="page-errors"' in html, 'page-errors div not found in index.html'


def test_page_feedback_div_exists():
    """page-feedback div must exist as a page-level element."""
    html = _load_html()
    assert 'id="page-feedback"' in html, 'page-feedback div not found in index.html'


# ── Summary band on Errors page ──────────────────────────────────────────────

def test_errors_page_has_summary_band():
    """Errors page must render a summary band showing key metrics."""
    html = _load_html()
    # Find the errors page block
    errors_start = html.find('id="page-errors"')
    assert errors_start != -1, "page-errors not found"
    # The summary band comes shortly after the page div
    errors_block = html[errors_start:errors_start + 3000]
    assert 'errors-summary-band' in errors_block or 'page-summary-band' in errors_block, (
        "Summary band not found in Errors page"
    )
    assert 'errors-total' in errors_block, "errors-total metric element not found"
    assert 'errors-sigs' in errors_block, "errors-sigs (unique signatures) element not found"


def test_errors_page_has_table():
    """Errors page must render a table with err-tbody for dynamic content."""
    html = _load_html()
    errors_start = html.find('id="page-errors"')
    errors_end = html.find('id="page-feedback"')
    assert errors_start != -1 and errors_end != -1
    errors_block = html[errors_start:errors_end]
    assert 'err-tbody' in errors_block, "err-tbody not found in Errors page"
    assert 'err-table' in errors_block, "err-table not found in Errors page"


# ── Feedback page tabs ────────────────────────────────────────────────────────

def test_feedback_page_has_bug_tab():
    """Feedback page must have a Bug tab. Accepts either the legacy
    ``fb-page-tab-bug``/``Bug report`` markers (old chat-intent button)
    or the current ``fb-kind-bug`` inline-form toggle id."""
    html = _load_html()
    fb_start = html.find('id="page-feedback"')
    assert fb_start != -1
    fb_block = html[fb_start:fb_start + 5000]
    assert (
        'fb-kind-bug' in fb_block
        or 'fb-page-tab-bug' in fb_block
        or 'Bug report' in fb_block
    ), "Bug tab not found on Feedback page"


def test_feedback_page_has_feature_tab():
    """Feedback page must have a Feature tab. Accepts the same set of
    markers as ``test_feedback_page_has_bug_tab``."""
    html = _load_html()
    fb_start = html.find('id="page-feedback"')
    assert fb_start != -1
    fb_block = html[fb_start:fb_start + 5000]
    assert (
        'fb-kind-feature' in fb_block
        or 'fb-page-tab-feature' in fb_block
        or 'Feature request' in fb_block
    ), "Feature tab not found on Feedback page"


# ── onPageActivate wiring ─────────────────────────────────────────────────────

def test_on_page_activate_handles_errors():
    """onPageActivate must call loadErrors() when page == 'errors'."""
    html = _load_html()
    activate_match = re.search(
        r"function onPageActivate\(page\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert activate_match, "onPageActivate function not found"
    body = activate_match.group(1)
    assert "loadErrors" in body, (
        "onPageActivate does not call loadErrors() — Errors page won't load data"
    )
    assert "'errors'" in body or '"errors"' in body, (
        "onPageActivate does not check page === 'errors'"
    )


def test_on_page_activate_handles_feedback():
    """onPageActivate must mount the evo chat thread when page == 'feedback'.

    The Feedback page migrated from a structured form (loadFeedback) to a
    hybrid form-button + evo-chat surface; the chat thread is mounted via
    _pageChatRenderThread('feedback'). The structured form still exists but
    lives in the global feedbackOpen() modal, opened from the page buttons.
    """
    html = _load_html()
    activate_match = re.search(
        r"function onPageActivate\(page\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert activate_match, "onPageActivate function not found"
    body = activate_match.group(1)
    assert "_pageChatRenderThread('feedback')" in body, (
        "onPageActivate does not mount the evo chat thread for 'feedback' — "
        "the Feedback page chat won't initialise"
    )


# ── Top-right popup banner deprecated ────────────────────────────────────────

def test_error_detected_banner_is_hidden():
    """error-detected-banner must not be rendered visibly (deprecated in Phase 1D)."""
    html = _load_html()
    # The banner must still exist (JS hooks reference it) but must be hidden
    assert 'id="error-detected-banner"' in html, (
        "error-detected-banner element was removed entirely — JS hooks still reference it"
    )
    # The banner must have a forced-hidden style (display:none!important or similar)
    banner_match = re.search(r'id="error-detected-banner"[^>]*>', html)
    assert banner_match, "error-detected-banner opening tag not found"
    tag = banner_match.group(0)
    assert "display:none" in tag or "display: none" in tag, (
        "error-detected-banner is not forced-hidden — it should be deprecated (display:none) in Phase 1D"
    )


def test_errlog_btn_is_hidden_in_utility_bar():
    """errlog-btn in the utility bar must be hidden (Errors moved to sidebar page)."""
    html = _load_html()
    # Find the utility-bar section
    ub_start = html.find('id="utility-bar"')
    assert ub_start != -1, "utility-bar not found"
    ub_block = html[ub_start:ub_start + 1000]
    # errlog-btn must be hidden
    btn_match = re.search(r'id="errlog-btn"[^>]*>', ub_block)
    assert btn_match, "errlog-btn not found in utility-bar"
    btn_tag = btn_match.group(0)
    assert "display:none" in btn_tag or "display: none" in btn_tag, (
        "errlog-btn is still visible in utility-bar — it should be hidden in Phase 1D"
    )


# ── /api/errors endpoint ─────────────────────────────────────────────────────

@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Minimal Flask test client with the report routes registered.

    We monkey-patch _reporter._raw_error_lines_since to return canned log lines
    so we can verify the dedup + grouping logic without a real log file.
    """
    from evolve_admin.web.server import create_app

    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "members": [],
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()

    app = create_app(network_path=net)
    app.config["TESTING"] = True
    return app.test_client()


def test_api_errors_returns_json_shape(app_client, monkeypatch):
    """GET /api/errors returns { errors, total_occurrences, last_error_at }."""
    from evolve_admin import reporter as _reporter_mod

    sample_lines = [
        "2026-05-16T10:00:00 ERROR   evolve_admin.deploy  deploy_bot failed [team_bot_a]: connection refused",
        "2026-05-16T10:01:00 ERROR   evolve_admin.deploy  deploy_bot failed [team_bot_a]: connection refused",
        "2026-05-16T10:05:00 CRITICAL evolve_admin.server  unexpected exception in worker thread",
    ]

    monkeypatch.setattr(_reporter_mod, "_raw_error_lines_since", lambda *a, **kw: sample_lines)

    r = app_client.get("/api/errors")
    assert r.status_code == 200
    data = r.get_json()
    assert "errors" in data, "response missing 'errors' key"
    assert "total_occurrences" in data, "response missing 'total_occurrences' key"
    assert "last_error_at" in data, "response missing 'last_error_at' key"


def test_api_errors_deduplicates_by_fingerprint(app_client, monkeypatch):
    """Two identical errors (same fingerprint) must produce a single grouped entry."""
    from evolve_admin import reporter as _reporter_mod

    # Two lines that are identical except for the timestamp → same fingerprint
    same_fp_lines = [
        "2026-05-16T10:00:00 ERROR   evolve_admin.deploy  deploy_bot failed [team_bot_a]: connection refused",
        "2026-05-16T10:01:00 ERROR   evolve_admin.deploy  deploy_bot failed [team_bot_a]: connection refused",
    ]
    monkeypatch.setattr(_reporter_mod, "_raw_error_lines_since", lambda *a, **kw: same_fp_lines)

    r = app_client.get("/api/errors")
    assert r.status_code == 200
    data = r.get_json()
    # Should be deduplicated to ONE entry with count=2
    assert len(data["errors"]) == 1, (
        f"Expected 1 deduplicated error entry, got {len(data['errors'])}"
    )
    assert data["errors"][0]["count"] == 2, (
        f"Expected count=2, got {data['errors'][0]['count']}"
    )
    assert data["total_occurrences"] == 2


def test_api_errors_severity_alert_for_critical(app_client, monkeypatch):
    """CRITICAL log lines must map to severity='alert'."""
    from evolve_admin import reporter as _reporter_mod

    monkeypatch.setattr(_reporter_mod, "_raw_error_lines_since", lambda *a, **kw: [
        "2026-05-16T10:00:00 CRITICAL evolve_admin.server  out of memory",
    ])
    r = app_client.get("/api/errors")
    data = r.get_json()
    assert data["errors"][0]["severity"] == "alert"


def test_api_errors_severity_warn_for_error(app_client, monkeypatch):
    """ERROR log lines must map to severity='warn'."""
    from evolve_admin import reporter as _reporter_mod

    monkeypatch.setattr(_reporter_mod, "_raw_error_lines_since", lambda *a, **kw: [
        "2026-05-16T10:00:00 ERROR   evolve_admin.deploy  something went wrong",
    ])
    r = app_client.get("/api/errors")
    data = r.get_json()
    assert data["errors"][0]["severity"] == "warn"


def test_api_errors_empty_when_no_log_lines(app_client, monkeypatch):
    """Empty log → empty errors list with total_occurrences=0."""
    from evolve_admin import reporter as _reporter_mod

    monkeypatch.setattr(_reporter_mod, "_raw_error_lines_since", lambda *a, **kw: [])
    r = app_client.get("/api/errors")
    data = r.get_json()
    assert data["errors"] == []
    assert data["total_occurrences"] == 0
    assert data["last_error_at"] is None
