"""tests/test_linux_isolation_seam.py — LinuxUserIsolation (Linux port L1).

The Linux sibling of ``test_isolation_seam.py``'s layer 1: with an
injected runner (zero real subprocesses), every lifecycle method must
emit exactly the useradd / userdel / getent / usermod / chpasswd ritual
from docs/design-linux-port-2026-06-10.md §2. The argv shapes are
contract: the sudoers grants a Linux wizard renders (L3) will match on
these full-path command strings, the way the macOS grants match the
dscl dash-forms today.

Security-relevant pins:
- ``set_password`` feeds chpasswd via **stdin** — the password must never
  appear in argv (on Linux /proc exposes every process's argv pod-wide).
- ``run_as`` keeps the ``sudo -u <user> -H`` + ``cwd=/tmp`` shape — the
  uv_cwd()/EACCES contract is identical across platforms.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from evolve_admin.runtime.isolation import (
    EVOLVE_BOTS_GROUP,
    LINUX_DEFAULT_UID_START,
    Identity,
    IsolationError,
    IsolationProvider,
    LinuxUserIsolation,
)


@pytest.fixture(autouse=True)
def _no_subprocess_anywhere(monkeypatch):
    """ZERO real spawns — a real useradd/userdel here would mutate the
    host's account database."""

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"a REAL subprocess spawn was attempted in a Linux isolation test. args={a!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    monkeypatch.setattr(os, "posix_spawn", _boom)
    monkeypatch.setattr(os, "posix_spawnp", _boom)


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(responses=None):
    """Runner stub: records (argv, kwargs); answers from ``responses``
    (argv-prefix tuple → _Result; first match wins; default rc=0)."""
    calls: "list[tuple[list, dict]]" = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        for prefix, result in (responses or {}).items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return _Result(0)

    return run, calls


# ── lifecycle argv parity ─────────────────────────────────────────────────────


def test_create_user_emits_groupadd_then_useradd(monkeypatch):
    """The 2-command ritual: idempotent inventory-group creation, then a
    single useradd carrying home/uid/shell/comment/group — full paths
    (/usr-merge), bash shell (run_as + OpenClaw exec need a real shell).

    pwd.getpwnam is forced to KeyError so the W10-F #11 post-useradd home-chown
    deterministically no-ops here regardless of the host's passwd database (the
    chown path is covered by test_w10f_linux_second_tier.py)."""
    import runtime.isolation as iso_mod
    monkeypatch.setattr(iso_mod.pwd, "getpwnam",
                        lambda u: (_ for _ in ()).throw(KeyError(u)))
    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run)

    iso.create_user("botacct", 1005)

    assert [c for c, _ in calls] == [
        ["sudo", "/usr/sbin/groupadd", "-f", EVOLVE_BOTS_GROUP],
        ["sudo", "/usr/sbin/useradd", "-m", "-u", "1005", "-s", "/bin/bash",
         "-c", "botacct", "-G", EVOLVE_BOTS_GROUP, "botacct"],
    ]
    # Every step is bounded — a hung useradd must not wedge provisioning.
    assert all(kw.get("timeout") == 30 for _, kw in calls)


def test_create_user_real_name_and_no_home():
    """The service-account path: custom comment field, -M (no home) —
    useradd's CREATE_HOME default must not decide this."""
    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run)

    iso.create_user("evolve", 999, real_name="Evolve Infrastructure", create_home=False)

    useradd = [c for c, _ in calls if "/usr/sbin/useradd" in c][0]
    assert "-M" in useradd and "-m" not in useradd
    assert useradd[useradd.index("-c") + 1] == "Evolve Infrastructure"


def test_create_user_failure_raises_with_command_head_and_stderr():
    run, _calls = _recording_runner({
        ("sudo", "/usr/sbin/useradd"): _Result(9, stderr="useradd: UID 1005 is not unique\n"),
    })
    iso = LinuxUserIsolation(runner=run)

    with pytest.raises(IsolationError) as exc:
        iso.create_user("botacct", 1005)
    assert "/usr/sbin/useradd" in str(exc.value)
    assert "not unique" in str(exc.value)


def test_create_user_timeout_raises_isolation_error():
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    with pytest.raises(IsolationError) as exc:
        LinuxUserIsolation(runner=run).create_user("botacct", 1005)
    assert "timeout" in str(exc.value)


