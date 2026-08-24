"""tests/test_rsi_l2_integration.py — L2 end-to-end flow.

The big-picture test: Sysadmin Watchdog emits a proposal → it lands in
pending/ → human approves → arbiter.apply runs → proposal enters applied/
→ verify daemon runs → outcome dispatched → file moves to the right dir.

This is the L2 §8.5 walkthrough: "seed a synthetic gateway-down incident
via the test harness; invoke Sysadmin Watchdog's observe() manually;
confirm proposal appears with expected shape; route and approve; apply;
advance clock past claim window; mock resolver returns 1.0; confirm
succeeded."
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import apply as arbiter_apply  # noqa: E402
from arbiter import ingest  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from generators.sysadmin_watchdog import observe  # noqa: E402
from generators.sysadmin_watchdog.observe import DetectorContext  # noqa: E402
from proposal_synthesizer.promote import _build_proposal_from_candidate  # noqa: E402
from proposal_synthesizer.store import iter_candidates as _iter_candidates  # noqa: E402


def _drive_observe_to_proposal(ctx, shared_dir: Path):
    """Phase 6c helper: observe() returns []; convert the candidate
    it emitted into a Proposal so integration tests can drive it
    through ingest/apply/verify."""
    ctx.shared_dir = shared_dir
    # The ACL-restore Proposal now fires in lock-step with its Signal (flap
    # hysteresis, internal/spec-transient-signal-suppression-2026-06-23.md). The
    # generator runner writes observe_signals() output before running
    # observe(); mirror that by promoting the acl_drift Signal so the proposal
    # pass sees it active. (A no-op for non-ACL contexts.)
    from generators.sysadmin_watchdog.signals import acl_drift_signal_kwargs
    from signals import store as _signals_store

    _signals_store.observe(shared_dir, **acl_drift_signal_kwargs(ctx.bot_id))
    assert observe(ctx) == []
    cands = list(_iter_candidates(shared_dir, subdirs=("pending",)))
    assert len(cands) == 1, f"expected 1 candidate, got {len(cands)}"
    return _build_proposal_from_candidate(cands[0])
from metrics.registry import MetricValue  # noqa: E402
from registry.charter_loader import load_charter_from_yaml  # noqa: E402
from schema import Proposal  # noqa: E402
from verify.daemon import ResolveResult, run_once  # noqa: E402


_WATCHDOG_CHARTER = Path(__file__).parent.parent / "generators/sysadmin_watchdog/charter.yaml"


def _resolver_factory(values: dict):
    def r(name, bot_id, t):
        return values.get(name, MetricValue(value=1.0, confidence=1.0))
    return r


def test_charter_loads_and_matches_code():
    """The shipped charter YAML parses cleanly and agrees with the registered invariants."""
    charter, fingerprint = load_charter_from_yaml(_WATCHDOG_CHARTER)
    assert charter.id == "sysadmin_watchdog"
    assert charter.type == "guardian"
    assert charter.cadence == "hourly"
    # Ensure the specified invariants are present
    inv_ids = {inv.id for inv in charter.invariants}
    assert "action_kind_allowed" in inv_ids
    assert "touches_forbidden_surfaces" in inv_ids


def test_watchdog_acl_proposal_passes_ingest_through_charter(tmp_path):
    """ACL drift produces a candidate that, when promoted to a Proposal,
    passes the charter invariants. Phase 6c: observe() returns [];
    proposal is derived from candidate via the gate's promoter."""
    charter, _ = load_charter_from_yaml(_WATCHDOG_CHARTER)
    ctx = DetectorContext(
        bot_id="team_bot_a",
        resolve=_resolver_factory({"acl.evolve_read": MetricValue(0.0)}),
    )
    proposal = _drive_observe_to_proposal(ctx, tmp_path)
    # Run through ingest to prove charter invariants don't reject it
    result = ingest(
        proposal,
        charter=charter,
        known_metrics=["acl.evolve_read"],
    )
    assert result.accepted


