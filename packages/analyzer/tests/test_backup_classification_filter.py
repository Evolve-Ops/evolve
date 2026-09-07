"""tests/test_backup_classification_filter.py — Phase 3 + 4b pipeline integration.

Verifies _apply_classification_pruning (with the back-compat alias
_apply_classification_filter) wired into _backup_bot_attempt:

- Pre-v15 pods (no manifests with classification) get a no-op pass —
  every path stays in the index.
- v15 manifests with data_paths unstage matching local/ephemeral paths
  from both the index AND HEAD via ``git rm --cached --ignore-unmatch``.
- Phase 4b reclassification: paths in HEAD but not in the index get the
  rm-from-index treatment too, so the next commit removes them from the
  cloud repo.
- The pruner is fail-open: a broken resolver doesn't block the commit.
- Operator-declared paths get unstaged at scale (chunking + partial-failure).
- First-backup case (no HEAD commit yet) is handled gracefully.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))


# ─── git stub helpers ──────────────────────────────────────────────────────


def _make_git_stub(
    *,
    index_paths: list[str] | None = None,
    head_paths: list[str] | None = None,
    head_error: str | None = None,
    rm_calls: list | None = None,
):
    """Build a git stub matching the Phase 4b pruner's commands.

    ``index_paths`` → return value of ``git ls-files --cached``
    ``head_paths``  → return value of ``git ls-tree -r --name-only HEAD``
    ``head_error``  → stderr to return on ``ls-tree HEAD`` (with rc=128).
                      Set this to "bad default revision 'HEAD'" to simulate
                      the first-backup-before-HEAD-exists case.
    ``rm_calls``    → optional list to append rm command args to (for
                      assertions about exact paths passed to rm).
    """
    index_paths = index_paths or []
    head_paths  = head_paths or []

    def stub(args, cwd, env=None):
        if args[:2] == ["ls-files", "--cached"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="\n".join(index_paths) + ("\n" if index_paths else ""),
                stderr="",
            )
        if args[:4] == ["ls-tree", "-r", "--name-only", "HEAD"]:
            if head_error is not None:
                return subprocess.CompletedProcess(
                    args=args, returncode=128, stdout="", stderr=head_error,
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="\n".join(head_paths) + ("\n" if head_paths else ""),
                stderr="",
            )
        if args[:2] == ["rm", "--cached"]:
            if rm_calls is not None:
                rm_calls.append(args)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr="",
        )
    return stub


def _rm_paths(rm_call: list[str]) -> list[str]:
    """Extract the path args (after the ``--`` terminator) from a single rm call."""
    if "--" in rm_call:
        return rm_call[rm_call.index("--") + 1:]
    return []


# ─── Pruner unit tests ─────────────────────────────────────────────────────


def test_pruner_noop_when_no_manifests(tmp_path, monkeypatch):
    """Pre-v15 pod with no manifests at all: every staged + HEAD file passes through."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    workspace.mkdir()
    rm_calls = []
    monkeypatch.setattr(
        "backup._git",
        _make_git_stub(
            index_paths=["notes/foo.md", "diary.py"],
            head_paths=["diary.py", "SOUL.md"],
            rm_calls=rm_calls,
        ),
    )
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert n == 0
    assert err is None
    assert rm_calls == []  # nothing classified non-cloud → no rm


