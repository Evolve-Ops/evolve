"""Integration tests for the unix-socket WSGI server + admin-bot routes.

Spec: internal/spec-evo-account-separation-2026-05-25.md §3, Phase E.3.1.

These tests bind an actual unix-socket server to a tmp_path location,
register the admin-bot routes on a Flask app, and verify that requests
flowing over the socket carry the expected REMOTE_TRANSPORT / peer-uid
environ keys AND that the admin-client module can talk to the server
end-to-end.

The trust list is patched to {current_uid} for each test so they pass
regardless of the runner's user identity.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

import pytest
from flask import Flask, jsonify

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import get_profile
from evolve_admin.evo.admin_client import (
    AdminDaemonUnavailable,
    get_json,
)
from evolve_admin.web.admin_bot_routes import register_admin_bot_routes
from evolve_admin.web.unix_socket_server import start_in_background


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def socket_path() -> Path:
    """A short tmp socket path. macOS limits AF_UNIX paths to ~104 chars,
    and pytest's tmp_path often exceeds that under $TMPDIR. Use a
    short /tmp path with a process-unique suffix to stay within bounds.
    """
    import tempfile
    # mkstemp gives us a unique filename without creating a stream;
    # delete the placeholder so bind can create the socket cleanly.
    fd, name = tempfile.mkstemp(prefix="evos-", suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(name)
    p = Path(name)
    yield p
    # Cleanup if test didn't already
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    # Register the admin-bot routes (C1 + _peer-identity)
    register_admin_bot_routes(app, network_path=Path("/tmp/unused"))
    return app


@pytest.fixture
def trust_current_user(monkeypatch):
    """Patch _resolve_uids to trust the test runner's uid."""
    def _stub(_names):
        return {os.getuid()}
    monkeypatch.setattr("evolve_admin.web.peer_auth._resolve_uids", _stub)


@pytest.fixture
def running_server(socket_path, flask_app, trust_current_user):
    """Start the unix-socket server, yield the (thread, server) tuple, clean up."""
    thread, server = start_in_background(flask_app, socket_path, enable_watchdog=False)
    # Give the thread a moment to spin up
    time.sleep(0.05)
    yield thread, server
    server.shutdown()
    server.server_close()


# ── Server binds the socket with expected mode ───────────────────────────────


def test_socket_file_created_with_correct_mode(running_server, socket_path):
    """Socket should be 0660 (owner + group read/write, no world).

    Note: macOS may apply umask masking; the helper explicitly chmods
    so this should hold regardless of the caller's umask.
    """
    assert socket_path.exists()
    mode = socket_path.stat().st_mode & 0o777
    assert mode == 0o660, f"expected 0o660; got {oct(mode)}"


def test_default_socket_group_is_platform_keyed_bot_group():
    """The bind chgrp target derives from ``platform_profile.bot_shared_group``
    (macOS ``staff`` / Linux ``evolve-bots``), not a hardcoded ``staff``."""
    from platform_profile import get_profile
    from evolve_admin.web.unix_socket_server import DEFAULT_SOCKET_GROUP

    assert DEFAULT_SOCKET_GROUP == get_profile().bot_shared_group


def test_socket_file_chgrp_to_shared_bot_group(running_server, socket_path):
    """Socket gets explicitly chgrp'd to the shared bot group so bot users
    (which have it as a primary/secondary group) can connect under mode 0660.
    Without this, the socket inherits the daemon's effective group — ``wheel``
    on production macOS launchd pods — locking every bot out.

    macOS: ``staff`` (gid 20) is the chgrp target AND the reach mechanism. On
    Linux the target is ``evolve-bots`` but ``evolve`` isn't a member so the
    chgrp EPERMs (the named ACE is the real reach there). The chgrp only takes
    when the runner is a member of the target group, so skip when the group is
    absent or the runner can't perform it — we assert the gid only when it can.
    """
    import grp
    import os
    expected_group = get_profile().bot_shared_group
    try:
        expected_gid = grp.getgrnam(expected_group).gr_gid
    except KeyError:
        pytest.skip(f"{expected_group!r} group not present in this environment")
    if expected_gid not in (set(os.getgroups()) | {os.getgid()}):
        pytest.skip(
            f"test runner is not a member of {expected_group!r} — unprivileged "
            f"chgrp would EPERM (mirrors evolve↔evolve-bots on the Linux pod, "
            f"where the named group ACE is the reach mechanism, not the chgrp)"
        )
    assert socket_path.stat().st_gid == expected_gid, (
        f"socket group should be {expected_gid} ({expected_group}); got "
        f"{socket_path.stat().st_gid}"
    )


def test_socket_group_can_be_overridden(socket_path, flask_app, trust_current_user):
    """Explicit ``socket_group=None`` skips the chgrp step entirely
    (escape hatch for environments where ``staff`` doesn't exist or
    the daemon shouldn't be re-grouping its socket).
    """
    thread, server = start_in_background(
        flask_app, socket_path, socket_group=None, enable_watchdog=False,
    )
    try:
        time.sleep(0.05)
        # No specific gid expected; we just verify the socket exists
        # and the server is healthy (no crash from skipping chgrp).
        assert socket_path.exists()
    finally:
        server.shutdown()
        server.server_close()


# ── bot-group connect ACL on bind (connect-EACCES fix, 2026-06-25) ────────────
#
# The Linux bot gateways run as users in the shared ``evolve-bots`` group but
# NOT in the socket-owning ``evolve`` group, so mode-0660 leaves them on the
# socket's ``other`` class = ``r--`` and a unix connect() (a WRITE on the inode)
# hits EACCES — pod-wide, every member bot. The bind path grants the shared bot
# GROUP a named write ACE OWNER-DIRECT (no sudo), without widening ``other``.
# macOS is a no-op (bots reach the socket via its ``staff`` group ownership).

