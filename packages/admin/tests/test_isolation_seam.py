"""tests/test_isolation_seam.py — the IsolationProvider seam (4.3 C, Track I).

Three layers:

1. ``MacOSIsolation`` argv parity — with an injected runner (zero real
   subprocesses), every lifecycle method must emit exactly the dscl /
   createhomedir ritual the call sites ran inline before the seam. The
   create/delete shapes are sudoers-grant-matched (full paths, dash-form
   verbs) — see test_provision_pipeline_sudoers.py — so argv drift here
   would break the evolve-user wizard path in production.

2. ``FakeIsolation`` semantics — the in-memory user table tests inject
   via ``set_isolation``.

3. The swappability proof (I3): the provisioning pipeline's user
   lifecycle runs against ``FakeIsolation`` with ZERO dscl /
   sysadminctl / createhomedir subprocesses spawned, including the
   rollback path (which exercises the same ``delete_user`` primitive
   retire.delete_bot migrated onto).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.runtime.isolation import (  # noqa: E402
    DEFAULT_UID_START,
    FakeIsolation,
    Identity,
    IsolationError,
    IsolationProvider,
    LinuxUserIsolation,
    MacOSIsolation,
    get_isolation,
    set_isolation,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(responses=None):
    """Runner stub: records (argv, kwargs); answers from ``responses``.

    ``responses`` maps an argv-prefix tuple to a _Result. First matching
    prefix wins; default is rc=0.
    """
    calls: list[tuple[list, dict]] = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        for prefix, result in (responses or {}).items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return _Result(0)

    return run, calls


@pytest.fixture
def fake_isolation():
    """Inject a FakeIsolation singleton; always reset afterwards."""
    fake = FakeIsolation()
    set_isolation(fake)
    try:
        yield fake
    finally:
        set_isolation(None)


# ── 1. MacOSIsolation argv parity (injected runner, no subprocess) ──────────


def test_create_user_emits_exact_dscl_ritual():
    """The 7-command ritual, byte-for-byte: full sudo paths + dash-form
    verbs (the shapes the sudoers grants match) and createhomedir last."""
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.create_user("botacct", 510)

    assert [c for c, _ in calls] == [
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct"],
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct", "UserShell", "/bin/zsh"],
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct", "RealName", "botacct"],
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct", "UniqueID", "510"],
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct", "PrimaryGroupID", "20"],
        ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/botacct", "NFSHomeDirectory", "/Users/botacct"],
        ["sudo", "/usr/sbin/createhomedir", "-c", "-u", "botacct"],
    ]
    # Every step is bounded — a hung dscl must not wedge provisioning.
    assert all(kw.get("timeout") == 30 for _, kw in calls)


def test_create_user_real_name_and_no_home():
    """cli.py's `setup evolve-user` path: custom RealName, no createhomedir."""
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.create_user("evolve", 500, real_name="Evolve Infrastructure", create_home=False)

    argvs = [c for c, _ in calls]
    assert ["sudo", "/usr/bin/dscl", ".", "-create", "/Users/evolve",
            "RealName", "Evolve Infrastructure"] in argvs
    assert not any("createhomedir" in " ".join(c) for c in argvs)


def test_create_user_failure_raises_with_command_head_and_stderr():
    """ProvisionError messages are built from str(IsolationError) — the
    failing command head + stderr must survive the seam crossing."""
    run, _calls = _recording_runner({
        ("sudo", "/usr/bin/dscl"): _Result(1, stderr="eDSPermissionError\n"),
    })
    iso = MacOSIsolation(runner=run)

    with pytest.raises(IsolationError) as exc:
        iso.create_user("botacct", 510)
    assert "/usr/bin/dscl . -create" in str(exc.value)
    assert "eDSPermissionError" in str(exc.value)


def test_create_user_timeout_raises_isolation_error():
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    iso = MacOSIsolation(runner=run)
    with pytest.raises(IsolationError) as exc:
        iso.create_user("botacct", 510)
    assert "timeout" in str(exc.value)


