"""Tests for the UpdatePermissionConfig applier.

Phase 3d (docs/spec-openclaw-json-derived-artifact-2026-05-24.md §7):
the applier now writes through ``config_sandbox/overrides/<bot>.json``
and materializes back into openclaw.json. Tests must point
``shared_override`` at a per-test tmp dir so the overrides file lives
inside the test sandbox (the previous version of these tests was leaking
writes to ``/Users/Shared/evolve/sandbox/overrides``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Importing triggers register_applier
from arbiter.appliers import permissions as _perms_app  # noqa: F401
from arbiter.appliers.base import get_applier
from schema.proposal import UpdatePermissionConfig


@pytest.fixture
def bot_home(tmp_path: Path) -> Path:
    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    return home


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    (d / "sandbox" / "overrides").mkdir(parents=True)
    return d


def _write_oc(home: Path, obj: dict) -> None:
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(obj))


def _applier_for_test(home: Path, shared: Path):
    """Return the registered applier with test hooks wired up.

    Both hooks are required post-Phase-3d:
    - ``home_override`` → openclaw.json read/write target.
    - ``shared_override`` → overrides file root + materializer input.

    The applier is a registered singleton; we mutate attributes per test
    rather than constructing a fresh instance.
    """
    a = get_applier("UpdatePermissionConfig")
    a.home_override = home  # type: ignore[attr-defined]
    a.shared_override = shared  # type: ignore[attr-defined]
    return a


def _read_overrides(shared: Path, bot_id: str) -> dict:
    p = shared / "sandbox" / "overrides" / f"{bot_id}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


# ── Happy path ─────────────────────────────────────────────────────────────

def test_apply_sets_single_field(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={"tools.exec.ask": "always"})

    result = a.apply(action, "bot")

    assert result.ok, result.message
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["ask"] == "always"


def test_apply_sets_multiple_fields(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    # sandbox.enabled is intentionally not exercised here — see
    # permissions/baseline.py: it's not in the applier's allowed field set.
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
        "tools.fs.workspaceOnly": True,
        "tools.web.search.enabled": False,
    })

    result = a.apply(action, "bot")

    assert result.ok, result.message
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["security"] == "allowlist"
    assert saved["tools"]["fs"]["workspaceOnly"] is True
    assert saved["tools"]["web"]["search"]["enabled"] is False


# ── record_intent_on_set reflex (Phase 2 of config-intent system) ─────────


def test_apply_records_intent_per_field(bot_home: Path, shared_dir: Path):
    """Spec docs/spec-config-intent-system-2026-05-21.md §3: a successful
    applier write must record one config-intent per changed field, with
    set_by encoding the proposal id. Closes the loop with auth_drift_filler:
    once an operator-accepted proposal lands, the generator will no longer
    re-propose the inverse on the next sweep."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "deny"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(
        bot_id="bot",
        fields={
            "tools.exec.security": "full",
            "tools.fs.workspaceOnly": False,
        },
    )

    result = a.apply(action, "bot", proposal_id="prop-xyz123")
    assert result.ok, result.message

    sidecar = json.loads(
        (shared_dir / "config_intents" / "bot.json").read_text()
    )
    by_field = {e["field_path"]: e for e in sidecar["intents"]}
    assert set(by_field.keys()) == {
        "tools.exec.security", "tools.fs.workspaceOnly",
    }
    for entry in by_field.values():
        assert entry["set_by"] == "applier:prop-xyz123"
        assert "prop-xyz123" in entry["reason"]