def test_bind_invokes_bot_socket_acl_grant(socket_path, flask_app, trust_current_user):
    """server_bind calls ensure_bot_socket_acl with the bound socket path,
    on every (re)bind — the primary enforcer of the bot-group connect grant."""
    from evolve_admin.web import unix_socket_server as uss

    calls: list[str] = []

    def _spy(path):
        calls.append(str(path))
        return True

    orig = uss.ensure_bot_socket_acl
    uss.ensure_bot_socket_acl = _spy  # type: ignore[assignment]
    try:
        thread, server = start_in_background(
            flask_app, socket_path, enable_watchdog=False,
        )
        try:
            time.sleep(0.05)
            assert str(socket_path) in calls
        finally:
            server.shutdown()
            server.server_close()
    finally:
        uss.ensure_bot_socket_acl = orig  # type: ignore[assignment]


def test_ensure_bot_socket_acl_noop_on_macos(socket_path, monkeypatch):
    """macOS profile: no named ACE (bots reach via ``staff`` group ownership) —
    returns False and never shells out to setfacl."""
    from platform_profile import MACOS, set_profile
    from evolve_admin.web import unix_socket_server as uss
    import evolve_admin.evo_socket_acl as esa

    ran = {"n": 0}
    monkeypatch.setattr(esa.subprocess, "run",
                        lambda *a, **k: ran.__setitem__("n", ran["n"] + 1))
    set_profile(MACOS)
    try:
        assert uss.ensure_bot_socket_acl(socket_path) is False
    finally:
        set_profile(None)
    assert ran["n"] == 0


def test_ensure_bot_socket_acl_linux_grants_group_ace_owner_direct_no_sudo(
    socket_path, monkeypatch,
):
    """Linux profile: shells out to ``setfacl -m g:evolve-bots:rwx`` applied
    OWNER-DIRECT — the argv must NOT begin with ``sudo`` (the crux: the owner
    sets the ACE on its own socket, so no sudoers grant is needed; routing it
    through the sudo-wrapping seam is exactly what silently broke #3223)."""
    from platform_profile import LINUX, set_profile
    import evolve_admin.evo_socket_acl as esa

    captured: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kwargs):
        captured.append(list(cmd))
        return _OK()

    monkeypatch.setattr(esa.subprocess, "run", _run)
    set_profile(LINUX)
    try:
        ok = esa._ensure_bot_socket_acl(socket_path)
    finally:
        set_profile(None)
    assert ok is True
    assert len(captured) == 1
    argv = captured[0]
    assert "sudo" not in argv, f"grant must be owner-direct, not sudo: {argv}"
    assert argv[0] == LINUX.setfacl  # /usr/bin/setfacl, invoked directly
    assert argv[1] == "-m"
    assert argv[2] == "g:evolve-bots:rwx"  # shared bot GROUP, never u:evo, never other
    assert argv[3] == str(socket_path)


def test_ensure_bot_socket_acl_fail_soft_on_grant_error(socket_path, monkeypatch):
    """A setfacl failure (or any raise) must NOT break the bind — fail-soft to
    False so the socket still serves; only the bot connect channel degrades."""
    from evolve_admin.web import unix_socket_server as uss

    def _boom(path):
        raise OSError("setfacl exploded")

    monkeypatch.setattr("evolve_admin.evo_socket_acl._ensure_bot_socket_acl", _boom)
    assert uss.ensure_bot_socket_acl(socket_path) is False


def test_socket_file_cleaned_up_on_close(socket_path, flask_app, trust_current_user):
    """server_close() removes the socket file from disk."""
    thread, server = start_in_background(flask_app, socket_path, enable_watchdog=False)
    time.sleep(0.05)
    assert socket_path.exists()

    server.shutdown()
    server.server_close()
    time.sleep(0.05)
    assert not socket_path.exists()


def test_stale_socket_file_is_cleaned_up_on_start(socket_path, flask_app, trust_current_user):
    """Old socket file from a crashed server gets unlinked before bind.

    Without this, the second start would fail with EADDRINUSE.
    """
    # Pre-create a "stale" socket file
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    assert socket_path.exists()

    thread, server = start_in_background(flask_app, socket_path, enable_watchdog=False)
    time.sleep(0.05)
    try:
        assert socket_path.exists()
        # And it's actually a socket now, not the regular file
        import stat as _stat
        mode = socket_path.stat().st_mode
        assert _stat.S_ISSOCK(mode), f"expected socket; got {oct(mode)}"
    finally:
        server.shutdown()
        server.server_close()


# ── Bug A: silent daemon-thread death + self-healing ─────────────────────────


