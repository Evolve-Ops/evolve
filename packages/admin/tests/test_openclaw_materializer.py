"""Tests for evolve_admin.openclaw_materializer.

Phase 3b of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

The materializer's job is to regenerate the
``plugins.entries.evolve.config`` block in each bot's openclaw.json from
declared inputs (identity + defaults + overrides), with three properties
the existing gap-fill ``ensure_plugin_config`` can't deliver:

  1. **Stale-key prune.** Any field no longer in the live plugin's strict
     configSchema is dropped — the PR #1525 cleanup.
  2. **Auto-promote drift.** Any current value that diverges from the
     would-be value and isn't an override is auto-recorded as a
     needs_review=True override; redeploys never silently destroy edits.
  3. **Override consumption.** Per-bot overrides from Phase 2 supersede
     defaults.

These tests pin all three plus the boring edge cases.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evolve_admin.openclaw_materializer import (
    PolicySliceResult,
    load_plugin_config_schema,
    materialize_evolve_plugin_config,
    materialize_openclaw_policy_slice,
)
from evolve_admin.config_sandbox import (
    read_bot_overrides,
    write_override,
)


# A minimal-but-realistic plugin manifest. Mirrors the real
# packages/plugin/openclaw.plugin.json shape with strict
# additionalProperties: false.
_FAKE_MANIFEST = {
    "id": "evolve",
    "configSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "botId": {"type": "string"},
            "role": {"type": "string"},
            "networkId": {"type": "string"},
            "sharedDir": {"type": "string"},
            "repoRoot": {"type": "string"},
            "tier": {"type": "string"},
            "summarizerMinTurns": {"type": "number"},
            "classifierKeywordConfidenceFloor": {"type": "number"},
            "classifierModel": {"type": "string"},
            "costLedgerEnabled": {"type": "boolean"},
            "tierClassification": {"type": "string"},
            "dashboardEnabled": {"type": "boolean"},
        },
    },
}


_DEFAULTS = {
    "classifierModel": "anthropic/claude-haiku-4-5",
    "tierClassification": "session",
    "tier": "full",
    "summarizerMinTurns": 2,
    "classifierKeywordConfidenceFloor": 0.80,
    "costLedgerEnabled": True,
}


# Explicit repoRoot so identity resolution is deterministic regardless of
# the host platform the suite runs on (CI is Linux; dev is macOS). The
# network override takes precedence over the platform-profile default —
# the profile-derivation path is exercised separately in the
# ``materialize: repoRoot`` section below via set_profile().
_NETWORK = {
    "networkId": "test-pod",
    "sharedDir": "/Users/Shared/evolve",
    "repoRoot": "/Users/Shared/evolve-repo",
    "members": ["team_bot_a", "security_bot"],
    "bots": {
        "team_bot_a":    {"role": "member"},
        "security_bot": {"role": "member"},
        "evolve": {"role": "primary"},
    },
}


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    p = tmp_path / "openclaw.plugin.json"
    p.write_text(json.dumps(_FAKE_MANIFEST))
    return p


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 25, 18, 0, 0, tzinfo=timezone.utc)


# ─── load_plugin_config_schema ────────────────────────────────────────────


def test_load_schema_strict_returns_allowed_set(manifest):
    allowed, strict = load_plugin_config_schema(manifest)
    assert strict is True
    # Identity fields are folded into the allowed set so the materializer
    # doesn't accidentally prune them.
    assert {"botId", "role", "networkId", "sharedDir", "repoRoot"}.issubset(allowed)
    assert "tier" in allowed
    assert "classifierModel" in allowed


def test_load_schema_non_strict_recorded(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "id": "evolve",
        "configSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {"tier": {"type": "string"}},
        },
    }))
    allowed, strict = load_plugin_config_schema(p)
    assert strict is False
    assert "tier" in allowed


def test_load_schema_missing_manifest_degrades_safely(tmp_path):
    """Missing file → empty allowed set + non-strict. Materializer skips
    the stale-key prune; doesn't crash."""
    allowed, strict = load_plugin_config_schema(tmp_path / "nope.json")
    assert allowed == set()
    assert strict is False


