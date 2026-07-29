"""tests/test_rsi_verify.py — verify daemon + evaluate + dispatch."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.state_machine import transition  # noqa: E402
from schema.proposal import (  # noqa: E402
    Claim,
    Proposal,
    RevertPlan,
    ConfigPatch,
)
from testing.harness import make_config_patch_proposal  # noqa: E402
from verify.daemon import (  # noqa: E402
    MAX_RESOLUTION_RETRIES,
    ResolveResult,
    VerifyDaemon,
    run_once,
)
from verify.dispatch import dispatch_outcome  # noqa: E402
from verify.evaluate import (  # noqa: E402
    METRIC_CONFIDENCE_FLOOR,
    evaluate_claim,
)


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_claim
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_up_succeeded():
    c = Claim(metric="x", direction="up", magnitude=5.0, window_days=1, baseline=10.0)
    outcome = evaluate_claim(c, current_value=20.0)  # delta=10 >= 5
    assert outcome.result == "succeeded"
    assert outcome.delta == 10.0


def test_claim_up_failed():
    c = Claim(metric="x", direction="up", magnitude=5.0, window_days=1, baseline=10.0)
    outcome = evaluate_claim(c, current_value=12.0)  # delta=2, below threshold
    assert outcome.result == "failed"


def test_claim_down_succeeded():
    c = Claim(metric="x", direction="down", magnitude=5.0, window_days=1, baseline=10.0)
    outcome = evaluate_claim(c, current_value=3.0)  # delta=-7, -(-7)=7 >= 5
    assert outcome.result == "succeeded"


def test_claim_equal_within_tolerance():
    c = Claim(metric="x", direction="equal", magnitude=2.0, window_days=1, baseline=10.0)
    outcome = evaluate_claim(c, current_value=11.0)  # |delta|=1 <= 2
    assert outcome.result == "succeeded"


def test_claim_unresolved_on_low_confidence():
    c = Claim(metric="x", direction="up", magnitude=1.0, window_days=1, baseline=0.0)
    outcome = evaluate_claim(c, current_value=10.0, current_confidence=0.3)
    assert outcome.result == "unresolved"
    assert METRIC_CONFIDENCE_FLOOR > 0.3


# ─────────────────────────────────────────────────────────────────────────────
# dispatch_outcome
# ─────────────────────────────────────────────────────────────────────────────


def _apply_to_applied_state(tmp_path):
    """Return a proposal in ``applied`` state with a valid revert plan."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme", value="dark"
    )
    p.claim.fallback = "revert"
    p.revert_on_failure = RevertPlan(
        before_snapshot={
            "action_kind": "ConfigPatch",
            "bot_id": "team_bot_a",
            "file_path": str(target),
            "keys": ["ui", "theme"],
            "existed_before": True,
            "prior_value": "light",
            "file_existed_before": True,
        },
        revert_action=ConfigPatch(
            target_path=f"{target}::ui.theme", operation="set", value="light"
        ),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(
            timespec="seconds"
        ),
    )
    # Actually apply the change so the before-state on disk reflects post-apply
    target.write_text(json.dumps({"ui": {"theme": "dark"}}))

    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    transition(p, "applied", actor="arbiter")
    return p, target


def test_dispatch_succeeded(tmp_path):
    p, _ = _apply_to_applied_state(tmp_path)
    from verify.evaluate import ClaimOutcome

    outcome = ClaimOutcome(
        result="succeeded",
        delta=1.0,
        threshold_met=True,
        message="held",
    )
    result = dispatch_outcome(p, outcome)
    assert p.status == "succeeded"
    assert result.escalation is None


def test_dispatch_failed_with_revert(tmp_path):
    p, target = _apply_to_applied_state(tmp_path)
    from verify.evaluate import ClaimOutcome

    outcome = ClaimOutcome(
        result="failed",
        delta=0.0,
        threshold_met=False,
        message="did not hold",
    )
    result = dispatch_outcome(p, outcome)
    assert p.status == "failed_reverted"
    assert result.revert_ok is True
    # Verify revert actually restored the file
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "light"
    assert result.escalation is None


