"""tests/test_backup_workspace_drift.py — workspace-drift detection tests.

Fixture-git-repo coverage for the 2026-07-28 incident's first blind
spot: a bot-authored rogue backup job committing nightly in the
workspace (varying LLM-generated backup-ish messages) plus a second git
remote with an embedded credential in ``.git/config``.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_signal as bs  # noqa: E402
import backup_workspace_drift as bwd  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── fixture-repo helpers ─────────────────────────────────────────────────────


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "HOME": str(repo.parent),  # keep git away from real global config
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env, check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path, name: str = "workspace") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    return repo


def _commit(repo: Path, message: str, *, age: timedelta | None = None) -> None:
    marker = repo / "f.txt"
    marker.write_text(
        marker.read_text() + "x" if marker.exists() else "x", encoding="utf-8",
    )
    _git(repo, "add", "-A")
    date = None
    if age is not None:
        date = (datetime.now(timezone.utc) - age).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _git(repo, "commit", "-q", "-m", message, date=date)


# ── collect_workspace_drift ──────────────────────────────────────────────────


def test_clean_repo_yields_none(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _commit(repo, "[backup] nightly workspace sync")
    _commit(repo, "add new notes file")
    assert bwd.collect_workspace_drift("team_bot_a", repo) is None


def test_missing_repo_is_silently_no_finding(tmp_path):
    """A broken/absent repo is other signals' job — never a drift claim."""
    assert bwd.collect_workspace_drift("team_bot_a", tmp_path / "nope") is None
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    assert bwd.collect_workspace_drift("team_bot_a", not_a_repo) is None


