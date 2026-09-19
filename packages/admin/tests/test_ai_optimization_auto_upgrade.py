"""tests/test_ai_optimization_auto_upgrade.py — top-level auto-upgrade UI.

Spec: internal/spec-model-auto-upgrade-2026-07-30.md §Scope. The admin SPA has no
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
  5. The consolidated row SAYS which pin it currently stands on
     ("claude-sonnet · latest (now 4-6)"), and the picker's greyed entry says
     WHERE that id lives ("in Standard as claude-sonnet · latest") instead of
     a bare "(already added)". Operator report 2026-08-24: the two surfaces
     were both correct and read as a contradiction, because neither one said
     that the consolidated row IS the pinned id.
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
    assert "Right now this line is" in region
    assert "Kept behind it as fallback" in region


# ── 4. Honest display: name the pin, name where it lives (2026-08-24) ─────────

def test_family_row_label_names_the_standing_version(js: str):
    """The version has to be in the LABEL, not only a tooltip.

    "claude-sonnet · latest" next to a picker greying "claude-sonnet-4-6
    (already added)" is what the operator read as a contradiction. A hover-only
    disclosure does not answer it — the row itself must say "(now 4-6)".
    """
    region = _family_region(js)
    assert "${fam} · latest (now ${_aiPinVersionLabel(head, fam)})" in region, \
        "the consolidated chip label must name the version it stands on"
    # Head-of-chain, because the resolver routes to the first credentialed
    # member — the row must stand on the id routing would actually pick.
    assert "const head = (models && models[0]) || ''" in region


def test_pin_version_label_falls_back_to_the_whole_id(js: str):
    """A family stem is not always a prefix of the id.

    model_discovery._family_of maps "gemini-2.5-flash" to "gemini-flash" (the
    version sits mid-string), so clipping a "<fam>-" prefix would have to guess.
    The helper returns the full bare id in that case rather than inventing a
    fragment.
    """
    region = _family_region(js)
    m = re.search(
        r"function _aiPinVersionLabel\(model, fam\) \{(.*?)\n\}", region, re.DOTALL,
    )
    assert m, "could not locate _aiPinVersionLabel"
    body = m.group(1)
    assert "stem + '-'" in body, "must test the stem as a literal prefix"
    assert ": bare;" in body, "non-prefix families must fall back to the bare id"


def test_family_row_hover_text_reaches_the_chip(js: str):
    """The chip's own title shadows a title on its wrapper.

    _aiModelChip renders title="<provider>" on the chip element itself, so a
    disclosure hung on the wrapping <span> is only reachable in the few pixels
    around the chip. The consolidated row passes its text through to the chip.
    """
    region = _family_region(js)
    assert "{ title: covered," in region, \
        "the family row must override the chip title, not only the wrapper's"
    code = _strip_comments(js)
    assert "const title = o.title || (_aiProviderLabel(provider) || 'provider unknown')" in code


def test_picker_already_note_names_tier_and_consolidated_row(js: str):
    """The greyed picker entry says WHERE the id lives."""
    region = _family_region(js)
    m = re.search(
        r"function _aiAlreadyNoteFor\(scope, tierLabel\) \{(.*?)\n\}\n", region, re.DOTALL,
    )
    assert m, "could not locate _aiAlreadyNoteFor"
    body = m.group(1)
    # Auto ON: name the tier AND the consolidated row the id folds into.
    assert "`in ${tierLabel} as ${_aiFamilyOf(id, state.families)} · latest`" in body
    # Auto OFF: the row shows the exact id already, so the tier alone suffices.
    assert "`already in ${tierLabel}`" in body


def test_both_tier_editors_pass_the_already_note(js: str):
    """Display-only honesty is worthless on one of the two editors."""
    code = _strip_comments(js)
    assert "alreadyNote: _aiAlreadyNoteFor('bot', _AI_ROLE_LABELS[roleId] || roleId)" in code
    assert "alreadyNote: _aiAlreadyNoteFor('pod', label)" in code
