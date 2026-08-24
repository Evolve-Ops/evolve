"""Regression tests for heal.check_gateway.

Pins the contract that heal.py recognizes every shape of openclaw's
`health --json` response that we have observed in the wild. A drift
between this set and what openclaw actually returns causes heal.py to
mark healthy gateways as down and kill them on every 5-min cycle —
the bug that produced the 2026-04-26T07:22Z TCP-layer wedge.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """check_gateway sleeps `healthRetryBackoffSeconds` (default 5) between
    attempts. Without this fixture the existing test suite would burn
    ~5s per failure-path test waiting for the retry. Tests that explicitly
    care about sleep behavior install their own monkeypatch."""
    monkeypatch.setattr(heal.time, "sleep", lambda s: None)


@pytest.mark.parametrize("response,expected_healthy", [
    # The response shape openclaw 2026.4.x actually returns. Without the "ok"
    # check, heal kills every gateway on every 5-min cycle.
    ({"ok": True, "ts": 1, "channels": {}, "agents": []}, True),
    # Older shape — kept for backwards compat.
    ({"status": "ok"}, True),
    # Even older shape.
    ({"gateway": {"reachable": True}}, True),
    # Generic catch-all.
    ({"healthy": True}, True),
    # Multiple markers — should still be healthy.
    ({"ok": True, "status": "ok", "healthy": True}, True),
    # Negatives — none of the markers, must be unhealthy.
    ({"ok": False}, False),
    ({"status": "error"}, False),
    ({"gateway": {"reachable": False}}, False),
    ({}, False),
    ({"some_unrelated_field": "value"}, False),
])
def test_check_gateway_recognizes_all_known_health_shapes(response, expected_healthy):
    with patch("oc_cli.oc_health", return_value=response):
        status = heal.check_gateway("team_bot_a", 18789, {})
    assert status.healthy is expected_healthy, (
        f"oc_health returned {response!r} → expected healthy={expected_healthy}, "
        f"got healthy={status.healthy} (err={status.error!r})"
    )


def test_check_gateway_handles_no_data():
    """oc_health returns None on subprocess failure → unhealthy with explicit error."""
    with patch("oc_cli.oc_health", return_value=None):
        status = heal.check_gateway("team_bot_a", 18789, {})
    assert status.healthy is False
    assert "no data" in (status.error or "").lower()


def test_check_gateway_handles_oc_health_exception():
    """oc_health raising should be caught, not propagated — heal must keep
    iterating across bots even if one probe blows up."""
    with patch("oc_cli.oc_health", side_effect=RuntimeError("boom")):
        status = heal.check_gateway("team_bot_a", 18789, {})
    assert status.healthy is False
    assert "boom" in (status.error or "")


# ── per-bot timeout (PR addressing 2026-04-26-012 evolve restart cycle) ──


def test_check_gateway_passes_explicit_timeout_to_oc_health():
    """Explicit oc_health_timeout kwarg must be passed through to oc_health."""
    captured = {}

    def fake(bot_id, **kw):
        captured.update(kw)
        return {"ok": True}

    with patch("oc_cli.oc_health", side_effect=fake):
        heal.check_gateway("team_bot_a", 18789, {}, oc_health_timeout=60)

    assert captured.get("timeout") == 60


def test_check_gateway_uses_heal_cfg_default_when_no_explicit_timeout():
    """When no explicit override, falls back to heal_cfg.ocHealthTimeoutSeconds.
    When neither is provided, falls back to the 30s default."""
    captured = {}

    def fake(bot_id, **kw):
        captured.update(kw)
        return {"ok": True}

    with patch("oc_cli.oc_health", side_effect=fake):
        heal.check_gateway("team_bot_a", 18789, {"ocHealthTimeoutSeconds": 45})
    assert captured.get("timeout") == 45

    captured.clear()
    with patch("oc_cli.oc_health", side_effect=fake):
        heal.check_gateway("team_bot_a", 18789, {})
    assert captured.get("timeout") == 30, (
        "no heal_cfg → must default to 30s (more generous than oc_command's 15s)"
    )


@pytest.mark.parametrize("bots_cfg,heal_cfg,expected", [
    # No per-bot override, use heal_cfg default
    ({"team_bot_a": {}}, {"ocHealthTimeoutSeconds": 30}, 30),
    # Per-bot override wins over heal_cfg default
    ({"evolve": {"heal": {"ocHealthTimeoutSeconds": 60}}}, {"ocHealthTimeoutSeconds": 30}, 60),
    # No heal block at all on the bot — fall back to heal_cfg
    ({"team_bot_a": {"role": "member"}}, {"ocHealthTimeoutSeconds": 30}, 30),
    # Bot not in config at all — fall back to heal_cfg
    ({}, {"ocHealthTimeoutSeconds": 30}, 30),
    # heal_cfg missing the key entirely — fall back to 30
    ({}, {}, 30),
    # Per-bot value of 0 is treated as missing (defensive)
    ({"evolve": {"heal": {"ocHealthTimeoutSeconds": 0}}}, {"ocHealthTimeoutSeconds": 30}, 30),
])
def test_resolve_oc_health_timeout(bots_cfg, heal_cfg, expected):
    bot_id = next(iter(bots_cfg)) if bots_cfg else "team_bot_a"
    assert heal._resolve_oc_health_timeout(bot_id, bots_cfg, heal_cfg) == expected


# ── retry-with-backoff (PR addressing 1006 abnormal closure mode of 012) ──


def test_check_gateway_succeeds_on_first_attempt_no_retry():
    """When the first probe is healthy, do not retry (don't waste time)."""
    calls = 0

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return {"ok": True}

    cfg = {"healthMaxAttempts": 2, "healthRetryBackoffSeconds": 0}
    with patch("oc_cli.oc_health", side_effect=fake):
        status = heal.check_gateway("team_bot_a", 18789, cfg)

    assert status.healthy is True
    assert calls == 1, f"expected 1 attempt on healthy first probe, got {calls}"


def test_check_gateway_retries_after_first_failure_then_recovers():
    """Transient first-probe failure followed by a second-probe success
    must yield healthy=True. This is the 1006-abnormal-closure case: the
    gateway dropped the websocket but is healthy when probed again."""
    calls = 0

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return None if calls == 1 else {"ok": True}

    cfg = {"healthMaxAttempts": 2, "healthRetryBackoffSeconds": 0}
    with patch("oc_cli.oc_health", side_effect=fake):
        status = heal.check_gateway("evolve", 19030, cfg)

    assert status.healthy is True
    assert calls == 2, f"expected 2 attempts (first failed, second OK), got {calls}"


def test_check_gateway_marks_down_only_when_all_attempts_fail():
    """Both attempts fail → healthy=False, error preserved from final attempt."""
    calls = 0

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return None  # always fail

    cfg = {"healthMaxAttempts": 2, "healthRetryBackoffSeconds": 0}
    with patch("oc_cli.oc_health", side_effect=fake):
        status = heal.check_gateway("evolve", 19030, cfg)

    assert status.healthy is False
    assert "no data" in (status.error or "").lower()
    assert calls == 2


def test_check_gateway_max_attempts_one_disables_retry():
    """healthMaxAttempts=1 must mean exactly one probe — no retry, no wait.
    Lets operators opt out of retry-with-backoff if they prefer the old
    fail-fast behavior."""
    calls = 0

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return None

    cfg = {"healthMaxAttempts": 1, "healthRetryBackoffSeconds": 0}
    with patch("oc_cli.oc_health", side_effect=fake):
        status = heal.check_gateway("team_bot_a", 18789, cfg)

    assert status.healthy is False
    assert calls == 1


def test_check_gateway_retry_respects_backoff_sleep(monkeypatch):
    """Between attempts, must sleep healthRetryBackoffSeconds. Use a fake
    sleep so the test is fast but still asserts the call happened."""
    calls = 0
    sleeps: list[float] = []

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return None  # both fail

    monkeypatch.setattr(heal.time, "sleep", lambda s: sleeps.append(s))

    cfg = {"healthMaxAttempts": 3, "healthRetryBackoffSeconds": 7}
    with patch("oc_cli.oc_health", side_effect=fake):
        heal.check_gateway("team_bot_a", 18789, cfg)

    assert calls == 3
    assert sleeps == [7, 7], (
        f"expected one 7s sleep before each retry (2 retries), got {sleeps}"
    )


def test_check_gateway_default_max_attempts_is_two():
    """heal_cfg with no explicit healthMaxAttempts → defaults to 2.
    Empty heal_cfg → also 2 (back-compat doesn't drop retry behavior)."""
    calls = 0

    def fake(bot_id, **kw):
        nonlocal calls
        calls += 1
        return None

    with patch("oc_cli.oc_health", side_effect=fake), \
         patch.object(heal.time, "sleep", lambda s: None):
        heal.check_gateway("team_bot_a", 18789, {})

    assert calls == 2, "empty heal_cfg should still default to 2 attempts"
