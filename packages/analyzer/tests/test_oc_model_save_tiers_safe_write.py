"""tests/test_oc_model_save_tiers_safe_write.py — evolve-tiers.json writes
go through the ownership-robust safe-write pattern, never a naive in-place
``open("w")``.

Incident (2026-06-10): clicking "Reconcile catalog for team-bot-a" on the
AI-Optimization page died with::

    Reconcile failed: reconcile write failed for team-bot-a:
    [Errno 13] Permission denied: '/Users/team_bot_a/.openclaw/evolve-tiers.json'

Root cause: ``oc_model._save_tiers_file`` truncated the live file in place
via ``path.open("w")``, which needs write permission on the *file itself*.
When the live evolve-tiers.json was owned by a different uid than the
running process (root-owned after a migration; or bot-owned with the admin
server running as the evolve user), the write hit EACCES.

Locked here:
  1. Happy path: ``os.replace`` (atomic temp-in-dir + rename) is used, so
     the write needs only *directory* write permission — ownership-robust.
  2. Fallback path: on ``PermissionError`` from the atomic write, the
     helper stages in ``/tmp`` and shells out to ``sudo /bin/cp`` — the
     CLAUDE.md sanctioned path for the evolve user to land a bot-owned
     file. It NEVER does ``sudo -u <bot>`` (no such grant exists).
  3. The naive ``open(path, "w")`` truncate-in-place is gone.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_save_tiers_writes_atomically(fake_home):
    """Happy path lands the JSON content; no leftover temp file of any name."""
    oc_model._save_tiers_file("team_bot_a", {"cascade": {"enabled": True}})
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    assert json.loads(tiers_path.read_text()) == {"cascade": {"enabled": True}}
    # The atomic temp-in-dir file must have been renamed away, not left behind.
    # Checked over the WHOLE directory, not just ``*.tmp`` — the staging name is
    # randomised now (#3566 audit D-1), so a suffix-scoped glob would be blind.
    leftovers = [p.name for p in (fake_home / ".openclaw").iterdir()]
    assert leftovers == ["evolve-tiers.json"], f"stray temp files: {leftovers}"


def test_save_tiers_uses_atomic_replace_not_inplace_truncate(fake_home, monkeypatch):
    """The writer must use ``os.replace`` (rename), never ``Path.open('w')``
    truncate-in-place on the live file — that is the EACCES bug.

    We assert by tripping a sentinel: if anything calls ``open`` in write
    mode on the *live* path, fail. The atomic helper only opens the temp.
    """
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    # Seed an existing file so an in-place truncate WOULD be attempted by the
    # naive implementation.
    tiers_path.write_text(json.dumps({"old": True}))

    real_open = Path.open

    def guard_open(self, mode="r", *a, **k):
        if "w" in mode and self == tiers_path:
            raise AssertionError(
                "in-place truncate of live evolve-tiers.json — must use "
                "os.replace on a temp file instead"
            )
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", guard_open)
    oc_model._save_tiers_file("team_bot_a", {"new": True})
    assert json.loads(tiers_path.read_text()) == {"new": True}


def test_save_tiers_falls_back_to_sudo_cp_on_permission_error(fake_home, monkeypatch):
    """When the atomic write raises PermissionError (evolve user can't write
    the bot's home), fall back to /tmp staging + ``sudo /bin/cp``. Assert the
    exact command shape and that we never invoke ``sudo -u <bot>``."""
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"

    # Force the in-dir atomic path to fail with PermissionError so the
    # fallback engages.
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)

    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        # Emulate sudo /bin/cp by actually copying so the assertion below
        # can verify the content landed.
        src, dst = cmd[2], cmd[3]
        Path(dst).write_text(Path(src).read_text())

        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)

    oc_model._save_tiers_file("team_bot_a", {"viafallback": True})

    cmd = captured["cmd"]
    assert cmd[:2] == ["sudo", "/bin/cp"], cmd
    assert cmd[2].startswith("/tmp/evolve-tiers-"), cmd
    assert cmd[3] == str(tiers_path), cmd
    # Must NOT be a `sudo -u <bot>` invocation — evolve has no such grant.
    assert "-u" not in cmd, cmd
    assert json.loads(tiers_path.read_text()) == {"viafallback": True}


def test_save_tiers_raises_when_sudo_cp_fails(fake_home, monkeypatch):
    """A failed ``sudo /bin/cp`` surfaces as PermissionError (so the route
    returns a real error, not a silent no-op)."""
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)

    def failing_run(cmd, *a, **k):
        class R:
            returncode = 1
            stderr = "cp: permission denied"
            stdout = ""
        return R()

    monkeypatch.setattr(oc_model.subprocess, "run", failing_run)

    with pytest.raises(PermissionError):
        oc_model._save_tiers_file("team_bot_a", {"x": 1})


def test_save_tiers_chowns_back_to_bot_after_sudo_cp(fake_home, monkeypatch):
    """After the ``sudo /bin/cp`` fallback, the writer must chown the dest back
    to the bot + chmod 644. A bare cp to a *fresh* dest lands it root:wheel
    0600, which locks the bot out of reading its OWN tier config (the
    2026-06-16 fleet repo-puller heal failure). Wiring is asserted by spying on
    ``_restore_tiers_bot_ownership``."""
    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )

    restored: list = []
    monkeypatch.setattr(oc_model, "_restore_tiers_bot_ownership", lambda p: restored.append(p))

    oc_model._save_tiers_file("team_bot_a", {"x": 1})
    tiers_path = fake_home / ".openclaw" / "evolve-tiers.json"
    assert restored == [tiers_path], restored


def test_restore_tiers_bot_ownership_noop_for_unrecognised_path(monkeypatch):
    """A path that is not ``<home_root>/<user>/.openclaw/<file>`` (a bare test
    tmpdir) must not shell out — the repair would chown an unrelated file."""
    called = False

    def fake_run(*a, **k):
        nonlocal called
        called = True
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)
    oc_model._restore_tiers_bot_ownership(Path("/tmp/evolve-tiers-x.json"))
    assert not called


# ── #3566 audit B-1 / D-1 / D-2 ──────────────────────────────────────────────
#
# These three fixes ship together, and the ORDER matters. ``_restore_tiers_bot_
# ownership`` is the root-privileged repair that runs after the ``sudo /bin/cp``
# fallback. B-1 is that it is DEAD on Linux (a ``/Users``-hardcoded home-root
# guard, plus a macOS-hardcoded chown binary that the Linux sudoers grant does
# not cover). D-2 is that the cp — and that same repair — FOLLOW a symlink at
# the destination and act on its target as root. So B-1's bugs are currently
# the only thing keeping D-2's escalation-grade step from firing on the Linux
# pod: landing B-1 alone would ARM it. The tests below pin both halves.


def _pin_profile_rooted_at(monkeypatch, base: Path, linux: bool):
    """Pin a ``PlatformProfile`` whose ``user_home_root`` is ``base``.

    Two deliberate choices:

    * the root is a REAL tmp directory, so the symlink gate has something to
      lstat — an unwritable ``/Users`` or ``/home`` path only exists on one of
      the two pod platforms, and neither is writable by the test;
    * the patch lands on ``platform_profile.get_profile``, a symbol that
      exists on BOTH origin/main and this branch. Patching this module's own
      ``_get_platform_profile`` wrapper would make every B-1 test red on
      origin/main with ``AttributeError`` — green-by-construction, proving
      nothing about the behaviour. ``oc_model`` imports ``get_profile`` inside
      the function, so the patched attribute is picked up per call.
    """
    import dataclasses

    import platform_profile

    src = platform_profile.LINUX if linux else platform_profile.MACOS
    pinned = dataclasses.replace(src, user_home_root=str(base))
    monkeypatch.setattr(platform_profile, "get_profile", lambda *a, **k: pinned)
    return pinned


@pytest.mark.parametrize(
    "linux,expected_chown",
    [(True, "/usr/bin/chown"), (False, "/usr/sbin/chown")],
    ids=["linux", "macos"],
)
def test_restore_tiers_bot_ownership_runs_per_platform(
    tmp_path, monkeypatch, linux, expected_chown,
):
    """B-1: the repair must reach a Linux home AND name the profile's chown.

    Against origin/main the Linux case issues ZERO sudo calls — the guard
    ``parts[1] != "Users"`` early-returns for every ``/home/<bot>`` path — and
    even on macOS the binary is hardcoded to ``/usr/sbin/chown``, which is not
    what ``_render_evolve_sudoers`` grants on Linux
    (``{profile.chown} * {profile.user_home_root}/*/.openclaw/evolve-tiers.json``,
    i.e. ``/usr/bin/chown`` there). An ungranted binary makes sudo fall through
    to a password prompt, which the TTY-less admin daemon cannot answer.
    """
    oc_dir = tmp_path / "team_bot_a" / ".openclaw"
    oc_dir.mkdir(parents=True)
    dest = oc_dir / "evolve-tiers.json"
    dest.write_text("{}")

    _pin_profile_rooted_at(monkeypatch, tmp_path, linux)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: (calls.append(list(cmd)),
                              type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1],
    )

    oc_model._restore_tiers_bot_ownership(dest)

    assert ["sudo", expected_chown, "team_bot_a:staff", str(dest)] in calls, calls
    # 644, never 600 — tiers are model-routing config, not a secret.
    assert ["sudo", "/bin/chmod", "644", str(dest)] in calls, calls
    assert not any(c[:3] == ["sudo", "/bin/chmod", "600"] for c in calls), calls


def test_restore_tiers_bot_ownership_refuses_symlinked_dest(tmp_path, monkeypatch):
    """D-2 (repair leg): a symlinked destination issues no root chown/chmod.

    ``chown``/``chmod`` without ``-h`` follow a symlink argument, so the
    pre-fix repair would have relabelled the LINK'S TARGET as root. Honest note
    on the ratchet: this case would be vacuously green on origin/main's
    BEHAVIOUR (B-1 keeps the whole function dead for any non-``/Users`` root),
    so it is the sibling parametrised test above that carries the behavioural
    red. That vacuity is the ordering trap itself — fixing B-1 alone is what
    arms this step on the Linux pod.
    """
    oc_dir = tmp_path / "team_bot_a" / ".openclaw"
    oc_dir.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text('{"victim": true}')
    dest = oc_dir / "evolve-tiers.json"
    dest.symlink_to(victim)

    _pin_profile_rooted_at(monkeypatch, tmp_path, True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: (calls.append(list(cmd)),
                              type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1],
    )

    oc_model._restore_tiers_bot_ownership(dest)
    assert calls == [], f"root chown/chmod issued through a symlink: {calls}"


@pytest.mark.parametrize(
    "relpath",
    [
        ".openclaw/evolve-tiers.json",          # missing the <user> component
        "team_bot_a/.openclaw/sub/tiers.json",  # nested below .openclaw
        "bad:user/.openclaw/evolve-tiers.json",  # ":" would re-split the chown arg
    ],
    ids=["no-user", "nested", "unsafe-name"],
)
def test_tiers_bot_user_from_path_rejects_off_shape_paths(tmp_path, monkeypatch, relpath):
    """The account name lands in a sudo argv, so the path shape is exact.

    ``<home_root>/<user>/.openclaw/<file>`` and nothing else: a looser match
    would hand back ".openclaw" as the account for a short path, aim the repair
    at a nested file, or interpolate a ``:``-bearing name into
    ``chown <user>:staff``.
    """
    _pin_profile_rooted_at(monkeypatch, tmp_path, True)
    assert oc_model._tiers_bot_user_from_path(tmp_path / relpath) is None


def test_save_tiers_sudo_fallback_refuses_symlinked_dest(fake_home, monkeypatch):
    """D-2 (write leg): ``sudo /bin/cp`` must not be issued for a symlinked dest.

    ``cp`` has no flag that refuses to follow a destination symlink, and this
    cp runs as ROOT — so an unchecked dest is a root-write primitive. The
    sudoers path pin does not help: sudo matches the literal argv, and the argv
    is the legitimate-looking link path. Reproduced against the real function on
    origin/main: the victim's content was overwritten and its mode went
    0600 → 0644.
    """
    oc_dir = fake_home / ".openclaw"
    victim = fake_home / "victim.json"
    victim.write_text('{"victim": true}')
    victim.chmod(0o600)
    dest = oc_dir / "evolve-tiers.json"
    dest.symlink_to(victim)

    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        # Emulate what real `cp` does: write THROUGH the destination symlink.
        calls.append(list(cmd))
        Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)

    with pytest.raises(PermissionError, match="SYMLINK"):
        oc_model._save_tiers_file("team_bot_a", {"attacker": True})

    assert calls == [], f"root cp issued through a symlink: {calls}"
    assert json.loads(victim.read_text()) == {"victim": True}
    assert oct(victim.stat().st_mode)[-3:] == "600"


def test_save_tiers_direct_write_ignores_preplanted_sidecar_symlink(fake_home):
    """D-1: the staging name must not be derivable from the destination.

    origin/main staged at the DETERMINISTIC ``<dest>.tmp`` via
    ``Path.write_text`` — plain ``open(..., "w")``, no ``O_EXCL``, no
    ``O_NOFOLLOW`` — so a symlink pre-planted at that exact name was followed
    and the payload landed on the link's target. No race to win: the name is
    fixed, so the plant can sit there indefinitely. ``tempfile.mkstemp`` (via
    ``evolve_util.atomic_write_json``) uses a random name with
    ``O_CREAT|O_EXCL|O_NOFOLLOW``.
    """
    oc_dir = fake_home / ".openclaw"
    victim = fake_home / "victim.json"
    victim.write_text('{"victim": true}')
    (oc_dir / "evolve-tiers.json.tmp").symlink_to(victim)

    oc_model._save_tiers_file("team_bot_a", {"attacker": True})

    assert json.loads(victim.read_text()) == {"victim": True}
    dest = oc_dir / "evolve-tiers.json"
    assert not dest.is_symlink()
    assert json.loads(dest.read_text()) == {"attacker": True}
    # 0644 — the bot must be able to read its own routing config. mkstemp
    # defaults to 0600, so the mode is pinned rather than inherited.
    assert oct(dest.stat().st_mode)[-3:] == "644"


def test_save_tiers_pins_0644_regardless_of_umask(fake_home):
    """D-1 mode pin: ``mkstemp`` creates 0600 and honours no umask, so the mode
    must be set explicitly or the bot loses read access to its own routing
    config (the 2026-06-16 fleet repo-puller heal failure).

    Run under umask 077 so the ratchet is behavioural rather than incidental:
    origin/main's ``Path.write_text`` produces 0644 under CI's default umask
    022 by luck, and 0600 under 077. Measured on both trees.
    """
    old = os.umask(0o077)
    try:
        oc_model._save_tiers_file("team_bot_a", {"a": 1})
    finally:
        os.umask(old)
    dest = fake_home / ".openclaw" / "evolve-tiers.json"
    assert oct(dest.stat().st_mode)[-3:] == "644"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory mode bits, so lstat never raises EACCES",
)
def test_save_tiers_sudo_fallback_refuses_unverifiable_dest(fake_home, monkeypatch):
    """Deliberate behaviour change: an UNVERIFIABLE dest fails closed.

    When ``.openclaw`` is not traversable by the running user — the 0700 clamp
    that ``secret_config_perms._reassert_evolve_read_acl`` heals — the gate
    cannot lstat the destination. origin/main issued the root ``cp`` anyway.
    We refuse, with a message distinct from the symlink one so an operator can
    tell the two apart. Pinned here so the trade-off is a decision, not drift.
    """
    oc_dir = fake_home / ".openclaw"

    def boom_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(oc_model.os, "replace", boom_replace)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        oc_model.subprocess, "run",
        lambda cmd, *a, **k: (calls.append(list(cmd)),
                              type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1],
    )
    oc_dir.chmod(0o000)
    try:
        with pytest.raises(PermissionError, match="cannot verify"):
            oc_model._save_tiers_file("team_bot_a", {"x": 1})
    finally:
        oc_dir.chmod(0o700)
    assert calls == [], f"root cp issued against an unverifiable dest: {calls}"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory mode bits, so mkstemp never raises EACCES",
)
def test_save_tiers_unwritable_dir_still_reaches_sudo_fallback(fake_home, monkeypatch):
    """The direct → sudo control flow is unchanged by the D-1 rewrite.

    Not a stubbed ``os.replace``: the ``.openclaw`` directory is made genuinely
    unwritable, so ``mkstemp`` itself must raise ``PermissionError`` for the
    fallback to engage (verified on POSIX in #3574). Asserted by the sudo ARGV
    actually being issued — a bare ``pytest.raises(Exception)`` would pass even
    if the fallback were bypassed entirely.
    """
    oc_dir = fake_home / ".openclaw"
    oc_dir.chmod(0o500)
    # Patched on the oc_model namespace, not evolve_util's: the module does a
    # `from evolve_util import assert_safe_sudo_dest`, so the call site resolves
    # oc_model's own binding.
    monkeypatch.setattr(oc_model, "assert_safe_sudo_dest", lambda p: None)

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(oc_model.subprocess, "run", fake_run)
    try:
        oc_model._save_tiers_file("team_bot_a", {"viafallback": True})
    finally:
        oc_dir.chmod(0o700)

    assert calls, "sudo fallback never fired — mkstemp did not raise PermissionError"
    assert calls[0][:2] == ["sudo", "/bin/cp"], calls
    assert calls[0][2].startswith("/tmp/evolve-tiers-"), calls
    assert "-u" not in calls[0], calls
