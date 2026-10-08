"""Tests for autonomy.store — schema v1, CAS discipline, append-only history."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomy import catalog as _catalog
from autonomy import store as _store


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def test_set_posture_creates_entry_with_history(shared_dir: Path):
    p = _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="shipped_default",
    )
    assert p.rung == "draft_only"
    assert p.kind == "email"  # resolved from the integration binding
    assert p.set_by == {"actor": "shipped_default"}
    assert p.set_at
    assert len(p.history) == 1
    assert p.history[0]["from"] is None
    assert p.history[0]["to"] == "draft_only"
    assert p.history[0]["actor"] == "shipped_default"

    path = _store.autonomy_path(shared_dir, "alpha")
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == _store.SCHEMA_VERSION
    assert on_disk["bot_id"] == "alpha"
    assert "google_workspace" in on_disk["integrations"]


def test_promotion_appends_history(shared_dir: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="shipped_default",
    )
    p = _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui", note="allow more",
    )
    assert p.rung == "act_with_approval"
    assert len(p.history) == 2
    assert p.history[1]["from"] == "draft_only"
    assert p.history[1]["to"] == "act_with_approval"
    assert p.history[1]["actor"] == "operator_ui"
    assert p.history[1]["note"] == "allow more"
    assert p.set_by == {"actor": "operator_ui", "note": "allow more"}


def test_cas_mismatch_raises_stale(shared_dir: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="shipped_default",
    )
    with pytest.raises(_store.StalePostureError):
        _store.set_posture(
            shared_dir, "alpha", "google_workspace",
            rung="act_with_approval", actor="operator_ui",
            expected_current_rung="act_with_approval",  # stale view
        )
    # Matching expectation succeeds.
    p = _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui",
        expected_current_rung="draft_only",
    )
    assert p.rung == "act_with_approval"


def test_cas_expected_absent(shared_dir: Path):
    # expected_current_rung=None means "I believe no entry exists".
    p = _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="shipped_default",
        expected_current_rung=None,
    )
    assert p.rung == "draft_only"
    with pytest.raises(_store.StalePostureError):
        _store.set_posture(
            shared_dir, "alpha", "google_workspace",
            rung="draft_only", actor="shipped_default",
            expected_current_rung=None,
        )


def test_rung3_requires_rules(shared_dir: Path):
    with pytest.raises(ValueError, match="non-empty rules"):
        _store.set_posture(
            shared_dir, "alpha", "google_workspace",
            rung="autonomous_within_rules", actor="operator_ui",
        )
    p = _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="autonomous_within_rules", actor="operator_ui",
        rules={"reach_allow": ["*@example-company.com"], "actions_per_day": 20},
    )
    assert p.rules["actions_per_day"] == 20


def test_rules_rejected_below_rung3(shared_dir: Path):
    with pytest.raises(ValueError, match="only valid at rung"):
        _store.set_posture(
            shared_dir, "alpha", "google_workspace",
            rung="draft_only", actor="operator_ui",
            rules={"actions_per_day": 5},
        )


def test_unknown_rung_rejected(shared_dir: Path):
    with pytest.raises(ValueError, match="unknown rung"):
        _store.set_posture(
            shared_dir, "alpha", "google_workspace",
            rung="yolo", actor="operator_ui",
        )


def test_unknown_integration_without_kind_rejected(shared_dir: Path):
    with pytest.raises(ValueError, match="unknown kind"):
        _store.set_posture(
            shared_dir, "alpha", "mystery_server",
            rung="draft_only", actor="operator_ui",
        )


def test_ensure_entry_find_or_create(shared_dir: Path):
    p1, created1 = _store.ensure_entry(
        shared_dir, "alpha", "google_workspace",
        kind="email", rung="act_with_approval",
        actor=_store.ACTOR_BACKFILL,
    )
    assert created1
    p2, created2 = _store.ensure_entry(
        shared_dir, "alpha", "google_workspace",
        kind="email", rung="draft_only",   # would-be different rung
        actor=_store.ACTOR_BACKFILL,
    )
    assert not created2
    assert p2.rung == "act_with_approval"  # existing entry untouched
    assert len(p2.history) == 1


def test_record_enforcement_no_history_change(shared_dir: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="shipped_default",
    )
    records = [
        {"surface": "mcp_tool_allowlist", "mode": "mechanical",
         "rendered_at": "2026-06-10T00:00:00Z", "verified": True},
    ]
    p = _store.record_enforcement(shared_dir, "alpha", "google_workspace", records)
    assert p is not None
    assert p.enforcement == records
    assert len(p.history) == 1  # not a posture change
    assert p.set_by == {"actor": "shipped_default"}


def test_load_malformed_raises(shared_dir: Path):
    path = _store.autonomy_path(shared_dir, "alpha")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        _store.load(shared_dir, "alpha")


def test_load_unknown_rung_raises(shared_dir: Path):
    path = _store.autonomy_path(shared_dir, "alpha")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 1, "bot_id": "alpha",
        "integrations": {"google_workspace": {"kind": "email", "rung": "wide_open"}},
    }))
    with pytest.raises(ValueError, match="rung"):
        _store.load(shared_dir, "alpha")


def test_email_defaults_ship_in_code():
    """§5.1 guard: the kind table is code-shipped product data."""
    assert _catalog.KIND_SPECS["email"].default_rung == "draft_only"
    assert _catalog.DEFAULT_RUNG_BY_KIND["email"] == "draft_only"
    assert _catalog.DEFAULT_RUNG_FALLBACK == "draft_only"
