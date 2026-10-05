"""Tests for the plugin-config schema-skew fix.

Root cause (live on evolve-vps-pod 2026-06-25): the admin wrote ``repoRoot``
into ``plugins.entries.evolve.config`` unconditionally, but the DEPLOYED plugin
manifest the gateway loads can LAG the repo source (the VPS repo-puller needs
2-3 passes to fast-forward). When the staged manifest predated #3115 it lacked
``repoRoot`` in its strict ``additionalProperties: false`` configSchema, so OC
rejected the ENTIRE config on every reload — silent + total.

The fix keys BOTH the materializer's identity-write and the deploy.py strip
pass on the DEPLOYED manifest the gateway actually loads, so the admin never
writes (or leaves) a key the running plugin would reject. The schema-additive
identity field ``repoRoot`` is written iff the deployed manifest declares it;
omitting it during a lag window degrades gracefully (analyzer-dir falls back to
dirname(sharedDir)/evolve-repo) instead of breaking every reload.

These tests must FAIL against main (which writes repoRoot unconditionally and
sources the strip from the repo manifest).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_profile import LINUX, MACOS, set_profile

from evolve_admin.openclaw_materializer import (
    _read_manifest_config_keys,
    deployed_plugin_config_keys,
    load_plugin_config_schema,
    materialize_evolve_plugin_config,
)
from evolve_admin.plugin_schema_skew import (
    PRODUCER,
    compute_skew_keys,
    reconcile_plugin_schema_skew,
)
from signals import store as signals_store


# A full, current manifest — mirrors packages/plugin/openclaw.plugin.json.
_FULL_PROPS = {
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
}

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
    "repoRoot": "/Users/Shared/evolve-repo",   # deterministic identity value
    "bots": {"team_bot_a": {"role": "member"}},
}


def _write_manifest(path: Path, *, include_repo_root: bool, strict: bool = True) -> Path:
    props = dict(_FULL_PROPS)
    if not include_repo_root:
        props.pop("repoRoot")
    path.write_text(json.dumps({
        "id": "evolve",
        "configSchema": {
            "type": "object",
            "additionalProperties": False if strict else True,
            "properties": props,
        },
    }))
    return path


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture(autouse=True)
def _reset_profile():
    yield
    set_profile(None)


# ── Materializer write/strip gate (both OS profiles) ─────────────────────────

@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_repo_root_omitted_and_stripped_when_deployed_manifest_lags(
    profile, tmp_path, shared_dir
):
    """Deployed manifest MISSING repoRoot → the materializer must NOT write
    repoRoot AND must strip a pre-existing one, leaving a config with only keys
    the deployed manifest declares (no additionalProperties violation)."""
    manifest = _write_manifest(tmp_path / "deployed.json", include_repo_root=False)
    set_profile(profile)
    current_block = {
        "botId": "team_bot_a", "role": "member", "networkId": "test-pod",
        "sharedDir": "/Users/Shared/evolve",
        "repoRoot": "/stale/checkout/path",   # left over from a prior deploy
        "tier": "full", "classifierModel": "anthropic/claude-haiku-4-5",
        "tierClassification": "session", "summarizerMinTurns": 2,
        "classifierKeywordConfidenceFloor": 0.80, "costLedgerEnabled": True,
    }
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current_block=current_block,
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert "repoRoot" not in r.new_block, "must not write a key the deployed plugin rejects"
    assert "repoRoot" in r.pruned_stale, "pre-existing repoRoot should be reported pruned"
    deployed_keys = _read_manifest_config_keys(manifest)
    assert deployed_keys is not None
    extra = set(r.new_block) - deployed_keys
    assert not extra, f"config carries keys the deployed manifest rejects: {extra}"


@pytest.mark.parametrize("profile", [MACOS, LINUX], ids=["macos", "linux"])
def test_repo_root_written_when_deployed_manifest_current(profile, tmp_path, shared_dir):
    """Deployed manifest WITH repoRoot → the materializer writes it."""
    manifest = _write_manifest(tmp_path / "deployed.json", include_repo_root=True)
    set_profile(profile)
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current_block={},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["repoRoot"] == "/Users/Shared/evolve-repo"


def test_repo_root_written_when_manifest_non_strict(tmp_path, shared_dir):
    """Non-strict manifest (additionalProperties: true) → OC tolerates extra
    keys, so repoRoot is written even though the props omit it."""
    manifest = _write_manifest(
        tmp_path / "deployed.json", include_repo_root=False, strict=False
    )
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current_block={},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    assert r.new_block["repoRoot"] == "/Users/Shared/evolve-repo"


def test_repo_root_written_when_manifest_unreadable(tmp_path, shared_dir):
    """Missing manifest → degrade to writing repoRoot (pre-skew behavior); the
    caller's `openclaw config validate` is the ultimate gate."""
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current_block={},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=tmp_path / "nope.json",
    )
    assert r.new_block["repoRoot"] == "/Users/Shared/evolve-repo"


