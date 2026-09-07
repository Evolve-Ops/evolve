"""tests/test_setup_wizard_host_posture.py — Phase 8.2 wizard steps.

Covers the two steps that widened supported hardware from "a Mac mini" to
any dedicated, always-on Mac:

* `_run_power_posture_step` — detect portable hardware, require + offer
  never-sleep-on-AC, warn-don't-block on decline.
* `_run_dedication_ack_step` — informed-operator single-tenant
  acknowledgment, recorded once and reused on repair re-runs, never blocks.

All host_power reads/writes are stubbed; nothing touches pmset.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import host_power  # noqa: E402
from evolve_admin.setup_wizard import (  # noqa: E402
    _run_dedication_ack_step,
    _run_power_posture_step,
)


def _posture(**overrides):
    base = {
        "hardware_model": "MacBookPro18,2",
        "apple_silicon": True,
        "has_battery": True,
        "portable": True,
        "on_ac_power": True,
        "ac_sleep": 1,
        "ac_displaysleep": 60,
        "sleep_disabled": False,
    }
    base.update(overrides)
    return base


class TestPowerPostureStep:
    def test_already_never_sleep_makes_no_changes(self):
        with patch.object(host_power, "power_posture", return_value=_posture(ac_sleep=0)), \
             patch.object(host_power, "set_never_sleep_on_ac") as set_sleep:
            result = _run_power_posture_step(non_interactive=True)
        set_sleep.assert_not_called()
        assert result["ac_sleep"] == 0

    def test_sleep_enabled_non_interactive_applies_pmset(self):
        # non_interactive resolves the default-True confirm — the wizard
        # configures the host, matching its handles-everything contract.
        with patch.object(host_power, "power_posture", return_value=_posture(ac_sleep=10)), \
             patch.object(host_power, "set_never_sleep_on_ac", return_value=(True, "")) as set_sleep, \
             patch.object(host_power, "ac_power_settings",
                          return_value={"sleep": 0, "displaysleep": 0}):
            result = _run_power_posture_step(non_interactive=True)
        set_sleep.assert_called_once()
        assert result["ac_sleep"] == 0
        assert result["ac_displaysleep"] == 0

    def test_operator_decline_warns_but_never_blocks(self):
        with patch.object(host_power, "power_posture", return_value=_posture(ac_sleep=10)), \
             patch.object(host_power, "set_never_sleep_on_ac") as set_sleep, \
             patch("evolve_admin.setup_wizard._confirm", return_value=False):
            result = _run_power_posture_step(non_interactive=False)
        set_sleep.assert_not_called()
        assert result["ac_sleep"] == 10  # recorded honestly, install continues

    def test_pmset_failure_does_not_raise(self):
        with patch.object(host_power, "power_posture", return_value=_posture(ac_sleep=10)), \
             patch.object(host_power, "set_never_sleep_on_ac",
                          return_value=(False, "pmset: could not set")):
            result = _run_power_posture_step(non_interactive=True)
        assert result["ac_sleep"] == 10

    def test_disablesleep_counts_as_configured(self):
        with patch.object(host_power, "power_posture",
                          return_value=_posture(ac_sleep=1, sleep_disabled=True)), \
             patch.object(host_power, "set_never_sleep_on_ac") as set_sleep:
            _run_power_posture_step(non_interactive=True)
        set_sleep.assert_not_called()


class TestDedicationAckStep:
    def test_records_acknowledgment(self):
        ack = _run_dedication_ack_step(
            non_interactive=True, existing_host={}, admin_user="pod-admin")
        assert ack["acknowledged"] is True
        assert ack["acknowledged_by"] == "pod-admin"
        assert ack["mode"] == "non-interactive"
        assert ack["acknowledged_at"]  # ISO timestamp present

    def test_decline_is_recorded_and_never_blocks(self):
        with patch("evolve_admin.setup_wizard._confirm", return_value=False):
            ack = _run_dedication_ack_step(
                non_interactive=False, existing_host={}, admin_user="pod-admin")
        assert ack["acknowledged"] is False  # recorded honestly; no sys.exit

    def test_repair_rerun_reuses_prior_ack(self):
        prior = {
            "acknowledged": True,
            "acknowledged_at": "2026-06-10T00:00:00+00:00",
            "acknowledged_by": "pod-admin",
            "mode": "interactive",
        }
        with patch("evolve_admin.setup_wizard._confirm") as confirm:
            ack = _run_dedication_ack_step(
                non_interactive=False,
                existing_host={"dedication_ack": prior},
                admin_user="someone-else",
            )
        confirm.assert_not_called()
        assert ack == prior

    def test_prior_decline_reasks(self):
        # A recorded "no" isn't a durable ack — re-runs get another chance.
        prior = {"acknowledged": False, "acknowledged_by": "pod-admin"}
        ack = _run_dedication_ack_step(
            non_interactive=True,
            existing_host={"dedication_ack": prior},
            admin_user="pod-admin",
        )
        assert ack["acknowledged"] is True
        assert ack["mode"] == "non-interactive"
