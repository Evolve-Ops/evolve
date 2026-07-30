"""tests/test_bloat_investigator_phase_c.py — Phase C-1 content + gates.

Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md.

Pin three things the Phase C-1 migration owes the protocol:

1. **Operator-first content per cause_key.** Each of the four
   attribution outcomes (growing_memory, static_bloat,
   efficiency_drift, ambiguous) populates ``summary`` + ``explanation``
   + the matching action-tier fields.
2. **Tier mapping.** File-specific causes (growing, static) hand off
   via Tier 5 paste-to-bot instructions; non-file causes (efficiency
   drift, ambiguous) route to Tier 2 UI manual.
3. **Dismiss-signature suppression.** ``observe()`` skips emission
   when a matching dismiss entry is active for this bot (or pod-wide),
   keyed per cause_key so dismissing "memory bloat" doesn't suppress
   "static bloat" for the same bot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_ANALYZER_DIR = _THIS_FILE.parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import dismissals  # noqa: E402
from generators.bloat_investigator.observe import (  # noqa: E402
    BloatInvestigatorContext,
    _dismiss_signature_for,
    _phase_b_content_for,
    observe,
)
from investigation.toolkit import CorrelatedSignal, FileSize  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _csig(sig_id: str, sig_type: str, details: dict | None = None) -> CorrelatedSignal:
    return CorrelatedSignal(
        signal_id=sig_id,
        type=sig_type,
        producer="cost_watchdog",
        severity="warn",
        title=f"{sig_type} fixture",
        signature=f"team_bot_a/{sig_type}",
        details=details or {},
    )


def _growing_memory_signals() -> list[CorrelatedSignal]:
    return [
        _csig(
            "g1", "workspace_growth",
            details={
                "filename": "memory/2026-05-02.md",
                "growth_kb_per_day": 12.0,
                "current_kb": 196.0,
            },
        ),
        _csig(
            "g2", "cache_envelope_growth",
            details={"ratio": 5.0, "cur_tokens_per_call": 25_000},
        ),
        _csig(
            "g3", "efficiency_drift",
            details={"tier": "low", "ratio": 14.0},
        ),
    ]


def _static_bloat_signals() -> list[CorrelatedSignal]:
    return [
        _csig(
            "s1", "context_bloat",
            details={"filename": "BIG.md", "size_kb": 200.0},
        ),
        _csig(
            "s2", "cache_envelope_growth",
            details={"ratio": 5.0, "cur_tokens_per_call": 25_000},
        ),
    ]


def _efficiency_drift_only_signals() -> list[CorrelatedSignal]:
    return [
        _csig(
            "e1", "efficiency_drift",
            details={"tier": "low", "ratio": 14.0},
        ),
    ]


@pytest.fixture
def patched_observe(monkeypatch):
    """Patch the I/O functions correlated_signals + file_top_contributors
    so tests can inject fixture data deterministically."""
    obs_mod = sys.modules["generators.bloat_investigator.observe"]

    state = {
        "signals": [],
        "top_files": [FileSize(path="memory/2026-05-02.md", size_bytes=196_000)],
    }

    monkeypatch.setattr(
        obs_mod, "correlated_signals", lambda *a, **kw: list(state["signals"]),
    )
    monkeypatch.setattr(
        obs_mod, "file_top_contributors", lambda *a, **kw: list(state["top_files"]),
    )
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Per-cause content fields
# ─────────────────────────────────────────────────────────────────────────────


class TestContentPerCause:
    """_phase_b_content_for must return the right tier + voice for each cause."""

    def test_growing_memory_returns_tier_5_paste_to_bot(self):
        content = _phase_b_content_for(
            bot_id="team_bot_a",
            cause_key="growing_memory_drives_envelope",
            top_files=[("memory/2026-05-02.md", 196_000)],
        )
        assert content["summary"]
        assert content["explanation"]
        # Tier 5 — Investigation default button, no manual_path needed,
        # paste-to-bot instruction populated.
        assert content["action_label"] is None
        assert content["manual_instruction"]
        assert "audit" in content["manual_instruction"].lower()

    def test_static_bloat_returns_tier_5_paste_to_bot(self):
        content = _phase_b_content_for(
            bot_id="team_bot_a",
            cause_key="static_bloat_drives_envelope",
            top_files=[("BIG.md", 200_000)],
        )
        assert content["summary"]
        assert content["action_label"] is None
        assert content["manual_instruction"]
        # Specific to static bloat: asks the bot what it uses the file for.
        assert "use it for" in content["manual_instruction"].lower()

    def test_efficiency_drift_returns_tier_2_ui_manual(self):
        content = _phase_b_content_for(
            bot_id="team_bot_a",
            cause_key="efficiency_drift_without_envelope",
            top_files=[],
        )
        assert content["summary"]
        assert content["action_label"] == "Open Cost Optimization"
        assert content["manual_path"] == "Cost Optimization → team_bot_a"
        # No paste-to-bot — the operator owns the model decision.
        assert content["manual_instruction"] is None

    def test_ambiguous_returns_tier_2_ui_manual(self):
        content = _phase_b_content_for(
            bot_id="team_bot_a",
            cause_key="ambiguous",
            top_files=[],
        )
        assert content["summary"]
        assert content["action_label"] == "Open Cost Optimization"
        assert content["manual_path"] == "Cost Optimization → team_bot_a"
        assert content["manual_instruction"] is None


class TestVoiceRules:
    """Each cause's content must obey the spec's voice rules."""

    @pytest.mark.parametrize(
        "cause_key,top_files",
        [
            ("growing_memory_drives_envelope", [("memory/x.md", 100_000)]),
            ("static_bloat_drives_envelope", [("BIG.md", 200_000)]),
            ("efficiency_drift_without_envelope", []),
            ("ambiguous", []),
        ],
    )
    def test_summary_within_length_budget(self, cause_key, top_files):
        c = _phase_b_content_for(
            bot_id="team_bot_a", cause_key=cause_key, top_files=top_files,
        )
        assert len(c["summary"]) <= 400, (
            f"{cause_key} summary {len(c['summary'])} > 400 char budget"
        )

    @pytest.mark.parametrize(
        "cause_key,top_files",
        [
            ("growing_memory_drives_envelope", [("memory/x.md", 100_000)]),
            ("static_bloat_drives_envelope", [("BIG.md", 200_000)]),
            ("efficiency_drift_without_envelope", []),
            ("ambiguous", []),
        ],
    )
    def test_explanation_within_length_budget(self, cause_key, top_files):
        c = _phase_b_content_for(
            bot_id="team_bot_a", cause_key=cause_key, top_files=top_files,
        )
        assert len(c["explanation"]) <= 1500, (
            f"{cause_key} explanation {len(c['explanation'])} > 1500 char budget"
        )

    @pytest.mark.parametrize(
        "cause_key,top_files",
        [
            ("growing_memory_drives_envelope", [("memory/x.md", 100_000)]),
            ("static_bloat_drives_envelope", [("BIG.md", 200_000)]),
            ("efficiency_drift_without_envelope", []),
            ("ambiguous", []),
        ],
    )
    def test_explanation_names_trade_off(self, cause_key, top_files):
        """Voice rule #6 — explanation closes with what could go wrong."""
        c = _phase_b_content_for(
            bot_id="team_bot_a", cause_key=cause_key, top_files=top_files,
        )
        text = c["explanation"].lower()
        assert any(
            phrase in text
            for phrase in ("what could go wrong", "trade-off", "downside", "risk")
        ), f"{cause_key} explanation does not name a trade-off"

    def test_summary_no_field_path_slugs(self):
        """Voice rule #2 — no snake_case slugs that look like field paths
        or root-cause keys. Bare bot_id with an underscore is fine, so
        we mask the bot_id before scanning (otherwise valid operational
        names like ``team_bot_a`` trip the regex)."""
        import re

        bot_id = "team_bot_a"
        slug_re = re.compile(r"\b[a-z]+_[a-z]+_[a-z]+\b")
        for cause_key in (
            "growing_memory_drives_envelope",
            "static_bloat_drives_envelope",
            "efficiency_drift_without_envelope",
            "ambiguous",
        ):
            c = _phase_b_content_for(
                bot_id=bot_id, cause_key=cause_key, top_files=[],
            )
            scrubbed = c["summary"].replace(bot_id, "<bot>")
            leaks = slug_re.findall(scrubbed)
            assert not leaks, (
                f"{cause_key} summary leaks slug-shaped tokens: {leaks}"
            )

    def test_action_label_within_budget(self):
        for cause_key in (
            "efficiency_drift_without_envelope",
            "ambiguous",
        ):
            c = _phase_b_content_for(
                bot_id="team_bot_a", cause_key=cause_key, top_files=[],
            )
            assert len(c["action_label"]) <= 30


