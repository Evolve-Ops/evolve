"""tests/test_proposal_urgency_canonical.py — urgency-taxonomy drift detector.

Locks two invariants:

1. The canonical urgency set in ``migrations.proposal_urgency_normalize``
   matches the seven values declared by ``schema.proposal.Urgency`` and
   nothing else. Adding a value to the Literal without explicit intent
   here fails — adding to the taxonomy should be a deliberate edit.

2. ``iter_invalid`` + ``migrate_directory`` correctly classify and rewrite
   proposals carrying the three legacy values that escaped into on-disk
   data (``needs_attention``, ``discretionary``, ``cost_hygiene``) as
   well as proposals that pre-date the urgency field entirely.

The third invariant — that no live proposal on disk carries drift — is
enforced by the ``proposal_urgency_normalize`` CLI, intended to be run
once after this migration lands and re-run during periodic audits.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from migrations.proposal_urgency_normalize import (  # noqa: E402
    CANONICAL_URGENCIES,
    LEGACY_URGENCY_MAP,
    is_canonical,
    iter_invalid,
    migrate_directory,
)
from schema.proposal import Proposal, Urgency  # noqa: E402


# ── Drift detector ────────────────────────────────────────────────────────────


def test_canonical_set_matches_schema_literal_exactly():
    """If somebody adds or removes a value from Urgency, this fails.

    Forces the taxonomy change to land alongside an intentional update
    here (and to the LEGACY_URGENCY_MAP if removing a value).
    """
    from typing import get_args

    schema_values = frozenset(get_args(Urgency))
    expected = frozenset(
        {
            "security_critical",
            "operational_urgent",
            "cost_alert",
            "substrate_warn",
            "improvement",
            "hygiene",
            "whimsy",
        }
    )
    assert schema_values == expected
    assert CANONICAL_URGENCIES == expected


def test_legacy_map_targets_are_all_canonical():
    """Every legacy → canonical mapping must land in the canonical set."""
    for legacy, canonical in LEGACY_URGENCY_MAP.items():
        assert canonical in CANONICAL_URGENCIES, (
            f"Legacy mapping {legacy!r} -> {canonical!r} "
            "but target is not in CANONICAL_URGENCIES"
        )


def test_legacy_map_keys_are_not_in_canonical():
    """Catches the mistake of leaving a legacy value in the canonical set."""
    for legacy in LEGACY_URGENCY_MAP:
        assert legacy not in CANONICAL_URGENCIES


# ── is_canonical ──────────────────────────────────────────────────────────────


def test_is_canonical_accepts_each_canonical_value():
    for v in CANONICAL_URGENCIES:
        assert is_canonical(v)


def test_is_canonical_rejects_legacy_and_garbage():
    for v in LEGACY_URGENCY_MAP:
        assert not is_canonical(v)
    assert not is_canonical(None)
    assert not is_canonical("")
    assert not is_canonical(42)


# ── iter_invalid + migrate_directory fixtures ────────────────────────────────


def _write_proposal_json(path: Path, *, urgency: str | None, proposal_id: str) -> None:
    """Write a minimal proposal JSON to *path*. Omits urgency if None."""
    payload: dict = {
        "id": proposal_id,
        "schema_version": 2,
        "created_at": "2026-05-15T00:00:00+00:00",
        "bot_id": "team_bot_a",
        "generator_id": "test_fixture",
        "dimension": "capabilities",
        "trigger_observations": [],
        "provenance": {
            "technique": "fixture",
            "signals": {},
            "confidence": 0.5,
        },
        "problem": "fixture",
        "action": {
            "kind": "Investigation",
            "context": "fixture",
        },
        "risk_tag": {
            "blast_radius": "bot",
            "reversibility": "manual",
            "touches": [],
        },
        "approval_audience": "pod_operator",
        "admin_surface_summary": "fixture",
        "status": "pending",
        "history": [],
        "revisions": [],
        "motivating_signals": [],
        "signature": "",
    }
    if urgency is not None:
        payload["urgency"] = urgency
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.fixture
def proposals_root(tmp_path: Path) -> Path:
    root = tmp_path / "proposals"
    _write_proposal_json(
        root / "pending" / "canon-1.json",
        urgency="operational_urgent",
        proposal_id="canon-1",
    )
    _write_proposal_json(
        root / "pending" / "canon-2.json",
        urgency="hygiene",
        proposal_id="canon-2",
    )
    _write_proposal_json(
        root / "pending" / "legacy-needs.json",
        urgency="needs_attention",
        proposal_id="legacy-needs",
    )
    _write_proposal_json(
        root / "pending" / "legacy-disc.json",
        urgency="discretionary",
        proposal_id="legacy-disc",
    )
    _write_proposal_json(
        root / "snoozed" / "legacy-cost.json",
        urgency="cost_hygiene",
        proposal_id="legacy-cost",
    )
    _write_proposal_json(
        root / "applied" / "missing-urgency.json",
        urgency=None,
        proposal_id="missing-urgency",
    )
    return root


def test_iter_invalid_finds_all_drifted(proposals_root: Path):
    invalid_names = sorted(p.name for p, _ in iter_invalid(proposals_root))
    assert invalid_names == [
        "legacy-cost.json",
        "legacy-disc.json",
        "legacy-needs.json",
        "missing-urgency.json",
    ]


def test_iter_invalid_returns_actual_urgency_values(proposals_root: Path):
    by_name = {p.name: u for p, u in iter_invalid(proposals_root)}
    assert by_name["legacy-needs.json"] == "needs_attention"
    assert by_name["legacy-disc.json"] == "discretionary"
    assert by_name["legacy-cost.json"] == "cost_hygiene"
    assert by_name["missing-urgency.json"] is None


# ── migrate_directory ────────────────────────────────────────────────────────


def _read_urgency(path: Path) -> str | None:
    return json.loads(path.read_text(encoding="utf-8")).get("urgency")


def test_migrate_directory_rewrites_legacy_values(proposals_root: Path):
    report = migrate_directory(proposals_root)
    assert report["scanned"] == 6
    assert report["already_canonical"] == 2
    assert report["migrated"] == 4
    assert report["errors"] == []

    assert _read_urgency(
        proposals_root / "pending" / "legacy-needs.json"
    ) == "operational_urgent"
    assert _read_urgency(
        proposals_root / "pending" / "legacy-disc.json"
    ) == "improvement"
    assert _read_urgency(
        proposals_root / "snoozed" / "legacy-cost.json"
    ) == "hygiene"
    # Missing-urgency case round-trips through Proposal.from_dict, which
    # defaults to "improvement", then to_dict writes the field explicitly.
    assert _read_urgency(
        proposals_root / "applied" / "missing-urgency.json"
    ) == "improvement"


def test_migrate_directory_is_idempotent(proposals_root: Path):
    first = migrate_directory(proposals_root)
    second = migrate_directory(proposals_root)
    assert second["migrated"] == 0
    assert second["already_canonical"] == first["scanned"]
    assert second["errors"] == []


def test_migrate_directory_dry_run_doesnt_write(proposals_root: Path):
    before = _read_urgency(
        proposals_root / "pending" / "legacy-needs.json"
    )
    report = migrate_directory(proposals_root, dry_run=True)
    assert report["migrated"] == 4
    after = _read_urgency(
        proposals_root / "pending" / "legacy-needs.json"
    )
    assert before == after == "needs_attention"


def test_migrated_proposals_still_load_as_proposal(proposals_root: Path):
    migrate_directory(proposals_root)
    for path in proposals_root.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        proposal = Proposal.from_dict(data)
        assert proposal.urgency in CANONICAL_URGENCIES


def test_migrate_directory_handles_missing_root(tmp_path: Path):
    report = migrate_directory(tmp_path / "does-not-exist")
    assert report == {
        "scanned": 0,
        "already_canonical": 0,
        "migrated": 0,
        "errors": [],
        "changes": [],
    }


# ── Live-data check (opt-in via env var) ─────────────────────────────────────


def test_live_proposals_dir_has_no_drift():
    """Operator-driven validation: point EVOLVE_PROPOSALS_ROOT at the live
    shared dir and this test asserts every on-disk proposal carries a
    canonical urgency. Skipped in CI (no env var set).
    """
    root = os.environ.get("EVOLVE_PROPOSALS_ROOT")
    if not root:
        pytest.skip("Set EVOLVE_PROPOSALS_ROOT to validate live proposals dir")
    invalid = list(iter_invalid(Path(root)))
    assert not invalid, (
        "Non-canonical urgencies on disk: "
        + ", ".join(f"{p}={u!r}" for p, u in invalid)
    )
