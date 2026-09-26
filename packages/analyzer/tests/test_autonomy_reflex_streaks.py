"""Tests for autonomy.reflex (auto-demotion, §3.3 option b) +
autonomy.streaks (the promotion-streak producer, §3.2)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autonomy import actions_ledger as _ledger
from autonomy import limits as _limits
from autonomy import reflex as _reflex
from autonomy import store as _store
from autonomy import streaks as _streaks
from signals import store as _signals_store


BOT = "alpha"
IID = "google_workspace"
SEND = "send_gmail_message"

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
EARLY_ISO = "2026-05-01T00:00:00Z"


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


def _rec(ts: str, result: str = "ok") -> dict:
    return {
        "ts": ts, "integration_id": IID, "tool_name": SEND,
        "result": result, "session_id": "s", "turn_id": "t",
    }


def _after(iso_z: str, seconds: int) -> str:
    dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc,
    ) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_posture_at(
    shared_dir: Path, *, rung: str, rules: dict | None = None,
    actor: str = _store.ACTOR_OPERATOR_UI, set_at: str = EARLY_ISO,
    monkeypatch=None,
) -> None:
    if monkeypatch is not None:
        monkeypatch.setattr(_store, "now_iso", lambda: set_at)
    try:
        _store.set_posture(
            shared_dir, BOT, IID, rung=rung, rules=rules or {}, actor=actor,
        )
    finally:
        if monkeypatch is not None:
            monkeypatch.undo()


def _security_alert(shared_dir: Path, *, iid: str = IID, producer: str = "security_warden"):
    return _signals_store.observe(
        shared_dir,
        signature=f"{producer}:finding:{BOT}:{iid}",
        producer=producer,
        type="warden_finding",
        flavor="maintenance",
        severity="alert",
        scope="bot",
        bot_id=BOT,
        title="critical finding",
        body="…",
        details={"integration_id": iid},
    )


# ── Reflex trigger 1: probing past the pause ─────────────────────────────────

def test_escalation_after_pause_demotes_one_rung(
    shared_dir: Path, home: Path, monkeypatch,
):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 1}, monkeypatch=monkeypatch,
    )
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T08:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    paused_at = _limits.load_limits(shared_dir, BOT)[IID]["paused_at"]
    # Three attempts AFTER the pause render — probing the cage.
    _write_ledger(shared_dir, BOT, [
        _rec(_after(paused_at, i + 1), result="error") for i in range(3)
    ])

    findings, ran_ok = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert ran_ok
    posture = _store.load(shared_dir, BOT).integrations[IID]
    assert posture.rung == "act_with_approval"
    actor = posture.set_by["actor"]
    assert actor.startswith(_store.ACTOR_PREFIX_AUTO_DEMOTION)
    # The cleared rules block is preserved in the history record for
    # the one-click restore.
    assert posture.history[-1]["prior_rules"] == {"actions_per_day": 1}

    demoted = [f for f in findings if f["type"] == "autonomy_demoted"]
    assert len(demoted) == 1
    f = demoted[0]
    assert f["severity"] == "alert"
    assert f["details"]["prior_rules"] == {"actions_per_day": 1}
    rem = f["remediation"]
    assert rem["kind"] == "restore_autonomy_posture"
    assert rem["params"]["rules"] == {"actions_per_day": 1}
    assert rem["params"]["rung"] == "autonomous_within_rules"
    assert rem["params"]["expected_current_rung"] == "act_with_approval"


def test_few_attempts_after_pause_do_not_demote(
    shared_dir: Path, home: Path, monkeypatch,
):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 1}, monkeypatch=monkeypatch,
    )
    _write_ledger(shared_dir, BOT, [_rec("2026-06-11T08:00:00Z")])
    _limits.evaluate_bot(shared_dir, BOT, home_override=home, now=NOW)
    paused_at = _limits.load_limits(shared_dir, BOT)[IID]["paused_at"]
    _write_ledger(shared_dir, BOT, [
        _rec(_after(paused_at, 1), result="error"),
    ])
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "autonomous_within_rules"
    )


# ── Reflex trigger 2: critical security finding naming the integration ──────

def test_critical_security_finding_demotes(
    shared_dir: Path, home: Path, monkeypatch,
):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    sig = _security_alert(shared_dir)
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    posture = _store.load(shared_dir, BOT).integrations[IID]
    assert posture.rung == "act_with_approval"
    assert posture.set_by["actor"] == (
        f"{_store.ACTOR_PREFIX_AUTO_DEMOTION}{sig.id}"
    )
    assert findings[0]["details"]["trigger_signal_id"] == sig.id


def test_finding_must_name_the_integration_exactly(
    shared_dir: Path, home: Path, monkeypatch,
):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    _security_alert(shared_dir, iid="some_other_server")
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "autonomous_within_rules"
    )


def test_cost_breaker_signals_never_demote(
    shared_dir: Path, home: Path, monkeypatch,
):
    # Spec §3.3 trigger 3: the cost breaker already halts the bot —
    # posture is not double-punished. Even an alert-severity cost
    # signal NAMING the integration must not trip the reflex.
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    for producer in ("cost_watchdog", "spend_alert", "breakers_runner"):
        _security_alert(shared_dir, producer=producer)
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "autonomous_within_rules"
    )


# ── Reflex bounds + lifecycle ────────────────────────────────────────────────

def test_demotion_only_from_rung3(shared_dir: Path, home: Path, monkeypatch):
    _set_posture_at(shared_dir, rung="act_with_approval", monkeypatch=monkeypatch)
    _security_alert(shared_dir)
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "act_with_approval"
    )


def test_pending_review_rederives_until_operator_acts(
    shared_dir: Path, home: Path, monkeypatch,
):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    _security_alert(shared_dir)
    first, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    # Later passes re-derive the same finding (same signature scope,
    # remediation rebuilt from history) — no second demotion.
    second, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert len(second) == 1
    assert second[0]["type"] == "autonomy_demoted"
    assert second[0]["signature_scope"] == first[0]["signature_scope"]
    assert second[0]["remediation"]["params"]["rules"] == {"actions_per_day": 10}
    posture = _store.load(shared_dir, BOT).integrations[IID]
    demotions = [
        h for h in posture.history
        if str(h.get("actor", "")).startswith(_store.ACTOR_PREFIX_AUTO_DEMOTION)
    ]
    assert len(demotions) == 1

    # Operator restores (a promotion) → condition clears.
    _store.set_posture(
        shared_dir, BOT, IID, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, actor=_store.ACTOR_OPERATOR_UI,
    )
    # Signal gone for the security trigger? The warden signal still
    # fires, so the reflex would demote AGAIN — resolve it first (the
    # operator handled the incident).
    for sig in list(_signals_store.iter_active(shared_dir, bot_id=BOT)):
        _signals_store.apply_transition(sig, "resolved", shared_dir, actor="test")
    cleared, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert cleared == []


def test_restore_not_relooped_by_reobserved_finding(
    shared_dir: Path, home: Path, monkeypatch,
):
    # The security trigger floors on the Signal's created_at — a
    # sweep producer re-observing the SAME finding (which bumps
    # last_observed_at) after the operator restored must not re-demote
    # (the restore-button loop, second-pass review finding).
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    _security_alert(shared_dir)
    _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "act_with_approval"
    )
    # Operator restores AFTER the finding fired (set_at must postdate
    # created_at deterministically — pin it well into the future).
    monkeypatch.setattr(_store, "now_iso", lambda: "2027-01-01T00:00:00Z")
    _store.set_posture(
        shared_dir, BOT, IID, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, actor=_store.ACTOR_OPERATOR_UI,
    )
    monkeypatch.undo()
    # The warden's next sweep re-observes the still-firing finding.
    _security_alert(shared_dir)
    findings, _ = _reflex.run_bot(shared_dir, BOT, home_override=home, now=NOW)
    assert findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "autonomous_within_rules"
    )


def test_dry_run_never_acts(shared_dir: Path, home: Path, monkeypatch):
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 10}, monkeypatch=monkeypatch,
    )
    _security_alert(shared_dir)
    findings, ran_ok = _reflex.run_bot(
        shared_dir, BOT, home_override=home, now=NOW, act=False,
    )
    assert ran_ok and findings == []
    assert _store.load(shared_dir, BOT).integrations[IID].rung == (
        "autonomous_within_rules"
    )


# ── Streaks ──────────────────────────────────────────────────────────────────

def _streak_records(n: int = 12, days: int = 8) -> list[dict]:
    out = []
    for i in range(n):
        day = (NOW - timedelta(days=days - 1 - (i % days))).date().isoformat()
        out.append(_rec(f"{day}T0{i % 9}:00:00Z"))
    return out


def test_streak_fires_candidate(shared_dir: Path, monkeypatch):
    _set_posture_at(shared_dir, rung="act_with_approval", monkeypatch=monkeypatch)
    _write_ledger(shared_dir, BOT, _streak_records())
    findings, ran_ok = _streaks.candidates(shared_dir, BOT, now=NOW)
    assert ran_ok
    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "autonomy_promotion_candidate"
    assert f["severity"] == "info"
    assert f["signature_scope"] == f"{BOT}:{IID}"
    assert f["details"]["actions"] == 12
    assert f["details"]["suggested_actions_per_day"] >= 5
    assert f["details"]["next_rung"] == "autonomous_within_rules"


def test_streak_thresholds(shared_dir: Path, monkeypatch):
    _set_posture_at(shared_dir, rung="act_with_approval", monkeypatch=monkeypatch)
    # Too few actions.
    _write_ledger(shared_dir, BOT, _streak_records(n=5, days=8))
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []
    # Enough actions but compressed into too short a span.
    for p in _ledger.ledger_dir(shared_dir, BOT).glob("*.jsonl"):
        p.unlink()
    _write_ledger(shared_dir, BOT, _streak_records(n=12, days=3))
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []


def test_streak_only_counts_actions_at_current_posture(
    shared_dir: Path, monkeypatch,
):
    # Actions performed before "Asks first" became the rung don't count.
    _set_posture_at(
        shared_dir, rung="act_with_approval",
        set_at="2026-06-09T00:00:00Z", monkeypatch=monkeypatch,
    )
    _write_ledger(shared_dir, BOT, _streak_records())
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []


def test_streak_vetoes(shared_dir: Path, monkeypatch):
    # Wrong rung.
    _set_posture_at(
        shared_dir, rung="autonomous_within_rules",
        rules={"actions_per_day": 5}, monkeypatch=monkeypatch,
    )
    _write_ledger(shared_dir, BOT, _streak_records())
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []

    # Right rung but set by the demotion reflex — restore path owns it.
    monkeypatch.setattr(_store, "now_iso", lambda: EARLY_ISO)
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=f"{_store.ACTOR_PREFIX_AUTO_DEMOTION}sig-1",
    )
    monkeypatch.undo()
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []

    # Deliberate rung 2, but an autonomy incident is firing for the iid.
    monkeypatch.setattr(_store, "now_iso", lambda: EARLY_ISO)
    _store.set_posture(
        shared_dir, BOT, IID, rung="act_with_approval",
        actor=_store.ACTOR_OPERATOR_UI,
    )
    monkeypatch.undo()
    _signals_store.observe(
        shared_dir,
        signature=f"permission_monitor:autonomy_posture_drift:{BOT}:{IID}",
        producer="permission_monitor",
        type="autonomy_posture_drift",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=BOT,
        title="drift",
        body="…",
        details={"integration_id": IID},
    )
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []


def test_streak_skips_backfill_inferred(shared_dir: Path):
    _store.ensure_entry(
        shared_dir, BOT, IID, kind="email", rung="act_with_approval",
        actor=_store.ACTOR_BACKFILL,
    )
    # ensure_entry stamps set_at=now; backfill actor alone must veto.
    _write_ledger(shared_dir, BOT, _streak_records())
    assert _streaks.candidates(shared_dir, BOT, now=NOW)[0] == []
