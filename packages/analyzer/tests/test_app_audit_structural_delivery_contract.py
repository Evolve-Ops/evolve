"""Tier-2 delivery_contract assertions (v25).

Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §5 + §11.
``delivery_contract_invalid`` (shape) and
``delivery_contract_evidence_undeclared`` (declared run_file evidence
must appear in interface_contract.data_files), including the
{date} ↔ YYYY-MM-DD normalization both directions.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    ASSERTION_IDS,
    DEFAULT_ASSERTIONS,
    check_delivery_contract,
)


def _manifest(contract: dict | None, data_files: list[str]) -> dict:
    action: dict = {"id": "act-1", "mechanism": "launchd"}
    if contract is not None:
        action["delivery_contract"] = contract
    return {
        "id": "test-app",
        "scheduled_actions": [action],
        "interface_contract": {
            "data_files": [{"path": p} for p in data_files],
        },
    }


_GOOD = {
    "user_facing": True,
    "window_minutes": 30,
    "evidence": {
        "delivered": {"kind": "run_file", "path": "memory/runs/{date}.json"},
    },
    "heal": "rerun",
}


def test_assertions_registered():
    assert "delivery_contract_invalid" in ASSERTION_IDS
    assert "delivery_contract_evidence_undeclared" in ASSERTION_IDS
    assert check_delivery_contract in DEFAULT_ASSERTIONS


def test_no_contract_no_findings():
    assert check_delivery_contract(_manifest(None, []), {}) == []


def test_valid_contract_with_declared_evidence_passes():
    m = _manifest(_GOOD, ["memory/runs/YYYY-MM-DD.json"])
    assert check_delivery_contract(m, {}) == []


def test_date_spelling_normalizes_both_directions():
    # Contract spells YYYY-MM-DD literally; data_files uses {date}.
    contract = {
        "evidence": {"delivered": {
            "kind": "run_file", "path": "memory/runs/YYYY-MM-DD.json",
        }},
    }
    m = _manifest(contract, ["memory/runs/{date}.json"])
    assert check_delivery_contract(m, {}) == []


def test_malformed_contract_fires_major():
    m = _manifest({"heal": "kickstart", "window_minutes": 0}, [])
    findings = check_delivery_contract(m, {})
    assert [f.assertion_id for f in findings] == ["delivery_contract_invalid"]
    assert findings[0].severity == "major"
    assert findings[0].evidence["action_id"] == "act-1"
    assert len(findings[0].evidence["errors"]) == 2


def test_undeclared_run_file_evidence_fires_minor():
    m = _manifest(_GOOD, ["memory/other.json"])
    findings = check_delivery_contract(m, {})
    assert [f.assertion_id for f in findings] == [
        "delivery_contract_evidence_undeclared",
    ]
    assert findings[0].severity == "minor"
    assert findings[0].evidence["path"] == "memory/runs/YYYY-MM-DD.json"


def test_static_run_file_path_matches_exactly():
    contract = {
        "evidence": {"delivered": {
            "kind": "run_file", "path": "memory/evening-sweep-last.json",
        }},
    }
    m = _manifest(contract, ["memory/evening-sweep-last.json"])
    assert check_delivery_contract(m, {}) == []


def test_signal_line_and_scheduler_state_evidence_need_no_declaration():
    for delivered in (
        {"kind": "signal_line", "pattern": "SENT:"},
        {"kind": "scheduler_state"},
    ):
        m = _manifest({"evidence": {"delivered": delivered}}, [])
        assert check_delivery_contract(m, {}) == []


def test_malformed_contract_does_not_double_fire_evidence_check():
    contract = {
        "heal": "kickstart",
        "evidence": {"delivered": {"kind": "run_file", "path": "memory/x.json"}},
    }
    m = _manifest(contract, [])
    findings = check_delivery_contract(m, {})
    assert [f.assertion_id for f in findings] == ["delivery_contract_invalid"]
