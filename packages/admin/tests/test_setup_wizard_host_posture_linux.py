"""tests/test_setup_wizard_host_posture_linux.py — Linux port L3 / W1.

The Linux-profile side of Steps 6 (host power & sleep) and 7 (dedicated-host
acknowledgment). The macOS goldens are proven *unchanged* by the existing
``test_host_power.py`` / ``test_setup_wizard_host_posture.py`` suites running
under the conftest macOS pin; this module pins the LINUX profile to exercise
the new always-on no-op backend and the SSH-operator ack copy, and re-asserts
the macOS copy so the platform divergence is locked from both sides.

Design: internal/design-linux-port-2026-06-10.md §1 (SSH-operator topology),
census §3 W1 row. No pmset/sysctl ever runs — the Linux path must not touch
host-power subprocesses at all.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import host_power  # noqa: E402
from evolve_admin import setup_wizard as wizard  # noqa: E402
from evolve_admin.setup_wizard import (  # noqa: E402
    _run_dedication_ack_step,
    _run_power_posture_step,
)


@pytest.fixture
def linux_profile():
    """Pin the LINUX profile for the test body (the conftest autouse fixture
    restores MACOS on teardown). Also clears any host-power override so the
    profile-keyed default is what resolves."""
    from platform_profile import LINUX, set_profile

    host_power.set_host_power(None)
    set_profile(LINUX)
    yield
    host_power.set_host_power(None)
    # profile is restored to MACOS by the conftest per-test teardown.


@pytest.fixture
def macos_profile():
    """Explicit MACOS pin (redundant with conftest, but makes the
    divergence-lock tests self-documenting)."""
    from platform_profile import MACOS, set_profile

    host_power.set_host_power(None)
    set_profile(MACOS)
    yield
    host_power.set_host_power(None)


def _capture(fn, capsys) -> str:
    """Run a wizard step under a wide console (so Rich doesn't soft-wrap the
    assertion targets) and return stdout with the Rich panel border drawn
    away. Box-drawing characters (the ``│`` side rails) sit between words
    when prose wraps across panel lines, so we strip the whole box-drawing
    block (U+2500–U+257F) before collapsing whitespace — that reconstructs
    multi-line phrases for plain substring matching."""
    original_width = wizard.console.width
    try:
        wizard.console.width = 240
        fn()
    finally:
        wizard.console.width = original_width
    raw = capsys.readouterr().out
    no_box = re.sub(r"[─-╿]", " ", raw)
    return re.sub(r"\s+", " ", no_box)


# ── The platform backend (HostPower adapter) ──────────────────────────────────


class TestHostPowerBackend:
    def test_macos_profile_selects_pmset_backend(self, macos_profile):
        hp = host_power.get_host_power()
        assert isinstance(hp, host_power.MacOSHostPower)
        assert hp.manages_sleep() is True

    def test_linux_profile_selects_alwayson_backend(self, linux_profile):
        hp = host_power.get_host_power()
        assert isinstance(hp, host_power.LinuxHostPower)
        assert hp.manages_sleep() is False

    def test_macos_backend_delegates_to_module_funcs(self, macos_profile):
        # The MacOS backend is a thin pass-through: patching the module
        # function must flow through the adapter method (byte-identity seam).
        from unittest.mock import patch

        hp = host_power.get_host_power()
        with patch.object(host_power, "set_never_sleep_on_ac",
                          return_value=(True, "")) as m:
            assert hp.set_never_sleep_on_ac() == (True, "")
        m.assert_called_once()

    def test_linux_posture_is_alwayson_with_macos_key_shape(self, linux_profile):
        posture = host_power.get_host_power().power_posture()
        # Same keys as the macOS posture dict so network.json `host` readers
        # stay platform-neutral.
        assert set(posture) == {
            "hardware_model", "apple_silicon", "has_battery", "portable",
            "on_ac_power", "ac_sleep", "ac_displaysleep", "sleep_disabled",
        }
        # Honest always-on values: never sleeps, not portable, on power.
        assert posture["ac_sleep"] == 0
        assert posture["sleep_disabled"] is True
        assert posture["on_ac_power"] is True
        assert posture["portable"] is False
        assert posture["has_battery"] is False

    def test_linux_set_never_sleep_is_a_noop_success(self, linux_profile):
        # Never called by the wizard on Linux, but the Protocol contract must
        # stay honest: nothing to do on an always-on host → succeed.
        assert host_power.get_host_power().set_never_sleep_on_ac() == (True, "")

    def test_set_host_power_override_wins(self, macos_profile):
        sentinel = host_power.LinuxHostPower()
        host_power.set_host_power(sentinel)
        try:
            assert host_power.get_host_power() is sentinel
        finally:
            host_power.set_host_power(None)


# ── Step 6: power posture on Linux (the skip path) ────────────────────────────


class TestPowerPostureStepLinux:
    def test_renders_alwayson_skip_not_pmset_offer(self, linux_profile, capsys):
        out = _capture(lambda: _run_power_posture_step(non_interactive=True), capsys)
        assert "Always-on host" in out
        assert "no sleep management needed" in out
        # The macOS pmset framing must NOT appear on Linux.
        assert "pmset" not in out
        assert "sleep after" not in out

    def test_returns_alwayson_posture_record(self, linux_profile):
        posture = _run_power_posture_step(non_interactive=True)
        assert posture["ac_sleep"] == 0
        assert posture["sleep_disabled"] is True

    def test_never_touches_host_power_subprocess(self, linux_profile, monkeypatch):
        # The Linux path is a pure report — no pmset/sysctl spawn at all.
        def _boom(*a, **k):  # pragma: no cover — exists to fail loudly
            raise AssertionError(f"unexpected subprocess spawn on Linux path: {a!r}")

        monkeypatch.setattr(host_power, "_run", _boom)
        monkeypatch.setattr(subprocess, "run", _boom)
        # set_never_sleep_on_ac would be the only writer; assert it never fires.
        from unittest.mock import patch

        with patch.object(host_power, "set_never_sleep_on_ac") as set_sleep:
            _run_power_posture_step(non_interactive=True)
        set_sleep.assert_not_called()


# ── Step 7: dedicated-host acknowledgment copy ────────────────────────────────


class TestDedicationAckStepLinux:
    def test_linux_copy_is_ssh_operator_framed(self, linux_profile, capsys):
        out = _capture(
            lambda: _run_dedication_ack_step(
                non_interactive=True, existing_host={}, admin_user="ubuntu"),
            capsys,
        )
        # SSH-operator / VPS framing (design §1) is present…
        assert "SSH" in out
        assert "anyone who can SSH in is the operator" in out
        assert "127.0.0.1" in out
        assert "this host is" in out
        # …and the macOS chassis framing is gone.
        assert "MacBook" not in out
        assert "Mac mini" not in out

    def test_records_acknowledgment(self, linux_profile):
        ack = _run_dedication_ack_step(
            non_interactive=True, existing_host={}, admin_user="ubuntu")
        assert ack["acknowledged"] is True
        assert ack["acknowledged_by"] == "ubuntu"
        assert ack["mode"] == "non-interactive"
        assert ack["acknowledged_at"]

    def test_decline_is_recorded_and_never_blocks(self, linux_profile):
        from unittest.mock import patch

        with patch("evolve_admin.setup_wizard._confirm", return_value=False):
            ack = _run_dedication_ack_step(
                non_interactive=False, existing_host={}, admin_user="ubuntu")
        assert ack["acknowledged"] is False  # recorded honestly; no sys.exit

    def test_repair_rerun_reuses_prior_ack(self, linux_profile):
        prior = {
            "acknowledged": True,
            "acknowledged_at": "2026-06-16T00:00:00+00:00",
            "acknowledged_by": "ubuntu",
            "mode": "interactive",
        }
        from unittest.mock import patch

        with patch("evolve_admin.setup_wizard._confirm") as confirm:
            ack = _run_dedication_ack_step(
                non_interactive=False,
                existing_host={"dedication_ack": prior},
                admin_user="someone-else",
            )
        confirm.assert_not_called()
        assert ack == prior


# ── Divergence lock: the macOS copy keeps the chassis framing ─────────────────


class TestDedicationAckStepMacOSUnchanged:
    def test_macos_copy_is_chassis_framed_no_ssh(self, macos_profile, capsys):
        out = _capture(
            lambda: _run_dedication_ack_step(
                non_interactive=True, existing_host={}, admin_user="pod-admin"),
            capsys,
        )
        # The macOS dedicated-Mac chassis framing is intact…
        assert "this Mac is" in out
        assert "retired MacBook" in out
        assert "Mac mini" in out
        # …and the Linux SSH-operator paragraph never leaks onto macOS.
        assert "SSH in is the operator" not in out
        assert "ssh -L" not in out
