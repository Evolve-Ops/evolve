"""tests/test_host_power.py — Phase 8.2 dedicated-Apple host posture helpers.

Covers the pure parsers (pmset custom profile, battery detection, portable
classification) and the Homebrew-prefix resolution that fixed the
Intel-breaking `npm --prefix=/opt/homebrew` hardcode. All subprocess reads
are stubbed — no pmset/sysctl runs in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import host_power  # noqa: E402


# Captured from a real MacBookPro18,2 on macOS 14 — multi-word keys
# ('Sleep On Power Button') and a non-integer value (hibernatefile path).
_PMSET_CUSTOM_PORTABLE = """\
Battery Power:
 Sleep On Power Button 1
 powermode            1
 hibernatefile        /var/vm/sleepimage
 displaysleep         30
 sleep                1
 disksleep            10
AC Power:
 Sleep On Power Button 1
 powermode            0
 hibernatefile        /var/vm/sleepimage
 displaysleep         60
 sleep                1
 disksleep            10
"""

# Desktop Macs print a single AC section.
_PMSET_CUSTOM_DESKTOP = """\
AC Power:
 Sleep On Power Button 1
 displaysleep         10
 sleep                0
 disksleep            10
"""


class TestParsePmsetCustom:
    def test_portable_two_sections(self):
        sections = host_power.parse_pmset_custom(_PMSET_CUSTOM_PORTABLE)
        assert sections["AC Power"]["sleep"] == 1
        assert sections["AC Power"]["displaysleep"] == 60
        assert sections["Battery Power"]["sleep"] == 1
        assert sections["Battery Power"]["displaysleep"] == 30

    def test_multi_word_keys_and_non_int_values(self):
        sections = host_power.parse_pmset_custom(_PMSET_CUSTOM_PORTABLE)
        assert sections["AC Power"]["Sleep On Power Button"] == 1
        assert "hibernatefile" not in sections["AC Power"]

    def test_desktop_single_section(self):
        sections = host_power.parse_pmset_custom(_PMSET_CUSTOM_DESKTOP)
        assert sections["AC Power"]["sleep"] == 0
        assert "Battery Power" not in sections

    def test_headerless_output_defaults_to_ac(self):
        sections = host_power.parse_pmset_custom(" sleep  5\n displaysleep 10\n")
        assert sections["AC Power"]["sleep"] == 5

    def test_empty_output(self):
        assert host_power.parse_pmset_custom("") == {}


class TestBatteryAndPortable:
    def test_battery_detected(self, monkeypatch):
        monkeypatch.setattr(
            host_power, "_run",
            lambda cmd, timeout=10: (
                "Now drawing from 'AC Power'\n"
                " -InternalBattery-0 (id=39845987)\t100%; charged; 0:00 remaining present: true\n"
            ),
        )
        assert host_power.has_internal_battery() is True

    def test_no_battery(self, monkeypatch):
        monkeypatch.setattr(
            host_power, "_run",
            lambda cmd, timeout=10: "Now drawing from 'AC Power'\n",
        )
        assert host_power.has_internal_battery() is False

    def test_portable_by_battery(self):
        assert host_power.is_portable(model="Macmini9,1", battery=True) is True

    def test_portable_by_model_string(self):
        # SMC-deprioritized battery: pmset misses it, the model string catches it.
        assert host_power.is_portable(model="MacBookPro18,2", battery=False) is True

    def test_desktop(self):
        assert host_power.is_portable(model="Macmini9,1", battery=False) is False

    def test_on_ac_power(self, monkeypatch):
        monkeypatch.setattr(
            host_power, "_run",
            lambda cmd, timeout=10: "Now drawing from 'Battery Power'\n -InternalBattery-0 ...\n",
        )
        assert host_power.on_ac_power() is False


class TestSleepDisabled:
    def test_set(self, monkeypatch):
        monkeypatch.setattr(
            host_power, "_run",
            lambda cmd, timeout=10: "System-wide power settings:\n SleepDisabled\t\t1\nCurrently in use:\n",
        )
        assert host_power.sleep_disabled() is True

    def test_absent(self, monkeypatch):
        monkeypatch.setattr(host_power, "_run", lambda cmd, timeout=10: "Currently in use:\n sleep 0\n")
        assert host_power.sleep_disabled() is False


class TestHomebrewPrefix:
    def test_apple_silicon_brew(self):
        prefix = host_power._homebrew_prefix_impl(
            exists=lambda p: p == "/opt/homebrew/bin/brew", machine="arm64")
        assert prefix == "/opt/homebrew"

    def test_intel_brew(self):
        # The Phase 8.2 fix: Intel Homebrew lives at /usr/local, and the old
        # hardcoded --prefix=/opt/homebrew orphaned fresh Intel installs.
        prefix = host_power._homebrew_prefix_impl(
            exists=lambda p: p == "/usr/local/bin/brew", machine="x86_64")
        assert prefix == "/usr/local"

    def test_no_brew_falls_back_by_arch(self):
        assert host_power._homebrew_prefix_impl(exists=lambda p: False, machine="arm64") == "/opt/homebrew"
        assert host_power._homebrew_prefix_impl(exists=lambda p: False, machine="x86_64") == "/usr/local"

    def test_both_installed_prefers_native(self):
        # arm64 box with a Rosetta /usr/local brew alongside — native wins.
        prefix = host_power._homebrew_prefix_impl(exists=lambda p: True, machine="arm64")
        assert prefix == "/opt/homebrew"


class TestPowerPosture:
    def test_assembles_reads(self, monkeypatch):
        monkeypatch.setattr(host_power, "hardware_model", lambda: "MacBookPro18,2")
        monkeypatch.setattr(host_power, "has_internal_battery", lambda: True)
        monkeypatch.setattr(host_power, "on_ac_power", lambda: True)
        monkeypatch.setattr(host_power, "sleep_disabled", lambda: False)
        monkeypatch.setattr(
            host_power, "ac_power_settings", lambda: {"sleep": 1, "displaysleep": 60})
        posture = host_power.power_posture()
        assert posture == {
            "hardware_model": "MacBookPro18,2",
            "apple_silicon": host_power.is_apple_silicon(),
            "has_battery": True,
            "portable": True,
            "on_ac_power": True,
            "ac_sleep": 1,
            "ac_displaysleep": 60,
            "sleep_disabled": False,
        }

    def test_pmset_unreadable_yields_none_timers(self, monkeypatch):
        monkeypatch.setattr(host_power, "hardware_model", lambda: "Macmini9,1")
        monkeypatch.setattr(host_power, "has_internal_battery", lambda: False)
        monkeypatch.setattr(host_power, "on_ac_power", lambda: True)
        monkeypatch.setattr(host_power, "sleep_disabled", lambda: False)
        monkeypatch.setattr(host_power, "ac_power_settings", lambda: {})
        posture = host_power.power_posture()
        assert posture["ac_sleep"] is None
        assert posture["portable"] is False