def test_apply_records_intent_without_proposal_id(bot_home: Path,
                                                    shared_dir: Path):
    """Direct apply() calls without proposal_id (test paths, manual
    invocation) still record an intent — set_by falls back to 'applier'."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "deny"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(
        bot_id="bot", fields={"tools.exec.security": "full"},
    )
    result = a.apply(action, "bot")
    assert result.ok, result.message
    sidecar = json.loads(
        (shared_dir / "config_intents" / "bot.json").read_text()
    )
    assert sidecar["intents"][0]["set_by"] == "applier"


# ── Guard rails (spec §5.4) ────────────────────────────────────────────────

def test_guard_rejects_ask_off(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={"tools.exec.ask": "off"})

    result = a.apply(action, "bot")

    assert not result.ok
    assert "ask='off'" in result.message or "approval gate" in result.message
    # File must be unchanged
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["ask"] == "on-miss"
    # And the overrides file must NOT have been written — the guard fires
    # before any write_override call.
    assert _read_overrides(shared_dir, "bot") == {}


def test_guard_rejects_dangerous_combo(bot_home: Path, shared_dir: Path):
    """Post-apply combo of full + ask=off is rejected even when the proposal
    itself only touches an unrelated field — the guard reads current openclaw.json
    state and refuses if the union would leave the bot at the 'open' posture."""
    # Current state already has the dangerous combo. Any further mutation
    # that doesn't repair it (e.g. tightening fs scope while leaving ask=off)
    # should still trip the guard.
    _write_oc(bot_home, {
        "tools": {"exec": {"security": "full", "ask": "off"}},
    })
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={"tools.fs.workspaceOnly": True})

    result = a.apply(action, "bot")

    assert not result.ok
    assert "open" in result.message.lower() or "post-apply" in result.message.lower()
    # No override written when guard trips.
    assert _read_overrides(shared_dir, "bot") == {}


# ── Validation ─────────────────────────────────────────────────────────────

def test_rejects_unknown_field(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={"some.unknown.path": True})

    result = a.apply(action, "bot")

    assert not result.ok
    assert "outside permission-config" in result.message


def test_rejects_empty_fields(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={})

    result = a.apply(action, "bot")

    assert not result.ok
    assert "empty" in result.message


def test_rejects_missing_openclaw(bot_home: Path, shared_dir: Path):
    # File never created
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={"tools.exec.security": "deny"})

    result = a.apply(action, "bot")

    assert not result.ok
    assert "openclaw" in result.message.lower()


# ── Snapshot + revert ──────────────────────────────────────────────────────

def test_capture_snapshot_records_prior_values(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
        "tools.fs.workspaceOnly": True,
    })

    snap = a.capture_snapshot(action, "bot")

    assert snap["action_kind"] == "UpdatePermissionConfig"
    assert snap["bot_id"] == "bot"
    assert snap["prior_values"]["tools.exec.security"] == "full"
    # Field that didn't exist captured as None — revert will unset it
    assert snap["prior_values"]["tools.fs.workspaceOnly"] is None
    # prior_overrides starts empty (no prior overrides recorded)
    assert snap["prior_overrides"] == {}


def test_revert_restores_prior_state(bot_home: Path, shared_dir: Path):
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
        "tools.fs.workspaceOnly": True,
    })

    snap = a.capture_snapshot(action, "bot")
    a.apply(action, "bot")

    revert = a.revert(snap, "bot")

    assert revert.ok, revert.message
    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    assert saved["tools"]["exec"]["security"] == "full"
    # tools.fs.workspaceOnly didn't exist before — revert should have removed it
    assert "fs" not in saved.get("tools", {}) or "workspaceOnly" not in saved["tools"].get("fs", {})


# ── Phase 3d cutover ───────────────────────────────────────────────────────
#
# These tests pin the new behaviour: the applier writes through the
# overrides file with ``set_by="rsi:<proposal_id>"``, schema-validates
# fields (no static whitelist), revert removes the RSI entries (and
# restores prior overrides if any), and the materializer round-trips the
# value into openclaw.json. See
# docs/spec-openclaw-json-derived-artifact-2026-05-24.md §7.

def test_apply_writes_override_with_rsi_set_by(bot_home: Path, shared_dir: Path):
    """apply(proposal_id=...) records ``set_by="rsi:<proposal_id>"`` in the
    overrides file so the audit log links each RSI-written field back to
    its originating proposal."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
    })

    result = a.apply(action, "bot", proposal_id="prop_abc123")
    assert result.ok, result.message

    overrides = _read_overrides(shared_dir, "bot")
    entries = overrides["overrides"]
    assert "openclaw.tools.exec.security" in entries
    assert entries["openclaw.tools.exec.security"]["value"] == "allowlist"
    assert entries["openclaw.tools.exec.security"]["set_by"] == "rsi:prop_abc123"


