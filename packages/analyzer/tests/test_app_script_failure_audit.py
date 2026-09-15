"""Tests for ``app_script_failure_audit`` — the agent-invoked-script-failure daemon.

Sibling producer to ``agent_bypass_audit`` (which catches the never-invoked
case); this one catches the invoked-but-FAILED case via the OpenClaw
``(agent) failed`` exec chip. Coverage:

1. **Text flattening** — ``_all_text_of_record`` pulls the chip out of plain
   strings, text blocks, and ``tool_result`` blocks.

2. **Per-session audit** — ``_audit_one_session_failures``: chip present →
   counted; clean → 0; multiple chips → multiple; basename-without-marker
   and marker-without-basename → 0; cross-app attribution stays separate.

3. **Spec builder** — signature embeds (bot, app); body cites count, window,
   the plugin_intercept remediation, and the spec doc; details pin the
   actionable fields.

4. **collect + run** end-to-end against a tmp_path signal store — one Signal
   per failing (bot, app), dedup across reruns, sweep-resolve on recovery,
   dry-run writes nothing, and the written Signal inherits warn severity +
   platform category from the central producer maps.
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

_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from schema.signal import category_for_producer  # noqa: E402
from signals import store as signals_store  # noqa: E402
from signals.producer_severity import default_severity  # noqa: E402

import app_script_failure_audit  # noqa: E402
from agent_bypass_audit import AtRiskApp  # noqa: E402
from app_script_failure_audit import (  # noqa: E402
    PRODUCER,
    SIGNAL_TYPE,
    BotAppFailureTotals,
    _all_text_of_record,
    _audit_one_session_failures,
    _spec_for_failure,
    collect,
    main,
    run,
)


# A minimal at-risk app for failure detection — only app_id + script_basename
# matter here (the trigger predicate is the bypass audit's concern).
def _app(app_id: str = "personal-bot-task-manager", script: str = "tasks.py") -> AtRiskApp:
    return AtRiskApp(
        app_id=app_id, script_basename=script, trigger_predicate=lambda t, c: False,
    )


TASKS_APP = _app()


# Record builders ────────────────────────────────────────────────────────────


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _assistant_text_blocks(*texts: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": t} for t in texts],
    }


def _assistant_tool(command: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "exec",
             "input": {"command": command}},
        ],
    }


def _tool_result(text: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": text}],
    }


# The OpenClaw exec-failure chip shape (see internal/spec-app-derived-
# permissions-2026-05-24.md): "⚠️ 🔨 run python3 <path>/<script> (agent) failed".
def _chip(script_rel: str = "scripts/tasks.py") -> str:
    return f"⚠️ 🔨 run python3 {script_rel} (agent) failed"


# ─────────────────────────────────────────────────────────────────────────────
# Text flattening
# ─────────────────────────────────────────────────────────────────────────────


def test_flatten_plain_string_record() -> None:
    assert _chip() in _all_text_of_record(_assistant_text(_chip()))


def test_flatten_text_blocks_record() -> None:
    rec = _assistant_text_blocks("thinking…", _chip())
    out = _all_text_of_record(rec)
    assert _chip() in out


def test_flatten_tool_result_record() -> None:
    """The chip can come back as a tool_result block's content."""
    out = _all_text_of_record(_tool_result(_chip()))
    assert "(agent) failed" in out
    assert "tasks.py" in out


def test_flatten_tool_use_carries_no_chip() -> None:
    """A successful invocation (tool_use only) carries no failure text."""
    out = _all_text_of_record(_assistant_tool("python3 scripts/tasks.py /tmp/a.json"))
    assert "(agent) failed" not in out


# ─────────────────────────────────────────────────────────────────────────────
# Per-session audit
# ─────────────────────────────────────────────────────────────────────────────


