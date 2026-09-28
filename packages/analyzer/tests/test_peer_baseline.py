"""tests/test_peer_baseline.py — Phase 3 peer_baseline tests."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from investigation.peer_baseline import (  # noqa: E402
    PeerBaselineResult,
    peer_baseline,
    role_for_bot,
)


def test_role_from_explicit_config():
    cfg = {"bots": {"security_bot": {"role": "auditor"}}}
    assert role_for_bot("security_bot", cfg) == "auditor"


def test_role_falls_through_to_inference():
    cfg = {"bots": {"security_bot": {"user": "security_bot"}}}
    oc = {"agents": {"defaults": {"model": {"primary": "claude-haiku-4-5"}}}}
    role = role_for_bot("security_bot", cfg, oc_json_reader=lambda b: oc)
    assert role == "auditor"


def test_role_inference_recognizes_sonnet():
    oc = {"agents": {"defaults": {"model": {"primary": "claude-sonnet-4-6"}}}}
    role = role_for_bot("team_bot_a", {}, oc_json_reader=lambda b: oc)
    assert role == "primary"


def test_role_inference_unknown_when_no_oc_reader():
    assert role_for_bot("personal_bot", {}) == "unknown"


def test_peer_baseline_compares_same_role():
    """Security_bot vs team_bot_a + team_bot_b (all auditors); team_bot_c + admin_bot excluded as
    different role."""
    cfg = {
        "primary": "evolve",
        "members": ["security_bot", "team_bot_a", "team_bot_b", "team_bot_c", "admin_bot"],
        "bots": {
            "security_bot": {"role": "auditor"},
            "team_bot_a": {"role": "auditor"},
            "team_bot_b": {"role": "auditor"},
            "team_bot_c": {"role": "primary"},
            "admin_bot": {"role": "primary"},
            "evolve": {"role": "primary"},
        },
    }
    metrics = {
        "security_bot": 0.07,
        "team_bot_a": 0.01,
        "team_bot_b": 0.012,
        "team_bot_c": 0.03,
        "admin_bot": 0.025,
        "evolve": 0.04,
    }
    result = peer_baseline(
        "security_bot", "cost_per_call_low_tier",
        config=cfg, metric_reader=lambda b: metrics.get(b),
    )
    assert result.role == "auditor"
    assert sorted(result.peer_values) == [0.01, 0.012]
    assert result.bot_value == 0.07
    # Security_bot at 0.07 / peer_median 0.011 = ~6.4× — clear outlier
    assert result.ratio_to_median is not None
    assert result.ratio_to_median > 5.0


def test_peer_baseline_empty_when_no_role_peers():
    cfg = {
        "primary": "evolve",
        "members": ["security_bot"],
        "bots": {"security_bot": {"role": "auditor"}, "evolve": {"role": "primary"}},
    }
    result = peer_baseline(
        "security_bot", "metric",
        config=cfg, metric_reader=lambda b: 1.0,
    )
    assert result.peer_values == []
    assert result.ratio_to_median is None


def test_peer_baseline_excludes_subject_bot():
    """The bot under investigation must not appear in peer_values."""
    cfg = {
        "primary": "evolve",
        "members": ["security_bot", "team_bot_a"],
        "bots": {
            "security_bot": {"role": "auditor"},
            "team_bot_a": {"role": "auditor"},
            "evolve": {"role": "primary"},
        },
    }
    result = peer_baseline(
        "security_bot", "x",
        config=cfg, metric_reader=lambda b: 1.0,
    )
    assert result.peer_values == [1.0]


def test_peer_baseline_drops_none_readers():
    cfg = {
        "primary": "evolve",
        "members": ["security_bot", "team_bot_a", "team_bot_b"],
        "bots": {
            "security_bot": {"role": "auditor"},
            "team_bot_a": {"role": "auditor"},
            "team_bot_b": {"role": "auditor"},
            "evolve": {"role": "primary"},
        },
    }

    def reader(b):
        return {"team_bot_a": 5.0, "team_bot_b": None, "security_bot": 10.0}.get(b)

    result = peer_baseline(
        "security_bot", "x", config=cfg, metric_reader=reader,
    )
    # team_bot_b returns None → dropped; team_bot_a included
    assert result.peer_values == [5.0]
    assert result.bot_value == 10.0