def test_serve_with_recovery_logs_and_rebinds_on_crash(caplog):
    """If ``serve_forever`` raises, the recovery wrapper:
      1. Logs the exception (no more silent death).
      2. Calls the factory to construct a fresh server.
      3. Bails after _REBIND_MAX_ATTEMPTS failures.

    Verified with a stub factory that returns servers whose
    serve_forever raises on the first call and shuts down on the
    second. The recovery loop should:
      - call serve_forever on the initial server → raises → log
      - call factory → get fresh server → serve_forever → returns
        cleanly → exit loop
    """
    import logging
    from evolve_admin.web import unix_socket_server as uss

    # Speed the test up dramatically.
    monkey_backoff = 0.0

    class _StubServer:
        def __init__(self, behavior: str):
            self.behavior = behavior
            self.serve_calls = 0
            self.closed = False

        def serve_forever(self):
            self.serve_calls += 1
            if self.behavior == "crash":
                raise RuntimeError("simulated daemon thread death")
            # behavior == "ok": pretend a clean shutdown
            return

        def server_close(self):
            self.closed = True

    initial = _StubServer("crash")
    fresh = _StubServer("ok")
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return fresh

    # Override backoff so the test isn't slow
    orig_backoff = uss._REBIND_BACKOFF_SECONDS
    uss._REBIND_BACKOFF_SECONDS = monkey_backoff
    try:
        with caplog.at_level(logging.WARNING, logger="evolve_admin.web.unix_socket_server"):
            uss._serve_with_recovery(factory, initial_server=initial)
    finally:
        uss._REBIND_BACKOFF_SECONDS = orig_backoff

    # The initial server crashed and was closed
    assert initial.serve_calls == 1
    assert initial.closed is True
    # The factory was invoked once to build a fresh server
    assert len(factory_calls) == 1
    # The fresh server ran serve_forever once and returned cleanly
    assert fresh.serve_calls == 1

    # Crash got logged loudly (not silent)
    crash_logs = [
        r for r in caplog.records
        if "serve_forever crashed" in r.getMessage()
    ]
    assert len(crash_logs) >= 1, (
        f"expected a 'serve_forever crashed' log; got {[r.getMessage() for r in caplog.records]}"
    )


def test_serve_with_recovery_gives_up_after_max_attempts(caplog):
    """If serve_forever keeps crashing, the recovery loop bails after
    _REBIND_MAX_ATTEMPTS to avoid spinning the CPU on a permanently
    broken socket.
    """
    import logging
    from evolve_admin.web import unix_socket_server as uss

    class _AlwaysCrashServer:
        def __init__(self):
            self.serve_calls = 0

        def serve_forever(self):
            self.serve_calls += 1
            raise RuntimeError("permanent crash")

        def server_close(self):
            pass

    factory_calls = []

    def factory():
        factory_calls.append(1)
        return _AlwaysCrashServer()

    initial = _AlwaysCrashServer()

    orig_backoff = uss._REBIND_BACKOFF_SECONDS
    uss._REBIND_BACKOFF_SECONDS = 0.0
    try:
        with caplog.at_level(logging.ERROR, logger="evolve_admin.web.unix_socket_server"):
            uss._serve_with_recovery(factory, initial_server=initial)
    finally:
        uss._REBIND_BACKOFF_SECONDS = orig_backoff

    # Should have given up. Total crashes = MAX_ATTEMPTS (each iteration
    # counts as one).
    assert len(factory_calls) + 1 >= uss._REBIND_MAX_ATTEMPTS, (
        f"expected ~{uss._REBIND_MAX_ATTEMPTS} attempts; "
        f"got initial+factory={1 + len(factory_calls)}"
    )
    give_up_logs = [
        r for r in caplog.records
        if "giving up" in r.getMessage()
    ]
    assert len(give_up_logs) >= 1


def test_start_in_background_uses_recovery_wrapper(socket_path, flask_app, trust_current_user):
    """End-to-end: the public entrypoint launches the daemon thread
    through ``_serve_with_recovery``, not ``serve_forever`` directly.
    Regression check so a future refactor doesn't accidentally bypass
    the recovery shell.
    """
    thread, server = start_in_background(flask_app, socket_path, enable_watchdog=False)
    try:
        time.sleep(0.05)
        # The thread's target should be the recovery wrapper, not
        # the raw serve_forever.
        assert thread.name == "admin-daemon-unix-socket"
        # Thread is alive and the socket is up
        assert thread.is_alive()
        assert socket_path.exists()
    finally:
        server.shutdown()
        server.server_close()


# ── End-to-end: smoke endpoint returns peer identity ─────────────────────────


def test_peer_identity_endpoint_returns_calling_uid(running_server, socket_path):
    """GET /api/admin/_peer-identity returns the runner's uid + transport."""
    status, body = get_json(
        "/api/admin/_peer-identity", socket_path=socket_path,
    )
    assert status == 200, f"got {status} body={body}"
    assert body["transport"] == "unix-socket"
    assert body["peer_uid"] == os.getuid()
    # gid extraction may or may not work depending on macOS specifics —
    # accept any non-negative value
    assert body["peer_gid"] >= 0


# ── Auth rejection: peer uid not in trust list ───────────────────────────────


def test_protected_route_rejects_untrusted_peer_uid(
    socket_path, flask_app, monkeypatch,
):
    """When the peer uid isn't in the trust list, 403."""
    # Trust nobody (uid set is empty)
    monkeypatch.setattr(
        "evolve_admin.web.peer_auth._resolve_uids",
        lambda _names: set(),  # no one is trusted
    )

    thread, server = start_in_background(flask_app, socket_path, enable_watchdog=False)
    time.sleep(0.05)
    try:
        status, body = get_json(
            "/api/admin/_peer-identity", socket_path=socket_path,
        )
        # Current uid won't match anything in the empty trust set
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()


# ── Peer-credential extraction — platform-dispatched ─────────────────────────
# macOS uses getpeereid(3) (libc, BSD); Linux uses getsockopt(SO_PEERCRED).
# glibc lacks getpeereid, so without the Linux branch every member bot's
# REMOTE_PEER_UID is -1 → resolve_peer_bot_id returns None → /api/directory/digest
# 401s. These tests pin the platform profile and feed a fake socket so both
# branches are exercised regardless of the runner's OS.
from evolve_admin.web import unix_socket_server as _uss  # noqa: E402

# The peer-cred branch is OS-dispatched on _uss._is_linux() (real sys.platform),
# NOT the platform_profile — the admin suite's autouse fixture pins the MACOS
# profile even on Linux CI, so these tests override _is_linux() to exercise each
# branch deterministically on any host.


