"""Unit tests for skill_audit (Workstream B-skills).

Mirrors :mod:`test_app_audit_tier3` shape — we don't hit a real LLM;
instead we exercise the coercion, signature, contract extraction, and
end-to-end orchestration via mocked dispatch. The runner integration
is tested separately in test_audit_poller_substrate and the admin
substrate tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from skill_audit import (  # noqa: E402
    BotSkillState,
    OUTCOME_PROPOSE,
    SkillAuditOutput,
    SkillObservation,
    SkillTriageDecision,
    VALID_SKILL_CATEGORIES,
    _coerce_skill_observation,
    extract_synthetic_contract,
    find_install_module,
    run_skill_audit,
)


# ── Observation signature ────────────────────────────────────────────────────


def test_skill_observation_signature_stable() -> None:
    obs = SkillObservation(
        obs_id="obs-1", category="credential_state", severity="major",
        description="Token expired 3 days ago.",
        evidence=["auth-profiles.json:google_workspace:team_bot_a"],
    )
    s1 = obs.signature("team_bot_a", "gmail")
    s2 = obs.signature("team_bot_a", "gmail")
    assert s1 == s2
    # Producer-prefixed so it can't collide with app-audit signatures.
    assert s1.startswith("skill_audit:credential_state:team_bot_a:gmail:")


def test_skill_observation_signature_canonicalizes_whitespace_case() -> None:
    a = SkillObservation(
        obs_id="x", category="config_drift", severity="minor",
        description="Plugin entry missing.",
    )
    b = SkillObservation(
        obs_id="y", category="config_drift", severity="minor",
        description="Plugin   entry  missing.\n",
    )
    assert a.signature("team_bot_a", "gmail") == b.signature("team_bot_a", "gmail")


def test_skill_observation_signature_strips_line_numbers() -> None:
    a = SkillObservation(
        obs_id="x", category="code_vs_docstring", severity="minor",
        description="Docstring mentions foo() but only bar() is exported.",
        evidence=["gmail_install.py:42"],
    )
    b = SkillObservation(
        obs_id="y", category="code_vs_docstring", severity="minor",
        description="Docstring mentions foo() but only bar() is exported.",
        evidence=["gmail_install.py:99"],
    )
    assert a.signature("team_bot_a", "gmail") == b.signature("team_bot_a", "gmail")


def test_different_skill_id_yields_different_signature() -> None:
    obs = SkillObservation(
        obs_id="x", category="credential_state", severity="major",
        description="Token expired.",
    )
    assert obs.signature("team_bot_a", "gmail") != obs.signature("team_bot_a", "calendar")


# ── _coerce_skill_observation ────────────────────────────────────────────────


def test_coerce_skill_observation_valid() -> None:
    raw = {
        "obs_id": "obs-1",
        "category": "credential_state",
        "severity": "major",
        "description": "Token expired.",
        "evidence": ["x"],
    }
    obs = _coerce_skill_observation(raw, 0)
    assert obs is not None
    assert obs.category == "credential_state"
    assert obs.severity == "major"


def test_coerce_skill_observation_drops_invalid_category() -> None:
    raw = {
        "obs_id": "obs-1",
        "category": "made_up_category",
        "severity": "major",
        "description": "x",
    }
    assert _coerce_skill_observation(raw, 0) is None


def test_coerce_skill_observation_defaults_invalid_severity_to_info() -> None:
    raw = {
        "obs_id": "obs-1",
        "category": "credential_state",
        "severity": "epic",
        "description": "x",
    }
    obs = _coerce_skill_observation(raw, 0)
    assert obs is not None
    assert obs.severity == "info"


def test_coerce_skill_observation_requires_description() -> None:
    raw = {
        "obs_id": "obs-1",
        "category": "credential_state",
        "severity": "major",
        "description": "   ",
    }
    assert _coerce_skill_observation(raw, 0) is None


# ── BotSkillState prompt rendering ───────────────────────────────────────────


def test_bot_skill_state_prompt_dict_includes_scope_drift() -> None:
    state = BotSkillState(
        bot_id="team_bot_a", skill_id="gmail",
        scopes_present=["gmail.send"],
        scopes_expected=["gmail.readonly", "gmail.send"],
    )
    d = state.to_prompt_dict()
    assert d["scope_drift"] == ["gmail.readonly"]


def test_bot_skill_state_credentialless_marks_has_oauth_false() -> None:
    state = BotSkillState(
        bot_id="team_bot_a", skill_id="imessage", has_oauth=False,
    )
    d = state.to_prompt_dict()
    assert d["has_oauth"] is False


# ── extract_synthetic_contract ───────────────────────────────────────────────


def test_extract_synthetic_contract_reads_docstring(tmp_path: Path) -> None:
    p = tmp_path / "fake_install.py"
    p.write_text(
        '"""Fake skill — single-line docstring describing what this is."""\n'
        '\n'
        'GMAIL_SKILL_ID = "gmail"\n'
        '\n'
        'def install():\n'
        '    """Install the skill."""\n'
        '    pass\n'
    )
    contract = extract_synthetic_contract(p)
    assert "Fake skill" in contract["module_docstring"]
    assert contract["is_thin"] is True   # short docstring
    assert "install" in contract["public_symbols"]
    assert any(c["name"] == "GMAIL_SKILL_ID" for c in contract["constants"])


def test_extract_synthetic_contract_handles_syntax_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.py"
    p.write_text("def broken(:\n")   # syntax error
    contract = extract_synthetic_contract(p)
    assert "SyntaxError" in contract["parse_error"]
    assert contract["public_symbols"] == []


def test_extract_synthetic_contract_marks_thin_for_no_docstring(tmp_path: Path) -> None:
    p = tmp_path / "no_doc.py"
    p.write_text("def foo(): pass\n")
    contract = extract_synthetic_contract(p)
    assert contract["is_thin"] is True


def test_extract_synthetic_contract_long_docstring_not_thin(tmp_path: Path) -> None:
    long_doc = "x" * 300
    p = tmp_path / "fat.py"
    p.write_text(f'"""{long_doc}"""\n')
    contract = extract_synthetic_contract(p)
    assert contract["is_thin"] is False


# ── find_install_module ──────────────────────────────────────────────────────


def test_find_install_module_returns_none_for_unknown(tmp_path: Path) -> None:
    assert find_install_module("nonexistent_skill", cwd=str(tmp_path)) is None


def test_find_install_module_finds_dev_path(tmp_path: Path) -> None:
    skills_dir = tmp_path / "packages" / "admin" / "evolve_admin" / "skills"
    skills_dir.mkdir(parents=True)
    install_path = skills_dir / "gmail_install.py"
    install_path.write_text('"""Gmail install."""\n')
    assert find_install_module("gmail", cwd=str(tmp_path)) == install_path


# ── run_skill_audit end-to-end (mocked LLM) ──────────────────────────────────


def test_run_skill_audit_no_observations_returns_ok(tmp_path: Path) -> None:
    """When Stage 3a returns empty, we skip Stage 3b and report ok."""
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": [], "function_signatures": []}
    with patch("skill_audit._dispatch_via_oc") as m:
        m.return_value = ("[]", 100, "")
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
        )
    assert out.status == "ok"
    assert out.observations == []
    assert m.call_count == 1   # Only Stage 3a was dispatched.


def test_run_skill_audit_with_observation_runs_triage(tmp_path: Path) -> None:
    """A real observation triggers a Stage 3b dispatch."""
    state = BotSkillState(
        bot_id="team_bot_a", skill_id="gmail",
        scopes_present=["gmail.send"],
        scopes_expected=["gmail.readonly"],
    )
    contract = {"module_docstring": "x", "public_symbols": [], "function_signatures": []}
    stage3a_text = json.dumps([{
        "obs_id": "obs-1",
        "category": "scope_drift",
        "severity": "major",
        "description": "Profile has send scope but needs readonly.",
        "evidence": ["auth-profiles.json"],
    }])
    stage3b_text = json.dumps([{
        "obs_id": "obs-1",
        "outcome": "propose",
        "rationale": "operator needs to regenerate token",
    }])
    with patch("skill_audit._dispatch_via_oc") as m:
        m.side_effect = [
            (stage3a_text, 1000, ""),   # Stage 3a
            (stage3b_text, 200, ""),    # Stage 3b
        ]
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
        )
    assert out.status == "with_findings"
    assert len(out.observations) == 1
    assert out.observations[0].category == "scope_drift"
    assert len(out.decisions) == 1
    assert out.decisions[0].outcome == "propose"
    assert out.tokens_used == 1200


def test_run_skill_audit_stage_3a_failure_returns_failed(tmp_path: Path) -> None:
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": []}
    with patch("skill_audit._dispatch_via_oc") as m:
        m.return_value = ("", 0, "timeout")
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
        )
    assert out.status == "failed"
    assert "stage 3a" in out.error


def test_run_skill_audit_stage_3b_failure_defaults_to_propose(tmp_path: Path) -> None:
    """If Stage 3b dispatch fails, every observation defaults to propose
    so we don't silently lose findings."""
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": []}
    stage3a = json.dumps([{
        "obs_id": "obs-1", "category": "credential_state", "severity": "critical",
        "description": "Token expired.",
    }])
    with patch("skill_audit._dispatch_via_oc") as m:
        m.side_effect = [(stage3a, 100, ""), ("", 0, "boom")]
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
        )
    assert out.status == "failed"
    assert all(d.outcome == OUTCOME_PROPOSE for d in out.decisions)