def test_load_schema_malformed_json_degrades_safely(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{ not json")
    allowed, strict = load_plugin_config_schema(p)
    assert allowed == set()
    assert strict is False


# ─── materialize: identity ────────────────────────────────────────────────


def test_identity_always_populated_from_network(shared_dir, manifest):
    """Identity fields are pulled from network every time — a rename or
    role change takes effect on next deploy."""
    current = {"botId": "OLD", "role": "wrong", "networkId": "OLD", "sharedDir": "/wrong"}
    r = materialize_evolve_plugin_config(
        "security_bot", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["botId"] == "security_bot"
    assert r.new_block["role"] == "member"          # from network
    assert r.new_block["networkId"] == "test-pod"
    assert r.new_block["sharedDir"] == "/Users/Shared/evolve"
    assert r.new_block["repoRoot"] == "/Users/Shared/evolve-repo"  # network override


def test_role_change_propagates(shared_dir, manifest):
    """If the bot's role changes in network.json, the materialized
    block reflects it. (Primary bots get role=primary; this also makes
    dashboardEnabled-style role-conditional fields work in deploy.)"""
    current = {"botId": "evolve", "role": "member"}
    r = materialize_evolve_plugin_config(
        "evolve", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["role"] == "primary"


# ─── materialize: repoRoot (the EVO-LINUX-PLUGIN-PATH fix) ─────────────────
#
# repoRoot is the deploy checkout the plugin uses to locate
# packages/analyzer scripts. The pre-fix plugin derived it from
# dirname(sharedDir)+"evolve-repo", which is a sibling-layout assumption
# that only holds on macOS. On Linux it produced /var/lib/evolve-repo
# when the real checkout is /var/lib/evolve/repo (a CHILD of sharedDir),
# so session_surface.py couldn't be found and the per-turn capability
# block never rendered (live evolve-vsp-pod). The materializer now
# injects repoRoot explicitly from the active platform profile so the TS
# plugin no longer has to guess.


@pytest.fixture
def _no_repo_network():
    """A network without a repoRoot override, so identity resolution falls
    through to the platform profile's deploy_checkout_default."""
    net = dict(_NETWORK)
    net.pop("repoRoot", None)
    return net


def test_repo_root_macos_profile(shared_dir, manifest, _no_repo_network):
    """macOS profile → repoRoot is the real deploy checkout
    /Users/Shared/evolve-repo. This is BYTE-IDENTICAL to the path the old
    dirname(sharedDir)+evolve-repo derivation produced — the macOS path
    must not move."""
    from platform_profile import MACOS, set_profile
    set_profile(MACOS)
    try:
        r = materialize_evolve_plugin_config(
            "team_bot_a", _no_repo_network, {},
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest,
        )
    finally:
        set_profile(None)
    assert r.new_block["repoRoot"] == "/Users/Shared/evolve-repo"
    # The analyzer dir the plugin will build from this.
    assert (
        r.new_block["repoRoot"] + "/packages/analyzer"
        == "/Users/Shared/evolve-repo/packages/analyzer"
    )


def test_repo_root_linux_profile(shared_dir, manifest, _no_repo_network):
    """Linux profile → repoRoot is /var/lib/evolve/repo (a CHILD of
    sharedDir=/var/lib/evolve), NOT the broken sibling /var/lib/evolve-repo
    the old derivation produced."""
    from platform_profile import LINUX, set_profile
    set_profile(LINUX)
    try:
        r = materialize_evolve_plugin_config(
            "team_bot_a", _no_repo_network, {},
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest,
        )
    finally:
        set_profile(None)
    assert r.new_block["repoRoot"] == "/var/lib/evolve/repo"
    # The analyzer dir the plugin will build — the regression path.
    assert (
        r.new_block["repoRoot"] + "/packages/analyzer"
        == "/var/lib/evolve/repo/packages/analyzer"
    )
    assert r.new_block["repoRoot"] != "/var/lib/evolve-repo"


def test_repo_root_network_override_wins(shared_dir, manifest):
    """An explicit network.json repoRoot beats the platform default —
    operator escape hatch, symmetric with sharedDir. Pinned under the
    LINUX profile to prove the override (not the profile default) is used."""
    from platform_profile import LINUX, set_profile
    net = dict(_NETWORK)
    net["repoRoot"] = "/opt/custom/evolve-checkout"
    set_profile(LINUX)
    try:
        r = materialize_evolve_plugin_config(
            "team_bot_a", net, {},
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest,
        )
    finally:
        set_profile(None)
    assert r.new_block["repoRoot"] == "/opt/custom/evolve-checkout"


def test_repo_root_not_pruned_under_strict_schema(shared_dir, manifest):
    """repoRoot survives the strict-schema stale-key prune even though it
    is an identity field (folded into the allowed set via _IDENTITY_FIELDS),
    mirroring how botId/sharedDir survive."""
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, {},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert "repoRoot" in r.new_block
    assert "repoRoot" not in r.pruned_stale


# ─── materialize: defaults ────────────────────────────────────────────────


def test_defaults_populated_when_block_empty(shared_dir, manifest):
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, {},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    for k, v in _DEFAULTS.items():
        assert r.new_block[k] == v
    assert r.auto_promoted == []
    assert r.pruned_stale == []
    assert r.kept_overrides == []


def test_default_matching_current_value_is_no_op(shared_dir, manifest):
    """If a bot's openclaw.json already has the default value, no drift
    is detected and the materialized block has the same value."""
    current = {"botId": "team_bot_a", "tier": "full"}   # tier=full IS the default
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.auto_promoted == []
    assert r.new_block["tier"] == "full"


# ─── materialize: stale-key prune (the #1525 case) ────────────────────────


def test_stale_key_dropped_under_strict_schema(shared_dir, manifest):
    """The motivating case: schema removed ``reportingEnabled`` (PR
    #1525); current block still carries it; materializer drops it."""
    current = {
        "botId": "team_bot_a",
        "tier": "full",
        "reportingEnabled": True,    # ← no longer in manifest schema
    }
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert "reportingEnabled" not in r.new_block
    assert r.pruned_stale == ["reportingEnabled"]


def test_stale_key_kept_under_non_strict_schema(tmp_path, shared_dir):
    """If the manifest declares additionalProperties: true, unknown keys
    are tolerated — we don't prune (OC won't reject them)."""
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "id": "evolve",
        "configSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {"tier": {"type": "string"}},
        },
    }))
    current = {"tier": "full", "unknownKey": "kept"}
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=p,
    )
    assert r.new_block.get("unknownKey") == "kept"
    assert r.pruned_stale == []


