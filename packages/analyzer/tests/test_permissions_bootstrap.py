"""Tests for permissions.bootstrap — derive baseline from observed state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from permissions import baseline as _bl
from permissions import bootstrap as _bs
from permissions import inventory as _inv
from permissions import monitor as _mon


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def _seed_bot(tmp_path: Path, bid: str, oc: dict, cron_jobs: list | None = None) -> Path:
    home = tmp_path / "bots" / bid
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(oc))
    if cron_jobs is not None:
        (home / ".openclaw" / "cron").mkdir(parents=True, exist_ok=True)
        (home / ".openclaw" / "cron" / "jobs.json").write_text(json.dumps({"jobs": cron_jobs}))
    return home


# ── derive_baseline ────────────────────────────────────────────────────────

def test_derive_baseline_picks_modal_pod_default(tmp_path: Path):
    homes = {
        "a": _seed_bot(tmp_path, "a", {"tools": {"exec": {"security": "full", "ask": "on-miss"}}}),
        "b": _seed_bot(tmp_path, "b", {"tools": {"exec": {"security": "full", "ask": "on-miss"}}}),
        "c": _seed_bot(tmp_path, "c", {"tools": {"exec": {"security": "allowlist", "ask": "always"}}}),
    }

    baseline = _bs.derive_baseline(
        ["a", "b", "c"],
        home_override_by_bot=homes,
    )

    pc = baseline["pod_default"]["permission_config"]
    # Modal = full (2 of 3) and on-miss (2 of 3)
    assert pc["tools.exec.security"] == "full"
    assert pc["tools.exec.ask"] == "on-miss"
    # Bot c diverges → override
    assert "c" in baseline["per_bot_overrides"]
    assert baseline["per_bot_overrides"]["c"]["permission_config"]["tools.exec.security"] == "allowlist"
    # Bots a and b match the modal → no override
    assert "a" not in baseline["per_bot_overrides"]
    assert "b" not in baseline["per_bot_overrides"]


def test_derive_baseline_skips_unreadable_bots(tmp_path: Path):
    """A bot whose openclaw.json is missing is simply absent from the baseline."""
    homes = {
        "a": _seed_bot(tmp_path, "a", {"tools": {"exec": {"security": "full"}}}),
        "ghost": tmp_path / "bots" / "ghost",  # no openclaw.json
    }
    # Make sure ghost has the dir but not the file
    (homes["ghost"] / ".openclaw").mkdir(parents=True)

    baseline = _bs.derive_baseline(["a", "ghost"], home_override_by_bot=homes)

    assert "ghost" not in baseline["per_bot_overrides"]


def test_derive_baseline_captures_real_heterogeneity(tmp_path: Path):
    """Today's pod has team_bot_c with workspaceOnly=true — that should land as override."""
    homes = {
        "team_bot_a": _seed_bot(tmp_path, "team_bot_a", {
            "tools": {"exec": {"security": "full", "ask": "on-miss"}},
        }),
        "admin_bot": _seed_bot(tmp_path, "admin_bot", {
            "tools": {"exec": {"security": "full", "ask": "on-miss"}},
        }),
        "team_bot_c": _seed_bot(tmp_path, "team_bot_c", {
            "tools": {
                "exec": {"security": "full", "ask": "on-miss"},
                "fs": {"workspaceOnly": True},
            },
        }),
    }
    baseline = _bs.derive_baseline(list(homes.keys()), home_override_by_bot=homes)

    overrides = baseline["per_bot_overrides"]
    assert "team_bot_c" in overrides
    assert overrides["team_bot_c"]["permission_config"]["tools.fs.workspaceOnly"] is True
    assert "team_bot_a" not in overrides
    assert "admin_bot" not in overrides


# ── bootstrap (full flow) ───────────────────────────────────────────────────

def test_bootstrap_writes_baseline_and_cron_baselines(tmp_path: Path, shared_dir: Path):
    homes = {
        "a": _seed_bot(tmp_path, "a", {"tools": {"exec": {"security": "full"}}},
                       cron_jobs=[{"id": "j1", "name": "ping"}]),
    }

    baseline = _bs.bootstrap(
        shared_dir, ["a"], home_override_by_bot=homes,
    )

    # Baseline file written
    assert _bl.baseline_path(shared_dir).exists()
    loaded = _bl.load(shared_dir)
    assert loaded["pod_default"]["permission_config"]["tools.exec.security"] == "full"

    # Cron baseline written
    cron_bl = _mon._load_cron_baseline(shared_dir, "a")
    assert cron_bl is not None
    assert cron_bl["job_ids"] == ["j1"]


def test_bootstrap_no_op_when_baseline_exists(tmp_path: Path, shared_dir: Path):
    # Pre-write a baseline with a distinctive marker
    initial = {
        "version": 1,
        "pod_default": {"permission_config": {"tools.exec.security": "deny"}},
        "per_bot_overrides": {},
    }
    _bl.write(initial, shared_dir)
    homes = {"a": _seed_bot(tmp_path, "a", {"tools": {"exec": {"security": "full"}}})}

    result = _bs.bootstrap(shared_dir, ["a"], home_override_by_bot=homes)

    # Existing baseline preserved (no overwrite)
    assert result["pod_default"]["permission_config"]["tools.exec.security"] == "deny"


def test_bootstrap_overwrite_resnapshots(tmp_path: Path, shared_dir: Path):
    initial = {
        "version": 1,
        "pod_default": {"permission_config": {"tools.exec.security": "deny"}},
        "per_bot_overrides": {},
    }
    _bl.write(initial, shared_dir)
    homes = {"a": _seed_bot(tmp_path, "a", {"tools": {"exec": {"security": "full"}}})}

    result = _bs.bootstrap(shared_dir, ["a"], home_override_by_bot=homes, overwrite=True)

    # Overwrite took the observed value
    assert result["pod_default"]["permission_config"]["tools.exec.security"] == "full"


def test_bootstrap_then_monitor_first_run_is_silent_on_drift(
    monkeypatch, tmp_path: Path, shared_dir: Path,
):
    """The whole point of bootstrap: matches reality → no drift signals on first run."""
    homes = {
        "team_bot_a": _seed_bot(tmp_path, "team_bot_a", {
            "tools": {"exec": {"security": "full", "ask": "on-miss"},
                      "fs": {"workspaceOnly": False}},
        }),
        "team_bot_c": _seed_bot(tmp_path, "team_bot_c", {
            "tools": {"exec": {"security": "full", "ask": "on-miss"},
                      "fs": {"workspaceOnly": True}},
        }),
    }
    monkeypatch.setattr(_inv, "bot_home", lambda bid, *a, **kw: homes[bid])

    _bs.bootstrap(shared_dir, list(homes.keys()), home_override_by_bot=homes)
    monkeypatch.setattr(_mon, "_signals_store", None)

    result = _mon.run(shared_dir, list(homes.keys()), emit_signals=False)

    drift = [f for f in result["findings"] if f["type"] == "perm_config_drift"]
    assert drift == []
