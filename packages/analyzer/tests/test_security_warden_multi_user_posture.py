"""Tests for security_warden's multi-user posture checks.

The posture module emits Signal-spec dicts for multi-user bots whose
configuration is missing one of the deltas that make a multi-user bot
safe. Single-user bots produce no findings — the flag is a declared
audience, not a problem to warn about.

Three checks ship in v1:

  - ``multi_user_no_pod_admins`` (alert)
  - ``multi_user_no_primary_recorded`` (warn)
  - ``multi_user_exec_full_unscoped`` (alert)

These tests exercise each check in isolation plus the integration via
``observe_signals``, which is the entrypoint the generator runner calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: F401

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.security_warden import posture  # noqa: E402
from generators.security_warden.observe import (  # noqa: E402
    WardenContext,
    observe_signals,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _network(
    *,
    bot_id: str = "team_bot_a",
    multi_user: bool = True,
    admins: dict | None = None,
    primary_external_ids: dict | None = None,
) -> dict:
    """Build a minimal network.json fixture for posture tests."""
    return {
        "bots": {
            bot_id: {
                "role": "member",
                "port": 18789,
                "multiUser": multi_user,
                "primary_user": (
                    {"external_ids": primary_external_ids}
                    if primary_external_ids is not None
                    else {}
                ),
            }
        },
        "pod": {"admins": {"external_ids": admins or {}}},
    }


def _oc_config(*, exec_security: str | None = None, allowlist: list | None = None) -> dict:
    """Build a minimal openclaw.json fixture."""
    exec_block: dict = {}
    if exec_security is not None:
        exec_block["security"] = exec_security
    if allowlist is not None:
        exec_block["allowlist"] = allowlist
    return {"tools": {"exec": exec_block}} if exec_block else {"tools": {}}


def _types(specs: list[dict]) -> set[str]:
    return {s["type"] for s in specs}


# ─────────────────────────────────────────────────────────────────────────────
# Single-user bots are exempt
# ─────────────────────────────────────────────────────────────────────────────


def test_single_user_bot_emits_nothing():
    """multiUser=false → no posture findings, regardless of other config gaps."""
    net = _network(multi_user=False, admins={}, primary_external_ids={})
    oc = _oc_config(exec_security="full")
    assert posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=oc
    ) == []


def test_unflagged_bot_emits_nothing():
    """Bot not in network.json (or no multiUser key) → no findings."""
    net = {"bots": {}, "pod": {"admins": {"external_ids": {}}}}
    assert posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=None
    ) == []


def test_missing_network_emits_nothing():
    """No network → no posture work (registry already alerts on this)."""
    assert posture.check_multi_user_posture(
        bot_id="team_bot_a", network=None, oc_config=None
    ) == []


# ─────────────────────────────────────────────────────────────────────────────
# Check: no_pod_admins
# ─────────────────────────────────────────────────────────────────────────────


def test_no_pod_admins_fires_when_admins_dict_empty():
    net = _network(admins={}, primary_external_ids={"telegram": "12345"})
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    types = _types(specs)
    assert "multi_user_no_pod_admins" in types


def test_no_pod_admins_fires_when_channel_lists_empty():
    net = _network(
        admins={"telegram": [], "slack": []},
        primary_external_ids={"telegram": "12345"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    assert "multi_user_no_pod_admins" in _types(specs)


def test_no_pod_admins_silent_when_at_least_one_admin_recorded():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "12345"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    assert "multi_user_no_pod_admins" not in _types(specs)


def test_no_pod_admins_signal_shape():
    net = _network(admins={}, primary_external_ids={"telegram": "12345"})
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    [spec] = [s for s in specs if s["type"] == "multi_user_no_pod_admins"]
    assert spec["producer"] == "security_warden"
    assert spec["severity"] == "alert"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team_bot_a"
    assert spec["flavor"] == "maintenance"
    assert spec["signature"] == "security_warden:multi_user_no_pod_admins:team_bot_a"
    assert "claim" in spec["body"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Check: no_primary_recorded
# ─────────────────────────────────────────────────────────────────────────────


def test_no_primary_recorded_fires_when_external_ids_missing():
    net = _network(admins={"telegram": ["12345"]}, primary_external_ids={})
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    assert "multi_user_no_primary_recorded" in _types(specs)


def test_no_primary_recorded_silent_when_one_channel_recorded():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    assert "multi_user_no_primary_recorded" not in _types(specs)


def test_no_primary_recorded_signal_shape():
    net = _network(admins={"telegram": ["12345"]}, primary_external_ids={})
    [spec] = [
        s for s in posture.check_multi_user_posture(
            bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
        )
        if s["type"] == "multi_user_no_primary_recorded"
    ]
    assert spec["severity"] == "warn"
    assert spec["bot_id"] == "team_bot_a"


# ─────────────────────────────────────────────────────────────────────────────
# Check: exec_full_unscoped
# ─────────────────────────────────────────────────────────────────────────────


def test_exec_full_unscoped_fires_when_exec_full_no_allowlist():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="full")
    )
    assert "multi_user_exec_full_unscoped" in _types(specs)


def test_exec_full_silent_when_allowlist_set():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    oc = _oc_config(exec_security="full", allowlist=["ls", "git status"])
    specs = posture.check_multi_user_posture(bot_id="team_bot_a", network=net, oc_config=oc)
    assert "multi_user_exec_full_unscoped" not in _types(specs)


def test_exec_silent_when_security_is_allowlist():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="allowlist")
    )
    assert "multi_user_exec_full_unscoped" not in _types(specs)


def test_exec_check_skipped_when_oc_config_none():
    """oc_config unreadable → skip, don't surface a finding for what we can't see."""
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    specs = posture.check_multi_user_posture(
        bot_id="team_bot_a", network=net, oc_config=None
    )
    assert "multi_user_exec_full_unscoped" not in _types(specs)


def test_exec_full_unscoped_signal_shape():
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    [spec] = [
        s for s in posture.check_multi_user_posture(
            bot_id="team_bot_a", network=net, oc_config=_oc_config(exec_security="full")
        )
        if s["type"] == "multi_user_exec_full_unscoped"
    ]
    # Demoted from alert → info on 2026-06-04 (Alerts quality-control
    # pass): exec=full is the post-Phase-A member-bot default. See
    # internal/diagnosis-oc-noisy-advisories-2026-06-04.md + posture.py
    # module docstring.
    assert spec["severity"] == "info"
    # Flavor calibration (2026-06-12 reports pass): this signal is
    # explicitly informational, so it rides the advisory ``activity`` lane,
    # not the ``maintenance`` task inbox — unlike the warden's genuine
    # posture problems (no-pod-admins, version-below-floor) which stay
    # ``maintenance``.
    assert spec["flavor"] == "activity"
    assert spec["bot_id"] == "team_bot_a"
    assert "tools.exec.security" in spec["body"]


# ─────────────────────────────────────────────────────────────────────────────
# Multiple findings on a fresh multi-user bot
# ─────────────────────────────────────────────────────────────────────────────


def test_fresh_multi_user_bot_emits_all_three():
    """A bot freshly toggled to multi-user with default config trips all checks."""
    net = _network(admins={}, primary_external_ids={})
    oc = _oc_config(exec_security="full")
    specs = posture.check_multi_user_posture(bot_id="team_bot_a", network=net, oc_config=oc)
    assert _types(specs) == {
        "multi_user_no_pod_admins",
        "multi_user_no_primary_recorded",
        "multi_user_exec_full_unscoped",
    }


def test_well_configured_multi_user_bot_emits_nothing():
    """All three checks pass → no findings, even though multiUser=true."""
    net = _network(
        admins={"telegram": ["12345"]},
        primary_external_ids={"telegram": "67890"},
    )
    oc = _oc_config(exec_security="allowlist")
    specs = posture.check_multi_user_posture(bot_id="team_bot_a", network=net, oc_config=oc)
    assert specs == []


# ─────────────────────────────────────────────────────────────────────────────
# Integration via observe_signals (the runner-facing entrypoint)
# ─────────────────────────────────────────────────────────────────────────────


def _ctx(network: dict | None, oc_config: dict | None) -> WardenContext:
    def _reader(bid: str):  # noqa: ARG001
        return oc_config

    return WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda b, h: [],  # noqa: ARG005
        network=network,
        oc_config_reader=_reader,
    )


def test_observe_signals_returns_posture_specs():
    ctx = _ctx(_network(admins={}, primary_external_ids={}),
               _oc_config(exec_security="full"))
    specs = observe_signals(ctx)
    assert _types(specs) == {
        "multi_user_no_pod_admins",
        "multi_user_no_primary_recorded",
        "multi_user_exec_full_unscoped",
    }


def test_observe_signals_silent_for_single_user_bot():
    ctx = _ctx(_network(multi_user=False), _oc_config(exec_security="full"))
    assert observe_signals(ctx) == []


def test_observe_signals_silent_when_network_missing():
    """Generator runner builds ctx without network → posture skips silently."""
    ctx = WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda b, h: [],  # noqa: ARG005
    )
    assert observe_signals(ctx) == []


def test_observe_signals_handles_oc_reader_exception():
    """Reader raising shouldn't crash the warden — exec check just gets skipped."""
    def _broken_reader(bid: str):  # noqa: ARG001
        raise RuntimeError("filesystem error")

    ctx = WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda b, h: [],  # noqa: ARG005
        network=_network(admins={}, primary_external_ids={}),
        oc_config_reader=_broken_reader,
    )
    specs = observe_signals(ctx)
    types = _types(specs)
    # The two checks that don't depend on oc_config still fire.
    assert "multi_user_no_pod_admins" in types
    assert "multi_user_no_primary_recorded" in types
    # The exec check skipped silently — we can't see what we can't read.
    assert "multi_user_exec_full_unscoped" not in types
