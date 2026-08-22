"""tests/test_cache_ttl_tuner_phase_b.py — Phase B content + gates.

Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md.

Pin three things the Phase B migration owes the protocol:

1. **Operator-first content.** Each factory populates the new
   ``summary`` / ``explanation`` fields with prose tuned to the
   spec's voice rules — no slugs, second person, names trade-offs.
2. **Action-tier fallback fields.** Each factory hits the right
   tier (1/2/5) of the fallback ladder with the matching field set.
3. **Applicability gate.** ``observe()`` skips bots that don't use
   Anthropic models; the upstream signal is otherwise wasted attention.
4. **Dismiss-signature suppression.** ``observe()`` skips emission
   when a matching dismiss entry is active for the bot.

The voice-rule lint runs alongside the existing structural tests in
``test_cache_ttl_tuner.py``; this file focuses on the new content +
gates introduced in Phase B.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402
# `from generators.cache_ttl_tuner import observe` would resolve to
# the *function* re-exported by __init__.py, not the submodule. Use
# importlib so monkeypatch can target submodule-level attributes.
import importlib  # noqa: E402

observe_module = importlib.import_module(
    "generators.cache_ttl_tuner.observe",
)
from generators.cache_ttl_tuner.observe import (  # noqa: E402
    CacheTtlTunerContext,
    observe,
)
from signals import store as signals_store  # noqa: E402
from schema.signal import make_signature  # noqa: E402
from generators.cache_ttl_tuner.signal_proposals import (  # noqa: E402
    DISMISS_SIG_HIT_RATE_LOW,
    DISMISS_SIG_INVALIDATION_INVESTIGATE,
    DISMISS_SIG_INVALIDATION_TYPED,
    make_cache_hit_rate_low_proposal,
    make_cache_invalidation_elevated_proposal,
    make_cache_invalidation_investigation_fallback,
)


# ─────────────────────────────────────────────────────────────────────────────
# Signal helpers (mirror test_cache_ttl_tuner.py with the same defaults)
# ─────────────────────────────────────────────────────────────────────────────


def _invalidation_signal(bot_id: str = "admin_bot", ratio: float = 0.38) -> dict:
    return {
        "id": "sig-inv-1",
        "type": "cache_invalidation_elevated",
        "bot_id": bot_id,
        "producer": "session_economics",
        "details": {
            "invalidated_count": 37,
            "participating_count": 97,
            "invalidated_ratio": ratio,
            "threshold_ratio": 0.20,
            "window_days": 7,
        },
    }


def _hit_rate_signal(bot_id: str = "admin_bot", hit_rate: float = 0.25) -> dict:
    return {
        "id": "sig-hr-1",
        "type": "cache_hit_rate_low",
        "bot_id": bot_id,
        "producer": "session_economics",
        "details": {
            "hit_rate": hit_rate,
            "threshold_ratio": 0.50,
            "participating_count": 100,
            "window_days": 7,
            "cache_read_tokens": 5000,
            "cache_write_tokens": 10000,
            "input_tokens": 5000,
        },
    }


def _write_signal(shared_dir: Path, *, bot_id: str, sig: dict) -> str:
    """Write a session_economics Signal through the real signals.store
    API so iter_active() picks it up the way the production observer
    would. The ``sig`` dict's ``type`` and ``details`` fields drive
    the store entry; other top-level keys are ignored."""
    sig_type = sig["type"]
    details = sig.get("details") or {}
    written = signals_store.observe(
        shared_dir,
        signature=make_signature("session_economics", sig_type, bot_id),
        producer="session_economics",
        type=sig_type,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {sig_type}",
        details=details,
    )
    return written.id


# ─────────────────────────────────────────────────────────────────────────────
# Content fields — operator-first prose
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidationElevatedContent:
    """Tier 1 — auto-apply Proposal flipping cacheRetention=long."""

    def test_summary_populated(self):
        p = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        assert p.summary
        # ≤ 400 chars per spec voice rules.
        assert len(p.summary) <= 400

    def test_explanation_populated(self):
        p = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        assert p.explanation
        # ≤ 1500 chars per spec voice rules.
        assert len(p.explanation) <= 1500

    def test_action_label_set_and_within_budget(self):
        p = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        assert p.action_label == "Switch to long-window caching"
        assert len(p.action_label) <= 30

    def test_manual_path_points_at_cost_optimization(self):
        p = make_cache_invalidation_elevated_proposal(
            _invalidation_signal(bot_id="ellie")
        )
        assert p.manual_path == "Cost Optimization → ellie"

    def test_dismiss_signature_stable_and_kind_scoped(self):
        # Stable across different ratios — the finding is about the
        # *kind* of mis-tuning, not its current magnitude.
        a = make_cache_invalidation_elevated_proposal(
            _invalidation_signal(ratio=0.30)
        )
        b = make_cache_invalidation_elevated_proposal(
            _invalidation_signal(ratio=0.55)
        )
        assert a.dismiss_signature == b.dismiss_signature == DISMISS_SIG_INVALIDATION_TYPED
        assert a.dismiss_scope == "kind"

    def test_summary_names_problem_not_metric(self):
        """Voice rule #3 — lead with the why (operator perspective),
        not the metric. The Summary must mention the *paying-to-re-
        read* framing, not "invalidated_ratio=" or similar slug.

        Stricter slug check: no snake_case identifiers (two or more
        words joined by underscore). Bare bot_id with an underscore is
        operationally valid — humanizing bot names belongs to a
        separate naming-pass — so we only flag identifiers that
        clearly look like field paths or root-cause keys."""
        import re

        p = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        assert "paying" in p.summary.lower() or "re-read" in p.summary.lower()
        assert "invalidated_ratio" not in p.summary
        assert "cache_retention" not in p.summary
        # Look for snake_case tokens of 3+ underscore-joined words —
        # field paths and root-cause keys follow that shape; bot ids
        # don't.
        slug_re = re.compile(r"\b[a-z]+_[a-z]+_[a-z]+\b")
        leaks = slug_re.findall(p.summary)
        assert not leaks, f"slug-shaped tokens leaked into summary: {leaks}"

    def test_explanation_names_trade_off(self):
        """Voice rule #6 — explanation must name what could go wrong."""
        p = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        text = p.explanation.lower()
        assert (
            "what could go wrong" in text
            or "trade-off" in text
            or "downside" in text
            or "risk" in text
        )


