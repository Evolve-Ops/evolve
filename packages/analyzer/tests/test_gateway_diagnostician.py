"""Regression tests for the gateway_diagnostician generator.

The diagnostician is the first generator that closes the alerts → proposals
loop: it reads heal's incident stream, applies a heuristic, and emits a
WorkflowInstruction proposal asking the operator to raise the per-attempt
timeout when recent gateway_down incidents are dominated by timeout-shaped
errors.

Pins the contract that:
  - The detector emits exactly one proposal per (bot, run) when the
    pattern is present.
  - The detector stays silent when there's not enough evidence, when the
    timeout pattern doesn't dominate, or when the timeout is already at
    or above the ceiling.
  - The proposal carries enough provenance for the operator to evaluate
    the suggestion: incident count, timeout share, current value, proposed
    value.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from proposal_synthesizer.store import iter_candidates  # noqa: E402

# Phase 6c cutover: observe() returns []; findings flow through the
# candidate store. Tests inspect candidates/pending/ instead.


def _candidates(shared_dir: Path) -> list:
    return list(iter_candidates(shared_dir, subdirs=("pending",)))


from generators.gateway_diagnostician.observe import (  # noqa: E402
    GatewayDiagnosticianContext,
    _looks_like_timeout,
    observe,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_incident(
    shared_dir: Path,
    bot_id: str,
    when: datetime,
    *,
    type_: str = "gateway_down",
    detail: str = "",
    response_time_ms: float | None = None,
) -> None:
    day = when.strftime("%Y-%m-%d")
    day_dir = shared_dir / "incidents" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = when.strftime("%H%M%S%f")
    out = day_dir / f"{bot_id}-{ts}-{type_}.json"
    body = {
        "bot_id": bot_id,
        "detected_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": type_,
        "detail": detail,
        "port": 19000,
    }
    if response_time_ms is not None:
        body["response_time_ms"] = response_time_ms
    out.write_text(json.dumps(body))


def _make_ctx(shared_dir: Path, *, timeout: int = 30, **overrides) -> GatewayDiagnosticianContext:
    base = dict(
        bot_id="team_bot_a",
        shared_dir=shared_dir,
        heal_timeout_seconds=timeout,
        slow_threshold_ms=3000,
        now=datetime.now(timezone.utc),
        window_days=7,
        min_incidents=5,
        timeout_share_threshold=0.5,
        timeout_ceiling_seconds=60,
        proposed_increment_seconds=15,
        min_slow_incidents=10,
        max_down_for_slow_diagnosis=2,
        slow_threshold_ceiling_ms=10000,
        slow_threshold_margin_factor=1.2,
        slow_threshold_headroom_ms=500,
    )
    base.update(overrides)
    return GatewayDiagnosticianContext(**base)


def _seed_slow_incidents(
    shared_dir: Path,
    bot_id: str,
    now: datetime,
    *,
    count: int,
    response_time_ms: float,
) -> None:
    """Helper: write ``count`` gateway_slow incidents at varying response times
    centered on ``response_time_ms`` so the median is predictable."""
    for i in range(count):
        # Spread response times symmetrically: median sits at the requested value.
        rt = response_time_ms + (200 if i % 2 == 0 else -200)
        _write_incident(
            shared_dir, bot_id, now - timedelta(hours=2 * (i + 1)),
            type_="gateway_slow",
            detail=f"Response time {rt:.0f}ms",
            response_time_ms=rt,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Substring matcher
# ─────────────────────────────────────────────────────────────────────────────


def test_timeout_matcher_recognizes_known_shapes():
    """The matcher must catch the timeout patterns heal.py actually logs."""
    yes = [
        "subprocess.TimeoutExpired: 30s",
        "WebSocket abnormal closure (1006)",
        "openclaw health request timed out",
        "TimeoutExpired",
        "ws closed: 1006",
    ]
    for d in yes:
        assert _looks_like_timeout(d), f"expected timeout match for: {d!r}"

    no = [
        "",
        "connection refused",
        "openclaw not found",
        "schema mismatch",
        "permission denied",
    ]
    for d in no:
        assert not _looks_like_timeout(d), f"expected no match for: {d!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Detector — emit path
# ─────────────────────────────────────────────────────────────────────────────


def test_emits_when_timeout_pattern_dominates(tmp_path):
    now = datetime.now(timezone.utc)
    # 8 incidents — 6 timeout-shaped (>= 50%), 2 not — over the last week
    for i in range(6):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail=f"oc_health subprocess TimeoutExpired after 30s (attempt {i+1})",
        )
    for i in range(2):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 7)),
            detail="connection refused",
        )

    ctx = _make_ctx(tmp_path, timeout=30)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1, "diagnostician should emit one candidate"

    c = cands[0]
    assert c.bot_id == "team_bot_a"
    assert c.generator_id == "gateway_diagnostician"
    assert c.dimension == "substrate_health"
    assert c.draft_action.kind == "WorkflowInstruction"
    assert c.draft_urgency == "improvement"
    assert c.draft_approval_audience == "pod_operator"
    sig = c.provenance.signals
    assert sig["incidents_in_window"] == 8
    assert sig["timeout_share"] == 0.75
    assert sig["current_seconds"] == 30
    assert sig["proposed_seconds"] == 45
    assert c.draft_claim is not None
    assert c.draft_claim.metric == "gateway.consecutive_failures_24h"
    assert c.draft_claim.direction == "down"
    assert c.draft_claim.fallback == "revert"
    assert "ocHealthTimeoutSeconds" in c.draft_action.content
    assert "45" in c.draft_action.content


def test_proposed_value_is_capped_at_ceiling(tmp_path):
    """Increment must not push the value past the ceiling — otherwise we'd
    propose changes that are bigger than the diagnostician should make."""
    now = datetime.now(timezone.utc)
    for i in range(6):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )

    # Start near ceiling: 50s → +15 would be 65, ceiling caps at 60
    ctx = _make_ctx(tmp_path, timeout=50, timeout_ceiling_seconds=60)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].provenance.signals["proposed_seconds"] == 60


# ─────────────────────────────────────────────────────────────────────────────
# Detector — silence paths
# ─────────────────────────────────────────────────────────────────────────────


def test_silent_when_too_few_incidents(tmp_path):
    """Below ``min_incidents`` the pattern isn't well-supported. Quiet."""
    now = datetime.now(timezone.utc)
    for i in range(3):  # < min_incidents=5
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )
    assert observe(_make_ctx(tmp_path, timeout=30)) == []