def test_full_cycle_acl_drift_detected_through_succeed(tmp_path):
    """Emit → ingest → route → auto-approve → apply → verify → succeeded.

    ACL drift is the one Proposal-emitting Sysadmin Watchdog detector after
    Phase 1b (other platform-failure detectors route to the Signal store
    via ``observe_signals``). This walks the full RSI lifecycle on it.
    """
    shared = tmp_path / "shared"
    cand_dir = tmp_path / "cand"
    cand_dir.mkdir()

    # Step 1: Sysadmin Watchdog detects ACL drift. Phase 6c:
    # observe() returns []; we lift the resulting candidate into a
    # Proposal via the same path the gate's mechanical promoter uses.
    ctx = DetectorContext(
        bot_id="team_bot_a",
        resolve=_resolver_factory({"acl.evolve_read": MetricValue(0.0)}),
    )
    proposal = _drive_observe_to_proposal(ctx, cand_dir)
    assert proposal.generator_id == "sysadmin_watchdog"
    assert proposal.urgency == "substrate_warn"

    # Make the ConfigPatch operate on a real file so apply+revert succeed.
    target_path = tmp_path / "acl-state.json"
    target_path.write_text(json.dumps({"acl_restored": False}))
    from schema.proposal import ConfigPatch

    proposal.action = ConfigPatch(
        target_path=f"{target_path}::acl_restored",
        operation="set",
        value=True,
    )

    # Step 2: Ingest (uses the watchdog's real charter)
    charter, _ = load_charter_from_yaml(_WATCHDOG_CHARTER)
    ingest(proposal, charter=charter, known_metrics=["acl.evolve_read"])
    assert proposal.status == "pending"

    # Step 3: Auto-approve. ACL drift is autonomous-eligible
    # (reversibility="auto", touches=["acl"]); router gates on a revert_plan
    # which the applier captures at apply time, so we drive the transition
    # directly here just as the failure-path test below does.
    transition(proposal, "approved_auto", actor="arbiter")

    # Step 5: Apply
    outcome = arbiter_apply.apply(proposal)
    assert outcome.ok
    assert proposal.status == "applied"

    # Step 6: Back-date the applied transition so verify sees it as expired,
    # then drop the proposal in applied/ for the daemon to pick up.
    applied_at = datetime.now(timezone.utc) - timedelta(days=2)
    for h in reversed(proposal.history):
        if h.to_status == "applied":
            h.at = applied_at.isoformat(timespec="seconds")
            break
    applied_dir = shared / "proposals" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / f"{proposal.id}.json").write_text(
        json.dumps(proposal.to_dict())
    )

    # Step 7: Verify daemon — mocked resolver returns acl.evolve_read=1.0
    def healthy_resolver(m, b, t):
        return ResolveResult(value=1.0, confidence=1.0)

    now = datetime.now(timezone.utc)
    report = run_once(shared, healthy_resolver, now=now)

    # Step 8: Confirm outcome
    assert report.succeeded == 1
    succeeded_path = shared / "proposals" / "succeeded" / f"{proposal.id}.json"
    assert succeeded_path.exists()

    vr_path = shared / "proposals" / "verification-results" / f"{proposal.id}.json"
    assert vr_path.exists()
    with vr_path.open() as fh:
        vr = json.load(fh)
    assert vr["outcome"] == "succeeded"
    assert vr["metric"] == "acl.evolve_read"


def test_full_cycle_failure_triggers_escalation(tmp_path):
    """The same flow, but the claim fails and fallback=escalate fires."""
    shared = tmp_path / "shared"
    cand_dir = tmp_path / "cand"
    cand_dir.mkdir()

    # Phase 6c: observe() returns []; lift candidate to Proposal.
    ctx = DetectorContext(
        bot_id="team_bot_a",
        resolve=_resolver_factory({"acl.evolve_read": MetricValue(0.0)}),
    )
    proposal = _drive_observe_to_proposal(ctx, cand_dir)
    # Force fallback=escalate to exercise escalation path
    proposal.claim.fallback = "escalate"

    # Pre-populate revert plan so schema validation + apply can proceed.
    # (In real flow the applier captures this at apply time; here we just
    # need something that serializes and can be "reverted" by the applier
    # even if the target path is nominal.)
    target_path = tmp_path / "cfg.json"
    target_path.write_text(json.dumps({"acl_restored": False}))
    from schema.proposal import ConfigPatch, RevertPlan

    # Point the proposal's action at a real file so apply + revert work
    proposal.action = ConfigPatch(
        target_path=f"{target_path}::acl_restored",
        operation="set",
        value=True,
    )

    # Ingest with a stub charter that allows ConfigPatch
    from schema import Charter, Invariant

    stub_charter = Charter(
        id="sysadmin_watchdog",
        type="guardian",
        dimension="substrate_health",
        purpose="stub",
        cadence="hourly",
        invariants=[
            Invariant(
                id="allowed",
                description="allow ConfigPatch",
                check_kind="action_kind_allowed",
                params={"allowlist": ["ConfigPatch"]},
            ),
        ],
    )
    ingest(proposal, charter=stub_charter, known_metrics=["acl.evolve_read"])
    transition(proposal, "approved_auto", actor="arbiter")
    applied = arbiter_apply.apply(proposal)
    assert applied.ok

    # Back-date the applied transition so verify sees it as expired
    applied_at = datetime.now(timezone.utc) - timedelta(days=2)
    for h in reversed(proposal.history):
        if h.to_status == "applied":
            h.at = applied_at.isoformat(timespec="seconds")
            break
    applied_dir = shared / "proposals" / "applied"
    applied_dir.mkdir(parents=True)
    (applied_dir / f"{proposal.id}.json").write_text(
        json.dumps(proposal.to_dict())
    )

    # Resolver returns 0 (claim fails: direction=up, magnitude=1, baseline=0,
    # value=0 → delta=0 < 1 → failed)
    def failing_resolver(m, b, t):
        return ResolveResult(value=0.0, confidence=1.0)

    report = run_once(shared, failing_resolver, now=datetime.now(timezone.utc))
    assert report.failed_reverted == 1
    # fallback=escalate → escalation emitted too
    assert report.escalations_emitted == 1

    # Escalation proposal should be in pending/
    pending_files = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(pending_files) == 1
    with pending_files[0].open() as fh:
        esc = json.load(fh)
    assert esc["generator_id"] == "verify_daemon"
    assert esc["approval_audience"] == "pod_operator"
    assert esc["dimension"] == "meta_health"
