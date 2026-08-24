"""tests/test_phase_c2_batch.py — Phase C-2 batch.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

Pins the operator-first content fields and dismiss-signature
suppression for the surviving Phase C-2 generators:

  - cost_spike            — Tier 2 (UI manual, Cost tab)
  - cron_caps_filler      — Tier 1 (auto-apply caps); per-job signature

(``plugin_curator`` was the third generator in the original batch but
was retired by internal/spec-plugin-posture-rework-2026-06-06.md; its
tests were removed alongside the generator's observe() going inert.)

Two things per generator:
  1. Each emitted proposal carries summary + explanation + the right
     tier-specific action fields + a stable dismiss_signature.
  2. observe() respects dismissals.is_suppressed at the signature's
     granularity (per-bot, per-job, per-plugin as appropriate).

The voice-rule lint is light here — we already exercise the spec's
length-budget + trade-off checks for cache_ttl_tuner and
bloat_investigator. This file focuses on per-generator wiring +
suppression granularity.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# cost_spike
# ─────────────────────────────────────────────────────────────────────────────


cost_spike_observe = importlib.import_module("generators.cost_spike.observe")
cost_spike_proposals = importlib.import_module("generators.cost_spike.signal_proposals")


def _cost_spike_details() -> dict:
    return {
        "cost_cur_usd": 12.50,
        "cost_prior_usd": 3.10,
        "ratio": 4.03,
        "multiplier_threshold": 2.0,
        "floor_usd": 5.0,
        "window_days": 7,
    }


def _write_cost_spike_signal(shared_dir: Path, bot_id: str) -> str:
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("cost_watchdog", "cost_spike", bot_id),
        producer="cost_watchdog",
        type="cost_spike",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: cost_spike",
        details=_cost_spike_details(),
    )
    return sig.id


class TestCostSpikeContent:
    def test_factory_populates_content_fields(self):
        sig = {
            "id": "sig-1",
            "bot_id": "team_bot_a",
            "details": _cost_spike_details(),
        }
        p = cost_spike_proposals.make_cost_spike_proposal(sig)
        assert p.summary
        assert len(p.summary) <= 400
        assert p.explanation
        assert len(p.explanation) <= 1500
        assert p.action_label == "Open Cost tab"
        assert "team_bot_a" in p.manual_path
        assert p.dismiss_signature == cost_spike_proposals.DISMISS_SIG_COST_SPIKE
        assert p.dismiss_scope == "kind"

    def test_explanation_names_trade_off(self):
        sig = {"id": "x", "bot_id": "team_bot_a", "details": _cost_spike_details()}
        p = cost_spike_proposals.make_cost_spike_proposal(sig)
        text = p.explanation.lower()
        assert any(
            phrase in text
            for phrase in ("what could go wrong", "trade-off", "risk")
        )


class TestCostSpikeSuppression:
    def test_observe_emits_when_no_dismiss(self, tmp_path):
        _write_cost_spike_signal(tmp_path, "team_bot_a")
        out = cost_spike_observe.observe(
            cost_spike_observe.CostSpikeContext(
                bot_id="team_bot_a", shared_dir=tmp_path,
            )
        )
        assert len(out) == 1

    def test_observe_suppresses_when_dismissed(self, tmp_path):
        _write_cost_spike_signal(tmp_path, "team_bot_a")
        dismissals.record_dismissal(
            tmp_path,
            signature=cost_spike_proposals.DISMISS_SIG_COST_SPIKE,
            bot_id="team_bot_a",
            scope="kind",
        )
        out = cost_spike_observe.observe(
            cost_spike_observe.CostSpikeContext(
                bot_id="team_bot_a", shared_dir=tmp_path,
            )
        )
        assert out == []

    def test_consult_dismissals_false_bypasses_gate(self, tmp_path):
        _write_cost_spike_signal(tmp_path, "team_bot_a")
        dismissals.record_dismissal(
            tmp_path,
            signature=cost_spike_proposals.DISMISS_SIG_COST_SPIKE,
            bot_id="team_bot_a",
            scope="kind",
        )
        out = cost_spike_observe.observe(
            cost_spike_observe.CostSpikeContext(
                bot_id="team_bot_a",
                shared_dir=tmp_path,
                consult_dismissals=False,
            )
        )
        assert len(out) == 1


# ─────────────────────────────────────────────────────────────────────────────
# cron_caps_filler
# ─────────────────────────────────────────────────────────────────────────────


cron_caps_observe = importlib.import_module("generators.cron_caps_filler.observe")
cron_caps_proposals = importlib.import_module(
    "generators.cron_caps_filler.signal_proposals",
)


def _uncapped_signal(bot_id: str, job_id: str) -> dict:
    return {
        "id": f"sig-{job_id}",
        "bot_id": bot_id,
        "type": "perm_cron_uncapped_agent_turn",
        "details": {"bot_id": bot_id, "job_id": job_id, "name": "morning_brief"},
    }


def _cron_job(job_id: str, name: str = "morning_brief") -> dict:
    return {
        "id": job_id,
        "name": name,
        "kind": "agentTurn",
        "schedule": "0 7 * * *",
        "payload": {"prompt": "hi"},
    }


class TestCronCapsContent:
    def test_factory_populates_content_fields(self):
        sig = _uncapped_signal("team_bot_a", "j-morning")
        job = _cron_job("j-morning")
        p = cron_caps_proposals.make_uncapped_cron_proposal(sig, job=job)
        assert p.summary
        assert len(p.summary) <= 400
        assert p.explanation
        assert len(p.explanation) <= 1500
        assert p.action_label == "Add the standard caps"
        assert "morning_brief" in p.manual_path
        # Per-job signature so dismissing one job doesn't suppress others.
        assert p.dismiss_signature == (
            "cron_caps_filler:uncapped_agent_turn:j-morning"
        )
        assert p.dismiss_scope == "kind"

    def test_different_jobs_get_different_signatures(self):
        sig_a = _uncapped_signal("team_bot_a", "j-a")
        sig_b = _uncapped_signal("team_bot_a", "j-b")
        pa = cron_caps_proposals.make_uncapped_cron_proposal(
            sig_a, job=_cron_job("j-a"),
        )
        pb = cron_caps_proposals.make_uncapped_cron_proposal(
            sig_b, job=_cron_job("j-b"),
        )
        assert pa.dismiss_signature != pb.dismiss_signature


def _write_uncapped_signal(shared_dir: Path, bot_id: str, job_id: str) -> str:
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature(
            "permission_monitor", "perm_cron_uncapped_agent_turn", bot_id,
        ),
        producer="permission_monitor",
        type="perm_cron_uncapped_agent_turn",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: uncapped cron",
        details={"bot_id": bot_id, "job_id": job_id, "name": "morning_brief"},
    )
    return sig.id


@pytest.fixture
def cron_jobs_stub(monkeypatch):
    """Inject a jobs list into the observe path so we don't need to
    write to a real /Users/<bot>/.openclaw/cron path."""
    state = {"jobs": [_cron_job("j-morning")]}
    monkeypatch.setattr(
        cron_caps_observe, "_read_jobs_for_bot",
        lambda bot_id, home_override: list(state["jobs"]),
    )
    return state


class TestCronCapsSuppression:
    def test_emits_when_no_dismiss(self, tmp_path, cron_jobs_stub):
        _write_uncapped_signal(tmp_path, "team_bot_a", "j-morning")
        out = cron_caps_observe.observe(
            cron_caps_observe.CronCapsFillerContext(
                bot_id="team_bot_a", shared_dir=tmp_path,
            )
        )
        assert len(out) == 1

    def test_per_job_suppression_does_not_leak_to_other_jobs(
        self, tmp_path, cron_jobs_stub,
    ):
        """Dismiss caps on j-morning. Then add j-evening (a new
        uncapped job). The new job should still surface."""
        # Initial dismiss for j-morning.
        dismissals.record_dismissal(
            tmp_path,
            signature=cron_caps_proposals.dismiss_signature_for_job("j-morning"),
            bot_id="team_bot_a",
            scope="kind",
        )
        # Add a different uncapped job.
        cron_jobs_stub["jobs"].append(_cron_job("j-evening", name="evening_brief"))
        # Both signals firing — observe should emit only for j-evening.
        _write_uncapped_signal(tmp_path, "team_bot_a", "j-morning")
        # For tests, signature uniqueness comes from the producer/type/bot
        # composite, but we need two distinct *signal entries* so the
        # observer sees both job_ids; emit a second signal with a
        # different signature to bypass dedup.
        # The store de-dups by signature, so we override
        # via dict-style observe with a hand-crafted detail.
        sig2 = signals_store.observe(
            tmp_path,
            signature="permission_monitor:perm_cron_uncapped_agent_turn:team_bot_a:evening",
            producer="permission_monitor",
            type="perm_cron_uncapped_agent_turn",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id="team_bot_a",
            title="team_bot_a: uncapped cron (evening)",
            details={
                "bot_id": "team_bot_a", "job_id": "j-evening",
                "name": "evening_brief",
            },
        )
        out = cron_caps_observe.observe(
            cron_caps_observe.CronCapsFillerContext(
                bot_id="team_bot_a", shared_dir=tmp_path,
            )
        )
        # j-morning is suppressed; j-evening emits.
        job_ids = [
            p.action.job["id"] for p in out
            if hasattr(p.action, "job")
        ]
        assert job_ids == ["j-evening"]