def test_silent_when_timeout_share_below_threshold(tmp_path):
    """If timeouts are a minority of failures, the right fix is probably
    something else entirely. Don't propose a timeout raise on weak evidence."""
    now = datetime.now(timezone.utc)
    for i in range(6):  # mostly NOT timeouts
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="connection refused",
        )
    for i in range(2):  # only 2/8 = 25% < 50%
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 7)),
            detail="subprocess TimeoutExpired",
        )
    assert observe(_make_ctx(tmp_path, timeout=30)) == []


def test_silent_when_already_at_ceiling(tmp_path):
    """If the timeout is already at or above the ceiling, there's nothing
    further the diagnostician can suggest — it must escalate or stay quiet."""
    now = datetime.now(timezone.utc)
    for i in range(8):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )
    ctx = _make_ctx(tmp_path, timeout=60, timeout_ceiling_seconds=60)
    assert observe(ctx) == []


def test_silent_when_no_incidents_at_all(tmp_path):
    """No data → no proposal. (The Alerts surface stays empty too;
    nothing for the diagnostician to comment on.)"""
    ctx = _make_ctx(tmp_path, timeout=30)
    assert observe(ctx) == []


def test_only_counts_incidents_within_window(tmp_path):
    """An incident from 30 days ago should not count toward a 7-day window —
    otherwise old data could sustain a stale recommendation forever."""
    now = datetime.now(timezone.utc)
    # 4 fresh + 4 ancient → only 4 in window, below min_incidents=5
    for i in range(4):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )
    for i in range(4):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(days=30 + i),
            detail="subprocess TimeoutExpired",
        )
    assert observe(_make_ctx(tmp_path, timeout=30, window_days=7)) == []


def test_only_counts_this_bots_incidents(tmp_path):
    """Per-bot detector — incidents for evolve must not influence team_bot_a's diagnosis."""
    now = datetime.now(timezone.utc)
    for i in range(8):
        _write_incident(
            tmp_path, "evolve", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )
    assert observe(_make_ctx(tmp_path, timeout=30)) == []


def test_only_counts_gateway_down_incidents(tmp_path):
    """restart_attempted / restart_succeeded entries are bookkeeping, not
    failure evidence. They must not inflate the timeout-share calculation."""
    now = datetime.now(timezone.utc)
    for i in range(8):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            type_="restart_attempted",
            detail="kicking gateway",
        )
    assert observe(_make_ctx(tmp_path, timeout=30)) == []


# ─────────────────────────────────────────────────────────────────────────────
# Detector — slow_threshold_too_low: emit path
# ─────────────────────────────────────────────────────────────────────────────


def test_slow_emits_when_median_well_above_threshold(tmp_path):
    """A heavy bot whose normal probes hover at 5500ms with the threshold at
    3000ms should get a calibrated raise to ~6000ms (median + headroom)."""
    now = datetime.now(timezone.utc)
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=5500)

    ctx = _make_ctx(tmp_path, slow_threshold_ms=3000)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1, "diagnostician should emit one slow-threshold candidate"

    c = cands[0]
    assert c.bot_id == "team_bot_a"
    assert c.generator_id == "gateway_diagnostician"
    assert c.draft_urgency == "hygiene"
    assert c.draft_action.kind == "WorkflowInstruction"

    sig = c.provenance.signals
    assert sig["slow_incidents_in_window"] == 12
    assert sig["down_incidents_in_window"] == 0
    assert sig["current_threshold_ms"] == 3000
    assert sig["median_response_ms"] == 5500
    assert sig["proposed_threshold_ms"] == 6000
    assert c.draft_claim is not None
    assert c.draft_claim.metric == "gateway.slow_incidents_24h"
    assert c.draft_claim.direction == "down"
    assert c.draft_claim.fallback == "revert"
    assert "slowThresholdMs" in c.draft_action.content
    assert "6000" in c.draft_action.content


