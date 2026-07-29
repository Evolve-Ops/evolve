"""Schema v23 — delivery_contract{} on scheduled_actions[] entries.

Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §5. Exercises
the canonical shape validator (shared by Tier-2 and the delivery_monitor
daemon) and the migration behavior: the optional block round-trips
untouched and the version stamp moves to 23 with no backfill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    DELIVERY_EVIDENCE_KINDS,
    DELIVERY_HEAL_VALUES,
    MANIFEST_SCHEMA_VERSION,
    migrate_manifest,
    validate_delivery_contract,
)


_VALID = {
    "user_facing": True,
    "window_minutes": 30,
    "evidence": {
        "ran": {"kind": "scheduler_state"},
        "delivered": {"kind": "run_file", "path": "memory/briefing-runs/{date}.json"},
    },
    "heal": "rerun",
}


def test_valid_full_contract():
    assert validate_delivery_contract(_VALID) == []


def test_empty_contract_is_valid():
    # All fields optional — absence means derived Option-A defaults.
    assert validate_delivery_contract({}) == []


def test_non_dict_rejected():
    errs = validate_delivery_contract(["rerun"])
    assert errs and "must be a dict" in errs[0]


def test_unknown_key_rejected():
    errs = validate_delivery_contract({"window_mins": 30})
    assert any("window_mins" in e for e in errs)


def test_user_facing_must_be_bool():
    assert validate_delivery_contract({"user_facing": "yes"})


def test_window_minutes_validation():
    assert validate_delivery_contract({"window_minutes": 0})
    assert validate_delivery_contract({"window_minutes": "30"})
    assert validate_delivery_contract({"window_minutes": True})  # bool is not an int here
    assert validate_delivery_contract({"window_minutes": 1}) == []


def test_heal_enum():
    assert validate_delivery_contract({"heal": "kickstart"})
    for value in DELIVERY_HEAL_VALUES:
        assert validate_delivery_contract({"heal": value}) == []


def test_evidence_unknown_role_rejected():
    errs = validate_delivery_contract({"evidence": {"sent": {"kind": "run_file"}}})
    assert any("sent" in e for e in errs)


def test_evidence_kind_enum():
    errs = validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "telepathy"}}},
    )
    assert any("telepathy" in e for e in errs)
    assert "scheduler_state" in DELIVERY_EVIDENCE_KINDS


def test_run_file_requires_workspace_relative_path():
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "run_file"}}},
    )
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "run_file", "path": "/etc/passwd"}}},
    )
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "run_file", "path": "../escape.json"}}},
    )
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "run_file", "path": "memory/x/{date}.json"}}},
    ) == []


def test_signal_line_requires_pattern():
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "signal_line"}}},
    )
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "signal_line", "pattern": "SENT:", "log": ""}}},
    )
    assert validate_delivery_contract(
        {"evidence": {"delivered": {"kind": "signal_line", "pattern": "SENT:"}}},
    ) == []


def test_migration_stamps_v23_and_preserves_contract(tmp_path: Path) -> None:
    manifest = {
        "id": "test-app",
        "name": "Test App",
        "bot_id": "team_bot",
        "schema_version": 20,
        "scheduled_actions": [
            {
                "id": "act",
                "mechanism": "launchd",
                "delivery_contract": dict(_VALID),
            },
        ],
    }
    path = tmp_path / "test-app.json"
    path.write_text(json.dumps(manifest))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    # Constant-relative: the intent is "migration stamps the CURRENT
    # version"; the exact-value pin lives in
    # test_manifest_v13_scheduled_actions.test_schema_version_is_24.
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION >= 23
    # No backfill: the contract is optional and migration must not touch it.
    assert after["scheduled_actions"][0]["delivery_contract"] == _VALID


def test_migration_does_not_add_contract_to_plain_actions(tmp_path: Path) -> None:
    manifest = {
        "id": "test-app",
        "name": "Test App",
        "bot_id": "team_bot",
        "schema_version": 20,
        "scheduled_actions": [{"id": "act", "mechanism": "launchd"}],
    }
    path = tmp_path / "test-app.json"
    path.write_text(json.dumps(manifest))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert "delivery_contract" not in after["scheduled_actions"][0]
