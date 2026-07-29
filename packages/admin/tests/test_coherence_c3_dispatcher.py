"""Tests for the Pass C3 LLM dispatcher.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

The dispatcher itself is the bit that takes (system_prompt, user_prompt),
calls Anthropic, parses the response, and writes
``manifest.coherence.last_capability_check``. The pure C3 module already
has its own tests; this file focuses on the dispatch wiring.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Stub get_bot_workspace so manifests live under tmp_path."""
    workspace_root = tmp_path / "bot-workspace"
    workspace_root.mkdir()

    def _stub_workspace(bot_id: str):
        return workspace_root

    import evolve_admin.config as _config
    monkeypatch.setattr(_config, "get_bot_workspace", _stub_workspace)
    try:
        monkeypatch.setattr(
            "evolve_admin.applications.manifest.get_bot_workspace",
            _stub_workspace, raising=False,
        )
    except Exception:
        pass
    yield workspace_root


def _write_manifest(workspace_root: Path, bot_id: str, manifest: dict) -> Path:
    """Drop a manifest into the per-bot workspace."""
    mdir = workspace_root / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    app_id = manifest.get("id") or "app1"
    p = mdir / f"{app_id}.json"
    # Add the minimal required fields ApplicationManifest.from_dict needs.
    manifest.setdefault("bot_id", bot_id)
    manifest.setdefault("name", manifest.get("id", "app1"))
    manifest.setdefault("description", "test application")
    p.write_text(json.dumps(manifest))
    return p


def _read_manifest(workspace_root: Path, app_id: str) -> dict:
    return json.loads((workspace_root / "manifests" / f"{app_id}.json").read_text())


# ── should_run_c3 / preflight skip paths ───────────────────────────────


def test_dispatch_rejects_unknown_trigger(workspace):
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    result = dispatch_c3(
        bot_id="bot-x", app_id="any", trigger="totally-bogus",
        shared_dir=workspace,
    )
    assert result.ok is False
    assert result.skipped is True
    assert "unknown trigger" in result.reason


def test_dispatch_skips_when_manifest_missing(workspace):
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    result = dispatch_c3(
        bot_id="bot-x", app_id="missing-app", trigger="on_demand",
        shared_dir=workspace,
    )
    assert result.ok is False
    assert result.skipped is True
    assert "manifest not found" in result.reason


def test_dispatch_skips_when_rate_limited(workspace):
    """Recent check (within 24h) — dispatcher must skip without calling LLM."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    recent_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "coherence": {"last_capability_check": {
            "severity": "feasible",
            "rationale": "looks fine",
            "checked_at": recent_iso,
        }},
    })

    # If the dispatcher accidentally called the LLM, this monkeypatch would
    # explode. The skip path must not reach it.
    def _boom(*args, **kwargs):
        raise AssertionError("LLM call must not happen on rate-limited path")

    from evolve_admin.applications import coherence_c3_dispatcher as _d
    _d._call_anthropic = _boom  # type: ignore[attr-defined]

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="on_demand",
        shared_dir=workspace,
    )
    assert result.ok is False
    assert result.skipped is True
    assert "rate-limited" in result.reason


def test_dispatch_skips_charter_change_without_before(workspace):
    """charter_change trigger requires a before snapshot — without it,
    should_run_c3 says no and the dispatcher honors that."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {"id": "j"})
    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="charter_change",
        shared_dir=workspace,
        before_manifest=None,
    )
    assert result.ok is False
    assert result.skipped is True
    assert "charter_change requires prior manifest" in result.reason


