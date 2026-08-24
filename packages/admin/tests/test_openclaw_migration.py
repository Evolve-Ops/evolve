"""Tests for evolve_admin.openclaw_migration.

Phase 3c of internal/spec-openclaw-json-derived-artifact-2026-05-24.md.

Migration A populates each bot's per-bot overrides file with the
divergences between current openclaw.json and shipped defaults, with
``set_by="migration:openclaw_derived_2026_05_24"`` and
``needs_review=True``.

These tests pin:
- Idempotency (re-running doesn't duplicate).
- Schema-invalid drift is skipped (logged, not written).
- Bots with unreadable openclaw.json produce an error result but don't
  blow up the migration.
- ``dry_run=True`` computes the migration without writing.
- Schema-known fields without a defaults_registry entry are skipped
  (e.g. dashboardEnabled — deploy-computed, not Migration A's business).
- The summary formatter renders all paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from evolve_admin.config_sandbox import (
    BotOverrides,
    OverrideEntry,
    read_bot_overrides,
    write_override,
)
from evolve_admin.openclaw_migration import (
    BotMigrationResult,
    MIGRATION_TAG,
    MigrationResult,
    format_migration_summary,
    migrate_all_bots,
    migrate_bot,
)


_DEFAULTS = {
    "classifierModel": "anthropic/claude-haiku-4-5",
    "tierClassification": "session",
    "tier": "full",
    "summarizerMinTurns": 2,
    "classifierKeywordConfidenceFloor": 0.80,
    "costLedgerEnabled": True,
}


_NETWORK = {
    "networkId": "test-pod",
    "sharedDir": "/Users/Shared/evolve",
    "members": ["team_bot_a", "security_bot"],
    "bots": {
        "team_bot_a":    {"role": "member", "user": "team_bot_a"},
        "security_bot": {"role": "member", "user": "security_bot"},
        "evolve": {"role": "primary", "user": "evolve"},
    },
}


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 25, 18, 0, 0, tzinfo=timezone.utc)


def _patch_openclaw_reads(monkeypatch, content_by_bot: dict[str, dict | None]):
    """Make _read_openclaw_json return the provided content for each bot.

    ``content_by_bot``: bot_id → dict (the parsed openclaw.json) or None
    (simulates unreadable). Bots not in the map raise an error.
    """
    def fake_read(bot_id, network):
        if bot_id not in content_by_bot:
            return None, f"no fixture for {bot_id}"
        c = content_by_bot[bot_id]
        if c is None:
            return None, "simulated unreadable"
        return c, ""
    monkeypatch.setattr(
        "evolve_admin.openclaw_migration._read_openclaw_json", fake_read,
    )


# ─── migrate_bot: happy path ──────────────────────────────────────────────


def test_migrate_bot_promotes_drift(shared_dir, monkeypatch, fixed_now):
    """A bot with classifierKeywordConfidenceFloor=0.95 has that promoted
    as an override; default-matching fields are skipped."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {
            "plugins": {"entries": {"evolve": {"config": {
                "botId": "security_bot",
                "tier": "full",                          # matches default
                "summarizerMinTurns": 2,                 # matches default
                "classifierKeywordConfidenceFloor": 0.95,  # DRIFT
                "costLedgerEnabled": True,               # matches default
            }}}},
        },
    })

    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir, now=fixed_now,
    )
    assert r.promoted == ["classifierKeywordConfidenceFloor"]
    assert r.already_overridden == []
    assert r.schema_invalid == []
    assert r.error == ""

    # And the override actually landed.
    bo = read_bot_overrides(shared_dir, "security_bot")
    entry = bo.get("openclaw.plugins.evolve.classifierKeywordConfidenceFloor")
    assert entry is not None
    assert entry.value == 0.95
    assert entry.set_by == MIGRATION_TAG
    assert entry.needs_review is True


