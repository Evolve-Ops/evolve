"""tests/test_empty_reply_session_jsonl_fallback.py — Phase 1 of the
surface-aware help-style spec + Priority 1 of the empty-reply
diagnosis (docs/diagnosis-empty-reply-after-successful-tool-calls-2026-05-21.md).

When OC's agent loop terminates mid-flight after emitting tool calls
(file-lock contention is the recurring cause), ``payloads[0].text``
is empty even though the work succeeded. The proxy reads the OC
session JSONL for the failed run, lists the toolCall + toolResult
pairs, and synthesizes a yellow-bubble confirmation instead of
surfacing ``(evo returned an empty reply)`` as a red error.

This file covers:

  * ``read_run_tool_calls`` returns the right shape from a fixture
    JSONL filtered by ``run_id``.
  * ``send_to_evo`` falls back to the synthesized confirmation when
    OC returned empty text but tool calls were recorded.
  * ``send_to_evo`` falls back to the legacy placeholder when no
    tool calls were recorded (truly-empty case).
  * The route handler maps ``empty_reply`` to ``source: "proxy_warn"``
    (yellow bubble) — not ``proxy_error`` (red).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import proxy as P  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# read_run_tool_calls — pairs toolCall + toolResult records by run_id
# ─────────────────────────────────────────────────────────────────────────────


def _write_session_jsonl(tmp_path: Path, session_id: str, lines: list[dict]):
    sd = tmp_path / "sessions"
    sd.mkdir(exist_ok=True)
    f = sd / f"{session_id}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    return sd


def test_read_run_tool_calls_returns_pairs_for_run_id(tmp_path):
    """The JSONL contains records from two distinct run_ids; only the
    target run's pairs are returned."""
    sid = "admin-ui-recommendations"
    target_run = "r-success"
    other_run = "r-other"
    lines = [
        # toolCall in target run
        {
            "type": "message", "runId": target_run,
            "message": {
                "role": "assistant", "timestamp": 1000,
                "content": [{
                    "type": "tool_use", "id": "tc-1",
                    "name": "evo_tools__action-proposal-apply",
                }],
            },
        },
        # toolResult in target run — pairs with tc-1
        {
            "type": "message", "runId": target_run,
            "message": {
                "role": "toolResult", "timestamp": 1500,
                "toolCallId": "tc-1",
                "content": [{"type": "text", "text": '{"ok": true, "to_state": "applied"}'}],
            },
        },
        # toolCall + toolResult in a DIFFERENT run — must be filtered out
        {
            "type": "message", "runId": other_run,
            "message": {
                "role": "assistant", "timestamp": 2000,
                "content": [{
                    "type": "tool_use", "id": "tc-2",
                    "name": "evo_tools__action-signal-snooze",
                }],
            },
        },
        {
            "type": "message", "runId": other_run,
            "message": {
                "role": "toolResult", "timestamp": 2500,
                "toolCallId": "tc-2",
                "content": [{"type": "text", "text": '{"ok": true}'}],
            },
        },
    ]
    sd = _write_session_jsonl(tmp_path, sid, lines)
    out = P.read_run_tool_calls(sid, target_run, sessions_dir=sd)
    assert len(out) == 1
    assert out[0]["tool"] == "action.proposal.apply"
    assert out[0]["outcome"] == "ok"
    # signal-snooze (other run) must NOT appear
    assert all("snooze" not in c["tool"] for c in out)


def test_read_run_tool_calls_no_run_id_returns_empty(tmp_path):
    """Without a run_id we can't scope to a single turn — return
    empty so the caller falls back to the legacy placeholder."""
    sid = "x"
    sd = _write_session_jsonl(tmp_path, sid, [])
    assert P.read_run_tool_calls(sid, None, sessions_dir=sd) == []


def test_read_run_tool_calls_no_session_file_returns_empty(tmp_path):
    """Missing session JSONL → empty. Never raise — the synthesized
    fallback is opportunistic; absence is fine, just falls back to
    the placeholder."""
    out = P.read_run_tool_calls("missing", "r1", sessions_dir=tmp_path)
    assert out == []


def test_read_run_tool_calls_surfaces_unpaired_tool_use_as_no_result(tmp_path):
    """The file-lock-contention case: tool_use was written but no
    toolResult landed (or it landed in a different turn). Surface the
    tool_use as ``no_result`` so the synthesized message names that
    state explicitly — operator can tell "OC race ate the result"
    from "tool genuinely failed"."""
    sid = "x"
    run = "r-mid-flight"
    lines = [
        {
            "type": "message", "runId": run,
            "message": {
                "role": "assistant", "timestamp": 1000,
                "content": [{
                    "type": "tool_use", "id": "tc-orphan",
                    "name": "evo_tools__action-proposal-apply",
                }],
            },
        },
    ]
    sd = _write_session_jsonl(tmp_path, sid, lines)
    out = P.read_run_tool_calls(sid, run, sessions_dir=sd)
    assert len(out) == 1
    assert out[0]["outcome"] == "no_result"