def test_delete_user_userdel_r_owns_home_removal():
    """``userdel -r`` removes the home itself — no separate ``rm -rf``
    step exists on this platform (one less privileged delete to grant)."""
    run, calls = _recording_runner()
    LinuxUserIsolation(runner=run).delete_user("botacct")
    assert [c for c, _ in calls] == [["sudo", "/usr/sbin/userdel", "-r", "botacct"]]


def test_delete_user_remove_home_false_drops_the_r_flag():
    run, calls = _recording_runner()
    LinuxUserIsolation(runner=run).delete_user("sharedacct", remove_home=False)
    assert [c for c, _ in calls] == [["sudo", "/usr/sbin/userdel", "sharedacct"]]


def test_delete_user_is_best_effort():
    """Rollback callers run this on half-created accounts — a failing
    userdel must not raise (mirrors the macOS contract)."""
    run, _calls = _recording_runner({
        ("sudo", "/usr/sbin/userdel"): _Result(6, stderr="userdel: user 'ghost' does not exist\n"),
    })
    LinuxUserIsolation(runner=run).delete_user("ghost")  # no raise


def test_set_password_uses_chpasswd_stdin_never_argv():
    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run)

    assert iso.set_password("botacct", "s3cret-hunter2") is True

    argv, kwargs = calls[0]
    assert argv == ["sudo", "/usr/sbin/chpasswd"]
    assert kwargs["input"] == "botacct:s3cret-hunter2\n"
    assert not any("hunter2" in a for a in argv), "password must NEVER be in argv"

    failing, _ = _recording_runner({("sudo",): _Result(1, stderr="nope")})
    assert LinuxUserIsolation(runner=failing).set_password("botacct", "x") is False


def test_add_to_group_usermod_aG():
    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run)

    assert iso.add_to_group("botacct", "render") is True
    assert [c for c, _ in calls] == [
        ["sudo", "/usr/sbin/usermod", "-aG", "render", "botacct"],
    ]

    failing, _ = _recording_runner({("sudo",): _Result(6, stderr="group does not exist")})
    assert LinuxUserIsolation(runner=failing).add_to_group("botacct", "ghost") is False


# ── reads ─────────────────────────────────────────────────────────────────────


def test_user_exists_probe_and_failure_modes():
    run, calls = _recording_runner({
        ("getent", "passwd", "present"): _Result(0, stdout="present:x:1001:1001::/home/present:/bin/bash\n"),
        ("getent", "passwd", "absent"): _Result(2),
    })
    iso = LinuxUserIsolation(runner=run)

    assert iso.user_exists("present") is True
    assert iso.user_exists("absent") is False
    assert calls[0][0] == ["getent", "passwd", "present"]

    def boom(cmd, **kwargs):
        raise OSError("no getent on this platform")

    assert LinuxUserIsolation(runner=boom).user_exists("anyone") is False


def test_next_free_uid_parses_getent_passwd_and_starts_at_1000():
    run, calls = _recording_runner({
        ("getent", "passwd"): _Result(0, stdout=(
            "root:x:0:0:root:/root:/bin/bash\n"
            "ubuntu:x:1000:1000:Ubuntu:/home/ubuntu:/bin/bash\n"
            "bot_a:x:1001:1001:bot_a:/home/bot_a:/bin/bash\n"
            "malformed line without colons\n"
        )),
    })
    iso = LinuxUserIsolation(runner=run)

    assert iso.used_uids() == {0, 1000, 1001}
    assert iso.next_free_uid() == 1002
    assert iso.next_free_uid(start=2000) == 2000
    assert LINUX_DEFAULT_UID_START == 1000
    assert calls[0][0] == ["getent", "passwd"]


def test_group_members_parses_getent_group():
    run, _calls = _recording_runner({
        ("getent", "group", EVOLVE_BOTS_GROUP): _Result(
            0, stdout=f"{EVOLVE_BOTS_GROUP}:x:1500:bot_a,bot_b\n"),
        ("getent", "group", "empty-group"): _Result(0, stdout="empty-group:x:1501:\n"),
        ("getent", "group", "no-such"): _Result(2),
    })
    iso = LinuxUserIsolation(runner=run)

    assert iso.group_members(EVOLVE_BOTS_GROUP) == ["bot_a", "bot_b"]
    assert iso.group_members("empty-group") == []
    assert iso.group_members("no-such") == []

    def boom(cmd, **kwargs):
        raise OSError("nope")

    assert LinuxUserIsolation(runner=boom).group_members("g") == []


