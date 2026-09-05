"""Unit tests for provider_audit (Workstream B-skills)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from provider_audit import (  # noqa: E402
    BotProviderState,
    OUTCOME_PROPOSE,
    ProviderAuditOutput,
    ProviderObservation,
    ProviderTriageDecision,
    VALID_PROVIDER_CATEGORIES,
    _coerce_provider_observation,
    extract_provider_contract,
    find_provider_module,
    run_provider_audit,
)


def test_provider_observation_signature_stable() -> None:
    obs = ProviderObservation(
        obs_id="obs-1", category="token_state", severity="critical",
        description="Token expires in 4 days.",
    )
    s1 = obs.signature("team_bot_a", "google_workspace")
    s2 = obs.signature("team_bot_a", "google_workspace")
    assert s1 == s2
    assert s1.startswith("provider_audit:token_state:team_bot_a:google_workspace:")


def test_provider_observation_signature_differs_per_bot() -> None:
    obs = ProviderObservation(
        obs_id="x", category="token_state", severity="major",
        description="Token state issue.",
    )
    assert obs.signature("team_bot_a", "google_workspace") != obs.signature("admin_bot", "google_workspace")


def test_coerce_provider_observation_valid() -> None:
    raw = {
        "obs_id": "obs-1", "category": "scope_coverage", "severity": "major",
        "description": "Scopes don't cover skill needs.",
    }
    obs = _coerce_provider_observation(raw, 0)
    assert obs is not None
    assert obs.category == "scope_coverage"


def test_coerce_provider_observation_drops_invalid_category() -> None:
    raw = {"obs_id": "x", "category": "nonsense", "severity": "major", "description": "x"}
    assert _coerce_provider_observation(raw, 0) is None


def test_bot_provider_state_prompt_dict_scope_drift_per_skill() -> None:
    state = BotProviderState(
        bot_id="team_bot_a", provider_id="google_workspace",
        scopes_present=["gmail.readonly"],
        scopes_needed_by_skills={
            "gmail":    ["gmail.readonly"],
            "calendar": ["calendar.readonly", "calendar.events"],
        },
    )
    d = state.to_prompt_dict()
    assert d["scope_drift_per_skill"]["gmail"] == []
    assert "calendar.readonly" in d["scope_drift_per_skill"]["calendar"]
    assert "calendar.events" in d["scope_drift_per_skill"]["calendar"]


def test_extract_provider_contract_reads_docstring(tmp_path: Path) -> None:
    p = tmp_path / "gmail_provider.py"
    p.write_text(
        '"""Gmail OAuth provider — handles token refresh, scope checking, etc."""\n'
        '\n'
        'def get_token(): pass\n'
    )
    contract = extract_provider_contract(p)
    assert "Gmail OAuth" in contract["module_docstring"]
    assert "get_token" in contract["public_symbols"]


def test_extract_provider_contract_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    p.write_text("def foo(:\n")
    contract = extract_provider_contract(p)
    assert "SyntaxError" in contract["parse_error"]


def test_find_provider_module_returns_none_for_unknown(tmp_path: Path) -> None:
    assert find_provider_module("nonexistent", cwd=str(tmp_path)) is None


def test_run_provider_audit_no_observations_ok() -> None:
    state = BotProviderState(bot_id="team_bot_a", provider_id="google_workspace")
    contract = {"module_docstring": "x", "public_symbols": []}
    with patch("provider_audit._dispatch_via_oc") as m:
        m.return_value = ("[]", 50, "")
        out = run_provider_audit(
            provider_id="google_workspace", bot_id="team_bot_a", state=state,
            contract=contract, audit_run_id="run-1",
        )
    assert out.status == "ok"
    assert out.observations == []


def test_run_provider_audit_with_findings_triages() -> None:
    state = BotProviderState(
        bot_id="team_bot_a", provider_id="google_workspace",
        token_expire_days=4,
        scopes_present=["gmail.readonly"],
    )
    contract = {"module_docstring": "x", "public_symbols": []}
    stage3a = json.dumps([{
        "obs_id": "obs-1", "category": "token_state", "severity": "critical",
        "description": "Token expires in 4 days.",
    }])
    stage3b = json.dumps([{
        "obs_id": "obs-1", "outcome": "propose", "rationale": "operator must rotate",
    }])
    with patch("provider_audit._dispatch_via_oc") as m:
        m.side_effect = [(stage3a, 1000, ""), (stage3b, 200, "")]
        out = run_provider_audit(
            provider_id="google_workspace", bot_id="team_bot_a", state=state,
            contract=contract, audit_run_id="run-1",
        )
    assert out.status == "with_findings"
    assert out.decisions[0].outcome == "propose"


def test_run_provider_audit_missing_provider_module_fails() -> None:
    state = BotProviderState(bot_id="team_bot_a", provider_id="fake_provider")
    with patch("provider_audit.find_provider_module") as m:
        m.return_value = None
        out = run_provider_audit(
            provider_id="fake_provider", bot_id="team_bot_a", state=state,
            audit_run_id="run-1",
        )
    assert out.status == "failed"
    assert "no provider module" in out.error


def test_run_provider_audit_full_audit_overrides_accepted() -> None:
    state = BotProviderState(bot_id="team_bot_a", provider_id="google_workspace")
    contract = {"module_docstring": "x", "public_symbols": []}
    obs = ProviderObservation(
        obs_id="obs-1", category="token_state", severity="major",
        description="Token expires soon.",
    )
    sig = obs.signature("team_bot_a", "google_workspace")
    stage3a = json.dumps([obs.to_dict()])
    stage3b = json.dumps([{"obs_id": "obs-1", "outcome": "propose", "rationale": "x"}])
    with patch("provider_audit._dispatch_via_oc") as m:
        m.side_effect = [(stage3a, 100, ""), (stage3b, 50, "")]
        out = run_provider_audit(
            provider_id="google_workspace", bot_id="team_bot_a", state=state,
            contract=contract, audit_run_id="run-1",
            accepted_signatures={sig}, full_audit=True,
        )
    assert out.status == "with_findings"
    assert len(out.observations) == 1
