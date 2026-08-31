"""Tests for session_surface.load_autonomy_block — the procedural
enforcement surface of the autonomy ladder (spec §2.3)."""
from __future__ import annotations

from pathlib import Path

import pytest

import session_surface as ss
from autonomy import store as _store


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


def test_no_posture_file_no_block(shared_dir: Path):
    assert ss.load_autonomy_block("alpha", shared_dir) == ""
    assert ss.load_autonomy_block(None, shared_dir) == ""


def test_deliberate_posture_renders_guidance(shared_dir: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="draft_only", actor="operator_ui",
    )
    block = ss.load_autonomy_block("alpha", shared_dir)
    assert "[AUTONOMY" in block
    assert "Drafts only" in block
    assert "Google Workspace" in block
    assert "Never send, forward, or delete" in block


def test_backfilled_posture_not_injected(shared_dir: Path):
    """Observe-first (§5.2): instructing a previously-free bot to start
    asking would be a behavior change nobody decided on."""
    _store.ensure_entry(
        shared_dir, "alpha", "google_workspace",
        kind="email", rung="act_with_approval", actor=_store.ACTOR_BACKFILL,
    )
    assert ss.load_autonomy_block("alpha", shared_dir) == ""


def test_malformed_file_soft_fails(shared_dir: Path):
    path = _store.autonomy_path(shared_dir, "alpha")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert ss.load_autonomy_block("alpha", shared_dir) == ""


def test_block_lands_in_session_prefix(shared_dir: Path):
    _store.set_posture(
        shared_dir, "alpha", "google_workspace",
        rung="act_with_approval", actor="operator_ui",
    )
    block = ss.load_autonomy_block("alpha", shared_dir)
    prefix = ss.build_session_prefix(autonomy_block=block)
    assert "Asks first" in prefix
    # Limits land before the role scaffolds / state blocks.
    assert prefix.index("[AUTONOMY") < len(prefix)