def test_session_with_failure_chip_is_counted() -> None:
    records = [
        _user("add buy milk to my list"),
        _assistant_text(_chip()),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 1
    assert out.has_failure is True
    assert "tasks.py" in out.example_text


def test_clean_session_has_no_failures() -> None:
    records = [
        _user("add buy milk to my list"),
        _assistant_tool("python3 scripts/tasks.py /tmp/req.json"),
        _assistant_text("Added 'buy milk' to your list."),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 0
    assert out.has_failure is False


def test_multiple_chips_count_each() -> None:
    records = [
        _assistant_text(_chip()),
        _user("try again"),
        _tool_result(_chip()),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 2


def test_basename_without_marker_is_not_a_failure() -> None:
    """A benign mention of the script (no '(agent) failed') is not counted."""
    records = [
        _assistant_text("I ran scripts/tasks.py and it worked great."),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 0


def test_marker_without_basename_is_not_attributed() -> None:
    """A '(agent) failed' chip for some OTHER script is not this app's failure."""
    records = [
        _assistant_text("⚠️ 🔨 run python3 scripts/other_helper.py (agent) failed"),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 0


def test_cross_app_attribution_is_separate() -> None:
    """Two apps in one transcript: each only sees its own script's failures."""
    research = _app(app_id="atlas-on-demand-research", script="atlas_research.py")
    records = [
        _assistant_text(_chip("scripts/tasks.py")),
        _assistant_text("⚠️ 🔨 run python3 a/atlas_research.py (agent) failed"),
    ]
    assert _audit_one_session_failures(records, TASKS_APP).failures_seen == 1
    assert _audit_one_session_failures(records, research).failures_seen == 1


def test_marker_is_case_and_whitespace_tolerant() -> None:
    records = [
        _assistant_text("run scripts/tasks.py (AGENT)  failed: exec preflight denied"),
    ]
    out = _audit_one_session_failures(records, TASKS_APP)
    assert out.failures_seen == 1


# ─────────────────────────────────────────────────────────────────────────────
# Spec builder
# ─────────────────────────────────────────────────────────────────────────────


def _totals(failure_sessions: int = 2) -> BotAppFailureTotals:
    t = BotAppFailureTotals(
        bot_id="personal-bot", app_id="personal-bot-task-manager", script_basename="tasks.py",
    )
    t.sessions_audited = 5
    t.failure_sessions = failure_sessions
    t.failures_total = failure_sessions + 1
    t.examples = ["session abc12345: 1 failure(s) — e.g. 'run tasks.py (agent) failed'"]
    return t


def test_spec_signature_embeds_bot_and_app() -> None:
    a = _spec_for_failure(_totals(), 24)
    b = _spec_for_failure(
        BotAppFailureTotals(bot_id="other", app_id="personal-bot-task-manager"), 24,
    )
    c = _spec_for_failure(
        BotAppFailureTotals(bot_id="personal-bot", app_id="other-app"), 24,
    )
    assert a["signature"] != b["signature"]
    assert a["signature"] != c["signature"]
    assert a["signature"].startswith(f"{PRODUCER}:{SIGNAL_TYPE}:")


def test_spec_identity_fields() -> None:
    spec = _spec_for_failure(_totals(), 24)
    assert spec["producer"] == PRODUCER
    assert spec["type"] == SIGNAL_TYPE
    assert spec["scope"] == "pod"
    # Severity/flavor are deliberately NOT in the spec — they come from the
    # central producer-severity map at observe() time.
    assert "severity" not in spec
    assert "flavor" not in spec


def test_spec_body_cites_counts_window_remediation_and_spec_doc() -> None:
    spec = _spec_for_failure(_totals(failure_sessions=4), 24)
    body = spec["body"]
    assert "4 session" in body
    assert "24h" in body
    assert "plugin_intercept" in body
    assert "(agent) failed" in body
    assert "spec-agent-freelance-bypass" in body


def test_spec_details_pin_actionable_fields() -> None:
    spec = _spec_for_failure(_totals(failure_sessions=4), 24)
    d = spec["details"]
    assert d["bot_id"] == "personal-bot"
    assert d["app_id"] == "personal-bot-task-manager"
    assert d["script_basename"] == "tasks.py"
    assert d["failure_sessions"] == 4
    assert d["lookback_hours"] == 24


# ─────────────────────────────────────────────────────────────────────────────
# collect + run end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _write_shared_turn(
    shared_dir: Path, bot_id: str, date_str: str, session_id: str, ts: str,
) -> None:
    path = shared_dir / bot_id / "turns" / f"turns-{date_str}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": ts, "instance": bot_id, "session_id": session_id,
            "channel": "telegram", "source": "human",
        }) + "\n")


def _write_oc_transcript(home: Path, session_id: str, records: list[dict]) -> None:
    path = (home / ".openclaw" / "agents" / "main" / "agent" / "sessions"
            / session_id / "transcript.jsonl")
    _write_jsonl(path, records)


def _patch_bot_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _bh(bot_id: str, bot_info):  # noqa: ANN001
        user = (bot_info or {}).get("user", bot_id) if isinstance(bot_info, dict) else bot_id
        return tmp_path / "Users" / user
    monkeypatch.setattr(app_script_failure_audit, "_bot_home", _bh)


def _patch_installed(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[AtRiskApp]],
) -> None:
    def _installed(bot_id: str) -> list[AtRiskApp]:
        return list(mapping.get(bot_id, []))
    monkeypatch.setattr(
        app_script_failure_audit, "_installed_at_risk_apps_for_bot", _installed,
    )


