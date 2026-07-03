"""tests/test_budget_hawk_phase_c.py — Phase C-7 content + gates.

Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md.

budget_hawk has 7 proposal factories. This file pins:
  1. Each factory carries Phase C-7 content fields (summary,
     explanation, action_label, manual_path, dismiss_signature).
  2. The 8 distinct signatures are pairwise distinct.
  3. app_cost_imbalance.dominance is per-app (X dismissed doesn't
     suppress Y).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

p = importlib.import_module("generators.budget_hawk.proposals")


class TestContent:
    def test_warn_cap_crossed(self):
        x = p.make_warn_cap_crossed("team_bot_a", current_usd=3.0, cap_usd=2.0)
        assert x.summary and x.explanation and x.action_label == "Open Cost tab"
        assert x.dismiss_signature == p.DISMISS_SIG_WARN_CAP_CROSSED

    def test_tier_downgrade(self):
        x = p.make_tier_downgrade(
            "team_bot_a", target_class="maintenance", new_tier="haiku",
        )
        assert x.summary and x.explanation
        assert x.action_label == "Move maintenance to haiku"
        assert x.dismiss_signature == p.DISMISS_SIG_TIER_DOWNGRADE

    def test_cost_anomaly(self):
        x = p.make_cost_anomaly(
            "team_bot_a", current_usd=5.0, mean_usd=1.0, stdevs=4.0,
        )
        assert x.summary and x.action_label == "Open Sessions page"
        assert x.dismiss_signature == p.DISMISS_SIG_COST_ANOMALY

    def test_warn_pattern_investigation(self):
        x = p.make_warn_pattern_investigation(
            "team_bot_a", current_usd=3.0, cap_usd=2.0, observation_count=4,
        )
        assert x.summary and x.dismiss_signature == p.DISMISS_SIG_WARN_CAP_PATTERN

    def test_summarizer_min_turns_patch(self):
        x = p.make_summarizer_min_turns_patch(
            "team_bot_a", new_value=3,
            offending_events=10, cost_waste_usd=0.05, lookback_days=7,
            example_session_ids=["s1"],
        )
        assert x.summary and "summarizerMinTurns" in x.action_label
        assert x.dismiss_signature == p.DISMISS_SIG_SUMMARIZER_TRIVIAL

    def test_app_cost_dominance_per_app(self):
        a = p.make_app_cost_imbalance(
            "team_bot_a", kind="dominance",
            dominant_app="memo", dominant_cost_usd=4.0,
            dominant_share=0.8, total_cost_usd=5.0, lookback_days=7,
        )
        b = p.make_app_cost_imbalance(
            "team_bot_a", kind="dominance",
            dominant_app="task", dominant_cost_usd=4.0,
            dominant_share=0.8, total_cost_usd=5.0, lookback_days=7,
        )
        assert a.dismiss_signature != b.dismiss_signature
        assert "memo" in a.dismiss_signature
        assert "task" in b.dismiss_signature

    def test_app_cost_coverage(self):
        x = p.make_app_cost_imbalance(
            "team_bot_a", kind="coverage",
            dominant_app=None, dominant_cost_usd=4.0,
            dominant_share=0.8, total_cost_usd=5.0, lookback_days=7,
        )
        assert x.dismiss_signature == p.DISMISS_SIG_APP_COST_COVERAGE

    def test_classifier_threshold_patch(self):
        x = p.make_classifier_threshold_patch(
            "team_bot_a", new_value=0.85,
            classifier_events=20, per_day=3.0, confident_share=0.9,
            total_cost_usd=0.10, lookback_days=7,
        )
        assert x.summary
        assert x.dismiss_signature == p.DISMISS_SIG_CLASSIFIER_NOISE


class TestSignaturesDistinct:
    def test_all_signatures_pairwise_distinct(self):
        sigs = {
            p.DISMISS_SIG_WARN_CAP_CROSSED,
            p.DISMISS_SIG_TIER_DOWNGRADE,
            p.DISMISS_SIG_COST_ANOMALY,
            p.DISMISS_SIG_WARN_CAP_PATTERN,
            p.DISMISS_SIG_SUMMARIZER_TRIVIAL,
            p.DISMISS_SIG_APP_COST_COVERAGE,
            p.DISMISS_SIG_CLASSIFIER_NOISE,
            p.dismiss_signature_for_app_dominance("any-app"),
        }
        assert len(sigs) == 8
