"""Tests for autonomy.actions_ledger (OQ-3 bot-side counters, evolve-side
reader) + autonomy.limits (rung-3 daily caps, §1.3)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autonomy import actions_ledger as _ledger
from autonomy import limits as _limits
from autonomy import renderer as _renderer
from autonomy import store as _store


BOT = "alpha"
IID = "google_workspace"
SEND = "send_gmail_message"
SEARCH = "search_gmail_messages"

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".openclaw").mkdir(parents=True)
    (h / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {IID: {"command": "uvx", "args": ["workspace-mcp"]}}},
    }))
    return h


def _write_ledger(shared_dir: Path, bot_id: str, records: list[dict]) -> None:
    root = _ledger.ledger_dir(shared_dir, bot_id)
    root.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for rec in records:
        by_day.setdefault(rec["ts"][:10], []).append(json.dumps(rec))
    for day, lines in by_day.items():
        path = root / f"{_ledger.LEDGER_FILE_PREFIX}{day}.jsonl"
        with path.open("a") as f:
            f.write("\n".join(lines) + "\n")


def _rec(ts: str, tool: str = SEND, result: str = "ok", iid: str = IID) -> dict:
    return {
        "ts": ts, "integration_id": iid, "tool_name": tool,
        "result": result, "session_id": "s", "turn_id": "t",
    }


def _live_deny(home: Path) -> list[str]:
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    return (cfg.get("tools") or {}).get("deny") or []


def _set_rung3(shared_dir: Path, cap: int = 2) -> None:
    _store.set_posture(
        shared_dir, BOT, IID,
        rung="autonomous_within_rules",
        rules={"actions_per_day": cap},
        actor=_store.ACTOR_OPERATOR_UI,
    )


# ── Ledger reader ────────────────────────────────────────────────────────────

def test_reader_classifies_outward_only(shared_dir: Path):
    _write_ledger(shared_dir, BOT, [
        _rec("2026-06-11T10:00:00Z"),                       # send → outward
        _rec("2026-06-11T10:01:00Z", tool=SEARCH),          # read-tier → skipped
        _rec("2026-06-11T10:02:00Z", iid="unknown_server"),  # no binding → skipped
        _rec("2026-06-11T10:03:00Z", result="error"),
    ])
    actions = _ledger.read_outward_actions(shared_dir, BOT, now=NOW)
    assert [a.tool_name for a in actions] == [SEND, SEND]
    assert [a.verb for a in actions] == ["send", "send"]
    assert [a.result for a in actions] == ["ok", "error"]


def test_reader_window_and_garbage_tolerance(shared_dir: Path):
    old = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_ledger(shared_dir, BOT, [_rec(old), _rec("2026-06-11T08:00:00Z")])
    root = _ledger.ledger_dir(shared_dir, BOT)
    (root / f"{_ledger.LEDGER_FILE_PREFIX}2026-06-11.jsonl").open("a").write(
        "not-json\n{\"ts\": 42}\n"
    )
    actions = _ledger.read_outward_actions(shared_dir, BOT, window_days=30, now=NOW)
    assert len(actions) == 1
    assert actions[0].day == "2026-06-11"


def test_count_for_day_errors_split(shared_dir: Path):
    _write_ledger(shared_dir, BOT, [
        _rec("2026-06-11T10:00:00Z"),
        _rec("2026-06-11T11:00:00Z", result="error"),
    ])
    actions = _ledger.read_outward_actions(shared_dir, BOT, now=NOW)
    assert _ledger.count_for_day(actions, IID, "2026-06-11") == 1
    assert _ledger.count_for_day(actions, IID, "2026-06-11", include_errors=True) == 2


def test_prune_removes_only_stale_files(shared_dir: Path):
    old_day = (NOW - timedelta(days=120)).date().isoformat()
    _write_ledger(shared_dir, BOT, [
        _rec(f"{old_day}T10:00:00Z"), _rec("2026-06-11T10:00:00Z"),
    ])
    removed = _ledger.prune(shared_dir, BOT, now=NOW)
    assert removed == 1
    names = [p.name for p in _ledger.ledger_dir(shared_dir, BOT).glob("*.jsonl")]
    assert names == [f"{_ledger.LEDGER_FILE_PREFIX}2026-06-11.jsonl"]


# ── Limits: pause lifecycle ──────────────────────────────────────────────────

def test_cap_hit_pauses_renders_and_fires(shared_dir: Path, home: Path):
    _set_rung3(shared_dir, cap=2)
    _write_ledger(shared_dir, BOT, [
        _rec("2026-06-11T09:00:00Z"), _rec("2026-06-11T10:00:00Z"),
    ])
    findings, ran_ok = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert ran_ok
    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "autonomy_limit_hit"
    assert f["severity"] == "warn"
    assert f["signature_scope"] == f"{BOT}:{IID}"
    assert f["details"]["count"] == 2 and f["details"]["cap"] == 2
    # Pause is recorded and mechanically rendered: send tool denied.
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == {IID}
    assert f"mcp__{IID}__{SEND}" in _live_deny(home)
    # Sidecar is bot-readable (0644) for the session-surface pause note.
    assert (_limits.limits_path(shared_dir, BOT).stat().st_mode & 0o777) == 0o644


def test_under_cap_no_pause_no_findings(shared_dir: Path, home: Path):
    _set_rung3(shared_dir, cap=5)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    findings, ran_ok = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert ran_ok and findings == []
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == set()
    assert f"mcp__{IID}__{SEND}" not in _live_deny(home)


def test_errors_do_not_consume_the_cap(shared_dir: Path, home: Path):
    _set_rung3(shared_dir, cap=2)
    _write_ledger(shared_dir, BOT, [
        _rec("2026-06-11T09:00:00Z"),
        _rec("2026-06-11T10:00:00Z", result="error"),
    ])
    findings, _ = _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []


def test_day_roll_unpauses_and_rerenders(shared_dir: Path, home: Path):
    _set_rung3(shared_dir, cap=1)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert f"mcp__{IID}__{SEND}" in _live_deny(home)

    tomorrow = NOW + timedelta(days=1)
    # paused_integrations reads stale pauses as lifted immediately…
    assert _limits.paused_integrations(shared_dir, BOT, now=tomorrow) == set()
    # …and the next evaluation clears state + re-renders the deny away.
    findings, _ = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=tomorrow,
    )
    assert findings == []
    assert f"mcp__{IID}__{SEND}" not in _live_deny(home)


def test_pause_persists_for_the_day_even_if_count_lower(
    shared_dir: Path, home: Path,
):
    # Once paused, a later evaluation the same day keeps the pause even
    # if the ledger reads lower (e.g. a pruned file) — no flap.
    _set_rung3(shared_dir, cap=1)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    for p in _ledger.ledger_dir(shared_dir, BOT).glob("*.jsonl"):
        p.unlink()
    findings, _ = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert len(findings) == 1
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == {IID}


def test_same_day_demotion_keeps_the_pause_wall(shared_dir: Path, home: Path):
    # Second-pass review finding: leaving rung 3 the same day (incl.
    # the auto-demotion reflex) must NOT lift the outward-deny wall the
    # day's spent budget earned — that would mechanically WIDEN the
    # exact integration that was being probed.
    _set_rung3(shared_dir, cap=1)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=_store.ACTOR_OPERATOR_UI,
    )
    findings, _ = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert len(findings) == 1  # the pause condition still holds
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == {IID}
    assert f"mcp__{IID}__{SEND}" in _live_deny(home)

    # The day rolls → the leftover entry clears and the deny lifts.
    tomorrow = NOW + timedelta(days=1)
    findings, _ = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=tomorrow,
    )
    assert findings == []
    assert _limits.load_limits(shared_dir, BOT) == {}
    assert f"mcp__{IID}__{SEND}" not in _live_deny(home)


def test_raising_the_cap_unpauses_same_day(shared_dir: Path, home: Path):
    # The autonomy_limit_hit alert tells the operator to raise the
    # daily limit if it keeps happening — doing so must actually work
    # the same day (second-pass review finding).
    _set_rung3(shared_dir, cap=2)
    _write_ledger(shared_dir, BOT, [
        _rec("2026-06-11T09:00:00Z"), _rec("2026-06-11T10:00:00Z"),
    ])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == {IID}

    _set_rung3(shared_dir, cap=10)
    findings, _ = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert findings == []
    assert _limits.paused_integrations(shared_dir, BOT, now=NOW) == set()
    assert f"mcp__{IID}__{SEND}" not in _live_deny(home)


def test_backfill_inferred_rung_never_counted(shared_dir: Path, home: Path):
    # Observe-only postures are never rendered — and never capped.
    # (Backfill can't write rung 3, but defend the invariant directly.)
    _store.ensure_entry(
        shared_dir, BOT, IID, kind="email", rung="act_with_approval",
        actor=_store.ACTOR_BACKFILL,
    )
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    findings, ran_ok = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW,
    )
    assert ran_ok and findings == []
    assert _limits.load_limits(shared_dir, BOT) == {}


def test_dry_run_computes_without_writing(shared_dir: Path, home: Path):
    _set_rung3(shared_dir, cap=1)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    findings, ran_ok = _limits.evaluate_bot(
        shared_dir, BOT, home_override=home, now=NOW, enforce=False,
    )
    assert ran_ok and len(findings) == 1
    assert not _limits.limits_path(shared_dir, BOT).exists()
    assert f"mcp__{IID}__{SEND}" not in _live_deny(home)


# ── Coherence agrees with the pause render ──────────────────────────────────

def test_coherence_clean_while_paused(shared_dir: Path, home: Path):
    from autonomy import coherence as _coherence
    _set_rung3(shared_dir, cap=1)
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T09:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    # The audit must read the same pause set as the render — a pause is
    # not drift. Both pin the same instant so the check doesn't read the
    # day as rolled relative to the render.
    assert _coherence.check_bot(
        BOT, shared_dir, home_override=home, now=NOW,
    ) == []


def test_renderer_pause_param_widens_deny(shared_dir: Path):
    _set_rung3(shared_dir, cap=1)
    doc = _store.load(shared_dir, BOT)
    expected, _ = _renderer.expected_deny_by_integration(doc)
    assert expected[IID] == []  # no delete tool on this server
    expected_paused, _ = _renderer.expected_deny_by_integration(
        doc, paused={IID},
    )
    assert f"mcp__{IID}__{SEND}" in expected_paused[IID]