# ── bot-identity resolution + run-as (identical contracts to macOS) ──────────


def test_resolve_and_home_dir_wrap_blessed_helpers():
    """resolve()/home_dir() must route through evolve_config (the blessed
    bot-identity home, dup-primitive-lint) + pwd.getpwnam — POSIX-portable,
    no /home path math."""
    import pwd as pwd_mod

    me = pwd_mod.getpwuid(os.getuid())
    network = {"bots": {"somebot": {"user": me.pw_name}}}

    iso = LinuxUserIsolation(runner=lambda cmd, **kw: _Result(0))
    assert iso.resolve("somebot", network) == Identity(
        bot_id="somebot", user=me.pw_name, uid=me.pw_uid, home=Path(me.pw_dir),
    )
    assert iso.home_dir("somebot", network) == Path(me.pw_dir)
    assert iso.resolve("ghostbot", {"bots": {"ghostbot": {"user": "no_such_acct_xyz"}}}) is None


def test_run_as_composes_sudo_dash_u_dash_H_with_tmp_cwd():
    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run)

    iso.run_as("botacct", ["/usr/bin/openclaw", "onboard"], capture_output=True)

    argv, kwargs = calls[0]
    assert argv == ["sudo", "-u", "botacct", "-H", "/usr/bin/openclaw", "onboard"]
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["capture_output"] is True


def test_linux_isolation_satisfies_the_protocol():
    assert isinstance(LinuxUserIsolation(), IsolationProvider)


# ── real-execution path: own process group + whole-group kill on timeout ─────
#
# PR #2694's linux-e2e bring-up: a timed-out `sudo useradd` had only the
# sudo WRAPPER killed (subprocess.run's timeout kill); root-owned useradd
# survived and completed in the background, leaving half-tracked account
# state. The fix: when no runner is injected, LinuxUserIsolation runs
# commands via a Popen factory with start_new_session=True and a timeout
# SIGKILLs the whole group. These tests drive that path through the
# popen_factory / kill_pgroup seams — zero real spawns, zero patching of
# subprocess attributes (the autouse guard above stays the tripwire).


class _FakePopen:
    """Popen stand-in produced by the injected factory (no real spawn)."""

    def __init__(self, cmd, *, hang: bool = False, returncode: int = 0,
                 stdout: str = "", stderr: str = ""):
        self.cmd = list(cmd)
        self.pid = 54321
        self.returncode: "int | None" = None
        self._hang = hang
        self._rc = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.communicate_kwargs: "dict | None" = None
        self.killed = False
        self.reaped = False

    def communicate(self, input=None, timeout=None):
        self.communicate_kwargs = {"input": input, "timeout": timeout}
        if self._hang:
            raise subprocess.TimeoutExpired(self.cmd, timeout or 0)
        self.returncode = self._rc
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.reaped = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


def _popen_recorder(hang_on: "tuple | None" = None):
    """Factory stub: records (argv, kwargs) and the produced procs; a proc
    whose argv starts with ``hang_on`` raises TimeoutExpired from
    communicate() the way a hung real command would."""
    calls: "list[tuple[list, dict]]" = []
    procs: "list[_FakePopen]" = []

    def factory(cmd, **kwargs):
        hang = hang_on is not None and tuple(cmd[: len(hang_on)]) == hang_on
        proc = _FakePopen(cmd, hang=hang)
        calls.append((list(cmd), kwargs))
        procs.append(proc)
        return proc

    return factory, calls, procs


def test_lifecycle_commands_run_in_their_own_process_group():
    """No runner injected → the popen path: every spawn must carry
    start_new_session=True (the precondition for a group kill), keep the
    capture_output/text plumbing, and bound each step with the timeout."""
    factory, calls, procs = _popen_recorder()
    iso = LinuxUserIsolation(popen_factory=factory, kill_pgroup=lambda pid: None)

    iso.create_user("botacct", 1005)

    assert [c for c, _ in calls] == [
        ["sudo", "/usr/sbin/groupadd", "-f", EVOLVE_BOTS_GROUP],
        ["sudo", "/usr/sbin/useradd", "-m", "-u", "1005", "-s", "/bin/bash",
         "-c", "botacct", "-G", EVOLVE_BOTS_GROUP, "botacct"],
    ]
    assert all(kw.get("start_new_session") is True for _, kw in calls)
    assert all(kw.get("stdout") == subprocess.PIPE for _, kw in calls)
    assert all(kw.get("stderr") == subprocess.PIPE for _, kw in calls)
    assert all(kw.get("text") is True for _, kw in calls)
    # timeout is consumed by communicate(), never handed to Popen.
    assert all("timeout" not in kw for _, kw in calls)
    assert all(p.communicate_kwargs == {"input": None, "timeout": 30} for p in procs)


