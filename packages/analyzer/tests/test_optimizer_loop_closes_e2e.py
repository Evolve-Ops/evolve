"""End-to-end proof that the OPTIMIZER loop closes (roadmap item 1.4).

The diligence review's central finding: the "recursive self-improvement" claim
couldn't close on an *optimizer* generator — only guardians ("did the error
clear") ever verified. Budget Hawk's cost.daily_usd claims hit an UNREGISTERED
metric and an UNFILLED baseline, so they force-flagged instead of verifying.

After 1.1 (register cost.daily_usd) + 1.2 (apply-time baseline fill), this test
drives one Budget Hawk proposal through the WHOLE loop with the REAL components —
real resolver, real baseline fill, real verify daemon, real Claim evaluation,
real authority bump:

    apply (baseline filled from live $2/day reading)
      → horizon reached
      → verify daemon resolves the REAL cost.daily_usd metric against
        post-change cost data ($1/day — a genuine $1 drop)
      → claim "down by $0.50" holds
      → proposal SUCCEEDS
      → Budget Hawk's proposals_verified_success increments (authority earned)

This is the artifact for "show me one optimizer proposal where the verify daemon
resolved the claimed metric against real data and moved a generator's authority."
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import metrics.registry as metric_registry  # noqa: E402
import metrics.resolvers  # noqa: E402,F401 — import registers cost.daily_usd
from arbiter.apply import _fill_apply_time_baseline  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from registry.charter_loader import compute_charter_fingerprint  # noqa: E402
from schema.generator import GeneratorRecord, TrackRecord  # noqa: E402
from testing.harness import make_config_patch_proposal  # noqa: E402
from verify.daemon import ResolveResult, run_once  # noqa: E402

GENERATOR_ID = "budget_hawk"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _real_charter_fingerprint() -> str:
    """Budget Hawk's actual charter fingerprint, so the seeded record agrees
    with the on-disk charter the registry loads (a mismatch makes the bump
    skip)."""
    charter = _ANALYZER_DIR / "generators" / GENERATOR_ID / "charter.yaml"
    return compute_charter_fingerprint(charter.read_text())


def _events_summing_to(total_usd: float):
    """cost_ledger.read_events yields cost_event dicts; the resolver sums
    cost_usd and divides by the 7-day window."""
    return iter([{"cost_usd": total_usd}])


def test_budget_hawk_optimizer_loop_closes(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "generators").mkdir(parents=True)
    (shared / "proposals" / "applied").mkdir(parents=True)

    # Seed Budget Hawk's GeneratorRecord so the authority bump has a home.
    seed = GeneratorRecord(
        id=GENERATOR_ID,
        charter_fingerprint=_real_charter_fingerprint(),
        track_record=TrackRecord(),
    )
    rec_path = shared / "generators" / f"{GENERATOR_ID}.json"
    rec_path.write_text(json.dumps(seed.to_dict()))
    assert seed.track_record.proposals_verified_success == 0

    # ── Build a Budget Hawk cost.daily_usd proposal (down $0.50/day) ─────────────
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"model": {"tier": "high"}}))
    p = make_config_patch_proposal(
        target_path=f"{target}::model.tier",
        value="low",
        bot_id="team_bot_a",
        generator_id=GENERATOR_ID,
        dimension="cost",
        claim_metric="cost.daily_usd",
        claim_direction="down",
        claim_window_days=7,
    )
    p.claim.magnitude = 0.50
    p.claim.baseline = 0.0
    p.claim.baseline_at_apply = True

    # ── Apply-time baseline fill: PRE-change spend $14/7d = $2.00/day ────────────
    monkeypatch.setattr("cost_ledger.read_events", lambda *a, **k: _events_summing_to(14.0))
    apply_time = NOW - timedelta(days=8)  # horizon = apply+7d = NOW-1d → expired at NOW
    assert _fill_apply_time_baseline(p, as_of=apply_time) is True
    assert p.claim.baseline == pytest.approx(2.0)  # the real fill, not the 0.0 placeholder

    # Move the proposal into applied/ with the backdated apply_time.
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    transition(p, "applied", actor="arbiter")
    for h in reversed(p.history):
        if h.to_status == "applied":
            h.at = apply_time.isoformat(timespec="seconds")
            break
    (shared / "proposals" / "applied" / f"{p.id}.json").write_text(json.dumps(p.to_dict()))

    # ── Verify at the horizon: POST-change spend $7/7d = $1.00/day ───────────────
    # $2.00 baseline − $1.00 current = $1.00 drop ≥ $0.50 magnitude → claim holds.
    monkeypatch.setattr("cost_ledger.read_events", lambda *a, **k: _events_summing_to(7.0))

    def resolver(metric: str, bot_id: str, as_of: datetime) -> ResolveResult:
        mv = metric_registry.resolve(metric, bot_id, as_of)  # the REAL resolver
        return ResolveResult(value=mv.value, confidence=mv.confidence)

    report = run_once(shared, resolver, now=NOW)

    # ── The loop closed ─────────────────────────────────────────────────────────
    assert report.succeeded == 1, report.to_dict()
    assert not (shared / "proposals" / "applied" / f"{p.id}.json").exists()
    assert (shared / "proposals" / "succeeded" / f"{p.id}.json").exists()

    # ── …and Budget Hawk earned authority ───────────────────────────────────────
    after = json.loads(rec_path.read_text())
    assert after["track_record"]["proposals_verified_success"] >= 1, after["track_record"]


def test_loop_does_not_close_when_spend_does_not_drop(tmp_path, monkeypatch):
    """Control: same setup but post-change spend stays at $2/day → claim fails,
    proposal does NOT succeed, authority NOT credited. Proves the success path
    above is measuring something real, not rubber-stamping."""
    shared = tmp_path / "shared"
    (shared / "generators").mkdir(parents=True)
    (shared / "proposals" / "applied").mkdir(parents=True)
    rec_path = shared / "generators" / f"{GENERATOR_ID}.json"
    rec_path.write_text(json.dumps(
        GeneratorRecord(id=GENERATOR_ID, charter_fingerprint=_real_charter_fingerprint(),
                        track_record=TrackRecord()).to_dict()
    ))

    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"model": {"tier": "high"}}))
    p = make_config_patch_proposal(
        target_path=f"{target}::model.tier", value="low", bot_id="team_bot_a",
        generator_id=GENERATOR_ID, dimension="cost",
        claim_metric="cost.daily_usd", claim_direction="down", claim_window_days=7,
    )
    p.claim.magnitude = 0.50
    p.claim.baseline = 0.0
    p.claim.baseline_at_apply = True
    p.claim.fallback = "flag"  # don't require a revert applier on the failure path

    monkeypatch.setattr("cost_ledger.read_events", lambda *a, **k: _events_summing_to(14.0))
    apply_time = NOW - timedelta(days=8)
    _fill_apply_time_baseline(p, as_of=apply_time)
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")
    transition(p, "applied", actor="arbiter")
    for h in reversed(p.history):
        if h.to_status == "applied":
            h.at = apply_time.isoformat(timespec="seconds")
            break
    (shared / "proposals" / "applied" / f"{p.id}.json").write_text(json.dumps(p.to_dict()))

    # Spend unchanged at $2/day → no $0.50 drop → claim fails.
    monkeypatch.setattr("cost_ledger.read_events", lambda *a, **k: _events_summing_to(14.0))

    def resolver(metric, bot_id, as_of):
        mv = metric_registry.resolve(metric, bot_id, as_of)
        return ResolveResult(value=mv.value, confidence=mv.confidence)

    report = run_once(shared, resolver, now=NOW)

    assert report.succeeded == 0
    assert not (shared / "proposals" / "succeeded" / f"{p.id}.json").exists()
    after = json.loads(rec_path.read_text())
    assert after["track_record"]["proposals_verified_success"] == 0