def test_get_peer_eid_linux_decodes_so_peercred(monkeypatch):
    """On Linux, _get_peer_eid reads the peer uid/gid from
    getsockopt(SOL_SOCKET, SO_PEERCRED) — the SECOND/THIRD fields of struct
    ucred — and the value is KERNEL-sourced (queried off the socket), never
    taken from the request. Fails on main: _get_peer_eid takes an fd + has no
    Linux branch, and _SO_PEERCRED doesn't exist."""
    captured = {}

    class _FakeConn:
        def getsockopt(self, level, optname, buflen):
            captured["args"] = (level, optname, buflen)
            # struct ucred { pid_t pid; uid_t uid; gid_t gid; }
            return struct.pack("iII", 4242, 1234, 5678)

    monkeypatch.setattr(_uss, "_is_linux", lambda: True)
    uid, gid = _uss._get_peer_eid(_FakeConn())

    assert (uid, gid) == (1234, 5678), "uid is the 2nd ucred field, gid the 3rd"
    # Proves the value is kernel-sourced off the socket, not client-supplied.
    assert captured["args"][0] == socket.SOL_SOCKET
    assert captured["args"][1] == _uss._SO_PEERCRED
    assert captured["args"][2] == struct.calcsize("iII")


def test_so_peercred_decodes_high_uid_as_unsigned():
    """A uid >= 2**31 (e.g. a systemd DynamicUser) decodes UNSIGNED — matching
    the macOS c_uint32 path — never a negative int. Fails if _UCRED_FMT decoded
    uid/gid as signed ('3i'): the value would come back as a negative number
    (which would then trip peer_auth's ``peer_uid < 0`` fail-closed guard and
    wrongly deny a legitimate high-uid bot)."""
    big = 2**31 + 7  # 2147483655 — negative under signed int32

    class _FakeConn:
        def getsockopt(self, *_a):
            return struct.pack("iII", 4242, big, big)  # pid signed, uid/gid unsigned

    uid, gid = _uss._peer_eid_so_peercred(_FakeConn())
    assert (uid, gid) == (big, big), "uid/gid must decode as unsigned 32-bit"
    assert uid > 0 and gid > 0, "must not decode negative under a signed unpack"


def test_get_peer_eid_dispatch_picks_per_os(monkeypatch):
    """The dispatch routes to the SO_PEERCRED helper on Linux and the
    getpeereid helper on macOS — and ONLY that one. Fails on main: no dispatch
    + the helper functions don't exist."""
    calls = []
    monkeypatch.setattr(
        _uss, "_peer_eid_so_peercred", lambda s: calls.append("linux") or (11, 11)
    )
    monkeypatch.setattr(
        _uss, "_peer_eid_getpeereid", lambda s: calls.append("macos") or (22, 22)
    )

    monkeypatch.setattr(_uss, "_is_linux", lambda: True)
    assert _uss._get_peer_eid(object()) == (11, 11)
    monkeypatch.setattr(_uss, "_is_linux", lambda: False)
    assert _uss._get_peer_eid(object()) == (22, 22)

    assert calls == ["linux", "macos"], "each OS calls exactly its own helper"


def test_is_linux_keys_off_real_sys_platform(monkeypatch):
    """_is_linux follows the REAL sys.platform, NOT the (pinnable) platform
    profile — the property that keeps the Linux SO_PEERCRED branch reachable on
    a Linux CI runner even though the suite pins the MACOS profile."""
    monkeypatch.setattr(_uss.sys, "platform", "linux")
    assert _uss._is_linux() is True
    monkeypatch.setattr(_uss.sys, "platform", "darwin")
    assert _uss._is_linux() is False


def test_so_peercred_degrades_on_syscall_error():
    """A getsockopt failure (option unsupported on the kernel) degrades to
    (None, None) — the handler then sets uid -1 and peer_auth 403s. Never raises."""

    class _BadConn:
        def getsockopt(self, *_a):
            raise OSError(95, "Operation not supported")

    assert _uss._peer_eid_so_peercred(_BadConn()) == (None, None)


def test_so_peercred_degrades_on_short_read():
    """A truncated ucred buffer (struct.error) degrades to (None, None)."""

    class _ShortConn:
        def getsockopt(self, *_a):
            return b"\x00\x00"  # < calcsize("3i") → struct.error

    assert _uss._peer_eid_so_peercred(_ShortConn()) == (None, None)


def test_getpeereid_degrades_when_symbol_absent(monkeypatch):
    """The exact live Linux failure mode: libc loads but lacks getpeereid (a BSD
    symbol glibc omits) → AttributeError → (None, None), no crash. This is why
    Linux must use SO_PEERCRED, and why the macOS branch fails soft there."""

    class _GlibcNoGetpeereid:
        def __getattr__(self, name):  # any symbol lookup raises, like a missing one
            raise AttributeError(name)

    monkeypatch.setattr(_uss, "_libc", _GlibcNoGetpeereid())

    class _Conn:
        def fileno(self):
            return 7

    assert _uss._peer_eid_getpeereid(_Conn()) == (None, None)


def test_so_peercred_constant_resolves():
    """_SO_PEERCRED is the real Linux constant when present, else the canonical
    fallback (17) so the module imports on a non-Linux build."""
    assert _uss._SO_PEERCRED == getattr(socket, "SO_PEERCRED", 17)


# ── Admin client error handling ──────────────────────────────────────────────


def test_admin_client_raises_when_socket_absent(tmp_path):
    """When the socket file doesn't exist, AdminDaemonUnavailable is raised
    (not a bare ConnectionError)."""
    nonexistent = tmp_path / "nope.sock"
    with pytest.raises(AdminDaemonUnavailable):
        get_json("/api/admin/_peer-identity", socket_path=nonexistent, timeout=1.0)