def test_dispatch_failed_with_flag_fallback(tmp_path):
    p, _ = _apply_to_applied_state(tmp_path)
    p.claim.fallback = "flag"
    from verify.evaluate import ClaimOutcome

    outcome = ClaimOutcome(
        result="failed",
        delta=0.0,
        threshold_met=False,
        message="did not hold",
    )
    result = dispatch_outcome(p, outcome)
    assert p.status == "failed_flagged"
    assert result.escalation is not None
    assert result.escalation.approval_audience == "pod_operator"


def test_dispatch_failed_with_escalate(tmp_path):
    p, _ = _apply_to_applied_state(tmp_path)
    p.claim.fallback = "escalate"
    from verify.evaluate import ClaimOutcome

    outcome = ClaimOutcome(
        result="failed",
        delta=0.0,
        threshold_met=False,
        message="did not hold",
    )
    result = dispatch_outcome(p, outcome)
    assert p.status == "failed_reverted"
    # Escalate means: revert AND emit escalation
    assert result.revert_ok is True
    assert result.escalation is not None


def test_dispatch_unresolved_leaves_state(tmp_path):
    p, _ = _apply_to_applied_state(tmp_path)
    from verify.evaluate import ClaimOutcome

    outcome = ClaimOutcome(
        result="unresolved",
        delta=0.0,
        threshold_met=False,
        message="low confidence",
    )
    result = dispatch_outcome(p, outcome)
    assert p.status == "applied"  # unchanged
    assert result.escalation is None


# ─────────────────────────────────────────────────────────────────────────────
# VerifyDaemon — full cycle
# ─────────────────────────────────────────────────────────────────────────────


def _write_proposal(path: Path, proposal: Proposal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.to_dict()))


