"""Regression tests for the app-scan early-exit incident (2026-05-22).

Two distinct bugs collided to produce a "scan exits after first or
second step without finding anything" user report on the Evolve admin
UI's Apps page:

1. The scanner's API-key reader hardcoded the auth-profile id
   ``"anthropic:api"``. evo's auth-profiles.json uses
   ``"anthropic:api_key"``. Result: ``_read_api_key`` returned ``""``,
   ``llm_discover_applications`` returned ``[]`` silently, and the
   pipeline raced to ``status=done, found=0``.

2. ``list_manifests`` iterated the manifests dir without filtering
   dotfiles, so every call mutated ``.scan-status.json`` through
   ``migrate_manifest`` (fills in default manifest fields).

These tests pin both fixes. The auth SOURCE read now flows through
``evolve_admin.oc_store`` (per-agent sqlite store → legacy auth-profiles.json
→ transitional bak) after OpenClaw 2026.6 moved the JSON into sqlite; and a
missing key now DEGRADES the scan to structural (--no-llm) rather than aborting
with ``error_kind=missing_api_key``.
"""

from __future__ import annotations

import json
from pathlib import Path

import sys

import pytest

# The analyzer package is loaded via ``sys.path.insert`` inside the
# scanner; we mirror that here so the extractor import in the tests
# resolves the same way.
_ANALYZER_DIR = Path(__file__).resolve().parents[2] / "analyzer"
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.applications import scanner as _scanner
from evolve_admin.applications import manifest as _manifest


# ── _read_api_key: tolerant of profile-id variance ───────────────────────────

@pytest.mark.parametrize("pid", ["anthropic:api", "anthropic:api_key"])
def test_extract_anthropic_key_resolves_both_profile_ids(pid):
    """Both legacy ``anthropic:api`` and newer ``anthropic:api_key`` resolve.

    The shared extractor in ``primary_bot`` is now the source of truth
    for every callsite (scanner, server, forge_engine,
    app_posture_reflect). Before the fix, those callsites hardcoded
    ``profiles["anthropic:api"]`` and silently returned "" when the
    bot used the newer ``_key`` variant. Evo on the production mini
    was in exactly this state; every scan of the primary bot
    returned 0 results without an error.
    """
    from primary_bot import extract_anthropic_key  # type: ignore
    raw = {"profiles": {pid: {"type": "api_key", "key": "sk-ant-test123"}}}
    assert extract_anthropic_key(raw) == "sk-ant-test123"


def test_read_api_key_env_wins(monkeypatch):
    """ANTHROPIC_API_KEY env beats anything on disk."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-wins")
    assert _scanner._read_api_key("nonexistent-bot") == "sk-ant-env-wins"


def test_read_api_key_handles_anthropic_api_key_variant(monkeypatch, tmp_path):
    """Scanner's auth-file reader must resolve evo's ``anthropic:api_key`` shape.

    Repros the live mini state on 2026-05-22: evo's auth-profiles.json
    keys an Anthropic profile under ``anthropic:api_key`` (not the
    legacy ``anthropic:api``). The brittle hardcoded form this fix
    replaced returned "" for that file.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_home = tmp_path / "Users" / "bot1"
    profile_dir = fake_home / ".openclaw" / "agents" / "main" / "agent"
    profile_dir.mkdir(parents=True)
    (profile_dir / "auth-profiles.json").write_text(json.dumps({
        "profiles": {
            "anthropic:api_key": {"type": "api_key", "key": "sk-ant-newshape"},
            "brave_api_key": {"type": "api_key", "key": "brave-xxx"},
        }
    }))

    # The SOURCE read now flows through ``oc_store``, which resolves an account
    # name via the blessed ``evolve_config.user_home``. Point that at our temp
    # tree (no sqlite store present → legacy json source). oc_store imports
    # ``user_home`` lazily at call time, so patching the module attribute takes.
    import evolve_config as _evolve_config
    monkeypatch.setattr(_evolve_config, "user_home", lambda user: fake_home)
    assert _scanner._read_api_key("bot1", user="bot1") == "sk-ant-newshape"


# ── llm_discover_applications raises on missing key ──────────────────────────

