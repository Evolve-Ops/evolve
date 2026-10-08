"""Tests for _preflight_oc_version_match auto-kickstart behavior.

Earlier this function raised RuntimeError on CLI/gateway version drift
and made the operator manually kickstart the gateway + re-run deploy.
Now it auto-kickstarts on detection. These tests pin the new behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import deploy  # noqa: E402


def _mock_read_versions(cli_ver, cli_err, gw_ver, gw_err):
    """Set up the two _read_oc_*_version mocks with given returns."""
    cli_patch = patch.object(
        deploy, "_read_oc_cli_version", return_value=(cli_ver, cli_err),
    )
    gw_patch = patch.object(
        deploy, "_read_oc_gateway_version", return_value=(gw_ver, gw_err),
    )
    return cli_patch, gw_patch


# ── Healthy path: no mismatch → no kickstart ────────────────────────────────


def test_preflight_no_drift_skips_kickstart():
    cli_p, gw_p = _mock_read_versions("2026.5.27", "", "2026.5.27", "")
    with cli_p, gw_p, patch.object(deploy, "_kickstart_gateway_and_wait") as ks:
        deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        ks.assert_not_called()


def test_preflight_unreadable_versions_skip_silently():
    """Gateway not running, CLI broken, output-shape change → log + skip.
    Must not crash the deploy."""
    cli_p, gw_p = _mock_read_versions(None, "", None, "")
    with cli_p, gw_p, patch.object(deploy, "_kickstart_gateway_and_wait") as ks:
        # No raise; no kickstart.
        deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        ks.assert_not_called()


# ── Drift detection → auto-kickstart ────────────────────────────────────────


def test_preflight_version_drift_triggers_kickstart():
    """CLI 2026.5.27, gateway 2026.5.26 → kickstart called."""
    cli_p, gw_p = _mock_read_versions("2026.5.27", "", "2026.5.26", "")
    with cli_p, gw_p, patch.object(
        deploy, "_kickstart_gateway_and_wait",
        return_value=(True, "gateway back up at version 2026.5.27"),
    ) as ks:
        deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        ks.assert_called_once()
        # Caller must pass expected_cli_ver so the kickstart helper can
        # gate its return on a match — otherwise we'd accept "any
        # version" as success.
        assert ks.call_args.kwargs.get("expected_cli_ver") == "2026.5.27"


def test_preflight_config_drift_triggers_kickstart():
    """`config changed since last load` on stderr → kickstart called even
    when versions are unreadable. That stderr message IS the failure
    signature we exist to catch."""
    cli_p, gw_p = _mock_read_versions(
        None, deploy._OC_CONFIG_DRIFT_STDERR, None, "",
    )
    with cli_p, gw_p, patch.object(
        deploy, "_kickstart_gateway_and_wait",
        return_value=(True, "gateway back up"),
    ) as ks:
        deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        ks.assert_called_once()


def test_preflight_config_drift_on_gateway_side_also_triggers():
    """Same as above but stderr is on the gateway-status side."""
    cli_p, gw_p = _mock_read_versions(
        "2026.5.27", "", None, deploy._OC_CONFIG_DRIFT_STDERR,
    )
    with cli_p, gw_p, patch.object(
        deploy, "_kickstart_gateway_and_wait",
        return_value=(True, "gateway back up at version 2026.5.27"),
    ) as ks:
        deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        ks.assert_called_once()


# ── Auto-fix failure → clear error ──────────────────────────────────────────


def test_preflight_raises_when_kickstart_fails():
    """If launchctl kickstart itself fails, raise with the underlying
    error — don't let the deploy proceed into the cryptic OC chain."""
    cli_p, gw_p = _mock_read_versions("2026.5.27", "", "2026.5.26", "")
    with cli_p, gw_p, patch.object(
        deploy, "_kickstart_gateway_and_wait",
        return_value=(False, "launchctl kickstart failed: permission denied"),
    ):
        with pytest.raises(RuntimeError) as exc:
            deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        assert "team_bot_a" in str(exc.value)
        assert "auto-kickstart failed" in str(exc.value)
        assert "launchctl kickstart failed" in str(exc.value)


def test_preflight_raises_when_gateway_does_not_catch_up():
    """Kickstart succeeded but the new gateway still reports the old
    version — brew/auto-updater is in a weird state."""
    cli_p, gw_p = _mock_read_versions("2026.5.27", "", "2026.5.26", "")
    with cli_p, gw_p, patch.object(
        deploy, "_kickstart_gateway_and_wait",
        return_value=(False, "gateway came back at version 2026.5.26 but CLI is 2026.5.27"),
    ):
        with pytest.raises(RuntimeError) as exc:
            deploy._preflight_oc_version_match("team_bot_a", "team_bot_a", "/Users/team_bot_a")
        # Manual-fix command should be in the error for the operator.
        assert "launchctl kickstart -k" in str(exc.value)


# ── Kickstart helper itself ────────────────────────────────────────────────
#
# The kickstart flows through the Scheduler seam (4.3C S2) — every test
# injects a LaunchdScheduler with a fake runner so no real ``sudo
# launchctl`` is ever spawned. deploy.subprocess.run stays patched for the
# helper's non-launchctl subprocesses (preflight validation et al).


@pytest.fixture(autouse=True)
def _reset_scheduler():
    from evolve_admin.runtime import set_scheduler

    yield
    set_scheduler(None)


def _inject_launchctl(rc: int, out: str = "", err: str = "") -> None:
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    set_scheduler(LaunchdScheduler(runner=lambda argv: (rc, out, err)))


def test_kickstart_helper_fails_when_launchctl_exits_nonzero(monkeypatch):
    """launchctl returning non-zero → kickstart helper returns (False, ...)."""

    class _Result:
        returncode = 1
        stderr = "kickstart: not authorized\n"
        stdout = ""
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: _Result())
    _inject_launchctl(1, err="kickstart: not authorized")
    ok, msg = deploy._kickstart_gateway_and_wait(
        "team_bot_a", "team_bot_a", "/Users/team_bot_a",
        expected_cli_ver="2026.5.27",
        timeout_seconds=2,
    )
    assert ok is False
    assert "kickstart" in msg.lower()
    assert "not authorized" in msg


def test_kickstart_helper_succeeds_when_gateway_matches_quickly(monkeypatch):
    """launchctl OK + gateway version matches → (True, ...)."""

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: _Result())
    _inject_launchctl(0)
    monkeypatch.setattr(
        deploy, "_read_oc_gateway_version",
        lambda u, h: ("2026.5.27", ""),
    )
    # Speed up the poll for the test.
    ok, msg = deploy._kickstart_gateway_and_wait(
        "team_bot_a", "team_bot_a", "/Users/team_bot_a",
        expected_cli_ver="2026.5.27",
        timeout_seconds=5,
    )
    assert ok is True
    assert "2026.5.27" in msg


def test_kickstart_helper_times_out_on_stale_version(monkeypatch):
    """Gateway comes back but version is wrong → timeout + last-seen msg."""
    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: _Result())
    _inject_launchctl(0)
    monkeypatch.setattr(
        deploy, "_read_oc_gateway_version",
        lambda u, h: ("2026.5.26", ""),  # stale
    )
    ok, msg = deploy._kickstart_gateway_and_wait(
        "team_bot_a", "team_bot_a", "/Users/team_bot_a",
        expected_cli_ver="2026.5.27",
        timeout_seconds=2,
    )
    assert ok is False
    assert "2026.5.26" in msg
    assert "2026.5.27" in msg
