"""Structural pins for the pod-wide Setup checklist UI (Phase B).

Regex-on-source checks against index.html — same pattern as the per-bot
setup_checklist UI tests. Each test pins one invariant of the rendered
markup or wiring so silent regressions show up here before the operator
sees them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ── Placeholder + render wiring ────────────────────────────────────────────


def test_pod_setup_card_placeholder_exists_in_quick_start_subtab():
    """The Quick Start subtab has the placeholder div the renderer writes into."""
    html = _html()
    assert 'id="pod-setup-checklist"' in html


def test_pod_setup_card_title_says_pod_setup_checklist():
    html = _html()
    assert re.search(
        r'<div class="card-title">Pod setup checklist',
        html,
    ), "Pod setup checklist card-title missing"


def test_legacy_narrative_walkthrough_is_collapsed_inside_details():
    """The 7-step narrative cards still ship as reference content but
    inside a <details> collapsible — checklist is the primary surface."""
    html = _html()
    assert re.search(
        r'<summary[^>]*>\s*Step-by-step walkthrough',
        html,
    ), "narrative walkthrough <summary> missing"


def test_load_fn_defined():
    html = _html()
    assert "async function loadPodSetupChecklist()" in html


def test_load_fn_called_on_page_activate():
    """onPageActivate() must call loadPodSetupChecklist when the operator
    navigates to Getting Started — fresh fetch each visit, no stale state."""
    html = _html()
    # Match within the onPageActivate function body
    fn = re.search(
        r"function onPageActivate\(page\)\s*\{(.+?)\n\}\s*\n",
        html, re.DOTALL,
    )
    assert fn, "onPageActivate not found"
    assert "page === 'getting-started'" in fn.group(1)
    assert "loadPodSetupChecklist()" in fn.group(1)


# ── Action dispatcher ─────────────────────────────────────────────────────


def test_action_dispatcher_registers_all_thirteen_action_ids():
    """Every action string declared by an ONBOARDING_TASKS entry must
    have a dispatcher entry, or the row's Go button silently no-ops.
    Pins the contract between the registry and the UI."""
    html = _html()
    # Find the _POD_SETUP_ACTIONS const block.
    block = re.search(
        r"const _POD_SETUP_ACTIONS\s*=\s*\{(.+?)\};",
        html, re.DOTALL,
    )
    assert block, "_POD_SETUP_ACTIONS const not found"
    body = block.group(1)
    expected_actions = [
        # 6 new pod-wide
        "open_install_evo", "open_users", "open_https_wizard",
        "open_pod_conduct", "open_github_dev_wizard", "open_gallery",
        # 7 pre-existing
        "open_setup", "run_health_check", "run_scanner",
        "open_applications", "open_cost_config", "open_reports_config",
        "open_security",
    ]
    for action in expected_actions:
        assert action in body, f"action {action!r} missing from _POD_SETUP_ACTIONS"


def test_action_dispatch_runs_on_go_button_click():
    """The Go button's onclick calls _podSetupActionDispatch with the task id."""
    html = _html()
    assert "_podSetupActionDispatch(" in html


