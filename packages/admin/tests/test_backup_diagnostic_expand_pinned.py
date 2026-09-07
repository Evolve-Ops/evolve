"""Pin the expandable backup-diagnostic affordance into index.html.

Two surfaces (Backup → Status and Maintenance → Bot Versions) gained an
expandable sub-row that reveals the classified cause + raw stderr + fix
steps for a failing backup. These tests don't run the JS — they grep
the bundled HTML to verify the structural pieces stayed wired up.

Companion to test_backup_status_summary_honors_attempt.py and
test_maintenance_backup_cell_honors_attempt.py. The pattern is:
silent-failure regressions in this file are common (one edit drops a
key field and the table renders without it), so each new affordance
gets a pinned-in-source guard so the next edit trips a clear failure.

Background: 2026-06-07 the operator saw "✗ 2× in a row" with no inline
explanation despite the diagnostic being one hover away in a tooltip.
Without these pins, a refactor could drop the expansion back to a
tooltip and silently re-ship the bug.
"""
from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "index.html"
_BACKUP_JS = Path(__file__).resolve().parent.parent / "evolve_admin" / "web" / "static" / "js" / "pages" / "backup.js"
_TEXT = _INDEX_HTML.read_text() + "\n" + _BACKUP_JS.read_text()


def test_diagnostic_panel_renderer_exists() -> None:
    """The shared renderer must be defined at module scope so both
    Backup → Status and Maintenance → Bot Versions can call it. If
    this disappears the call sites die at runtime with ReferenceError.
    """
    assert "function renderBackupDiagnosticPanel(" in _TEXT


def test_diagnostic_renderer_consumes_classified_cause() -> None:
    """The renderer must read ``classified_cause`` from the bot payload —
    that's the structured fix-steps source. Pre-fix the UI only had a
    tooltip-truncated last_error.
    """
    # Find the renderer body and assert the field is referenced.
    start = _TEXT.find("function renderBackupDiagnosticPanel(")
    end = _TEXT.find("\n}\n", start)
    body = _TEXT[start:end]
    assert "classified_cause" in body
    assert "fix_steps" in body


def test_diagnostic_renderer_surfaces_raw_error_block() -> None:
    """A code block showing the raw stderr must be in the panel —
    operators rely on it when the classifier didn't match a pattern.
    """
    start = _TEXT.find("function renderBackupDiagnosticPanel(")
    end = _TEXT.find("\n}\n", start)
    body = _TEXT[start:end]
    assert "RAW ERROR" in body
    assert "last_error" in body or "lastError" in body


def test_toggle_function_exists() -> None:
    """toggleBackupDiagnostic must exist — both surfaces wire to it."""
    assert "function toggleBackupDiagnostic(" in _TEXT


def test_toggle_scope_distinguishes_two_surfaces() -> None:
    """The toggle takes a (botId, scope) pair so Backup → Status and
    Maintenance → Bot Versions don't collide on identical id strings.
    Backup uses scope='bk', Maintenance uses scope='sysm'.
    """
    assert "'bk'" in _TEXT  # call site
    assert "'sysm'" in _TEXT


def test_backup_status_row_has_expand_affordance() -> None:
    """The Backup → Status row must call toggleBackupDiagnostic with
    scope='bk' when expandable. Catches a regression that drops the
    onclick binding back to plain row chrome.
    """
    assert "toggleBackupDiagnostic" in _TEXT
    # The Backup → Status surface uses scope 'bk' via toggleBackupDiagnostic('<id>','bk').
    assert "toggleBackupDiagnostic('${escHtml(id)}','bk')" in _TEXT


def test_maintenance_backup_cell_has_expand_affordance() -> None:
    """The Maintenance Bot Versions Backed-up cell must wire its chevron
    to toggleBackupDiagnostic with scope='sysm'.
    """
    assert "toggleBackupDiagnostic('${escHtml(id)}','sysm')" in _TEXT
    # sysm detail row anchor — used by the toggle to find the right element.
    assert "sysm-bkdiag-" in _TEXT


def test_backup_now_action_wired_through_diagnostic_panel() -> None:
    """The diagnostic panel must include a 'Backup now' button that
    posts to the existing /api/backup/cloud/run endpoint, so the
    operator can retry inline without leaving the table.
    """
    assert "backupNowFromDiagnostic(" in _TEXT
    assert "/api/backup/cloud/run" in _TEXT
