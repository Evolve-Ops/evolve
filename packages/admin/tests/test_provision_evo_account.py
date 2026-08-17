"""Tests for setup_wizard._provision_evo_account (Phase E.2.a).

Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.a".

The helper creates the `evo` macOS account, an empty `.openclaw/`
directory tree, and grants the admin daemon ACL read access. It does
NOT write openclaw.json or change any plist — those land in E.2.b's
cutover after E.3's endpoints are in place.

Tests mock subprocess.run + dscl probes so they run anywhere — no real
macOS user manipulation required.
"""
from __future__ import annotations

from evolve_admin.setup_wizard import _provision_evo_account


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Result:
    """Minimal stand-in for subprocess.run's CompletedProcess."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(
    *,
    user_exists: bool = False,
    chown_ok: bool = True,
    mkdir_ok: bool = True,
    dscl_create_ok: bool = True,
    createhomedir_ok: bool = True,
    chmod_ok: bool = True,
):
    """Build a fake subprocess.run that produces controlled responses.

    Records call args so tests can assert sequence + commands.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # dscl . -read /Users/evo UniqueID → exists? (isolation-seam probe)
        if cmd[:3] == ["dscl", ".", "-read"]:
            return _Result(returncode=0 if user_exists else 1)
        # dscl . -list /Users UniqueID → list of UIDs (for next_free_uid)
        if cmd[:4] == ["dscl", ".", "-list", "/Users"]:
            return _Result(returncode=0, stdout="root 0\nadmin 501\n")
        # sudo dscl create
        if cmd[:3] == ["sudo", "/usr/bin/dscl", "."]:
            return _Result(returncode=0 if dscl_create_ok else 1, stderr="")
        # sudo /usr/sbin/createhomedir (full path — sudo secure_path
        # on production pods omits /usr/sbin, so bare name 404s).
        if cmd[:2] == ["sudo", "/usr/sbin/createhomedir"]:
            return _Result(returncode=0 if createhomedir_ok else 1)
        # sudo /bin/mkdir
        if cmd[:3] == ["sudo", "/bin/mkdir", "-p"]:
            return _Result(returncode=0 if mkdir_ok else 1)
        # sudo /usr/sbin/chown
        if cmd[:2] == ["sudo", "/usr/sbin/chown"]:
            return _Result(returncode=0 if chown_ok else 1)
        # sudo /bin/chmod
        if cmd[:2] == ["sudo", "/bin/chmod"]:
            return _Result(returncode=0 if chmod_ok else 1)
        # Anything else (set_evolve_read_acl etc.) — passthrough OK
        return _Result(returncode=0)

    return fake_run, calls


# ── Successful provisioning path ─────────────────────────────────────────────


def test_provision_succeeds_on_fresh_install(monkeypatch):
    """No 'evo' user, every subprocess succeeds → returns True."""
    fake_run, calls = _make_fake_run(user_exists=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    # set_evolve_read_acl uses its own subprocess.run inside deploy.py;
    # stub it out so the test doesn't try to chmod +a on a fake bot home.
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl",
        lambda bot_id: None,
    )

    result = _provision_evo_account()
    assert result is True

    # dscl create the user
    dscl_creates = [c for c in calls if c[:3] == ["sudo", "/usr/bin/dscl", "."]]
    assert len(dscl_creates) >= 5, (
        f"expected at least 5 dscl create commands; got: {dscl_creates}"
    )

    # mkdir -p for every required subdir
    mkdir_calls = [c for c in calls if c[:3] == ["sudo", "/bin/mkdir", "-p"]]
    mkdir_paths = [c[-1] for c in mkdir_calls]
    # Required tree
    assert "/Users/evo/.openclaw" in mkdir_paths
    assert "/Users/evo/.openclaw/workspace" in mkdir_paths
    assert "/Users/evo/.openclaw/workspace/evolve" in mkdir_paths
    assert "/Users/evo/.openclaw/logs" in mkdir_paths
    assert "/Users/evo/.openclaw/agents/main/agent" in mkdir_paths
    assert "/Users/evo/.openclaw/credentials" in mkdir_paths

    # chown to evo:staff recursive on .openclaw/
    chown_calls = [c for c in calls if c[:2] == ["sudo", "/usr/sbin/chown"]]
    assert any(
        c[2:5] == ["-R", "evo:staff", "/Users/evo/.openclaw"]
        for c in chown_calls
    ), f"expected recursive chown to evo:staff; got: {chown_calls}"