def test_apply_without_proposal_id_falls_back_to_bare_rsi(bot_home: Path, shared_dir: Path):
    """When apply() is called without a proposal_id (legacy callers), set_by
    falls back to the bare ``"rsi"`` tag rather than crashing or recording
    a meaningless ``"rsi:None"`` literal."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
    })

    result = a.apply(action, "bot")
    assert result.ok, result.message

    overrides = _read_overrides(shared_dir, "bot")
    entries = overrides["overrides"]
    assert entries["openclaw.tools.exec.security"]["set_by"] == "rsi"


def test_apply_validates_all_11_permission_posture_fields(bot_home: Path, shared_dir: Path):
    """Every field in the prior _ALLOWED_FIELDS whitelist must be schema-known
    (per_bot=True OPENCLAW entry); otherwise this test surfaces the gap."""
    _write_oc(bot_home, {
        "tools": {"exec": {"security": "deny", "ask": "on-miss"}},
    })
    # All 11 prior-whitelist paths, one apply each.
    expected_fields = [
        "tools.exec.security",
        "tools.exec.ask",
        "tools.exec.host",
        "tools.fs.workspaceOnly",
        "tools.web.search.enabled",
        "tools.web.fetch.enabled",
        "commands.native",
        "commands.nativeSkills",
        "commands.elevated",
        "commands.useAccessGroups",
        "commands.ownerAllowFrom",
    ]
    # Use safe values that pass the guards (no ask=off, no full+ask=off).
    safe_values: dict = {
        "tools.exec.security": "deny",
        "tools.exec.ask": "always",
        "tools.exec.host": "none",
        "tools.fs.workspaceOnly": True,
        "tools.web.search.enabled": False,
        "tools.web.fetch.enabled": False,
        "commands.native": "off",
        "commands.nativeSkills": "off",
        "commands.elevated": "deny",
        "commands.useAccessGroups": True,
        "commands.ownerAllowFrom": ["another_bot"],
    }
    for f in expected_fields:
        a = _applier_for_test(bot_home, shared_dir)
        action = UpdatePermissionConfig(bot_id="bot", fields={f: safe_values[f]})
        result = a.apply(action, "bot", proposal_id="prop_test")
        assert result.ok, f"Field {f!r} rejected by schema: {result.message}"


def test_apply_rejects_field_outside_permission_posture(bot_home: Path, shared_dir: Path):
    """Schema may declare other OPENCLAW entries (e.g.
    ``openclaw.agents.defaults.model.primary``); the applier still
    restricts itself to ``tools.*`` and ``commands.*``."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    # agents.defaults.model.primary is a sandbox-schema OPENCLAW entry
    # (added 2026-05-25), but it's not in the permission-posture set —
    # this applier should refuse it.
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "agents.defaults.model.primary": "anthropic/claude-haiku-4-5",
    })
    result = a.apply(action, "bot", proposal_id="prop_xyz")
    assert not result.ok
    assert "outside permission-config" in result.message


