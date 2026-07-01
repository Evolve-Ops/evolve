"""Both-profile tests for the wizard add-bot account/gateway provisioning.

Covers W10-A findings #4 (the `/Users/<bot>` chown leak), #5 ("Creating macOS
user" copy on Linux), and the #1 deferred-deploy clarity, all of which live in
``wizard._provision_account_and_gateway`` (extracted from ``_create_bot_flow``
section 9 so it can be driven without the full interactive flow).

The invariant: on Linux the chown targets ``user_home(name)/.openclaw`` via
``get_profile().chown`` (NOT ``/Users``, NOT a bare ``chown``) and the copy
says "Linux"; on macOS the argv + copy stay byte-identical to today. A fresh
pod (deploy venv not yet built) defers the gateway install with an INFO note
instead of shelling out to a not-yet-present ``evolve-admin``.

Placeholder bot name per docs/PLACEHOLDER_NAMING.md (never a real bot name —
the scrub gate enforces this).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from platform_profile import LINUX, MACOS, get_profile, set_profile  # noqa: E402

from evolve_admin import wizard  # noqa: E402

BOT = "team-bot-a"


class _FakeIso:
    """Minimal isolation seam stand-in: account doesn't exist, creation OK."""

    def __init__(self) -> None:
        self.created: "list[tuple[str, int]]" = []

    def user_exists(self, name: str) -> bool:
        return False

    def next_free_uid(self) -> int:
        return 510

    def create_user(self, name: str, uid: int) -> None:
        self.created.append((name, uid))


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


# The wizard hands sudo an ABSOLUTE evolve-admin path (FIND-A) — a bare name
# dies under sudo's secure_path on a uv-sync pod. The resolver is stubbed so the
# test pins the call shape without depending on the test runner's interpreter.
_RESOLVED_EA = "/var/lib/evolve/repo/.venv/bin/evolve-admin"


def _drive(monkeypatch, profile, *, venv_present: bool):
    """Run ``_provision_account_and_gateway`` under a pinned profile with the
    isolation seam + subprocess + filesystem all faked. Returns (argv calls,
    printed lines)."""
    set_profile(profile)
    venv_python = get_profile().venv_python

    calls: "list[list[str]]" = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result()

    def fake_exists(self) -> bool:
        # Only the deploy-venv python "exists" (and only when asked); the
        # per-bot daemon units are always absent so the missing-unit branch
        # is exercised on both platforms.
        return venv_present and str(self) == venv_python

    printed: "list[str]" = []

    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(wizard, "get_isolation", lambda: _FakeIso())
    monkeypatch.setattr(wizard, "resolve_evolve_admin_bin", lambda: _RESOLVED_EA)
    monkeypatch.setattr(Path, "exists", fake_exists)
    for fn in ("_info", "_ok", "_warn", "_skip"):
        monkeypatch.setattr(wizard, fn, lambda msg, _p=printed: _p.append(str(msg)))

    wizard._provision_account_and_gateway(BOT)
    return calls, printed


def _chown_argv(calls: "list[list[str]]") -> "list[str]":
    for c in calls:
        if len(c) >= 2 and c[1].endswith("chown"):
            return c
    raise AssertionError(f"no chown invocation recorded in {calls}")


# ── #4: chown targets the profile-resolved account home, not /Users ──────────


def test_linux_chown_targets_account_home_via_profile_binary(monkeypatch) -> None:
    calls, _ = _drive(monkeypatch, LINUX, venv_present=False)
    argv = _chown_argv(calls)
    # /usr/bin/chown (Linux), pwd-fallback home /home/<name>, :staff literal.
    assert argv == [
        "sudo", "/usr/bin/chown", "-R", f"{BOT}:staff", f"/home/{BOT}/.openclaw",
    ]
    joined = " ".join(argv)
    assert "/Users/" not in joined, "macOS home path leaked into the Linux chown"
    assert argv[1] != "chown", "bare chown — must route through profile.chown"


def test_macos_chown_argv_byte_identical(monkeypatch) -> None:
    calls, _ = _drive(monkeypatch, MACOS, venv_present=False)
    argv = _chown_argv(calls)
    # Exactly today's macOS argv (/usr/sbin/chown, /Users/<name>/.openclaw).
    assert argv == [
        "sudo", "/usr/sbin/chown", "-R", f"{BOT}:staff", f"/Users/{BOT}/.openclaw",
    ]


# ── #5: account-creation copy is platform-keyed ──────────────────────────────


def test_linux_copy_says_linux_not_macos(monkeypatch) -> None:
    _, printed = _drive(monkeypatch, LINUX, venv_present=False)
    joined = "\n".join(printed)
    assert f"Creating Linux user '{BOT}'" in joined
    assert f"Linux user '{BOT}' created" in joined
    assert "macOS" not in joined, "macOS noun leaked into Linux output"


def test_macos_copy_says_macos(monkeypatch) -> None:
    _, printed = _drive(monkeypatch, MACOS, venv_present=False)
    joined = "\n".join(printed)
    assert f"Creating macOS user '{BOT}'" in joined
    assert f"macOS user '{BOT}' created" in joined


