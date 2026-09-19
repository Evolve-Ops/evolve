"""tests/test_exec_outcome_investigator.py — Phase 2 generator tests.

Two layers:
  1. Pure-Python attribution rules.
  2. observe() end-to-end with mocked correlated_signals.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from generators.exec_outcome_investigator.attribution import (  # noqa: E402
    ExecOutcomeEvidence,
    attribute,
    rule_approval_timeout_operator_missed,
    rule_exec_denied_allowlist_gap,
    rule_preflight_block_known,
    rule_tool_error_burst_unclassified,
)
from generators.exec_outcome_investigator.observe import (  # noqa: E402
    ExecOutcomeInvestigatorContext,
    observe,
)
from investigation.toolkit import CorrelatedSignal  # noqa: E402


# ── attribution rules ───────────────────────────────────────────────────────


def _evidence(**overrides):
    base = dict(
        bot_id="team_bot_a",
        primary_signal_type="exec_denied",
        primary_signal_signature="team_bot_a/Bash",
        tool_error_burst=None,
        exec_denied=[],
        approval_timeout=None,
        preflight_block=None,
    )
    base.update(overrides)
    return ExecOutcomeEvidence(**base)


def test_suggested_pattern_for_tool_uses_usr_bin_prefix():
    from generators.exec_outcome_investigator.attribution import (
        _suggested_pattern_for_tool,
    )
    assert _suggested_pattern_for_tool("python3") == "/usr/bin/python3*"
    assert _suggested_pattern_for_tool("ps") == "/usr/bin/ps*"


def test_suggested_pattern_strips_path_to_leaf():
    """If the tool name accidentally carries a path, only the leaf
    name is used so the bin_prefix is unambiguous."""
    from generators.exec_outcome_investigator.attribution import (
        _suggested_pattern_for_tool,
    )
    assert _suggested_pattern_for_tool("/some/path/curl") == "/usr/bin/curl*"


def test_suggested_pattern_empty_for_generic_tool():
    """Generic 'exec' fallback means we couldn't derive a tool from the
    failure → no pattern → no UpdateExecApproval; observe falls back to
    Investigation."""
    from generators.exec_outcome_investigator.attribution import (
        _suggested_pattern_for_tool,
    )
    assert _suggested_pattern_for_tool("exec") == ""
    assert _suggested_pattern_for_tool("") == ""


def test_rule_exec_denied_populates_suggested_pattern():
    """Phase 3: the rule attaches a suggested_pattern so observe.py can
    emit an UpdateExecApproval action."""
    ev = _evidence(
        exec_denied=[
            {"tool_name": "curl", "denial_count": 5,
             "sample_result_preview": "denied", "sample_input_preview": "x"},
        ],
    )
    result = rule_exec_denied_allowlist_gap(ev)
    assert result is not None
    assert result.evidence["suggested_pattern"] == "/usr/bin/curl*"


def test_rule_exec_denied_fires_picks_highest_count():
    ev = _evidence(
        exec_denied=[
            {"tool_name": "Bash", "denial_count": 3,
             "sample_result_preview": "denied", "sample_input_preview": "x"},
            {"tool_name": "WebFetch", "denial_count": 8,
             "sample_result_preview": "denied", "sample_input_preview": "y"},
        ],
    )
    result = rule_exec_denied_allowlist_gap(ev)
    assert result is not None
    assert result.cause_key == "exec_denied_allowlist_gap"
    assert result.primary_target == "WebFetch"
    assert result.confidence == 0.9


def test_rule_exec_denied_silent_when_empty():
    assert rule_exec_denied_allowlist_gap(_evidence()) is None


def test_rule_approval_timeout_fires():
    ev = _evidence(
        approval_timeout={
            "timeout_count": 2,
            "distinct_tools": ["Bash"],
            "per_tool_counts": {"Bash": 2},
            "max_single_tool_count": 2,
            "top_tool": "Bash",
            "sample_session_id": "s-1",
            "sample_result_preview": "approval timed out",
        },
    )
    result = rule_approval_timeout_operator_missed(ev)
    assert result is not None
    assert result.cause_key == "approval_timeout_operator_missed"
    assert result.evidence["timeout_count"] == 2


# ── Phase 4: recurrence-aware approval timeout ──────────────────────────────


def test_rule_approval_timeout_recurring_tool_fires_at_threshold():
    """When a single tool dominates the timeouts (3+ on same tool), the
    recurring-tool rule fires with an UpdateExecApproval suggestion.
    """
    from generators.exec_outcome_investigator.attribution import (
        rule_approval_timeout_recurring_tool,
    )
    ev = _evidence(
        approval_timeout={
            "timeout_count": 4,
            "distinct_tools": ["python3", "ps"],
            "per_tool_counts": {"python3": 3, "ps": 1},
            "max_single_tool_count": 3,
            "top_tool": "python3",
        },
    )
    result = rule_approval_timeout_recurring_tool(ev)
    assert result is not None
    assert result.cause_key == "approval_timeout_recurring_tool"
    assert result.primary_target == "python3"
    assert result.evidence["suggested_pattern"] == "/usr/bin/python3*"
    assert result.evidence["primary_tool_timeout_count"] == 3


def test_rule_approval_timeout_recurring_tool_quiet_below_threshold():
    """Two timeouts on the same tool is below the 3× threshold —
    the recurring rule defers, operator_missed rule handles it."""
    from generators.exec_outcome_investigator.attribution import (
        rule_approval_timeout_recurring_tool,
    )
    ev = _evidence(
        approval_timeout={
            "max_single_tool_count": 2,
            "top_tool": "python3",
            "timeout_count": 2,
            "distinct_tools": ["python3"],
            "per_tool_counts": {"python3": 2},
        },
    )
    assert rule_approval_timeout_recurring_tool(ev) is None


def test_rule_approval_timeout_recurring_tool_quiet_for_generic_tool():
    """If top_tool is 'exec' (generic), no pattern can be derived;
    rule defers."""
    from generators.exec_outcome_investigator.attribution import (
        rule_approval_timeout_recurring_tool,
    )
    ev = _evidence(
        approval_timeout={
            "max_single_tool_count": 5,
            "top_tool": "exec",
            "timeout_count": 5,
            "per_tool_counts": {"exec": 5},
        },
    )
    assert rule_approval_timeout_recurring_tool(ev) is None


def test_attribute_recurring_wins_over_operator_missed():
    """Both rules could match the data — recurring fires first because
    it ships a more-actionable fix."""
    ev = _evidence(
        approval_timeout={
            "timeout_count": 4,
            "distinct_tools": ["python3", "ps"],
            "per_tool_counts": {"python3": 3, "ps": 1},
            "max_single_tool_count": 3,
            "top_tool": "python3",
        },
    )
    result = attribute(ev)
    assert result.cause_key == "approval_timeout_recurring_tool"


def test_attribute_falls_through_to_operator_missed_when_scattered():
    """When timeouts are spread across many tools with none above threshold,
    the recurring rule defers and operator_missed catches it."""
    ev = _evidence(
        approval_timeout={
            "timeout_count": 6,
            "distinct_tools": ["a", "b", "c", "d", "e", "f"],
            "per_tool_counts": {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1, "f": 1},
            "max_single_tool_count": 1,
            "top_tool": "a",
        },
    )
    result = attribute(ev)
    assert result.cause_key == "approval_timeout_operator_missed"


def test_rule_preflight_fires():
    ev = _evidence(
        preflight_block={
            "block_count": 1,
            "distinct_tools": ["Bash"],
            "sample_input_preview": "python -c x",
            "sample_result_preview": "preflight blocked",
        },
    )
    result = rule_preflight_block_known(ev)
    assert result is not None
    assert result.cause_key == "preflight_block_known"
    assert result.evidence["upstream_issue"] == "openclaw/openclaw#87371"


def test_rule_burst_unclassified_fires_alone():
    ev = _evidence(
        tool_error_burst={
            "tool_error_total": 10, "sessions_with_errors": 3,
            "worst_session_id": "s-worst", "worst_session_errors": 5,
        },
    )
    result = rule_tool_error_burst_unclassified(ev)
    assert result is not None
    assert result.cause_key == "tool_error_burst_unclassified"
    assert result.confidence == 0.4
    assert result.primary_target == "s-worst"


def test_rule_burst_defers_when_specific_rule_present():
    """When exec_denied is also firing, burst-unclassified defers."""
    ev = _evidence(
        tool_error_burst={
            "tool_error_total": 5, "sessions_with_errors": 2,
            "worst_session_id": "s", "worst_session_errors": 3,
        },
        exec_denied=[
            {"tool_name": "Bash", "denial_count": 2,
             "sample_result_preview": "x", "sample_input_preview": "y"},
        ],
    )
    assert rule_tool_error_burst_unclassified(ev) is None


def test_attribute_rule_order_denied_wins():
    """All four signals firing → denied wins (highest specificity)."""
    ev = _evidence(
        tool_error_burst={
            "tool_error_total": 10, "sessions_with_errors": 3,
            "worst_session_id": "s", "worst_session_errors": 5,
        },
        exec_denied=[
            {"tool_name": "Bash", "denial_count": 4,
             "sample_result_preview": "x", "sample_input_preview": "y"},
        ],
        approval_timeout={"timeout_count": 1, "distinct_tools": ["x"]},
        preflight_block={"block_count": 1, "distinct_tools": ["y"]},
    )
    result = attribute(ev)
    assert result.cause_key == "exec_denied_allowlist_gap"


def test_attribute_falls_through_to_ambiguous():
    """No signal types present → ambiguous (safety net)."""
    ev = _evidence()
    result = attribute(ev)
    assert result.cause_key == "ambiguous"


# ── observe() end-to-end ────────────────────────────────────────────────────


def _csig(*, sig_id: str, sig_type: str, signature: str = "",
          details: dict | None = None) -> CorrelatedSignal:
    return CorrelatedSignal(
        signal_id=sig_id, type=sig_type, producer="exec_outcome_watchdog",
        severity="warn", title=f"{sig_type} fixture",
        signature=signature or f"security_bot/{sig_type}",
        details=details or {},
    )


def test_observe_emits_exec_denied_proposal(monkeypatch, tmp_path):
    """Phase 3: exec_denied + a derivable tool pattern → UpdateExecApproval
    action (one-click fix), not Investigation."""
    fake = [
        _csig(
            sig_id="g1", sig_type="exec_denied",
            signature="team_bot_a/python3",
            details={
                "tool_name": "python3",
                "denial_count": 4,
                "sample_result_preview": "exec-policy denied",
                "sample_input_preview": "python3 ops/...",
            },
        ),
        _csig(
            sig_id="g2", sig_type="tool_error_burst",
            details={
                "tool_error_total": 6, "sessions_with_errors": 3,
                "worst_session_id": "s-bad", "worst_session_errors": 4,
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )

    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    p = out[0]
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "exec_denied_allowlist_gap"
    assert rca["primary_target"] == "python3"
    # Phase 3: action is the L2 applier shape
    assert p.action.kind == "UpdateExecApproval"
    assert p.action.bot_id == "team_bot_a"
    assert p.action.operation == "add"
    assert p.action.pattern == "/usr/bin/python3*"
    assert p.action.agent_id == "main"
    assert p.action.scope == "agent"
    assert p.risk_tag.touches == ["auth_config"]
    assert p.risk_tag.reversibility == "auto"
    assert sorted(p.motivating_signals) == ["g1", "g2"]


def test_observe_bumps_urgency_when_manifest_declares_tool(monkeypatch, tmp_path):
    """Manifest cross-ref: when the bot's installed-app manifests mention
    the denied tool, urgency goes critical (mechanically-wrong state) and
    the body leads with the manifest evidence."""
    from investigation.toolkit import ManifestMention
    fake = [
        _csig(
            sig_id="g1", sig_type="exec_denied",
            signature="team_bot_a/python3",
            details={
                "tool_name": "python3",
                "denial_count": 4,
                "sample_result_preview": "denied",
                "sample_input_preview": "python3 ops/...",
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )
    # Inject a manifest match — the bot's "Protein Tracker" app
    # references python3 in its purpose.
    monkeypatch.setattr(
        obs_mod, "manifest_mentions",
        lambda bot_id, query, **kw: [ManifestMention(
            app_id="i-15709081",
            app_name="Protein Tracker",
            manifest_path="i-15709081.json",
            snippet="...purpose: Run python3 scripts to ingest macro data...",
        )],
    )

    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    p = out[0]
    # Urgency bumped from improvement → critical
    assert p.urgency == "critical"
    # Body leads with the manifest evidence
    assert "Manifest declares this capability" in p.problem
    assert "Protein Tracker" in p.problem
    # Provenance carries the declaring app data so the operator can audit
    sigs_block = p.provenance.signals
    assert sigs_block.get("manifest_declares_tool") is True
    declaring = sigs_block.get("declaring_apps") or []
    assert len(declaring) == 1
    assert declaring[0]["app_id"] == "i-15709081"
    assert declaring[0]["app_name"] == "Protein Tracker"
    # Still produces the UpdateExecApproval action — same one-click fix
    assert p.action.kind == "UpdateExecApproval"
    assert p.action.pattern == "/usr/bin/python3*"


def test_observe_keeps_improvement_urgency_when_no_manifest_match(
    monkeypatch, tmp_path,
):
    """No manifest mention → urgency stays improvement, no manifest block
    in body or provenance."""
    fake = [
        _csig(
            sig_id="g1", sig_type="exec_denied",
            signature="team_bot_a/python3",
            details={
                "tool_name": "python3",
                "denial_count": 4,
                "sample_result_preview": "denied",
                "sample_input_preview": "x",
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )
    monkeypatch.setattr(
        obs_mod, "manifest_mentions", lambda *a, **kw: [],
    )
    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    out = observe(ctx)
    p = out[0]
    assert p.urgency == "improvement"
    assert "Manifest declares" not in p.problem
    assert "manifest_declares_tool" not in p.provenance.signals


def test_observe_falls_back_to_investigation_when_pattern_generic(
    monkeypatch, tmp_path,
):
    """When the denied tool name is generic ('exec'), suggested_pattern
    is empty and we fall back to Investigation rather than emitting a
    broken UpdateExecApproval(pattern=\"\")."""
    fake = [
        _csig(
            sig_id="g1", sig_type="exec_denied",
            signature="team_bot_a/exec",
            details={
                "tool_name": "exec",  # generic fallback name
                "denial_count": 3,
                "sample_result_preview": "denied",
                "sample_input_preview": "x",
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )

    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    p = out[0]
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "exec_denied_allowlist_gap"
    # Generic tool → no suggested_pattern → falls back to Investigation
    assert rca["evidence"]["suggested_pattern"] == ""
    assert p.action.kind == "Investigation"
    # Body explains the operator-only path
    assert "operator-only" in p.action.context.lower()


def test_observe_emits_recurring_tool_update_exec_approval(monkeypatch, tmp_path):
    """Phase 4: recurring approval timeout on one tool → UpdateExecApproval
    action, mirroring the Phase 3 exec_denied path."""
    fake = [
        _csig(
            sig_id="g1", sig_type="approval_timeout",
            details={
                "timeout_count": 4,
                "distinct_tools": ["python3", "ps"],
                "per_tool_counts": {"python3": 3, "ps": 1},
                "max_single_tool_count": 3,
                "top_tool": "python3",
                "sample_session_id": "s-1",
                "sample_result_preview": "approval timed out",
            },
        ),
        _csig(
            sig_id="g2", sig_type="tool_error_burst",
            details={
                "tool_error_total": 4, "sessions_with_errors": 2,
                "worst_session_id": "s-1", "worst_session_errors": 3,
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )

    ctx = ExecOutcomeInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    p = out[0]
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "approval_timeout_recurring_tool"
    assert rca["primary_target"] == "python3"
    # Same applier shape as Phase 3's exec_denied path
    assert p.action.kind == "UpdateExecApproval"
    assert p.action.pattern == "/usr/bin/python3*"
    assert p.action.bot_id == "security_bot"
    assert p.risk_tag.touches == ["auth_config"]
    assert p.risk_tag.reversibility == "auto"
    # Body lives in `problem` for UpdateExecApproval (the Action has no
    # context field; the admin UI's kv-card renderer would drop body
    # text otherwise). Verify the recurrence rationale lands there.
    assert "recurrence threshold" in p.problem.lower() or \
           "stall time" in p.problem.lower()


def test_observe_emits_scattered_timeout_proposal(monkeypatch, tmp_path):
    """Phase 4: scattered timeouts across tools → Investigation
    (operator-routing fix), not UpdateExecApproval."""
    fake = [
        _csig(
            sig_id="g1", sig_type="approval_timeout",
            details={
                "timeout_count": 6,
                "distinct_tools": ["a", "b", "c", "d", "e", "f"],
                "per_tool_counts": {
                    "a": 1, "b": 1, "c": 1, "d": 1, "e": 1, "f": 1,
                },
                "max_single_tool_count": 1,
                "top_tool": "a",
                "sample_session_id": "s-1",
                "sample_result_preview": "approval timed out",
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )

    ctx = ExecOutcomeInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    p = out[0]
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "approval_timeout_operator_missed"
    assert p.action.kind == "Investigation"
    # Body should mention the routing-vs-allowlist distinction
    assert "scattered" in p.action.context.lower() or \
           "visibility" in p.action.context.lower()


def test_observe_emits_approval_timeout_proposal(monkeypatch, tmp_path):
    fake = [
        _csig(
            sig_id="g1", sig_type="approval_timeout",
            details={
                "timeout_count": 2,
                "distinct_tools": ["Bash", "WebFetch"],
                "sample_session_id": "s-1",
                "sample_result_preview": "approval timed out",
            },
        ),
        _csig(
            sig_id="g2", sig_type="tool_error_burst",
            details={
                "tool_error_total": 2, "sessions_with_errors": 2,
                "worst_session_id": "s-1", "worst_session_errors": 1,
            },
        ),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )
    ctx = ExecOutcomeInvestigatorContext(bot_id="security_bot", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    rca = out[0].provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == "approval_timeout_operator_missed"
    # Body suggests faster channel
    assert "channel" in out[0].action.context.lower()


def test_observe_silent_when_no_signals(monkeypatch, tmp_path):
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(obs_mod, "correlated_signals", lambda *a, **kw: [])
    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    assert observe(ctx) == []


def test_observe_suppresses_on_repeat_decline(monkeypatch, tmp_path):
    fake = [_csig(
        sig_id="g1", sig_type="exec_denied",
        details={
            "tool_name": "Bash", "denial_count": 3,
            "sample_result_preview": "denied", "sample_input_preview": "x",
        },
    )]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )
    monkeypatch.setattr(
        obs_mod, "operator_already_declined", lambda *a, **kw: True,
    )
    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    assert observe(ctx) == []


def test_observe_one_proposal_per_bot(monkeypatch, tmp_path):
    """Many cooperating signals → one Investigation, not N."""
    fake = [
        _csig(sig_id="g1", sig_type="tool_error_burst",
              details={"tool_error_total": 10, "sessions_with_errors": 4,
                       "worst_session_id": "s-w", "worst_session_errors": 6}),
        _csig(sig_id="g2", sig_type="exec_denied",
              signature="team_bot_a/Bash",
              details={"tool_name": "Bash", "denial_count": 3,
                       "sample_result_preview": "x",
                       "sample_input_preview": "y"}),
        _csig(sig_id="g3", sig_type="exec_denied",
              signature="team_bot_a/WebFetch",
              details={"tool_name": "WebFetch", "denial_count": 1,
                       "sample_result_preview": "x",
                       "sample_input_preview": "y"}),
    ]
    obs_mod = sys.modules["generators.exec_outcome_investigator.observe"]
    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(fake),
    )
    monkeypatch.setattr(
        obs_mod, "operator_already_declined", lambda *a, **kw: False,
    )
    ctx = ExecOutcomeInvestigatorContext(bot_id="team_bot_a", shared_dir=tmp_path)
    out = observe(ctx)
    assert len(out) == 1
    rca = out[0].provenance.signals["root_cause_attribution"]
    # Picks the highest-count denial as target
    assert rca["primary_target"] == "Bash"
