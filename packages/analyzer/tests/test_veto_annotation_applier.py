"""tests/test_veto_annotation_applier.py — the VetoAnnotation applier.

`VetoAnnotation` was the last action kind in the registry with no applier, and
the only one of the four ``apply.INFORMATIONAL_KINDS`` without one. That tag
is a *display* predicate — it routes proposals to the Observations stream and
blocks nothing — so acting on a VetoAnnotation reached ``get_applier`` and
raised, while its three siblings no-opped cleanly.

The applier mutates nothing, so what is worth asserting is not "it wrote the
right thing" but: it resolves, it stays a no-op, and the proposal lands where
an operator has to close it out rather than closing itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import apply as arbiter_apply  # noqa: E402
from arbiter.appliers import get_applier, known_action_kinds  # noqa: E402
from arbiter.snapshot import capture  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from schema.proposal import (  # noqa: E402
    Proposal,
    RiskTag,
    VetoAnnotation,
    new_proposal_id,
)
from schema.provenance import Provenance  # noqa: E402

BOT = "team_bot_a"


def _action(severity: str = "high") -> VetoAnnotation:
    return VetoAnnotation(reason="widens exec allowlist", severity=severity)


def _proposal() -> Proposal:
    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id="scope_evaluator",
        dimension="safety",
        trigger_observations=["guardian:veto:1"],
        provenance=Provenance(technique="guardian.veto", signals={}, confidence=1.0),
        problem="proposal widens the exec allowlist",
        action=_action(),
        risk_tag=RiskTag(blast_radius="bot", reversibility="auto", touches=[]),
        claim=None,
    )
    for step in ("pending", "approved_human"):
        transition(proposal, step, actor="test", reason="test")
    return proposal


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────


def test_applier_is_registered():
    assert get_applier("VetoAnnotation") is not None
    assert "VetoAnnotation" in known_action_kinds()


def test_registered_by_the_package_import_alone():
    """A fresh interpreter that imports only the package — so a deleted
    ``__init__`` line cannot hide behind an import in this file."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, %r);\n"
            "from arbiter.appliers import known_action_kinds;\n"
            "print('VetoAnnotation' in known_action_kinds())" % str(_ANALYZER_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True", proc.stdout + proc.stderr


def test_every_registry_kind_now_has_an_applier():
    """The registry-vs-appliers gap this series closed, asserted as a set.

    AgentsAppend, InstallApp and VetoAnnotation each shipped because a
    declared kind had no applier. MemoryCurate is the deliberate exception —
    nothing emits it and nothing defines what ``target_selector`` selects, so
    it is human-only and unbuilt (see internal/pending-ideas.md §2c).
    """
    from schema.proposal import _ACTION_KIND_REGISTRY

    missing = set(_ACTION_KIND_REGISTRY) - set(known_action_kinds())
    assert missing == {"MemoryCurate"}, (
        "a declared action kind with no applier fails at approval time; add "
        f"the applier or record the exception: {sorted(missing)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# No-op semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_is_a_no_op_that_carries_the_risk_info():
    result = get_applier("VetoAnnotation").apply(_action(severity="critical"), BOT)
    assert result.ok
    assert result.details["reason"] == "widens exec allowlist"
    assert result.details["severity"] == "critical"
    assert result.details["bot_id"] == BOT


def test_snapshot_through_the_arbiter_entry_point():
    """``arbiter.snapshot.capture`` is the call that raised before this
    applier existed."""
    plan = capture(_action(), BOT)
    assert plan.before_snapshot["action_kind"] == "VetoAnnotation"
    assert plan.before_snapshot["severity"] == "high"
    assert plan.revert_action.kind == "VetoAnnotation"


def test_revert_is_a_no_op():
    applier = get_applier("VetoAnnotation")
    snap = applier.capture_snapshot(_action(), BOT)
    assert applier.revert(snap, BOT).ok


# ─────────────────────────────────────────────────────────────────────────────
# Completion: the operator closes it out, it does not close itself
# ─────────────────────────────────────────────────────────────────────────────


def test_applied_veto_waits_for_the_operator(tmp_path):
    """Claim-less proposals auto-succeed unless their kind defers completion.

    The other three INFORMATIONAL_KINDS are manual-completion; this one was
    not, so applying it would have stamped "succeeded" — acknowledged with
    nobody having acknowledged it.
    """
    proposal = _proposal()
    outcome = arbiter_apply.apply(proposal, shared_dir=tmp_path)
    assert outcome.ok, outcome.message
    assert proposal.status == "applied"
    assert arbiter_apply.is_manual_completion_kind("VetoAnnotation")


def test_every_informational_kind_defers_completion():
    """The four are one class; none of them may close itself out."""
    for kind in sorted(arbiter_apply.INFORMATIONAL_KINDS):
        assert arbiter_apply.is_deferred_completion_kind(kind), kind


def test_veto_is_breaker_exempt_because_it_writes_nothing(tmp_path):
    """A tripped breaker must not defer an FYI that mutates no bot state."""
    from breakers import store as _bstore

    _bstore.trip(
        shared_dir=tmp_path, scope=BOT, breaker_type="full",
        duration=None, initiated_by="test", reason="suppression test",
    )
    proposal = _proposal()
    outcome = arbiter_apply.apply(proposal, shared_dir=tmp_path)
    assert outcome.ok
    assert outcome.deferred is False
