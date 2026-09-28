"""tests/test_proposal_synthesizer_tools.py — Phase 4 investigation tools.

Each read-only tool is tested against a tmp_path shared_dir fixture
that mocks the on-disk shape (signal store, watchdog log, audit
findings, proposal store) without touching production data.

Bot-scoped tools that read from ``/Users/<bot>/.openclaw/`` use a
monkeypatched ``_bot_home`` so the test puts the bot's home inside
the tmp_path tree.

Tools never raise — error paths return ``{"error": "..."}``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from proposal_synthesizer import tools  # noqa: E402


# ── Fixture: redirect _bot_home into tmp_path ────────────────────────────────


@pytest.fixture
def fake_bot_home(tmp_path, monkeypatch):
    """Monkeypatch `_bot_home` so bot reads land in tmp_path/<bot>/."""
    def _home(bot_id: str) -> Path:
        return tmp_path / "bots" / bot_id

    monkeypatch.setattr(tools, "_bot_home", _home)
    return _home


# ── read_signal_history ──────────────────────────────────────────────────────


def test_signal_history_returns_records_matching_fingerprint(tmp_path):
    from signals.store import write_signal
    from schema.signal import Signal, new_signal_id

    sig = Signal(
        id=new_signal_id(),
        producer="cost_watchdog",
        type="heartbeat_no_model_override",
        signature="efficiency_hawk:heartbeat_no_model_override:admin_bot",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        state="firing",
        bot_id="admin_bot",
        title="admin_bot heartbeat",
        body="b",
        details={},
        created_at=datetime.now(timezone.utc).isoformat(),
        last_observed_at=datetime.now(timezone.utc).isoformat(),
    )
    write_signal(sig, tmp_path)

    out = tools.dispatch_tool(
        "read_signal_history",
        {"fingerprint": "efficiency_hawk:heartbeat_no_model_override:admin_bot"},
        tmp_path,
    )
    assert "signal_history" in out
    assert out["total"] == 1
    assert out["signal_history"][0]["bot_id"] != ""  # field present


def test_signal_history_requires_fingerprint(tmp_path):
    out = tools.dispatch_tool("read_signal_history", {}, tmp_path)
    assert "error" in out


# ── read_cost_ledger ─────────────────────────────────────────────────────────


def test_cost_ledger_summary_includes_breakdown(tmp_path, monkeypatch):
    """cost_ledger.read_events is what the tool wraps; inject a fake."""
    import cost_ledger

    events = [
        {"trigger_kind": "heartbeat", "cost_usd": 1.0, "ts": "2026-05-11T10:00:00Z"},
        {"trigger_kind": "user_turn", "cost_usd": 0.5, "ts": "2026-05-11T11:00:00Z"},
        {"trigger_kind": "heartbeat", "cost_usd": 2.0, "ts": "2026-05-11T12:00:00Z"},
    ]
    monkeypatch.setattr(
        cost_ledger,
        "read_events",
        lambda bot_id, days=7, shared_dir=None: iter(events),
    )

    out = tools.dispatch_tool(
        "read_cost_ledger", {"bot_id": "admin_bot"}, tmp_path
    )
    assert out["event_count"] == 3
    assert out["total_cost_usd"] == 3.5
    assert out["cost_by_trigger_kind"]["heartbeat"] == 3.0
    assert out["cost_by_trigger_kind"]["user_turn"] == 0.5


def test_cost_ledger_filter_narrows(tmp_path, monkeypatch):
    import cost_ledger

    events = [
        {"trigger_kind": "heartbeat", "cost_usd": 1.0},
        {"trigger_kind": "user_turn", "cost_usd": 0.5},
    ]
    monkeypatch.setattr(
        cost_ledger,
        "read_events",
        lambda bot_id, days=7, shared_dir=None: iter(events),
    )

    out = tools.dispatch_tool(
        "read_cost_ledger",
        {"bot_id": "admin_bot", "trigger_kind": "heartbeat"},
        tmp_path,
    )
    assert out["event_count"] == 1
    assert "user_turn" not in out["cost_by_trigger_kind"]


def test_cost_ledger_requires_bot_id(tmp_path):
    out = tools.dispatch_tool("read_cost_ledger", {}, tmp_path)
    assert "error" in out


# ── read_session_transcript ──────────────────────────────────────────────────


def test_session_transcript_reads_jsonl(tmp_path, fake_bot_home):
    sess = fake_bot_home("admin_bot") / ".openclaw" / "sessions" / "sess-1"
    sess.mkdir(parents=True)
    (sess / "transcript.jsonl").write_text(
        '{"role": "user", "text": "hi"}\n'
        '{"role": "assistant", "text": "hello"}\n'
    )
    out = tools.dispatch_tool(
        "read_session_transcript",
        {"bot_id": "admin_bot", "session_id": "sess-1"},
        tmp_path,
    )
    assert out["turn_count"] == 2
    assert out["last_turns"][-1]["text"] == "hello"


def test_session_transcript_missing_file_returns_error(tmp_path, fake_bot_home):
    out = tools.dispatch_tool(
        "read_session_transcript",
        {"bot_id": "admin_bot", "session_id": "no-such-session"},
        tmp_path,
    )
    assert "error" in out


# ── read_bot_config ──────────────────────────────────────────────────────────


def test_bot_config_returns_parsed_json(tmp_path, fake_bot_home):
    oc = fake_bot_home("admin_bot") / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text('{"agents": {"defaults": {"heartbeat": {"every": "1h"}}}}')
    out = tools.dispatch_tool("read_bot_config", {"bot_id": "admin_bot"}, tmp_path)
    assert out["config"]["agents"]["defaults"]["heartbeat"]["every"] == "1h"


def test_bot_config_invalid_json_returns_error(tmp_path, fake_bot_home):
    oc = fake_bot_home("admin_bot") / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "openclaw.json").write_text("{not valid json")
    out = tools.dispatch_tool("read_bot_config", {"bot_id": "admin_bot"}, tmp_path)
    assert "error" in out
    assert "parse failure" in out["error"]


# ── read_workspace_file ──────────────────────────────────────────────────────


def test_workspace_file_reads_content(tmp_path, fake_bot_home):
    ws = fake_bot_home("admin_bot") / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (ws / "heartbeats.md").write_text("# heartbeat log\n\nlast: 12:00")
    out = tools.dispatch_tool(
        "read_workspace_file",
        {"bot_id": "admin_bot", "path": "heartbeats.md"},
        tmp_path,
    )
    assert "heartbeat log" in out["content"]
    assert out["size_bytes"] > 0


def test_workspace_file_rejects_traversal(tmp_path, fake_bot_home):
    out = tools.dispatch_tool(
        "read_workspace_file",
        {"bot_id": "admin_bot", "path": "../../etc/passwd"},
        tmp_path,
    )
    assert "error" in out


def test_workspace_file_rejects_absolute(tmp_path, fake_bot_home):
    out = tools.dispatch_tool(
        "read_workspace_file",
        {"bot_id": "admin_bot", "path": "/etc/passwd"},
        tmp_path,
    )
    assert "error" in out


# ── read_watchdog_log ────────────────────────────────────────────────────────


def test_watchdog_log_reads_jsonl(tmp_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (tmp_path / "watchdog").mkdir()
    (tmp_path / "watchdog" / f"{today}.jsonl").write_text(
        '{"type": "gateway_flap", "ts": "2026-05-11T10:00:00Z"}\n'
        '{"type": "disk_high", "ts": "2026-05-11T11:00:00Z"}\n'
    )
    out = tools.dispatch_tool("read_watchdog_log", {"days": 1}, tmp_path)
    assert out["total"] == 2


def test_watchdog_log_filter_by_type(tmp_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (tmp_path / "watchdog").mkdir()
    (tmp_path / "watchdog" / f"{today}.jsonl").write_text(
        '{"type": "gateway_flap", "ts": "2026-05-11T10:00:00Z"}\n'
        '{"type": "disk_high", "ts": "2026-05-11T11:00:00Z"}\n'
    )
    out = tools.dispatch_tool(
        "read_watchdog_log",
        {"days": 1, "event_type": "gateway_flap"},
        tmp_path,
    )
    assert out["total"] == 1
    assert out["events"][0]["type"] == "gateway_flap"


def test_watchdog_log_missing_dir_returns_empty(tmp_path):
    out = tools.dispatch_tool("read_watchdog_log", {}, tmp_path)
    assert out["events"] == []
    assert out["total"] == 0


# ── read_audit_findings ──────────────────────────────────────────────────────


def test_audit_findings_returns_json(tmp_path):
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "current-findings.json").write_text(
        '{"findings": [{"id": "f1", "severity": "warn"}]}'
    )
    out = tools.dispatch_tool("read_audit_findings", {}, tmp_path)
    assert "findings" in out
    assert out["findings"]["findings"][0]["id"] == "f1"


def test_audit_findings_missing_returns_note(tmp_path):
    out = tools.dispatch_tool("read_audit_findings", {}, tmp_path)
    assert out["findings"] == []
    assert "no current-findings" in out.get("note", "")


# ── read_proposal_history ────────────────────────────────────────────────────


def test_proposal_history_filter_by_bot(tmp_path):
    from arbiter.store import write_proposal
    from schema.proposal import Investigation, Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    for bot_id in ("admin_bot", "team_bot_c"):
        p = Proposal(
            id=new_proposal_id(),
            bot_id=bot_id,
            generator_id="efficiency_hawk",
            dimension="efficiency",
            trigger_observations=[],
            provenance=Provenance(technique="t", signals={}, confidence=0.8),
            problem=f"{bot_id} something",
            action=Investigation(context="x"),
            risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
            status="pending",
        )
        write_proposal(p, tmp_path)

    out = tools.dispatch_tool(
        "read_proposal_history", {"bot_id": "admin_bot"}, tmp_path
    )
    assert out["total"] == 1
    assert out["proposals"][0]["bot_id"] == "admin_bot"


# ── git_log / git_blame ──────────────────────────────────────────────────────


def _init_test_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo for git_log / git_blame testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "foo.txt").write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "initial"],
        cwd=repo,
        check=True,
    )
    return repo


def test_git_log_returns_commits(tmp_path):
    repo = _init_test_repo(tmp_path)
    out = tools.dispatch_tool(
        "git_log",
        {"days": 30, "_repo_path_for_test": str(repo)},
        tmp_path,
    )
    assert "error" not in out
    assert out["total"] >= 1
    assert out["commits"][0]["subject"] == "initial"


def test_git_log_missing_repo_returns_error(tmp_path):
    out = tools.dispatch_tool(
        "git_log",
        {"_repo_path_for_test": str(tmp_path / "no-such-repo")},
        tmp_path,
    )
    assert "error" in out


def test_git_blame_returns_lines(tmp_path):
    repo = _init_test_repo(tmp_path)
    out = tools.dispatch_tool(
        "git_blame",
        {
            "path": "foo.txt",
            "line_start": 1,
            "line_end": 3,
            "_repo_path_for_test": str(repo),
        },
        tmp_path,
    )
    assert "error" not in out
    assert len(out["lines"]) == 3


def test_git_blame_rejects_wide_range(tmp_path):
    out = tools.dispatch_tool(
        "git_blame",
        {"path": "foo.txt", "line_start": 1, "line_end": 500},
        tmp_path,
    )
    assert "error" in out
    assert "too wide" in out["error"]


# ── Registry shape ───────────────────────────────────────────────────────────


def test_tool_registry_has_all_ten_tools():
    expected = {
        "read_signal_history",
        "read_cost_ledger",
        "read_session_transcript",
        "read_bot_config",
        "read_workspace_file",
        "read_watchdog_log",
        "read_audit_findings",
        "read_proposal_history",
        "git_log",
        "git_blame",
    }
    assert set(tools.TOOL_REGISTRY.keys()) == expected


def test_anthropic_tools_schema_is_well_formed():
    schemas = tools.anthropic_tools_schema()
    assert len(schemas) == 10
    for s in schemas:
        assert s["name"]
        assert s["description"]
        assert s["input_schema"]["type"] == "object"


def test_dispatch_unknown_tool_returns_error(tmp_path):
    out = tools.dispatch_tool("nope", {}, tmp_path)
    assert out == {"error": "unknown tool: 'nope'"}


def test_tool_response_size_cap_applies(tmp_path, fake_bot_home, monkeypatch):
    """A huge tool response is truncated with a clear marker."""
    ws = fake_bot_home("admin_bot") / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    huge = "x" * (tools.MAX_TOOL_RESPONSE_BYTES * 2)
    (ws / "huge.md").write_text(huge)
    out = tools.dispatch_tool(
        "read_workspace_file",
        {"bot_id": "admin_bot", "path": "huge.md"},
        tmp_path,
    )
    assert out.get("truncated") is True
    assert "preview" in out