def test_llm_discover_raises_when_key_missing(monkeypatch, tmp_path):
    """The LLM phase must abort loudly when no key resolves.

    Previously it returned [] and the pipeline thought "0 apps found"
    rather than "scan failed". This made the scan look like a fast
    success when it was actually broken — the reported UX symptom.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(_scanner, "_read_api_key", lambda bot_id, user=None: "")

    inv = _scanner.WorkspaceInventory(workspace=tmp_path, bot_id="bot1")
    inv.user = "bot1"
    with pytest.raises(_scanner.MissingApiKeyError, match="No Anthropic API key"):
        _scanner.llm_discover_applications(inv, model="claude-haiku-4-5")


def test_pipeline_degrades_to_structural_on_missing_key(monkeypatch, tmp_path):
    """When LLM is requested but no key resolves, the scan DEGRADES to a
    structural (--no-llm) scan and finishes ``status=done`` with an
    ``llm_degraded`` marker — NOT a red ``error_kind=missing_api_key``.

    A bot may legitimately have no raw Anthropic key (gateway-token auth) or
    be mid-OC-migration; aborting the whole scan there denied the operator the
    structural results the scanner can still produce. The degrade marker keeps
    the "no LLM key" signal visible without painting the scan as failed.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    output_dir = tmp_path / "manifests"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(_scanner, "_read_api_key", lambda bot_id, user=None: "")

    # Avoid the launchctl subprocess in inventory collection
    monkeypatch.setattr(_scanner, "_snapshot_launchctl_labels", lambda bot_id: [])
    monkeypatch.setattr(_scanner, "_collect_crons", lambda bot_id, ws: [])

    result = _scanner.scan_workspace_pipeline(
        workspace=workspace,
        bot_id="bot1",
        shared_dir=tmp_path / "shared",
        config={},
        use_llm=True,
        output_dir=output_dir,
        user="bot1",
    )

    assert result == []
    status = json.loads((output_dir / ".scan-status.json").read_text())
    assert status["status"] == "done"
    assert status.get("llm_degraded") is True
    assert status.get("llm_degraded_reason") == "no_anthropic_key"
    # The misleading hard-error must be gone.
    assert status.get("error_kind") != "missing_api_key"
    assert "error_kind" not in status
    # The log accumulator made it into the status file, so the UI has
    # something to render past the phase number.
    assert status.get("log"), "scan_log should be persisted into the status file"


# ── list_manifests / migrate_manifest: don't clobber .scan-status.json ──────

def test_list_manifests_skips_dotfiles(tmp_path):
    """``.scan-status.json`` must not be picked up by manifest enumeration.

    Before the fix, ``list_manifests`` called ``migrate_manifest`` on
    every ``*.json`` (no dotfile filter), and ``migrate_manifest``
    happily filled in default manifest fields and rewrote the file.
    Result: every scan-status.json on the production mini had
    schema_version, identity, manifest_shape, etc. baked in.
    """
    # Lay out the shape ``list_manifests`` expects under shared_dir
    shared = tmp_path / "shared"
    bot_home = tmp_path / "Users" / "bot1"
    manifests_dir = bot_home / ".openclaw" / "workspace" / "manifests"
    manifests_dir.mkdir(parents=True)
    # A real manifest
    (manifests_dir / "app1.json").write_text(json.dumps({
        "id": "app1", "name": "App One", "bot_id": "bot1",
        "manifest_type": "evolve_application", "schema_version": 1,
    }))
    # The scan-status file we want preserved
    status_payload = {
        "phase": 4, "phase_total": 4, "status": "done", "found": 2,
        "updated_at": "2026-05-23T01:00:00Z",
    }
    status_path = manifests_dir / ".scan-status.json"
    status_path.write_text(json.dumps(status_payload))
    status_before = status_path.read_text()

    # Point applications_dir lookup at our tmp tree
    import evolve_admin.applications.manifest as m
    orig_get_workspace = None
    try:
        from evolve_admin import config as _cfg
        orig_get_workspace = _cfg.get_bot_workspace
        _cfg.get_bot_workspace = lambda bot_id: bot_home / ".openclaw" / "workspace"
        manifests = m.list_manifests(shared, "bot1")
    finally:
        if orig_get_workspace is not None:
            _cfg.get_bot_workspace = orig_get_workspace

    # Only the real manifest is returned
    assert [x.id for x in manifests] == ["app1"]
    # And .scan-status.json is byte-for-byte unchanged
    assert status_path.read_text() == status_before, (
        "list_manifests must not mutate .scan-status.json — migrate_manifest "
        "filled in default manifest fields here before the fix"
    )


def test_migrate_manifest_refuses_non_manifest(tmp_path):
    """``migrate_manifest`` on a non-manifest JSON is a no-op.

    Defensive: even if a caller passes the wrong path, we never fill in
    default manifest fields on a file that doesn't have an ``id``.
    """
    target = tmp_path / "not-a-manifest.json"
    payload = {"phase": 4, "status": "done", "found": 0}
    target.write_text(json.dumps(payload))
    before = target.read_text()
    _manifest.migrate_manifest(target)
    assert target.read_text() == before
