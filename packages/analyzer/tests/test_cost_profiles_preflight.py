"""Tests for the heartbeat preflight gate + profile rename + alias.

The gate is a single-purpose invariant: lightContext=false AND every>=1h
is rejected. Everything else passes. The PATCH endpoint at server.py
relies on the err message containing "no valid use case" to map to HTTP
400; the message text is therefore load-bearing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import cost_profiles as cp  # noqa: E402


# ── _parse_every_to_seconds ─────────────────────────────────────────────────


def test_parse_every_seconds() -> None:
    assert cp._parse_every_to_seconds("30s") == 30


def test_parse_every_minutes() -> None:
    assert cp._parse_every_to_seconds("5m") == 300


def test_parse_every_hours() -> None:
    assert cp._parse_every_to_seconds("2h") == 7200


def test_parse_every_days() -> None:
    assert cp._parse_every_to_seconds("1d") == 86400


def test_parse_every_unparseable_returns_none() -> None:
    assert cp._parse_every_to_seconds(None) is None
    assert cp._parse_every_to_seconds("") is None
    assert cp._parse_every_to_seconds("forever") is None
    assert cp._parse_every_to_seconds("5") is None  # no unit


# ── preflight_heartbeat_combination ─────────────────────────────────────────


def _wrapped(hb: dict) -> dict:
    """Wrap a heartbeat dict in the canonical openclaw.json shape so the
    gate's path lookup works without test boilerplate."""
    return {"agents": {"defaults": {"heartbeat": hb}}}


def test_gate_passes_lightcontext_true() -> None:
    assert cp.preflight_heartbeat_combination(
        _wrapped({"lightContext": True, "every": "2h"})
    ) is None


def test_gate_passes_lightcontext_false_at_short_interval() -> None:
    """Sub-1h heartbeat — Anthropic's extended-TTL cache can plausibly hit."""
    assert cp.preflight_heartbeat_combination(
        _wrapped({"lightContext": False, "every": "3m"})
    ) is None


def test_gate_trips_lightcontext_false_at_one_hour() -> None:
    err = cp.preflight_heartbeat_combination(
        _wrapped({"lightContext": False, "every": "1h"})
    )
    assert err is not None
    assert "no valid use case" in err  # load-bearing for HTTP 400 routing


def test_gate_trips_lightcontext_false_at_two_hours() -> None:
    """Atlas's exact configuration the day of the 2026-06-07 incident."""
    err = cp.preflight_heartbeat_combination(
        _wrapped({"lightContext": False, "every": "2h"})
    )
    assert err is not None
    assert "no valid use case" in err


def test_gate_passes_when_lightcontext_unset() -> None:
    """Unset lightContext means OC default (true). Don't flag implicit cases —
    only explicit false is the misconfiguration."""
    assert cp.preflight_heartbeat_combination(
        _wrapped({"every": "4h"})
    ) is None


def test_gate_passes_when_every_unparseable() -> None:
    """If we can't parse the interval, we can't tell if it's in cache TTL —
    err on the side of letting the write through."""
    assert cp.preflight_heartbeat_combination(
        _wrapped({"lightContext": False, "every": "whenever"})
    ) is None


def test_gate_passes_when_no_heartbeat_block() -> None:
    assert cp.preflight_heartbeat_combination(_wrapped({})) is None
    assert cp.preflight_heartbeat_combination({}) is None


# ── Profile rename + alias ──────────────────────────────────────────────────


def test_unrestricted_debug_is_the_canonical_name(tmp_path: Path) -> None:
    profile = cp.get_profile("unrestricted-debug", tmp_path)
    assert profile is not None
    assert profile["name"] == "unrestricted-debug"
    assert profile["builtin"] is True


def test_performance_alias_resolves_to_unrestricted_debug(tmp_path: Path) -> None:
    """Old persisted profile-name references must still load. If this
    breaks, every bot whose cost-settings/<bot>.json was written by the
    pre-rename build hits 'profile not found' on next deploy."""
    profile = cp.get_profile("performance", tmp_path)
    assert profile is not None
    assert profile["name"] == "unrestricted-debug"


def test_legacy_label_and_description_replaced(tmp_path: Path) -> None:
    """Make sure no test or operator can re-read the old misleading copy."""
    profile = cp.get_profile("unrestricted-debug", tmp_path)
    label = profile["label"].lower()
    desc = profile["description"].lower()
    # Affirmative claims: must now mention the actual trade-off.
    assert "debug" in label
    assert "warning" in desc or "no cache benefit" in desc
    # Negative claims: must NOT carry the old misleading copy.
    assert "context continuity is critical" not in desc
    assert profile["expected_savings"].lower().startswith("negative")


def test_performance_no_longer_in_builtin_profiles_dict() -> None:
    """Direct dict lookup should NOT find 'performance' — only the alias
    layer in get_profile() handles legacy names."""
    assert "performance" not in cp.BUILTIN_PROFILES
    assert "unrestricted-debug" in cp.BUILTIN_PROFILES


# ── Signal emission helpers don't crash without signals package ─────────────


def test_emit_signal_safe_no_raise_when_signals_missing(tmp_path: Path) -> None:
    """The Signal store isn't available in every importer context.
    Emitters must be best-effort — never propagate."""
    # Functions return None and shouldn't raise even with weird inputs.
    cp.emit_unrestricted_profile_applied_signal("test_bot", tmp_path)
    cp.emit_cost_setting_forced_signal("test_bot", {}, tmp_path)
    cp.emit_cost_setting_forced_signal("test_bot", {"heartbeat": {}}, tmp_path)
