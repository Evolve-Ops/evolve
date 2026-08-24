"""Tests for the Tier-1 capability-index menu + the public Tier-2 detail renderer
(app_registry recognition layer; spec-app-invocation-just-works §2.1).

The always-on AGENTS.md menu (``_render_agents_md_section``) must:
  - list DEFINED (operator-vouched) apps only — never churny ``discovered`` drafts
    (spec OQ-3);
  - end each entry with the ``expand_app("<id>")`` Tier-2 pointer;
  - read as a *menu*, not a rigid trigger (the "just works, not rigid" contract) —
    it must not tell the model to pattern-match a keyword;
  - degrade honestly when nothing is defined.

The detail renderer (``render_app_detail_section``) is the on-demand ``expand_app``
payload — the same per-app block as INSTALLED_APPS.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.applications import app_registry as ar  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


def _mk(**kw) -> ApplicationManifest:
    base = {"bot_id": "atlas", "status": "active"}
    base.update(kw)
    return ApplicationManifest.from_dict(base)


DEFINED = _mk(
    id="task_manager", name="task_manager", display_name="Task Manager",
    definition_status="defined",
    identity={"purpose": "Track your to-dos and reminders"},
    interface_contract={"cli": [{"command": "tasks.py add", "key_flags": ["--due"]}]},
)
DISCOVERED = _mk(
    id="scratch", name="scratch", display_name="Scratch",
    definition_status="discovered",
    identity={"purpose": "A discovered scratch script"},
)


# ── helpers ───────────────────────────────────────────────────────────────────


def test_app_ref_prefers_id():
    assert ar.app_ref(DEFINED) == "task_manager"
    # Falls back to name/display_name when id is blank.
    assert ar.app_ref(_mk(id="", name="jrnl", display_name="Journal")) == "jrnl"
    assert ar.app_ref(_mk(id="", name="", display_name="Journal")) == "Journal"


def test_is_defined():
    assert ar.is_defined(DEFINED) is True
    assert ar.is_defined(DISCOVERED) is False
    # Default (unset) definition_status is discovered → not defined.
    assert ar.is_defined(_mk(id="x", name="x")) is False


# ── Tier-1 menu ───────────────────────────────────────────────────────────────


def test_menu_lists_defined_only():
    out = ar._render_agents_md_section([DEFINED, DISCOVERED])
    assert "Task Manager" in out
    # The discovered app is NOT in the always-on menu (OQ-3 — no scanner churn).
    assert "Scratch" not in out
    assert "1 Evolve-managed app installed" in out


def test_menu_entry_carries_expand_app_pointer():
    out = ar._render_agents_md_section([DEFINED])
    # Per-line Tier-2 trigger with the canonical id.
    assert '`expand_app("task_manager")`' in out
    # And the tool is explained once up top.
    assert 'expand_app("<id>")' in out


def test_menu_is_a_menu_not_a_rigid_trigger():
    """The anti-rigidity contract: the menu must frame apps as the model's choice
    from intent, never instruct it to fire on a keyword/pattern match."""
    out = ar._render_agents_md_section([DEFINED]).lower()
    assert "it's your call" in out
    assert "not from a rigid keyword match" in out
    # The old rigid phrasing ("when a user message matches ... call the app's CLI")
    # must be gone.
    assert "message matches" not in out


def test_menu_empty_state_when_nothing_defined_but_discovered_exists():
    """A bot with only discovered apps must NOT be told it has zero apps."""
    out = ar._render_agents_md_section([DISCOVERED])
    assert "no Evolve-managed apps installed yet" not in out
    assert "still being characterized" in out
    # Still no discovered names leaked into the always-on context.
    assert "Scratch" not in out


def test_menu_true_empty_state():
    out = ar._render_agents_md_section([])
    assert "no Evolve-managed apps installed yet" in out


def test_menu_markers_present():
    out = ar._render_agents_md_section([DEFINED])
    assert out.startswith(ar._AGENTS_MARKER_BEGIN)
    assert out.rstrip().endswith(ar._AGENTS_MARKER_END)


# ── Tier-2 detail ─────────────────────────────────────────────────────────────


def test_detail_renders_usage_and_cli():
    detail = ar.render_app_detail_section(DEFINED)
    assert detail.startswith("## Task Manager")
    assert "How to use" in detail
    assert "tasks.py add" in detail