def test_stale_key_kept_when_manifest_unreadable(tmp_path, shared_dir):
    """Missing manifest → safe path. Don't prune (caller's openclaw
    validate is still the ultimate gate)."""
    current = {"tier": "full", "unknownKey": "still here"}
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=tmp_path / "nope.json",
    )
    assert r.new_block.get("unknownKey") == "still here"
    assert r.pruned_stale == []


# ─── materialize: drift auto-promotion ────────────────────────────────────


def test_drift_auto_promoted_to_override(shared_dir, manifest, fixed_now):
    """A current value diverging from the default with no recorded
    override gets auto-promoted with needs_review=True. The
    materialized block reflects the operator's edit (override now in
    inputs)."""
    current = {"botId": "team_bot_a", "tier": "monitor"}   # default is "full"
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest, now=fixed_now,
    )
    assert r.auto_promoted == ["tier"]
    assert r.new_block["tier"] == "monitor"
    # And the override file actually carries the entry.
    bo = read_bot_overrides(shared_dir, "team_bot_a")
    entry = bo.get("openclaw.plugins.evolve.tier")
    assert entry is not None
    assert entry.value == "monitor"
    assert entry.needs_review is True
    assert entry.set_by.startswith("auto:drift_")


def test_drift_with_existing_override_not_re_promoted(shared_dir, manifest, fixed_now):
    """If an override is already recorded for the field, the current
    value matching it is NOT re-recorded as drift."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.plugins.evolve.tier",
        "monitor", set_by="operator", now=fixed_now,
    )
    current = {"botId": "team_bot_a", "tier": "monitor"}
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.auto_promoted == []
    assert r.kept_overrides == ["tier"]
    assert r.new_block["tier"] == "monitor"


def test_drift_schema_invalid_NOT_auto_promoted(shared_dir, manifest, caplog):
    """A drift value that fails the sandbox schema's type check is NOT
    auto-promoted (would just recreate the bug class inside the
    overrides file). The materializer logs a warning and the next
    deploy reverts to the default."""
    import logging
    # tier is an enum (string); 42 (int) would fail the type check.
    current = {"botId": "team_bot_a", "tier": 42}
    with caplog.at_level(logging.WARNING, logger="evolve_admin.openclaw_materializer"):
        r = materialize_evolve_plugin_config(
            "team_bot_a", _NETWORK, current,
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest,
        )
    assert r.auto_promoted == []
    assert r.new_block["tier"] == "full"  # reverted to default
    assert any("schema-invalid" in rec.message for rec in caplog.records)


# ─── materialize: override consumption ────────────────────────────────────


def test_override_value_supersedes_default(shared_dir, manifest, fixed_now):
    write_override(
        shared_dir, "team_bot_a", "openclaw.plugins.evolve.tier",
        "monitor", set_by="operator", now=fixed_now,
    )
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, {},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["tier"] == "monitor"
    assert "tier" in r.kept_overrides


def test_multiple_overrides_consumed(shared_dir, manifest):
    write_override(shared_dir, "team_bot_a", "openclaw.plugins.evolve.tier", "monitor", set_by="operator")
    write_override(shared_dir, "team_bot_a", "openclaw.plugins.evolve.summarizerMinTurns", 5, set_by="operator")
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, {},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["tier"] == "monitor"
    assert r.new_block["summarizerMinTurns"] == 5
    assert set(r.kept_overrides) == {"tier", "summarizerMinTurns"}


# ─── materialize: unchanged optimization ──────────────────────────────────


def test_unchanged_when_block_matches_inputs(shared_dir, manifest):
    """A bot whose openclaw.json already matches the would-be value
    reports unchanged=True (caller skips writing)."""
    # Build a block that already matches identity + defaults exactly.
    current = {
        "botId": "team_bot_a",
        "role": "member",
        "networkId": "test-pod",
        "sharedDir": "/Users/Shared/evolve",
        "repoRoot": "/Users/Shared/evolve-repo",
        **_DEFAULTS,
    }
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.unchanged is True
    assert r.auto_promoted == []
    assert r.pruned_stale == []


def test_unchanged_false_when_drift_promoted(shared_dir, manifest, fixed_now):
    """Even if the new_block ends up equal to current after the
    auto-promote re-derivation, ``unchanged`` is False — the side effect
    of writing to overrides is a state change the caller should know
    about."""
    current = {"botId": "team_bot_a", "tier": "monitor"}   # drift on tier
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest, now=fixed_now,
    )
    assert r.unchanged is False
    assert r.auto_promoted == ["tier"]


# ─── materialize: keep newer-than-admin manifest fields ───────────────────


def test_manifest_field_without_default_or_override_kept(shared_dir, manifest):
    """A field present in the manifest but absent from our defaults_registry
    and overrides — preserved from the current block. This is the
    "plugin shipped a newer field than the admin knows about" path."""
    current = {"botId": "team_bot_a", "futureField": "leave-me"}
    # Patch the manifest to allow futureField.
    extended = json.loads(manifest.read_text())
    extended["configSchema"]["properties"]["futureField"] = {"type": "string"}
    manifest.write_text(json.dumps(extended))

    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block.get("futureField") == "leave-me"
    assert r.pruned_stale == []


# ─── materialize: docstring-pattern combinations ─────────────────────────


def test_present_with_null_value_for_bool_is_schema_invalid_drift(
    shared_dir, manifest, fixed_now, caplog,
):
    """A bot whose openclaw.json has ``costLedgerEnabled: null`` (explicit
    operator nulling) is detected as drift, but the auto-promote refuses
    null-for-bool (Phase 2 type validation). The materializer logs
    schema-invalid and the materialized block reverts to the default —
    matching spec §11.5 ("schema-invalid edits are NOT auto-promoted ...
    file a Signal and get reverted next deploy").

    The reviewer flagged the OLD code path that used
    ``current_block.get(field_name) is None`` which conflated "absent"
    with "explicitly null" — both fell through silently. The fix
    differentiates: absent → use default; null → drift → schema check
    fires (refuse for typed fields)."""
    import logging
    current = {"botId": "team_bot_a", "costLedgerEnabled": None}  # explicit null
    with caplog.at_level(logging.WARNING, logger="evolve_admin.openclaw_materializer"):
        r = materialize_evolve_plugin_config(
            "team_bot_a", _NETWORK, current,
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest, now=fixed_now,
        )
    # Drift detected, but auto-promote refused as schema-invalid
    assert r.auto_promoted == []
    assert any(
        "schema-invalid" in rec.message and "costLedgerEnabled" in rec.message
        for rec in caplog.records
    )
    # Materialized block falls through to default
    assert r.new_block["costLedgerEnabled"] is True


def test_absent_key_falls_through_to_default(shared_dir, manifest):
    """The complement of the above: a key genuinely absent from current_block
    is NOT drift — Pass 3 injects the default."""
    current = {"botId": "team_bot_a"}   # no costLedgerEnabled at all
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.auto_promoted == []
    assert r.new_block["costLedgerEnabled"] is True   # the default


def test_persistence_failed_set_on_write_oserror(shared_dir, manifest, monkeypatch, fixed_now):
    """If write_override raises a non-validation error, the in-memory value
    is still used but persistence_failed=True so the caller can surface
    the operator action item."""
    import evolve_admin.openclaw_materializer as m

    def boom(*a, **kw):
        raise OSError("permission denied: /shared/sandbox/overrides")
    monkeypatch.setattr(m, "write_override", boom)

    current = {"botId": "team_bot_a", "tier": "monitor"}   # drift
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest, now=fixed_now,
    )
    assert r.persistence_failed is True
    assert r.new_block["tier"] == "monitor"          # operator value preserved
    assert r.auto_promoted == []                     # but not actually persisted


def test_overrides_file_unreadable_breaks_loop_no_cascade(shared_dir, manifest, monkeypatch, fixed_now, caplog):
    """If the overrides file is malformed (OverrideStateError on first
    write), the loop breaks immediately so we don't flood the log with
    duplicate warnings; subsequent drifted keys keep their in-memory
    values but persistence_failed=True so the caller signals once."""
    import logging
    import evolve_admin.openclaw_materializer as m
    from evolve_admin.config_sandbox import OverrideStateError

    call_count = {"n": 0}
    def write_overrides_state_error(*a, **kw):
        call_count["n"] += 1
        raise OverrideStateError("file is malformed")
    monkeypatch.setattr(m, "write_override", write_overrides_state_error)

    current = {
        "botId": "team_bot_a",
        "tier": "monitor",                          # drift #1
        "summarizerMinTurns": 9,                    # drift #2
        "classifierKeywordConfidenceFloor": 0.95,   # drift #3
    }
    with caplog.at_level(logging.WARNING, logger="evolve_admin.openclaw_materializer"):
        r = materialize_evolve_plugin_config(
            "team_bot_a", _NETWORK, current,
            defaults_registry=_DEFAULTS, shared_dir=shared_dir,
            plugin_manifest=manifest, now=fixed_now,
        )

    assert r.persistence_failed is True
    # Operator's edits preserved in the materialized output
    assert r.new_block["tier"] == "monitor"
    assert r.new_block["summarizerMinTurns"] == 9
    assert r.new_block["classifierKeywordConfidenceFloor"] == 0.95
    # write_override called exactly once — after that we short-circuit
    assert call_count["n"] == 1
    # Only one "unreadable" warning, not three
    unreadable_logs = [r for r in caplog.records if "unreadable" in r.message]
    assert len(unreadable_logs) == 1


def test_simultaneous_stale_prune_drift_promote_and_override(shared_dir, manifest, fixed_now):
    """Realistic combo: a bot with a stale key (reportingEnabled),
    an ad-hoc tier edit (auto-promote), and an existing summarizerMinTurns
    override. All three behaviors fire in one pass."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.plugins.evolve.summarizerMinTurns",
        7, set_by="operator", now=fixed_now,
    )
    current = {
        "botId": "team_bot_a",
        "tier": "manage",              # drift; will auto-promote
        "reportingEnabled": True,      # stale; will prune
        "summarizerMinTurns": 7,       # matches override; kept
        "costLedgerEnabled": True,     # matches default; no change
    }
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest, now=fixed_now,
    )
    assert r.auto_promoted == ["tier"]
    assert r.pruned_stale == ["reportingEnabled"]
    assert "tier" in r.kept_overrides or r.new_block["tier"] == "manage"
    assert r.new_block["summarizerMinTurns"] == 7
    assert "reportingEnabled" not in r.new_block
    assert r.new_block["tier"] == "manage"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3d — materialize_openclaw_policy_slice