def test_provision_succeeds_on_existing_user(monkeypatch):
    """'evo' user already exists → skip dscl create, still mkdir + chown + ACL."""
    fake_run, calls = _make_fake_run(user_exists=True)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl",
        lambda bot_id: None,
    )

    result = _provision_evo_account()
    assert result is True

    # No dscl create calls because the user already exists
    dscl_creates = [
        c for c in calls
        if c[:3] == ["sudo", "/usr/bin/dscl", "."] and "-create" in c
    ]
    assert dscl_creates == [], (
        f"expected zero dscl create calls when user exists; got: {dscl_creates}"
    )

    # But mkdir + chown still happened — provisioning is idempotent on
    # the directory side too.
    mkdir_calls = [c for c in calls if c[:3] == ["sudo", "/bin/mkdir", "-p"]]
    assert len(mkdir_calls) >= 6


def test_provision_credentials_dir_gets_700_no_acl(monkeypatch):
    """credentials/ must be 700 (no inherited ACL); matches deploy invariant."""
    fake_run, calls = _make_fake_run(user_exists=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl",
        lambda bot_id: None,
    )

    _provision_evo_account()

    chmod_calls = [c for c in calls if c[:2] == ["sudo", "/bin/chmod"]]
    assert any(
        c[2] == "700" and c[3] == "/Users/evo/.openclaw/credentials"
        for c in chmod_calls
    ), f"expected chmod 700 on credentials dir; got: {chmod_calls}"


def test_provision_invokes_set_evolve_read_acl(monkeypatch):
    """The ACL grant is what makes the admin daemon (evolve user) able to
    read evo's manifests/state. Test that we actually call the helper."""
    fake_run, _calls = _make_fake_run(user_exists=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)

    acl_calls: list[str] = []

    def stub_acl(bot_id: str) -> None:
        acl_calls.append(bot_id)

    monkeypatch.setattr("evolve_admin.deploy.set_evolve_read_acl", stub_acl)

    _provision_evo_account()
    assert acl_calls == ["evo"], (
        f"expected set_evolve_read_acl('evo') called once; got: {acl_calls}"
    )


# ── Failure paths ────────────────────────────────────────────────────────────


def test_provision_fails_when_dscl_create_errors(monkeypatch):
    """dscl create returning non-zero → False, no further side effects."""
    fake_run, calls = _make_fake_run(user_exists=False, dscl_create_ok=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)

    result = _provision_evo_account()
    assert result is False

    # We stop early — no mkdir should have run
    mkdir_calls = [c for c in calls if c[:3] == ["sudo", "/bin/mkdir", "-p"]]
    assert mkdir_calls == [], (
        f"expected zero mkdir calls when dscl create fails; got: {mkdir_calls}"
    )


def test_provision_fails_when_mkdir_errors(monkeypatch):
    """mkdir failure → False, no chown."""
    fake_run, calls = _make_fake_run(user_exists=False, mkdir_ok=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)

    result = _provision_evo_account()
    assert result is False

    chown_calls = [c for c in calls if c[:2] == ["sudo", "/usr/sbin/chown"]]
    assert chown_calls == [], (
        f"expected no chown when mkdir fails; got: {chown_calls}"
    )


def test_provision_fails_when_chown_errors(monkeypatch):
    """chown failure → False."""
    fake_run, _calls = _make_fake_run(user_exists=False, chown_ok=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)

    result = _provision_evo_account()
    assert result is False


def test_provision_continues_when_acl_grant_errors(monkeypatch):
    """ACL grant failure is non-fatal — deploy.py re-applies it later."""
    fake_run, _calls = _make_fake_run(user_exists=False)
    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)

    def raising_acl(bot_id: str) -> None:
        raise RuntimeError("simulated ACL failure")

    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl", raising_acl,
    )

    # Should still return True — the function logs a warning but doesn't
    # mark the whole provisioning as failed.
    result = _provision_evo_account()
    assert result is True


# ── Sanity: idempotency on a re-run ──────────────────────────────────────────


def test_provision_idempotent_when_called_twice(monkeypatch):
    """Two calls in succession both succeed (the second sees user_exists=True)."""
    # First call: user doesn't exist; second call: it does. We approximate
    # this by alternating responses to the dscl read probe.
    call_state = {"user_check_count": 0}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["dscl", ".", "-read"]:
            call_state["user_check_count"] += 1
            # First check: user doesn't exist (rc=1). Second check: exists (rc=0).
            if call_state["user_check_count"] == 1:
                return _Result(returncode=1)
            return _Result(returncode=0)
        if cmd[:4] == ["dscl", ".", "-list", "/Users"]:
            return _Result(returncode=0, stdout="root 0\n")
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl",
        lambda bot_id: None,
    )

    assert _provision_evo_account() is True
    assert _provision_evo_account() is True
    # First call did the user check; second call did it again
    assert call_state["user_check_count"] == 2
