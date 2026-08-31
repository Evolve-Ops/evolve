"""tests/test_agents_append_applier.py — the AgentsAppend applier.

Registration is the cheap half. The failure this applier exists to kill
(proposal 9d8fec97, efficiency_hawk) died in ``capture_snapshot`` before
anything was written, so the snapshot/apply/revert round-trip is asserted
against the file's actual bytes, not against ``ApplyResult``.

Path resolution is exercised for real: only ``soul_edit._bot_home`` is
redirected at tmp_path, so the tests pin that the target really is
``<bot home>/.openclaw/workspace/AGENTS.md``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.appliers import get_applier, known_action_kinds  # noqa: E402
from arbiter.snapshot import capture  # noqa: E402
from schema.proposal import AgentsAppend, RevertPlan  # noqa: E402

BOT = "team_bot_a"


@pytest.fixture()
def agents_md(tmp_path, monkeypatch):
    """Redirect the bot home; return the real AGENTS.md target path."""
    from arbiter.appliers import soul_edit

    monkeypatch.setattr(soul_edit, "_bot_home", lambda bot_id: tmp_path)
    return tmp_path / ".openclaw" / "workspace" / "AGENTS.md"


def _action(section: str = "Streamline — email", content: str = "Be brief.") -> AgentsAppend:
    return AgentsAppend(bot_id=BOT, section=section, content=content)


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────


def test_applier_is_registered():
    assert get_applier("AgentsAppend") is not None
    assert "AgentsAppend" in known_action_kinds()


def test_registered_by_the_package_import_alone():
    """The production failure was "the module wasn't imported".

    Importing ``arbiter.appliers.agents_append`` anywhere in this test file
    would register the kind as a side effect and make the assertion above
    pass even with the ``__init__.py`` import deleted. A fresh interpreter
    that imports only the package is the check that actually bites.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, %r);\n"
            "from arbiter.appliers import known_action_kinds;\n"
            "print('AgentsAppend' in known_action_kinds())" % str(_ANALYZER_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True", proc.stdout + proc.stderr


def test_target_is_agents_md_under_workspace(agents_md):
    from arbiter.appliers import soul_edit

    assert soul_edit._target_path(BOT, "agents") == agents_md


# ─────────────────────────────────────────────────────────────────────────────
# capture_snapshot — where proposal 9d8fec97 actually died
# ─────────────────────────────────────────────────────────────────────────────


def test_snapshot_on_missing_file(agents_md):
    applier = get_applier("AgentsAppend")
    snap = applier.capture_snapshot(_action(), BOT)
    assert snap["existed_before"] is False
    assert snap["prior_content"] == ""
    assert snap["path"] == str(agents_md)
    assert snap["action_kind"] == "AgentsAppend"
    assert not agents_md.exists()  # snapshot must not create the file


def test_snapshot_on_existing_file_captures_exact_bytes(agents_md):
    original = "# Agents\n\n## Tone\n\nBe direct.\n"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(original, encoding="utf-8")

    snap = get_applier("AgentsAppend").capture_snapshot(_action(), BOT)
    assert snap["existed_before"] is True
    assert snap["prior_content"] == original


def test_capture_through_arbiter_snapshot_module(agents_md):
    """The real entry point: arbiter.snapshot.capture() dispatches by kind.

    This is the call that raised ``No applier registered for action kind
    'AgentsAppend'`` and flipped the proposal to failed_flagged.
    """
    plan = capture(_action(), BOT)
    assert plan.before_snapshot["action_kind"] == "AgentsAppend"
    assert plan.revert_action.kind == "AgentsAppend"


# ─────────────────────────────────────────────────────────────────────────────
# apply — additive, newline-safe
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_creates_file_when_absent(agents_md):
    result = get_applier("AgentsAppend").apply(
        _action(section="Streamline — email", content="Preload the thread."), BOT
    )
    assert result.ok, result.message
    assert agents_md.read_text(encoding="utf-8") == (
        "\n## Streamline — email\n\nPreload the thread.\n"
    )


def test_apply_appends_to_file_with_trailing_newline(agents_md):
    original = "# Agents\n\nExisting guidance.\n"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(original, encoding="utf-8")

    assert get_applier("AgentsAppend").apply(_action(content="Keep it short."), BOT).ok
    text = agents_md.read_text(encoding="utf-8")
    assert text.startswith(original)  # additive only — nothing rewritten
    assert text == original + "\n## Streamline — email\n\nKeep it short.\n"


def test_apply_appends_to_file_without_trailing_newline(agents_md):
    original = "# Agents\n\nExisting guidance."  # no trailing newline
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(original, encoding="utf-8")

    assert get_applier("AgentsAppend").apply(_action(content="Keep it short."), BOT).ok
    text = agents_md.read_text(encoding="utf-8")
    assert text.startswith(original)
    # A newline is interposed so the heading cannot glue onto the last line.
    assert "guidance.\n" in text
    assert text == original + "\n\n## Streamline — email\n\nKeep it short.\n"


def test_apply_twice_keeps_both_sections(agents_md):
    applier = get_applier("AgentsAppend")
    assert applier.apply(_action(section="First", content="One."), BOT).ok
    assert applier.apply(_action(section="Second", content="Two."), BOT).ok
    text = agents_md.read_text(encoding="utf-8")
    assert "## First" in text and "## Second" in text
    assert text.index("## First") < text.index("## Second")


# ─────────────────────────────────────────────────────────────────────────────
# Heading rendering — the shipping producers embed their own heading
# ─────────────────────────────────────────────────────────────────────────────


def test_producer_supplied_heading_is_not_doubled(agents_md):
    """efficiency_hawk/persona_tuner put a ``##`` heading inside content."""
    content = "## Streamline — email / triaging\n\nSessions run long.\n"
    assert get_applier("AgentsAppend").apply(
        _action(section="Streamline — email", content=content), BOT
    ).ok
    text = agents_md.read_text(encoding="utf-8")
    assert text.count("## Streamline") == 1
    assert "## Streamline — email\n" not in text  # the section label was not prepended
    assert "## Streamline — email / triaging" in text


def test_render_section_variants():
    from arbiter.appliers.agents_append import _render_section

    assert _render_section("Tone", "Be brief.") == "## Tone\n\nBe brief."
    assert _render_section("Tone", "# Own heading\n\nBody") == "# Own heading\n\nBody"
    assert _render_section("", "Be brief.") == "Be brief."
    assert _render_section("Tone", "") == "## Tone"


# ─────────────────────────────────────────────────────────────────────────────
# revert — exact restore / delete
# ─────────────────────────────────────────────────────────────────────────────


def test_revert_restores_exact_prior_bytes(agents_md):
    original = "# Agents\n\n## Tone\n\nBe direct.\n\ntrailing   spaces   \n"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(original, encoding="utf-8")

    applier = get_applier("AgentsAppend")
    snap = applier.capture_snapshot(_action(), BOT)
    assert applier.apply(_action(), BOT).ok
    assert agents_md.read_text(encoding="utf-8") != original

    assert applier.revert(snap, BOT).ok
    assert agents_md.read_text(encoding="utf-8") == original


def test_revert_deletes_file_that_did_not_exist(agents_md):
    applier = get_applier("AgentsAppend")
    snap = applier.capture_snapshot(_action(), BOT)
    assert applier.apply(_action(), BOT).ok
    assert agents_md.exists()

    assert applier.revert(snap, BOT).ok
    assert not agents_md.exists()


def test_revert_is_idempotent_on_already_deleted_file(agents_md):
    applier = get_applier("AgentsAppend")
    snap = applier.capture_snapshot(_action(), BOT)
    assert applier.apply(_action(), BOT).ok
    assert applier.revert(snap, BOT).ok
    assert applier.revert(snap, BOT).ok  # second revert must not raise
    assert not agents_md.exists()


def test_revert_survives_the_json_round_trip(agents_md):
    """Revert runs in the verify daemon, off the persisted proposal JSON.

    The RevertPlan is written to disk between apply and revert, so the
    snapshot must survive ``RevertPlan.to_dict()`` → JSON → ``from_dict``
    and still restore the exact prior bytes.
    """
    original = "# Agents\n\nPrior guidance.\n"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(original, encoding="utf-8")

    plan = capture(_action(), BOT)
    applier = get_applier("AgentsAppend")
    assert applier.apply(_action(), BOT).ok

    rehydrated = RevertPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert rehydrated.revert_action.kind == "AgentsAppend"
    # The verify daemon dispatches on revert_action.kind, then reverts.
    assert get_applier(rehydrated.revert_action.kind).revert(
        rehydrated.before_snapshot, BOT
    ).ok
    assert agents_md.read_text(encoding="utf-8") == original
