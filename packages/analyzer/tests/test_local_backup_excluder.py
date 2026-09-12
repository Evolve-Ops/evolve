"""tests/test_local_backup_excluder.py — Phase 4c TM exclusion reconciler."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import local_backup_excluder as lbe  # noqa: E402


# ─── _run stubs ───────────────────────────────────────────────────────────


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(rc: int = 1, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


def _make_runner(by_path_isexcluded: dict[str, bool], add_failures: set[str] | None = None):
    """Build a fake _run dispatching on tmutil subcommand + path arg.

    ``by_path_isexcluded`` maps absolute path string → True / False.
    Missing entries → return None-equivalent (rc=1).
    ``add_failures`` names paths whose addexclusion should fail.
    """
    add_failures = add_failures or set()
    calls = {"isexcluded": [], "addexclusion": []}

    def _run(args):
        if len(args) < 3:
            return _fail()
        sub = args[1]
        path = args[2]
        if sub == "isexcluded":
            calls["isexcluded"].append(path)
            if path in by_path_isexcluded:
                state = "[Excluded]" if by_path_isexcluded[path] else "[Included]"
                return _ok(f"{state} {path}\n")
            return _fail(rc=1)
        if sub == "addexclusion":
            calls["addexclusion"].append(path)
            if path in add_failures:
                return _fail(rc=1, stderr="tmutil: cannot add — permission denied")
            return _ok()
        return _fail()
    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ─── Path collection ──────────────────────────────────────────────────────


def test_collect_pod_wide_ephemeral_paths(tmp_path):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "data_paths": [
                {"path": "cache/", "privacy": "ephemeral"},
                {"path": "signals/", "privacy": "cloud"},
                {"path": "obs/", "privacy": "ephemeral"},
            ],
        },
        "bots": {},
    }
    paths = lbe.collect_ephemeral_paths(network, bots=[])
    assert paths == [shared_dir / "cache", shared_dir / "obs"]


def test_collect_skips_per_bot_paths_by_default(tmp_path, monkeypatch):
    """Per-bot manifests are NOT included in the v1 default sweep.

    Regression for the 2026-05-29 review-session bug: the daemon runs
    as ``evolve``, which has only read-ACL on ``.openclaw/workspace/``.
    ``tmutil addexclusion`` would EACCES on every per-bot ephemeral
    path. Opt-in via ``include_per_bot=True`` for future use.
    """
    team_bot_a_workspace = tmp_path / "team_bot_a-home" / ".openclaw" / "workspace"
    team_bot_a_workspace.mkdir(parents=True)

    network = {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"team_bot_a": {}},
    }
    (tmp_path / "shared").mkdir()

    monkeypatch.setattr(
        "local_backup_excluder.bot_home",
        lambda bot_id, net: tmp_path / f"{bot_id}-home",
    )

    notes_m = {
        "id": "notes",
        "data_paths": [
            {"path": "cache/", "privacy": "ephemeral"},
            {"path": "notes/", "privacy": "local"},
        ],
    }
    paths = lbe.collect_ephemeral_paths(
        network, bots=["team_bot_a"], manifest_loader=lambda ws: [notes_m],
    )
    # Default mode: pod-wide only. No pod-wide rules declared, so empty.
    assert paths == []


def test_collect_per_bot_ephemeral_paths_when_opted_in(tmp_path, monkeypatch):
    """The ``include_per_bot=True`` opt-in still walks per-bot manifests.

    Useful for future enhancements (per-bot daemons, sudo wrapping).
    """
    team_bot_a_workspace = tmp_path / "team_bot_a-home" / ".openclaw" / "workspace"
    team_bot_a_workspace.mkdir(parents=True)

    network = {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"team_bot_a": {}},
    }
    (tmp_path / "shared").mkdir()

    monkeypatch.setattr(
        "local_backup_excluder.bot_home",
        lambda bot_id, net: tmp_path / f"{bot_id}-home",
    )

    notes_m = {
        "id": "notes",
        "data_paths": [
            {"path": "cache/", "privacy": "ephemeral"},
            {"path": "notes/", "privacy": "local"},  # non-ephemeral — skipped
        ],
    }
    paths = lbe.collect_ephemeral_paths(
        network, bots=["team_bot_a"], manifest_loader=lambda ws: [notes_m],
        include_per_bot=True,
    )
    assert paths == [team_bot_a_workspace / "cache"]


def test_collect_dedups_across_bots_and_pod_when_opted_in(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    network = {
        "sharedDir": str(shared_dir),
        "backup": {"data_paths": [{"path": "cache/", "privacy": "ephemeral"}]},
        "bots": {"team_bot_a": {}, "admin_bot": {}},
    }
    (tmp_path / "team_bot_a-home" / ".openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / "admin_bot-home" / ".openclaw" / "workspace").mkdir(parents=True)

    monkeypatch.setattr(
        "local_backup_excluder.bot_home",
        lambda bot_id, net: tmp_path / f"{bot_id}-home",
    )
    common_m = {
        "id": "shared-app",
        "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
    }
    paths = lbe.collect_ephemeral_paths(
        network, bots=["team_bot_a", "admin_bot"],
        manifest_loader=lambda ws: [common_m],
        include_per_bot=True,
    )
    # Distinct: shared_dir/cache, team_bot_a/cache, admin_bot/cache.
    assert len(paths) == 3


def test_collect_skips_malformed_entries(tmp_path):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "data_paths": [
                {"path": "ok/", "privacy": "ephemeral"},
                {"path": "/absolute/no", "privacy": "ephemeral"},  # absolute → rejected
                {"path": "../escape", "privacy": "ephemeral"},     # ..-traversal → rejected
                "not-a-dict",
                {"privacy": "ephemeral"},                          # missing path
                {"path": "", "privacy": "ephemeral"},              # empty path
            ],
        },
        "bots": {},
    }
    paths = lbe.collect_ephemeral_paths(network, bots=[])
    assert paths == [shared_dir / "ok"]


# ─── Opt-in flag ──────────────────────────────────────────────────────────


def test_reconcile_disabled_when_opt_in_off(tmp_path, monkeypatch):
    network = {"bots": {}}
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    result = lbe.reconcile(network, _run=_make_runner({}))
    assert result.enabled is False
    assert result.newly_excluded == 0


def test_reconcile_enabled_when_flag_true(tmp_path, monkeypatch):
    network = {
        "bots": {},
        "backup": {"tm_exclusion_sync": True, "data_paths": []},
        "sharedDir": str(tmp_path),
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    result = lbe.reconcile(network, _run=_make_runner({}))
    assert result.enabled is True
    assert result.newly_excluded == 0  # no paths declared


def test_reconcile_non_macos_returns_unavailable(monkeypatch):
    network = {"bots": {}, "backup": {"tm_exclusion_sync": True}}
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: False)
    result = lbe.reconcile(network, _run=_make_runner({}))
    assert result.enabled is True
    assert result.available is False


# ─── Reconciler behaviour ─────────────────────────────────────────────────


def test_reconcile_excludes_new_paths(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared"
    cache_dir = shared_dir / "cache"
    cache_dir.mkdir(parents=True)

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    # Currently included → exclusion will be added.
    runner = _make_runner({str(cache_dir): False})
    result = lbe.reconcile(network, _run=runner, bots=[])
    assert result.candidates == 1
    assert result.newly_excluded == 1
    assert result.already_excluded == 0
    assert runner.calls["addexclusion"] == [str(cache_dir)]


def test_reconcile_skips_already_excluded(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared"
    cache_dir = shared_dir / "cache"
    cache_dir.mkdir(parents=True)

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    runner = _make_runner({str(cache_dir): True})
    result = lbe.reconcile(network, _run=runner, bots=[])
    assert result.already_excluded == 1
    assert result.newly_excluded == 0
    # No addexclusion call.
    assert runner.calls["addexclusion"] == []


def test_reconcile_skips_nonexistent_paths(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()  # but cache_dir does NOT exist

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    runner = _make_runner({})
    result = lbe.reconcile(network, _run=runner, bots=[])
    assert result.candidates == 1
    assert result.skipped_nonexistent == 1
    assert result.newly_excluded == 0
    assert runner.calls["isexcluded"] == []
    assert runner.calls["addexclusion"] == []


def test_reconcile_dry_run_does_not_call_addexclusion(tmp_path, monkeypatch):
    shared_dir = tmp_path / "shared"
    cache_dir = shared_dir / "cache"
    cache_dir.mkdir(parents=True)

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    runner = _make_runner({str(cache_dir): False})
    result = lbe.reconcile(network, _run=runner, bots=[], dry_run=True)
    assert result.newly_excluded == 1  # counted but not actually run
    assert runner.calls["addexclusion"] == []


def test_reconcile_records_per_path_errors_but_continues(tmp_path, monkeypatch):
    """One path's addexclusion failing shouldn't stop the rest from being added."""
    shared_dir = tmp_path / "shared"
    cache_a = shared_dir / "a"
    cache_b = shared_dir / "b"
    cache_a.mkdir(parents=True)
    cache_b.mkdir(parents=True)

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [
                {"path": "a/", "privacy": "ephemeral"},
                {"path": "b/", "privacy": "ephemeral"},
            ],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    runner = _make_runner(
        {str(cache_a): False, str(cache_b): False},
        add_failures={str(cache_a)},
    )
    result = lbe.reconcile(network, _run=runner, bots=[])
    assert result.candidates == 2
    assert result.newly_excluded == 1                # only b succeeded
    assert any(str(cache_a) in e for e in result.errors)
    # Both paths were attempted — the loop didn't bail on the first error.
    assert sorted(runner.calls["addexclusion"]) == sorted([str(cache_a), str(cache_b)])


def test_reconcile_isexcluded_unknown_falls_through_to_addexclusion(tmp_path, monkeypatch):
    """If isexcluded can't determine state, we still try addexclusion (additive safety)."""
    shared_dir = tmp_path / "shared"
    cache_dir = shared_dir / "cache"
    cache_dir.mkdir(parents=True)

    network = {
        "sharedDir": str(shared_dir),
        "backup": {
            "tm_exclusion_sync": True,
            "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
        },
        "bots": {},
    }
    monkeypatch.setattr("local_backup_excluder._is_macos", lambda: True)
    runner = _make_runner({})  # no isexcluded entry → returns None
    result = lbe.reconcile(network, _run=runner, bots=[])
    assert result.newly_excluded == 1
    assert runner.calls["addexclusion"] == [str(cache_dir)]


# ─── as_dict serializable ─────────────────────────────────────────────────


def test_result_as_dict_is_json_serializable():
    r = lbe.ExcluderResult(
        enabled=True, available=True, candidates=3,
        already_excluded=1, newly_excluded=2,
        errors=["/p: msg"],
    )
    blob = json.dumps(r.as_dict())  # must not raise
    assert "/p: msg" in blob
    assert "newly_excluded" in blob