#
# Tests that the policy-slice materializer (permission-posture + non-plugin
# OPENCLAW fields) applies overrides on top of an in-memory cfg dict.
# Spec: docs/spec-openclaw-json-derived-artifact-2026-05-24.md §7.
# ─────────────────────────────────────────────────────────────────────────────


def test_policy_slice_no_overrides_is_no_op(shared_dir):
    """Bot has no overrides file → cfg unchanged, applied_overrides empty."""
    cfg = {"tools": {"exec": {"security": "deny", "ask": "on-miss"}}}
    before = json.dumps(cfg, sort_keys=True)
    r = materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert json.dumps(cfg, sort_keys=True) == before
    assert r.applied_overrides == []
    assert r.applied_dotpaths == []
    assert r.skipped_unparseable_overrides is False


def test_policy_slice_applies_override_into_cfg(shared_dir, fixed_now):
    """Override exists → policy slice writes the value at the dotpath."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.tools.exec.security",
        "allowlist", set_by="rsi:prop_abc", now=fixed_now,
    )
    cfg = {"tools": {"exec": {"security": "deny", "ask": "on-miss"}}}
    r = materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert cfg["tools"]["exec"]["security"] == "allowlist"
    # Other fields untouched
    assert cfg["tools"]["exec"]["ask"] == "on-miss"
    assert r.applied_dotpaths == ["tools.exec.security"]
    assert r.applied_overrides == ["openclaw.tools.exec.security"]


def test_policy_slice_creates_nested_path_when_absent(shared_dir, fixed_now):
    """Override at a deep dotpath that doesn't exist in cfg yet → the
    materializer creates the intermediate dicts on the way."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.commands.ownerAllowFrom",
        ["admin_bot"], set_by="operator", now=fixed_now,
    )
    cfg = {}  # empty cfg
    materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert cfg == {"commands": {"ownerAllowFrom": ["admin_bot"]}}