def test_run_skill_audit_filters_accepted_signatures() -> None:
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": []}
    # Construct an observation locally so we can pre-compute its signature.
    obs = SkillObservation(
        obs_id="obs-1", category="credential_state", severity="major",
        description="Token expired.",
    )
    sig = obs.signature("team_bot_a", "gmail")
    stage3a = json.dumps([obs.to_dict()])
    with patch("skill_audit._dispatch_via_oc") as m:
        m.return_value = (stage3a, 100, "")
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
            accepted_signatures={sig},
        )
    # Accepted observations are filtered before triage; result is ok.
    assert out.status == "ok"
    assert out.observations == []


def test_run_skill_audit_full_audit_ignores_accepted_signatures() -> None:
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": []}
    obs = SkillObservation(
        obs_id="obs-1", category="credential_state", severity="major",
        description="Token expired.",
    )
    sig = obs.signature("team_bot_a", "gmail")
    stage3a = json.dumps([obs.to_dict()])
    stage3b = json.dumps([{"obs_id": "obs-1", "outcome": "propose", "rationale": "x"}])
    with patch("skill_audit._dispatch_via_oc") as m:
        m.side_effect = [(stage3a, 100, ""), (stage3b, 50, "")]
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
            accepted_signatures={sig},
            full_audit=True,
        )
    # full_audit=True bypasses the accepted filter.
    assert out.status == "with_findings"
    assert len(out.observations) == 1


