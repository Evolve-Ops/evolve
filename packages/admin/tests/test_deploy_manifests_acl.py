"""tests/test_deploy_manifests_acl.py — manifests/ dir creation + write ACL.

Atlas's atlas-daily-digest forge run on 2026-05-29 failed because
/Users/atlas/.openclaw/workspace/manifests/ didn't exist, and the
manifest write fallback in PR #1775 couldn't recover: sudoers grants
/bin/cp for the manifest path but NOT /bin/mkdir. The sudo mkdir -p
attempt failed with "a terminal is required to read the password",
the manifest write silently failed 4 times during the forge run, then
approve fired with "manifest not found".

Structural fix in set_evolve_read_acl: create workspace/manifests/
during ACL setup (runs on every deploy) so the dir always exists with
the evolve write ACL before any forge run needs it.

Strategy: use real tmp_path-rooted dirs so Path.exists / Path.glob
behave naturally; monkeypatch _bot_user_for to redirect the function
at our tmp tree; mock subprocess.run to capture the sudo invocations
without actually running them. This dodges the brittleness of patching
Path internals.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))


def _setup_fake_bot_tree(tmp_path, *, with_manifests: bool):
    """Build a fake bot home under tmp_path with the dirs the function
    expects, then monkeypatch the /Users/... prefix to redirect there.

    Returns the bot_home path for assertion convenience.
    """
    bot_home = tmp_path / "Users" / "atlas"
    oc = bot_home / ".openclaw"
    ws = oc / "workspace"
    (oc / "credentials").mkdir(parents=True)
    (oc / "profiles").mkdir(parents=True)
    (ws / "evolve").mkdir(parents=True)
    (ws / "evolve-backup").mkdir(parents=True)
    if with_manifests:
        (ws / "manifests").mkdir(parents=True)
    return bot_home


@pytest.fixture
def patched_deploy(tmp_path, monkeypatch):
    """Redirect set_evolve_read_acl from /Users/atlas/... to our tmp tree
    by patching the f-string compositions inside the function.

    Cleanest way: monkeypatch _bot_user_for to return our tmp-rooted
    path string. But that breaks the assumed shape ("/Users/{user}/...").
    Instead: we monkeypatch the deploy module's Path concatenations by
    intercepting subprocess.run — captured commands tell us what the
    function tried to do. Plus we patch the prefix via a sys.path-style
    redirect: the function builds strings like "/Users/atlas/.openclaw"
    that won't exist, so we patch Path's existence check with a
    redirector.
    """
    from evolve_admin import deploy

    bot_home_str = str(tmp_path / "Users" / "atlas")

    def redirect(s):
        return s.replace("/Users/atlas", bot_home_str)

    # Patch _bot_user_for to return our path-prefixed username so the
    # f-strings inside set_evolve_read_acl naturally hit tmp_path. We
    # use a sentinel "user" name and then post-process: the user value
    # is interpolated into "/Users/{bot_user}/.openclaw" — if we pass
    # a path-shaped value, e.g. "atlas" plus a redirect prefix, we get
    # the redirection for free.
    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: bot_home_str.replace("/Users/", "") + "/x")
    # The above is awkward. Cleaner: use the actual interpolated paths
    # and patch existence via a custom resolver. Use _path_resolver fixture.

    captured: list[list[str]] = []
    created: set[str] = set()

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if cmd[0] == "sudo" and cmd[1] == "/bin/mkdir" and "-p" in cmd:
            created.add(cmd[3])
        return MagicMock(returncode=0)

    return deploy, captured, fake_run, redirect


def test_set_evolve_read_acl_creates_manifests_dir_when_missing(tmp_path, monkeypatch):
    """Fresh bot path: .openclaw/workspace/ exists but manifests/ doesn't.
    set_evolve_read_acl must mkdir + chown + chmod +a so subsequent
    forge writes don't fall through to the broken sudo-mkdir path."""
    from evolve_admin import deploy

    # Use a fresh bot_user so /Users/{user} = tmp_path/User-Equivalent
    bot_home = _setup_fake_bot_tree(tmp_path, with_manifests=False)

    # Patch _bot_user_for to return a value such that /Users/{value}
    # resolves under tmp_path. macOS /Users is the real prefix; we
    # work around by patching the function's path-building to land
    # under tmp_path. Easiest path: monkeypatch the f-strings via the
    # actual function code by monkeypatching Path so any path starting
    # with /Users/atlas redirects to bot_home.
    bot_home_str = str(bot_home)
    real_exists = Path.exists
    real_glob = Path.glob

    def fake_exists(self):
        s = str(self)
        if s.startswith("/Users/atlas"):
            return real_exists(Path(s.replace("/Users/atlas", bot_home_str)))
        return real_exists(self)

    def fake_glob(self, pat):
        s = str(self)
        if s.startswith("/Users/atlas"):
            return real_glob(Path(s.replace("/Users/atlas", bot_home_str)), pat)
        return real_glob(self, pat)

    captured: list[list[str]] = []
    created: set[str] = set()

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        if cmd[0] == "sudo" and cmd[1] == "/bin/mkdir" and "-p" in cmd:
            # Also actually create it so the post-mkdir exists() check works
            real_target = cmd[3].replace("/Users/atlas", bot_home_str)
            Path(real_target).mkdir(parents=True, exist_ok=True)
            created.add(cmd[3])
        return MagicMock(returncode=0)

    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "atlas")

    with patch.object(Path, "exists", fake_exists), \
         patch.object(Path, "glob", fake_glob), \
         patch.object(Path, "iterdir", lambda self: iter([])), \
         patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run):
        deploy.set_evolve_read_acl("atlas")

    # Verify mkdir was called for the manifests path
    mkdir_calls = [c for c in captured if c[0] == "sudo" and c[1] == "/bin/mkdir"]
    assert any("/Users/atlas/.openclaw/workspace/manifests" in c[3] for c in mkdir_calls), (
        f"expected sudo mkdir for manifests/; got {mkdir_calls!r}"
    )
    # Verify chown was called
    chown_calls = [c for c in captured if c[0] == "sudo" and c[1] == "/usr/sbin/chown"]
    assert any("/Users/atlas/.openclaw/workspace/manifests" in c[3] for c in chown_calls)