def test_slow_proposed_value_capped_at_ceiling(tmp_path):
    """median + headroom must never exceed slow_threshold_ceiling_ms — that's
    the operator's hard upper bound on what the diagnostician can suggest."""
    now = datetime.now(timezone.utc)
    # Median 9800, headroom 500 → would suggest 10300; ceiling caps at 10000.
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=9800)

    ctx = _make_ctx(tmp_path, slow_threshold_ms=3000, slow_threshold_ceiling_ms=10000)
    assert observe(ctx) == []
    cands = _candidates(tmp_path)
    assert len(cands) == 1
    assert cands[0].provenance.signals["proposed_threshold_ms"] == 10000


def test_detectors_run_independently_in_one_observe(tmp_path):
    """observe() drives every detector. With timeout evidence present and no
    slow evidence, exactly the timeout detector fires; the slow detector's
    gate (down-count must stay low) is satisfied by zero slows but no slow
    evidence means nothing to propose. This pins that observe() doesn't
    short-circuit after the first detector — adding more detectors later
    keeps the existing emit paths intact."""
    now = datetime.now(timezone.utc)
    for i in range(6):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )

    assert observe(_make_ctx(tmp_path, slow_threshold_ms=3000)) == []
    techniques = [c.provenance.technique for c in _candidates(tmp_path)]
    assert techniques == ["gateway_diagnostician.health_timeout_too_low"]


def test_slow_diagnosis_gated_by_down_count(tmp_path):
    """Slow + many gateway_down events means the bot is degrading — the
    right fix is probably the timeout knob, not the slow threshold. The
    slow detector should defer when down-count exceeds its tolerance,
    leaving the timeout detector to handle the case alone. This test pins
    the gate that prevents the slow detector from second-guessing real
    failures."""
    now = datetime.now(timezone.utc)
    # Strong slow evidence
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=5500)
    # Plus enough down events to exceed max_down_for_slow_diagnosis=2
    for i in range(5):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="subprocess TimeoutExpired",
        )

    assert observe(_make_ctx(tmp_path, slow_threshold_ms=3000)) == []
    techniques = {c.provenance.technique for c in _candidates(tmp_path)}
    # Timeout detector fires (5 timeout-shaped downs == its min_incidents).
    assert "gateway_diagnostician.health_timeout_too_low" in techniques
    # Slow detector stays out of it because down-count > gate.
    assert "gateway_diagnostician.slow_threshold_too_low" not in techniques


# ─────────────────────────────────────────────────────────────────────────────
# Detector — slow_threshold_too_low: silence paths
# ─────────────────────────────────────────────────────────────────────────────


def test_slow_silent_when_too_few_slow_incidents(tmp_path):
    """Below ``min_slow_incidents`` the noise floor isn't well-supported."""
    now = datetime.now(timezone.utc)
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=5, response_time_ms=5500)
    assert observe(_make_ctx(tmp_path, slow_threshold_ms=3000)) == []


def test_slow_silent_when_too_many_down_incidents(tmp_path):
    """Slow + many gateway_down means the bot is degrading — the right fix
    is probably the timeout knob, not the slow threshold. Stay quiet on the
    slow side and let the timeout detector handle it."""
    now = datetime.now(timezone.utc)
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=5500)
    # Many real failures alongside the slow incidents
    for i in range(5):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            detail="connection refused",
        )

    proposals = observe(_make_ctx(tmp_path, slow_threshold_ms=3000))
    techniques = {p.provenance.technique for p in proposals}
    assert "gateway_diagnostician.slow_threshold_too_low" not in techniques


def test_slow_silent_when_already_at_ceiling(tmp_path):
    now = datetime.now(timezone.utc)
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=9500)
    ctx = _make_ctx(
        tmp_path, slow_threshold_ms=10000, slow_threshold_ceiling_ms=10000,
    )
    assert observe(ctx) == []


def test_slow_silent_when_median_within_margin(tmp_path):
    """If the median is only marginally above the current threshold, the
    diagnostician shouldn't propose a churn-y small bump. Wait for evidence
    the noise floor is clearly elevated."""
    now = datetime.now(timezone.utc)
    # Threshold 3000, median 3200 → ratio 1.067, below margin_factor=1.2
    _seed_slow_incidents(tmp_path, "team_bot_a", now, count=12, response_time_ms=3200)
    assert observe(_make_ctx(tmp_path, slow_threshold_ms=3000)) == []


def test_slow_silent_when_records_lack_response_time_ms(tmp_path):
    """Defensive: pre-existing slow incidents predating the response_time_ms
    field shouldn't crash the detector — it should treat them as no signal
    and stay quiet."""
    now = datetime.now(timezone.utc)
    for i in range(12):
        _write_incident(
            tmp_path, "team_bot_a", now - timedelta(hours=2 * (i + 1)),
            type_="gateway_slow",
            detail="slow probe (no response_time_ms field)",
        )
    assert observe(_make_ctx(tmp_path, slow_threshold_ms=3000)) == []
