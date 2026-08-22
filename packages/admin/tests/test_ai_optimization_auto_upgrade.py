"""tests/test_ai_optimization_auto_upgrade.py — top-level auto-upgrade UI.

Spec: docs/spec-model-auto-upgrade-2026-07-30.md §Scope. The admin SPA has no
JS test harness; the established pattern is to pin UI behaviour by asserting
on ai-optimization.js *source strings*. These pin that:

  1. The "Automatically use the latest version of each model" toggle is a
     TOP-LEVEL control on the tier cards — the pod-defaults editor and each
     bot tab — wired to PUT /api/models/auto-upgrade. It is NOT inside the
     easy-setup modal any more (the modal only mirrors the posture).
  2. A Use-defaults bot renders the toggle disabled (it follows the pod —
     spec §Scope), and the pod toggle states its governance split.
  3. When a scope's policy is ON, the tier editors consolidate pinned
     versions into ONE "family · latest" row per (provider, family) — a
     display-only grouping: group ops re-flatten the SAME pending buffer, so
     the saved config keeps every concrete pinned id.
  4. Unknown policy (fetch failed) degrades to pinned-id rows and no toggle —
     the UI never guesses a policy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


# ── 1. Top-level toggle on the tier cards, not the modal ──────────────────────

def test_toggle_rendered_on_pod_and_bot_cards(js: str):
    code = _strip_comments(js)
    # Pod defaults card owns its toggle...
    assert "_aiAutoUpgradeToggleRow('pod', _aiPodAuto, true)" in code
    # ...a Custom bot owns its own...
    assert "_aiAutoUpgradeToggleRow(botId, _aiBotAuto, true)" in code
    # ...and a Use-defaults bot shows it DISABLED (follows the pod).
    assert "_aiAutoUpgradeToggleRow(botId, _aiBotAuto, false)" in code


def test_toggle_writes_through_dedicated_endpoint(js: str):
    code = _strip_comments(js)
    assert "'/api/models/auto-upgrade'" in code
    assert "function _aiToggleAutoUpgrade(" in code
    # The PUT sends scope + a real bool, mirroring the server's bool-only gate.
    assert re.search(r"api\('PUT',\s*'/api/models/auto-upgrade',\s*\{\s*scope:", code)


def test_modal_no_longer_owns_the_toggle(js: str):
    code = _strip_comments(js)
    # The old modal checkbox and its handler are gone...
    assert "_aiEasyToggleAutoUpgrade" not in code
    assert "ai-easy-auto-upgrade" not in code
    # ...and the easy-setup write no longer carries the policy: the modal
    # never writes autoUpgrade (the route leaves an absent key untouched).
    assert "auto_upgrade_enabled" not in code


def test_pod_toggle_states_governance(js: str):
    code = _strip_comments(js)
    start = code.find("function _aiAutoUpgradeToggleRow(")
    end = code.find("function _aiToggleAutoUpgrade(")
    assert start != -1 and end != -1
    region = code[start:end]
    # Spec §migration-era hazard: the pod toggle names how many bots it
    # governs and which Custom bots are excluded.
    assert "Governs the" in region
    assert "Custom bots keep their own setting" in region


# ── 2. Unknown policy degrades honestly ───────────────────────────────────────

def test_unknown_state_renders_nothing(js: str):
    code = _strip_comments(js)
    start = code.find("function _aiAutoUpgradeToggleRow(")
    assert start != -1
    region = code[start:code.find("{", start) + 200]
    # First statement bails on a null state — no checkbox that lies.
    assert "if (!state) return ''" in region


# ── 3. Consolidated "family · latest" rows ────────────────────────────────────

def _family_region(js: str) -> str:
    code = _strip_comments(js)
    start = code.find("function _aiFamilyGroups(")
    end = code.find("function _aiAutoUpgradeToggleRow(")
    assert start != -1 and end != -1
    return code[start:end]


def test_editors_consolidate_when_auto_on(js: str):
    code = _strip_comments(js)
    # Both editors branch on the scope's policy and group via families.
    assert "_aiScopeAutoOn('pod')" in code
    assert "_aiScopeAutoOn('bot')" in code
    assert code.count("_aiFamilyGroups(") >= 4  # pod editor, bot editor, read-only view, group ops
    # The consolidated chip is the version-less family form.
    assert "· latest" in code


def test_group_ops_reflatten_concrete_ids(js: str):
    region = _family_region(js)
    # Reorder/remove rebuild the flat pinned-id chain from the groups — the
    # save payload keeps every concrete id (display-only consolidation).
    assert "flatMap(g => g.models)" in region


def test_family_rows_are_not_draggable(js: str):
    region = _family_region(js)
    # DnD splices flat model indices, which don't line up with group rows —
    # the group row must not opt into the drag handlers.
    assert "draggable" not in region
    assert "ondragstart" not in region


def test_family_row_tooltip_names_pinned_ids(js: str):
    region = _family_region(js)
    # The consolidated tile stays honest: the covered pinned ids are visible.
    assert "Currently pinned" in region
