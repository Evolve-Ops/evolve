"""tests/test_rsi_l6_appliers.py — SoulEdit, DeprecateApp, Throttle, Pause."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.appliers import get_applier  # noqa: E402
from arbiter.appliers.throttle_generator import (  # noqa: E402
    set_shared_dir as tg_set_shared_dir,
)
from schema.generator import GeneratorRecord, TrackRecord  # noqa: E402
from schema.proposal import (  # noqa: E402
    DeprecateApp,
    PauseGenerator,
    SoulEdit,
    ThrottleGenerator,
)


# ─────────────────────────────────────────────────────────────────────────────
# SoulEdit applier — append_section
# ─────────────────────────────────────────────────────────────────────────────


def test_soul_edit_append_section_creates_file(tmp_path, monkeypatch):
    # Redirect the hardcoded path for the bot — monkeypatch via an
    # override on _target_path? Simpler: accept that the applier writes
    # into /Users/{bot}/... ; in tests we use a bot_id that maps to tmp_path.
    # We can't easily monkeypatch that without hoisting the path function.
    # Instead, call _target_path module-level by overriding bot_id to point
    # at tmp_path using a bot_id of the form "test/abs/path" — but that
    # also embeds / which the path needs. Simplest: monkeypatch the
    # module-level _target_path function directly.
    from arbiter.appliers import soul_edit

    target = tmp_path / "SOUL.md"

    def override(bot_id, tgt):  # noqa: ARG001
        return target

    monkeypatch.setattr(soul_edit, "_target_path", override)

    applier = get_applier("SoulEdit")
    action = SoulEdit(
        bot_id="team_bot_a",
        target="soul",
        operation="append_section",
        content="## Tone\n\nBe direct.",
    )
    snap = applier.capture_snapshot(action, "team_bot_a")
    result = applier.apply(action, "team_bot_a")
    assert result.ok
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "Be direct" in text

    # Revert — file didn't exist before, so revert deletes it
    revert = applier.revert(snap, "team_bot_a")
    assert revert.ok
    assert not target.exists()


def test_soul_edit_replace_section_replaces_only_that_section(tmp_path, monkeypatch):
    from arbiter.appliers import soul_edit

    target = tmp_path / "SOUL.md"
    original = (
        "# Soul\n\n"
        "## Tone\n\nOld tone guidance.\n\n"
        "## Values\n\nBe helpful.\n"
    )
    target.write_text(original, encoding="utf-8")

    def override(bot_id, tgt):  # noqa: ARG001
        return target

    monkeypatch.setattr(soul_edit, "_target_path", override)

    applier = get_applier("SoulEdit")
    action = SoulEdit(
        bot_id="team_bot_a",
        target="soul",
        operation="replace_section",
        content="New tone guidance.",
        anchor="## Tone",
    )
    snap = applier.capture_snapshot(action, "team_bot_a")
    result = applier.apply(action, "team_bot_a")
    assert result.ok
    text = target.read_text(encoding="utf-8")
    assert "New tone guidance." in text
    assert "Be helpful." in text  # Values section untouched
    assert "Old tone guidance." not in text

    # Revert restores original
    applier.revert(snap, "team_bot_a")
    assert target.read_text(encoding="utf-8") == original


def test_soul_edit_rejects_missing_anchor_on_replace(tmp_path, monkeypatch):
    from arbiter.appliers import soul_edit

    target = tmp_path / "SOUL.md"
    target.write_text("# Soul\n\n## Values\n\nBe kind.\n", encoding="utf-8")

    def override(bot_id, tgt):  # noqa: ARG001
        return target

    monkeypatch.setattr(soul_edit, "_target_path", override)

    applier = get_applier("SoulEdit")
    action = SoulEdit(
        bot_id="team_bot_a",
        target="soul",
        operation="replace_section",
        content="new content",
        anchor="## NoSuchSection",
    )
    applier.capture_snapshot(action, "team_bot_a")
    result = applier.apply(action, "team_bot_a")
    assert not result.ok


# ─────────────────────────────────────────────────────────────────────────────
# DeprecateApp applier
# ─────────────────────────────────────────────────────────────────────────────


def test_deprecate_app_sets_status_deprecated(tmp_path, monkeypatch):
    from arbiter.appliers import deprecate_app

    manifest_path = tmp_path / "fitness.json"
    manifest_path.write_text(
        json.dumps({"id": "fitness", "status": "active"})
    )

    def override(bot_id, app_id):  # noqa: ARG001
        return manifest_path

    monkeypatch.setattr(deprecate_app, "_manifest_path", override)

    applier = get_applier("DeprecateApp")
    action = DeprecateApp(
        app_id="fitness", reason="unused", days_inactive=45
    )
    snap = applier.capture_snapshot(action, "team_bot_a")
    result = applier.apply(action, "team_bot_a")
    assert result.ok

    data = json.loads(manifest_path.read_text())
    assert data["status"] == "deprecated"
    assert data["days_inactive"] == 45

    # Revert
    applier.revert(snap, "team_bot_a")
    data = json.loads(manifest_path.read_text())
    assert data["status"] == "active"


def test_deprecate_app_missing_manifest_fails_cleanly(tmp_path, monkeypatch):
    from arbiter.appliers import deprecate_app

    def override(bot_id, app_id):  # noqa: ARG001
        return tmp_path / "nonexistent.json"

    monkeypatch.setattr(deprecate_app, "_manifest_path", override)

    applier = get_applier("DeprecateApp")
    action = DeprecateApp(app_id="ghost", reason="unused")
    applier.capture_snapshot(action, "team_bot_a")
    result = applier.apply(action, "team_bot_a")
    assert not result.ok


# ─────────────────────────────────────────────────────────────────────────────
# ThrottleGenerator + PauseGenerator appliers
# ─────────────────────────────────────────────────────────────────────────────


def _setup_generator_record(tmp_path, generator_id="gen_a"):
    """Helper: write a GeneratorRecord file into tmp_path/generators/."""
    record = GeneratorRecord(
        id=generator_id,
        charter_fingerprint="x",
        config={"cost_budget_per_window_usd": 1.0},
        state={},
        track_record=TrackRecord(),
        status="active",
        budget_policy="competitive",
    )
    gen_dir = tmp_path / "generators"
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / f"{generator_id}.json").write_text(json.dumps(record.to_dict()))
    return record


def test_throttle_reduce_budget_updates_record(tmp_path):
    _setup_generator_record(tmp_path, "gen_a")
    tg_set_shared_dir(tmp_path)
    try:
        applier = get_applier("ThrottleGenerator")
        action = ThrottleGenerator(
            generator_id="gen_a",
            throttle_type="reduce_budget",
            new_value="0.25",
            reason="test throttle",
        )
        snap = applier.capture_snapshot(action, "team_bot_a")
        result = applier.apply(action, "team_bot_a")
        assert result.ok

        path = tmp_path / "generators" / "gen_a.json"
        data = json.loads(path.read_text())
        assert data["config"]["cost_budget_per_window_usd"] == 0.25

        # Revert
        applier.revert(snap, "team_bot_a")
        data = json.loads(path.read_text())
        assert data["config"]["cost_budget_per_window_usd"] == 1.0
    finally:
        tg_set_shared_dir(Path("/Users/Shared/evolve"))


def test_throttle_reduce_cadence_sets_state_marker(tmp_path):
    _setup_generator_record(tmp_path, "gen_a")
    tg_set_shared_dir(tmp_path)
    try:
        applier = get_applier("ThrottleGenerator")
        action = ThrottleGenerator(
            generator_id="gen_a",
            throttle_type="reduce_cadence",
            new_value="weekly",
            reason="test cadence throttle",
        )
        applier.capture_snapshot(action, "team_bot_a")
        result = applier.apply(action, "team_bot_a")
        assert result.ok
        path = tmp_path / "generators" / "gen_a.json"
        data = json.loads(path.read_text())
        assert data["state"]["throttled_cadence"] == "weekly"
    finally:
        tg_set_shared_dir(Path("/Users/Shared/evolve"))


def test_pause_generator_sets_status_paused(tmp_path):
    _setup_generator_record(tmp_path, "gen_a")
    tg_set_shared_dir(tmp_path)
    try:
        applier = get_applier("PauseGenerator")
        action = PauseGenerator(
            generator_id="gen_a", reason="quarantine"
        )
        snap = applier.capture_snapshot(action, "team_bot_a")
        result = applier.apply(action, "team_bot_a")
        assert result.ok

        path = tmp_path / "generators" / "gen_a.json"
        data = json.loads(path.read_text())
        assert data["status"] == "paused"

        # Revert → back to active
        applier.revert(snap, "team_bot_a")
        data = json.loads(path.read_text())
        assert data["status"] == "active"
    finally:
        tg_set_shared_dir(Path("/Users/Shared/evolve"))


def test_throttle_on_missing_record_fails(tmp_path):
    tg_set_shared_dir(tmp_path)
    try:
        applier = get_applier("ThrottleGenerator")
        action = ThrottleGenerator(
            generator_id="nonexistent",
            throttle_type="reduce_budget",
            new_value="0.5",
        )
        applier.capture_snapshot(action, "team_bot_a")
        result = applier.apply(action, "team_bot_a")
        assert not result.ok
    finally:
        tg_set_shared_dir(Path("/Users/Shared/evolve"))