def test_migrate_bot_idempotent(shared_dir, monkeypatch, fixed_now):
    """Running the migration twice doesn't duplicate. Second run sees the
    override and counts it as already_overridden."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {
            "plugins": {"entries": {"evolve": {"config": {
                "classifierKeywordConfidenceFloor": 0.95,
            }}}},
        },
    })

    r1 = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir, now=fixed_now,
    )
    r2 = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir, now=fixed_now,
    )
    assert r1.promoted == ["classifierKeywordConfidenceFloor"]
    assert r2.promoted == []
    assert r2.already_overridden == ["classifierKeywordConfidenceFloor"]


def test_migrate_bot_dry_run_does_not_write(shared_dir, monkeypatch, fixed_now):
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {
            "plugins": {"entries": {"evolve": {"config": {
                "classifierKeywordConfidenceFloor": 0.95,
            }}}},
        },
    })

    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        now=fixed_now, dry_run=True,
    )
    # Reports the divergence
    assert r.promoted == ["classifierKeywordConfidenceFloor"]
    # But the override file is NOT created
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.overrides == {}


def test_migrate_bot_skips_keys_not_in_defaults_registry(shared_dir, monkeypatch):
    """dashboardEnabled is in the schema but NOT in defaults_registry
    (deploy.py computes it from role). Migration A leaves it alone."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {
            "plugins": {"entries": {"evolve": {"config": {
                "dashboardEnabled": True,   # value present but not in defaults_registry
                "tier": "monitor",           # IS in defaults_registry → drift
            }}}},
        },
    })

    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert "dashboardEnabled" not in r.promoted
    assert r.promoted == ["tier"]


# ─── migrate_bot: error paths ─────────────────────────────────────────────


def test_migrate_bot_unreadable_openclaw_records_error(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {"security_bot": None})
    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert r.error == "simulated unreadable"
    assert r.promoted == []


def test_migrate_bot_no_evolve_config_returns_clean(shared_dir, monkeypatch):
    """A bot whose openclaw.json has no plugins.entries.evolve.config
    block returns a clean result (nothing to migrate)."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {"plugins": {"entries": {}}},
    })
    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert r.error == ""
    assert r.promoted == []


def test_migrate_bot_skips_schema_invalid_drift(shared_dir, monkeypatch):
    """A drift value that fails Phase 2's type check (e.g. tier=42 — int
    where enum/string expected) is recorded as schema_invalid, NOT
    promoted. The materializer will revert on next deploy."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {
            "plugins": {"entries": {"evolve": {"config": {
                "tier": 42,   # int, not enum
            }}}},
        },
    })

    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert r.promoted == []
    assert len(r.schema_invalid) == 1
    field_name, current_val, reason = r.schema_invalid[0]
    assert field_name == "tier"
    assert current_val == 42                        # original value preserved
    assert "wrong type" in reason


# ─── migrate_all_bots ─────────────────────────────────────────────────────


def test_migrate_all_bots_iterates_network(shared_dir, monkeypatch, fixed_now):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    {"plugins": {"entries": {"evolve": {"config": {"tier": "monitor"}}}}},
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {"tier": "manage"}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {"tier": "full"}}}}},  # matches default
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir, now=fixed_now,
    )
    assert set(result.per_bot.keys()) == {"team_bot_a", "security_bot", "evolve"}
    assert result.per_bot["team_bot_a"].promoted == ["tier"]
    assert result.per_bot["security_bot"].promoted == ["tier"]
    assert result.per_bot["evolve"].promoted == []
    assert result.total_promoted() == 2


