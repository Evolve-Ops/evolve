"""Tests for the GitHub-backup deep-link affordances on the Maintenance →
Backup and Maintenance → Recovery surfaces.

Pattern-match the rendered JS in index.html — same markup-contract style
as test_inbox_repos_ui.py. The actual click flow is unit-tested in the
onboarding-modal tests; these just guard that the entry points exist
where the operator hits the friction.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_INDEX_HTML = _ADMIN_PKG / "evolve_admin" / "web" / "index.html"


_BACKUP_JS = _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "backup.js"
_RECOVERY_JS = _ADMIN_PKG / "evolve_admin" / "web" / "static" / "js" / "pages" / "recovery.js"


def _read_html() -> str:
    # Backup config + cloud lives in backup.js (Phase 3h); Recovery
    # subtab + pod-rollback log lives in recovery.js (Phase 3s).
    return (
        _INDEX_HTML.read_text()
        + "\n"
        + _BACKUP_JS.read_text()
        + "\n"
        + _RECOVERY_JS.read_text()
    )


# ── Backup tab — guided setup CTA on unconfigured bot cards ───────────────


def test_backup_card_offers_guided_setup_when_unconfigured():
    """When a bot has no backupRepoUrl, the per-bot card on Maintenance →
    Backup must show a CTA that opens the existing GitHub onboarding modal.

    Without this, operators see "not configured" + an empty URL input and
    have to know to navigate to Credentials. The CTA closes that gap.
    """
    html = _read_html()
    start = html.find("function renderBackupConfig(")
    assert start > 0, "renderBackupConfig must exist in index.html"
    fn_body = html[start:start + 8000]
    # The guided-setup CTA must be conditional on `!configured` (otherwise
    # it would render even when the bot already has a working backup, which
    # would be noise).
    assert "guidedSetupCTA" in fn_body, (
        "renderBackupConfig must compute a `guidedSetupCTA` block — the "
        "unconfigured-state affordance that opens the onboarding modal."
    )
    # And it must route to the existing per-bot onboarding flow.
    assert "openOnboardModal('github'" in fn_body, (
        "guided-setup CTA must call openOnboardModal('github', [botId]) — "
        "the existing per-bot backup-repo onboarding flow."
    )
    # The CTA must render the bot ID into the call so the modal targets
    # the right bot.
    assert "${escHtml(botId)}" in fn_body or "${botId}" in fn_body


# ── Recovery — same deep-link from rollback empty-state ───────────────────


def test_rollback_empty_state_deep_links_to_onboarding():
    """The Recovery rollback card's "no backup configured" empty state
    must open the onboarding modal directly — not just text-link to the
    Backup tab where the operator has to figure out the next click.
    """
    html = _read_html()
    # Find the rollback-points handler.
    start = html.find("backup_url_configured")
    assert start > 0, "rollback empty-state code path must reference backup_url_configured"
    # Take a window large enough to cover both the rollback dropdown
    # empty-state AND the workspace-commits empty-state below it.
    window = html[start:start + 6000]
    # Both empty-states must include the onboarding-modal deep-link.
    occurrences = window.count("openOnboardModal('github'")
    assert occurrences >= 2, (
        f"Expected at least 2 openOnboardModal('github', …) calls in the "
        f"recovery empty-state region (one for the rollback dropdown, one "
        f"for workspace-commits), found {occurrences}."
    )


def test_rollback_workspace_not_git_keeps_backup_tab_link():
    """When backup IS configured but workspace isn't a git repo yet, the
    fix is `backupInitWorkspace`, not the onboarding modal. The hint for
    that case should still point to Maintenance → Backup (where the "Init
    now" button lives), not the GitHub onboarding modal.
    """
    html = _read_html()
    # Find the workspace_is_git branch specifically.
    idx = html.find("data.workspace_is_git")
    assert idx > 0
    # Take a small window covering both occurrences in the recovery code.
    window = html[idx:idx + 2000]
    # Should still mention navigating to Maintenance → Backup for init,
    # not open the onboarding modal.
    assert 'data-subtab=&quot;backup&quot;' in window, (
        "workspace-not-git hint should still deep-link to Maintenance → "
        "Backup (where the Init now button lives), not the github "
        "onboarding modal."
    )
