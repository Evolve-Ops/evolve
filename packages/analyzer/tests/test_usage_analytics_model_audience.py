"""compute_summary's by_model_by_audience field — Model × Audience table.

The Usage tab's new "Model × Audience" card answers a single question:
"am I using premium models on autonomous work when a cheaper tier would
do?" That requires splitting each model's calls + cost by whether the
turn came from direct operator input (Human) or anything else (Non-
Human — heartbeat, cron, subagent, forge, unknown).

These tests pin:
  - The audience split mirrors compute_summary's bucket logic. The
    single source of truth is the bucket field — Human = bucket "human",
    Non-Human = bucket "scheduled" or "background".
  - Forge turns (added by _apply_forge_retag → source="forge") land in
    Non-Human. Regression check on the cost-misclassification bug the
    2026-05-29 atlas incident surfaced.
  - Models with only one audience represented still show in the list
    with zeros for the other side.
  - Sort order is by total cost descending — the operator's first
    question is "where is the money going."
  - Empty-input path returns an empty list (not a crash).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import usage_analytics as ua  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Turn fixtures — match the cost-event-converter schema
# ─────────────────────────────────────────────────────────────────────────────


def _turn(
    *, model="anthropic/claude-sonnet-4-6", source="human", channel="telegram",
    cost=0.5, session_id="s1", ts="2026-05-29T10:00:00Z",
):
    """Minimal valid turn record — only fields compute_summary reads."""
    return {
        "ts": ts,
        "instance": "atlas",
        "model": model,
        "provider": model.split("/")[0] if "/" in model else "anthropic",
        "source": source,
        "channel": channel,
        "user_id": "ops",
        "cost": cost,
        "input_tokens": 5,
        "output_tokens": 200,
        "cache_read_tokens": 10000,
        "cache_write_tokens": 0,
        "session_id": session_id,
        "auth_mode": "api_key",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Audience split — bucket semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_human_source_lands_in_human_audience():
    summary = ua.compute_summary([_turn(source="human", cost=1.0)])
    rows = summary["by_model_by_audience"]
    assert len(rows) == 1
    assert rows[0]["human"] == {"calls": 1, "cost": 1.0}
    assert rows[0]["non_human"] == {"calls": 0, "cost": 0.0}


def test_heartbeat_source_lands_in_non_human():
    """Heartbeat is autonomous; UI bucket "scheduled" rolls into Non-Human."""
    summary = ua.compute_summary([_turn(source="heartbeat", cost=0.3)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["human"] == {"calls": 0, "cost": 0.0}
    assert rows[0]["non_human"] == {"calls": 1, "cost": 0.3}


def test_cron_source_lands_in_non_human():
    summary = ua.compute_summary([_turn(source="cron", cost=0.4)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["non_human"]["calls"] == 1


def test_subagent_source_lands_in_non_human():
    """Subagent is bucket "background" → Non-Human."""
    summary = ua.compute_summary([_turn(source="subagent", cost=0.2)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["non_human"]["calls"] == 1


def test_forge_source_lands_in_non_human():
    """Forge turns (after _apply_forge_retag) must NOT inflate Human.
    This is the regression guard for the 2026-05-29 atlas bug — the
    Model × Audience card would be misleading if Forge counted as Human."""
    summary = ua.compute_summary([_turn(source="forge", cost=1.0)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["human"] == {"calls": 0, "cost": 0.0}
    assert rows[0]["non_human"]["calls"] == 1
    assert rows[0]["non_human"]["cost"] == 1.0


def test_unknown_source_lands_in_non_human():
    """Unmapped / null source is autonomous-ish by default. Bucket
    "background" → Non-Human."""
    summary = ua.compute_summary([_turn(source="unknown", cost=0.1)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["non_human"]["calls"] == 1


def test_user_source_normalized_to_human():
    """source="user" from the evolve-shared TurnObserver path normalizes
    to "human" in load_turns; the audience split must also see it as
    human regardless of which schema produced the row."""
    summary = ua.compute_summary([_turn(source="user", cost=0.7)])
    rows = summary["by_model_by_audience"]
    assert rows[0]["human"]["calls"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Per-model aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_mixed_audience_for_same_model():
    """Same model used in both audiences — both sides populated."""
    summary = ua.compute_summary([
        _turn(model="anthropic/claude-sonnet-4-6", source="human", cost=2.0),
        _turn(model="anthropic/claude-sonnet-4-6", source="heartbeat", cost=0.5,
              session_id="s2"),
        _turn(model="anthropic/claude-sonnet-4-6", source="forge", cost=1.5,
              session_id="s3"),
    ])
    rows = summary["by_model_by_audience"]
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "anthropic/claude-sonnet-4-6"
    assert r["human"] == {"calls": 1, "cost": 2.0}
    assert r["non_human"] == {"calls": 2, "cost": 2.0}
    assert r["total_calls"] == 3
    assert r["total_cost"] == 4.0


def test_separate_rows_per_model():
    summary = ua.compute_summary([
        _turn(model="anthropic/claude-sonnet-4-6", source="human", cost=2.0),
        _turn(model="anthropic/claude-haiku-4-5", source="heartbeat", cost=0.1,
              session_id="s2"),
    ])
    rows = summary["by_model_by_audience"]
    assert len(rows) == 2
    by_model = {r["model"]: r for r in rows}
    assert by_model["anthropic/claude-sonnet-4-6"]["human"]["calls"] == 1
    assert by_model["anthropic/claude-haiku-4-5"]["non_human"]["calls"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Sort order + structural fields
# ─────────────────────────────────────────────────────────────────────────────


def test_sorted_by_total_cost_desc():
    """Highest-cost model first — operators look at this card to spot
    where the money goes."""
    summary = ua.compute_summary([
        _turn(model="anthropic/claude-haiku-4-5", source="human", cost=0.05),
        _turn(model="anthropic/claude-sonnet-4-6", source="human", cost=5.0,
              session_id="s2"),
        _turn(model="anthropic/claude-opus-4-6", source="human", cost=1.0,
              session_id="s3"),
    ])
    rows = summary["by_model_by_audience"]
    assert [r["model"] for r in rows] == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-6",
        "anthropic/claude-haiku-4-5",
    ]


def test_row_includes_color_field_for_ui_dot():
    """The UI renders the same colored swatch as the By Model table —
    relies on the color field flowing through compute_summary."""
    summary = ua.compute_summary([_turn(model="anthropic/claude-sonnet-4-6")])
    assert "color" in summary["by_model_by_audience"][0]


def test_empty_input_returns_empty_list():
    """No crash on the no-data path — Usage tab renders "No data" placeholder."""
    summary = ua.compute_summary([])
    assert summary["by_model_by_audience"] == []


def test_existing_by_model_total_matches_audience_total():
    """Consistency check: per-model totals in by_model and the sum of
    human + non_human in by_model_by_audience must agree. If they
    diverge, one of the rollups missed a record path."""
    summary = ua.compute_summary([
        _turn(model="anthropic/claude-sonnet-4-6", source="human", cost=2.0),
        _turn(model="anthropic/claude-sonnet-4-6", source="forge", cost=1.5,
              session_id="s2"),
    ])
    bm = {r["model"]: r for r in summary["by_model"]}
    bma = {r["model"]: r for r in summary["by_model_by_audience"]}
    sonnet_bm = bm["anthropic/claude-sonnet-4-6"]
    sonnet_bma = bma["anthropic/claude-sonnet-4-6"]
    assert sonnet_bm["calls"] == sonnet_bma["total_calls"]
    assert abs(sonnet_bm["cost"] - sonnet_bma["total_cost"]) < 1e-9
    assert (sonnet_bma["human"]["calls"] + sonnet_bma["non_human"]["calls"]
            == sonnet_bm["calls"])