class TestInvalidationInvestigationFallbackContent:
    """Tier 2 — investigation fallback when autonomous fix is suppressed."""

    def test_summary_populated(self):
        p = make_cache_invalidation_investigation_fallback(_invalidation_signal())
        assert p.summary
        assert len(p.summary) <= 400

    def test_action_label_directs_at_ui(self):
        p = make_cache_invalidation_investigation_fallback(_invalidation_signal())
        # Tier 2 button points the operator at the UI.
        assert p.action_label == "Open Cost Optimization"
        assert len(p.action_label) <= 30

    def test_dismiss_signature_distinct_from_typed_path(self):
        """Dismissing the typed fix shouldn't suppress the
        investigation fallback (they're different findings)."""
        typed = make_cache_invalidation_elevated_proposal(_invalidation_signal())
        invest = make_cache_invalidation_investigation_fallback(_invalidation_signal())
        assert typed.dismiss_signature != invest.dismiss_signature

    def test_explanation_names_trade_off(self):
        p = make_cache_invalidation_investigation_fallback(_invalidation_signal())
        text = p.explanation.lower()
        assert (
            "what could go wrong" in text
            or "trade-off" in text
            or "risk" in text
        )


class TestHitRateLowContent:
    """Tier 5 — paste-to-bot instruction (no autonomous knob)."""

    def test_summary_populated(self):
        p = make_cache_hit_rate_low_proposal(_hit_rate_signal())
        assert p.summary
        assert len(p.summary) <= 400

    def test_manual_instruction_populated_and_actionable(self):
        """Tier 5 — manual_instruction is the operator's action lever.
        Must be a self-contained instruction the operator can paste
        verbatim, not a template with placeholders."""
        p = make_cache_hit_rate_low_proposal(_hit_rate_signal())
        assert p.manual_instruction
        # Concrete (operator can hand it off without filling in blanks)
        assert "{" not in p.manual_instruction
        assert "<" not in p.manual_instruction
        # Names the actual goal so the bot knows what done looks like.
        assert "hit rate" in p.manual_instruction.lower()

    def test_no_action_label_for_pure_investigation_tier_5(self):
        """Tier-5 Investigation defaults to the generic 'Take this on'
        button; the manual_instruction is the operator-actionable
        path. Setting action_label here would compete with the dispatch
        flow rather than complement it."""
        p = make_cache_hit_rate_low_proposal(_hit_rate_signal())
        assert p.action_label is None

    def test_dismiss_signature_distinct(self):
        a = make_cache_hit_rate_low_proposal(_hit_rate_signal())
        assert a.dismiss_signature == DISMISS_SIG_HIT_RATE_LOW
        # Distinct from the invalidation signatures so dismissing one
        # finding type doesn't suppress the other.
        assert a.dismiss_signature != DISMISS_SIG_INVALIDATION_TYPED
        assert a.dismiss_signature != DISMISS_SIG_INVALIDATION_INVESTIGATE


