"""Tests for applications.marker_embed_helper — the narrow privileged write
behind the Sync 'Fix' (stamp marker) action for bot-owned workspace files.

The helper runs as ROOT under a single fixed-path sudoers grant, so its
input-validation IS the security boundary. These tests are adversarial: they
try to make the helper write OUTSIDE the intended bot's workspace (traversal,
symlink at the dest, symlinked ancestor escaping the workspace, symlinked
ancestor redirecting to ANOTHER bot), write a never-ownable path (AGENTS.md, an
evolve/ telemetry file), accept a bogus staged source, or write under the wrong
bot — and assert each is refused before any write. The symlinked-ancestor tests
double as the TOCTOU regression: the parent dir is opened (O_NOFOLLOW, per
component) by the SAME call that validates, and that held fd is what the write
uses, so a swapped-in symlink can never redirect the write. One happy-path test
confirms a valid scripts/*.py stamp preserves the file's prior mode and chowns
to the bot (the #2781 root-owned-lockout / mode-clamp guard).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import marker_embed_helper as meh  # noqa: E402
# Force the can_app_own → scanner → evolve_config import chain NOW, under the
# real profile, so its module-level get_profile() reads (shared_dir_default …)
# happen before the home_root fixture installs a user_home_root-only stub.
from evolve_admin.applications import app_ownership_policy as _aop  # noqa: E402,F401

_MARKED = "# evolve: spec=p-a3f91c8b@2026.05.20-1.0 file=f-d4e8f901@2026.05.20-1.0\nprint('hi')\n"
_BOT = "team_bot_a"


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    """A fake user-home root with one bot workspace + a marked script.

    Patches platform_profile.get_profile so the helper resolves the workspace
    under tmp_path instead of the real /Users or /home.
    """
    root = tmp_path / "homes"
    ws = root / _BOT / ".openclaw" / "workspace"
    (ws / "scripts").mkdir(parents=True)
    script = ws / "scripts" / "do_thing.py"
    script.write_text(_MARKED)
    os.chmod(script, 0o755)  # a cron-ish executable — prior mode must survive

    stub = types.SimpleNamespace(user_home_root=str(root))
    monkeypatch.setattr("platform_profile.get_profile", lambda: stub)
    return root


def _ws(home_root):
    return home_root / _BOT / ".openclaw" / "workspace"


def _stage(content=_MARKED):
    """Write a staged source under the real temp dir (helper requires it there)."""
    import tempfile
    fd, p = tempfile.mkstemp(dir="/tmp", prefix="evolve-marker-test-", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return p


def _walk(dest, expected_bot=_BOT):
    """Run _secure_walk_to_parent and always close any returned fd."""
    fd, leaf, bot, reason = meh._secure_walk_to_parent(str(dest), expected_bot)
    if fd is not None:
        os.close(fd)
    return fd, leaf, bot, reason


# ── _staged_source_ok ─────────────────────────────────────────────────────────


def test_staged_source_ok_accepts_marked_tmp_file():
    p = _stage()
    try:
        ok, reason = meh._staged_source_ok(p)
        assert ok, reason
    finally:
        os.unlink(p)


def test_staged_source_rejects_non_tmp_path():
    # _ADMIN_DIR is the admin package dir — definitely not under any temp dir.
    src = _ADMIN_DIR / "definitely_not_a_temp_file.py"
    ok, reason = meh._staged_source_ok(str(src))
    assert not ok and "not under a temp dir" in reason


def test_staged_source_rejects_symlink():
    real = _stage()
    link = real + ".link"
    os.symlink(real, link)
    try:
        ok, reason = meh._staged_source_ok(link)
        assert not ok  # symlink-at-path or realpath-containment both refuse
    finally:
        os.unlink(link)
        os.unlink(real)


def test_staged_source_rejects_unmarked_content():
    p = _stage(content="print('no marker here')\n")
    try:
        ok, reason = meh._staged_source_ok(p)
        assert not ok and "no provenance marker" in reason
    finally:
        os.unlink(p)


# ── _secure_walk_to_parent ────────────────────────────────────────────────────


def test_walk_accepts_ownable_script(home_root):
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    fd, leaf, bot, reason = _walk(dest)
    assert fd is not None, reason
    assert leaf == "do_thing.py"
    assert bot == _BOT


def test_walk_rejects_dotdot(home_root):
    dest = _ws(home_root) / "scripts" / ".." / ".." / "AGENTS.md"
    fd, leaf, bot, reason = _walk(dest)
    # realpath collapses .. ; the resulting AGENTS.md is rejected by can_app_own
    # (or by containment). Either way: refused, no fd.
    assert fd is None and reason


def test_walk_rejects_never_ownable_agents_md(home_root):
    agents = _ws(home_root) / "AGENTS.md"
    agents.write_text("# agents\n")
    fd, leaf, bot, reason = _walk(agents)
    assert fd is None and "ownable" in reason


def test_walk_rejects_evolve_telemetry(home_root):
    rec_dir = _ws(home_root) / "evolve"
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec = rec_dir / "rec-123.json"
    rec.write_text("{}\n")
    fd, leaf, bot, reason = _walk(rec)
    assert fd is None and "ownable" in reason


def test_walk_rejects_symlink_dest(home_root):
    scripts = _ws(home_root) / "scripts"
    link = scripts / "evil_link.py"
    os.symlink(scripts / "do_thing.py", link)
    fd, leaf, bot, reason = _walk(link)
    assert fd is None and "symlink" in reason


def test_walk_rejects_symlinked_ancestor_escaping_workspace(home_root, tmp_path):
    # A symlinked workspace subdir pointing OUTSIDE the workspace must not let a
    # write escape. This is also the TOCTOU regression: the O_NOFOLLOW per-
    # component walk is exactly what runs at write time.
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (outside / "victim.py").write_text("print('victim')\n")
    os.symlink(outside, _ws(home_root) / "linked")
    fd, leaf, bot, reason = _walk(_ws(home_root) / "linked" / "victim.py")
    assert fd is None and reason


def test_walk_rejects_symlinked_ancestor_into_other_bot(home_root):
    # A symlink from bot A's workspace into bot B's workspace must be refused by
    # the helper itself (not just the server gate) — cross-bot containment.
    other_ws = home_root / "team_bot_b" / ".openclaw" / "workspace" / "scripts"
    other_ws.mkdir(parents=True)
    (other_ws / "x.py").write_text(_MARKED)
    os.symlink(other_ws, _ws(home_root) / "blink")
    fd, leaf, bot, reason = _walk(_ws(home_root) / "blink" / "x.py")
    assert fd is None and reason  # realpath lands in team_bot_b → bot mismatch / symlink walk refused


def test_walk_rejects_wrong_expected_bot(home_root):
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    fd, leaf, bot, reason = _walk(dest, expected_bot="someone_else")
    assert fd is None and "expected" in reason


def test_walk_binds_to_the_account_not_the_bot_id(home_root):
    """The expected-bot argv is compared against the destination's own HOME
    component — the macOS/Linux ACCOUNT name. On a pod where a bot's account
    differs from its network.json bot_id (``team_bot_b`` running as
    ``personal_bot_user``), passing the id refuses the write; passing the
    account is accepted.

    This is the boundary the caller-side fix depends on, and the reason it is
    fixed at the CALLER: teaching the walk to also accept a bot_id would mean
    resolving network.json as root and would make the comparison — the thing
    that stops a cross-bot redirect — no longer derivable from the path alone.
    """
    ws = home_root / "personal_bot_user" / ".openclaw" / "workspace" / "scripts"
    ws.mkdir(parents=True)
    dest = ws / "app.py"
    dest.write_text(_MARKED)

    fd, leaf, bot, reason = _walk(dest, expected_bot="team_bot_b")  # the bot_id
    assert fd is None and "expected" in reason

    fd, leaf, bot, reason = _walk(dest, expected_bot="personal_bot_user")
    assert fd is not None and bot == "personal_bot_user" and leaf == "app.py"


def test_walk_still_refuses_a_genuine_cross_bot_redirect(home_root):
    """Companion to the test above: accepting the ACCOUNT must not soften the
    containment check. A destination in ANOTHER account's workspace is refused
    even when that account exists and the path is otherwise ownable."""
    other = home_root / "personal_bot_user" / ".openclaw" / "workspace" / "scripts"
    other.mkdir(parents=True)
    (other / "app.py").write_text(_MARKED)

    fd, leaf, bot, reason = _walk(other / "app.py", expected_bot=_BOT)
    assert fd is None and "expected" in reason


def test_walk_rejects_outside_workspace_tree(home_root):
    other = home_root / _BOT / ".openclaw" / "openclaw.json"
    other.write_text("{}\n")
    fd, leaf, bot, reason = _walk(other)
    assert fd is None and "workspace" in reason


def test_walk_rejects_relative_path(home_root):
    fd, leaf, bot, reason = _walk("scripts/do_thing.py")
    assert fd is None and "absolute" in reason


# ── main() end-to-end ─────────────────────────────────────────────────────────


def test_main_usage_requires_three_args(home_root):
    p = _stage()
    try:
        assert meh.main(["x", p, str(_ws(home_root) / "scripts" / "do_thing.py")]) == 2
    finally:
        os.unlink(p)


def test_main_rejects_bad_dest_before_write(home_root):
    p = _stage()
    agents = _ws(home_root) / "AGENTS.md"
    agents.write_text("# agents\n")
    before = agents.read_text()
    try:
        rc = meh.main(["x", p, str(agents), _BOT])
        assert rc == 2
        assert agents.read_text() == before  # untouched
    finally:
        os.unlink(p)


def test_main_happy_path_preserves_mode_and_owner(home_root, monkeypatch):
    """A valid scripts/*.py stamp applies the staged content, preserves the
    prior mode (0755), and chowns to the bot (uid/gid mocked to the test user)."""
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    assert (os.stat(dest).st_mode & 0o777) == 0o755

    new_marked = _MARKED.replace("p-a3f91c8b", "p-deadbeef")
    p = _stage(content=new_marked)

    monkeypatch.setattr(meh.pwd, "getpwnam",
                        lambda u: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    monkeypatch.setattr(meh.grp, "getgrnam",
                        lambda g: types.SimpleNamespace(gr_gid=os.getgid()))

    try:
        rc = meh.main(["x", p, str(dest), _BOT])
        assert rc == 0, "valid stamp should succeed"
        assert dest.read_text() == new_marked
        assert (os.stat(dest).st_mode & 0o777) == 0o755  # mode preserved
    finally:
        os.unlink(p)


# ── --delete mode (uninstall's failed-unlink fallback) ────────────────────────
#
# Same security boundary as the write: every refusal case above goes through
# the SAME _secure_walk_to_parent, so these tests pin the delete-specific
# surface — happy path, refusals leave the file on disk, and usage shape.


def test_delete_mode_unlinks_ownable_script(home_root):
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    assert meh.main(["x", "--delete", str(dest), _BOT]) == 0
    assert not dest.exists()


def test_delete_mode_rejects_wrong_bot_and_leaves_file(home_root):
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    assert meh.main(["x", "--delete", str(dest), "someone_else"]) == 2
    assert dest.exists()


def test_delete_mode_rejects_never_ownable_agents_md(home_root):
    agents = _ws(home_root) / "AGENTS.md"
    agents.write_text("# agents\n")
    assert meh.main(["x", "--delete", str(agents), _BOT]) == 2
    assert agents.exists()


def test_delete_mode_rejects_symlink_dest(home_root):
    scripts = _ws(home_root) / "scripts"
    link = scripts / "evil_link.py"
    os.symlink(scripts / "do_thing.py", link)
    assert meh.main(["x", "--delete", str(link), _BOT]) == 2
    # the symlink AND its target are untouched
    assert os.path.lexists(link)
    assert (scripts / "do_thing.py").exists()


def test_delete_mode_rejects_symlinked_ancestor_into_other_bot(home_root):
    other_ws = home_root / "team_bot_b" / ".openclaw" / "workspace" / "scripts"
    other_ws.mkdir(parents=True)
    victim = other_ws / "x.py"
    victim.write_text(_MARKED)
    os.symlink(other_ws, _ws(home_root) / "blink")
    assert meh.main(["x", "--delete", str(_ws(home_root) / "blink" / "x.py"), _BOT]) == 2
    assert victim.exists()


def test_delete_mode_missing_leaf_is_refusal(home_root):
    dest = _ws(home_root) / "scripts" / "never_existed.py"
    assert meh.main(["x", "--delete", str(dest), _BOT]) == 2


def test_delete_mode_refuses_directory_leaf(home_root, capsys):
    """A directory sitting where the manifest expects a file (Linux-pod wedge
    class): the walk refuses the non-regular leaf with exit 2 so the caller
    routes it to manual_delete_required instead of retrying forever."""
    dest = _ws(home_root) / "scripts" / "surprise_dir.py"
    dest.mkdir()
    assert meh.main(["x", "--delete", str(dest), _BOT]) == 2
    assert dest.is_dir()
    assert "a directory" in capsys.readouterr().err


def test_delete_mode_eperm_unlink_is_permanent_refusal(home_root, monkeypatch, capsys):
    """Root-denied unlink (EPERM ≙ macOS uchg/schg, Linux chattr +i, SIP) exits
    2, not 1: the condition is permanent until an operator clears the flag, so
    it must land in manual_delete_required — exit 1 kept it in failed_deletes
    and wedged finalize on every re-run."""
    import errno

    dest = _ws(home_root) / "scripts" / "do_thing.py"
    real_unlink = os.unlink

    def eperm_unlink(path, *a, **kw):
        if path == "do_thing.py" and kw.get("dir_fd") is not None:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(meh.os, "unlink", eperm_unlink)
    assert meh.main(["x", "--delete", str(dest), _BOT]) == 2
    assert dest.exists()
    err = capsys.readouterr().err
    assert err.startswith("marker_embed_helper:")  # the wrapper's refusal prefix
    assert "denied even to root" in err


def test_delete_mode_generic_io_error_stays_retryable(home_root, monkeypatch):
    """A non-EPERM unlink failure (e.g. EIO) is environmental — exit 1 keeps
    the file in the retryable failed_deletes bucket."""
    import errno

    dest = _ws(home_root) / "scripts" / "do_thing.py"
    real_unlink = os.unlink

    def eio_unlink(path, *a, **kw):
        if path == "do_thing.py" and kw.get("dir_fd") is not None:
            raise OSError(errno.EIO, "Input/output error")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(meh.os, "unlink", eio_unlink)
    assert meh.main(["x", "--delete", str(dest), _BOT]) == 1
    assert dest.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="chflags uchg is macOS/BSD-only")
def test_delete_mode_real_uchg_flag_refuses_and_names_flag(home_root, capsys):
    """End-to-end on macOS with a REAL uchg flag: unlink hits EPERM, the helper
    exits 2, and the refusal names the flag + the chflags remedy (this string
    lands in manual_delete_required and the archived manifest)."""
    import stat as _stat

    dest = _ws(home_root) / "scripts" / "do_thing.py"
    os.chflags(dest, _stat.UF_IMMUTABLE)
    try:
        rc = meh.main(["x", "--delete", str(dest), _BOT])
    finally:
        os.chflags(dest, 0)  # or tmp_path teardown chokes on the flag
    assert rc == 2
    assert dest.exists()
    err = capsys.readouterr().err
    assert "uchg" in err and "chflags nouchg" in err


def test_privileged_delete_wrapper_classifies_exit_codes(monkeypatch, tmp_path):
    """The ``(ok, detail, refused)`` contract the uninstall route keys its
    permanent-vs-resumable split on: exit 2 (root-side validation refusal) →
    ``refused=True``; exit 1 (I/O failure, or sudo itself failing on a missing
    grant) → ``refused=False`` so the file stays in the retryable bucket.
    """
    import subprocess as sp

    monkeypatch.setattr(meh, "helper_script_path", lambda: __file__)
    monkeypatch.setattr("evolve_admin.config.scanner_python", lambda: "python3")

    def fake_run(rc, err):
        return lambda *a, **kw: types.SimpleNamespace(returncode=rc, stderr=err, stdout="")

    target = tmp_path / "x.py"
    monkeypatch.setattr(sp, "run", fake_run(
        2, "marker_embed_helper: destination is not an application-ownable path"))
    ok, detail, refused = meh.delete_workspace_file_privileged(target, bot_user=_BOT)
    assert (ok, refused) == (False, True)
    assert "exit 2" in detail and "not an application-ownable path" in detail

    # Exit 2 WITHOUT the helper's stderr prefix is CPython failing to open the
    # script (mid-repo-pull race) — environmental, must stay retryable.
    monkeypatch.setattr(sp, "run", fake_run(2, "python3: can't open file '/x/helper.py'"))
    ok, _detail, refused = meh.delete_workspace_file_privileged(target, bot_user=_BOT)
    assert (ok, refused) == (False, False)

    monkeypatch.setattr(sp, "run", fake_run(1, "sudo: a password is required"))
    ok, _detail, refused = meh.delete_workspace_file_privileged(target, bot_user=_BOT)
    assert (ok, refused) == (False, False)

    def raise_timeout(*a, **kw):
        raise sp.TimeoutExpired(cmd="sudo", timeout=20)

    monkeypatch.setattr(sp, "run", raise_timeout)
    ok, _detail, refused = meh.delete_workspace_file_privileged(target, bot_user=_BOT)
    assert (ok, refused) == (False, False)

    monkeypatch.setattr(sp, "run", fake_run(0, ""))
    ok, _detail, refused = meh.delete_workspace_file_privileged(target, bot_user=_BOT)
    assert (ok, refused) == (True, False)


def test_main_happy_path_never_leaves_temp(home_root, monkeypatch):
    """After a successful stamp, no .marker-embed-* temp is left in the dir."""
    dest = _ws(home_root) / "scripts" / "do_thing.py"
    p = _stage()
    monkeypatch.setattr(meh.pwd, "getpwnam",
                        lambda u: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    monkeypatch.setattr(meh.grp, "getgrnam",
                        lambda g: types.SimpleNamespace(gr_gid=os.getgid()))
    try:
        assert meh.main(["x", p, str(dest), _BOT]) == 0
        leftovers = [q for q in (_ws(home_root) / "scripts").iterdir()
                     if q.name.startswith(".marker-embed-")]
        assert not leftovers
    finally:
        os.unlink(p)