def _proposal_with_apply_time(tmp_path, apply_time: datetime):
    """Build a proposal already in applied state with a given apply_time."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "dark"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme", value="dark"
    )
    p.claim.fallback = "revert"
    p.revert_on_failure = RevertPlan(
        before_snapshot={
            "action_kind": "ConfigPatch",
            "bot_id": "team_bot_a",
            "file_path": str(target),
            "keys": ["ui", "theme"],
            "existed_before": True,
            "prior_value": "light",
            "file_existed_before": True,
        },
        revert_action=ConfigPatch(
            target_path=f"{target}::ui.theme", operation="set", value="light"
        ),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(
            timespec="seconds"
        ),
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    transition(p, "applied", actor="arbiter")

    # Override the most recent history entry's timestamp to apply_time
    for h in reversed(p.history):
        if h.to_status == "applied":
            h.at = apply_time.isoformat(timespec="seconds")
            break

    return p, target


def test_daemon_skips_proposals_before_horizon(tmp_path):
    shared = tmp_path / "shared"
    apply_time = _now() - timedelta(hours=2)
    p, _ = _proposal_with_apply_time(tmp_path, apply_time)
    # claim window 1 day — horizon not reached at _now()
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    def resolver(m, b, t):
        return ResolveResult(value=5.0, confidence=1.0)

    report = run_once(shared, resolver, now=_now())
    assert report.scanned == 1
    assert report.not_expired == 1
    assert report.succeeded == 0


def test_daemon_succeeded_path(tmp_path):
    shared = tmp_path / "shared"
    apply_time = _now() - timedelta(days=2)
    p, _ = _proposal_with_apply_time(tmp_path, apply_time)
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    # Metric resolves favorably (claim: up by magnitude 1 from baseline 0)
    def resolver(m, b, t):
        return ResolveResult(value=5.0, confidence=1.0)

    report = run_once(shared, resolver, now=_now())
    assert report.succeeded == 1
    # File moved from applied/ to succeeded/
    assert not (shared / "proposals" / "applied" / f"{p.id}.json").exists()
    assert (shared / "proposals" / "succeeded" / f"{p.id}.json").exists()
    # Verification result written
    assert (
        shared / "proposals" / "verification-results" / f"{p.id}.json"
    ).exists()


def test_daemon_failed_reverted_path(tmp_path):
    shared = tmp_path / "shared"
    apply_time = _now() - timedelta(days=2)
    p, target = _proposal_with_apply_time(tmp_path, apply_time)
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    def resolver(m, b, t):
        return ResolveResult(value=0.0, confidence=1.0)  # claim fails

    report = run_once(shared, resolver, now=_now())
    assert report.failed_reverted == 1
    assert (shared / "proposals" / "failed_reverted" / f"{p.id}.json").exists()
    # Revert restored the file
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "light"


def test_daemon_resolution_retry_then_flag(tmp_path):
    shared = tmp_path / "shared"
    apply_time = _now() - timedelta(days=2)
    p, _ = _proposal_with_apply_time(tmp_path, apply_time)
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    # Resolver that always returns low confidence → unresolved
    def resolver(m, b, t):
        return ResolveResult(value=0.0, confidence=0.1)

    # Run MAX_RESOLUTION_RETRIES cycles
    for i in range(MAX_RESOLUTION_RETRIES):
        report = run_once(shared, resolver, now=_now())
        if i < MAX_RESOLUTION_RETRIES - 1:
            assert report.unresolved == 1
            # File stays in applied/ with bumped attempt counter
            assert (shared / "proposals" / "applied" / f"{p.id}.json").exists()

    # Final cycle should force-flag + escalation
    assert report.retry_exhausted == 1
    assert report.failed_flagged == 1
    assert report.escalations_emitted == 1
    # File moved to failed_flagged
    assert (shared / "proposals" / "failed_flagged" / f"{p.id}.json").exists()
    # Escalation proposal written to pending
    pending_files = list(
        (shared / "proposals" / "pending").glob("*.json")
    )
    assert len(pending_files) >= 1


def test_daemon_leaves_manual_completion_in_applied(tmp_path):
    """Investigation (manual-completion kind) belongs in applied/ awaiting
    operator Mark complete. The daemon should NOT rescue it — that would
    short-circuit the In Process queue."""
    from testing.harness import make_investigation_proposal

    shared = tmp_path / "shared"
    p = make_investigation_proposal()  # no claim, kind=Investigation
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")
    transition(p, "applied", actor="arbiter")
    assert p.claim is None
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    def resolver(m, b, t):
        raise AssertionError("resolver should not be called for claim-less proposal")

    report = run_once(shared, resolver, now=_now())
    assert report.scanned == 1
    assert report.succeeded == 0  # NOT rescued
    assert report.malformed == []
    # File stays in applied/ — proposal sits in the In Process queue.
    assert (shared / "proposals" / "applied" / f"{p.id}.json").exists()


def test_daemon_rescues_legacy_claimless_non_manual_kind(tmp_path):
    """Hypothetical legacy claim-less ConfigPatch stuck in applied/ from
    before apply.py learned to promote claim-less proposals. The daemon
    sweep should transition it to ``succeeded`` rather than parking it
    forever."""
    from testing.harness import make_config_patch_proposal

    shared = tmp_path / "shared"
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "dark"}}))
    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme", value="dark"
    )
    p.claim = None  # legacy: no claim; kind is ConfigPatch (not manual)
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    transition(p, "applied", actor="arbiter")
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    def resolver(m, b, t):
        raise AssertionError("resolver should not be called for claim-less proposal")

    report = run_once(shared, resolver, now=_now())
    assert report.scanned == 1
    assert report.succeeded == 1
    assert not (shared / "proposals" / "applied" / f"{p.id}.json").exists()
    assert (shared / "proposals" / "succeeded" / f"{p.id}.json").exists()


def test_daemon_handles_malformed_proposal(tmp_path):
    shared = tmp_path / "shared"
    (shared / "proposals" / "applied").mkdir(parents=True)
    (shared / "proposals" / "applied" / "broken.json").write_text("{not json")

    def resolver(m, b, t):
        raise AssertionError("should not be called")

    report = run_once(shared, resolver, now=_now())
    assert report.scanned == 0  # malformed files don't parse → skipped before count


def test_daemon_resolver_exception_counts_as_retry(tmp_path):
    shared = tmp_path / "shared"
    apply_time = _now() - timedelta(days=2)
    p, _ = _proposal_with_apply_time(tmp_path, apply_time)
    _write_proposal(shared / "proposals" / "applied" / f"{p.id}.json", p)

    def resolver(m, b, t):
        raise RuntimeError("metric source down")

    report = run_once(shared, resolver, now=_now())
    assert report.unresolved == 1  # first attempt, retry mechanism engaged