def test_core_identity_always_written_even_when_manifest_lags(tmp_path, shared_dir):
    """Core identity (botId/role/networkId/sharedDir) is NEVER gated."""
    manifest = _write_manifest(tmp_path / "deployed.json", include_repo_root=False)
    r = materialize_evolve_plugin_config(
        "team_bot_a", _NETWORK, current_block={},
        defaults_registry=_DEFAULTS, shared_dir=shared_dir,
        plugin_manifest=manifest,
    )
    for k in ("botId", "role", "networkId", "sharedDir"):
        assert k in r.new_block


# ── deployed_plugin_config_keys: staged-first, source-fallback ───────────────

def test_deployed_keys_prefers_staged(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(install / "openclaw.plugin.json", include_repo_root=False)
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)
    keys = deployed_plugin_config_keys(install, source)
    assert "repoRoot" not in keys, "must read the DEPLOYED (staged) manifest"


def test_deployed_keys_falls_back_to_source_when_staged_absent(tmp_path):
    install = tmp_path / "install"; install.mkdir()   # no manifest staged
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)
    keys = deployed_plugin_config_keys(install, source)
    assert "repoRoot" in keys, "fresh bring-up: fall back to source manifest"


def test_deployed_keys_empty_when_neither_readable(tmp_path):
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    assert deployed_plugin_config_keys(install, source) == set()


def test_load_schema_unions_only_core_identity(tmp_path):
    """A lagging manifest's allowed set must NOT re-add repoRoot via the
    identity union — that union was the bug."""
    manifest = _write_manifest(tmp_path / "deployed.json", include_repo_root=False)
    allowed, strict = load_plugin_config_schema(manifest)
    assert strict is True
    assert {"botId", "role", "networkId", "sharedDir"}.issubset(allowed)
    assert "repoRoot" not in allowed


def test_load_schema_tolerates_malformed_properties(tmp_path):
    """A manifest whose configSchema.properties is a non-dict (list/null) must
    not crash the (un-try/excepted) materializer call path."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "id": "evolve",
        "configSchema": {"additionalProperties": False, "properties": ["botId"]},
    }))
    allowed, strict = load_plugin_config_schema(p)
    assert strict is True
    assert allowed == set({"botId", "role", "networkId", "sharedDir"})  # CORE only


# ── _allowed_plugin_config_keys reads the staged path ────────────────────────

def test_deploy_allowed_keys_reads_staged_path(tmp_path, monkeypatch):
    import evolve_admin.deploy as deploy
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(install / "openclaw.plugin.json", include_repo_root=False)
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)
    monkeypatch.setattr(deploy, "PLUGIN_INSTALL_DIR", install)
    monkeypatch.setattr(deploy, "PLUGIN_SRC_DIR", source)
    keys = deploy._allowed_plugin_config_keys()
    assert "repoRoot" not in keys, "deploy strip must key on the DEPLOYED manifest"
    # staged removed → fall back to source (which still declares repoRoot)
    (install / "openclaw.plugin.json").unlink()
    assert "repoRoot" in deploy._allowed_plugin_config_keys()


# ── Skew Signal: fire on skew, resolve on catch-up ───────────────────────────

def test_skew_signal_fires_when_deployed_lags(shared_dir, tmp_path):
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(install / "openclaw.plugin.json", include_repo_root=False)
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)

    skew = reconcile_plugin_schema_skew(shared_dir, install_dir=install, source_dir=source)
    assert skew == {"repoRoot"}
    firing = list(signals_store.iter_active(shared_dir, producer=PRODUCER))
    assert len(firing) == 1
    assert firing[0].details["skew_keys"] == ["repoRoot"]
    assert str(install / "openclaw.plugin.json") in firing[0].details["deployed_manifest"]


def test_skew_signal_not_emitted_when_in_sync(shared_dir, tmp_path):
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(install / "openclaw.plugin.json", include_repo_root=True)
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)

    skew = reconcile_plugin_schema_skew(shared_dir, install_dir=install, source_dir=source)
    assert skew == set()
    assert list(signals_store.iter_active(shared_dir, producer=PRODUCER)) == []


def test_skew_signal_resolves_when_deployed_catches_up(shared_dir, tmp_path):
    install = tmp_path / "install"; install.mkdir()
    source = tmp_path / "source"; source.mkdir()
    deployed_manifest = install / "openclaw.plugin.json"
    _write_manifest(deployed_manifest, include_repo_root=False)
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)

    reconcile_plugin_schema_skew(shared_dir, install_dir=install, source_dir=source)
    assert len(list(signals_store.iter_active(shared_dir, producer=PRODUCER))) == 1

    # Staged plugin catches up → skew clears → Signal auto-resolves.
    _write_manifest(deployed_manifest, include_repo_root=True)
    reconcile_plugin_schema_skew(shared_dir, install_dir=install, source_dir=source)
    assert list(signals_store.iter_active(shared_dir, producer=PRODUCER)) == []
    archived = list(signals_store.iter_signals(shared_dir, subdirs=("archived",)))
    assert any(s.producer == PRODUCER and s.state == "resolved" for s in archived)


def test_compute_skew_empty_when_deployed_unreadable(tmp_path):
    install = tmp_path / "install"; install.mkdir()   # no deployed manifest
    source = tmp_path / "source"; source.mkdir()
    _write_manifest(source / "openclaw.plugin.json", include_repo_root=True)
    # Can't read the deployed manifest → can't claim skew.
    assert compute_skew_keys(install, source) == set()