def test_admin_client_returns_404_when_endpoint_missing(running_server, socket_path):
    """The server returns 404 for an unknown path; admin_client surfaces it."""
    status, body = get_json(
        "/api/admin/no_such_endpoint", socket_path=socket_path,
    )
    assert status == 404


# ── C1 endpoint — /api/admin/bot/<id>/openclaw-config ────────────────────────


def test_c1_endpoint_returns_projected_config(running_server, socket_path, tmp_path, monkeypatch):
    """End-to-end: write a fake bot home, call C1, get the projected shape back."""
    # Build a synthetic /Users/<bot>/.openclaw/openclaw.json
    bot_home_path = tmp_path / "team_bot_a_home"
    oc_dir = bot_home_path / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps({
        "gateway": {
            "port": 18789,
            "bind": "loopback",
            "auth": {"mode": "token", "token": "secret-token-value-do-not-leak"},
        },
        "agents": {
            "defaults": {
                "model": {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []},
                "thinkingDefault": "off",
            },
        },
        "tools": {"exec": {"security": "full", "ask": "on-miss"}},
        "channels": {
            "slack": {"enabled": True, "botToken": "xoxb-secret"},
        },
    }))

    # Patch bot_home + load_network in the admin-side helper
    import evolve_admin.config as _cfg
    monkeypatch.setattr(_cfg, "bot_home", lambda bot_id, network=None: bot_home_path)
    monkeypatch.setattr(_cfg, "load_network", lambda path=None: {
        "bots": {"team_bot_a": {"role": "member"}},
    })

    status, body = get_json(
        "/api/admin/bot/team_bot_a/openclaw-config", socket_path=socket_path,
    )
    assert status == 200, f"got {status} body={body}"
    assert body["bot_id"] == "team_bot_a"
    assert body["available"] is True
    cfg = body["config"]

    # Gateway shape
    assert cfg["gateway"]["port"] == 18789
    assert cfg["gateway"]["auth_mode"] == "token"
    # Token should NOT appear in projected output
    assert "secret-token-value-do-not-leak" not in json.dumps(body)

    # Agents shape
    assert cfg["agents"]["model_primary"] == "anthropic/claude-sonnet-4-6"

    # Tools
    assert cfg["tools"]["exec_security"] == "full"
    assert cfg["tools"]["exec_ask"] == "on-miss"

    # Slack token also redacted
    assert "xoxb-secret" not in json.dumps(body)


def test_c1_endpoint_returns_404_for_missing_bot(running_server, socket_path, monkeypatch):
    """When bot_home points at a non-existent path, the endpoint returns 404
    with available=False in the body."""
    nonexistent = Path("/no/such/path/anywhere")
    import evolve_admin.config as _cfg
    monkeypatch.setattr(_cfg, "bot_home", lambda bot_id, network=None: nonexistent)
    monkeypatch.setattr(_cfg, "load_network", lambda path=None: {"bots": {}})

    status, body = get_json(
        "/api/admin/bot/ghost/openclaw-config", socket_path=socket_path,
    )
    assert status == 404
    assert body["bot_id"] == "ghost"
    assert body["available"] is False
    assert "could not read" in body["error"].lower()


# ── Threading: concurrent requests don't queue behind each other ─────────────


@pytest.mark.real_sleep  # slow route sleeps 1s to prove requests are served concurrently
def test_concurrent_requests_processed_in_parallel(socket_path, flask_app, trust_current_user):
    """ThreadingMixIn means each request gets a worker thread. A slow
    request should NOT block other requests from being accepted +
    handled.

    Regression test for the 2026-05-26 production hang: the single-
    threaded base server let one wedged handler block the entire
    accept loop, leaving evo's daemon-routed tools unreachable.
    """
    import urllib.request
    import threading as _threading

    # Add a slow route that sleeps 1s — single-threaded would force
    # later requests to wait at least 1s.
    @flask_app.route("/api/test/slow")
    def _slow():
        import time as _time
        _time.sleep(1.0)
        return {"slept": True}

    @flask_app.route("/api/test/fast")
    def _fast():
        return {"fast": True}

    thread, server = start_in_background(
        flask_app, socket_path, enable_watchdog=False,
    )
    try:
        time.sleep(0.05)

        results: dict[str, float] = {}

        def fire(label: str, route: str):
            from evolve_admin.evo.admin_client import get_json
            t0 = time.time()
            status, body = get_json(route, socket_path=str(socket_path))
            results[label] = (time.time() - t0, status)

        # Start the slow request, give it ~50ms to begin processing,
        # then fire a fast request. If threading works, the fast one
        # should return BEFORE the slow one and within ~100ms.
        t_slow = _threading.Thread(target=fire, args=("slow", "/api/test/slow"))
        t_fast = _threading.Thread(target=fire, args=("fast", "/api/test/fast"))
        t_slow.start()
        time.sleep(0.1)
        t_fast.start()
        t_fast.join(timeout=2.0)
        t_slow.join(timeout=2.0)

        assert "fast" in results, "fast request never completed"
        assert "slow" in results, "slow request never completed"
        fast_elapsed, fast_status = results["fast"]
        slow_elapsed, _ = results["slow"]
        assert fast_status == 200
        # Fast request should NOT have waited for slow — single-threaded
        # would force fast_elapsed >= ~0.9s. Threaded should be well
        # under 0.5s. Conservative threshold to absorb CI jitter.
        assert fast_elapsed < 0.5, (
            f"fast request waited {fast_elapsed:.2f}s — likely blocked "
            f"behind slow request (took {slow_elapsed:.2f}s). "
            f"ThreadingMixIn regression?"
        )
    finally:
        server.shutdown()
        server.server_close()