def _setup_shared_dir(tmp_path: Path) -> Path:
    shared = tmp_path / "shared" / "evolve"
    shared.mkdir(parents=True)
    return shared


def _seed_failing_session(
    tmp_path: Path, shared: Path, now: datetime, *, sid: str = "sess-fail",
) -> None:
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_shared_turn(shared, "personal-bot", now.date().isoformat(), sid, ts)
    _write_oc_transcript(
        tmp_path / "Users" / "personal-bot", sid,
        [_user("add buy milk"), _assistant_text(_chip())],
    )


def test_collect_returns_no_specs_when_no_at_risk_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_installed(monkeypatch, {})
    specs = collect(
        shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}},
        now=datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc),
    )
    assert specs == []


def test_collect_fires_one_spec_per_failing_bot_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    _seed_failing_session(tmp_path, shared, now)

    specs = collect(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    assert len(specs) == 1
    spec = specs[0]
    assert "personal-bot" in spec["title"]
    assert "personal-bot-task-manager" in spec["title"]
    assert spec["details"]["failure_sessions"] == 1
    assert spec["details"]["failures_total"] == 1
    assert spec["details"]["script_basename"] == "tasks.py"


def test_collect_skips_clean_bot_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_shared_turn(shared, "personal-bot", "2026-06-15", "sess-ok", ts)
    _write_oc_transcript(
        tmp_path / "Users" / "personal-bot", "sess-ok",
        [_user("add buy milk"),
         _assistant_tool("python3 scripts/tasks.py /tmp/a.json"),
         _assistant_text("Done.")],
    )
    specs = collect(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    assert specs == []


def test_run_writes_signal_then_dedupes_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    _seed_failing_session(tmp_path, shared, now)

    kept1, fired1, _ = run(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    assert fired1 == 1
    assert len(kept1) == 1

    kept2, fired2, _ = run(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    assert fired2 == 1            # spec still emitted; observe() dedups
    assert kept2 == kept1

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1       # only one Signal on disk


def test_run_written_signal_inherits_warn_and_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Severity + category come from the central producer maps, not the spec —
    this pins that the registration is load-bearing."""
    assert default_severity(PRODUCER) == "warn"
    assert category_for_producer(PRODUCER) == "platform"

    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    _seed_failing_session(tmp_path, shared, now)

    run(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    sig = signals_store.load_signal_file(firing[0])
    assert sig is not None
    assert sig.severity == "warn"
    assert sig.category == "platform"
    assert sig.producer == PRODUCER
    assert sig.type == SIGNAL_TYPE


def test_run_sweep_resolves_when_failure_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    _seed_failing_session(tmp_path, shared, now)

    _, fired1, _ = run(shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now)
    assert fired1 == 1

    # Operator migrates/fixes the app — the transcript now shows a clean
    # invocation, no failure chip.
    _write_oc_transcript(
        tmp_path / "Users" / "personal-bot", "sess-fail",
        [_user("add buy milk"),
         _assistant_tool("python3 scripts/tasks.py /tmp/a.json")],
    )
    kept2, fired2, resolved2 = run(
        shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now,
    )
    assert fired2 == 0
    assert kept2 == set()
    assert resolved2 == 1
    archived = list((shared / "signals" / "archived").glob("*.json"))
    assert len(archived) == 1


def test_run_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    _patch_bot_home(monkeypatch, tmp_path)
    _patch_installed(monkeypatch, {"personal-bot": [TASKS_APP]})
    now = datetime(2026, 6, 15, 4, 40, tzinfo=timezone.utc)
    _seed_failing_session(tmp_path, shared, now)

    _, fired, _ = run(
        shared, network={"bots": {"personal-bot": {"user": "personal-bot"}}}, now=now, dry_run=True,
    )
    assert fired == 1
    assert not (shared / "signals" / "firing").exists() or \
        not any((shared / "signals" / "firing").glob("*.json"))
    assert "would_observe" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def test_main_returns_zero_on_empty_pod(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    shared = _setup_shared_dir(tmp_path)
    rc = main(["--shared-dir", str(shared), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"[{PRODUCER}]" in out
    assert "fired=0" in out


def test_main_accepts_once_for_launchd_compat(tmp_path: Path) -> None:
    shared = _setup_shared_dir(tmp_path)
    assert main(["--shared-dir", str(shared), "--once"]) == 0