def test_read_run_tool_calls_records_error_outcome(tmp_path):
    """A toolResult with ``isError: true`` records outcome=error so
    the synthesized text can name failures separately."""
    sid = "x"
    run = "r"
    lines = [
        {
            "type": "message", "runId": run,
            "message": {
                "role": "assistant", "timestamp": 1000,
                "content": [{
                    "type": "tool_use", "id": "tc",
                    "name": "evo_tools__action-proposal-apply",
                }],
            },
        },
        {
            "type": "message", "runId": run, "isError": True,
            "message": {
                "role": "toolResult", "timestamp": 1500,
                "toolCallId": "tc",
                "content": [{"type": "text", "text": "file lock stale"}],
            },
        },
    ]
    sd = _write_session_jsonl(tmp_path, sid, lines)
    out = P.read_run_tool_calls(sid, run, sessions_dir=sd)
    assert out[0]["outcome"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# _synthesize_empty_reply_text — operator-facing yellow-bubble text
# ─────────────────────────────────────────────────────────────────────────────


def test_synthesize_empty_reply_text_names_counts_and_calls():
    """The synthesized text must say 'evo ran N tool calls', list
    succeeded/error/no_result counts, and enumerate each call. Without
    this the operator has no ground truth about what happened."""
    tool_calls = [
        {"tool": "action.proposal.apply", "outcome": "ok",
         "summary": "ok=True"},
        {"tool": "action.proposal.apply", "outcome": "error",
         "summary": "file lock stale"},
        {"tool": "action.proposal.apply", "outcome": "no_result",
         "summary": "(no result recorded)"},
    ]
    text = P._synthesize_empty_reply_text(tool_calls)
    assert "3 tool call" in text
    assert "1 succeeded" in text
    assert "1 returned an error" in text
    assert "1 have no recorded result" in text
    # Each call shows up
    assert text.count("`action.proposal.apply`") == 3


def test_synthesize_empty_reply_text_empty_list_returns_empty():
    """No tool calls → no synthesized text; caller falls back to the
    legacy placeholder."""
    assert P._synthesize_empty_reply_text([]) == ""


# ─────────────────────────────────────────────────────────────────────────────
# send_to_evo — empty-reply branch wires the fallback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Replaces subprocess.run with a recorder, same shape as the
    other proxy test fixture."""
    class Recorder:
        def __init__(self):
            self.calls: list[tuple[list[str], dict]] = []
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

        def __call__(self, cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(
                args=cmd, returncode=self.returncode,
                stdout=self.stdout, stderr=self.stderr,
            )

    rec = Recorder()
    monkeypatch.setattr(P.subprocess, "run", rec)
    return rec


def test_send_to_evo_synthesizes_confirmation_when_tool_calls_ran(
    patched_subprocess, tmp_path, monkeypatch,
):
    """OC returns rc=0 with empty payload AND tool calls exist in the
    session JSONL for this run → ProxyResult.text is the synthesized
    confirmation, error="empty_reply"."""
    # OC returns empty payload but a real run_id
    patched_subprocess.stdout = json.dumps({
        "runId": "r-empty-but-ran",
        "result": {"payloads": [], "meta": {"agentMeta": {"model": "m"}}},
    })

    monkeypatch.setattr(P, "read_run_tool_calls", lambda sid, rid, **kw: [
        {"tool": "action.proposal.apply", "outcome": "ok",
         "summary": "applied"},
        {"tool": "action.proposal.apply", "outcome": "error",
         "summary": "file lock stale"},
    ])

    result = P.send_to_evo(
        "apply the proposals",
        session_id="admin-ui-recommendations",
        network_path=tmp_path / "n.json",
    )
    assert result.error == "empty_reply"
    assert "evo ran 2 tool calls" in result.text
    assert "1 succeeded" in result.text
    assert "1 returned an error" in result.text
    # The placeholder text MUST NOT appear — that's the legacy case
    assert "(evo returned an empty reply)" not in result.text


def test_send_to_evo_falls_back_to_placeholder_when_no_tool_calls(
    patched_subprocess, tmp_path, monkeypatch,
):
    """OC returns rc=0 with empty payload AND no tool calls recorded
    → legacy placeholder. This is the truly-empty case (model
    genuinely chose to be silent without acting)."""
    patched_subprocess.stdout = json.dumps({
        "runId": "r-nothing-happened",
        "result": {"payloads": [], "meta": {"agentMeta": {"model": "m"}}},
    })
    monkeypatch.setattr(P, "read_run_tool_calls", lambda sid, rid, **kw: [])

    result = P.send_to_evo(
        "hi",
        session_id="admin-ui-recommendations",
        network_path=tmp_path / "n.json",
    )
    assert result.error == "empty_reply"
    assert result.text == "(evo returned an empty reply)"


# ─────────────────────────────────────────────────────────────────────────────
# Route handler taxonomy — empty_reply → proxy_warn, not proxy_error
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {"evolve": {"role": "primary"}},
        "members": ["evolve"],
    }))
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from evolve_admin.web.server import create_app
    return create_app(net)


def test_route_maps_empty_reply_to_proxy_warn(tmp_path, monkeypatch):
    """The yellow-bubble UX hinges on this taxonomy split. ``empty_reply``
    must surface as ``source: "proxy_warn"`` so the chat-drawer JS
    can render it with warning (not error) styling."""
    from evolve_admin.evo import proxy as _proxy

    def fake_send(message, *, session_id, network_path, **kw):
        return _proxy.ProxyResult(
            text="(synthesized)", session_id=session_id,
            error="empty_reply", model="m", run_id="r1",
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "hi"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "proxy_warn"
    assert body["error"] == "empty_reply"
    # The synthesized text rides along on the reply
    assert body["reply"] == "(synthesized)"


def test_route_still_maps_other_errors_to_proxy_error(tmp_path, monkeypatch):
    """The taxonomy split is empty_reply-specific. Subprocess /
    OC-binary / rc!=0 failures still surface as ``proxy_error`` (red
    bubble)."""
    from evolve_admin.evo import proxy as _proxy

    def fake_send(message, *, session_id, network_path, **kw):
        return _proxy.ProxyResult(
            text="evo's gateway returned an error.",
            session_id=session_id,
            error="openclaw_rc=7: gateway not running",
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "hi"})
    body = r.get_json()
    assert body["source"] == "proxy_error"
    assert body["error"].startswith("openclaw_rc=7")
