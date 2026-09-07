"""tests/test_evo_onboarding_gate.py — Onboarded-state marker + bare-evo orientation.

Bare ``evo`` no longer dual-routes (wizard if not onboarded, rec_pending
otherwise). It now prints a short orientation message that points at
``evo wizard`` or ``evo better`` based on whether the wizard has run.
``evo better`` is unconditional — always tries to surface a rec.

This suite covers:
  * ``wizard.state.is_onboarded`` / ``mark_onboarded`` /
    ``mark_completed`` marker semantics (unchanged).
  * The bare-``evo`` orientation handler: which next-step it suggests
    based on the onboarded marker.
  * ``evo better`` no longer pre-empts to the wizard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ──────────────────────────────────────────────────────────────────────────
# state.is_onboarded / mark_onboarded primitives
# ──────────────────────────────────────────────────────────────────────────


def test_is_onboarded_false_when_no_marker(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    assert not _state.is_onboarded(tmp_path, "team_bot_a", "ext:telegram:12345")


def test_mark_onboarded_writes_marker(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    _state.mark_onboarded(
        tmp_path, "team_bot_a", "ext:telegram:12345", audience="primary"
    )
    assert _state.is_onboarded(tmp_path, "team_bot_a", "ext:telegram:12345")


def test_marker_payload_includes_audience(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    _state.mark_onboarded(
        tmp_path, "team_bot_a", "ext:telegram:abc", audience="secondary"
    )
    marker = _state._onboarded_marker_path(
        tmp_path, "team_bot_a", "ext:telegram:abc"
    )
    payload = json.loads(marker.read_text())
    assert payload["audience"] == "secondary"
    assert payload["onboarded_at"]  # ISO timestamp present


def test_mark_onboarded_idempotent(tmp_path: Path):
    """Re-writing the marker just refreshes — doesn't error."""
    from evolve_admin.evo.wizard import state as _state

    _state.mark_onboarded(tmp_path, "team_bot_a", "u1", audience="primary")
    _state.mark_onboarded(tmp_path, "team_bot_a", "u1", audience="primary")
    assert _state.is_onboarded(tmp_path, "team_bot_a", "u1")


def test_marker_isolated_per_user_and_bot(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    _state.mark_onboarded(tmp_path, "team_bot_a", "userA", audience="primary")
    assert _state.is_onboarded(tmp_path, "team_bot_a", "userA")
    assert not _state.is_onboarded(tmp_path, "team_bot_a", "userB")
    assert not _state.is_onboarded(tmp_path, "admin_bot", "userA")


# ──────────────────────────────────────────────────────────────────────────
# mark_completed — writes marker for primary/secondary, not other audiences
# ──────────────────────────────────────────────────────────────────────────


def _persist_state(
    tmp_path: Path,
    *,
    audience: str,
    bot_id: str = "team_bot_a",
    user_key: str = "u1",
    phase: str = "wrap",
):
    from evolve_admin.evo.wizard import state as _state

    return _state.initialize(
        tmp_path,
        bot_id=bot_id,
        user_key=user_key,
        audience=audience,  # type: ignore[arg-type]
        initial_phase=phase,
    )


def test_mark_completed_writes_marker_for_primary(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    st = _persist_state(tmp_path, audience="primary")
    _state.mark_completed(tmp_path, st)
    assert _state.is_onboarded(tmp_path, "team_bot_a", "u1")


def test_mark_completed_writes_marker_for_secondary(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    st = _persist_state(tmp_path, audience="secondary")
    _state.mark_completed(tmp_path, st)
    assert _state.is_onboarded(tmp_path, "team_bot_a", "u1")


def test_mark_completed_skips_marker_for_approver(tmp_path: Path):
    """rec_pending sessions complete with audience='approver'; that's a
    proposal-approval flow, not onboarding. The marker must NOT fire."""
    from evolve_admin.evo.wizard import state as _state

    st = _persist_state(tmp_path, audience="approver")
    _state.mark_completed(tmp_path, st)
    assert not _state.is_onboarded(tmp_path, "team_bot_a", "u1")


def test_mark_completed_skips_marker_for_guide_drafter(tmp_path: Path):
    from evolve_admin.evo.wizard import state as _state

    st = _persist_state(tmp_path, audience="guide_drafter")
    _state.mark_completed(tmp_path, st)
    assert not _state.is_onboarded(tmp_path, "team_bot_a", "u1")


def test_marker_survives_subsequent_state_overwrite(tmp_path: Path):
    """The state file (one slot per (bot, user)) gets overwritten when
    rec_pending or guide_drafter sessions start. The onboarding marker is
    a SEPARATE file, so it must persist across those overwrites — that's
    the whole point of having it."""
    from evolve_admin.evo.wizard import state as _state

    # Onboard the user
    primary_st = _persist_state(tmp_path, audience="primary")
    _state.mark_completed(tmp_path, primary_st)
    assert _state.is_onboarded(tmp_path, "team_bot_a", "u1")

    # Now a rec_pending session starts and overwrites the state file
    _state.initialize(
        tmp_path,
        bot_id="team_bot_a",
        user_key="u1",
        audience="approver",
        initial_phase="rec_pending",
    )
    # Marker still here — that's the load-bearing assertion
    assert _state.is_onboarded(tmp_path, "team_bot_a", "u1")


# ──────────────────────────────────────────────────────────────────────────
# Dispatcher pre-emption gate
# ──────────────────────────────────────────────────────────────────────────


def _dispatch(network, raw_text, *, sender_external_id="12345"):
    from evolve_admin.evo.dispatch import dispatch

    return dispatch(
        network,
        bot_id="team_bot_a",
        channel="telegram",
        sender_external_id=sender_external_id,
        raw_text=raw_text,
    )


def test_bare_evo_suggests_wizard_when_not_onboarded(tmp_path: Path):
    """Brand-new user typing ``evo`` should get an orientation message
    that points at ``evo wizard`` as the next step. No wizard session
    is started — the user has to invoke ``evo wizard`` explicitly."""
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    result = _dispatch(network, "evo")
    assert result.mode == "speak"
    assert result.wizard_session_id is None
    assert result.subcommand == "evo"
    assert "evo wizard" in result.direct_send_message
    assert "evo help" in result.direct_send_message
    # Should NOT pitch `evo better` to someone who hasn't onboarded.
    assert "evo better" not in result.direct_send_message


def test_bare_evo_suggests_better_when_onboarded(tmp_path: Path):
    """Once the wizard has run, bare ``evo`` points at ``evo better``."""
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import state as _state

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="12345"
    )
    _state.mark_onboarded(tmp_path, "team_bot_a", user_key, audience="primary")

    result = _dispatch(network, "evo")
    assert result.mode == "speak"
    assert result.wizard_session_id is None
    assert "evo better" in result.direct_send_message
    assert "evo help" in result.direct_send_message
    # Should not pitch `evo wizard` to someone already onboarded.
    assert "evo wizard" not in result.direct_send_message


def test_evo_better_does_not_preempt_to_wizard(tmp_path: Path):
    """``evo better`` is unconditional — it always tries to surface a
    rec, even for un-onboarded users. The bare-``evo`` orientation
    message is what directs new users to the wizard."""
    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    result = _dispatch(network, "evo better")
    assert result.mode == "speak"
    # No wizard pre-emption — the notes should not mention onboarding.
    assert not any(
        "onboarding wizard" in (n or "") for n in result.notes
    ), result.notes


def test_new_evo_command_abandons_active_wizard(tmp_path: Path):
    """The wizard is optional — a new ``evo`` command (bare or
    subcommand) must abandon any in-flight wizard so the user is never
    trapped. After the new command, the recovery probe sees no active
    wizard."""
    from evolve_admin.evo import dispatch as _disp
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import state as _state

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="12345",
    )

    # Start a wizard so there's something to abandon.
    _engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key=user_key,
        audience="primary", role="primary",
    )
    pre = _state.read_state(tmp_path, "team_bot_a", user_key)
    assert pre is not None and pre.is_active(), "wizard should be active"

    # Now the user types `evo help` — a fresh command that doesn't start
    # its own wizard. The dispatcher must mark the in-flight wizard
    # abandoned so the next probe / restart won't re-attach to it.
    result = _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo help",
    )
    assert result.subcommand == "help"

    post = _state.read_state(tmp_path, "team_bot_a", user_key)
    assert post is not None
    assert not post.is_active(), (
        f"wizard should be inactive after a new evo command "
        f"(status={post.status})"
    )
    assert post.status == "abandoned"


