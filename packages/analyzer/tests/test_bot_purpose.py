"""Per-bot purpose anchor (Effectiveness Layer, Phase B)."""

from __future__ import annotations

import importlib

bp = importlib.import_module("bot_purpose")


def test_valid_archetype_normalizes():
    p = bp.normalize_purpose({"archetype": "Personal-Assistant", "mission": "Keep my schedule."})
    assert p == {
        "archetype": "personal-assistant",   # lowercased
        "mission": "Keep my schedule.",
        "captured": "declared",
        "confidence": 1.0,
    }


def test_unknown_archetype_becomes_custom():
    p = bp.normalize_purpose({"archetype": "wizardbot", "mission": "Cast spells."})
    assert p["archetype"] == "custom"
    assert p["mission"] == "Cast spells."


def test_empty_payload_is_none():
    assert bp.normalize_purpose(None) is None
    assert bp.normalize_purpose({}) is None
    assert bp.normalize_purpose({"archetype": "", "mission": "  "}) is None


def test_mission_is_trimmed_to_cap():
    long = "x" * (bp.MISSION_MAX_CHARS + 50)
    p = bp.normalize_purpose({"archetype": "custom", "mission": long})
    assert len(p["mission"]) == bp.MISSION_MAX_CHARS


def test_declared_forces_confidence_1():
    p = bp.normalize_purpose({"archetype": "custom", "mission": "x", "confidence": 0.2}, captured="declared")
    assert p["confidence"] == 1.0


def test_inferred_keeps_clamped_confidence():
    p = bp.normalize_purpose({"archetype": "research-analyst", "mission": "x", "confidence": 0.7}, captured="inferred")
    assert p["captured"] == "inferred"
    assert p["confidence"] == 0.7
    p2 = bp.normalize_purpose({"archetype": "custom", "mission": "x", "confidence": 5}, captured="inferred")
    assert p2["confidence"] == 1.0  # clamped


def test_mission_only_is_custom():
    p = bp.normalize_purpose({"mission": "Do the thing."})
    assert p["archetype"] == "custom"
    assert p["mission"] == "Do the thing."


def test_get_and_has_purpose():
    net = {"bots": {"team_bot_a": {"purpose": {"archetype": "project-manager", "mission": "PM"}}, "team_bot_b": {}}}
    assert bp.get_bot_purpose(net, "team_bot_a")["archetype"] == "project-manager"
    assert bp.get_bot_purpose(net, "team_bot_b") is None
    assert bp.get_bot_purpose(net, "unknown") is None
    assert bp.has_purpose(net, "team_bot_a") is True
    assert bp.has_purpose(net, "team_bot_b") is False


# ── set_bot_purpose — the shared write path (admin API + Fit-Reviewer CLI) ──


def test_set_bot_purpose_declares_and_stamps():
    net = {"bots": {"team_bot_a": {}}}
    out = bp.set_bot_purpose(
        net, "team_bot_a",
        {"archetype": "Project-Manager", "mission": "Run projects."},
        reviewed_at="2026-06-12T00:00:00+00:00",
    )
    assert out["archetype"] == "project-manager"  # normalized
    assert out["captured"] == "declared" and out["confidence"] == 1.0
    assert out["reviewed_at"] == "2026-06-12T00:00:00+00:00"
    assert net["bots"]["team_bot_a"]["purpose"] == out
    assert bp.has_purpose(net, "team_bot_a") is True


def test_set_bot_purpose_empty_clears():
    net = {"bots": {"team_bot_a": {"purpose": {"archetype": "custom", "mission": "x"}}}}
    assert bp.set_bot_purpose(net, "team_bot_a", None) is None
    assert "purpose" not in net["bots"]["team_bot_a"]


def test_set_bot_purpose_non_member_raises():
    import pytest

    net = {"bots": {"team_bot_a": {}}}
    with pytest.raises(KeyError):
        bp.set_bot_purpose(net, "ghost_bot", {"archetype": "custom", "mission": "x"})


def test_set_bot_purpose_no_reviewed_at_omits_field():
    net = {"bots": {"team_bot_a": {}}}
    out = bp.set_bot_purpose(net, "team_bot_a", {"archetype": "custom", "mission": "x"})
    assert "reviewed_at" not in out