def test_policy_slice_override_supersedes_inferred_value(shared_dir, fixed_now):
    """Layering rule from spec §7: per-bot override beats whatever
    inference / gap-fill already wrote into cfg."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.tools.exec.security",
        "full", set_by="operator", note="bot needs full exec for monitoring",
        now=fixed_now,
    )
    # Caller's inference pre-wrote "deny" (e.g. _infer_exec_policy fallback).
    cfg = {"tools": {"exec": {"security": "deny"}}}
    materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert cfg["tools"]["exec"]["security"] == "full"


def test_policy_slice_skips_evolve_plugin_block(shared_dir, fixed_now):
    """The slice materializer must NOT touch the evolve plugin config
    block — that's owned by materialize_evolve_plugin_config. Writing
    both would either double-up or fight."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.plugins.evolve.tier",
        "monitor", set_by="operator", now=fixed_now,
    )
    cfg = {"plugins": {"entries": {"evolve": {"config": {"tier": "full"}}}}}
    r = materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    # Override exists but the policy-slice pass ignores it (plugin-block
    # materializer's responsibility).
    assert cfg["plugins"]["entries"]["evolve"]["config"]["tier"] == "full"
    assert "openclaw.plugins.evolve.tier" not in r.applied_overrides


def test_policy_slice_applies_all_permission_posture_overrides(shared_dir, fixed_now):
    """Cover the full set of 11 permission-posture fields the L2 applier
    can target. Each override should land at its expected dotpath."""
    fields: dict = {
        "openclaw.tools.exec.security": ("tools.exec.security", "allowlist"),
        "openclaw.tools.exec.ask": ("tools.exec.ask", "always"),
        "openclaw.tools.exec.host": ("tools.exec.host", "none"),
        "openclaw.tools.fs.workspaceOnly": ("tools.fs.workspaceOnly", True),
        "openclaw.tools.web.search.enabled": ("tools.web.search.enabled", False),
        "openclaw.tools.web.fetch.enabled": ("tools.web.fetch.enabled", False),
        "openclaw.commands.native": ("commands.native", "off"),
        "openclaw.commands.nativeSkills": ("commands.nativeSkills", "off"),
        "openclaw.commands.elevated": ("commands.elevated", "deny"),
        "openclaw.commands.useAccessGroups": ("commands.useAccessGroups", True),
        "openclaw.commands.ownerAllowFrom": ("commands.ownerAllowFrom", ["another_bot"]),
    }
    for schema_path, (_dotpath, value) in fields.items():
        write_override(
            shared_dir, "team_bot_a", schema_path, value,
            set_by="rsi:prop_each", now=fixed_now,
        )
    cfg: dict = {}
    r = materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    for schema_path, (dotpath, value) in fields.items():
        # Each dotpath has been written into cfg at the right location.
        parts = dotpath.split(".")
        cur = cfg
        for seg in parts[:-1]:
            cur = cur[seg]
        assert cur[parts[-1]] == value, f"{dotpath} not materialized"
        assert dotpath in r.applied_dotpaths


