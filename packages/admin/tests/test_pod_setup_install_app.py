"""Tests for the install-as-app onboarding tasks + manual-complete endpoint.

Two checklist items can't auto-detect — the pod has no signal for
"operator installed the dashboard as a PWA / saved to home screen on
their phone." The fix is:

  1. Two new ONBOARDING_TASKS entries (install_desktop_app,
     install_mobile_app) with `check=lambda state: False` so the engine
     sweep never auto-completes them.
  2. A new endpoint POST /api/better/getting-started/<id>/done that
     marks `completed=True, how="manual"` — operator's "Mark installed"
     button in the modal.
  3. UI modals + action dispatcher entries that route the Go button
     into the modals, and the bottom-of-modal Mark button into /done.

These tests pin all three layers as a single contract.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"
SERVER_PY = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "routes_better.py"

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _server() -> str:
    return SERVER_PY.read_text(encoding="utf-8")


# ── Registry: tasks exist with correct shape ─────────────────────────────


def test_install_desktop_app_task_registered():
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS
    task = next((t for t in ONBOARDING_TASKS if t.id == "install_desktop_app"), None)
    assert task is not None, "install_desktop_app task missing"
    assert task.per_bot is False
    assert "scope:pod" in task.tags
    assert "category:client" in task.tags
    assert task.action == "open_desktop_install_modal"


def test_install_mobile_app_task_registered():
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS
    task = next((t for t in ONBOARDING_TASKS if t.id == "install_mobile_app"), None)
    assert task is not None, "install_mobile_app task missing"
    assert task.per_bot is False
    assert "scope:pod" in task.tags
    assert task.action == "open_mobile_install_modal"


def test_install_mobile_depends_on_https_for_ios_pwa():
    """iOS Safari won't install an HTTP page as a standalone home-screen
    app — only as a regular bookmark. The mobile task depends on
    https_enabled so it stays Locked until HTTPS is up."""
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS
    task = next(t for t in ONBOARDING_TASKS if t.id == "install_mobile_app")
    assert "https_enabled" in task.depends_on
    assert "primary_installed" in task.depends_on


def test_install_desktop_depends_only_on_primary():
    """Desktop browsers happily install HTTP pages as web apps / dock
    shortcuts — no HTTPS dependency."""
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS
    task = next(t for t in ONBOARDING_TASKS if t.id == "install_desktop_app")
    assert task.depends_on == ["primary_installed"]


def test_install_tasks_check_always_returns_false():
    """No pod-side signal for PWA install state — check() must always
    be False so the engine sweep never thinks these are auto-completed.
    Operator clears them via the manual /done endpoint or by dismissing."""
    from evolve_admin.better_engine.onboarding import ONBOARDING_TASKS
    desktop = next(t for t in ONBOARDING_TASKS if t.id == "install_desktop_app")
    mobile = next(t for t in ONBOARDING_TASKS if t.id == "install_mobile_app")
    # Any state shape — check should always be False.
    for state in ({}, {"primary_installed": True}, {"https_enabled": True}):
        assert desktop.check(state) is False
        assert mobile.check(state) is False


# ── Endpoint: /done marks manual ─────────────────────────────────────────


def test_done_endpoint_route_registered_in_server():
    text = _server()
    assert "/api/better/getting-started/<task_id>/done" in text
    assert "def api_better_done_task" in text


def test_done_endpoint_uses_mark_task_complete_with_manual():
    """The endpoint must call mark_task_complete with how='manual' so
    the storage entry lands with how=manual, not auto or skipped. Skip
    means dismissed; manual means the operator did the action."""
    text = _server()
    fn = re.search(
        r"def api_better_done_task\(task_id: str\) -> Response:(.+?)\n    # ──",
        text, re.DOTALL,
    )
    assert fn, "api_better_done_task body not found"
    body = fn.group(1)
    assert 'mark_task_complete(task_id, "manual"' in body


def test_done_endpoint_persists_to_disk():
    """save_getting_started must be called or the manual-complete is
    lost on next page load."""
    text = _server()
    fn = re.search(
        r"def api_better_done_task\(task_id: str\) -> Response:(.+?)\n    # ──",
        text, re.DOTALL,
    )
    assert fn
    body = fn.group(1)
    assert "save_getting_started" in body


# ── UI: dispatcher entries + modal wiring ────────────────────────────────


def test_action_dispatcher_has_install_modal_entries():
    html = _html()
    block = re.search(
        r"const _POD_SETUP_ACTIONS\s*=\s*\{(.+?)\};",
        html, re.DOTALL,
    )
    assert block, "_POD_SETUP_ACTIONS const not found"
    body = block.group(1)
    assert "open_desktop_install_modal" in body
    assert "open_mobile_install_modal" in body
    assert "openInstallDesktopModal()" in body
    assert "openInstallMobileModal()" in body


def test_desktop_install_modal_markup_exists():
    html = _html()
    assert 'id="install-desktop-modal"' in html
    # Three browser tabs surfaced
    assert "Chrome / Edge / Arc" in html
    assert "Add to Dock" in html  # Safari instruction
    # Mark-done button wires to /done via _markInstallTaskDone
    assert '_markInstallTaskDone(\'install_desktop_app\'' in html


def test_mobile_install_modal_markup_exists():
    html = _html()
    assert 'id="install-mobile-modal"' in html
    # iOS + Android tabs surfaced with their distinctive gestures
    assert "Add to Home Screen" in html  # iOS Safari
    assert "Install app" in html         # Android Chrome
    assert '_markInstallTaskDone(\'install_mobile_app\'' in html


def test_mark_install_task_done_posts_to_done_endpoint():
    """Bottom-of-modal "Mark installed" button must POST to the new
    /done endpoint, not /skip (which would mark dismissed)."""
    html = _html()
    fn = re.search(
        r"async function _markInstallTaskDone\(taskId, modalElId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_markInstallTaskDone not found"
    body = fn.group(1)
    assert "/api/better/getting-started/${encodeURIComponent(taskId)}/done" in body
    # And not the skip endpoint
    assert "/skip" not in body


def test_mark_install_refreshes_checklist_and_chip():
    """After /done lands, the row should flip to Done without a page
    reload — both the checklist and the Overview chip need to re-fetch."""
    html = _html()
    fn = re.search(
        r"async function _markInstallTaskDone\(taskId, modalElId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn
    body = fn.group(1)
    assert "loadPodSetupChecklist" in body
    assert "loadPodSetupChip" in body


# ── Maintenance shortcuts card in Quick Start ─────────────────────────────


def test_maintenance_shortcuts_card_renders_in_quick_start():
    """The Quick Start subtab gained a static card pointing to the two
    Maintenance subtabs (Setup wizard + Admin Server). Always visible —
    they're operator tools that don't map to pod-state checklist items."""
    html = _html()
    assert 'id="getting-started-maint-shortcuts"' in html
    # Cards link to the right subtab navigators
    assert "_podSetupNavSubtab('maintenance','setup')" in html
    assert "_podSetupNavSubtab('maintenance','adminserver')" in html


def test_maintenance_shortcuts_state_loader_defined():
    """The card has live badges; loadGettingStartedShortcutsState reads
    the admin-service status endpoint and stamps Running / Pending."""
    html = _html()
    assert "async function loadGettingStartedShortcutsState()" in html
    fn = re.search(
        r"async function loadGettingStartedShortcutsState\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn
    body = fn.group(1)
    assert "/api/admin/service/status" in body


def test_maintenance_shortcuts_loader_called_on_page_activate():
    html = _html()
    fn = re.search(
        r"function onPageActivate\(page\)\s*\{(.+?)\n\}\s*\n",
        html, re.DOTALL,
    )
    assert fn
    body = fn.group(1)
    assert "loadGettingStartedShortcutsState()" in body
