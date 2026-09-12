"""tests/test_rsi_veto_and_merge.py — Guardian veto pass + merge judge."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.merge import (  # noqa: E402
    judge,
    judge_collisions,
    reset_merge_judge,
    set_merge_judge,
)
from arbiter.veto import Guardian, run_veto_pass  # noqa: E402
from schema import MergeJudgment, VetoResult  # noqa: E402
from schema.proposal import ConfigPatch  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
    make_workflow_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Veto pass
# ─────────────────────────────────────────────────────────────────────────────


def _pass_guardian(gid="g_pass"):
    return Guardian(
        id=gid,
        evaluate=lambda p: VetoResult(
            guardian_id=gid, verdict="pass", reason="ok"
        ),
    )


def _annotate_guardian(gid="g_annot", severity="medium"):
    return Guardian(
        id=gid,
        evaluate=lambda p: VetoResult(
            guardian_id=gid,
            verdict="annotate",
            severity=severity,
            reason="noted",
        ),
    )


def _veto_guardian(gid="g_veto", severity="high"):
    return Guardian(
        id=gid,
        evaluate=lambda p: VetoResult(
            guardian_id=gid,
            verdict="veto",
            severity=severity,
            reason="blocked",
        ),
    )


def _abstain_guardian(gid="g_abstain"):
    return Guardian(id=gid, evaluate=lambda p: None)


def test_veto_pass_all_pass_is_not_blocked():
    p = make_investigation_proposal()
    outcome = run_veto_pass(
        p, [_pass_guardian("a"), _pass_guardian("b")]
    )
    assert not outcome.blocked
    assert outcome.annotations == []
    assert len(outcome.pass_verdicts) == 2


def test_veto_pass_single_veto_blocks():
    p = make_investigation_proposal()
    outcome = run_veto_pass(
        p, [_pass_guardian(), _veto_guardian()]
    )
    assert outcome.blocked
    assert len(outcome.blocking_verdicts) == 1
    # Annotation also applied
    assert p.guardian_annotations[0].guardian_id == "g_veto"


def test_veto_pass_critical_severity_is_any_critical_true():
    p = make_investigation_proposal()
    outcome = run_veto_pass(
        p, [_veto_guardian(severity="critical")]
    )
    assert outcome.blocked
    assert outcome.any_critical


def test_veto_pass_annotate_does_not_block():
    p = make_investigation_proposal()
    outcome = run_veto_pass(p, [_annotate_guardian()])
    assert not outcome.blocked
    assert len(p.guardian_annotations) == 1
    assert p.guardian_annotations[0].severity == "medium"


def test_veto_pass_skips_self():
    # Guardian with id matching proposal.generator_id is skipped
    p = make_investigation_proposal(generator_id="self_guardian")
    outcome = run_veto_pass(
        p,
        [
            _veto_guardian(gid="self_guardian"),  # this would veto if it ran
            _pass_guardian(gid="other"),
        ],
    )
    assert not outcome.blocked
    assert "self_guardian" in outcome.skipped_self


def test_veto_pass_abstention_has_no_effect():
    p = make_investigation_proposal()
    outcome = run_veto_pass(p, [_abstain_guardian(), _pass_guardian()])
    assert not outcome.blocked
    assert outcome.annotations == []


def test_veto_pass_tolerates_guardian_exception():
    """A raising guardian becomes an annotation, not a crash."""

    def raising(p):
        raise ValueError("evaluator bug")

    p = make_investigation_proposal()
    outcome = run_veto_pass(
        p, [Guardian(id="bugsy", evaluate=raising), _pass_guardian()]
    )
    assert not outcome.blocked  # evaluator bugs don't silently veto
    # But the exception did surface as a low-severity annotation
    assert any(a.guardian_id == "bugsy" for a in p.guardian_annotations)


# ─────────────────────────────────────────────────────────────────────────────
# Merge judge
# ─────────────────────────────────────────────────────────────────────────────


def test_heuristic_judge_keeps_both_on_different_action_kinds():
    a = make_investigation_proposal()
    b = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    j = judge(a, b)
    assert j.decision == "keep_both"


def test_heuristic_judge_merges_same_kind_same_surface():
    a = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    b = make_config_patch_proposal(target_path="/tmp/x.json::k", value=2)
    # Same target_path → same surface → merge
    j = judge(a, b)
    assert j.decision == "merge"
    assert j.credit_split.get(a.generator_id, 0) + j.credit_split.get(
        b.generator_id, 0
    ) > 0


def test_heuristic_judge_keeps_both_same_kind_different_surface():
    a = make_config_patch_proposal(target_path="/tmp/x.json::k", value=1)
    b = make_config_patch_proposal(target_path="/tmp/y.json::k", value=1)
    j = judge(a, b)
    assert j.decision == "keep_both"


def test_installable_custom_judge_called():
    calls = []

    def my_judge(a, b):
        calls.append((a.id, b.id))
        return MergeJudgment(
            decision="prefer_a",
            credit_split={a.generator_id: 1.0, b.generator_id: 0.0},
            reason="test override",
            confidence=0.99,
        )

    set_merge_judge(my_judge)
    try:
        a = make_investigation_proposal()
        b = make_investigation_proposal()
        j = judge(a, b)
        assert j.decision == "prefer_a"
        assert calls == [(a.id, b.id)]
    finally:
        reset_merge_judge()


def test_judge_collisions_returns_one_judgment_per_pair():
    a = make_investigation_proposal()
    b = make_investigation_proposal()
    c = make_investigation_proposal()
    judgments = judge_collisions(a, [b, c])
    assert len(judgments) == 2
