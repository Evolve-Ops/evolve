"""tests/test_evo_mute.py — `evo mute` / `evo unmute` handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _write_real_signal(
    shared: Path, *, sig_id: str, state: str = "firing",
    title: str = "Test signal", scope: str = "pod",
    bot_id: str | None = None,
    snoozed_until: str | None = None,
) -> None:
    """Write a Signal JSON the analyzer's store can round-trip."""
    from signals.store import subdir_for_state, signals_root
    d = signals_root(shared) / subdir_for_state(state)
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "id": sig_id,
        "signature": f"test:{sig_id}",
        "producer": "test",
        "type": "test_type",
        "flavor": "maintenance",
        "severity": "warn",
        "scope": scope,
        "bot_id": bot_id,
        "title": title,
        "body": "",
        "details": {},
        "state": state,
        "created_at": "2026-05-13T00:00:00+00:00",
        "last_observed_at": "2026-05-13T00:00:00+00:00",
        "observation_count": 1,
        "snoozed_until": snoozed_until,
        "resolved_at": None,
        "state_history": [],
        "motivated_proposals": [],
        "config_hint": None,
        "remediation": None,
    }
    (d / f"{sig_id}.json").write_text(json.dumps(body))


def _call(action: str, tmp_path: Path, bot_id: str, args: str):
    if action == "mute":
        from evolve_admin.evo.handlers.mute import render_mute as fn
    else:
        from evolve_admin.evo.handlers.mute import render_unmute as fn
    network = {"sharedDir": str(tmp_path), "members": [bot_id]}
    return fn(role="primary", bot_id=bot_id, args=args, network=network)


# ─────────────────────────────────────────────────────────────────────────────
# Usage / arg parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_mute_no_args_shows_usage(tmp_path):
    r = _call("mute", tmp_path, "admin_bot", "")
    body = r.direct_send_message or ""
    assert "Usage" in body
    assert "evo alerts" in body


def test_unmute_no_args_shows_usage(tmp_path):
    r = _call("unmute", tmp_path, "admin_bot", "")
    body = r.direct_send_message or ""
    assert "Usage" in body


def test_mute_invalid_duration(tmp_path):
    _write_real_signal(tmp_path, sig_id="abc12345", scope="pod")
    r = _call("mute", tmp_path, "admin_bot", "abc12345 bogus")
    body = r.direct_send_message or ""
    assert "isn't a duration I understand" in body


def test_mute_unknown_id(tmp_path):
    r = _call("mute", tmp_path, "admin_bot", "nope12345")
    body = r.direct_send_message or ""
    assert "No signal" in body and "nope12345" in body


# ─────────────────────────────────────────────────────────────────────────────
# Mute happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_mute_firing_signal_transitions_to_snoozed(tmp_path):
    _write_real_signal(tmp_path, sig_id="aabb1234", title="Cost spike",
                       scope="pod")
    r = _call("mute", tmp_path, "admin_bot", "aabb1234")
    body = r.direct_send_message or ""
    assert "Muted" in body and "Cost spike" in body
    # File moved from firing/ to snoozed/
    assert not (tmp_path / "signals" / "firing" / "aabb1234.json").exists()
    assert (tmp_path / "signals" / "snoozed" / "aabb1234.json").exists()
    on_disk = json.loads((tmp_path / "signals" / "snoozed" / "aabb1234.json").read_text())
    assert on_disk["state"] == "snoozed"
    assert on_disk["snoozed_until"]
    # Default 24h shown
    assert "24 hours" in body or "1 day" in body


def test_mute_resolves_by_prefix(tmp_path):
    _write_real_signal(tmp_path, sig_id="prefix12345abc", scope="pod")
    r = _call("mute", tmp_path, "admin_bot", "prefix12")
    body = r.direct_send_message or ""
    assert "Muted" in body


def test_mute_custom_duration(tmp_path):
    _write_real_signal(tmp_path, sig_id="dur12345", scope="pod")
    r = _call("mute", tmp_path, "admin_bot", "dur12345 1w")
    body = r.direct_send_message or ""
    assert "1 week" in body


def test_mute_ambiguous_prefix_lists_candidates(tmp_path):
    _write_real_signal(tmp_path, sig_id="dup11111aaa", title="A", scope="pod")
    _write_real_signal(tmp_path, sig_id="dup11111bbb", title="B", scope="pod")
    r = _call("mute", tmp_path, "admin_bot", "dup")
    body = r.direct_send_message or ""
    assert "matches more than one" in body
    assert "A" in body and "B" in body


def test_mute_blocks_cross_bot_signal_for_non_evolve_caller(tmp_path):
    _write_real_signal(tmp_path, sig_id="team_bot_a_12345", title="Team_bot_a thing",
                       scope="bot", bot_id="team_bot_a")
    # admin_bot caller — bot-scope signal on team_bot_a should be refused
    r = _call("mute", tmp_path, "admin_bot", "team_bot_a_12345")
    body = r.direct_send_message or ""
    assert "belongs to `team_bot_a`" in body
    # File still in firing
    assert (tmp_path / "signals" / "firing" / "team_bot_a_12345.json").exists()


def test_mute_already_snoozed_returns_friendly_msg(tmp_path):
    _write_real_signal(tmp_path, sig_id="snz12345", state="snoozed",
                       scope="pod", snoozed_until="2026-12-31T00:00:00+00:00")
    r = _call("mute", tmp_path, "admin_bot", "snz12345")
    body = r.direct_send_message or ""
    assert "already in state `snoozed`" in body


# ─────────────────────────────────────────────────────────────────────────────
# Unmute
# ─────────────────────────────────────────────────────────────────────────────


def test_unmute_snoozed_signal_transitions_to_firing(tmp_path):
    _write_real_signal(tmp_path, sig_id="snz67890", state="snoozed",
                       title="Snoozed thing", scope="pod",
                       snoozed_until="2026-12-31T00:00:00+00:00")
    r = _call("unmute", tmp_path, "admin_bot", "snz67890")
    body = r.direct_send_message or ""
    assert "Unmuted" in body
    assert (tmp_path / "signals" / "firing" / "snz67890.json").exists()
    assert not (tmp_path / "signals" / "snoozed" / "snz67890.json").exists()


def test_unmute_firing_signal_is_friendly(tmp_path):
    _write_real_signal(tmp_path, sig_id="fir12345", scope="pod")
    r = _call("unmute", tmp_path, "admin_bot", "fir12345")
    body = r.direct_send_message or ""
    assert "already in state `firing`" in body or "in state `firing`" in body