def test_pruner_excludes_local_paths_in_index(tmp_path, monkeypatch):
    """Index paths matching a local data_path get unstaged."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "notes-app.json").write_text(_json.dumps({
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))

    rm_calls = []
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=["notes/2026.md", "notes/sub/private.md", "index/abc.json", "SOUL.md"],
        head_paths=[],
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 2
    assert len(rm_calls) == 1
    # ``--ignore-unmatch`` is in the rm args.
    assert "--ignore-unmatch" in rm_calls[0]
    paths = _rm_paths(rm_calls[0])
    assert sorted(paths) == ["notes/2026.md", "notes/sub/private.md"]


def test_pruner_excludes_head_only_reclassified_paths(tmp_path, monkeypatch):
    """Phase 4b: a path that's already in HEAD but now classifies local gets pruned.

    Scenario: operator pushed `notes/foo.md` last week when `notes/` was
    cloud-classified. Today they marked `notes/` as local in the Data tab.
    The file is still in HEAD but is not modified in this backup's index.
    The pruner should `git rm --cached` it so the next commit removes it
    from the cloud repo.
    """
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "notes-app.json").write_text(_json.dumps({
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))

    rm_calls = []
    monkeypatch.setattr("backup._git", _make_git_stub(
        # Nothing new in the index this run...
        index_paths=["SOUL.md"],
        # ...but notes/foo.md is in HEAD from a previous backup.
        head_paths=["SOUL.md", "notes/foo.md", "notes/old.md"],
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 2  # notes/foo.md and notes/old.md
    paths = _rm_paths(rm_calls[0])
    assert sorted(paths) == ["notes/foo.md", "notes/old.md"]


def test_pruner_unions_index_and_head(tmp_path, monkeypatch):
    """Paths in both index and HEAD aren't double-counted."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "notes-app.json").write_text(_json.dumps({
        "id": "notes-app",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))

    rm_calls = []
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=["notes/a.md", "notes/b.md"],
        head_paths=["notes/a.md", "notes/c.md"],  # a is in both
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 3  # a, b, c — not 4
    paths = _rm_paths(rm_calls[0])
    assert sorted(paths) == ["notes/a.md", "notes/b.md", "notes/c.md"]


def test_pruner_first_backup_no_head_is_silent(tmp_path, monkeypatch):
    """A workspace whose first backup commit hasn't been made yet: ls-tree HEAD
    fails with `bad default revision 'HEAD'`. The pruner should handle this
    silently and just process the index. Operator-declared local rule lets
    us verify the no-HEAD path without depending on a built-in."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "notes.json").write_text(_json.dumps({
        "id": "notes",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))
    rm_calls = []
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=["notes/private.md", "SOUL.md"],
        head_error="fatal: bad default revision 'HEAD'",
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 1
    assert _rm_paths(rm_calls[0]) == ["notes/private.md"]


def test_pruner_unexpected_head_error_fails_open(tmp_path, monkeypatch):
    """A non-expected ls-tree HEAD error returns an error string (fail-open)."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=["foo.md"],
        head_error="fatal: permission denied",
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert n == 0
    assert "ls-tree HEAD failed" in (err or "")


def test_pruner_does_not_strip_evolve_backup_payload(tmp_path, monkeypatch):
    """Regression for the 2026-05-29 review-session bug.

    The original ``evolve-backup/ → ephemeral`` built-in was framed as a
    "recursion guard" but actually stripped the cloud backup's own
    payload files (``evolve-backup/openclaw.json``, ``metrics/``,
    ``state.json``) — the very files ``backup.py`` writes there for the
    cloud commit. With no operator-declared rules, those paths must now
    pass through the pruner unchanged.
    """
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    workspace.mkdir()
    rm_calls = []
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=[
            "evolve-backup/openclaw.json",
            "evolve-backup/state.json",
            "evolve-backup/metrics/latest.json",
            "SOUL.md",
        ],
        head_paths=[],
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 0          # nothing should be unstaged
    assert rm_calls == []  # no rm --cached call at all


def test_pruner_chunks_large_excluded_set(tmp_path, monkeypatch):
    """Exclusions get chunked at 50 paths/call to avoid ARG_MAX."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "cache-app.json").write_text(_json.dumps({
        "id": "cache-app",
        "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
    }))
    rm_calls = []
    paths = [f"cache/file-{i}.json" for i in range(120)]
    monkeypatch.setattr("backup._git", _make_git_stub(
        index_paths=paths,
        head_paths=[],
        rm_calls=rm_calls,
    ))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert err is None
    assert n == 120
    # 120 / 50 = 3 chunks (50 + 50 + 20).
    assert len(rm_calls) == 3
    assert sum(len(_rm_paths(c)) for c in rm_calls) == 120


def test_pruner_fails_open_on_resolver_error(tmp_path, monkeypatch):
    """If the resolver build crashes, the pruner returns no-op + error string."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    workspace.mkdir()

    def boom(**kw):
        raise RuntimeError("synthetic resolver crash")

    monkeypatch.setattr("backup._build_classification_resolver", boom)
    monkeypatch.setattr("backup._git", _make_git_stub(index_paths=["x.md"]))
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert n == 0
    assert err is not None
    assert "synthetic resolver crash" in err


