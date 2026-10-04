"""tests/test_anti_domain_integration.py — pin the contradicted-path
behavior across the two monitors + the amplifier generator.

The anti-domain parser introduced a third state (alongside
confirmed / neutral|emergent / skip):

  - ``contradicted`` — the bot's AGENTS.md has an explicit
    ``## Out of scope`` (or inline ``out of scope:``) marker covering
    the candidate domain.

Different consumers handle ``contradicted`` differently:

  - **capability_gap_monitor** drops the candidate. Operator already
    said "don't suggest this"; emitting a Recommendations card the
    operator dismisses every cycle is operator-hostile.
  - **engagement_amplifier_monitor** still emits the Signal but with
    ``objective_alignment="contradicted"``. The fact that users keep
    engaging on something the operator excluded is itself an
    RSI-worthy finding — operator needs to make a decision.
  - **engagement_amplifier generator** reframes the entire pitch:
    "make a decision" (widen scope / redirect / tolerate) instead of
    "pick a way to deepen."
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import capability_gap_monitor as cap_mod  # noqa: E402
import engagement_amplifier_monitor as amp_mod  # noqa: E402
from generators.engagement_amplifier import (  # noqa: E402
    EngagementAmplifierContext,
    observe as amp_generator_observe,
)
from generators.engagement_amplifier.observe import (  # noqa: E402
    _build_proposal as amp_build_proposal,
)
from observations.tuples import write_tuples  # noqa: E402
from schema.observation import ObservationTuple  # noqa: E402
from signals import store as signals_store  # noqa: E402


BOT_ID = "team-bot-a"
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


SAILING_BOT_PURPOSE_WITH_EXCLUSION = """
# Purpose
A sailing assistant — weather, routing, regatta planning.

## Out of scope
- fitness
- finance
"""


def _write_tuples(
    shared_dir, bot_id, *, noun, verb="tracking", n=8, days_span=8,
    engagement_each=4, mood="enthusiastic",
):
    for i in range(n):
        day = NOW - timedelta(days=(i % days_span))
        t = ObservationTuple(
            id=f"obs-{noun}-{verb}-{i}",
            bot_id=bot_id,
            session_id=f"sess-{noun}-{verb}-{i}",
            segment_id=f"seg-{i}",
            noun=noun, verb=verb, mood=mood,
            engagement=engagement_each,
            timestamp_start=day.isoformat(),
            timestamp_end=(day + timedelta(minutes=5)).isoformat(),
            source_hash=f"hash-{noun}-{verb}-{i}",
        )
        write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)


def _stub_agents_md_on_module(module, tmp_path, content, monkeypatch):
    """Drop a fake AGENTS.md and patch the module's reader to use it."""
    p = tmp_path / f"agents-{module.PRODUCER}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        module, "_bot_workspace_agents_md", lambda bid: p
    )


def _stub_no_manifests(tmp_path, bot_id):
    (tmp_path / "applications" / bot_id).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# capability_gap_monitor: contradicted → drop
# ─────────────────────────────────────────────────────────────────────────────