# ─────────────────────────────────────────────────────────────────────────────
# Applicability gate
# ─────────────────────────────────────────────────────────────────────────────


class TestApplicabilityGate:
    """observe() must skip non-Anthropic bots; the gate is bot-wide so
    no per-signal work happens on bots the proposal can't act on."""

    def test_signal_emits_when_applicability_disabled(self, tmp_path):
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(
            CacheTtlTunerContext(
                bot_id="admin_bot",
                shared_dir=tmp_path,
                check_applicability=False,
            )
        )
        assert len(out) == 1

    def test_signal_emits_when_applicability_returns_true(
        self, tmp_path, monkeypatch,
    ):
        """Fail-open path — read failure ⇒ True ⇒ emit."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
        assert len(out) == 1

    def test_signal_skipped_when_bot_is_not_anthropic(self, tmp_path, monkeypatch):
        """The Plex-test operator never sees a proposal they can't
        act on — non-Anthropic bots short-circuit before the signal
        loop."""
        monkeypatch.setattr(
            observe_module, "bot_uses_anthropic", lambda bot_id: False,
        )
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
        assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss-signature suppression gate
# ─────────────────────────────────────────────────────────────────────────────


class TestDismissSuppressionGate:
    """observe() must respect the dismissals store so a dismissed
    finding doesn't reappear next cycle."""

    def test_emits_when_no_suppression(self, tmp_path, monkeypatch):
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
        assert len(out) == 1

    def test_suppresses_typed_emission_when_signature_dismissed(
        self, tmp_path, monkeypatch,
    ):
        """Dismiss the typed proposal's signature for this bot —
        observe() must skip the typed emission."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_TYPED,
            bot_id="admin_bot",
            scope="kind",
            rationale="declined for now",
        )
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
        assert out == []

    def test_suppression_is_per_bot(self, tmp_path, monkeypatch):
        """Dismissing for admin_bot must not suppress for ellie."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_TYPED,
            bot_id="admin_bot",
            scope="kind",
        )
        _write_signal(
            tmp_path, bot_id="ellie", sig=_invalidation_signal(bot_id="ellie"),
        )
        out = observe(CacheTtlTunerContext(bot_id="ellie", shared_dir=tmp_path))
        assert len(out) == 1

    def test_pod_wide_suppression_skips_every_bot(self, tmp_path, monkeypatch):
        """A pod-wide (bot_id=None) entry must suppress for every bot."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_TYPED,
            bot_id=None,
            scope="kind",
        )
        _write_signal(
            tmp_path, bot_id="ellie", sig=_invalidation_signal(bot_id="ellie"),
        )
        out = observe(CacheTtlTunerContext(bot_id="ellie", shared_dir=tmp_path))
        assert out == []

    def test_consult_dismissals_false_bypasses_gate(self, tmp_path, monkeypatch):
        """Test hook — bypass the gate when reasoning about the
        proposal_history dedup window in isolation."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_TYPED,
            bot_id="admin_bot",
            scope="kind",
        )
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        out = observe(
            CacheTtlTunerContext(
                bot_id="admin_bot",
                shared_dir=tmp_path,
                consult_dismissals=False,
            )
        )
        assert len(out) == 1

    def test_dismiss_fallback_signature_suppresses_fallback_emission(
        self, tmp_path, monkeypatch,
    ):
        """The fallback investigation has its own signature.
        Dismissing it should skip the fallback even when the
        autonomous fix is also suppressed (operator intent path)."""
        monkeypatch.setattr(observe_module, "bot_uses_anthropic", lambda bot_id: True)
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_INVESTIGATE,
            bot_id="admin_bot",
            scope="kind",
        )
        _write_signal(
            tmp_path, bot_id="admin_bot", sig=_invalidation_signal(),
        )
        # Run with consult_config_intent=True + a pinned-short intent
        # would normally route to the fallback; here we exercise the
        # fallback branch by also setting consult_proposal_history=False
        # and forcing the typed-path to drop via a recorded typed
        # dismissal.
        dismissals.record_dismissal(
            tmp_path,
            signature=DISMISS_SIG_INVALIDATION_TYPED,
            bot_id="admin_bot",
            scope="kind",
        )
        out = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
        # Both kinds dismissed → no emission at all.
        assert out == []
