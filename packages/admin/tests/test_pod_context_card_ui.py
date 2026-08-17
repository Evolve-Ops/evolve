"""tests/test_pod_context_card_ui.py — structural guards for the Pod
Context card on the Identity subtab.

Operator asked (2026-05-20):
  * "Why is this read-only?"
  * "What if I want to change the environment / admin user / machine?"
  * "What do the (?) indicate?"

The card stays read-only by design (these are facts about the
deploy, not configuration knobs), but two things changed in
response:

  1. The bare ``(?)`` literal text was replaced with the standard
     ``help-btn`` + ``tip`` pattern used elsewhere on the same
     subtab, for visual consistency + better discoverability.
  2. A ``details`` block under the card explains the four
     change-it workflows (rename machine, change admin user,
     move machines, override Admin URL) so operators don't have
     to ask.

These tests pin those changes. They're structural (HTML/JS source
inspection) — same pattern as the other UI guards in this dir.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

_WEB = _ADMIN_PKG / "evolve_admin" / "web"
_INDEX_HTML = _WEB / "index.html"
_USERS_JS = _WEB / "static" / "js" / "pages" / "users.js"


@pytest.fixture(scope="module")
def html() -> str:
    # The Pod Context card is rendered by _idRenderPodContext, which moved
    # to pages/users.js (Phase 3g — Identity + Users page extraction).
    # Concat so the existing regex-shape assertions stay valid.
    return _INDEX_HTML.read_text(encoding="utf-8") + "\n" + _USERS_JS.read_text(encoding="utf-8")


# ── Card structure ──────────────────────────────────────────────────────────


def test_pod_context_card_exists(html: str):
    """The card itself must still be in the DOM."""
    assert 'id="identity-pod-context-card"' in html, (
        "Pod Context card markup is gone. Operator-facing 'where am I "
        "deployed' surface lost."
    )


def test_pod_context_card_still_read_only(html: str):
    """The card stays read-only by design. There should be no edit
    affordance — no save button, no editable input, no contenteditable."""
    # Pull a window of HTML around the card so we don't false-positive
    # on edit affordances elsewhere on the page.
    start = html.find('id="identity-pod-context-card"')
    assert start > 0, "Pod Context card not located"
    # Card is contained in its <div class="card">. Pull ~1500 chars
    # after — covers the card + help-text details block but stops
    # before adjacent sections.
    window = html[start:start + 3000]
    for editing_marker in (
        'contenteditable',
        '<input ',
        '<textarea',
        'onclick="savePodContext',
        'onclick="updatePodContext',
    ):
        assert editing_marker not in window, (
            f"Pod Context card region contains {editing_marker!r} — "
            f"the card is supposed to be read-only. If you genuinely "
            f"want editing here, update this test with rationale."
        )


# ── Tooltip pattern unification ─────────────────────────────────────────────


def test_pod_context_uses_help_btn_pattern_not_bare_questionmark(html: str):
    """Each field's tooltip must use ``<span class="help-btn">?<span
    class="tip">...</span></span>`` — same pattern as the Pod Admins
    and Per-bot Owners cards. The earlier ``<span class="subtle"
    title="...">(?)</span>`` pattern looked like literal text and
    was inconsistent with the rest of the subtab."""
    # The render function _idRenderPodContext is the source of truth.
    m = re.search(
        r"function\s+_idRenderPodContext\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _idRenderPodContext function body"
    body = m.group(1)

    # New pattern present
    assert 'class="help-btn"' in body, (
        "_idRenderPodContext no longer uses the help-btn pattern. "
        "Reverting to bare ``(?)`` looks inconsistent with the rest "
        "of the Identity subtab and loses the styled popover."
    )
    assert 'class="tip"' in body, (
        "_idRenderPodContext no longer uses the tip popover. "
        "Tooltips would fall back to the browser's native title "
        "behavior (less discoverable, inconsistent styling)."
    )
    # Old pattern gone
    assert '">(?)<' not in body, (
        "_idRenderPodContext still renders bare ``(?)`` text. The "
        "tooltip pattern was supposed to switch to help-btn for "
        "consistency."
    )


def test_pod_context_all_three_fields_have_tooltips(html: str):
    """The earlier rendering had tips on hostname + admin user but
    not on Admin URL. The unified version covers all three so the
    operator gets the override workflow for the URL too."""
    m = re.search(
        r"function\s+_idRenderPodContext\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _idRenderPodContext function body"
    body = m.group(1)
    # Count help-btn occurrences — must be at least 3 (hostname,
    # admin user, admin URL).
    n_help = body.count('class="help-btn"')
    assert n_help >= 3, (
        f"_idRenderPodContext has only {n_help} help-btn tooltips; "
        f"expected ≥3 (one per field: hostname, admin user, admin URL). "
        f"If the Admin URL tooltip went away, the operator loses the "
        f"override workflow guidance."
    )


def test_pod_context_admin_url_tooltip_mentions_override(html: str):
    """The Admin URL tip must mention the override paths (network.adminBaseUrl
    + EVOLVE_ADMIN_BASE_URL env var). That's the only field on the card
    with a real change-it path; if the tip doesn't say so, operators
    don't know they can override it."""
    m = re.search(
        r"function\s+_idRenderPodContext\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        html,
    )
    assert m, "could not locate _idRenderPodContext function body"
    body = m.group(1)
    assert (
        "adminBaseUrl" in body
        or "EVOLVE_ADMIN_BASE_URL" in body
    ), (
        "the Admin URL tooltip no longer mentions either override "
        "path. Operators need to know `network.adminBaseUrl` (JSON) "
        "or `EVOLVE_ADMIN_BASE_URL` (env var) is how you pin a value."
    )