def test_manifests_acl_grants_evolve_write(tmp_path, monkeypatch):
    """The ACL applied to manifests/ must include write,delete,add_file,
    add_subdirectory and the inheritance flags — same shape as
    workspace/evolve/."""
    from evolve_admin import deploy

    bot_home = _setup_fake_bot_tree(tmp_path, with_manifests=True)
    bot_home_str = str(bot_home)
    real_exists = Path.exists
    real_glob = Path.glob

    def fake_exists(self):
        s = str(self)
        if s.startswith("/Users/atlas"):
            return real_exists(Path(s.replace("/Users/atlas", bot_home_str)))
        return real_exists(self)

    def fake_glob(self, pat):
        s = str(self)
        if s.startswith("/Users/atlas"):
            return real_glob(Path(s.replace("/Users/atlas", bot_home_str)), pat)
        return real_glob(self, pat)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0)

    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "atlas")

    with patch.object(Path, "exists", fake_exists), \
         patch.object(Path, "glob", fake_glob), \
         patch.object(Path, "iterdir", lambda self: iter([])), \
         patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run):
        deploy.set_evolve_read_acl("atlas")

    chmod_calls = [c for c in captured if c[0] == "sudo" and c[1] == "/bin/chmod"]
    manifests_acl_calls = [
        c for c in chmod_calls
        if "manifests" in c[-1]
        and "+a" in c
        and any("write" in arg and "delete" in arg for arg in c)
    ]
    assert manifests_acl_calls, (
        f"expected chmod +a write/delete ACL on manifests/; got chmod: {chmod_calls!r}"
    )
    acl_string = next(
        arg for c in manifests_acl_calls for arg in c if "evolve allow" in arg
    )
    assert "file_inherit" in acl_string
    assert "directory_inherit" in acl_string


def test_manifests_acl_skipped_when_oc_dir_missing(tmp_path, monkeypatch):
    """If .openclaw/ doesn't exist, set_evolve_read_acl returns early
    without touching anything."""
    from evolve_admin import deploy

    # tmp_path has nothing in it — Path.exists returns False for all
    # /Users/atlas/... paths
    real_exists = Path.exists
    real_glob = Path.glob
    nonexistent = tmp_path / "nope" / "atlas"

    def fake_exists(self):
        s = str(self)
        if s.startswith("/Users/atlas"):
            return real_exists(Path(s.replace("/Users/atlas", str(nonexistent))))
        return real_exists(self)

    def fake_glob(self, pat):
        return iter([])

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0)

    monkeypatch.setattr(deploy, "_bot_user_for", lambda bot_id: "atlas")

    with patch.object(Path, "exists", fake_exists), \
         patch.object(Path, "glob", fake_glob), \
         patch.object(Path, "iterdir", lambda self: iter([])), \
         patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run):
        deploy.set_evolve_read_acl("atlas")

    manifests_calls = [c for c in captured if any("manifests" in str(arg) for arg in c)]
    assert not manifests_calls, (
        f"expected no manifests/ touches when .openclaw/ missing; got {manifests_calls!r}"
    )