def test_policy_slice_idempotent(shared_dir, fixed_now):
    """Running the slice twice produces the same cfg both times."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.tools.exec.security",
        "allowlist", set_by="rsi:p1", now=fixed_now,
    )
    cfg = {"tools": {"exec": {"security": "deny"}}}
    materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    after_first = json.dumps(cfg, sort_keys=True)
    materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert json.dumps(cfg, sort_keys=True) == after_first


def test_policy_slice_returns_dataclass_result(shared_dir, fixed_now):
    """Result is a PolicySliceResult with the expected attributes."""
    write_override(
        shared_dir, "team_bot_a", "openclaw.tools.exec.security",
        "allowlist", set_by="rsi:p1", now=fixed_now,
    )
    cfg = {}
    r = materialize_openclaw_policy_slice(cfg, "team_bot_a", shared_dir)
    assert isinstance(r, PolicySliceResult)
    assert hasattr(r, "applied_overrides")
    assert hasattr(r, "applied_dotpaths")
    assert hasattr(r, "skipped_unparseable_overrides")


# ─────────────────────────────────────────────────────────────────────────────
# PR A — cacheRetention wildcard fan-out
#
# Pins the cacheRetention tunable: one logical override → fanned out to
# every Anthropic model in the bot's catalog under
# ``agents.defaults.models.<id>.params.cacheRetention``. Non-Anthropic
# models stay untouched (the setting has no meaning for other providers).
# Motivating incident: team-bot-a session 3d5cde22 (2026-05-29) — $7.67 cost,
# 92% from prompt-cache writes invalidated by the 5-min TTL.
# ─────────────────────────────────────────────────────────────────────────────


def _kit_like_catalog_cfg() -> dict:
    """A catalog shaped like team-bot-a's real openclaw.json (verified 2026-05-30).
    Mix of Anthropic and non-Anthropic models with the existing
    ``alias`` + ``params`` metadata the materializer must preserve."""
    return {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {
                        "alias": "Sonnet",
                        "params": {"cacheRetention": "short"},
                    },
                    "anthropic/claude-opus-4-6": {
                        "alias": "Opus",
                        "params": {"cacheRetention": "short"},
                    },
                    "anthropic/claude-opus-4-7": {},
                    "google/gemini-2.5-pro": {},
                    "xai/grok-4": {},
                    "openai/gpt-5.5": {},
                    "openai/gpt-5.4-mini": {"alias": "gpt-mini"},
                    "anthropic/claude-haiku-4-5": {
                        "params": {"cacheRetention": "short"},
                    },
                    "anthropic/claude-sonnet-4-5": {
                        "params": {"cacheRetention": "short"},
                    },
                },
            },
        },
    }


def test_cache_retention_fans_out_to_every_anthropic_model(
    shared_dir, fixed_now, _be_config_importable,
):
    """Setting per_bot_cache_retention='long' in BE config updates
    params.cacheRetention on every Anthropic catalog entry while leaving
    non-Anthropic models untouched. Existing per-model metadata (alias)
    is preserved."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = _kit_like_catalog_cfg()
    r = materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)

    models = cfg["agents"]["defaults"]["models"]
    # Every Anthropic model now reads "long".
    for mid in (
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-6",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-5",
    ):
        assert models[mid]["params"]["cacheRetention"] == "long", (
            f"{mid} didn't get the fan-out value"
        )
    # Existing alias metadata preserved on the two models that had one.
    assert models["anthropic/claude-sonnet-4-6"]["alias"] == "Sonnet"
    assert models["anthropic/claude-opus-4-6"]["alias"] == "Opus"
    # Non-Anthropic models untouched — no ``params.cacheRetention`` key
    # gets inserted into google/openai/xai entries (other providers don't
    # accept the field).
    for mid in ("google/gemini-2.5-pro", "xai/grok-4",
                "openai/gpt-5.5", "openai/gpt-5.4-mini"):
        assert "cacheRetention" not in (models[mid].get("params") or {}), (
            f"{mid} (non-Anthropic) was incorrectly touched"
        )
    # Result enumerates the schema_path once and one dotpath per affected model.
    assert "openclaw.agents.defaults.models.cacheRetention" in r.applied_overrides
    affected_dotpaths = [d for d in r.applied_dotpaths if "cacheRetention" in d]
    assert len(affected_dotpaths) == 5  # five Anthropic models in the catalog