def test_delete_user_emits_dscl_delete_then_rm():
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.delete_user("botacct")

    assert [c for c, _ in calls] == [
        ["sudo", "/usr/bin/dscl", ".", "-delete", "/Users/botacct"],
        ["sudo", "/bin/rm", "-rf", "/Users/botacct"],
    ]


def test_delete_user_remove_home_false_skips_rm():
    """Shared-account bots: delete the record, never the human's home."""
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.delete_user("sharedacct", remove_home=False)

    argvs = [c for c, _ in calls]
    assert argvs == [["sudo", "/usr/bin/dscl", ".", "-delete", "/Users/sharedacct"]]


def test_delete_user_guard_rail_refuses_short_names():
    """Defense-in-depth: never `rm -rf` a path shorter than /Users/<3+chars>."""
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.delete_user("ab")  # 2 chars — dscl-delete OK, rm must be refused

    argvs = [c for c, _ in calls]
    assert not any("/bin/rm" in c for c in argvs), argvs


def test_user_exists_probe_and_failure_modes():
    run, calls = _recording_runner({
        ("dscl", ".", "-read", "/Users/present"): _Result(0),
        ("dscl", ".", "-read", "/Users/absent"): _Result(56),
    })
    iso = MacOSIsolation(runner=run)

    assert iso.user_exists("present") is True
    assert iso.user_exists("absent") is False
    assert calls[0][0] == ["dscl", ".", "-read", "/Users/present", "UniqueID"]

    def boom(cmd, **kwargs):
        raise OSError("no dscl on this platform")

    assert MacOSIsolation(runner=boom).user_exists("anyone") is False


def test_next_free_uid_parses_dscl_list():
    run, calls = _recording_runner({
        ("dscl", ".", "-list"): _Result(0, stdout="root 0\nfoo 502\nbar 503\n"),
    })
    iso = MacOSIsolation(runner=run)

    assert iso.next_free_uid() == 504
    assert iso.next_free_uid(start=600) == 600
    assert calls[0][0] == ["dscl", ".", "-list", "/Users", "UniqueID"]
    assert iso.used_uids() == {0, 502, 503}


def test_group_members_parses_membership_line():
    run, _calls = _recording_runner({
        ("dscl", ".", "-read", "/Groups/admin"): _Result(
            0, stdout="GroupMembership: root someadmin\n"),
    })
    iso = MacOSIsolation(runner=run)
    assert iso.group_members("admin") == ["root", "someadmin"]

    def boom(cmd, **kwargs):
        raise OSError("nope")

    assert MacOSIsolation(runner=boom).group_members("admin") == []


def test_set_password_and_add_to_group_argv():
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    assert iso.set_password("evolve", "s3cret") is True
    assert iso.add_to_group("evolve", "wheel") is True
    assert [c for c, _ in calls] == [
        ["sudo", "/usr/bin/dscl", ".", "-passwd", "/Users/evolve", "s3cret"],
        ["sudo", "/usr/bin/dscl", ".", "-append", "/Groups/wheel", "GroupMembership", "evolve"],
    ]

    failing, _ = _recording_runner({("sudo",): _Result(1, stderr="nope")})
    iso2 = MacOSIsolation(runner=failing)
    assert iso2.set_password("evolve", "x") is False
    assert iso2.add_to_group("evolve", "wheel") is False


def test_run_as_composes_sudo_dash_u_dash_H_with_tmp_cwd():
    """`sudo -u <user> -H` + cwd=/tmp is the uv_cwd()-safe invocation shape
    (CLAUDE.md §SSH) — provisioning's openclaw-onboard call depends on it."""
    run, calls = _recording_runner()
    iso = MacOSIsolation(runner=run)

    iso.run_as("botacct", ["/usr/local/bin/openclaw", "onboard"], capture_output=True)

    argv, kwargs = calls[0]
    assert argv == ["sudo", "-u", "botacct", "-H", "/usr/local/bin/openclaw", "onboard"]
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["capture_output"] is True