# ── Help-text expansion ──────────────────────────────────────────────────────


def test_pod_context_has_change_it_details_block(html: str):
    """A ``<details>`` block under the card covers the four change-it
    workflows. Without it, operators land on a read-only card with
    no clue what to do if they need to actually change something."""
    start = html.find('id="identity-pod-context-card"')
    assert start > 0, "Pod Context card not located"
    window = html[start:start + 4000]
    assert '<details' in window, (
        "the Pod Context card no longer has a <details> 'If you need "
        "to change these…' block. Operators who land on this card "
        "want to know how to change a value — keep the expandable "
        "guidance."
    )
    # And the four scenarios must all be named.
    for scenario_keyword in (
        "Renamed your machine",
        "Changed admin username",
        "Moving to a new machine",
        "Admin URL needs an override",
    ):
        assert scenario_keyword in window, (
            f"the change-it details block no longer covers {scenario_keyword!r}. "
            f"The four scenarios were chosen to match the operator's "
            f"real questions; dropping any of them reopens the doc gap."
        )


def test_pod_context_change_it_block_mentions_restart_daemon(html: str):
    """The 'renamed your machine' scenario must mention restarting
    the admin daemon — otherwise operators rename the machine, the
    Admin URL stays stale, and they don't know why."""
    start = html.find('id="identity-pod-context-card"')
    window = html[start:start + 4000]
    assert (
        "launchctl kickstart" in window
        or "restart the admin daemon" in window
    ), (
        "the rename-machine workflow doesn't mention restarting the "
        "admin daemon. Without that step the URL stays stale; that's "
        "an easy source of operator confusion."
    )


def test_pod_context_change_it_block_mentions_evolve_admin_setup(html: str):
    """The 'changed admin username' scenario must point at re-running
    evolve-admin setup — that's how sudoers grants get migrated to
    the new user."""
    start = html.find('id="identity-pod-context-card"')
    window = html[start:start + 4000]
    assert "evolve-admin setup" in window, (
        "the change-admin-username workflow doesn't reference "
        "evolve-admin setup. That's the only path that migrates "
        "sudoers grants; without it operators try to rename a Unix "
        "user from a web form."
    )


# ── Card subtitle updated ───────────────────────────────────────────────────


def test_pod_context_subtitle_no_longer_says_just_change_in_settings(html: str):
    """The old subtitle was a one-liner that hand-waved the actual
    workflow. The new subtitle points operators at the per-field
    tooltips + the change-it block. Catches a revert."""
    start = html.find('id="identity-pod-context-card"')
    end = html.find('id="identity-pod-context-state"', start)
    subtitle_region = html[start:end] if end > start else ""
    # The new subtitle explicitly tells operators the fields are
    # read-only because they're system-level state — not because
    # the UI just doesn't expose the editor.
    assert (
        "system-level state" in subtitle_region
        or "not configurable knobs" in subtitle_region
    ), (
        "the Pod Context subtitle no longer explains WHY the card "
        "is read-only. Without that, operators read it as 'UI is "
        "missing the editor' rather than 'these aren't editable by "
        "design.'"
    )