"""Tests for signals.session_budget_emit — convert plugin-written
session-budget breaker files into Signals.

PR C (2026-05-31) — the Python side of the session-budget cap.

The TS plugin's SessionCostMonitor writes per-session breaker files at
``{shared_dir}/breakers/<bot_id>/session-<sid>.json`` when a session's
accumulated cost crosses ``agents.defaults.sessionBudgetCapUsd``. This
module reads those files and produces observe() kwargs dicts the
cost_watchdog loop forwards to ``signals.store.observe`` so the alert
lands on the operator's Alerts page.

Tests pin the breaker-file schema we expect, the Signal shape we emit,
and the various malformed-file fail-soft paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from signals import session_budget_emit  # noqa: E402


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    (d / "breakers").mkdir(parents=True)
    return d


def _write_breaker(
    shared_dir: Path, bot_id: str, session_id: str,
    *, cap_usd: float = 1.0, cost_usd: float = 1.5,
    user_id: str | None = "U-TEST",
    channel_id: str | None = "C-TEST",
    channel_kind: str | None = "slack_dm",
    tripped_at: str | None = "2026-05-31T12:00:00Z",
    model: str | None = "anthropic/claude-sonnet-4-6",
    extra: dict | None = None,
) -> Path:
    """Write a breaker file in the shape SessionCostMonitor produces."""
    bot_dir = shared_dir / "breakers" / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)
    safe_sid = "".join(c if c.isalnum() or c in "_-" else "_" for c in session_id)
    p = bot_dir / f"session-{safe_sid[:64]}.json"
    rec = {
        "bot_id": bot_id,
        "session_id": session_id,
        "type": "session_budget",
        "state": "tripped",
        "tripped_at": tripped_at,
        "cap_usd": cap_usd,
        "cost_usd": cost_usd,
        "last_model": model,
        "last_provider": "anthropic",
        "user_id": user_id,
        "channel_id": channel_id,
        "channel_kind": channel_kind,
        "reason": (
            f"Session cost ${cost_usd:.4f} exceeded per-session cap ${cap_usd:.2f}."
        ),
    }
    if extra:
        rec.update(extra)
    p.write_text(json.dumps(rec, indent=2))
    return p


# ── collect_for_bot ─────────────────────────────────────────────────────────


def test_collect_silent_when_no_breaker_files(shared_dir: Path):
    """No breaker dir → no signals."""
    assert session_budget_emit.collect_for_bot("team-bot-a", shared_dir) == []


def test_collect_silent_when_breaker_dir_empty(shared_dir: Path):
    """Bot has the breakers dir but no session files → no signals."""
    (shared_dir / "breakers" / "team-bot-a").mkdir()
    assert session_budget_emit.collect_for_bot("team-bot-a", shared_dir) == []


def test_collect_one_signal_per_session_breaker(shared_dir: Path):
    """Each breaker file becomes one observe() kwargs dict with the
    expected fields."""
    _write_breaker(
        shared_dir, "team-bot-a", "session-abc",
        cap_usd=1.00, cost_usd=1.50,
    )
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert len(specs) == 1
    spec = specs[0]
    assert spec["producer"] == "session_cost_monitor"
    assert spec["type"] == "session_budget_exceeded"
    assert spec["scope"] == "bot"
    assert spec["bot_id"] == "team-bot-a"
    assert spec["flavor"] == "maintenance"
    assert spec["severity"] == "alert"
    # Signature includes the bot+session — independent across sessions.
    assert "team-bot-a:session-abc" in spec["signature"]
    # Details carry the breaker fields verbatim for downstream consumers.
    d = spec["details"]
    assert d["session_id"] == "session-abc"
    assert d["cap_usd"] == 1.00
    assert d["cost_usd"] == 1.50
    assert d["user_id"] == "U-TEST"
    assert d["channel_id"] == "C-TEST"
    assert d["channel_kind"] == "slack_dm"
    # Title carries cost + cap so the row in the alerts list is
    # immediately diagnostic.
    assert "1.5" in spec["title"] and "1.00" in spec["title"]


def test_collect_multiple_sessions(shared_dir: Path):
    """Multiple runaway sessions on the same bot each get their own
    Signal (no collapsing — they're independent incidents)."""
    _write_breaker(shared_dir, "team-bot-a", "s1", cost_usd=2.0)
    _write_breaker(shared_dir, "team-bot-a", "s2", cost_usd=3.0)
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert len(specs) == 2
    signatures = {s["signature"] for s in specs}
    assert len(signatures) == 2, "each session must produce a distinct signature"


def test_collect_skips_unparseable_files(shared_dir: Path):
    """A malformed JSON breaker file is skipped (treated as absent)
    — fail-soft so one bad file doesn't suppress the others."""
    bot_dir = shared_dir / "breakers" / "team-bot-a"
    bot_dir.mkdir(parents=True)
    (bot_dir / "session-bad.json").write_text("{ not valid")
    _write_breaker(shared_dir, "team-bot-a", "good")
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert len(specs) == 1
    assert specs[0]["details"]["session_id"] == "good"


def test_collect_skips_files_missing_required_fields(shared_dir: Path):
    """Records missing cap/cost/session_id are skipped — the breaker
    isn't actionable without them."""
    bot_dir = shared_dir / "breakers" / "team-bot-a"
    bot_dir.mkdir(parents=True)
    # Valid JSON but missing the required fields.
    (bot_dir / "session-malformed.json").write_text(json.dumps({
        "bot_id": "team-bot-a",
        "type": "session_budget",
    }))
    _write_breaker(shared_dir, "team-bot-a", "ok")
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert len(specs) == 1
    assert specs[0]["details"]["session_id"] == "ok"


def test_collect_skips_non_session_budget_files(shared_dir: Path):
    """The bot's breakers/ dir also holds cost.json / full.json from the
    L1 breaker — only files with type=session_budget are picked up."""
    bot_dir = shared_dir / "breakers" / "team-bot-a"
    bot_dir.mkdir(parents=True)
    # Daily L1 cost breaker — same dir, different type.
    (bot_dir / "cost.json").write_text(json.dumps({
        "bot_id": "team-bot-a",
        "type": "cost",
        "state": "tripped",
        "tripped_at": "2026-05-31T00:00:00Z",
        "expires_at": None,
        "initiated_by": "auto",
        "reason": "daily cap",
    }))
    # No matching session file — the L1 breaker is ignored by THIS module
    # because we glob session-*.json (the L1 file is cost.json).
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert specs == []


def test_collect_handles_missing_user_channel_context(shared_dir: Path):
    """user_id / channel_id may be null when the gateway didn't thread
    them — the body still renders, the details carry null."""
    _write_breaker(
        shared_dir, "team-bot-a", "anon",
        user_id=None, channel_id=None, channel_kind=None,
    )
    specs = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert len(specs) == 1
    assert specs[0]["details"]["user_id"] is None
    assert specs[0]["details"]["channel_id"] is None
    # Body uses "(unknown)" for null fields rather than crashing on None.
    assert "(unknown)" in specs[0]["body"]


def test_signature_stable_across_calls(shared_dir: Path):
    """Calling collect_for_bot twice on the same breaker file produces
    the same signature — that's what makes observe() dedup'd."""
    _write_breaker(shared_dir, "team-bot-a", "stable")
    s1 = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    s2 = session_budget_emit.collect_for_bot("team-bot-a", shared_dir)
    assert s1[0]["signature"] == s2[0]["signature"]