def test_dispatch_skips_charter_change_no_field_changed(workspace):
    """charter_change with before==after on charter fields → skip."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "same",
    })
    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="charter_change",
        shared_dir=workspace,
        before_manifest={"description": "same"},
    )
    assert result.ok is False
    assert result.skipped is True
    assert "no charter fields changed" in result.reason


# ── End-to-end with mocked LLM ────────────────────────────────────────


def _install_mock_anthropic(monkeypatch, response_text: str, tokens: int = 5000):
    """Replace _call_anthropic with a stub returning canned output."""
    from evolve_admin.applications import coherence_c3_dispatcher as _d

    calls: list[dict] = []

    def _stub_call(*, system_prompt, user_message, model, api_key):
        calls.append({
            "system_prompt": system_prompt,
            "user_message": user_message,
            "model": model,
            "api_key_present": bool(api_key),
        })
        return response_text, tokens

    monkeypatch.setattr(_d, "_call_anthropic", _stub_call)
    monkeypatch.setattr(_d, "_resolve_primary_bot_key", lambda _net: "sk-test")
    monkeypatch.setattr(
        _d, "_resolve_judge_model", lambda _net: "anthropic/claude-haiku-4-5"
    )
    return calls


def test_dispatch_writes_capability_check_on_success(workspace, monkeypatch):
    """Happy path: LLM returns valid JSON → verdict cached on manifest."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "Summarize Slack threads weekly",
    })
    calls = _install_mock_anthropic(monkeypatch, json.dumps({
        "severity": "feasible",
        "rationale": "Manifest hangs together — Slack inputs declared, "
                     "weekly trigger present, summary output sensible.",
    }))

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="on_demand",
        shared_dir=workspace,
    )

    # Verify dispatch result
    assert result.ok is True, result.error
    assert result.skipped is False
    assert result.check is not None
    assert result.check.severity == "feasible"
    assert "Slack inputs declared" in result.check.rationale
    assert result.check.triggered_by == "on_demand"
    assert result.check.checked_at  # ISO timestamp written

    # Verify the LLM call shape
    assert len(calls) == 1
    assert "incoherent" in calls[0]["system_prompt"]  # C3 prompt
    assert "Summarize Slack threads weekly" in calls[0]["user_message"]

    # Verify it landed on disk
    persisted = _read_manifest(workspace, "j")
    cap_check = persisted["coherence"]["last_capability_check"]
    assert cap_check["severity"] == "feasible"
    assert cap_check["triggered_by"] == "on_demand"
    assert cap_check["checked_at"]


def test_dispatch_handles_incoherent_verdict(workspace, monkeypatch):
    """LLM says incoherent → verdict cached with incoherent severity."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "Summarize Telegram messages",
        # No telegram integration declared anywhere on the manifest.
    })
    _install_mock_anthropic(monkeypatch, json.dumps({
        "severity": "incoherent",
        "rationale": "Description mentions Telegram but the manifest "
                     "declares no Telegram inputs or integrations.",
    }))

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="forge_approval",
        shared_dir=workspace,
    )
    assert result.ok is True
    assert result.check.severity == "incoherent"
    assert result.check.triggered_by == "forge_approval"


def test_dispatch_returns_error_on_unparseable_response(workspace, monkeypatch):
    """LLM returns garbage → dispatcher surfaces parse error + raw body."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {"id": "j"})
    _install_mock_anthropic(monkeypatch, "not json — no JSON here at all")

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="on_demand",
        shared_dir=workspace,
    )
    assert result.ok is False
    assert result.skipped is False
    assert "did not parse" in result.error
    assert "not json" in result.raw_response

    # Manifest must NOT have a cached verdict from this failed call.
    persisted = _read_manifest(workspace, "j")
    assert (persisted.get("coherence") or {}).get(
        "last_capability_check") in (None, {})


def test_dispatch_returns_error_when_api_key_missing(workspace, monkeypatch):
    """No primary-bot key → error, no LLM call attempted."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3
    from evolve_admin.applications import coherence_c3_dispatcher as _d

    _write_manifest(workspace, "bot-x", {"id": "j"})
    monkeypatch.setattr(_d, "_resolve_primary_bot_key", lambda _net: "")

    # Belt-and-braces: any LLM call here would be a regression.
    def _boom(*args, **kwargs):
        raise AssertionError("LLM must not be called without an API key")

    monkeypatch.setattr(_d, "_call_anthropic", _boom)

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="on_demand",
        shared_dir=workspace,
    )
    assert result.ok is False
    assert result.skipped is False
    assert "no Anthropic API key" in result.error


def test_dispatch_cost_estimate_non_zero(workspace, monkeypatch):
    """The result.cost_estimate_usd must be set so operator surfaces can
    show what the call cost."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {"id": "j"})
    _install_mock_anthropic(monkeypatch, json.dumps({
        "severity": "feasible", "rationale": "looks fine",
    }), tokens=5000)

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="on_demand",
        shared_dir=workspace,
    )
    assert result.ok is True
    # 5000 tokens at $0.80/MTok = $0.004. Estimate is rough so just assert
    # non-zero and within an order of magnitude of the spec target.
    assert 0 < result.cost_estimate_usd < 0.1


def test_dispatch_charter_change_with_real_field_change(workspace, monkeypatch):
    """charter_change with a charter-field edit → dispatch runs."""
    from evolve_admin.applications.coherence_c3_dispatcher import dispatch_c3

    _write_manifest(workspace, "bot-x", {
        "id": "j",
        "description": "AFTER — new design",
    })
    _install_mock_anthropic(monkeypatch, json.dumps({
        "severity": "feasible", "rationale": "ok",
    }))

    result = dispatch_c3(
        bot_id="bot-x", app_id="j", trigger="charter_change",
        shared_dir=workspace,
        before_manifest={"description": "BEFORE — old design"},
    )
    assert result.ok is True
    assert result.check.triggered_by == "charter_change"