def test_unknown_action_surfaces_friendly_error():
    """If a task ships a new action id the UI doesn't know, show a clear
    message in the status div instead of silently doing nothing. Defends
    against the Phase A → UI version skew when ONBOARDING_TASKS gains
    new entries before the UI is redeployed."""
    html = _html()
    fn = re.search(
        r"function _podSetupActionDispatch\(taskId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupActionDispatch not found"
    body = fn.group(1)
    assert "Unknown action" in body


# ── Skip / unskip wiring ──────────────────────────────────────────────────


def test_skip_button_hits_existing_skip_endpoint():
    html = _html()
    fn = re.search(
        r"async function _podSetupSkip\(taskId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupSkip not found"
    body = fn.group(1)
    assert "/api/better/getting-started/${encodeURIComponent(taskId)}/skip" in body


def test_unskip_button_hits_new_unskip_endpoint():
    """Bring-back button must POST to the new /unskip route, not /skip."""
    html = _html()
    fn = re.search(
        r"async function _podSetupUnskip\(taskId\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupUnskip not found"
    body = fn.group(1)
    assert "/api/better/getting-started/${encodeURIComponent(taskId)}/unskip" in body
    # Make sure the helper actually fetches — not still the placeholder
    # "follow-up API" string from the pre-endpoint draft.
    assert "follow-up API" not in body


# ── Phase D/E stub placeholders ───────────────────────────────────────────


def test_https_wizard_action_opens_modal():
    """Phase D upgraded the HTTPS row's Go button from an alert() stub
    to a real modal (openHttpsSetupWizard). The dispatcher entry name
    stayed `_podSetupHttpsWizardStub` for binding stability — the body
    now delegates to the modal opener instead of surfacing the CLI
    command directly. The actual CLI command lives in
    _httpsSetupCopyCmd (tested under test_pod_setup_https_wizard)."""
    html = _html()
    fn = re.search(
        r"function _podSetupHttpsWizardStub\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupHttpsWizardStub not found"
    body = fn.group(1)
    assert "openHttpsSetupWizard()" in body


def test_github_dev_wizard_action_opens_modal():
    """Phase E upgraded the github-dev row's Go button from an alert()
    stub to a real modal (openGithubDevWizard). Dispatcher name kept
    for binding stability. The CLI command itself lives in
    _githubDevCopyCmd (tested under test_pod_setup_github_dev_wizard)."""
    html = _html()
    fn = re.search(
        r"function _podSetupGithubDevWizardStub\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupGithubDevWizardStub not found"
    body = fn.group(1)
    assert "openGithubDevWizard()" in body


# ── State derivation ──────────────────────────────────────────────────────


def test_narrative_section_auto_renders_from_tasks_meta():
    """2026-06-04: the 7 hardcoded narrative cards (Quick Start tab)
    were replaced with an auto-rendered version backed by the same
    tasks_meta the compact checklist uses. One source of truth: a task
    added to ONBOARDING_TASKS lands in both the checklist AND the
    narrative without duplication."""
    html = _html()
    # The placeholder div is where the narrative cards mount
    assert 'id="pod-setup-narrative-rows"' in html
    # Render function exists and pulls from gs.tasks_meta
    assert "function _renderPodSetupNarrative(el, gs)" in html
    fn = re.search(
        r"function _renderPodSetupNarrative\(el, gs\)\s*\{(.+?)\nasync function ",
        html, re.DOTALL,
    )
    assert fn, "_renderPodSetupNarrative body not found"
    body = fn.group(1)
    assert "tasks_meta" in body
    # Uses the task's `context` field for the rich prose
    assert "meta.context" in body


def test_narrative_renders_alongside_checklist_on_same_fetch():
    """loadPodSetupChecklist fetches /api/better/getting-started once
    and feeds both the compact checklist AND the narrative section —
    no second round-trip, no chance of the two surfaces showing
    inconsistent data."""
    html = _html()
    fn = re.search(
        r"async function loadPodSetupChecklist\(\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "loadPodSetupChecklist not found"
    body = fn.group(1)
    assert "_renderPodSetupChecklist" in body
    assert "_renderPodSetupNarrative" in body
    # Only ONE fetch call to the gs endpoint — same payload feeds both
    assert body.count("/api/better/getting-started") == 1


def test_legacy_narrative_cards_removed():
    """The 7 hardcoded narrative cards (numbered 0 through 6) used to
    live above this comment. They've been replaced by auto-render. A
    regression check that re-adds the literal "Sanity-check the
    install" content would defeat the normalization."""
    html = _html()
    # Two telltale strings from the legacy hardcoded narrative
    assert "Sanity-check the install" not in html
    assert "Your daily routine" not in html
    # But the new bookend cards are present
    assert "Before you start — open the dashboard from your laptop" in html
    assert "After setup — your daily routine" in html


def test_action_dispatcher_includes_new_open_actions():
    """Two new action ids landed: open_ai_optimization (Phase A follow-up
    for the tier task) and open_skills (the messaging-channel task)."""
    html = _html()
    block = re.search(
        r"const _POD_SETUP_ACTIONS\s*=\s*\{(.+?)\};",
        html, re.DOTALL,
    )
    assert block
    body = block.group(1)
    for action in ("open_ai_optimization", "open_skills"):
        assert action in body, f"missing action {action!r}"


def test_state_derivation_maps_storage_shape_to_three_states():
    """The storage layer uses {completed, how} — our derivation must
    collapse that into the three-state vocabulary used by the per-bot
    setup_checklist (done | pending | dismissed) so the UI vocabulary is
    consistent."""
    html = _html()
    fn = re.search(
        r"function _podSetupRowState\(stateEntry\)\s*\{(.+?)\n\}",
        html, re.DOTALL,
    )
    assert fn, "_podSetupRowState not found"
    body = fn.group(1)
    assert "'done'" in body
    assert "'pending'" in body
    assert "'dismissed'" in body
    assert "skipped" in body  # bridge from storage's "how" field