def test_resolve_and_home_dir_wrap_blessed_helpers(monkeypatch):
    """resolve()/home_dir() must route through evolve_config (the blessed
    bot-identity home — dup-primitive-lint) rather than re-deriving paths."""
    import pwd as pwd_mod
    import os

    me = pwd_mod.getpwuid(os.getuid())
    network = {"bots": {"somebot": {"user": me.pw_name}}}

    iso = MacOSIsolation(runner=lambda cmd, **kw: _Result(0))
    ident = iso.resolve("somebot", network)
    assert ident is not None
    assert ident == Identity(
        bot_id="somebot", user=me.pw_name, uid=me.pw_uid, home=Path(me.pw_dir),
    )
    # blessed bot_home: pwd lookup for an existing account
    assert iso.home_dir("somebot", network) == Path(me.pw_dir)

    # Nonexistent account → resolve None; home_dir falls back to /Users/<user>
    network2 = {"bots": {"ghostbot": {"user": "no_such_acct_xyz"}}}
    assert iso.resolve("ghostbot", network2) is None
    assert iso.home_dir("ghostbot", network2) == Path("/Users/no_such_acct_xyz")


# ── 2. FakeIsolation semantics ───────────────────────────────────────────────


def test_fake_isolation_lifecycle_round_trip():
    fake = FakeIsolation()
    assert not fake.user_exists("botacct")

    uid = fake.next_free_uid()
    assert uid == DEFAULT_UID_START
    fake.create_user("botacct", uid)
    assert fake.user_exists("botacct")
    assert fake.used_uids() == {uid}
    assert fake.next_free_uid() == uid + 1

    # create on an existing account raises like the real ritual would fail
    with pytest.raises(IsolationError):
        fake.create_user("botacct", uid + 1)

    ident = fake.resolve("botacct")
    assert ident == Identity("botacct", "botacct", uid, Path("/Users/botacct"))

    fake.delete_user("botacct")
    assert not fake.user_exists("botacct")
    assert fake.resolve("botacct") is None
    assert ("delete_user", "botacct", True) in fake.calls


def test_fake_isolation_bot_user_mapping_and_run_as():
    """bot_id ≠ account name must be representable in the fake too."""
    fake = FakeIsolation()
    fake.seed_user("shared_acct", 505)
    fake.seed_bot_user("team_bot", "shared_acct")

    ident = fake.resolve("team_bot")
    assert ident is not None and ident.user == "shared_acct" and ident.uid == 505
    # network dict override takes precedence over the seeded map
    ident2 = fake.resolve("team_bot", {"bots": {"team_bot": {"user": "shared_acct"}}})
    assert ident2 is not None and ident2.user == "shared_acct"
    assert fake.home_dir("team_bot") == Path("/Users/shared_acct")

    proc = fake.run_as("shared_acct", ["echo", "hi"])
    assert proc.returncode == 0
    assert ("run_as", "shared_acct", ["echo", "hi"]) in fake.calls


def test_fake_and_macos_satisfy_the_protocol():
    assert isinstance(FakeIsolation(), IsolationProvider)
    assert isinstance(MacOSIsolation(), IsolationProvider)


def test_singleton_injection_and_reset():
    set_isolation(None)
    try:
        default = get_isolation()
        assert isinstance(default, MacOSIsolation)
        fake = FakeIsolation()
        set_isolation(fake)
        assert get_isolation() is fake
    finally:
        set_isolation(None)
    assert isinstance(get_isolation(), MacOSIsolation)
    set_isolation(None)


def test_get_isolation_default_keys_off_platform_profile():
    """The COLD default is profile-keyed: Linux → LinuxUserIsolation/getent,
    macOS → MacOSIsolation. Regression for the Linux/VPS pod's false 'macOS
    user not found' Pod Health failures — the admin-ui daemon never runs the
    install-time platform gate that pins ``LinuxUserIsolation``, so every cold
    ``get_isolation()`` caller fell through to ``MacOSIsolation`` and shelled
    ``dscl`` (absent on Linux), reporting existing accounts as missing.

    The admin suite's autouse fixture pins MACOS, so this test flips to LINUX
    explicitly inside the body and resets BOTH the profile and the isolation
    singleton in teardown so no state leaks to later tests.
    """
    from platform_profile import LINUX, MACOS, set_profile

    try:
        # Linux profile → LinuxUserIsolation, and its probe is getent (NOT dscl).
        set_profile(LINUX)
        set_isolation(None)
        iso = get_isolation()
        assert isinstance(iso, LinuxUserIsolation)

        calls: list[list] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _Result(0)

        # Mock the singleton's _run so user_exists doesn't spawn a real getent.
        iso._run = fake_run  # type: ignore[method-assign]
        assert iso.user_exists("evolve") is True
        assert calls == [["getent", "passwd", "evolve"]]

        # macOS profile → MacOSIsolation, byte-identical to the historical default.
        set_profile(MACOS)
        set_isolation(None)
        assert isinstance(get_isolation(), MacOSIsolation)
    finally:
        set_isolation(None)
        # Restore MACOS (the value the autouse fixture's teardown expects), not
        # None — matching the per-test pin contract in conftest.
        set_profile(MACOS)


