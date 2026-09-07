"""tests/test_evo_notifications.py — per-user notification queue + session_surface
integration + forge_engine auto_approve_actor parameter (slice 5b7a).

Spec: internal/spec-forge-via-messaging-2026-05-07.md.

Exercises:
  * notifications.append_event / read_unread / mark_read round-trip
  * Multiple events accumulate; mark_read resets read state per call
  * Stable filename encoding (colons in user_key don't break paths)
  * Renderer surfaces forge_complete / forge_failed correctly
  * session_surface invokes the queue when --user-key is set; skips
    when not; integrates the rendered block into the prefix
  * forge_engine's run_forge_job auto_approve_actor parameter shape
    (signature check; full integration deferred to 5b7b)
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ─────────────────────────────────────────────────────────────────────────────
# Notification queue — round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_append_then_read_returns_events(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(
        tmp_path, "ext:telegram:12345",
        kind="forge_complete", bot_id="team_bot_a",
        app_name="calendar-watcher", app_id="app_cal",
        summary="Watches calendar", detail="Runs every 30 min",
    )
    events = _n.read_unread(tmp_path, "ext:telegram:12345")
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "forge_complete"
    assert e["bot_id"] == "team_bot_a"
    assert e["app_name"] == "calendar-watcher"
    assert e["summary"] == "Watches calendar"
    assert e["detail"] == "Runs every 30 min"
    assert "ts" in e


def test_mark_read_clears_subsequent_reads(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(
        tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
        app_name="x", app_id="app_x",
    )
    assert len(_n.read_unread(tmp_path, "u")) == 1
    _n.mark_read(tmp_path, "u")
    assert len(_n.read_unread(tmp_path, "u")) == 0


def test_new_events_after_mark_read_show_up(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(tmp_path, "u", kind="forge_complete", bot_id="team_bot_a")
    _n.mark_read(tmp_path, "u")
    _n.append_event(
        tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
        app_name="new-one",
    )
    events = _n.read_unread(tmp_path, "u")
    assert len(events) == 1
    assert events[0]["app_name"] == "new-one"


def test_read_returns_empty_for_unknown_user(tmp_path):
    from evolve_admin.evo import notifications as _n
    assert _n.read_unread(tmp_path, "ext:telegram:99999") == []


def test_mark_read_on_empty_queue_is_noop(tmp_path):
    """mark_read with no prior events should not crash."""
    from evolve_admin.evo import notifications as _n
    _n.mark_read(tmp_path, "u")  # no error


def test_user_key_with_colons_writes_safe_filename(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(
        tmp_path, "ext:telegram:12345",
        kind="forge_complete", bot_id="team_bot_a",
    )
    qpath = _n.queue_path(tmp_path, "ext:telegram:12345")
    assert ":" not in qpath.name
    assert qpath.exists()


def test_append_rejects_empty_user_key(tmp_path):
    from evolve_admin.evo import notifications as _n
    with pytest.raises(ValueError):
        _n.append_event(tmp_path, "", kind="x", bot_id="team_bot_a")


def test_append_rejects_empty_kind(tmp_path):
    from evolve_admin.evo import notifications as _n
    with pytest.raises(ValueError):
        _n.append_event(tmp_path, "u", kind="", bot_id="team_bot_a")


def test_corrupt_lines_are_skipped(tmp_path):
    """A garbled line in the middle of the queue shouldn't lose
    well-formed events around it."""
    from evolve_admin.evo import notifications as _n

    qpath = _n.queue_path(tmp_path, "u")
    qpath.parent.mkdir(parents=True, exist_ok=True)
    _n.append_event(tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
                    app_name="first")
    # Inject malformed line directly
    with qpath.open("ab") as fh:
        fh.write(b"this is not json\n")
    _n.append_event(tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
                    app_name="second")

    events = _n.read_unread(tmp_path, "u")
    names = [e.get("app_name") for e in events]
    assert names == ["first", "second"]


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


def test_render_empty_returns_empty_string():
    from evolve_admin.evo import notifications as _n
    assert _n.render_for_session_prompt([]) == ""


def test_render_forge_complete_surfaces_summary_and_detail():
    from evolve_admin.evo import notifications as _n

    block = _n.render_for_session_prompt([{
        "ts": "2026-05-07T10:00:00Z",
        "kind": "forge_complete",
        "bot_id": "team_bot_a",
        "app_name": "calendar-watcher",
        "summary": "Watches your calendar.",
        "detail": "Runs every 30 min. Reply 'snooze' to mute.",
    }])
    assert "Build complete" in block
    assert "calendar-watcher" in block
    assert "Watches your calendar" in block
    assert "Runs every 30 min" in block


def test_render_forge_failed_surfaces_detail_and_revise_hint():
    from evolve_admin.evo import notifications as _n

    block = _n.render_for_session_prompt([{
        "ts": "2026-05-07T10:00:00Z",
        "kind": "forge_failed",
        "bot_id": "team_bot_a",
        "app_name": "bad-app",
        "summary": "The original spec.",
        "detail": "Tests failed: ImportError",
    }])
    assert "Build failed" in block
    assert "bad-app" in block
    assert "ImportError" in block
    assert "try again" in block.lower() or "shelve" in block.lower()


def test_render_unknown_kind_doesnt_crash():
    from evolve_admin.evo import notifications as _n
    block = _n.render_for_session_prompt([{
        "kind": "something_new", "detail": "hello world",
    }])
    assert block != ""
    assert "hello world" in block


def test_render_block_carries_lead_in_instruction():
    """The rendered block tells the LLM how to surface the events
    naturally rather than dumping the block verbatim."""
    from evolve_admin.evo import notifications as _n

    block = _n.render_for_session_prompt([{
        "kind": "forge_complete", "app_name": "x", "bot_id": "team_bot_a",
        "summary": "S", "detail": "D",
    }])
    assert "Lead your next reply" in block or "weave it in" in block


# ─────────────────────────────────────────────────────────────────────────────
# session_surface integration (subprocess invocation, mirrors plugin path)
# ─────────────────────────────────────────────────────────────────────────────


def _run_session_surface(*extra_args, shared_dir):
    """Run session_surface.py the way the plugin does. Returns (rc, stdout, stderr).

    The plugin invokes the shared venv's python3, where evolve-admin and
    evolve-analyzer are pip-installed (session_surface imports
    evolve_admin.*). There's no pod venv on dev/CI machines, so mirror
    that interpreter contract with sys.executable + PYTHONPATH pointing
    at THIS worktree's packages (not whatever editable install the dev
    environment carries).
    """
    script = _ANALYZER_DIR / "session_surface.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in (_ADMIN_DIR, _ANALYZER_DIR))
    proc = subprocess.run(
        [sys.executable, str(script),
         "--bot", "team_bot_a", "--shared-dir", str(shared_dir), *extra_args],
        capture_output=True, text=True, timeout=20, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_session_surface_without_user_key_skips_notifications(tmp_path):
    """Without --user-key, session_surface skips the notifications block
    entirely (no surface to read for)."""
    from evolve_admin.evo import notifications as _n

    _n.append_event(tmp_path, "ext:telegram:12345", kind="forge_complete",
                    bot_id="team_bot_a", app_name="should-not-appear")
    rc, stdout, _stderr = _run_session_surface(shared_dir=tmp_path)
    assert rc == 0
    assert "EVOLVE NOTIFICATIONS" not in stdout
    assert "should-not-appear" not in stdout


def test_session_surface_with_user_key_surfaces_notifications(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(tmp_path, "ext:telegram:12345", kind="forge_complete",
                    bot_id="team_bot_a", app_name="calendar-watcher",
                    summary="watches calendar", detail="runs every 30 min")
    rc, stdout, _stderr = _run_session_surface(
        "--user-key", "ext:telegram:12345",
        shared_dir=tmp_path,
    )
    assert rc == 0
    assert "EVOLVE NOTIFICATIONS" in stdout
    assert "calendar-watcher" in stdout


def test_session_surface_marks_notifications_read(tmp_path):
    """A second session_surface run with the same user_key should see
    no notifications — the first run marked them read."""
    from evolve_admin.evo import notifications as _n

    _n.append_event(tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
                    app_name="x")
    _run_session_surface("--user-key", "u", shared_dir=tmp_path)
    rc, stdout, _stderr = _run_session_surface("--user-key", "u",
                                               shared_dir=tmp_path)
    assert rc == 0
    assert "EVOLVE NOTIFICATIONS" not in stdout


def test_session_surface_json_includes_notifications_field(tmp_path):
    from evolve_admin.evo import notifications as _n

    _n.append_event(tmp_path, "u", kind="forge_complete", bot_id="team_bot_a",
                    app_name="x", summary="S", detail="D")
    rc, stdout, _stderr = _run_session_surface(
        "--user-key", "u", "--json",
        shared_dir=tmp_path,
    )
    assert rc == 0
    data = json.loads(stdout)
    assert "notifications" in data
    assert data["notifications"]  # non-empty


def test_build_session_prefix_orders_blocks_correctly():
    """conduct → guide → notifications (per spec's ordering).

    The legacy 'tasks' block was removed when Continuity v1's task queue
    was deleted — under v2 the bot owns its own defer queue, no
    operator-approval surface to inject at session start.
    """
    from session_surface import build_session_prefix

    prefix = build_session_prefix(
        guide_block="[BOT GUIDE — test guide block]",
        notifications_block="[EVOLVE NOTIFICATIONS — test note]",
    )
    guide_pos = prefix.find("[BOT GUIDE")
    notif_pos = prefix.find("[EVOLVE NOTIFICATIONS")
    # Guide before notifications
    assert guide_pos != -1
    assert notif_pos != -1
    assert guide_pos < notif_pos


# ─────────────────────────────────────────────────────────────────────────────
# forge_engine.run_forge_job auto_approve_actor signature
# ─────────────────────────────────────────────────────────────────────────────


def test_run_forge_job_signature_accepts_auto_approve_actor():
    """Lightweight signature check — confirms 5b7a's parameter is in
    place. Full integration is exercised in 5b7b's tests where the
    wizard kicks off real builds end-to-end."""
    import inspect
    from evolve_admin.applications import forge_engine

    sig = inspect.signature(forge_engine.run_forge_job)
    assert "auto_approve_actor" in sig.parameters
    # Default is None (operator-approval path is the legacy default)
    assert sig.parameters["auto_approve_actor"].default is None
