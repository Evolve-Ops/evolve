"""Tests for generator_runner.run_one_generator.

The on-demand helper used by scan_workspace_pipeline to surface
findings from a single generator without waiting for the scheduled
cadence. Mirrors the same ingest pipeline as ``run_generators`` (dedup,
fingerprint, charter invariants, rejection cooldown).

These tests use a real {shared_dir}/signals/ + {shared_dir}/proposals/
tree (under tmp_path) and a stub generator so we can verify the
end-to-end run-one-and-ingest cycle without mocking the arbiter.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import generator_runner


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def stub_network() -> dict:
    return {
        "networkId": "test-net",
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"role": "member", "user": "team_bot_a"}},
    }


# ── Unknown / unwired ids ────────────────────────────────────────────────────


def test_run_one_generator_unknown_id_returns_zero(shared_dir: Path, stub_network: dict):
    """Unknown generator id → no-op + log, no crash."""
    result = generator_runner.run_one_generator(
        shared_dir, stub_network, "no_such_generator_id_exists",
        bot_id="team_bot_a",
    )
    assert result == 0


def test_run_one_generator_per_bot_without_bot_id_is_no_op(shared_dir: Path, stub_network: dict):
    """Per-bot generators called without bot_id → no-op (would otherwise
    try to build a context with bot_id=None and emit nothing useful)."""
    result = generator_runner.run_one_generator(
        shared_dir, stub_network, "app_permission_review",
        # bot_id intentionally omitted
    )
    assert result == 0


# ── Registry-not-active path ─────────────────────────────────────────────────


def test_run_one_generator_inactive_returns_zero(
    shared_dir: Path, stub_network: dict, monkeypatch,
):
    """If the named generator is in _CONTEXT_FACTORIES but its charter
    isn't loaded / it's marked inactive, the run should be a clean no-op."""
    # Patch active_generators to return an empty dict
    real_registry_class = None

    class _StubRegistry:
        def __init__(self, *args, **kwargs):
            pass

        def load_all(self, strict=False):
            return {}

        def active_generators(self):
            return {}

    monkeypatch.setattr("registry.registry.Registry", _StubRegistry)

    result = generator_runner.run_one_generator(
        shared_dir, stub_network, "app_permission_review",
        bot_id="team_bot_a",
    )
    assert result == 0


# ── End-to-end: run a real generator on a real synthetic shared_dir ──────────


def test_run_one_generator_ingests_app_permission_review_findings(
    shared_dir: Path, stub_network: dict, tmp_path: Path, monkeypatch,
):
    """End-to-end: scanner-shaped invocation actually produces proposals.

    Builds a synthetic bot home with a manifest+permissions block that
    will fire a finding, then calls run_one_generator and checks that a
    proposal landed in the pending dir.

    Patches evolve_admin.config.bot_home so the generator's lazy import
    points at our tmp_path tree instead of /Users/team_bot_a/.
    """
    # Build a bot home that will fire one app_permission_review finding
    # (manifest declares an exec entry for a file that doesn't exist).
    bot_home_path = tmp_path / "team_bot_a_home"
    manifests_dir = bot_home_path / ".openclaw" / "workspace" / "manifests"
    manifests_dir.mkdir(parents=True)
    (manifests_dir / "i-app.json").write_text(json.dumps({
        "id": "i-app",
        "name": "App",
        "files": [],  # no files
        "permissions": {"exec": ["scripts/ghost.py"]},  # ghost.py doesn't exist
    }))

    # Patch the helpers the generator's lazy import grabs
    import evolve_admin.config as _admin_cfg
    monkeypatch.setattr(_admin_cfg, "bot_home", lambda bot_id, network=None: bot_home_path)
    monkeypatch.setattr(_admin_cfg, "load_network", lambda: stub_network)

    ingested = generator_runner.run_one_generator(
        shared_dir, stub_network, "app_permission_review",
        bot_id="team_bot_a",
    )
    # Should have ingested at least the one ghost.py finding
    assert ingested >= 1

    # And the proposal should be persisted under shared/proposals/pending/
    pending = shared_dir / "proposals" / "pending"
    assert pending.exists()
    pending_files = list(pending.glob("*.json"))
    assert len(pending_files) >= 1
    found = False
    for pf in pending_files:
        try:
            data = json.loads(pf.read_text())
        except Exception:
            continue
        if (data.get("generator_id") == "app_permission_review"
            and "ghost.py" in (data.get("problem") or "")):
            found = True
            break
    assert found, (
        "expected a proposal mentioning ghost.py from app_permission_review"
    )


def test_run_one_generator_zero_proposals_is_clean_no_op(
    shared_dir: Path, stub_network: dict, tmp_path: Path, monkeypatch,
):
    """When the generator emits 0 proposals (e.g. no manifests have
    permissions blocks), the call returns 0 cleanly."""
    bot_home_path = tmp_path / "team_bot_a_home"
    (bot_home_path / ".openclaw" / "workspace" / "manifests").mkdir(parents=True)
    # No manifests with permissions blocks → review emits 0 proposals

    import evolve_admin.config as _admin_cfg
    monkeypatch.setattr(_admin_cfg, "bot_home", lambda bot_id, network=None: bot_home_path)
    monkeypatch.setattr(_admin_cfg, "load_network", lambda: stub_network)

    result = generator_runner.run_one_generator(
        shared_dir, stub_network, "app_permission_review",
        bot_id="team_bot_a",
    )
    assert result == 0
