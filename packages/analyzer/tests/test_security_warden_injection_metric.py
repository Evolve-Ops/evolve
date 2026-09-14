"""Tests for security.injection_events_per_week resolver.

Verifies the metric counts only proposals that originated from
Security Warden's prompt-injection scanner, scoped to the requested
bot, and inside the trailing 7-day window.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from arbiter.store import write_proposal  # noqa: E402
from metrics.resolvers import security as sec_mod  # noqa: E402
from metrics.registry import known, resolve  # noqa: E402
from schema.proposal import (  # noqa: E402
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


_NOW = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _restore_shared_dir():
    """Don't leak the test shared_dir override into later tests in the suite."""
    original = sec_mod._SHARED_DIR
    yield
    sec_mod.set_shared_dir(original)


def _make_proposal(
    *,
    bot_id: str,
    generator_id: str,
    trigger: str,
    created_at: datetime,
) -> Proposal:
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=generator_id,
        dimension="safety",
        trigger_observations=[trigger],
        provenance=Provenance(technique="t"),
        problem="x",
        action=Investigation(context="x"),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        approval_audience="pod_operator",
        urgency="security_critical",
        admin_surface_summary="x",
        created_at=created_at.isoformat(timespec="seconds"),
        status="pending",
    )


def test_metric_is_registered():
    assert "security.injection_events_per_week" in known()


def test_metric_counts_recent_injection_proposals_only(tmp_path):
    sec_mod.set_shared_dir(tmp_path)

    fresh = _NOW - timedelta(days=2)
    fresher = _NOW - timedelta(hours=3)
    stale = _NOW - timedelta(days=10)

    proposals = [
        _make_proposal(
            bot_id="team_bot_a", generator_id="security_warden",
            trigger="prompt_injection:s1:0", created_at=fresh,
        ),
        _make_proposal(
            bot_id="team_bot_a", generator_id="security_warden",
            trigger="prompt_injection:s2:0", created_at=fresher,
        ),
        # Stale: outside 7-day window
        _make_proposal(
            bot_id="team_bot_a", generator_id="security_warden",
            trigger="prompt_injection:s3:0", created_at=stale,
        ),
        # Wrong generator
        _make_proposal(
            bot_id="team_bot_a", generator_id="budget_hawk",
            trigger="prompt_injection:s4:0", created_at=fresh,
        ),
        # Right generator, wrong observation tag (credential, not injection)
        _make_proposal(
            bot_id="team_bot_a", generator_id="security_warden",
            trigger="credential_exposure:s5:0", created_at=fresh,
        ),
        # Different bot
        _make_proposal(
            bot_id="admin_bot", generator_id="security_warden",
            trigger="prompt_injection:s6:0", created_at=fresh,
        ),
    ]
    for p in proposals:
        write_proposal(p, tmp_path)

    value = resolve("security.injection_events_per_week", "team_bot_a", _NOW)
    assert value.value == 2.0


def test_metric_returns_zero_when_no_proposals(tmp_path):
    sec_mod.set_shared_dir(tmp_path)
    value = resolve("security.injection_events_per_week", "team_bot_a", _NOW)
    assert value.value == 0.0


def test_metric_skips_files_with_stale_mtime(tmp_path, monkeypatch):
    """O(N) optimisation: stale archived files are skipped at stat time
    without paying the JSON parse cost.
    """
    sec_mod.set_shared_dir(tmp_path)

    # One fresh proposal
    fresh = _make_proposal(
        bot_id="team_bot_a", generator_id="security_warden",
        trigger="prompt_injection:s1:0",
        created_at=_NOW - timedelta(hours=2),
    )
    fresh_path = write_proposal(fresh, tmp_path)

    # One stale archived proposal — mtime forced into the deep past
    stale = _make_proposal(
        bot_id="team_bot_a", generator_id="security_warden",
        trigger="prompt_injection:s2:0",
        created_at=_NOW - timedelta(days=30),
    )
    stale.status = "dismissed"
    stale_path = write_proposal(stale, tmp_path)
    import os as _os
    old_ts = (_NOW - timedelta(days=30)).timestamp()
    _os.utime(stale_path, (old_ts, old_ts))

    # Wrap json.loads to count parse calls
    parse_calls = 0
    real_loads = sec_mod.json.loads

    def counting_loads(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(sec_mod.json, "loads", counting_loads)

    value = resolve("security.injection_events_per_week", "team_bot_a", _NOW)
    assert value.value == 1.0
    # The stale file's mtime is 30 days old, well past the 8-day mtime
    # cutoff (7d window + 1d grace). It should be skipped without a parse.
    assert parse_calls == 1, f"expected 1 parse (fresh only), got {parse_calls}"