def test_unexpected_remote_detected_and_credential_redacted(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    # The incident shape: a rogue HTTPS remote with an embedded PAT.
    _git(
        repo, "remote", "add", "evolve-backup",
        "https://x-access-token:ghp_FAKEFAKEFAKE@github.com/x/rogue.git",
    )
    _commit(repo, "[backup] nightly workspace sync")
    findings = bwd.collect_workspace_drift("team_bot_a", repo)
    assert findings is not None
    assert [r["name"] for r in findings["unexpected_remotes"]] == ["evolve-backup"]
    url = findings["unexpected_remotes"][0]["url"]
    assert "ghp_FAKEFAKEFAKE" not in url
    assert "x-access-token" not in url
    assert "<credentials-redacted>" in url
    assert "github.com/x/rogue.git" in url


def test_rogue_backupish_commit_detected(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _commit(repo, "[backup] nightly workspace sync")
    _commit(repo, "Automated cron backup")
    _commit(repo, "workspace-backup: nightly sync")
    _commit(repo, "tweak prompt wording")  # not backup-ish — ignored
    findings = bwd.collect_workspace_drift("team_bot_a", repo)
    assert findings is not None
    assert findings["unexpected_remotes"] == []
    subjects = {c["subject"] for c in findings["rogue_commits"]}
    assert subjects == {"Automated cron backup", "workspace-backup: nightly sync"}
    assert findings["rogue_commit_count"] == 2


def test_legit_backup_prefix_never_counts_as_rogue(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _commit(repo, "[backup] nightly workspace sync")
    _commit(repo, "[backup] snapshot after apply")
    assert bwd.collect_workspace_drift("team_bot_a", repo) is None


def test_old_rogue_commit_outside_window_ignored(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _commit(repo, "Automated cron backup", age=timedelta(days=5))
    assert bwd.collect_workspace_drift("team_bot_a", repo) is None


def test_repo_without_commits_but_extra_remote_still_fires(tmp_path):
    """git log fails on an empty repo; the remote finding must survive."""
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _git(repo, "remote", "add", "spare", "git@github.com:x/spare.git")
    findings = bwd.collect_workspace_drift("team_bot_a", repo)
    assert findings is not None
    assert [r["name"] for r in findings["unexpected_remotes"]] == ["spare"]
    assert findings["rogue_commit_count"] == 0


# ── last_backup_commit_iso (silence-check anchor) ────────────────────────────


def test_last_backup_commit_iso_returns_timestamp(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "[backup] nightly workspace sync", age=timedelta(days=3))
    _commit(repo, "unrelated change")
    iso = bwd.last_backup_commit_iso(repo)
    assert iso is not None
    # git may emit a trailing Z; datetime.fromisoformat can't parse it
    # until Python 3.11 and the repo floor is 3.10 — normalize first
    # (same pattern as backup_signal._parse_iso_utc).
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - ts
    assert timedelta(days=2, hours=23) < age < timedelta(days=3, hours=1)


def test_last_backup_commit_iso_none_without_backup_commits(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "just a normal commit")
    assert bwd.last_backup_commit_iso(repo) is None
    assert bwd.last_backup_commit_iso(tmp_path / "nope") is None


# ── build_signal_for_workspace_drift ─────────────────────────────────────────


def _findings(*, remotes=(), commits=(), count=None):
    commits = [{"sha": f"abc{i}", "subject": s} for i, s in enumerate(commits)]
    return {
        "unexpected_remotes": [
            {"name": n, "url": f"git@github.com:x/{n}.git"} for n in remotes
        ],
        "rogue_commits": commits,
        "rogue_commit_count": count if count is not None else len(commits),
    }


def test_drift_signal_alert_when_unexpected_remote():
    spec = bwd.build_signal_for_workspace_drift(
        "team_bot_a", "url", _findings(remotes=["evolve-backup"]),
    )
    assert spec["type"] == "workspace_backup_drift"
    assert spec["severity"] == "alert"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team_bot_a"
    assert "evolve-backup" in spec["body"]
    assert "Evolve owns workspace backups" in spec["body"]


def test_drift_signal_warn_when_only_rogue_commits():
    spec = bwd.build_signal_for_workspace_drift(
        "team_bot_a", "url", _findings(commits=["Automated cron backup"]),
    )
    assert spec["severity"] == "warn"
    assert "Automated cron backup" in spec["body"]


def test_drift_signal_signature_stable_per_bot():
    a = bwd.build_signal_for_workspace_drift(
        "team_bot_a", "url", _findings(remotes=["evolve-backup"]),
    )
    b = bwd.build_signal_for_workspace_drift(
        "team_bot_a", "url", _findings(commits=["Automated cron backup"]),
    )
    assert a["signature"] == b["signature"]
    c = bwd.build_signal_for_workspace_drift(
        "team_bot_b", "url", _findings(remotes=["evolve-backup"]),
    )
    assert c["signature"] != a["signature"]


def test_drift_signal_truncated_sample_notes_overflow():
    spec = bwd.build_signal_for_workspace_drift(
        "team_bot_a", "url",
        _findings(commits=["backup 1", "backup 2"], count=9),
    )
    assert "and 7 more" in spec["body"]
    assert spec["details"]["rogue_commit_count"] == 9


# ── wiring through backup_signal.collect_for_bot / run() ─────────────────────


def _pat_ok(_config):
    return "test-pat"


def _private(_url, pat=None):
    return "private"


def _key_unknown(_user, _bot):
    return None


def _fresh_state(_sd, _bot):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"last_attempt_at": now, "consecutive_failures": 0}


def test_collect_for_bot_emits_drift_signal(tmp_path):
    out = bs.collect_for_bot(
        tmp_path, "team_bot_a",
        {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}},
        state_loader=_fresh_state,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        drift_collector=lambda _bot, _cfg: _findings(remotes=["evolve-backup"]),
    )
    assert len(out) == 1
    assert out[0]["type"] == "workspace_backup_drift"


def test_collect_for_bot_skips_drift_for_unconfigured_bot(tmp_path):
    """No backupRepoUrl → the workspace isn't Evolve's to police here."""
    calls = []

    def _spy(bot, _cfg):
        calls.append(bot)
        return _findings(remotes=["evolve-backup"])

    out = bs.collect_for_bot(
        tmp_path, "personal_bot",
        {"bots": {"personal_bot": {}}},
        state_loader=_fresh_state,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        drift_collector=_spy,
    )
    assert out == []
    assert calls == []


def test_run_drift_fires_and_resolves_when_cleaned(tmp_path):
    cfg = {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}}

    # Pass 1: rogue remote + rogue commits observed.
    kept, n_fired, _ = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=_fresh_state,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        drift_collector=lambda _bot, _cfg: _findings(
            remotes=["evolve-backup"], commits=["Automated cron backup"],
        ),
    )
    assert n_fired == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="backup_signal"))
    assert len(sigs) == 1
    assert sigs[0].type == "workspace_backup_drift"
    assert sigs[0].severity == "alert"

    # Pass 2: operator removed the rogue job + remote — sweep archives.
    kept2, n_fired2, n_resolved2 = bs.run(
        tmp_path, cfg, bots=["team_bot_a"],
        state_loader=_fresh_state,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        drift_collector=lambda _bot, _cfg: None,
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert list(signals_store.iter_active(tmp_path, producer="backup_signal")) == []


def test_run_end_to_end_with_fixture_repo(tmp_path):
    """Full path: real git fixture repo → drift collector → signal store."""
    repo = _make_repo(tmp_path, name="ws")
    _git(repo, "remote", "add", "origin", "git@github.com:x/team_bot_a.git")
    _commit(repo, "[backup] nightly workspace sync")
    _commit(repo, "Automated cron backup")
    _git(
        repo, "remote", "add", "evolve-backup",
        "https://x-access-token:ghp_FAKEFAKEFAKE@github.com/x/rogue.git",
    )
    cfg = {"bots": {"team_bot_a": {"backupRepoUrl": "url"}}}
    store_dir = tmp_path / "signals-store"
    store_dir.mkdir()

    _, n_fired, _ = bs.run(
        store_dir, cfg, bots=["team_bot_a"],
        state_loader=_fresh_state,
        pat_loader=_pat_ok, visibility_checker=_private,
        ssh_key_checker=_key_unknown,
        drift_collector=lambda bot, _cfg: bwd.collect_workspace_drift(bot, repo),
    )
    assert n_fired == 1
    sigs = list(signals_store.iter_active(store_dir, producer="backup_signal"))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == "workspace_backup_drift"
    # The embedded PAT must never reach the signal store.
    dumped = sig.body + repr(sig.details)
    assert "ghp_FAKEFAKEFAKE" not in dumped
    assert "<credentials-redacted>" in dumped