def test_cache_retention_short_value_also_fans_out(
    shared_dir, fixed_now, _be_config_importable,
):
    """Operator picks 'short' explicitly (== default): BE config carries
    the value and the materializer still propagates it, so downstream
    catalog-drift detectors see a consistent state."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="short")
    cfg = _kit_like_catalog_cfg()
    # Mutate one model to "long" so we can verify the override puts it back to "short".
    cfg["agents"]["defaults"]["models"]["anthropic/claude-haiku-4-5"]["params"][
        "cacheRetention"
    ] = "long"
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert (
        cfg["agents"]["defaults"]["models"]["anthropic/claude-haiku-4-5"]
        ["params"]["cacheRetention"]
        == "short"
    )


def test_cache_retention_no_override_is_no_op(shared_dir):
    """No override → materializer doesn't touch params.cacheRetention on
    any model. (The shipped default is enforced at OC's runtime — the
    materializer's job is just to make the operator's override stick.)"""
    cfg = _kit_like_catalog_cfg()
    before = json.dumps(cfg, sort_keys=True)
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert json.dumps(cfg, sort_keys=True) == before


def test_cache_retention_creates_params_dict_when_absent(
    shared_dir, fixed_now, _be_config_importable,
):
    """Anthropic catalog entry without a ``params`` dict gets one created
    (rather than throwing or being silently skipped)."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {},  # no params dict
                },
            },
        },
    }
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert (
        cfg["agents"]["defaults"]["models"]["anthropic/claude-sonnet-4-6"]
        ["params"]["cacheRetention"]
        == "long"
    )


def test_cache_retention_empty_catalog_is_no_op(
    shared_dir, fixed_now, _be_config_importable,
):
    """BE config carries the value but the bot's catalog is empty → no error,
    nothing materialized (no models to fan out to)."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = {"agents": {"defaults": {"models": {}}}}
    r = materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    # No Anthropic models in the catalog → no fan-out entries recorded.
    assert "openclaw.agents.defaults.models.cacheRetention" not in r.applied_overrides
    assert cfg == {"agents": {"defaults": {"models": {}}}}


def test_cache_retention_anthropic_only_catalog_full_fan_out(
    shared_dir, fixed_now, _be_config_importable,
):
    """Pure-Anthropic catalog gets every model updated (the team-bot-a case after
    a non-anthropic-credential cleanup, for example)."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = {
        "agents": {
            "defaults": {
                "models": {
                    "anthropic/claude-sonnet-4-6": {"alias": "Sonnet"},
                    "anthropic/claude-haiku-4-5": {},
                },
            },
        },
    }
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert (cfg["agents"]["defaults"]["models"]
            ["anthropic/claude-sonnet-4-6"]["params"]["cacheRetention"]) == "long"
    assert (cfg["agents"]["defaults"]["models"]
            ["anthropic/claude-haiku-4-5"]["params"]["cacheRetention"]) == "long"


def test_cache_retention_idempotent(
    shared_dir, fixed_now, _be_config_importable,
):
    """Running the slice twice produces byte-identical cfg both times."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = _kit_like_catalog_cfg()
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    after_first = json.dumps(cfg, sort_keys=True)
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert json.dumps(cfg, sort_keys=True) == after_first


def test_cache_retention_clearing_be_config_is_no_op(
    shared_dir, fixed_now, _be_config_importable,
):
    """Clearing per_bot_cache_retention in BE config (setting to null) makes
    the slice no longer touch the catalog; the operator's pre-existing
    in-cfg values stay put. Materializer is not in the business of resetting
    drift — that's the auto-promote pass's job."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_cache_retention="long")
    cfg = _kit_like_catalog_cfg()
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    # Snapshot post-set: every anthropic model is "long".
    snapshot_post_set = {
        mid: m.get("params", {}).get("cacheRetention")
        for mid, m in cfg["agents"]["defaults"]["models"].items()
        if mid.startswith("anthropic/")
    }
    assert all(v == "long" for v in snapshot_post_set.values())

    # Now clear: rewrite BE config with no per_bot_cache_retention.
    _write_be_config(shared_dir, "team-bot-a")  # empty budget block
    r = materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    # Without the value, the slice no longer applies cacheRetention.
    cache_applied = [d for d in r.applied_dotpaths if "cacheRetention" in d]
    assert cache_applied == []


# test_cache_retention_read_path_walks_wildcard removed in Phase 4b:
# the cacheRetention TunableKey was deleted from the sandbox schema,
# so by_path returns None for this path and stores.read_value no longer
# applies. Customizations UI no longer surfaces this knob (it's in the
# canonical Cost & caps card now).


# ─────────────────────────────────────────────────────────────────────────────
# PR C — sessionBudgetCapUsd scalar materialization
#
# The new tunable is a plain scalar at agents.defaults.sessionBudgetCapUsd
# (no wildcard, no fan-out). It flows through the same _policy_slice_entries
# loop as the permission-posture fields — these tests pin that integration
# and confirm the cap is written verbatim when an override exists.
# Spec: docs/spec-session-budget-cap-2026-05-31.md (this PR).
# ─────────────────────────────────────────────────────────────────────────────