# ── Watchdog: probe-failure rebind ──────────────────────────────────────────


def test_probe_socket_alive_returns_true_for_responsive_server(
    socket_path, flask_app, trust_current_user,
):
    from evolve_admin.web.unix_socket_server import _probe_socket_alive

    thread, server = start_in_background(
        flask_app, socket_path, enable_watchdog=False,
    )
    try:
        time.sleep(0.05)
        assert _probe_socket_alive(str(socket_path), timeout=2.0) is True
    finally:
        server.shutdown()
        server.server_close()


def test_probe_socket_alive_returns_false_when_socket_absent(tmp_path):
    from evolve_admin.web.unix_socket_server import _probe_socket_alive
    nonexistent = str(tmp_path / "no.sock")
    assert _probe_socket_alive(nonexistent, timeout=1.0) is False


# Both watchdog-loop unit tests below bound the loop with ``stop_event``
# and count *probe calls*, never sleeps. An earlier version drove them by
# patching ``uss.time.sleep`` with a counter that raised SystemExit past N
# iterations — but ``uss.time`` IS the stdlib ``time`` module, so that
# patch is process-global: every other live thread in the shard (leftover
# server/handler threads, other modules' daemons) burned iterations off
# the shared counter, and the loop exited before reaching the threshold.
# That is the 2026-08-05 "shutdown_called=0" flake in Admin shard 2/4.
# Probe calls are 1:1 with loop cycles and only this test can advance them.


def test_watchdog_triggers_shutdown_after_consecutive_failures(monkeypatch):
    """Direct unit test of the watchdog loop logic.

    Uses an in-memory probe stub that always returns False, and a
    server stub that records shutdown() calls. Verifies the watchdog
    calls shutdown after ``failure_threshold`` consecutive failures.
    """
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    class _ServerStub:
        def __init__(self):
            self.shutdown_called = 0

        def shutdown(self):
            self.shutdown_called += 1

    srv_stub = _ServerStub()
    server_ref = [srv_stub]
    stop_event = _th.Event()

    threshold = 2
    # One probe per loop cycle. Run one cycle past the threshold so a
    # premature trip (shutdown before `threshold` failures) would show up
    # as an extra shutdown rather than passing silently.
    max_probes = threshold + 1
    probes = {"n": 0}

    def dead_probe(_path, _timeout):
        probes["n"] += 1
        if probes["n"] >= max_probes:
            stop_event.set()
        return False

    monkeypatch.setattr(uss, "_probe_socket_alive", dead_probe)

    uss._run_liveness_watchdog(
        "/nonexistent/sock", server_ref,
        period=0.0, timeout=0.001, failure_threshold=threshold,
        stop_event=stop_event,
    )

    # F (fail=1), F (fail=2 → shutdown, counter resets), F (fail=1, no trip).
    assert srv_stub.shutdown_called == 1, (
        f"watchdog should have called server.shutdown() exactly once after "
        f"{threshold} consecutive failures over {max_probes} probes; got "
        f"shutdown_called={srv_stub.shutdown_called}"
    )


def test_watchdog_resets_failure_count_on_recovery(monkeypatch):
    """After a recovery (probe returns True), the failure counter
    resets — a future failure has to re-accumulate to threshold."""
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    class _ServerStub:
        def __init__(self):
            self.shutdown_called = 0

        def shutdown(self):
            self.shutdown_called += 1

    srv_stub = _ServerStub()
    server_ref = [srv_stub]
    stop_event = _th.Event()

    # Probe script: False, False, True (recovery), False — then stop.
    script = [False, False, True, False]
    probes = {"n": 0}

    def scripted_probe(_path, _timeout):
        n = probes["n"]
        probes["n"] += 1
        if probes["n"] >= len(script):
            stop_event.set()
        return script[n] if n < len(script) else True

    monkeypatch.setattr(uss, "_probe_socket_alive", scripted_probe)

    uss._run_liveness_watchdog(
        "/nonexistent/sock", server_ref,
        period=0.0, timeout=0.001, failure_threshold=2,
        stop_event=stop_event,
    )

    # Sequence: F (fail=1), F (fail=2 → shutdown), T (reset), F (fail=1)
    # → shutdown called exactly once during the threshold trip.
    assert srv_stub.shutdown_called == 1, (
        f"expected 1 shutdown (after 2 consecutive failures, then "
        f"recovery, then 1 more failure shouldn't trip); got "
        f"{srv_stub.shutdown_called}"
    )
    assert probes["n"] == len(script), (
        f"the loop should run exactly {len(script)} cycles; got {probes['n']}"
    )


# ── 2026-07-28 incident: EMFILE-storm survival ──────────────────────────────
# The admin daemon's unix-socket listener died during an fd-exhaustion
# storm (launchd soft NOFILE 256 + werkzeug thread amplification) and
# stayed dead for 10 days: bound fd alive, backlog full, every connect()
# → ECONNREFUSED. These tests pin the four hardening layers.