def test_pruner_fails_open_on_ls_files_error(tmp_path, monkeypatch):
    """``git ls-files --cached`` failure → no-op + error."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    workspace.mkdir()

    def git_stub(args, cwd, env=None):
        if args[:2] == ["ls-files", "--cached"]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="fatal: index corrupt",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backup._git", git_stub)
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert n == 0
    assert "ls-files --cached failed" in (err or "")


def test_pruner_returns_partial_count_on_rm_failure(tmp_path, monkeypatch):
    """If a chunked rm fails partway, return the count of successful chunks."""
    from backup import _apply_classification_pruning

    workspace = tmp_path / "ws"
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True)
    import json as _json
    (mdir / "cache-app.json").write_text(_json.dumps({
        "id": "cache-app",
        "data_paths": [{"path": "cache/", "privacy": "ephemeral"}],
    }))

    paths = [f"cache/file-{i}.json" for i in range(120)]
    rm_call_count = {"n": 0}
    def git_stub(args, cwd, env=None):
        if args[:2] == ["ls-files", "--cached"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="\n".join(paths), stderr="",
            )
        if args[:4] == ["ls-tree", "-r", "--name-only", "HEAD"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:2] == ["rm", "--cached"]:
            rm_call_count["n"] += 1
            # First call succeeds, second fails.
            if rm_call_count["n"] == 1:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="permission denied",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr("backup._git", git_stub)
    n, err = _apply_classification_pruning("team_bot_a", workspace, network=None)
    assert n == 50  # only the first chunk succeeded
    assert "rm --cached failed" in (err or "")


def test_back_compat_alias_still_exposes_old_name():
    """The Phase 4b rename keeps _apply_classification_filter as an alias."""
    from backup import _apply_classification_filter, _apply_classification_pruning
    assert _apply_classification_filter is _apply_classification_pruning


# ─── End-to-end: _backup_bot_attempt invokes the pruner ───────────────────


def test_backup_bot_attempt_calls_pruner_after_add(tmp_path, monkeypatch):
    """Sanity check that the pruner sits between ``git add -A`` and commit."""
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    call_order: list[str] = []
    def git_stub(args, cwd, env=None):
        if args[:1] == ["add"]:
            call_order.append("add")
        elif args[:2] == ["ls-files", "--cached"]:
            call_order.append("ls-files")
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="x.md\n", stderr="",
            )
        elif args[:4] == ["ls-tree", "-r", "--name-only", "HEAD"]:
            call_order.append("ls-tree-head")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        elif args[:3] == ["diff", "--cached", "--quiet"]:
            # Pretend the index has something staged so we fall through
            # to the commit branch (rc=1 means "diff present").
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        elif args[:1] == ["commit"]:
            call_order.append("commit")
        elif args[:2] == ["status", "--porcelain"]:
            # Pre-Phase-4b porcelain check is gone; this branch is now dead
            # but kept for compatibility with the broader stub.
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=" M x.md\n", stderr="",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backup._bot_home", lambda b: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda b, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._copy_with_sudo", lambda s, d, b: (True, ""))
    monkeypatch.setattr("backup._redact_snapshot_file", lambda p: (False, "no-secrets"))
    monkeypatch.setattr("backup.sha256_file", lambda p: "h" * 16)
    monkeypatch.setattr("backup.get_applied_since_last_backup", lambda b, s, w: [])
    monkeypatch.setattr("backup.now_iso", lambda: "2026-05-29T00:00:00Z")
    monkeypatch.setattr("backup._load_github_pat", lambda n: "ghp_test")
    monkeypatch.setattr("backup.check_repo_visibility", lambda url, pat=None: "private")

    _backup_bot_attempt(
        bot_id="team_bot_a",
        shared_dir=shared_dir,
        backup_url="git@github.com:cjalden/test.git",
        dry_run=False,
        network={},
    )
    add_i        = call_order.index("add")
    ls_files_i   = call_order.index("ls-files")
    ls_tree_i    = call_order.index("ls-tree-head")
    commit_i     = call_order.index("commit")
    assert add_i < ls_files_i < commit_i, (
        f"Pruner ls-files@{ls_files_i} did not land between add@{add_i} and commit@{commit_i}"
    )
    assert add_i < ls_tree_i < commit_i, (
        f"Pruner ls-tree-head@{ls_tree_i} did not land between add@{add_i} and commit@{commit_i}"
    )


# ─── Regression from 2026-05-29 review session ───────────────────────────────


def test_pruner_runs_even_when_working_tree_is_clean(tmp_path, monkeypatch):
    """Phase 4b cleanup must fire when the operator reclassifies a path even
    if nothing else changed in the working tree.

    Before this fix, `_backup_bot_attempt` did its `git status --porcelain`
    check BEFORE `git add -A` + the pruner. A clean working tree → early
    return → pruner never ran → reclassified paths stayed in HEAD forever
    and the Phase 4a audit Signal stayed firing.

    This test puts a previously-cloud path in HEAD, declares it local in a
    fresh manifest, leaves the working tree clean (porcelain empty), and
    asserts the pruner ran (ls-files + ls-tree HEAD both fired between add
    and the commit attempt).
    """
    from backup import _backup_bot_attempt

    bot_home = tmp_path / "bot"
    workspace = bot_home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (mdir / "notes.json").write_text(_json.dumps({
        "id": "notes",
        "data_paths": [{"path": "notes/", "privacy": "local"}],
    }))
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    call_order: list[str] = []
    def git_stub(args, cwd, env=None):
        if args[:1] == ["add"]:
            call_order.append("add")
        elif args[:2] == ["ls-files", "--cached"]:
            call_order.append("ls-files")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        elif args[:4] == ["ls-tree", "-r", "--name-only", "HEAD"]:
            call_order.append("ls-tree-head")
            # Simulate a previously-committed note that's now local.
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="notes/leaked.md\nSOUL.md\n", stderr="",
            )
        elif args[:2] == ["rm", "--cached"]:
            call_order.append("rm-cached")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        elif args[:3] == ["diff", "--cached", "--quiet"]:
            # After the rm --cached, the index has a staged deletion → diff
            # exists → rc=1.
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        elif args[:1] == ["commit"]:
            call_order.append("commit")
        elif args[:2] == ["status", "--porcelain"]:
            # WORKING TREE IS CLEAN. Pre-fix this short-circuited everything.
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backup._bot_home", lambda b: bot_home)
    monkeypatch.setattr("backup._ssh_env", lambda b, network=None: {})
    monkeypatch.setattr("backup._git", git_stub)
    monkeypatch.setattr("backup._copy_with_sudo", lambda s, d, b: (True, ""))
    monkeypatch.setattr("backup._redact_snapshot_file", lambda p: (False, "no-secrets"))
    monkeypatch.setattr("backup.sha256_file", lambda p: "h" * 16)
    monkeypatch.setattr("backup.get_applied_since_last_backup", lambda b, s, w: [])
    monkeypatch.setattr("backup.now_iso", lambda: "2026-05-29T00:00:00Z")
    monkeypatch.setattr("backup._load_github_pat", lambda n: "ghp_test")
    monkeypatch.setattr("backup.check_repo_visibility", lambda url, pat=None: "private")

    _backup_bot_attempt(
        bot_id="team_bot_a",
        shared_dir=shared_dir,
        backup_url="git@github.com:cjalden/test.git",
        dry_run=False,
        network={},
    )
    # All three pruner-side steps must run, AND we must reach commit
    # (because the staged deletion means there IS something to commit).
    assert "ls-files" in call_order
    assert "ls-tree-head" in call_order
    assert "rm-cached" in call_order
    assert "commit" in call_order, (
        "Pruner staged a deletion but commit was not reached — Phase 4b "
        "cleanup ordering regression"
    )