def test_session_budget_cap_unset_is_no_op(shared_dir, _be_config_importable):
    """No BE config value → cfg unchanged, no sessionBudgetCapUsd key inserted.
    The shipped default is None (no cap) and the materializer's job is
    only to make a deliberate operator value stick."""
    cfg = {"agents": {"defaults": {}}}
    before = json.dumps(cfg, sort_keys=True)
    r = materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert json.dumps(cfg, sort_keys=True) == before
    assert "openclaw.agents.defaults.sessionBudgetCapUsd" not in r.applied_overrides


def test_session_budget_cap_be_config_writes_scalar(
    shared_dir, fixed_now, _be_config_importable,
):
    """Operator-set value lands at agents.defaults.sessionBudgetCapUsd."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_session_cost_cap_usd=2.50)
    cfg = {"agents": {"defaults": {}}}
    r = materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert cfg["agents"]["defaults"]["sessionBudgetCapUsd"] == 2.50
    assert "openclaw.agents.defaults.sessionBudgetCapUsd" in r.applied_overrides
    assert "agents.defaults.sessionBudgetCapUsd" in r.applied_dotpaths


def test_session_budget_cap_supersedes_existing_disk_value(
    shared_dir, fixed_now, _be_config_importable,
):
    """BE config value beats whatever was on disk pre-materialize. Layering
    rule from spec §7: per-bot override is the top of the cascade."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_session_cost_cap_usd=5.00)
    cfg = {"agents": {"defaults": {"sessionBudgetCapUsd": 1.00}}}
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert cfg["agents"]["defaults"]["sessionBudgetCapUsd"] == 5.00


def test_session_budget_cap_creates_nested_path(
    shared_dir, fixed_now, _be_config_importable,
):
    """BE-config value on a cfg that's missing the agents.defaults parent
    path still lands (the dotpath setter creates intermediate dicts)."""
    _write_be_config(shared_dir, "team-bot-a", per_bot_session_cost_cap_usd=1.50)
    cfg = {}  # totally empty
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    assert cfg == {"agents": {"defaults": {"sessionBudgetCapUsd": 1.50}}}


def test_session_budget_cap_schema_entry_is_removed():
    """Phase 4b deleted the TunableKey schema entry — by_path returns None.
    The customizations UI no longer surfaces this knob; it lives in the
    canonical Cost & caps card now."""
    from evolve_admin.config_sandbox import by_path
    assert by_path("openclaw.agents.defaults.sessionBudgetCapUsd") is None
    assert by_path("openclaw.agents.defaults.models.cacheRetention") is None


# ─────────────────────────────────────────────────────────────────────────────
# Cost-cap normalization — BE config helpers (used by the tests above)
#
# Spec: docs/spec-cost-caps-2026-06-05.md.
#
# After Phase 4b, ``better-engine-config.json`` is the SOLE source for the
# per-bot per-session cap + cache TTL. The sandbox-override path is gone,
# and the TunableKey schema entries for these two keys are deleted (see
# test_session_budget_cap_schema_entry_is_removed above). The fixture +
# helper below stay because the cache-retention + session-budget tests
# above need to write a BE config file in their tmp shared_dir.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _be_config_importable():
    """Ensure ``better_engine_config`` is on sys.path so the materializer's
    in-function import succeeds. Production wires this via the admin server's
    bootstrap path; tests have to do it themselves."""
    import sys as _sys
    from pathlib import Path as _Path
    analyzer_dir = _Path(__file__).resolve().parents[2] / "analyzer"
    added = False
    if str(analyzer_dir) not in _sys.path:
        _sys.path.insert(0, str(analyzer_dir))
        added = True
    yield
    if added:
        try:
            _sys.path.remove(str(analyzer_dir))
        except ValueError:
            pass
        _sys.modules.pop("better_engine_config", None)


def _write_be_config(shared_dir: Path, bot_id: str, **bot_budget) -> None:
    """Write a minimal better-engine-config.json with a per-bot budget block."""
    payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {bot_id: {"budget": bot_budget}},
    }
    (shared_dir / "better-engine-config.json").write_text(json.dumps(payload))


def test_legacy_sandbox_value_is_silently_ignored(
    shared_dir, fixed_now, _be_config_importable,
):
    """A sandbox-overrides file from before Phase 4b that still carries
    one of the two retired cost keys is now silently ignored — neither
    by_path nor the materializer's slice loop will surface it. Pinning
    this so a future regression doesn't reintroduce the duplicate
    source-of-truth bug."""
    # No BE config file exists; only a legacy sandbox override remains.
    # write_override would reject these paths now (schema entries gone),
    # so we hand-write the file in the legacy shape instead.
    import json as _json
    overrides_dir = shared_dir / "sandbox" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    overrides_dir.joinpath("team-bot-a.json").write_text(_json.dumps({
        "bot_id": "team-bot-a",
        "overrides": {
            "openclaw.agents.defaults.sessionBudgetCapUsd": {
                "value": 1.00, "set_by": "legacy", "set_at": "2026-05-01",
            },
        },
    }))
    cfg: dict = {}
    materialize_openclaw_policy_slice(cfg, "team-bot-a", shared_dir)
    # No sessionBudgetCapUsd written — sandbox path is dead.
    assert cfg == {}
