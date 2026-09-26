"""Deploy half of compaction-and-memory-flush-on-cheap-rung.

``evolve_admin.compaction_housekeeping`` converges ``agents.defaults.compaction``
on every deploy: the retired ``reserveTokensFloor`` goes, a transcript-byte
budget replaces it (profile-owned, gap-filled), the memory flush always fires
below that budget (F-CE2 — no silent memory loss), and with the lever on the
compaction summariser runs on the bot's ``fast`` role with thinking off.

The plugin half (the flush's model, tool set and turn tag) is pinned in
``packages/plugin/tests/housekeepingCheapRung.test.mjs``.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import cost_profiles  # noqa: E402
from evolve_admin.compaction_housekeeping import (  # noqa: E402
    BALANCED_COMPACTION_BUDGET_BYTES,
    FLUSH_BEFORE_COMPACTION_RATIO,
    apply_compaction_housekeeping,
    apply_for_bot,
    estimate_flush_input_tokens,
    parse_byte_size,
)
from evolve_admin.deploy import _BALANCED_COST_DEFAULTS, gap_fill_cost_settings  # noqa: E402

FAST = "anthropic/claude-haiku-4-5"
NETWORK = {"bots": {"team-bot-a": {"role": "member", "port": 19001}}}


def _fast(_network: dict, _bot: str) -> str:
    return FAST


def _silent(_msg: str) -> None:
    return None


def _comp(cfg: dict) -> dict:
    return cfg["agents"]["defaults"]["compaction"]


# ── Deploy writes the block (fresh install) ───────────────────────────────


def test_fresh_deploy_writes_the_housekeeping_block() -> None:
    cfg: dict = {}
    gap_fill_cost_settings(cfg, snapshot=None)
    apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    comp = _comp(cfg)
    assert comp["model"] == FAST
    assert comp["thinkingLevel"] == "off"
    assert comp["maxActiveTranscriptBytes"] == BALANCED_COMPACTION_BUDGET_BYTES
    assert comp["memoryFlush"]["forceFlushTranscriptBytes"] < comp["maxActiveTranscriptBytes"]
    assert comp["memoryFlush"]["enabled"] is True
    assert "reserveTokensFloor" not in comp
    # memoryFlush.model deliberately unset: OC drops the session fallback
    # chain for a config-pinned flush model; the plugin routes it instead.
    assert "model" not in comp["memoryFlush"]


def test_deploy_default_carries_no_retired_floor() -> None:
    assert "reserveTokensFloor" not in _BALANCED_COST_DEFAULTS["compaction"]


# ── Migration: a block that predates this change ──────────────────────────


PRE_EXISTING = {
    "agents": {"defaults": {"compaction": {
        "mode": "safeguard",
        "reserveTokensFloor": 80000,
        "memoryFlush": {"enabled": True, "softThresholdTokens": 10000},
    }}},
}


def test_migration_converges_a_pre_existing_block() -> None:
    cfg = copy.deepcopy(PRE_EXISTING)
    changed = apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    assert changed is True
    comp = _comp(cfg)
    assert "reserveTokensFloor" not in comp
    assert comp["model"] == FAST and comp["thinkingLevel"] == "off"
    assert comp["maxActiveTranscriptBytes"] == BALANCED_COMPACTION_BUDGET_BYTES
    assert comp["memoryFlush"]["forceFlushTranscriptBytes"] == int(
        BALANCED_COMPACTION_BUDGET_BYTES * FLUSH_BEFORE_COMPACTION_RATIO
    )
    # Untouched siblings.
    assert comp["mode"] == "safeguard"
    assert comp["memoryFlush"]["softThresholdTokens"] == 10000


def test_migration_is_idempotent() -> None:
    cfg = copy.deepcopy(PRE_EXISTING)
    apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    once = copy.deepcopy(cfg)
    assert apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent) is False
    assert cfg == once


# ── Operator values and the F-CE2 guard ───────────────────────────────────


def test_operator_budget_is_never_overwritten() -> None:
    cfg = {"agents": {"defaults": {"compaction": {
        "mode": "safeguard", "maxActiveTranscriptBytes": "1mb",
        "memoryFlush": {"enabled": True},
    }}}}
    apply_compaction_housekeeping(cfg, fast_model=FAST, lever_on=True)
    comp = _comp(cfg)
    assert comp["maxActiveTranscriptBytes"] == "1mb"
    assert comp["memoryFlush"]["forceFlushTranscriptBytes"] == int(1024 * 1024 * 0.8)


def test_flush_threshold_past_the_budget_is_pulled_below_it() -> None:
    """F-CE2: a flush that fires after compaction loses that cycle's note."""
    cfg = {"agents": {"defaults": {"compaction": {
        "maxActiveTranscriptBytes": 400 * 1024,
        "memoryFlush": {"enabled": True, "forceFlushTranscriptBytes": "2mb"},
    }}}}
    apply_compaction_housekeeping(cfg, fast_model=None, lever_on=True)
    assert _comp(cfg)["memoryFlush"]["forceFlushTranscriptBytes"] < 400 * 1024


def test_operator_flush_threshold_under_the_budget_stands() -> None:
    cfg = {"agents": {"defaults": {"compaction": {
        "maxActiveTranscriptBytes": 400 * 1024,
        "memoryFlush": {"enabled": True, "forceFlushTranscriptBytes": "100kb"},
    }}}}
    apply_compaction_housekeeping(cfg, fast_model=None, lever_on=True)
    assert _comp(cfg)["memoryFlush"]["forceFlushTranscriptBytes"] == "100kb"