# ── #1: deferred-vs-real gateway install keyed on venv existence ─────────────


def test_deferred_install_when_venv_absent_skips_evolve_admin(monkeypatch) -> None:
    calls, printed = _drive(monkeypatch, LINUX, venv_present=False)
    assert not any("evolve-admin" in tok for cmd in calls for tok in cmd), (
        "should NOT shell out to evolve-admin when the deploy venv is absent"
    )
    joined = "\n".join(printed).lower()
    assert "deferred" in joined, "absent venv should produce a clear deferred note"


def test_real_deploy_runs_when_venv_present(monkeypatch) -> None:
    calls, _ = _drive(monkeypatch, LINUX, venv_present=True)
    # FIND-A: the deploy shells out to the RESOLVED absolute evolve-admin path,
    # never a bare "evolve-admin" (which dies off sudo's secure_path).
    deploy_calls = [c for c in calls if c[:1] == ["sudo"] and c[2:] == ["deploy", BOT]]
    assert deploy_calls == [["sudo", _RESOLVED_EA, "deploy", BOT]]
    assert all(c[1] != "evolve-admin" for c in deploy_calls), (
        "deploy must not invoke a bare 'evolve-admin' off PATH"
    )


# ── #1: missing-daemon check is platform-keyed (dir + unit suffix) ───────────


def test_linux_missing_unit_uses_systemd_service_path(monkeypatch) -> None:
    _, printed = _drive(monkeypatch, LINUX, venv_present=False)
    joined = "\n".join(printed)
    assert (
        f"/etc/systemd/system/ai.openclaw.evolve.apply.{BOT}.service" in joined
    )
    assert f"/etc/systemd/system/ai.openclaw.evolve.test.{BOT}.service" in joined
    assert ".plist" not in joined, "macOS plist suffix leaked into Linux output"
    assert "/Library/LaunchDaemons" not in joined
    assert "systemd jobs" in joined


def test_macos_missing_unit_uses_launchd_plist_path(monkeypatch) -> None:
    _, printed = _drive(monkeypatch, MACOS, venv_present=False)
    joined = "\n".join(printed)
    assert (
        f"/Library/LaunchDaemons/ai.openclaw.evolve.apply.{BOT}.plist" in joined
    )
    assert "launchd jobs" in joined


# ── platform-keyed account noun (sibling of #3180 health.py fix) ──────────────
#
# `_os_user_noun()` is the single source for the operator-facing OS noun used by
# the interactive account-creation strings. The duplicate-account warning in
# `_create_bot_flow` (was a hardcoded "macOS user ...") and the manual-bot-entry
# prompt in `run_wizard` both interpolate it, so locking the helper here pins the
# vocabulary both sites print.


def test_os_user_noun_linux() -> None:
    set_profile(LINUX)
    assert wizard._os_user_noun() == "Linux"


def test_os_user_noun_macos() -> None:
    # MACOS is the conftest default, but assert it explicitly so the lock is
    # self-contained.
    set_profile(MACOS)
    assert wizard._os_user_noun() == "macOS"


class _ExistingIso:
    """Isolation seam stand-in where the chosen account already exists."""

    def user_exists(self, name: str) -> bool:
        return True


def _drive_create_bot_flow_existing_name(monkeypatch, profile) -> "list[str]":
    """Drive ``_create_bot_flow`` just far enough to print the duplicate-account
    warning, under a pinned profile, then cancel.

    The first name prompt returns an already-existing account (the isolation
    seam says so → warning fires); the operator declines "use anyway", so the
    name loop repeats and the second prompt returns "" → the flow cancels and
    returns None. Net effect: exactly the one warning we want to assert, no
    privileged side effects. Returns the captured output lines.
    """
    set_profile(profile)

    asks = iter([BOT, ""])  # 1st: existing name → warning; 2nd: empty → cancel

    def fake_ask(prompt, default=""):
        return next(asks)

    printed: "list[str]" = []

    monkeypatch.setattr(wizard, "get_isolation", lambda: _ExistingIso())
    monkeypatch.setattr(wizard, "_ask", fake_ask)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    for fn in ("_info", "_ok", "_warn", "_skip", "_header"):
        monkeypatch.setattr(
            wizard, fn, lambda msg="", _p=printed: _p.append(str(msg))
        )

    result = wizard._create_bot_flow([])
    assert result is None, "declining the existing name then Enter should cancel"
    return printed


def test_create_bot_flow_existing_warning_says_linux(monkeypatch) -> None:
    printed = _drive_create_bot_flow_existing_name(monkeypatch, LINUX)
    joined = "\n".join(printed)
    assert f"Linux user '{BOT}' already exists." in joined
    assert "macOS" not in joined, "macOS noun leaked into Linux add-bot warning"


def test_create_bot_flow_existing_warning_says_macos(monkeypatch) -> None:
    printed = _drive_create_bot_flow_existing_name(monkeypatch, MACOS)
    joined = "\n".join(printed)
    # Byte-identical to today's macOS copy.
    assert f"macOS user '{BOT}' already exists." in joined