def test_cap_gap_drops_contradicted_candidate(tmp_path, monkeypatch):
    """A workout pattern on a sailing bot whose AGENTS.md explicitly
    excludes fitness must NOT produce a capability_gap_monitor
    detection. Pre-anti-domain, this fired with neutral fit;
    post-anti-domain, the operator's explicit "no fitness" wins."""
    _stub_agents_md_on_module(
        cap_mod, tmp_path,
        SAILING_BOT_PURPOSE_WITH_EXCLUSION, monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    detections = cap_mod.detect_capability_gaps(BOT_ID, tmp_path, now=NOW)
    fit_cats = [d["details"]["category"] for d in detections]
    assert "fitness_tracking" not in fit_cats, (
        f"fitness_tracking emitted despite explicit exclusion; "
        f"got {fit_cats}"
    )


def test_cap_gap_still_emits_non_contradicted_candidates(
    tmp_path, monkeypatch
):
    """The anti-domain marker on fitness must NOT suppress unrelated
    gaps. A workout pattern is dropped (excluded), but a learning
    pattern still produces a detection (no exclusion for that
    domain)."""
    _stub_agents_md_on_module(
        cap_mod, tmp_path,
        SAILING_BOT_PURPOSE_WITH_EXCLUSION
        + "\nThis bot helps with learning logs too.\n",
        monkeypatch,
    )
    _stub_no_manifests(tmp_path, BOT_ID)
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    _write_tuples(tmp_path, BOT_ID, noun="learning", verb="recording")
    detections = cap_mod.detect_capability_gaps(BOT_ID, tmp_path, now=NOW)
    cats = [d["details"]["category"] for d in detections]
    # fitness contradicted → dropped; learning confirmed → emitted.
    assert "fitness_tracking" not in cats
    assert "learning_log" in cats


def test_cap_gap_objective_fit_returns_contradicted_value():
    """Unit-level: the helper directly returns 'contradicted' when
    the candidate domain is in the anti-domain set, before the
    confirmed/neutral fallback."""
    kw = cap_mod._domain_keywords()
    assert cap_mod._objective_fit(
        "A sailing bot.", "domain:fitness", kw,
        anti_domains={"domain:fitness"},
    ) == "contradicted"
    # Without anti_domains arg, default is empty set → falls back.
    assert cap_mod._objective_fit(
        "A sailing bot.", "domain:fitness", kw,
    ) == "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# engagement_amplifier_monitor: contradicted → emit with new alignment
# ─────────────────────────────────────────────────────────────────────────────


def test_amp_emits_contradicted_signal(tmp_path, monkeypatch):
    """A sustained workout pattern on a sailing bot whose AGENTS.md
    excludes fitness must produce a Signal with
    ``objective_alignment="contradicted"``. The pattern is still RSI-
    worthy — operator needs to know users are doing something
    out-of-scope."""
    _stub_agents_md_on_module(
        amp_mod, tmp_path,
        SAILING_BOT_PURPOSE_WITH_EXCLUSION, monkeypatch,
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")
    detections = amp_mod.detect_amplification_opportunities(
        BOT_ID, tmp_path, now=NOW
    )
    assert len(detections) == 1
    assert detections[0]["details"]["objective_alignment"] == "contradicted"


def test_amp_objective_alignment_helper_returns_contradicted():
    """Unit-level: the alignment helper distinguishes contradicted
    from confirmed/emergent. The anti-domain check fires before the
    keyword-match fallback so a domain that's BOTH in purpose AND in
    out-of-scope (an inconsistent AGENTS.md) is still treated as
    contradicted — operator's explicit exclusion wins over inferred
    in-scope."""
    kw = amp_mod._domain_keywords()
    # Without anti-domains.
    assert amp_mod._objective_alignment(
        "sailing bot", "workout", kw,
    ) == "emergent"
    # With anti-domains.
    assert amp_mod._objective_alignment(
        "sailing bot", "workout", kw,
        anti_domains={"domain:fitness"},
    ) == "contradicted"


# ─────────────────────────────────────────────────────────────────────────────
# engagement_amplifier generator: contradicted reframes pitch
# ─────────────────────────────────────────────────────────────────────────────


class _StubSignal:
    def __init__(self, sig_id, bot_id, **details):
        self.id = sig_id
        self.bot_id = bot_id
        self.type = amp_mod.SIGNAL_TYPE
        self.details = {"bot_id": bot_id, **details}


def _contradicted_signal():
    return _StubSignal(
        "sig-1", BOT_ID,
        noun="workout",
        verb="tracking",
        objective_alignment="contradicted",
        engagement_total=42,
        distinct_sessions=8,
        distinct_days=12,
        frustrated_share=0.05,
        positive_mood_share=0.6,
        domain_tags=["domain:fitness"],
        window_days=30,
    )


def test_contradicted_summary_reframes_as_decision():
    """The summary text must change from 'consider deepening' to
    'make a decision'. The operator-facing call to action is
    fundamentally different — they're not deepening, they're
    choosing between widening scope and tolerating."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(
            bot_ids=[BOT_ID], shared_dir=Path("/tmp")
        ),
    )
    assert p is not None
    assert "out of scope" in p.summary
    assert "make a decision" in p.summary.lower()


def test_contradicted_headline_says_pattern_contradicts_scope():
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(
            bot_ids=[BOT_ID], shared_dir=Path("/tmp")
        ),
    )
    assert p is not None
    assert "contradicts stated scope" in p.admin_surface_summary.lower()


def test_contradicted_action_label_is_decision_oriented():
    """The action_label drives the button text. 'Pick a way to
    deepen' is wrong for a contradicted pattern — must read 'Make
    a scope decision' or equivalent."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(
            bot_ids=[BOT_ID], shared_dir=Path("/tmp")
        ),
    )
    assert p is not None
    assert "decision" in (p.action_label or "").lower()
    assert "deepen" not in (p.action_label or "").lower()


def test_contradicted_explanation_lists_decision_options():
    """The 'Ways to ...' section must list scope-decision options
    (widen / redirect / tolerate), not deepen options (cron / app /
    AGENTS note)."""
    p = amp_build_proposal(
        _contradicted_signal(),
        EngagementAmplifierContext(
            bot_ids=[BOT_ID], shared_dir=Path("/tmp")
        ),
    )
    assert p is not None
    assert "Widen the scope" in p.explanation
    assert "Redirect users" in p.explanation
    # Tolerate-as-is + Dismiss were merged into one item (PR following
    # the 2026-06-05 audit — the two had identical effects but separate
    # framing, confusing the operator). Check for the merged item.
    assert "Dismiss / tolerate as-is" in p.explanation
    # And the deepen-shaped options MUST NOT appear in this branch.
    assert "Schedule a proactive surface" not in p.explanation
    assert "Formalize as an app" not in p.explanation


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: contradicted Signal flows through to a decision Proposal
# ─────────────────────────────────────────────────────────────────────────────


def test_end_to_end_contradicted_signal_to_decision_proposal(
    tmp_path, monkeypatch
):
    """Sailing bot AGENTS.md excludes fitness; workout pattern fires;
    monitor emits a Signal with alignment=contradicted; generator
    consumes it and produces a 'make a decision' Proposal."""
    _stub_agents_md_on_module(
        amp_mod, tmp_path,
        SAILING_BOT_PURPOSE_WITH_EXCLUSION, monkeypatch,
    )
    _write_tuples(tmp_path, BOT_ID, noun="workout", verb="tracking")

    # Producer run.
    summary = amp_mod.run_for_pod([BOT_ID], tmp_path, now=NOW)
    assert summary["signals_emitted"] == 1

    # Consumer run.
    ctx = EngagementAmplifierContext(
        bot_ids=[BOT_ID], shared_dir=tmp_path, max_per_run=10
    )
    proposals = amp_generator_observe(ctx)
    assert len(proposals) == 1
    p = proposals[0]
    # The proposal must use the contradicted framing.
    assert "out of scope" in p.summary
    assert "Widen the scope" in p.explanation
    assert p.action_label == "Make a scope decision"