def test_run_skill_audit_missing_install_module_fails() -> None:
    state = BotSkillState(bot_id="team_bot_a", skill_id="not_a_real_skill")
    with patch("skill_audit.find_install_module") as m:
        m.return_value = None
        out = run_skill_audit(
            skill_id="not_a_real_skill", bot_id="team_bot_a", state=state,
            audit_run_id="run-1",
        )
    assert out.status == "failed"
    assert "no install module" in out.error


def test_run_skill_audit_missing_triage_decisions_backfilled() -> None:
    """If Stage 3b doesn't return a decision for every observation, the
    missing ones default to propose."""
    state = BotSkillState(bot_id="team_bot_a", skill_id="gmail")
    contract = {"module_docstring": "x", "public_symbols": []}
    stage3a = json.dumps([
        {
            "obs_id": "obs-1", "category": "credential_state", "severity": "major",
            "description": "Token expired.",
        },
        {
            "obs_id": "obs-2", "category": "scope_drift", "severity": "minor",
            "description": "Scope drift.",
        },
    ])
    # Triage only returns one of the two decisions.
    stage3b = json.dumps([
        {"obs_id": "obs-1", "outcome": "dismiss", "rationale": "false positive"},
    ])
    with patch("skill_audit._dispatch_via_oc") as m:
        m.side_effect = [(stage3a, 100, ""), (stage3b, 50, "")]
        out = run_skill_audit(
            skill_id="gmail", bot_id="team_bot_a", state=state, contract=contract,
            audit_run_id="run-1",
        )
    assert len(out.decisions) == 2
    by_id = {d.obs_id: d for d in out.decisions}
    assert by_id["obs-1"].outcome == "dismiss"
    assert by_id["obs-2"].outcome == "propose"   # Backfilled.