def test_accept_oserror_logs_backs_off_and_keeps_accepting(
    socket_path, flask_app, trust_current_user, monkeypatch, caplog,
):
    """accept() raising OSError (EMFILE) must not take down the accept
    loop: the server logs at ERROR with the errno, backs off briefly,
    and the SAME queued connection is eventually accepted and served."""
    import errno as _errno
    import logging
    import socketserver as _ss
    from evolve_admin.web import unix_socket_server as uss

    monkeypatch.setattr(uss, "_ACCEPT_BACKOFF_MIN_SECONDS", 0.01)
    monkeypatch.setattr(uss, "_ACCEPT_BACKOFF_MAX_SECONDS", 0.02)

    # _UnixWSGIServer.get_request's super() resolves to TCPServer's —
    # make it fail twice with EMFILE, then behave normally. The pending
    # connection stays queued across failures (accept never consumed it),
    # so the loop retries against the same client.
    fail = {"remaining": 2}
    orig_get = _ss.TCPServer.get_request

    def flaky(self):
        if fail["remaining"] > 0:
            fail["remaining"] -= 1
            raise OSError(_errno.EMFILE, "Too many open files")
        return orig_get(self)

    monkeypatch.setattr(_ss.TCPServer, "get_request", flaky)

    with caplog.at_level(
        logging.ERROR, logger="evolve_admin.web.unix_socket_server",
    ):
        thread, server = start_in_background(
            flask_app, socket_path, enable_watchdog=False,
        )
        try:
            time.sleep(0.05)
            status, body = get_json(
                "/api/admin/_peer-identity", socket_path=socket_path,
            )
        finally:
            server.shutdown()
            server.server_close()

    assert status == 200, f"got {status} body={body}"
    assert fail["remaining"] == 0, "the EMFILE branch was never exercised"
    accept_logs = [
        r for r in caplog.records if "accept() failed" in r.getMessage()
    ]
    assert len(accept_logs) == 2
    assert f"errno={_errno.EMFILE}" in accept_logs[0].getMessage()


def test_process_request_failure_drops_connection_keeps_listener(
    socket_path, flask_app, trust_current_user, monkeypatch, caplog,
):
    """Thread-start failure (resource exhaustion) for ONE accepted
    connection must not tear down the bound listener — pre-fix it
    propagated out of serve_forever and forced a full rebind (dropping
    the whole backlog)."""
    import logging
    import socketserver as _ss

    state = {"boom": True}
    orig_pr = _ss.ThreadingMixIn.process_request

    def maybe_boom(self, request, client_address):
        if state["boom"]:
            raise RuntimeError("can't start new thread")
        return orig_pr(self, request, client_address)

    monkeypatch.setattr(_ss.ThreadingMixIn, "process_request", maybe_boom)

    with caplog.at_level(
        logging.ERROR, logger="evolve_admin.web.unix_socket_server",
    ):
        thread, server = start_in_background(
            flask_app, socket_path, enable_watchdog=False,
        )
        try:
            time.sleep(0.05)
            # First request: its connection is dropped, client sees a
            # connection-level failure surfaced as AdminDaemonUnavailable.
            with pytest.raises(AdminDaemonUnavailable):
                get_json(
                    "/api/admin/_peer-identity",
                    socket_path=socket_path, timeout=2.0,
                )
            # The listener survived: with the failure cleared, the next
            # request on the SAME server succeeds.
            state["boom"] = False
            status, _body = get_json(
                "/api/admin/_peer-identity", socket_path=socket_path,
            )
            assert status == 200
        finally:
            server.shutdown()
            server.server_close()

    assert any(
        "could not service accepted connection" in r.getMessage()
        for r in caplog.records
    )


def test_serve_with_recovery_rebinds_on_watchdog_requested_clean_return(caplog):
    """server.shutdown() makes serve_forever RETURN cleanly — it never
    raises. Pre-fix, the recovery wrapper treated that as an intentional
    stop, so the watchdog's teardown-to-rebind permanently killed the
    listener (the 10-day-outage mechanism). With rebind_request set, a
    clean return must rebind via the factory instead of exiting."""
    import logging
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    class _StubServer:
        def __init__(self, behavior):
            self.behavior = behavior
            self.serve_calls = 0
            self.closed = False

        def serve_forever(self):
            self.serve_calls += 1
            return  # clean return, as after shutdown()

        def server_close(self):
            self.closed = True

    rebind_request = _th.Event()
    rebind_request.set()  # the watchdog sets this BEFORE shutdown()
    initial = _StubServer("watchdog-teardown")
    fresh = _StubServer("ok")  # its clean return (event now clear) ends the loop
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return fresh

    orig_backoff = uss._REBIND_BACKOFF_SECONDS
    uss._REBIND_BACKOFF_SECONDS = 0.0
    try:
        with caplog.at_level(
            logging.WARNING, logger="evolve_admin.web.unix_socket_server",
        ):
            uss._serve_with_recovery(
                factory, initial_server=initial, rebind_request=rebind_request,
            )
    finally:
        uss._REBIND_BACKOFF_SECONDS = orig_backoff

    assert initial.serve_calls == 1
    assert initial.closed is True, "the torn-down server must be closed"
    assert len(factory_calls) == 1, "a rebind must have been attempted"
    assert fresh.serve_calls == 1
    assert not rebind_request.is_set(), "the event must be consumed"
    assert any(
        "watchdog-requested teardown" in r.getMessage() for r in caplog.records
    )


def test_serve_with_recovery_clean_return_without_request_still_exits():
    """A clean return with NO rebind request is an intentional shutdown
    (tests, process teardown) — the wrapper must exit, not spin."""
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    class _StubServer:
        def __init__(self):
            self.serve_calls = 0

        def serve_forever(self):
            self.serve_calls += 1

        def server_close(self):
            pass

    stub = _StubServer()
    uss._serve_with_recovery(
        lambda: (_ for _ in ()).throw(AssertionError("factory must not run")),
        initial_server=stub,
        rebind_request=_th.Event(),  # never set
    )
    assert stub.serve_calls == 1


