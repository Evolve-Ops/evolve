"""tests/test_oc_model_routing_refusal_report.py — the routing writer reports
what it refused.

``json_full_config_set`` whitelists the routing block key-by-key and DROPS what
it rejects, while still returning a success-shaped result. Printing the drop to
stderr is not a signal an in-process caller can act on — that is how the
arbiter's tier_adjustment revert came to report ok=True over a write that
landed nothing (#3566 audit E-3 follow-up). ``routingKeysRefused`` is that
signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


def _bot_home(tmp_path: Path, monkeypatch, routing: dict) -> tuple[Path, Path]:
    """Seed a bot home with the given routing block. Returns (oc_json, tiers)."""
    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"routing": routing}))
    monkeypatch.setenv("HOME", str(home))
    return oc_json, tiers_path


def test_refused_routing_keys_are_named_in_the_result(tmp_path, monkeypatch):
    oc_json, tiers_path = _bot_home(tmp_path, monkeypatch, {"enabled": True})

    result = oc_model.json_full_config_set(
        "evolve",
        {"routing": {
            "maintenanceRole": "turbo",     # not a canonical role
            "backgroundTier": "haiku",      # not a tierN id
            "ambiguousTier": "tier3",       # fine
        }},
        oc_json_path=oc_json,
    )

    assert result["routingKeysRefused"] == ["backgroundTier", "maintenanceRole"]
    stored = json.loads(tiers_path.read_text())["routing"]
    # The accepted tier key lands as its ROLE (#3662 boundary translation —
    # a persisted *Tier key would be refused wholesale by the runtime).
    assert stored == {"enabled": True, "ambiguousRole": "fast"}


def test_clean_write_does_not_add_the_key(tmp_path, monkeypatch):
    """Absent, not empty — the happy-path shape is unchanged for every
    existing consumer of this dict."""
    oc_json, _ = _bot_home(tmp_path, monkeypatch, {"enabled": True})

    result = oc_model.json_full_config_set(
        "evolve", {"routing": {"maintenanceTier": "tier2"}}, oc_json_path=oc_json,
    )

    assert "routingKeysRefused" not in result


def test_non_dict_routing_block_is_reported_not_silently_ignored(
    tmp_path, monkeypatch
):
    """The endpoint 400s this shape; a non-endpoint caller that reaches the
    storage layer with it must not get a success-shaped result for a write
    that did nothing."""
    oc_json, tiers_path = _bot_home(tmp_path, monkeypatch, {"enabled": True})

    result = oc_model.json_full_config_set(
        "evolve", {"routing": "tier3"}, oc_json_path=oc_json,
    )

    assert result["routingKeysRefused"] == ["routing"]
    assert json.loads(tiers_path.read_text())["routing"] == {"enabled": True}


def test_unmapped_tier_id_is_refused_not_written_as_null_role(
    tmp_path, monkeypatch
):
    """``_is_tier_id_or_none`` admits any tierN but only tier1-3 map to a
    role. A tier7 must land in ``routingKeysRefused`` like any other rejected
    value — not silently become ``<x>Role: null`` (#3662 review follow-up).
    tier0 is unmapped too since the judge-role collapse, so it is refused the
    same way while the mappable sibling still translates."""
    oc_json, tiers_path = _bot_home(tmp_path, monkeypatch, {
        "enabled": True, "maintenanceRole": "fast",
    })

    result = oc_model.json_full_config_set(
        "evolve",
        {"routing": {"maintenanceTier": "tier7", "backgroundTier": "tier0",
                     "ambiguousTier": "tier2"}},
        oc_json_path=oc_json,
    )

    assert sorted(result["routingKeysRefused"]) == [
        "backgroundTier", "maintenanceTier",
    ]
    stored = json.loads(tiers_path.read_text())["routing"]
    # The refused slots are untouched; the mappable one translates.
    assert stored == {
        "enabled": True, "maintenanceRole": "fast", "ambiguousRole": "standard",
    }


def test_self_heal_of_unmapped_on_disk_tier_is_stripped_and_reported(
    tmp_path, monkeypatch
):
    """Legacy residue on disk with an unmapped tierN: the key must still be
    stripped (the runtime refuses the whole block on PRESENCE of a *Tier
    key), but its value is unrepresentable — no null role is minted, and the
    loss is reported instead of swallowed."""
    oc_json, tiers_path = _bot_home(tmp_path, monkeypatch, {
        "enabled": True, "backgroundTier": "tier9",
    })

    result = oc_model.json_full_config_set(
        "evolve", {"routing": {"maintenanceRole": "fast"}}, oc_json_path=oc_json,
    )

    assert result["routingKeysRefused"] == ["backgroundTier"]
    stored = json.loads(tiers_path.read_text())["routing"]
    assert stored == {"enabled": True, "maintenanceRole": "fast"}


def test_tier_shaped_write_lands_role_shaped_and_self_heals(tmp_path, monkeypatch):
    """#3662: the plugin runtime refuses any persisted ``routing.*Tier`` key
    on sight (LegacyTierShapeError — presence, not value), so the write
    boundary translates a tier-shaped update onto the role slot AND strips a
    legacy key already on disk. The body below is the admin SPA routing
    card's exact pre-#3662 payload; the on-disk ``backgroundTier`` plays the
    pre-existing legacy residue the self-heal must clear."""
    oc_json, tiers_path = _bot_home(tmp_path, monkeypatch, {
        "enabled": True, "backgroundTier": "tier3",
    })

    oc_model.json_full_config_set(
        "evolve",
        {"routing": {
            "enabled": True,
            "maintenanceTier": "tier3",
            "ambiguousTier": None,
            "confidenceThreshold": 0.65,
            "classifierDowngrade": False,
        }},
        oc_json_path=oc_json,
    )

    stored = json.loads(tiers_path.read_text())["routing"]
    assert stored["maintenanceRole"] == "fast"
    assert stored["ambiguousRole"] is None
    assert stored["backgroundRole"] == "fast"
    assert [k for k in stored if k.endswith("Tier")] == []
