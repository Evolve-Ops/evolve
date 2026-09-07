"""Tests for forge_engine._bake_result_harness — the forge integration that
bakes the execution-integrity floor into a forged app.

Confirms: the wrapper file lands in the bot workspace, the honesty bot_guidance
section is merged onto the manifest, both are idempotent, and the merged
section does NOT trip the bot_guidance freelance-bypass gate (Phase 5c).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_result as ar  # noqa: E402
from evolve_admin.applications import forge_engine as fe  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402
from evolve_admin.applications.bot_guidance_freelance_validator import (  # noqa: E402
    validate_bot_guidance,
)


class _Job:
    job_id = "j-test1234"
    bot_id = "team_bot_a"


def _bake(tmp_path, manifest, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(fe, "_resolve_workspace_root", lambda bot_id: ws)
    fe._bake_result_harness(_Job(), manifest, tmp_path)
    return ws


def test_bake_writes_wrapper_and_guidance(tmp_path, monkeypatch):
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    ws = _bake(tmp_path, m, monkeypatch)

    wrapper = ws / ar.WRAPPER_WORKSPACE_RELPATH
    assert wrapper.exists()
    assert wrapper.read_text(encoding="utf-8") == ar.WRAPPER_SOURCE

    titles = [s.get("section") for s in m.bot_guidance]
    assert ar.BOT_GUIDANCE_SECTION in titles


def test_bake_is_idempotent(tmp_path, monkeypatch):
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    _bake(tmp_path, m, monkeypatch)
    # Second bake on the same manifest/workspace: still one section, one file.
    ws = tmp_path / "workspace"
    monkeypatch.setattr(fe, "_resolve_workspace_root", lambda bot_id: ws)
    fe._bake_result_harness(_Job(), m, tmp_path)
    assert sum(s.get("section") == ar.BOT_GUIDANCE_SECTION
               for s in m.bot_guidance) == 1


def test_bake_preserves_existing_guidance(tmp_path, monkeypatch):
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    m.bot_guidance = [{"section": "How to use", "content": "do the thing"}]
    _bake(tmp_path, m, monkeypatch)
    titles = [s.get("section") for s in m.bot_guidance]
    assert "How to use" in titles
    assert ar.BOT_GUIDANCE_SECTION in titles


def test_harness_guidance_does_not_trip_freelance_gate(tmp_path, monkeypatch):
    # A realistic at-risk-shaped app (references a scripts/ file) gains the
    # harness section. The gate may still flag the app on the pre-existing
    # script reference (correct — the badge stays), but the result must never
    # be a hard FAIL purely from our added section, and our section must not be
    # what introduces an at-risk marker.
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    m.bot_guidance = ar.merge_bot_guidance(None)
    res = validate_bot_guidance(m)
    # validate_bot_guidance returns a dict with "ok"; our section alone (no
    # script reference, no at-risk prose) must keep it OK.
    assert res["ok"] is True


def test_bake_survives_unwritable_workspace(tmp_path, monkeypatch):
    # Wrapper write failure must NOT raise (best-effort floor); guidance still
    # merges because it runs first and is pure in-memory.
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")

    def _boom(bot_id):
        raise OSError("no workspace")

    monkeypatch.setattr(fe, "_resolve_workspace_root", _boom)
    fe._bake_result_harness(_Job(), m, tmp_path)  # must not raise
    assert any(s.get("section") == ar.BOT_GUIDANCE_SECTION
               for s in m.bot_guidance)
