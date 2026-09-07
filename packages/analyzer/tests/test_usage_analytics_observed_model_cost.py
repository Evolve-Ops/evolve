"""Tests for the observed per-model $/1k-token enrichment in usage_analytics.

Pins:
  1. _observed_per_1k math — split (input/output) and blended rates.
  2. Min-sample threshold drives the low_confidence flag.
  3. compute_summary's by_model rows carry the new fields and tally
     input/output tokens correctly across turns.
  4. Zero-cost / zero-token models degrade to None rates, never raise.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_analytics as ua  # noqa: E402


def _turn(model, cost, inp, out, **kw):
    base = {
        "ts": "2026-06-10T01:00:00Z",
        "model": model,
        "source": "human",
        "cost": cost,
        "input_tokens": inp,
        "output_tokens": out,
        "session_id": "s-" + str(inp) + str(out),
        "auth_mode": "api_key",
    }
    base.update(kw)
    return base


# ── _observed_per_1k unit ────────────────────────────────────────────────────


def test_observed_per_1k_split_and_blended():
    # $0.40 over 30k input + 5k output tokens.
    r = ua._observed_per_1k(0.40, 30_000, 5_000)
    # $0.40 / (30k/1k) = $0.01333…
    assert r["usd_per_1k_input"] == 0.0133
    # $0.40 / (5k/1k) = $0.08
    assert r["usd_per_1k_output"] == 0.08
    # $0.40 / (35k/1k) = $0.01142…
    assert r["usd_per_1k_blended"] == 0.0114
    assert r["billed_tokens"] == 35_000
    assert r["low_confidence"] is False


def test_observed_per_1k_min_sample_flag():
    # 600 billed tokens is well under the 10k threshold.
    r = ua._observed_per_1k(0.001, 500, 100)
    assert r["billed_tokens"] == 600
    assert r["low_confidence"] is True
    # Just at the threshold → trusted.
    r2 = ua._observed_per_1k(1.0, ua.MODEL_COST_MIN_TOKENS, 0)
    assert r2["low_confidence"] is False


def test_observed_per_1k_zero_cost_or_tokens_returns_none():
    # No cost recorded → rates are None (not a divide-by-zero, not 0.0).
    r = ua._observed_per_1k(0.0, 10_000, 2_000)
    assert r["usd_per_1k_input"] is None
    assert r["usd_per_1k_output"] is None
    assert r["usd_per_1k_blended"] is None
    # Cost present but no output tokens → output rate None, input/blended set.
    r2 = ua._observed_per_1k(0.5, 20_000, 0)
    assert r2["usd_per_1k_input"] is not None
    assert r2["usd_per_1k_output"] is None
    assert r2["usd_per_1k_blended"] is not None


# ── compute_summary integration ──────────────────────────────────────────────


def test_by_model_rows_carry_observed_fields_and_tally_tokens():
    turns = [
        _turn("anthropic/claude-sonnet-4-6", 0.30, 20_000, 4_000, session_id="s1"),
        _turn("anthropic/claude-sonnet-4-6", 0.10, 10_000, 1_000,
              source="cron", session_id="s2"),
        _turn("anthropic/claude-haiku-4-5", 0.001, 500, 100, session_id="s3"),
    ]
    summary = ua.compute_summary(turns)
    by_model = {r["model"]: r for r in summary["by_model"]}

    sonnet = by_model["anthropic/claude-sonnet-4-6"]
    # Tokens summed across both sonnet turns.
    assert sonnet["input_tokens"] == 30_000
    assert sonnet["output_tokens"] == 5_000
    assert sonnet["cost"] == 0.4
    # 35k billed tokens → trusted; blended = $0.40 / 35 = $0.0114.
    assert sonnet["low_confidence"] is False
    assert sonnet["usd_per_1k_blended"] == 0.0114

    haiku = by_model["anthropic/claude-haiku-4-5"]
    # 600 billed tokens → flagged low-confidence.
    assert haiku["low_confidence"] is True


def test_empty_summary_has_no_model_rows():
    # Sanity: the empty path still returns an empty by_model list (the
    # enrichment must not change the no-turns contract).
    summary = ua.compute_summary([])
    assert summary["by_model"] == []
