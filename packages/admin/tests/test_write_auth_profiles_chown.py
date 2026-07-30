"""tests/test_write_auth_profiles_chown.py

Regression guard for the EACCES bug that took atlas's Telegram bot down on
2026-05-30: web/server.py:_write_auth_profiles's docstring promised a chown
step that was never implemented. Result: `sudo cp` writes the file as
root, mode-644-as-root → bot can read but the PARENT DIRECTORY stays
root-owned, so OC's token-refresh atomic write fails:

  [diagnostic] lane task error: lane=session:agent:main:telegram:direct:...
  error="Error: EACCES: permission denied, open
  '/Users/atlas/.openclaw/agents/main/agent/auth-profiles.json.<uuid>.tmp'"

  Embedded agent failed before reply: EACCES ...

Operator sees "⚠️ Something went wrong while processing your request"
on every DM until the parent dir is chowned by hand.

The fix expands the sudo sequence to:

  1. sudo /bin/mkdir -p <parent>
  2. sudo /usr/sbin/chown <bot>:staff <parent>      ← was missing, load-bearing
  3. sudo /bin/cp /tmp/... <dest>
  4. sudo /usr/sbin/chown <bot>:staff <dest>         ← was promised, never written
  5. sudo /bin/chmod 600 <dest>                      ← was 644 → API-key file

These tests:

  - Spin up a Flask test app, monkey-patch subprocess.run to record
    every sudo invocation.
  - Hit an /api/admin/keys/<bot>/add endpoint that triggers
    _write_auth_profiles.
  - Assert the call sequence includes the two chown steps in the right
    order, with the right targets (parent dir + file, both bot-owned).
  - Cover the bot_id != bot_user case (team_bot_b-on-personal_bot_user-account memory).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _setup_fake_bot(tmp_path: Path, bot_id: str, bot_user: str | None = None):
    """Build a tmp-rooted bot home with a starter auth-profiles.json,
    plus the resolve_bot_paths return dict the route expects."""
    bot_user = bot_user or bot_id
    home = tmp_path / bot_user
    oc = home / ".openclaw"
    agent_dir = oc / "agents" / "main" / "agent"
    workspace = oc / "workspace"
    agent_dir.mkdir(parents=True)
    workspace.mkdir(parents=True)

    auth_path = agent_dir / "auth-profiles.json"
    auth_path.write_text(json.dumps({"profiles": {}}, indent=2))

    oc_path = oc / "openclaw.json"
    oc_path.write_text("{}")

    paths = {
        "auth_profiles": str(auth_path),
        "agent_dir": str(agent_dir),
        "oc_config": str(oc_path),
        "turns_dir": str(workspace / "turns"),
        "turns_dir_fallback": str(workspace / "turns"),
        "turns_dir_candidates": [str(workspace / "turns")],
        "logs_dir": str(oc / "logs"),
        "workspace": str(workspace),
        "openclaw_json": str(oc_path),
        "user": bot_user,
    }
    return {
        "home": home,
        "oc_path": oc_path,
        "auth_path": auth_path,
        "agent_dir": agent_dir,
        "paths": paths,
        "bot_id": bot_id,
        "bot_user": bot_user,
    }


def _make_fake_run(tmp_path: Path, calls_holder: list):
    """Intercept sudo cp / mkdir / chmod / chown / cat — execute the real
    filesystem operation on the tmp tree and record the call."""
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "sudo":
            calls_holder.append(list(args))
            verb = args[1] if len(args) > 1 else ""
            rest = list(args[2:])
            try:
                if verb in ("/bin/cat", "cat"):
                    text = Path(rest[0]).read_text()
                    return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
                if verb == "/bin/cp":
                    src, dst = rest[0], rest[1]
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, dst)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if verb == "/bin/mkdir":
                    target = rest[-1]
                    Path(target).mkdir(parents=True, exist_ok=True)
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                # Accept the chown binary on EITHER platform — macOS
                # (/usr/sbin/chown) and Linux (/usr/bin/chown). The point of
                # the platform-profile parity fix is that the argv tracks the
                # active profile; the fake must not assume macOS.
                if verb in ("/bin/chmod", "/usr/sbin/chown", "/usr/bin/chown"):
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            except Exception as exc:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))
        return real_run(args, *a, **kw)

    return fake_run


@pytest.fixture
def app_and_bot(tmp_path, monkeypatch):
    bot = _setup_fake_bot(tmp_path, bot_id="atlas")
    from evolve_admin.web import server as srv

    monkeypatch.setattr(
        srv, "resolve_bot_paths", lambda b, user=None: bot["paths"],
    )

    calls: list[list] = []
    monkeypatch.setattr(subprocess, "run", _make_fake_run(tmp_path, calls))
    monkeypatch.setattr("subprocess.run", _make_fake_run(tmp_path, calls))

    network = {"bots": {bot["bot_id"]: {"user": bot["bot_user"]}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app
    app = srv.create_app(network_path)
    app.config["TESTING"] = True
    return app, bot, calls


# ─── _write_auth_profiles sudo sequence ──────────────────────────────────────


def test_write_auth_profiles_chowns_parent_then_file(app_and_bot):
    """The fix requires TWO chowns: parent dir (so OC can atomic-write
    tmp files there) and the file itself (so the bot owns its credentials).
    Both must come from /usr/sbin/chown, both must target <bot_user>:staff."""
    app, bot, calls = app_and_bot

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot['bot_id']}/add",
            json={
                "provider": "anthropic",
                "key_type": "api_key",
                "key_value": "sk-test-x",
            },
        )
        assert resp.status_code == 200, resp.get_json()

    # Filter to the sudo calls _write_auth_profiles is responsible for —
    # those targeting the auth_profiles path or its parent.
    auth_path = bot["paths"]["auth_profiles"]
    agent_dir = bot["paths"]["agent_dir"]
    relevant = [
        c_ for c_ in calls
        if len(c_) >= 3 and any(
            arg in (auth_path, agent_dir) for arg in c_
        )
    ]
    # Sequence must include mkdir, chown(parent), cp, chown(file), chmod.
    ops = [(c_[1], c_[-1]) for c_ in relevant]

    # mkdir on the parent
    assert ("/bin/mkdir", agent_dir) in ops, ops
    # chown on the parent (the load-bearing fix)
    assert ("/usr/sbin/chown", agent_dir) in ops, (
        f"missing parent-dir chown — OC can't atomic-write tmp files in "
        f"a root-owned parent. ops={ops!r}"
    )
    # cp + chown(file) + chmod(600) on the file
    assert ("/bin/cp", auth_path) in ops, ops
    assert ("/usr/sbin/chown", auth_path) in ops, (
        f"missing file chown — bot user can't open its own auth-profiles. "
        f"ops={ops!r}"
    )
    assert ("/bin/chmod", auth_path) in ops, ops


def test_write_auth_profiles_chown_parent_before_cp(app_and_bot):
    """The parent chown must run BEFORE the cp — if cp runs against a
    root-owned parent, the atomic write would already be at risk if OC
    refreshed concurrently. Order is the cheap insurance."""
    app, bot, calls = app_and_bot

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot['bot_id']}/add",
            json={
                "provider": "anthropic",
                "key_type": "api_key",
                "key_value": "sk-test-y",
            },
        )
        assert resp.status_code == 200

    auth_path = bot["paths"]["auth_profiles"]
    agent_dir = bot["paths"]["agent_dir"]

    # Index of the first chown of the parent
    chown_parent_idx = next(
        i for i, c_ in enumerate(calls)
        if c_[1] == "/usr/sbin/chown" and c_[-1] == agent_dir
    )
    # Index of the cp into the auth_profiles path
    cp_idx = next(
        i for i, c_ in enumerate(calls)
        if c_[1] == "/bin/cp" and c_[-1] == auth_path
    )
    assert chown_parent_idx < cp_idx, (
        f"parent chown (idx {chown_parent_idx}) must precede cp "
        f"(idx {cp_idx})"
    )


def test_write_auth_profiles_chmod_is_600_not_644(app_and_bot):
    """auth-profiles.json carries API keys — file must be locked 600
    after the chown. Previous code used 644 which leaked the file to
    anyone who could traverse /Users/<bot>/."""
    app, bot, calls = app_and_bot

    with app.test_client() as c:
        c.post(
            f"/api/admin/keys/{bot['bot_id']}/add",
            json={
                "provider": "anthropic",
                "key_type": "api_key",
                "key_value": "sk-test-z",
            },
        )

    auth_path = bot["paths"]["auth_profiles"]
    chmod_calls = [c_ for c_ in calls if c_[1] == "/bin/chmod" and c_[-1] == auth_path]
    assert chmod_calls, "chmod was not called on auth-profiles.json"
    # mode is the second arg after "/bin/chmod"
    modes = [c_[2] for c_ in chmod_calls]
    assert "600" in modes, (
        f"auth-profiles.json must be chmod 600 (API keys), got {modes!r}"
    )
    assert "644" not in modes, (
        f"chmod 644 is the pre-fix shape that leaks the API-key file; "
        f"got {modes!r}"
    )


def test_write_auth_profiles_chown_uses_bot_user_not_bot_id(tmp_path, monkeypatch):
    """When bot_id != bot_user (team_bot_b runs on the personal_bot_user macOS account),
    the chown target must use the *user*, not the bot_id. Regression
    guard for the bot_id ≠ macOS-account rule."""
    bot = _setup_fake_bot(tmp_path, bot_id="team_bot_b", bot_user="personal_bot_user")
    from evolve_admin.web import server as srv

    monkeypatch.setattr(
        srv, "resolve_bot_paths", lambda b, user=None: bot["paths"],
    )

    calls: list[list] = []
    monkeypatch.setattr(subprocess, "run", _make_fake_run(tmp_path, calls))
    monkeypatch.setattr("subprocess.run", _make_fake_run(tmp_path, calls))

    network = {"bots": {bot["bot_id"]: {"user": bot["bot_user"]}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app
    app = create_app(network_path)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot['bot_id']}/add",
            json={
                "provider": "anthropic",
                "key_type": "api_key",
                "key_value": "sk-test-team_bot_b",
            },
        )
        assert resp.status_code == 200, resp.get_json()

    # Every chown call's user-spec arg must be "personal_bot_user:staff" (the
    # macOS account), not "team_bot_b:staff" (the bot_id).
    chowns = [c_ for c_ in calls if c_[1] in ("/usr/sbin/chown", "/usr/bin/chown")]
    assert chowns, "no chown calls captured"
    for c_ in chowns:
        user_spec = c_[2]
        assert user_spec == "personal_bot_user:staff", (
            f"chown must target the macOS user (personal_bot_user), not bot_id "
            f"(team_bot_b). Got {user_spec!r} in call: {c_!r}"
        )


# ─── platform-profile chown-path parity ─────────────────────────────────────
#
# Regression guard for the 'sudo: a terminal is required to read the password'
# error on the Credentials tab on fresh Linux VPS pods. The credential-write
# path hardcoded the macOS chown binary (/usr/sbin/chown), but the
# platform-aware NOPASSWD sudoers grant rendered by
# setup_wizard._render_evolve_sudoers lists /usr/bin/chown on Linux. argv that
# does not match the grant makes sudo fall through to a password prompt; the
# admin server (evolve user) has no TTY, so sudo emits the terminal-required
# error. The fix routes the chown binary through
# platform_profile.get_profile().chown, the same single source the grant is
# derived from.


def test_write_auth_profiles_chown_uses_linux_path_under_linux_profile(
    tmp_path, monkeypatch,
):
    """Under the LINUX platform profile, the credential-write chowns MUST use
    /usr/bin/chown (the path the Linux NOPASSWD grant lists), NOT the macOS
    /usr/sbin/chown. A mismatch is exactly what produced the
    'a terminal is required to read the password' error in the field."""
    from platform_profile import LINUX, MACOS, set_profile

    bot = _setup_fake_bot(tmp_path, bot_id="atlas")
    from evolve_admin.web import server as srv

    monkeypatch.setattr(
        srv, "resolve_bot_paths", lambda b, user=None: bot["paths"],
    )

    calls: list[list] = []
    monkeypatch.setattr(subprocess, "run", _make_fake_run(tmp_path, calls))
    monkeypatch.setattr("subprocess.run", _make_fake_run(tmp_path, calls))

    network = {"bots": {bot["bot_id"]: {"user": bot["bot_user"]}}}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    from evolve_admin.web.server import create_app
    app = create_app(network_path)
    app.config["TESTING"] = True

    # Flip the active profile to Linux for the duration of the request, then
    # restore the conftest-pinned macOS profile (autouse fixture also resets).
    set_profile(LINUX)
    try:
        with app.test_client() as c:
            resp = c.post(
                f"/api/admin/keys/{bot['bot_id']}/add",
                json={
                    "provider": "anthropic",
                    "key_type": "api_key",
                    "key_value": "sk-test-linux",
                },
            )
            assert resp.status_code == 200, resp.get_json()
    finally:
        set_profile(MACOS)

    chowns = [c_ for c_ in calls if c_[1] in ("/usr/sbin/chown", "/usr/bin/chown")]
    assert chowns, "no chown calls captured"
    # Every chown must use the Linux binary path; none may use the macOS path.
    bins = {c_[1] for c_ in chowns}
    assert bins == {"/usr/bin/chown"}, (
        f"under the LINUX profile, credential-write chowns must use "
        f"/usr/bin/chown (matching the Linux NOPASSWD grant), not the macOS "
        f"/usr/sbin/chown. Got {bins!r}"
    )
    # Both load-bearing chowns (parent dir + file) must be present.
    auth_path = bot["paths"]["auth_profiles"]
    agent_dir = bot["paths"]["agent_dir"]
    targets = {c_[-1] for c_ in chowns}
    assert auth_path in targets and agent_dir in targets, (
        f"both the parent-dir and file chown must fire under Linux too; "
        f"targets={targets!r}"
    )


def test_write_auth_profiles_chown_uses_macos_path_under_macos_profile(app_and_bot):
    """The macOS profile (conftest default) keeps /usr/sbin/chown — the
    parity fix must NOT regress the existing platform."""
    app, bot, calls = app_and_bot

    with app.test_client() as c:
        resp = c.post(
            f"/api/admin/keys/{bot['bot_id']}/add",
            json={
                "provider": "anthropic",
                "key_type": "api_key",
                "key_value": "sk-test-macos",
            },
        )
        assert resp.status_code == 200, resp.get_json()

    chowns = [c_ for c_ in calls if c_[1] in ("/usr/sbin/chown", "/usr/bin/chown")]
    assert chowns, "no chown calls captured"
    bins = {c_[1] for c_ in chowns}
    assert bins == {"/usr/sbin/chown"}, (
        f"under the MACOS profile, chowns must use /usr/sbin/chown; got {bins!r}"
    )