# ── 3. I3 proof: provisioning lifecycle on FakeIsolation, zero dscl ─────────


def _forbidding_subprocess_recorder():
    """subprocess.run stand-in: rc=0 for everything, but HARD-FAILS the test
    if any isolation-lifecycle binary is invoked — the whole point of the
    seam is that these never reach subprocess in a FakeIsolation run."""
    seen: list[list[str]] = []
    forbidden = ("dscl", "sysadminctl", "createhomedir")

    def run(cmd, **kwargs):
        argv = [str(a) for a in cmd]
        seen.append(argv)
        joined = " ".join(argv)
        assert not any(tool in joined for tool in forbidden), (
            f"isolation lifecycle leaked past the seam: {argv}"
        )
        return _Result(0)

    return run, seen


def test_provision_pipeline_creates_user_via_seam_no_dscl(
    fake_isolation, tmp_path, monkeypatch,
):
    """provision_bot's user stage runs against FakeIsolation: the macOS
    account 'exists' afterwards, and no dscl/sysadminctl/createhomedir
    subprocess was spawned anywhere in the pipeline."""
    import json
    from evolve_admin import provisioning

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}, "members": []}))

    guard, seen = _forbidding_subprocess_recorder()
    monkeypatch.setattr(provisioning.subprocess, "run", guard)
    # .openclaw pre-creation hits the create branch (no real /Users/<bot>),
    # whose mkdir/chown/chmod go through the guarded stub above.

    result = provisioning.provision_bot(
        "freshbot", port=19077,
        network_path=network_path,
        skip_onboard=True, skip_add_bot=True, skip_deploy=True,
        auth_choice="anthropic",
    )

    assert result.success, (result.error, result.failed_stage)
    assert result.created_user is True
    assert fake_isolation.user_exists("freshbot")
    assert ("create_user", "freshbot", result.uid) in fake_isolation.calls
    # The guard would have failed on contact; belt-and-braces re-assert:
    assert not any("dscl" in " ".join(c) for c in seen)


def test_provision_rollback_deletes_user_via_seam(
    fake_isolation, tmp_path, monkeypatch,
):
    """A mid-pipeline failure must tear the created account back down
    through the seam (the same delete_user primitive retire.delete_bot
    uses) — again with zero lifecycle subprocesses."""
    import json
    from evolve_admin import provisioning
    from evolve_admin.provisioning import ProvisionError, STAGE_CREATE_OC_DIR

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}, "members": []}))

    guard, _seen = _forbidding_subprocess_recorder()
    monkeypatch.setattr(provisioning.subprocess, "run", guard)

    def explode(**kwargs):
        raise ProvisionError(STAGE_CREATE_OC_DIR, "simulated mkdir failure")

    monkeypatch.setattr(provisioning, "_create_openclaw_dir", explode)

    result = provisioning.provision_bot(
        "doomedbot", port=19078,
        network_path=network_path,
        skip_onboard=True, skip_add_bot=True, skip_deploy=True,
        auth_choice="anthropic",
    )

    assert result.success is False
    assert result.failed_stage == STAGE_CREATE_OC_DIR
    # User was created, then rolled back — table is empty again.
    assert ("create_user", "doomedbot", result.uid) in fake_isolation.calls
    assert ("delete_user", "doomedbot", True) in fake_isolation.calls
    assert not fake_isolation.user_exists("doomedbot")
    assert any("deleted macOS user doomedbot" in s for s in result.rollback_log)
