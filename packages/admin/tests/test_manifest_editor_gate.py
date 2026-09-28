"""Tests that save_manifest_with_provenance enforces the coherence gate
for operator-driven writes.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §6.6.

The gate fires when source is user_authored (admin UI manifest editor)
or confirmed (Mark as ready click). Other sources — observational,
forge_built, bot_authored — bypass the gate because they're either
re-recording reality or have their own explicit gate elsewhere
(forge_built has approve_forge_job).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    ForgeCoherenceGateError,
    PROVENANCE_BOT_AUTHORED,
    PROVENANCE_CONFIRMED,
    PROVENANCE_FORGE_BUILT,
    PROVENANCE_OBSERVATIONAL,
    PROVENANCE_USER_AUTHORED,
    save_manifest_with_provenance,
)


@pytest.fixture(autouse=True)
def _stub_workspace(tmp_path, monkeypatch):
    """Stub get_bot_workspace so save_manifest writes to tmp_path."""
    workspace_root = tmp_path / "bot-workspace"
    workspace_root.mkdir()

    def _stub(bot_id: str):
        return workspace_root

    import evolve_admin.config as _config
    monkeypatch.setattr(_config, "get_bot_workspace", _stub)
    yield workspace_root


def _clean_manifest():
    return ApplicationManifest(
        id="myapp", bot_id="bot-x",
        description="A one-shot script the operator invokes.",
        name="My App", schema_version=20,
    )


def _incoherent_manifest():
    """Manifest with Pass A C-A1 critical: claims daily but no triggers."""
    return ApplicationManifest(
        id="myapp", bot_id="bot-x",
        description="Sends a daily briefing every morning.",
        name="My App", schema_version=20,
        scheduled_actions=[], crons=[],
    )


# ── Gate fires for user_authored (admin UI manifest editor) ─────────

def test_user_authored_clean_passes(tmp_path):
    """A clean manifest written by the admin UI editor goes through."""
    manifest = _clean_manifest()
    save_manifest_with_provenance(
        manifest, tmp_path, source=PROVENANCE_USER_AUTHORED,
        by="operator", via="manifest_editor",
    )


def test_user_authored_incoherent_blocked(tmp_path):
    """Operator UI edit of an incoherent manifest is refused at save."""
    manifest = _incoherent_manifest()
    with pytest.raises(ForgeCoherenceGateError) as exc:
        save_manifest_with_provenance(
            manifest, tmp_path, source=PROVENANCE_USER_AUTHORED,
            by="operator", via="manifest_editor",
        )
    assert "C-A1" in str(exc.value) or "daily" in str(exc.value)
    assert "override_key=" in str(exc.value)


def test_user_authored_with_matching_override_passes(tmp_path):
    """Operator can override by setting provenance.gate_override_key."""
    manifest = _incoherent_manifest()
    # First call: discover the key.
    try:
        save_manifest_with_provenance(
            manifest, tmp_path, source=PROVENANCE_USER_AUTHORED,
        )
        pytest.fail("expected gate to raise")
    except ForgeCoherenceGateError as exc:
        key = exc.override_key

    # Set the override key in provenance and retry.
    manifest.provenance = {"gate_override_key": key}
    save_manifest_with_provenance(
        manifest, tmp_path, source=PROVENANCE_USER_AUTHORED,
        by="operator", via="manifest_editor",
    )


# ── Gate fires for confirmed (Mark as ready) ────────────────────────

def test_confirmed_incoherent_blocked(tmp_path):
    """An explicit 'Mark as ready' click on an incoherent manifest is
    refused — confirmed is the strongest provenance source; can't be
    used to shortcut around incoherence."""
    manifest = _incoherent_manifest()
    with pytest.raises(ForgeCoherenceGateError):
        save_manifest_with_provenance(
            manifest, tmp_path, source=PROVENANCE_CONFIRMED,
            by="operator", via="mark_ready",
        )


# ── Gate does NOT fire for observational / forge_built / bot_authored ──

def test_observational_incoherent_passes(tmp_path):
    """Scanner re-stamping records reality, not intent — bypass gate.

    Without this, every existing pre-rearchitect manifest with drift
    would fail to re-scan, locking the operator out of fixing them.
    """
    manifest = _incoherent_manifest()
    save_manifest_with_provenance(
        manifest, tmp_path, source=PROVENANCE_OBSERVATIONAL,
        by="scanner", via="phase_5",
    )


def test_bot_authored_incoherent_passes(tmp_path):
    """The evo handler's promote operation runs as bot_authored. Bot
    writes don't fire the gate (the bot is acting on operator's
    behalf via chat — they already authorized via the command)."""
    manifest = _incoherent_manifest()
    save_manifest_with_provenance(
        manifest, tmp_path, source=PROVENANCE_BOT_AUTHORED,
        by="evo:app-changes", via="evo",
    )


def test_forge_built_incoherent_passes_at_this_layer(tmp_path):
    """Forge writes bypass the gate here — they have their own explicit
    gate inside approve_forge_job (PR #2323 wiring). Defense in depth:
    forge always passes through approve_forge_job, which calls
    validate_coherence_gate directly. So gating again here would be
    double-enforcement (harmless but wasteful)."""
    manifest = _incoherent_manifest()
    save_manifest_with_provenance(
        manifest, tmp_path, source=PROVENANCE_FORGE_BUILT,
        by="forge:j-abc", via="forge_apply",
    )