def test_revert_deletes_override_when_no_prior_existed(bot_home: Path, shared_dir: Path):
    """Capture → apply → revert. No prior override → revert removes the
    RSI-written entry from the overrides file."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
    })

    snap = a.capture_snapshot(action, "bot")
    a.apply(action, "bot", proposal_id="prop_rev")

    # After apply, override exists
    overrides = _read_overrides(shared_dir, "bot")
    assert "openclaw.tools.exec.security" in overrides["overrides"]

    a.revert(snap, "bot")

    # After revert, override is gone
    overrides = _read_overrides(shared_dir, "bot")
    assert "openclaw.tools.exec.security" not in overrides.get("overrides", {})


def test_revert_restores_prior_operator_override(bot_home: Path, shared_dir: Path):
    """Apply over an existing operator override → revert restores it
    instead of deleting (the operator's pin should outlive an RSI churn)."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "allowlist", "ask": "on-miss"}}})
    # Seed an operator override for the same field.
    from evolve_admin.config_sandbox import write_override
    write_override(
        shared_dir, "bot",
        "openclaw.tools.exec.security",
        "allowlist",
        set_by="operator",
        note="pinned by operator for cron jobs",
    )
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "deny",
    })

    snap = a.capture_snapshot(action, "bot")
    assert "openclaw.tools.exec.security" in snap["prior_overrides"]
    assert snap["prior_overrides"]["openclaw.tools.exec.security"]["set_by"] == "operator"

    a.apply(action, "bot", proposal_id="prop_overwrite")

    # After apply, override exists with rsi set_by
    overrides = _read_overrides(shared_dir, "bot")
    assert overrides["overrides"]["openclaw.tools.exec.security"]["set_by"] == "rsi:prop_overwrite"

    a.revert(snap, "bot")

    # After revert, operator override is back
    overrides = _read_overrides(shared_dir, "bot")
    restored = overrides["overrides"]["openclaw.tools.exec.security"]
    assert restored["set_by"] == "operator"
    assert restored["value"] == "allowlist"
    # And note is preserved
    assert restored["note"] == "pinned by operator for cron jobs"


def test_apply_rolls_back_partial_writes_on_mid_loop_failure(
    bot_home: Path, shared_dir: Path, monkeypatch
):
    """If write_override raises after writing some but not all fields, the
    partial writes are cleaned up — otherwise they'd leak into openclaw.json
    on the next deploy via the policy-slice materializer.

    Simulated by monkey-patching write_override to raise after the first
    successful call.
    """
    _write_oc(bot_home, {"tools": {"exec": {"security": "deny", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)

    from evolve_admin import config_sandbox as _cs
    real_write = _cs.write_override
    call_count = {"n": 0}

    def _flaky_write(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_write(*args, **kwargs)
        raise RuntimeError("simulated FS error on second field")

    monkeypatch.setattr(_cs, "write_override", _flaky_write)
    # The applier imports write_override lazily inside apply() — patch the
    # module attribute, then make sure apply() picks up the patched version.
    import arbiter.appliers.permissions as _perm
    monkeypatch.setattr("evolve_admin.config_sandbox.write_override", _flaky_write)

    action = UpdatePermissionConfig(bot_id="bot", fields={
        "tools.exec.security": "allowlist",
        "tools.fs.workspaceOnly": True,
    })

    result = a.apply(action, "bot", proposal_id="prop_rollback")

    assert not result.ok
    # Rollback removed the first (successful) override; overrides file is
    # empty for this bot.
    overrides = _read_overrides(shared_dir, "bot")
    written_entries = overrides.get("overrides", {})
    assert "openclaw.tools.exec.security" not in written_entries, \
        f"partial write leaked: {written_entries}"


def test_materializer_writes_override_into_openclaw_json(bot_home: Path, shared_dir: Path):
    """apply() writes the override AND immediately reflects it in
    openclaw.json via the materializer — the gateway doesn't need to wait
    for the next deploy to see the new posture."""
    _write_oc(bot_home, {"tools": {"exec": {"security": "full", "ask": "on-miss"}}})
    a = _applier_for_test(bot_home, shared_dir)
    action = UpdatePermissionConfig(bot_id="bot", fields={
        "commands.native": "off",
        "tools.fs.workspaceOnly": True,
    })

    result = a.apply(action, "bot", proposal_id="prop_mat")
    assert result.ok, result.message

    saved = json.loads((bot_home / ".openclaw" / "openclaw.json").read_text())
    # Both fields appear in openclaw.json at their dotpath locations.
    assert saved["commands"]["native"] == "off"
    assert saved["tools"]["fs"]["workspaceOnly"] is True
    # ask is unchanged from initial state.
    assert saved["tools"]["exec"]["ask"] == "on-miss"