def test_bare_evo_also_abandons_active_wizard(tmp_path: Path):
    """Same rule for bare ``evo`` — it's the orientation surface, and a
    user typing it mid-wizard wants out of the wizard."""
    from evolve_admin.evo import dispatch as _disp
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import state as _state

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="12345",
    )
    _engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key=user_key,
        audience="primary", role="primary",
    )

    _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo",
    )
    post = _state.read_state(tmp_path, "team_bot_a", user_key)
    assert post is not None and not post.is_active()


def test_evo_wizard_overwrites_abandoned_state(tmp_path: Path):
    """Re-running ``evo wizard`` after abandoning starts fresh, not
    resumes the abandoned session. ``initialize`` writes a new
    in_progress state regardless of the prior abandoned marker."""
    from evolve_admin.evo import dispatch as _disp
    from evolve_admin.evo.identity import derive_user_key
    from evolve_admin.evo.wizard import engine as _engine
    from evolve_admin.evo.wizard import state as _state

    network = {"members": ["team_bot_a"], "sharedDir": str(tmp_path)}
    user_key = derive_user_key(
        network, bot_id="team_bot_a", channel="telegram", sender_external_id="12345",
    )
    _engine.start_session(
        tmp_path, bot_id="team_bot_a", user_key=user_key,
        audience="primary", role="primary",
    )
    # Abandon via a new command.
    _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo help",
    )
    # Re-run wizard.
    _disp.dispatch(
        network, bot_id="team_bot_a", channel="telegram",
        sender_external_id="12345", raw_text="evo wizard",
    )
    after = _state.read_state(tmp_path, "team_bot_a", user_key)
    assert after is not None and after.is_active()
    assert after.status == "in_progress"


def test_admin_bare_evo_orientation(tmp_path: Path):
    """Per design: admin role gets the same orientation message as
    primary. Verify the suggestion is `evo wizard` for an admin who
    hasn't onboarded yet."""
    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(tmp_path),
        "pod": {
            "admins": {
                "external_ids": {"telegram": ["admin-id"]}
            }
        },
    }
    from evolve_admin.evo.identity import resolve_role

    role = resolve_role(network, "team_bot_a", "telegram", "admin-id")
    assert role == "admin"

    result = _dispatch(network, "evo", sender_external_id="admin-id")
    assert result.mode == "speak"
    assert "evo wizard" in result.direct_send_message
