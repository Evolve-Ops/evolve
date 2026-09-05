"""tests/test_efficiency_hawk_phase_c.py — Phase C-5 content + gates.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

efficiency_hawk has 9 proposal factories — 7 signal-driven (signal_proposals.py)
+ 2 cost-detector (proposals.py). This file pins:

  1. Each factory populates summary + explanation + a tier-correct
     action_label / manual_path / manual_instruction.
  2. Distinct dismiss signatures per finding kind, with per-resource
     granularity for the three resource-keyed ones (cron_wakes:<cron_id>,
     cron_overactive:<cron_id>, context_bloat:<filename>).
  3. observe()'s suppression gate honors the signatures.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402

sp = importlib.import_module("generators.efficiency_hawk.signal_proposals")
cp = importlib.import_module("generators.efficiency_hawk.proposals")
obs_mod = importlib.import_module("generators.efficiency_hawk.observe")


# ─────────────────────────────────────────────────────────────────────────────
# Signal factory inputs (mirror the existing test fixtures)
# ─────────────────────────────────────────────────────────────────────────────


def _daily_spend(bot_id="team_bot_a"):
    return {
        "id": f"sig-ds-{bot_id}",
        "bot_id": bot_id,
        "severity": "warn",
        "details": {
            "cost_usd": 12.50,
            "threshold_usd": 10.0,
            "event_count": 42,
            "date": "2026-06-04",
        },
    }


def _automation(bot_id="team_bot_a"):
    return {
        "id": f"sig-auto-{bot_id}",
        "bot_id": bot_id,
        "details": {
            "automation_count": 90,
            "user_turn_count": 10,
            "automation_ratio": 0.9,
            "window_days": 3,
            "top_automation_kinds": {"heartbeat": 60, "cron:nightly": 20},
        },
    }


def _cron_wakes(bot_id="team_bot_a", cron_id="j-morning"):
    return {
        "id": f"sig-cw-{cron_id}",
        "bot_id": bot_id,
        "details": {
            "cron_id": cron_id,
            "cron_name": "morning_brief",
            "cadence": "5min",
            "session_target": "main",
            "shell": "echo hi",
        },
    }


def _cron_overactive(bot_id="team_bot_a", cron_id="j-overactive"):
    return {
        "id": f"sig-co-{cron_id}",
        "bot_id": bot_id,
        "details": {
            "cron_id": cron_id,
            "cron_name": "overactive_cron",
            "actual_fires": 41,
            "expected_fires": 24,
            "window_hours": 24,
            "every_ms": 3_600_000,
        },
    }


def _context_bloat(bot_id="team_bot_a", filename="memory.md"):
    return {
        "id": f"sig-cb-{filename}",
        "bot_id": bot_id,
        "details": {
            "filename": filename,
            "size_kb": 80.0,
            "threshold_kb": 50.0,
        },
    }


def _session_outlier(bot_id="team_bot_a"):
    return {
        "id": "sig-so-1",
        "bot_id": bot_id,
        "details": {
            "session_id": "abc123def456",
            "cost_usd": 5.0,
            "median_session_cost_usd": 1.0,
            "ratio": 5.0,
            "event_count": 10,
            "trigger_kinds": ["user_turn"],
            "first_ts": "2026-06-04T10:00Z",
            "last_ts": "2026-06-04T10:30Z",
        },
    }


def _heartbeat_no_override(bot_id="team_bot_a"):
    return {
        "id": "sig-hb-1",
        "bot_id": bot_id,
        "details": {
            "primary_model": "anthropic/claude-sonnet-4-6",
            "heartbeat_every": "5min",
            "light_context": False,
            "isolated_session": False,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-factory content
# ─────────────────────────────────────────────────────────────────────────────


class TestSignalFactoriesContent:
    @pytest.mark.parametrize("factory,sig,expected_sig_const", [
        (sp.make_daily_spend_proposal, _daily_spend(), sp.DISMISS_SIG_DAILY_SPEND_HIGH),
        (sp.make_automation_dominance_proposal, _automation(), sp.DISMISS_SIG_AUTOMATION_DOMINANCE),
        (sp.make_session_token_outlier_proposal, _session_outlier(), sp.DISMISS_SIG_SESSION_TOKEN_OUTLIER),
        (sp.make_heartbeat_no_model_override_proposal, _heartbeat_no_override(), sp.DISMISS_SIG_HEARTBEAT_NO_OVERRIDE),
    ])
    def test_bot_scoped_factories_populate_content_and_signature(
        self, factory, sig, expected_sig_const,
    ):
        p = factory(sig)
        assert p.summary
        assert len(p.summary) <= 400
        assert p.explanation
        assert len(p.explanation) <= 1500
        assert p.dismiss_signature == expected_sig_const
        assert p.dismiss_scope == "kind"

    def test_cron_wakes_has_per_cron_signature(self):
        a = sp.make_cron_wakes_agent_proposal(_cron_wakes(cron_id="j-a"))
        b = sp.make_cron_wakes_agent_proposal(_cron_wakes(cron_id="j-b"))
        assert a.dismiss_signature != b.dismiss_signature
        assert a.dismiss_signature.endswith(":j-a")

    def test_cron_overactive_has_per_cron_signature(self):
        a = sp.make_cron_overactive_proposal(_cron_overactive(cron_id="j-x"))
        b = sp.make_cron_overactive_proposal(_cron_overactive(cron_id="j-y"))
        assert a.dismiss_signature != b.dismiss_signature
        assert "j-x" in a.dismiss_signature

    def test_context_bloat_has_per_file_signature(self):
        a = sp.make_context_bloat_proposal(_context_bloat(filename="memory.md"))
        b = sp.make_context_bloat_proposal(_context_bloat(filename="AGENTS.md"))
        assert a.dismiss_signature != b.dismiss_signature
        assert "memory.md" in a.dismiss_signature
        # Heartbeat-prefixed files get heartbeat-specific instruction text.
        hb = sp.make_context_bloat_proposal(_context_bloat(filename="heartbeats.md"))
        assert "rotat" in hb.manual_instruction.lower() or (
            "trim" in hb.manual_instruction.lower()
        )


class TestCostDetectorFactoriesContent:
    def test_background_dominance_populates_content(self):
        p = cp.make_background_dominance(
            bot_id="team_bot_a",
            background_share=0.85,
            background_usd=8.5,
            classified_usd=10.0,
            total_usd=10.0,
            top_kinds=[("heartbeat", 6.0), ("cron", 2.0)],
            lookback_days=7,
        )
        assert p.summary
        assert p.explanation
        assert p.action_label == "Open Cost tab"
        assert "team_bot_a" in p.manual_path
        assert p.dismiss_signature == cp.DISMISS_SIG_BACKGROUND_DOMINANCE

    def test_tier_misrouting_populates_content_and_tier_1_action(self):
        p = cp.make_tier_misrouting(
            bot_id="team_bot_a",
            high_tier_share=0.75,
            high_tier_cost_usd=7.5,
            classified_maintenance_cost_usd=10.0,
            maintenance_session_count=30,
            high_tier_models=[("claude-sonnet-4-6", 7.5)],
            lookback_days=7,
            new_tier="haiku",
        )
        assert p.summary
        assert p.explanation
        assert p.action_label == "Route maintenance to haiku"
        assert len(p.action_label) <= 30
        assert p.dismiss_signature == cp.DISMISS_SIG_TIER_MISROUTING

    def test_signatures_distinct_from_signal_driven_factories(self):
        """The two cost-detector signatures must NOT collide with the
        seven signal-driven ones."""
        all_sigs = {
            sp.DISMISS_SIG_DAILY_SPEND_HIGH,
            sp.DISMISS_SIG_AUTOMATION_DOMINANCE,
            sp.DISMISS_SIG_SESSION_TOKEN_OUTLIER,
            sp.DISMISS_SIG_HEARTBEAT_NO_OVERRIDE,
            cp.DISMISS_SIG_BACKGROUND_DOMINANCE,
            cp.DISMISS_SIG_TIER_MISROUTING,
        }
        assert len(all_sigs) == 6  # all pairwise distinct


# ─────────────────────────────────────────────────────────────────────────────
# Voice rules — sample across factories
# ─────────────────────────────────────────────────────────────────────────────


class TestVoiceRules:
    """Length budgets + trade-off check across the high-traffic factories."""

    def test_explanation_names_trade_off_for_daily_spend(self):
        p = sp.make_daily_spend_proposal(_daily_spend())
        text = p.explanation.lower()
        assert any(
            phrase in text
            for phrase in ("what could go wrong", "trade-off", "risk")
        )

    def test_explanation_names_trade_off_for_tier_misrouting(self):
        p = cp.make_tier_misrouting(
            bot_id="team_bot_a",
            high_tier_share=0.75,
            high_tier_cost_usd=7.5,
            classified_maintenance_cost_usd=10.0,
            maintenance_session_count=30,
            high_tier_models=[("claude-sonnet-4-6", 7.5)],
            lookback_days=7,
        )
        text = p.explanation.lower()
        assert any(
            phrase in text
            for phrase in ("what could go wrong", "trade-off", "risk")
        )


# ─────────────────────────────────────────────────────────────────────────────
# observe() suppression gate
# ─────────────────────────────────────────────────────────────────────────────


def _signals_store():
    return importlib.import_module("signals.store")


def _make_signature(producer, sig_type, bot_id):
    from schema.signal import make_signature
    return make_signature(producer, sig_type, bot_id)


def _write_signal(shared_dir: Path, sig_type: str, bot_id: str, details: dict):
    return _signals_store().observe(
        shared_dir,
        signature=_make_signature("cost_watchdog", sig_type, bot_id),
        producer="cost_watchdog",
        type=sig_type,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {sig_type}",
        details=details,
    )


def _ctx(tmp_path, bot_id="team_bot_a"):
    """Minimal EfficiencyHawkContext that exercises _observe_from_signals."""
    from observations.access import window as obs_window

    return obs_mod.EfficiencyHawkContext(
        bot_id=bot_id,
        window=obs_window(bot_id, days=1, shared_dir=tmp_path),
        shared_dir=tmp_path,
    )


class TestObserveSuppression:
    def test_emits_when_no_dismiss(self, tmp_path):
        _write_signal(
            tmp_path, "daily_spend_high", "team_bot_a",
            _daily_spend()["details"],
        )
        out = obs_mod._observe_from_signals(_ctx(tmp_path))
        sigs = [p.dismiss_signature for p in out]
        assert sp.DISMISS_SIG_DAILY_SPEND_HIGH in sigs

    def test_dismissed_signature_suppresses_emission(self, tmp_path):
        _write_signal(
            tmp_path, "daily_spend_high", "team_bot_a",
            _daily_spend()["details"],
        )
        dismissals.record_dismissal(
            tmp_path,
            signature=sp.DISMISS_SIG_DAILY_SPEND_HIGH,
            bot_id="team_bot_a",
            scope="kind",
        )
        out = obs_mod._observe_from_signals(_ctx(tmp_path))
        sigs = [p.dismiss_signature for p in out]
        assert sp.DISMISS_SIG_DAILY_SPEND_HIGH not in sigs

    def test_per_cron_suppression_does_not_leak(self, tmp_path):
        """Dismiss j-morning's cron_wakes finding. j-evening's
        cron_wakes finding should still surface."""
        # Two distinct cron_wakes signals on the same bot.
        for cron_id in ("j-morning", "j-evening"):
            _signals_store().observe(
                tmp_path,
                signature=f"permission_monitor:cron_wakes_agent:team_bot_a:{cron_id}",
                producer="cost_watchdog",
                type="cron_wakes_agent",
                flavor="maintenance",
                severity="warn",
                scope="bot",
                bot_id="team_bot_a",
                title=f"team_bot_a: cron_wakes ({cron_id})",
                details=_cron_wakes(cron_id=cron_id)["details"],
            )
        dismissals.record_dismissal(
            tmp_path,
            signature=sp.dismiss_signature_for_cron_wakes("j-morning"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = obs_mod._observe_from_signals(_ctx(tmp_path))
        sigs = [p.dismiss_signature for p in out]
        assert sp.dismiss_signature_for_cron_wakes("j-morning") not in sigs
        assert sp.dismiss_signature_for_cron_wakes("j-evening") in sigs

    def test_pod_wide_dismiss_applies_to_every_bot(self, tmp_path):
        _write_signal(
            tmp_path, "daily_spend_high", "team_bot_a",
            _daily_spend()["details"],
        )
        dismissals.record_dismissal(
            tmp_path,
            signature=sp.DISMISS_SIG_DAILY_SPEND_HIGH,
            bot_id=None,
            scope="kind",
        )
        out = obs_mod._observe_from_signals(_ctx(tmp_path))
        sigs = [p.dismiss_signature for p in out]
        assert sp.DISMISS_SIG_DAILY_SPEND_HIGH not in sigs
