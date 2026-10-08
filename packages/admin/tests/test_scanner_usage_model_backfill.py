"""Tests for the scanner_usage_model_backfill script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scanner_usage_model_backfill import (  # noqa: E402
    _apply_one,
    _classify_one,
    _current_model,
    backfill,
)


def _write_manifest(workspace: Path, app_id: str, body: dict) -> Path:
    mdir = workspace / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    p = mdir / f"{app_id}.json"
    p.write_text(json.dumps(body))
    return p


# ── _current_model ──────────────────────────────────────────────────

def test_current_model_top_level() -> None:
    assert _current_model({"usage": {"model": "scheduled"}}) == "scheduled"


def test_current_model_identity_nested() -> None:
    """Verifier accepts identity.usage.model too; backfill must see it."""
    m = {"identity": {"usage": {"model": "scheduled"}}}
    assert _current_model(m) == "scheduled"


def test_current_model_unset() -> None:
    assert _current_model({}) == ""
    assert _current_model({"usage": {}}) == ""
    assert _current_model({"usage": {"model": ""}}) == ""


# ── _classify_one ──────────────────────────────────────────────────

def test_classify_heartbeat_app(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, "x", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
        "heartbeat_evidence": {"file_path": "HEARTBEAT.md"},
    })
    current, inferred = _classify_one(p)
    assert current == ""
    assert inferred == "scheduled"


def test_classify_already_set_app(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, "x", {
        "usage": {"model": "user-initiated"},
        "scheduled_actions": [],
    })
    current, inferred = _classify_one(p)
    assert current == "user-initiated"
    assert inferred == "user-initiated"


# ── _apply_one ─────────────────────────────────────────────────────

def test_apply_writes_top_level_usage(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, "x", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
    })
    assert _apply_one(p, "scheduled")
    on_disk = json.loads(p.read_text())
    assert on_disk["usage"]["model"] == "scheduled"


def test_apply_preserves_existing_usage_fields(tmp_path: Path) -> None:
    p = _write_manifest(tmp_path, "x", {
        "usage": {"how_to_use": "say hello"},
    })
    assert _apply_one(p, "scheduled")
    on_disk = json.loads(p.read_text())
    assert on_disk["usage"]["how_to_use"] == "say hello"
    assert on_disk["usage"]["model"] == "scheduled"


# ── backfill() end-to-end ──────────────────────────────────────────

def test_backfill_dry_run_changes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path
    p = _write_manifest(workspace, "heartbeat-app", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
        "heartbeat_evidence": {"file_path": "HEARTBEAT.md"},
    })
    summary = backfill(workspace, apply=False)
    # Reported as would_apply, but file unchanged.
    assert summary["changed"] == 1
    on_disk = json.loads(p.read_text())
    assert (on_disk.get("usage") or {}).get("model") in (None, "")


def test_backfill_apply_writes_scheduled(tmp_path: Path) -> None:
    workspace = tmp_path
    p = _write_manifest(workspace, "heartbeat-app", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
        "heartbeat_evidence": {"file_path": "HEARTBEAT.md"},
    })
    summary = backfill(workspace, apply=True)
    assert summary["changed"] == 1
    on_disk = json.loads(p.read_text())
    assert on_disk["usage"]["model"] == "scheduled"


def test_backfill_skips_already_set(tmp_path: Path) -> None:
    workspace = tmp_path
    p = _write_manifest(workspace, "already-set", {
        "usage": {"model": "user-initiated"},
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
    })
    summary = backfill(workspace, apply=True)
    assert summary["skipped_set"] == 1
    assert summary["changed"] == 0
    on_disk = json.loads(p.read_text())
    assert on_disk["usage"]["model"] == "user-initiated"


def test_backfill_skips_user_initiated_inference(tmp_path: Path) -> None:
    """Don't write 'user-initiated' to a manifest that has nothing —
    that's the verifier's default already. Writing it would noise the
    trail without changing behavior."""
    workspace = tmp_path
    _write_manifest(workspace, "x", {})
    summary = backfill(workspace, apply=True)
    assert summary["skipped_unset_user_initiated"] == 1
    assert summary["changed"] == 0


def test_backfill_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path
    _write_manifest(workspace, "x", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
    })
    s1 = backfill(workspace, apply=True)
    s2 = backfill(workspace, apply=True)
    assert s1["changed"] == 1
    # Second pass: already set, skipped.
    assert s2["changed"] == 0
    assert s2["skipped_set"] == 1


def test_backfill_force_overrides_existing(tmp_path: Path) -> None:
    workspace = tmp_path
    p = _write_manifest(workspace, "x", {
        "usage": {"model": "user-initiated"},
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
    })
    summary = backfill(workspace, apply=True, skip_if_set=False)
    assert summary["changed"] == 1
    on_disk = json.loads(p.read_text())
    assert on_disk["usage"]["model"] == "scheduled"


def test_backfill_skips_dotfiles(tmp_path: Path) -> None:
    workspace = tmp_path
    mdir = workspace / "manifests"
    mdir.mkdir()
    (mdir / ".scan-status.json").write_text("{}")
    _write_manifest(workspace, "real-app", {
        "scheduled_actions": [{"trigger": {"kind": "heartbeat"}}],
    })
    summary = backfill(workspace, apply=False)
    # Only real-app counted; .scan-status skipped.
    assert summary["total"] == 1
