"""Regression tests for the app-scan early-exit incident (2026-05-22).

Two distinct bugs collided to produce a "scan exits after first or
second step without finding anything" user report on the Evolve admin
UI's Apps page:

1. The scanner's API-key reader hardcoded the auth-profile id
   ``"anthropic:api"``. evo's auth-profiles.json uses
   ``"anthropic:api_key"``. Result: the key reader returned ``""``,
   ``llm_discover_applications`` returned ``[]`` silently, and the
   pipeline raced to ``status=done, found=0``. (The key resolution has
   since moved onto the provider-agnostic ``infra_llm`` resolver,
   #3466 — the pins below cover its scanner-side wiring.)

2. ``list_manifests`` iterated the manifests dir without filtering
   dotfiles, so every call mutated ``.scan-status.json`` through
   ``migrate_manifest`` (fills in default manifest fields).

These tests pin both fixes. Credential + model resolution now flows
through ``infra_llm`` (pod tier config → the primary bot's credentialed
providers, with per-provider env overrides for the sudo'd scan
subprocess); and a missing key still DEGRADES the scan to structural
(--no-llm) rather than aborting with ``error_kind=missing_api_key``.
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


def test_resolve_llm_env_key_resolves_target(monkeypatch):
    """A provider env var (the admin server's SETENV injection into the
    scan subprocess) is enough for _resolve_llm to produce a usable
    (model, api_key) pair — no auth store read needed."""
    import infra_llm
    from infra_llm import InfraLLMTarget

    monkeypatch.setattr(
        infra_llm, "resolve_infra_llm",
        lambda tier, network=None: InfraLLMTarget(
            provider="anthropic",
            model="anthropic/claude-haiku-4-5",
            api_key="sk-ant-env-wins",
        ),
    )
    assert _scanner._resolve_llm("tier3") == (
        "anthropic/claude-haiku-4-5", "sk-ant-env-wins",
    )


def test_resolve_llm_none_when_no_provider(monkeypatch):
    """No credentialed provider → ("", "") so the pipeline degrades."""
    import infra_llm

    monkeypatch.setattr(infra_llm, "resolve_infra_llm", lambda tier, network=None: None)
    assert _scanner._resolve_llm("tier3") == ("", "")


def test_call_llm_openai_target_flows_through(monkeypatch):
    """The (model, api_key) plumbing round-trips a full infra_llm target
    — an openai-qualified model dispatches through the provider-agnostic
    client (#3466), not an Anthropic-only URL."""
    import infra_llm

    seen: dict = {}

    def fake_complete(target, *, prompt=None, **kwargs):
        seen["provider"] = target.provider
        seen["model"] = target.model
        seen["api_key"] = target.api_key
        return "llm says hi"

    monkeypatch.setattr(infra_llm, "complete", fake_complete)
    out = _scanner._call_llm("openai/gpt-4o-mini", "prompt", "sk-openai-fake")
    assert out == "llm says hi"
    assert seen == {
        "provider": "openai",
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-openai-fake",
    }


def test_call_llm_bare_model_or_empty_key_returns_empty():
    """A bare model id can't name a provider (never presume one) and an
    empty key can't authenticate — both short-circuit to ""."""
    assert _scanner._call_llm("claude-haiku-4-5", "p", "sk-fake") == ""
    assert _scanner._call_llm("openai/gpt-4o-mini", "p", "") == ""


# ── llm_discover_applications raises on missing key ──────────────────────────

def test_llm_discover_raises_when_key_missing(monkeypatch, tmp_path):
    """The LLM phase must abort loudly when no key resolves.

    Previously it returned [] and the pipeline thought "0 apps found"
    rather than "scan failed". This made the scan look like a fast
    success when it was actually broken — the reported UX symptom.
    """
    monkeypatch.setattr(_scanner, "_resolve_llm", lambda tier="tier3": ("", ""))

    inv = _scanner.WorkspaceInventory(workspace=tmp_path, bot_id="bot1")
    inv.user = "bot1"
    with pytest.raises(_scanner.MissingApiKeyError, match="No LLM provider credentialed"):
        _scanner.llm_discover_applications(inv, model="anthropic/claude-haiku-4-5")


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

    monkeypatch.setattr(_scanner, "_resolve_llm", lambda tier="tier3": ("", ""))

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
    assert status.get("llm_degraded_reason") == "no_llm_provider_key"
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
