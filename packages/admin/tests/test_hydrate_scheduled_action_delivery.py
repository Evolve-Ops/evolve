"""Tests for per-action delivery-contract merge in hydrate_v7_arc_instance.

A scanner-extracted v7-arc Instance lands its ``scheduled_actions[]`` with
``outputs: []`` and no ``delivery_contract`` (``quality="extracted"``),
silently dropping the bound Spec's user-facing delivery declaration — so
the action falls out of delivery_monitor's monitored set (Atlas Daily
Digest silent non-delivery, 2026-06-16). Hydration must merge the Spec's
per-action ``outputs[].channel`` + ``delivery_contract`` back in, while the
Instance keeps its realized install/identity fields.

See internal/spec-apps-meta-2026-06-13.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    _merge_scheduled_action_delivery,
    hydrate_v7_arc_instance,
)

_SPEC_CONTRACT = {
    "user_facing": True,
    "window_minutes": 30,
    "evidence": {"ran": {"kind": "scheduler_state"}},
    "heal": "rerun",
}


def _seed_spec(shared_dir: Path, spec_id: str, spec_version: str,
               scheduled_actions: list) -> None:
    path = shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "spec_id": spec_id,
        "spec_version": spec_version,
        "name": "Daily Briefing",
        "manifest_shape": "v7-arc",
        "scheduled_actions": scheduled_actions,
    }))


def _instance(spec_id: str, spec_version: str, scheduled_actions: list) -> dict:
    return {
        "instance_id": "i-briefing01",
        "bot_id": "atlas",
        "manifest_shape": "v7-arc",
        "provenance": {"spec_id": spec_id, "spec_version": spec_version,
                       "source_pod_id": None},
        "status": "active",
        "scheduled_actions": scheduled_actions,
    }


def _spec_action() -> dict:
    return {
        "id": "briefing",
        "mechanism": "launchd",
        "install": {"plist_label": "ai.evolve.${bot_id}.briefing",
                    "command": "/bin/bash ${workspace}/scripts/briefing-cron.sh"},
        "outputs": [{"kind": "session_message", "channel": "primary"}],
        "delivery_contract": dict(_SPEC_CONTRACT),
    }


def _extracted_action() -> dict:
    # What the scanner extraction leaves behind: realized install info, but
    # outputs:[] and no delivery_contract.
    return {
        "id": "briefing",
        "mechanism": "launchd",
        "install": {"plist_label": "ai.evolve.atlas.briefing",
                    "command": "/bin/bash /Users/atlas/.openclaw/workspace/scripts/briefing-cron.sh"},
        "quality": "extracted",
        "installed_artifact": "/Library/LaunchDaemons/ai.evolve.atlas.briefing.plist",
        "outputs": [],
    }


# ── _merge_scheduled_action_delivery (pure) ──────────────────────────────────


def test_merge_fills_outputs_and_contract_from_spec() -> None:
    merged = _merge_scheduled_action_delivery(
        [_extracted_action()], [_spec_action()],
    )
    a = merged[0]
    # Spec's delivery declaration is restored.
    assert a["outputs"] == [{"kind": "session_message", "channel": "primary"}]
    assert a["delivery_contract"]["user_facing"] is True
    # Instance's realized install info is preserved (Instance wins).
    assert a["installed_artifact"] == "/Library/LaunchDaemons/ai.evolve.atlas.briefing.plist"
    assert a["quality"] == "extracted"


def test_merge_does_not_overwrite_instance_contract() -> None:
    inst = _extracted_action()
    inst["outputs"] = [{"kind": "session_message", "channel": "telegram"}]
    inst["delivery_contract"] = {"user_facing": True, "window_minutes": 5}
    merged = _merge_scheduled_action_delivery([inst], [_spec_action()])
    a = merged[0]
    assert a["outputs"] == [{"kind": "session_message", "channel": "telegram"}]
    assert a["delivery_contract"]["window_minutes"] == 5  # instance kept


def test_merge_leaves_unmatched_actions_untouched() -> None:
    other = {"id": "other", "mechanism": "launchd", "outputs": []}
    merged = _merge_scheduled_action_delivery([other], [_spec_action()])
    assert merged[0] == other


def test_merge_does_not_mutate_inputs() -> None:
    inst = _extracted_action()
    _merge_scheduled_action_delivery([inst], [_spec_action()])
    assert inst["outputs"] == []
    assert "delivery_contract" not in inst


# ── hydrate_v7_arc_instance (end-to-end) ─────────────────────────────────────


def test_hydrate_restores_extracted_action_contract(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "p-brief01", "2026.06.11-1.0", [_spec_action()])
    inst = _instance("p-brief01", "2026.06.11-1.0", [_extracted_action()])
    hydrated = hydrate_v7_arc_instance(inst, tmp_path)
    action = hydrated["scheduled_actions"][0]
    assert any(o.get("channel") for o in action["outputs"])
    assert action["delivery_contract"]["user_facing"] is True


def test_hydrate_uses_spec_actions_when_instance_has_none(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "p-brief01", "2026.06.11-1.0", [_spec_action()])
    inst = _instance("p-brief01", "2026.06.11-1.0", [])
    hydrated = hydrate_v7_arc_instance(inst, tmp_path)
    assert hydrated["scheduled_actions"][0]["id"] == "briefing"