# ─────────────────────────────────────────────────────────────────────────────
# observe() integration — proposals carry content
# ─────────────────────────────────────────────────────────────────────────────


class TestObserveCarriesContent:
    def test_growing_memory_proposal_carries_content_and_signature(
        self, patched_observe, tmp_path,
    ):
        patched_observe["signals"] = _growing_memory_signals()
        out = observe(BloatInvestigatorContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        assert len(out) == 1
        p = out[0]
        assert p.summary
        assert p.explanation
        assert p.dismiss_signature == "bloat_investigator:growing_memory_drives_envelope"
        assert p.dismiss_scope == "kind"
        assert p.manual_instruction
        # Tier 5: no action_label override.
        assert p.action_label is None


# ─────────────────────────────────────────────────────────────────────────────
# Dismiss-signature suppression gate
# ─────────────────────────────────────────────────────────────────────────────


class TestDismissSuppressionGate:
    def test_emits_when_no_suppression(self, patched_observe, tmp_path):
        patched_observe["signals"] = _growing_memory_signals()
        out = observe(BloatInvestigatorContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        assert len(out) == 1

    def test_suppresses_when_matching_dismiss_active(
        self, patched_observe, tmp_path,
    ):
        patched_observe["signals"] = _growing_memory_signals()
        dismissals.record_dismissal(
            tmp_path,
            signature=_dismiss_signature_for("growing_memory_drives_envelope"),
            bot_id="team_bot_a",
            scope="kind",
            rationale="declined for now",
        )
        out = observe(BloatInvestigatorContext(
            bot_id="team_bot_a", shared_dir=tmp_path,
        ))
        assert out == []

    def test_suppression_is_per_cause_not_pod_wide_generator(
        self, patched_observe, tmp_path,
    ):
        """Dismissing 'memory bloat' must NOT suppress 'static bloat'
        — the two are distinct findings with distinct signatures even
        though they share the same generator."""
        patched_observe["signals"] = _static_bloat_signals()
        patched_observe["top_files"] = [
            FileSize(path="BIG.md", size_bytes=200_000),
        ]
        dismissals.record_dismissal(
            tmp_path,
            signature=_dismiss_signature_for("growing_memory_drives_envelope"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = observe(BloatInvestigatorContext(
            bot_id="team_bot_a",
            shared_dir=tmp_path,
            consult_proposal_history=False,  # static_bloat needs no history check
        ))
        assert len(out) == 1
        # And the new finding correctly identifies as static_bloat.
        rca = out[0].provenance.signals["root_cause_attribution"]
        assert rca["cause_key"] == "static_bloat_drives_envelope"

    def test_suppression_is_per_bot(self, patched_observe, tmp_path):
        """Dismissing for team_bot_a doesn't affect ellie."""
        patched_observe["signals"] = _growing_memory_signals()
        dismissals.record_dismissal(
            tmp_path,
            signature=_dismiss_signature_for("growing_memory_drives_envelope"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = observe(BloatInvestigatorContext(
            bot_id="ellie", shared_dir=tmp_path,
        ))
        assert len(out) == 1

    def test_pod_wide_suppression_applies_to_every_bot(
        self, patched_observe, tmp_path,
    ):
        patched_observe["signals"] = _growing_memory_signals()
        dismissals.record_dismissal(
            tmp_path,
            signature=_dismiss_signature_for("growing_memory_drives_envelope"),
            bot_id=None,
            scope="kind",
        )
        out = observe(BloatInvestigatorContext(
            bot_id="ellie", shared_dir=tmp_path,
        ))
        assert out == []

    def test_consult_dismissals_false_bypasses_gate(
        self, patched_observe, tmp_path,
    ):
        patched_observe["signals"] = _growing_memory_signals()
        dismissals.record_dismissal(
            tmp_path,
            signature=_dismiss_signature_for("growing_memory_drives_envelope"),
            bot_id="team_bot_a",
            scope="kind",
        )
        out = observe(BloatInvestigatorContext(
            bot_id="team_bot_a",
            shared_dir=tmp_path,
            consult_dismissals=False,
        ))
        assert len(out) == 1