def test_migrate_all_bots_one_failure_does_not_block_others(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    None,   # unreadable
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {"tier": "monitor"}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert result.per_bot["team_bot_a"].error != ""
    assert result.per_bot["security_bot"].promoted == ["tier"]
    assert result.bots_with_errors() == ["team_bot_a"]


def test_migrate_all_bots_empty_network(shared_dir):
    result = migrate_all_bots(
        network={"networkId": "x", "bots": {}},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert result.per_bot == {}
    assert result.total_promoted() == 0


def test_migrate_all_bots_dry_run_writes_nothing(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    {"plugins": {"entries": {"evolve": {"config": {"tier": "monitor"}}}}},
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        dry_run=True,
    )
    assert result.per_bot["team_bot_a"].promoted == ["tier"]
    # No override file should have been created
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert bo.overrides == {}


# ─── Summary formatter ────────────────────────────────────────────────────


def test_format_migration_summary_promoted_and_already(shared_dir, monkeypatch, fixed_now):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    {"plugins": {"entries": {"evolve": {"config": {"tier": "monitor"}}}}},
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    # Pre-record an override on team_bot_a for tier — second run should report
    # it as already_overridden.
    write_override(
        shared_dir, "team_bot_a", "openclaw.plugins.evolve.tier",
        "monitor", set_by="operator", now=fixed_now,
    )

    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir, now=fixed_now,
    )
    out = format_migration_summary(result)
    assert "== team_bot_a ==" in out
    assert "Already overridden" in out
    assert "tier" in out
    assert "no drift detected" in out  # security_bot + evolve


def test_format_migration_summary_schema_invalid(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    {"plugins": {"entries": {"evolve": {"config": {"tier": 42}}}}},
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    out = format_migration_summary(result)
    assert "Schema-invalid drift" in out
    assert "REVERTED" in out                                # louder framing
    assert "Action required" in out                          # top-level WARN section
    assert "tier=42" in out                                  # value preserved + shown


def test_format_migration_summary_error(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    None,
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    out = format_migration_summary(result)
    assert "ERROR:" in out


def test_format_migration_summary_all_clean(shared_dir, monkeypatch):
    _patch_openclaw_reads(monkeypatch, {
        "team_bot_a":    {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "security_bot": {"plugins": {"entries": {"evolve": {"config": {}}}}},
        "evolve": {"plugins": {"entries": {"evolve": {"config": {}}}}},
    })
    result = migrate_all_bots(
        network=_NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    out = format_migration_summary(result)
    assert "Nothing to review" in out


# ─── _read_openclaw_json ─────────────────────────────────────────────────


def test_read_openclaw_json_direct_read_path(tmp_path, monkeypatch):
    """Direct Path.read_text succeeds when ACL is in place. Verify the
    helper parses the JSON correctly."""
    from evolve_admin.openclaw_migration import _read_openclaw_json

    target = tmp_path / "openclaw.json"
    target.write_text(json.dumps({"plugins": {"entries": {"evolve": {"config": {"tier": "full"}}}}}))

    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.get_bot_user",
        lambda bot_id, network: "x",
    )
    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.Path",
        lambda *args: target,
    )

    parsed, err = _read_openclaw_json("security_bot", _NETWORK)
    assert err == ""
    assert parsed["plugins"]["entries"]["evolve"]["config"]["tier"] == "full"


def test_read_openclaw_json_sudo_fallback_fires_on_permission_error(tmp_path, monkeypatch):
    """When the direct read raises PermissionError (ACL gap on a
    not-yet-set-up bot), the helper falls back to ``sudo /bin/cat``.
    Verifies the fallback wires through to subprocess.run with the
    expected argv."""
    from evolve_admin.openclaw_migration import _read_openclaw_json
    import subprocess

    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.get_bot_user",
        lambda bot_id, network: "security_bot",
    )

    real_path = Path("/Users/security_bot/.openclaw/openclaw.json")
    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.Path",
        lambda *args: real_path if "/Users/security_bot" in str(args[0]) else Path(*args),
    )

    # Direct read raises PermissionError; sudo /bin/cat returns the JSON.
    def fake_read_text(*a, **kw):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    captured: dict = {}
    def fake_run(cmd, **kw):
        from unittest.mock import MagicMock
        captured["cmd"] = cmd
        m = MagicMock()
        if cmd[0:2] == ["sudo", "/bin/test"]:
            m.returncode = 0   # file "exists"
            return m
        if cmd[0:2] == ["sudo", "/bin/cat"]:
            m.returncode = 0
            m.stdout = json.dumps({"plugins": {"entries": {"evolve": {"config": {"tier": "monitor"}}}}})
            m.stderr = ""
            return m
        m.returncode = 1
        return m
    monkeypatch.setattr(subprocess, "run", fake_run)

    parsed, err = _read_openclaw_json("security_bot", _NETWORK)
    assert err == ""
    assert parsed["plugins"]["entries"]["evolve"]["config"]["tier"] == "monitor"
    assert captured["cmd"][:2] == ["sudo", "/bin/cat"]


def test_read_openclaw_json_surfaces_sudo_failure_with_stderr(tmp_path, monkeypatch):
    """If sudo /bin/cat itself fails (e.g. sudoers grant missing), the
    error message surfaces the stderr tail so the operator can diagnose
    — instead of opaquely saying 'ACL + sudo both failed'."""
    from evolve_admin.openclaw_migration import _read_openclaw_json
    import subprocess

    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.get_bot_user",
        lambda bot_id, network: "security_bot",
    )
    real_path = Path("/Users/security_bot/.openclaw/openclaw.json")
    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.Path",
        lambda *args: real_path if "/Users/security_bot" in str(args[0]) else Path(*args),
    )
    def fake_read_text(*a, **kw):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    def fake_run(cmd, **kw):
        from unittest.mock import MagicMock
        m = MagicMock()
        if cmd[0:2] == ["sudo", "/bin/test"]:
            m.returncode = 0
            return m
        if cmd[0:2] == ["sudo", "/bin/cat"]:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "sudo: a password is required"
            return m
        return m
    monkeypatch.setattr(subprocess, "run", fake_run)

    parsed, err = _read_openclaw_json("security_bot", _NETWORK)
    assert parsed is None
    assert "sudo: a password is required" in err
    assert "/etc/sudoers.d/evolve" in err  # actionable hint


