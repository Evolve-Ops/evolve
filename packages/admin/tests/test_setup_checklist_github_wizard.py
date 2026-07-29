"""Structural pins for the GitHub setup mini-wizard (step 5).

Regex-on-source checks against index.html — same pattern as the other
setup_checklist UI tests. Each test pins one invariant of the modal +
its four progressive sections.

The wizard launches from the Setup checklist's github row "Set up ▸"
button, then composes four existing endpoints. The pins here cover:
  * Modal HTML exists with the right ids
  * Open/close functions are defined and refresh parent surfaces on close
  * "Set up ▸" button on the github row launches the wizard (not chipNav)
  * Each of the four sections calls the right existing backend endpoint
  * Section 2/3/4 dim when prerequisites aren't met (pointer-events:none)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_BOT_DETAIL_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "bot-detail.js"
_GITHUB_SETUP_WIZARD_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "github-setup-wizard.js"
def _html() -> str:
    # Setup checklist row + dispatch lives in bot-detail.js (Phase 3ac);
    # GitHub mini-wizard lives in github-setup-wizard.js (Phase 3ag).
    return (
        INDEX_HTML.read_text(encoding="utf-8")
        + "\n"
        + _BOT_DETAIL_JS.read_text(encoding="utf-8")
        + "\n"
        + _GITHUB_SETUP_WIZARD_JS.read_text(encoding="utf-8")
    )


# ── Modal HTML pins ───────────────────────────────────────────────────────


def test_modal_overlay_and_body_ids_exist():
    html = _html()
    assert 'id="github-setup-modal"' in html
    assert 'id="github-setup-modal-body"' in html
    assert 'id="github-setup-modal-title"' in html


def test_modal_closes_on_outside_click():
    html = _html()
    assert (
        "onclick=\"if(event.target===this)closeGithubSetupWizard()\""
        in html
    )


def test_open_close_functions_defined():
    html = _html()
    assert "async function openGithubSetupWizard(botId)" in html
    assert "function closeGithubSetupWizard()" in html


# ── Github row launches the wizard ────────────────────────────────────────


def test_github_row_launches_wizard_not_chip_nav():
    """The Go button on the github row must call openGithubSetupWizard.
    Other rows still go through _setupChecklistGo."""
    html = _html()
    # Find the _setupChecklistRow function body.
    fn = re.search(
        r"function _setupChecklistRow\(safeBot, item\)\s*\{(.+?)\nfunction ",
        html, re.DOTALL,
    )
    assert fn, "_setupChecklistRow not found"
    body = fn.group(1)
    assert "item.id === 'github'" in body
    assert "openGithubSetupWizard" in body
    # And the non-github fallback still uses the nav helper
    assert "_setupChecklistGo(" in body


# ── Endpoint wiring (one test per substep) ────────────────────────────────


def test_initial_load_calls_three_existing_endpoints_in_parallel():
    """openGithubSetupWizard fetches discover-default-pat, /api/network, and
    the MCP install status in one parallel batch so the wizard can render
    its starting state without serializing three round-trips."""
    html = _html()
    fn = re.search(
        r"async function openGithubSetupWizard\(botId\)\s*\{(.+?)\n\}\s*\n\s*function closeGithubSetupWizard",
        html, re.DOTALL,
    )
    assert fn, "openGithubSetupWizard not found"
    body = fn.group(1)
    assert "/api/admin/onboard/github/discover-default-pat" in body
    assert "/api/network" in body
    assert "/api/skills/install/github/status" in body
    # Parallel fetch via Promise.all
    assert "Promise.all(" in body


def test_section1_verify_hits_existing_verify_endpoint():
    """Section 1's Verify button POSTs to /api/admin/onboard/github/verify
    — the existing PAT-validation endpoint."""
    html = _html()
    assert "/api/admin/onboard/github/verify" in html


def test_section3_wire_backup_hits_existing_onboard_endpoint():
    """Section 3 POSTs to /api/admin/onboard/github — the existing
    per-bot backup-wiring endpoint that creates the repo, registers the
    deploy key, and writes backupRepoUrl."""
    html = _html()
    fn = re.search(
        r"async function _ghSetupWireBackup\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_ghSetupWireBackup not found"
    body = fn.group(1)
    assert "/api/admin/onboard/github" in body
    # Must pass the bot_id and repo_name in the single-bot list shape
    # the endpoint expects.
    assert "bots:" in body
    assert "repo_name" in body


def test_section4_wire_mcp_hits_existing_install_endpoint():
    """Section 4 POSTs to the existing GitHub MCP install endpoint
    (which writes the keystore slot + creates the InstallMcpServer
    proposal)."""
    html = _html()
    fn = re.search(
        r"async function _ghSetupWireMcp\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_ghSetupWireMcp not found"
    body = fn.group(1)
    assert "/api/skills/install/github/install-mcp-server" in body
    assert "access_token" in body


# ── Section dimming (prerequisite gating) ─────────────────────────────────


def test_section_2_dims_until_credentials_verified():
    """Section 2 (repo) must dim when there's no verified login yet.
    Verified via inline `opacity:0.45;pointer-events:none` when
    `haveCred` is false."""
    html = _html()
    fn = re.search(
        r"function _ghSetupSection2Html\(s\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_ghSetupSection2Html not found"
    body = fn.group(1)
    assert "s.login" in body  # gate is the verified login
    assert "opacity:0.45;pointer-events:none" in body


def test_section_3_dims_until_credentials_verified():
    html = _html()
    fn = re.search(
        r"function _ghSetupSection3Html\(s\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_ghSetupSection3Html not found"
    body = fn.group(1)
    assert "opacity:0.45;pointer-events:none" in body


def test_section_4_dims_until_pat_present():
    html = _html()
    fn = re.search(
        r"function _ghSetupSection4Html\(s\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_ghSetupSection4Html not found"
    body = fn.group(1)
    assert "opacity:0.45;pointer-events:none" in body


# ── Close-refresh callback ────────────────────────────────────────────────


def test_close_refreshes_parent_surfaces():
    """closeGithubSetupWizard must trigger renderBotSetupChecklistCard
    + the parent checklist modal so the github row picks up the new
    state without a full reload."""
    html = _html()
    fn = re.search(
        r"function closeGithubSetupWizard\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "closeGithubSetupWizard not found"
    body = fn.group(1)
    assert "renderBotSetupChecklistCard" in body
    assert "openSetupChecklistModal" in body
