"""Regression tests for the ``backup.py --commit-baseline-local`` CLI flag.

Added 2026-05-30 after a deploy-time privilege leak: ``deploy_bot`` used to
import ``commit_baseline_local`` and call it in-process. Since
``sudo evolve-admin deploy`` runs as root, the function's ``git config``
writes + ``backup_dir.mkdir`` left ``.git/config`` (mode 0600) and
``evolve-backup/`` owned by ``root:staff``, locking out the bot's nightly
backup daemon.

The fix routes the call through this CLI flag, invoked from deploy.py via
``sudo -H -u <bot_user> python3 backup.py --commit-baseline-local <bot_id>``,
so the writes happen under the bot's uid. These tests pin the contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import backup  # noqa: E402


def test_cli_flag_invokes_commit_baseline_local_and_exits_zero_on_ok(monkeypatch, capsys):
    """--commit-baseline-local <bot_id> calls commit_baseline_local and exits 0."""
    calls: list[tuple[str, Path]] = []

    def fake_commit(bot_id: str, shared_dir: Path):
        calls.append((bot_id, shared_dir))
        return True, "already at baseline — no commit needed"

    monkeypatch.setattr(backup, "commit_baseline_local", fake_commit)
    monkeypatch.setattr(backup, "load_config", lambda _network: {"sharedDir": "/tmp/shared-test"})
    monkeypatch.setattr(backup, "get_shared_dir", lambda _cfg: Path("/tmp/shared-test"))
    monkeypatch.setattr(sys, "argv", ["backup.py", "--commit-baseline-local", "team_bot_a"])

    with pytest.raises(SystemExit) as exc:
        backup.main()

    assert exc.value.code == 0
    assert calls == [("team_bot_a", Path("/tmp/shared-test"))]
    out = capsys.readouterr().out
    assert "baseline-refresh team_bot_a" in out
    assert "already at baseline" in out


def test_cli_flag_exits_nonzero_on_failure(monkeypatch, capsys):
    """If commit_baseline_local returns (False, msg), CLI exits 1 — so callers
    (deploy.py) can surface the failure as a warning instead of swallowing it.
    """
    monkeypatch.setattr(
        backup, "commit_baseline_local",
        lambda bot_id, shared: (False, "workspace not found at /Users/team_bot_a/.openclaw/workspace"),
    )
    monkeypatch.setattr(backup, "load_config", lambda _network: {})
    monkeypatch.setattr(backup, "get_shared_dir", lambda _cfg: Path("/tmp"))
    monkeypatch.setattr(sys, "argv", ["backup.py", "--commit-baseline-local", "team_bot_a"])

    with pytest.raises(SystemExit) as exc:
        backup.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "workspace not found" in out


def test_cli_flag_short_circuits_before_per_bot_loop(monkeypatch):
    """The --commit-baseline-local path must not also iterate every bot for a
    full backup pass — that would re-run pushes the operator didn't ask for
    and double-bill on big pods. Pin the short-circuit explicitly.
    """
    bot_loop_ran = False

    def fake_backup_bot(*_a, **_kw):
        nonlocal bot_loop_ran
        bot_loop_ran = True
        return "ok"

    monkeypatch.setattr(
        backup, "commit_baseline_local",
        lambda bot_id, shared: (True, "ok"),
    )
    monkeypatch.setattr(backup, "backup_bot", fake_backup_bot)
    monkeypatch.setattr(backup, "load_config", lambda _network: {
        "members": ["team_bot_a", "team_bot_b"],
        "bots": {"team_bot_a": {"backupRepoUrl": "git@x:y.git"}},
    })
    monkeypatch.setattr(backup, "get_shared_dir", lambda _cfg: Path("/tmp"))
    monkeypatch.setattr(backup, "get_members", lambda _cfg: ["team_bot_a", "team_bot_b"])
    monkeypatch.setattr(sys, "argv", ["backup.py", "--commit-baseline-local", "team_bot_a"])

    with pytest.raises(SystemExit):
        backup.main()

    assert bot_loop_ran is False, (
        "--commit-baseline-local must short-circuit before the per-bot "
        "backup loop; otherwise deploy_bot would silently re-trigger pushes"
    )
