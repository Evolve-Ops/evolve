"""Tests for security_warden.posture.check_openclaw_version_floor.

V1.5-3 pillar: surface fleet-wide OpenClaw drift below the upstream
floor that ships ``openclaw exec-policy``. The check reads
``meta.lastTouchedVersion`` from the bot's openclaw.json and emits a
``warn``-severity Signal-spec dict when the version is below the floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.security_warden import posture  # noqa: E402
from generators.security_warden.observe import (  # noqa: E402
    WardenContext,
    observe_signals,
)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("2026.4.12", (2026, 4, 12)),
    ("2026.4.29", (2026, 4, 29)),
    ("v2026.4.12", (2026, 4, 12)),
    ("2026.5.12-beta.1", (2026, 5, 12)),
    ("", None),
    (None, None),
    ("not-a-version", None),
])
def test_parse_calver(raw, expected):
    assert posture._parse_calver(raw) == expected


# ─────────────────────────────────────────────────────────────────────────────
# check_openclaw_version_floor — direct
# ─────────────────────────────────────────────────────────────────────────────


def test_above_floor_emits_nothing():
    spec = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.29"}},
    )
    assert spec is None


def test_equal_to_floor_emits_nothing():
    spec = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.12"}},
    )
    assert spec is None


def test_below_floor_emits_warn_signal():
    spec = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.11"}},
    )
    assert spec is not None
    assert spec["type"] == "openclaw_version_below_floor"
    assert spec["severity"] == "warn"
    assert spec["flavor"] == "maintenance"
    assert spec["bot_id"] == "team_bot_a"
    assert spec["producer"] == "security_warden"
    assert "2026.4.11" in spec["title"]
    assert "2026.4.12" in spec["title"]
    assert spec["details"]["observed_version"] == "2026.4.11"
    assert spec["details"]["minimum_version"] == "2026.4.12"
    assert spec["details"]["check"] == "openclaw_version_below_floor"


def test_custom_floor_above():
    """Caller can raise the floor; previously-compliant bots become findings."""
    spec = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.29"}},
        minimum_version="2026.5.0",
    )
    assert spec is not None
    assert "2026.5.0" in spec["title"]


def test_no_meta_block_returns_none():
    """Read failure → no finding (separate concern from version drift)."""
    assert posture.check_openclaw_version_floor(
        bot_id="team_bot_a", oc_config={},
    ) is None


def test_no_oc_config_returns_none():
    assert posture.check_openclaw_version_floor(
        bot_id="team_bot_a", oc_config=None,
    ) is None


def test_meta_not_dict_returns_none():
    assert posture.check_openclaw_version_floor(
        bot_id="team_bot_a", oc_config={"meta": "not-an-object"},
    ) is None


def test_unparseable_version_returns_none():
    """An unparseable version is a separate concern; not this check's job."""
    assert posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "garbage"}},
    ) is None


def test_empty_version_string_returns_none():
    assert posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": ""}},
    ) is None


def test_signature_stable_across_calls():
    """Same (producer, type, bot_id) → same signature; the runner uses
    this for de-dup across cycles.
    """
    a = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.10"}},
    )
    b = posture.check_openclaw_version_floor(
        bot_id="team_bot_a",
        oc_config={"meta": {"lastTouchedVersion": "2026.4.11"}},
    )
    # Different versions, but same signature — both still "below floor"
    # for the same bot, and the signal store should collapse them rather
    # than fire two distinct alerts.
    assert a["signature"] == b["signature"]


# ─────────────────────────────────────────────────────────────────────────────
# observe_signals integration — the runner-facing entrypoint
# ─────────────────────────────────────────────────────────────────────────────


def _net(bot_id: str = "team_bot_a", multi_user: bool = False) -> dict:
    return {
        "bots": {bot_id: {"role": "member", "port": 18789, "multiUser": multi_user}},
        "pod": {"admins": {"external_ids": {}}},
    }


def test_observe_signals_includes_version_floor_for_single_user(tmp_path):
    """Version-floor check runs even for single-user bots (unlike multi-user
    posture checks which short-circuit when multiUser=False)."""
    def _reader(_bid):
        return {"meta": {"lastTouchedVersion": "2026.4.10"}}

    ctx = WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda *a, **k: [],
        network=_net(multi_user=False),
        oc_config_reader=_reader,
    )
    specs = observe_signals(ctx)
    types = [s["type"] for s in specs]
    assert "openclaw_version_below_floor" in types


def test_observe_signals_no_finding_when_above_floor(tmp_path):
    def _reader(_bid):
        return {"meta": {"lastTouchedVersion": "2026.4.29"}}

    ctx = WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda *a, **k: [],
        network=_net(),
        oc_config_reader=_reader,
    )
    specs = observe_signals(ctx)
    types = [s["type"] for s in specs]
    assert "openclaw_version_below_floor" not in types


def test_observe_signals_runs_alongside_multi_user_checks(tmp_path):
    """A multi-user bot with both a version below floor AND a posture gap
    should produce both signals.
    """
    def _reader(_bid):
        return {
            "meta": {"lastTouchedVersion": "2026.4.10"},
            "tools": {"exec": {"security": "full"}},  # multi_user_exec_full_unscoped trigger
        }

    ctx = WardenContext(
        bot_id="team_bot_a",
        transcript_reader=lambda *a, **k: [],
        network=_net(multi_user=True),
        oc_config_reader=_reader,
    )
    specs = observe_signals(ctx)
    types = set(s["type"] for s in specs)
    assert "openclaw_version_below_floor" in types
    assert "multi_user_exec_full_unscoped" in types