def test_disabled_flush_is_left_disabled() -> None:
    cfg = {"agents": {"defaults": {"compaction": {"memoryFlush": {"enabled": False}}}}}
    apply_compaction_housekeeping(cfg, fast_model=FAST, lever_on=True)
    assert _comp(cfg)["memoryFlush"] == {"enabled": False}


def test_absent_compaction_block_is_not_created() -> None:
    """A cost snapshot that deliberately removed compaction keeps it removed."""
    cfg = {"agents": {"defaults": {}}}
    assert apply_compaction_housekeeping(cfg, fast_model=FAST, lever_on=True) == []
    assert "compaction" not in cfg["agents"]["defaults"]


# ── The lever (D-CS5: default on, per-bot opt-out) ────────────────────────


def test_opt_out_removes_what_the_lever_wrote_and_nothing_else() -> None:
    cfg = copy.deepcopy(PRE_EXISTING)
    apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    opted_out = {"bots": {"team-bot-a": {"cost": {"levers": {"housekeeping_cheap_rung": False}}}}}
    apply_for_bot(cfg, opted_out, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    comp = _comp(cfg)
    assert "model" not in comp and "thinkingLevel" not in comp
    assert comp["maxActiveTranscriptBytes"] == BALANCED_COMPACTION_BUDGET_BYTES, \
        "the budget is the profile's, not the lever's"


def test_opt_out_keeps_an_operator_chosen_compaction_model() -> None:
    cfg = {"agents": {"defaults": {"compaction": {"model": "anthropic/claude-sonnet-4-6"}}}}
    opted_out = {"cost": {"levers": {"housekeeping_cheap_rung": False}}}
    apply_for_bot(cfg, opted_out, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    assert _comp(cfg)["model"] == "anthropic/claude-sonnet-4-6"


def test_resolver_failure_still_applies_the_budget() -> None:
    def boom(_n: dict, _b: str) -> str:
        raise RuntimeError("tiers unreadable")

    cfg = copy.deepcopy(PRE_EXISTING)
    apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=boom, log=_silent)
    comp = _comp(cfg)
    assert "model" not in comp, "no half-write when the fast role is unknown"
    assert comp["maxActiveTranscriptBytes"] == BALANCED_COMPACTION_BUDGET_BYTES


# ── The flush carries a budget, not the whole window ──────────────────────


def test_flush_input_is_well_under_the_full_context() -> None:
    """Before: a 1M-window model with no budget flushes at whichever OC
    trigger fires first — its 2 MiB default force-flush (~740k tokens) beats
    window − 20k − soft (~970k) — and carries all of it. After: the flush
    fires at 80% of the 400 KB budget (~150k). The finding's measured flush
    carried 194k on a 200k-window model; unbudgeted on today's 1M models it
    would be ~4× that."""
    before = estimate_flush_input_tokens(
        context_window_tokens=1_000_000, compaction={"memoryFlush": {"softThresholdTokens": 10_000}},
    )
    cfg: dict = {}
    gap_fill_cost_settings(cfg, snapshot=None)
    apply_for_bot(cfg, NETWORK, "team-bot-a", resolve_fast_model=_fast, log=_silent)
    after = estimate_flush_input_tokens(context_window_tokens=1_000_000, compaction=_comp(cfg))
    assert before > 700_000
    assert after < 160_000
    assert after / before < 0.25, (before, after)


# ── Coherence and parsing ─────────────────────────────────────────────────


def test_balanced_profile_and_deploy_default_agree_on_the_budget() -> None:
    prof = cost_profiles.BUILTIN_PROFILES["balanced"]["settings"]["compaction"]
    dep = _BALANCED_COST_DEFAULTS["compaction"]
    assert prof["maxActiveTranscriptBytes"] == dep["maxActiveTranscriptBytes"] == BALANCED_COMPACTION_BUDGET_BYTES
    assert (prof["memoryFlush"]["forceFlushTranscriptBytes"]
            == dep["memoryFlush"]["forceFlushTranscriptBytes"])


@pytest.mark.parametrize("name", sorted(cost_profiles.BUILTIN_PROFILES))
def test_no_builtin_profile_writes_the_retired_floor(name: str) -> None:
    comp = cost_profiles.BUILTIN_PROFILES[name]["settings"].get("compaction") or {}
    assert "reserveTokensFloor" not in comp
    budget = parse_byte_size(comp.get("maxActiveTranscriptBytes"))
    flush = parse_byte_size((comp.get("memoryFlush") or {}).get("forceFlushTranscriptBytes"))
    if budget and flush:
        assert flush < budget, f"{name}: flush must fire before compaction"


@pytest.mark.parametrize(("raw", "want"), [
    (409600, 409600), ("400kb", 409600), ("2mb", 2 * 1024 * 1024), ("512", 512),
    ("1.5k", 1536), ("  2MB ", 2 * 1024 * 1024),
    ("2 mb", None), ("-1", None), ("lots", None), (None, None), (True, None), (-5, None),
])
def test_parse_byte_size_matches_ocs_grammar(raw, want) -> None:
    assert parse_byte_size(raw) == want
