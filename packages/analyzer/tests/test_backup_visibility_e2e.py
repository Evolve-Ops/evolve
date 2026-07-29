"""tests/test_backup_visibility_e2e.py — Phase 1 end-to-end coverage.

Stitches the two halves of the visibility guard together:

1. The bot user's ``backup_bot()`` refuses to push when the repo is
   public, returns status "failed", and never invokes ``git push``.
2. The admin-side ``backup_signal.run()`` monitor — running its own
   independent visibility check — fires a high-severity
   ``backup_repo_public`` Signal that lands in
   ``{shared_dir}/signals/firing/``.

This is the integration story for the Phase-1 design: bot blocks
locally; monitor surfaces the alert. The two halves cooperate without
sharing process or filesystem permissions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_signal as bs  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _visibility_stub(value):
    def _check(url, pat=None):
        return value
    return _check


def test_public_repo_blocks_push_and_lands_in_signal_store(tmp_path, monkeypatch):
    """End-to-end: public repo → bot refuses push → monitor fires Signal."""
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    push_attempted = {"flag": False}

    def git_stub(args, cwd, env=None):
        if args[:2] == ["push", "origin"]:
            push_attempted["flag"] = True
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._load_github_pat", lambda network: "ghp_test")
    monkeypatch.setattr(
        "backup.check_repo_visibility", _visibility_stub("public"),
    )

    # ── Phase 1 of the integration: bot user's push attempt ─────────────
    status, err = _backup_bot_attempt(
        bot_id="team_bot_a",
        shared_dir=shared_dir,
        backup_url="git@github.com:cjalden/test-repo.git",
        dry_run=False,
        network={"github": {"pat": "ghp_test"}},
    )
    assert status == "failed"
    assert "public" in (err or "")
    assert push_attempted["flag"] is False, (
        "git push must not run when visibility guard refuses"
    )

    # ── Phase 2: admin-side monitor independently fires Signal ──────────
    cfg = {
        "bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:cjalden/test-repo.git"}},
        "github": {"pat": "ghp_test"},
    }
    kept, n_fired, n_resolved = bs.run(
        shared_dir, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=lambda _cfg: "ghp_test",
        visibility_checker=_visibility_stub("public"),
    )
    assert n_fired == 1
    assert len(kept) == 1

    # Signal landed in {shared_dir}/signals/firing/
    firing_dir = shared_dir / "signals" / "firing"
    assert firing_dir.exists(), (
        f"firing dir not created at {firing_dir}"
    )
    firing_files = list(firing_dir.glob("*.json"))
    assert len(firing_files) == 1, (
        f"expected 1 firing signal, found {len(firing_files)}"
    )

    active = list(signals_store.iter_active(shared_dir, producer="backup_signal"))
    assert len(active) == 1
    sig = active[0]
    assert sig.type == "backup_repo_public"
    assert sig.severity == "alert"
    assert sig.bot_id == "team_bot_a"
    assert "github.com/evolve-ops/test-repo/settings" in sig.body


def test_private_repo_allows_push_and_keeps_signal_store_clean(tmp_path, monkeypatch):
    """End-to-end inverse: private repo → guard transparent → no Signal."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    cfg = {
        "bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:cjalden/test-repo.git"}},
        "github": {"pat": "ghp_test"},
    }
    _, n_fired, _ = bs.run(
        shared_dir, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=lambda _cfg: "ghp_test",
        visibility_checker=_visibility_stub("private"),
    )
    assert n_fired == 0
    assert list(signals_store.iter_active(shared_dir, producer="backup_signal")) == []


def test_pat_missing_path_emits_unverified_signal_and_blocks_push(tmp_path, monkeypatch):
    """End-to-end PAT-missing: bot 'skipped' (not failed); monitor warns."""
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    def git_stub(args, cwd, env=None):
        if args[:2] == ["push", "origin"]:
            raise AssertionError(
                "push must not run when PAT is missing (fail-safe)"
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("backup._bot_home", lambda bot_id: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda bot_id, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._load_github_pat", lambda network: None)
    # check_repo_visibility shouldn't be reached, but stub it defensively.
    monkeypatch.setattr(
        "backup.check_repo_visibility", _visibility_stub("unknown"),
    )

    status, err = _backup_bot_attempt(
        bot_id="team_bot_a",
        shared_dir=shared_dir,
        backup_url="git@github.com:cjalden/test-repo.git",
        dry_run=False,
        network={},
    )
    # "skipped" not "failed" — config gap, not push failure.
    assert status == "skipped"
    assert "github.pat" in (err or "").lower() or "pat" in (err or "").lower()

    cfg = {
        "bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:cjalden/test-repo.git"}},
        # no github.pat
    }
    _, n_fired, _ = bs.run(
        shared_dir, cfg, bots=["team_bot_a"],
        state_loader=lambda _sd, _bot: None,
        pat_loader=lambda _cfg: None,
        visibility_checker=_visibility_stub("unknown"),
    )
    assert n_fired == 1
    active = list(signals_store.iter_active(shared_dir, producer="backup_signal"))
    assert len(active) == 1
    # 2026-06-03: missing-PAT signals are coalesced to a single pod-scope
    # Signal listing every bot with a configured backup repo (one PAT
    # fixes all of them). Was per-bot ``backup_visibility_unverified``
    # with details.reason="missing_pat".
    assert active[0].type == "backup_pat_missing"
    assert active[0].scope == "pod"
    assert "team_bot_a" in active[0].details["bot_ids"]