def test_useradd_timeout_kills_the_whole_pgroup_and_raises_isolation_error():
    """The incident shape: useradd hangs past its timeout. The WHOLE
    process group must get the kill (not just the sudo wrapper), the
    wrapper must be reaped (no zombie), and create_user must raise
    IsolationError exactly as before."""
    useradd_head = ("sudo", "/usr/sbin/useradd")
    factory, _calls, procs = _popen_recorder(hang_on=useradd_head)
    killed_groups: "list[int]" = []
    iso = LinuxUserIsolation(popen_factory=factory, kill_pgroup=killed_groups.append)

    with pytest.raises(IsolationError) as exc:
        iso.create_user("botacct", 1005)
    assert "timeout" in str(exc.value)

    hung = procs[-1]
    assert hung.cmd[:2] == list(useradd_head)
    assert killed_groups == [hung.pid], "group kill must target the hung command"
    assert hung.killed and hung.reaped, "direct child must be killed + reaped"
    assert procs[0].killed is False, "the succeeded groupadd must not be killed"


def test_delete_user_timeout_group_kills_and_propagates_timeout_expired():
    """delete_user has no timeout handler today (TimeoutExpired propagates
    to the caller) — the pgroup path must preserve that contract while
    still killing the whole group."""
    factory, _calls, procs = _popen_recorder(hang_on=("sudo", "/usr/sbin/userdel"))
    killed_groups: "list[int]" = []
    iso = LinuxUserIsolation(popen_factory=factory, kill_pgroup=killed_groups.append)

    with pytest.raises(subprocess.TimeoutExpired):
        iso.delete_user("botacct")
    assert killed_groups == [procs[0].pid]
    assert procs[0].reaped


def test_set_password_popen_path_feeds_chpasswd_stdin():
    """The stdin-never-argv contract must survive the popen path: input
    flows through communicate(), with a PIPE stdin wired up."""
    factory, calls, procs = _popen_recorder()
    iso = LinuxUserIsolation(popen_factory=factory, kill_pgroup=lambda pid: None)

    assert iso.set_password("botacct", "s3cret-hunter2") is True

    argv, kwargs = calls[0]
    assert argv == ["sudo", "/usr/sbin/chpasswd"]
    assert kwargs.get("stdin") == subprocess.PIPE
    assert procs[0].communicate_kwargs is not None
    assert procs[0].communicate_kwargs["input"] == "botacct:s3cret-hunter2\n"
    assert not any("hunter2" in a for a in argv), "password must NEVER be in argv"


def test_injected_runner_takes_precedence_over_the_popen_path():
    """Every existing fake injects a runner — the popen/pgroup machinery
    must never engage when one is set."""

    def _boom_factory(cmd, **kwargs):  # pragma: no cover — exists to fail loudly
        raise AssertionError("popen path must not engage when a runner is injected")

    run, calls = _recording_runner()
    iso = LinuxUserIsolation(runner=run, popen_factory=_boom_factory)

    iso.create_user("botacct", 1005)
    assert len(calls) == 2


def test_default_pgroup_killer_is_best_effort(monkeypatch):
    """The default kill helper SIGKILLs pid's group and swallows exactly
    the two no-longer-actionable failures: group already gone
    (ProcessLookupError) and nothing signalable by this euid
    (PermissionError — sudo's root child when running unprivileged)."""
    import signal

    from runtime.isolation import _kill_process_group

    sent: "list[tuple[int, int]]" = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    _kill_process_group(4242)
    assert sent == [(4243, signal.SIGKILL)]

    def _eperm(pgid, sig):
        raise PermissionError("root-owned member")

    monkeypatch.setattr(os, "killpg", _eperm)
    _kill_process_group(4242)  # no raise

    def _esrch(pid):
        raise ProcessLookupError("group already gone")

    monkeypatch.setattr(os, "getpgid", _esrch)
    _kill_process_group(4242)  # no raise
