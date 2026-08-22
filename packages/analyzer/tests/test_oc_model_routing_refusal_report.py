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
    assert stored == {"enabled": True, "ambiguousTier": "tier3"}


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