def test_failure_streak_resets_after_healthy_uptime(monkeypatch):
    """Crashes separated by healthy serving periods must NOT accumulate
    toward the give-up budget. Pre-fix the counter was cumulative over
    the process lifetime, so storm bursts days apart eventually spent
    all 12 attempts and the wrapper gave up forever."""
    from evolve_admin.web import unix_socket_server as uss

    # Any positive uptime counts as "healthy" for the test.
    monkeypatch.setattr(uss, "_FAILURE_STREAK_RESET_SECONDS", 0.0)
    monkeypatch.setattr(uss, "_REBIND_BACKOFF_SECONDS", 0.0)

    crashes_wanted = uss._REBIND_MAX_ATTEMPTS + 3  # over the old budget
    state = {"crashes": 0}

    class _CrashThenOkServer:
        def serve_forever(self):
            if state["crashes"] < crashes_wanted:
                state["crashes"] += 1
                raise RuntimeError("burst crash")
            return  # clean exit ends the test

        def server_close(self):
            pass

    uss._serve_with_recovery(
        _CrashThenOkServer, initial_server=_CrashThenOkServer(),
    )
    assert state["crashes"] == crashes_wanted, (
        "the wrapper gave up early — the failure streak did not reset "
        "after healthy uptime"
    )


def test_probe_socket_alive_survives_emfile_at_socket_creation(monkeypatch):
    """Under fd exhaustion socket.socket() itself raises OSError(EMFILE).
    Pre-fix that escaped _probe_socket_alive and silently killed the
    watchdog thread — exactly when the daemon most needed it. It must
    report a failed probe instead."""
    import errno as _errno
    from evolve_admin.web import unix_socket_server as uss

    def _no_fds(*_a, **_kw):
        raise OSError(_errno.EMFILE, "Too many open files")

    monkeypatch.setattr(uss.socket, "socket", _no_fds)
    assert uss._probe_socket_alive("/tmp/whatever.sock", timeout=0.1) is False


def test_watchdog_sets_rebind_request_before_shutdown():
    """Ordering contract: the watchdog must set rebind_request BEFORE
    calling shutdown(), or the recovery wrapper races the clean return
    and may exit permanently."""
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    rebind_request = _th.Event()
    stop_event = _th.Event()
    seen = {"set_at_shutdown": None}

    class _ServerStub:
        def shutdown(self):
            seen["set_at_shutdown"] = rebind_request.is_set()
            stop_event.set()  # end the watchdog loop

    monkeypatch_probe = lambda p, t: False  # noqa: E731
    orig_probe = uss._probe_socket_alive
    uss._probe_socket_alive = monkeypatch_probe
    try:
        uss._run_liveness_watchdog(
            "/nonexistent/sock", [_ServerStub()],
            period=0.001, timeout=0.001, failure_threshold=1,
            rebind_request=rebind_request, stop_event=stop_event,
        )
    finally:
        uss._probe_socket_alive = orig_probe

    assert seen["set_at_shutdown"] is True, (
        "rebind_request must already be set when shutdown() runs"
    )


def test_watchdog_loop_survives_probe_exception():
    """An unexpected exception in a watchdog cycle must not kill the
    watchdog thread — it logs and keeps probing."""
    import threading as _th
    from evolve_admin.web import unix_socket_server as uss

    stop_event = _th.Event()
    calls = {"n": 0}

    def exploding_probe(_p, _t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("probe blew up")
        stop_event.set()  # second cycle ran → the loop survived
        return True

    orig_probe = uss._probe_socket_alive
    uss._probe_socket_alive = exploding_probe
    try:
        uss._run_liveness_watchdog(
            "/nonexistent/sock", [None],
            period=0.001, timeout=0.001, failure_threshold=2,
            stop_event=stop_event,
        )
    finally:
        uss._probe_socket_alive = orig_probe

    assert calls["n"] >= 2, "the watchdog died on the first probe exception"


# ── Fallback telemetry — a daemon outage must never be mute ─────────────────


def test_try_daemon_call_fallback_logs_structured_warning(monkeypatch, caplog):
    """Every AdminDaemonUnavailable fallback in try_daemon_call emits one
    WARNING with the socket path and errno. During the 2026-07-28 outage
    the silent catch hid 10 days of ECONNREFUSED fallbacks."""
    import errno as _errno
    import logging
    from evolve_admin.evo import admin_client

    def _refused(*_a, **_kw):
        err = ConnectionRefusedError(_errno.ECONNREFUSED, "Connection refused")
        raise AdminDaemonUnavailable("cannot connect to admin daemon socket") from err

    monkeypatch.setattr(admin_client, "get_json", _refused)
    with caplog.at_level(logging.WARNING, logger="evolve_admin.evo.admin_client"):
        used, status, body = admin_client.try_daemon_call("GET", "/api/status")

    assert (used, status, body) == (False, None, None)
    msgs = [
        r.getMessage() for r in caplog.records
        if "falling back to legacy path" in r.getMessage()
    ]
    assert len(msgs) == 1, f"expected exactly one fallback WARNING; got {msgs}"
    assert str(admin_client.DEFAULT_SOCKET_PATH) in msgs[0]
    assert f"errno={_errno.ECONNREFUSED}" in msgs[0]
    assert "GET /api/status" in msgs[0]


def test_try_daemon_call_unexpected_exception_logs_warning(monkeypatch, caplog):
    """The defensive catch-all fallback logs too (with traceback) — it was
    equally silent pre-incident."""
    import logging
    from evolve_admin.evo import admin_client

    def _boom(*_a, **_kw):
        raise ValueError("client bug")

    monkeypatch.setattr(admin_client, "get_json", _boom)
    with caplog.at_level(logging.WARNING, logger="evolve_admin.evo.admin_client"):
        used, status, body = admin_client.try_daemon_call("GET", "/api/status")

    assert (used, status, body) == (False, None, None)
    assert any(
        "raised unexpectedly" in r.getMessage() and r.exc_info
        for r in caplog.records
    )