def test_read_openclaw_json_distinguishes_missing_from_unreadable(tmp_path, monkeypatch):
    """A file that doesn't exist (bot half-deployed) produces a
    different error message than one that exists but can't be read
    (ACL gap)."""
    from evolve_admin.openclaw_migration import _read_openclaw_json
    import subprocess

    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.get_bot_user",
        lambda bot_id, network: "security_bot",
    )
    real_path = Path("/Users/security_bot/.openclaw/openclaw.json")
    monkeypatch.setattr(
        "evolve_admin.openclaw_migration.Path",
        lambda *args: real_path if "/Users/security_bot" in str(args[0]) else Path(*args),
    )

    def fake_read_text(*a, **kw):
        raise FileNotFoundError(2, "No such file")
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    def fake_run(cmd, **kw):
        from unittest.mock import MagicMock
        m = MagicMock()
        if cmd[0:2] == ["sudo", "/bin/test"]:
            m.returncode = 1   # file does NOT exist
            return m
        if cmd[0:2] == ["sudo", "/bin/cat"]:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "No such file"
            return m
        return m
    monkeypatch.setattr(subprocess, "run", fake_run)

    parsed, err = _read_openclaw_json("security_bot", _NETWORK)
    assert parsed is None
    assert "does not exist" in err
    assert "partially deployed" in err


def test_migrate_bot_malformed_openclaw_shape_clean_error(shared_dir, monkeypatch):
    """A bot whose openclaw.json has 'plugins' set to a list (not a dict)
    must surface a clean error, not an AttributeError stack trace."""
    _patch_openclaw_reads(monkeypatch, {
        "security_bot": {"plugins": ["not", "a", "dict"]},
    })
    r = migrate_bot(
        "security_bot", _NETWORK,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
    )
    assert r.error != ""
    assert "malformed" in r.error or "is not an object" in r.error


def test_migration_defaults_registry_matches_deploy(shared_dir):
    """Migration A inlines a copy of ``deploy._PLUGIN_CONFIG_DEFAULTS``.
    Pin the equality so a future drift gets caught."""
    from evolve_admin.openclaw_migration import _DEFAULTS_REGISTRY
    from evolve_admin.deploy import _PLUGIN_CONFIG_DEFAULTS
    assert _DEFAULTS_REGISTRY == _PLUGIN_CONFIG_DEFAULTS, (
        "openclaw_migration._DEFAULTS_REGISTRY drifted from "
        "deploy._PLUGIN_CONFIG_DEFAULTS — update one of them."
    )
