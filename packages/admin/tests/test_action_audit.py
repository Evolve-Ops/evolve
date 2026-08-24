"""tests/test_action_audit.py — action.audit.{run,mute,unmute} family.

These tools wrap admin-ui's Security → Audit surface so evo can act on
"re-run the audit", "mute that finding", "unmute that finding" without
trying to touch ``{shared_dir}/security/`` directly (that tree stays
evolve-only post-evo-account-separation, mirroring the HTTP-routing
pattern PR #1982 set for ``action.subscriptions.set``).

Tests cover the contract:
  - All three tools register at the expected dotted names, at
    ``write_safe`` tier, with a ``validate`` callable.
  - ``action.audit.run`` POSTs to ``/api/security/audit/refresh`` and
    URL-encodes ``?bot=<id>`` for bot-scoped runs.
  - ``action.audit.mute`` POSTs to ``/api/security/muted`` with
    ``{action: "mute", bot, message}`` and appends a record to
    ``{shared_dir}/audit-mutes-audit.jsonl``.
  - ``action.audit.unmute`` POSTs the unmute and surfaces the previous
    mute's reason from the journal (or None).
  - Malformed inputs (scope, finding_id, reason) are rejected before
    any HTTP call.
  - HTTP failures (500, unreachable) surface cleanly with no journal
    entry written.
  - Journal append is best-effort: mute still returns ok=True when the
    journal write fails (HTTP route is source of truth).

Closes the Security page gap from
``internal/audit-evo-tool-coverage-2026-06-02.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import action_audit  # noqa: E402


# ─── Test fixtures ──────────────────────────────────────────────────────────


_FAKE_BASE_URL = "http://127.0.0.1:5050"


def _make_request_stub(
    call_log: list[dict[str, Any]],
    *,
    status: int = 200,
    payload: dict[str, Any] | None = None,
    transport_err: str | None = None,
):
    """Build a ``RequestFn`` stub matching the action_audit.RequestFn
    signature: ``(method, url, body, timeout) -> (status, payload, err)``.
    Records each invocation into ``call_log``.
    """
    def stub(method: str, url: str, body: dict[str, Any] | None, timeout: int):
        call_log.append({
            "method": method,
            "url": url,
            "body": body,
            "timeout": timeout,
        })
        return status, (payload if payload is not None else {}), transport_err
    return stub


@pytest.fixture
def call_log() -> list[dict[str, Any]]:
    return []


# ─── Registration / manifest ────────────────────────────────────────────────


def test_action_audit_run_is_registered():
    tool = _tools.lookup("action.audit.run")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_audit_mute_is_registered():
    tool = _tools.lookup("action.audit.mute")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_action_audit_unmute_is_registered():
    tool = _tools.lookup("action.audit.unmute")
    assert tool is not None
    assert tool.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert tool.validate is not None


def test_audit_tools_in_manifest():
    """All three tools render with sensible input schemas."""
    manifest = _tools.build_tool_manifest()
    by_name = {e["name"]: e for e in manifest}

    run = by_name.get("action.audit.run")
    assert run is not None
    assert "scope" in run["input_schema"]["required"]

    mute = by_name.get("action.audit.mute")
    assert mute is not None
    assert "finding_id" in mute["input_schema"]["required"]
    assert "reason" in mute["input_schema"]["required"]

    unmute = by_name.get("action.audit.unmute")
    assert unmute is not None
    assert "finding_id" in unmute["input_schema"]["required"]


# ─── action.audit.run ───────────────────────────────────────────────────────


def test_run_pod_scope_posts_refresh_url(tmp_path, call_log):
    """pod-scope → POST /api/security/audit/refresh; no ?bot= query."""
    stub = _make_request_stub(call_log, status=200, payload={
        "status": "started", "total": 4,
    })
    result = action_audit._run_handler(
        shared_dir=tmp_path,
        scope="pod",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["scope"] == "pod"
    assert "started_at" in result
    assert "Pod-wide audit" in result["message"]

    assert len(call_log) == 1
    call = call_log[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{_FAKE_BASE_URL}/api/security/audit/refresh"
    # No ?bot= for pod scope.
    assert "?bot=" not in call["url"]

    # Run does not write to the journal.
    assert not (tmp_path / "audit-mutes-audit.jsonl").exists()


def test_run_bot_scope_includes_bot_query(tmp_path, call_log):
    """bot-scope → POST /api/security/audit/refresh?bot=<id>."""
    stub = _make_request_stub(call_log, status=200, payload={
        "status": "started", "total": 1,
    })
    result = action_audit._run_handler(
        shared_dir=tmp_path,
        scope="bot:team-bot-a",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["scope"] == "bot:team-bot-a"
    assert "team-bot-a" in result["message"]

    assert len(call_log) == 1
    assert call_log[0]["url"].endswith("?bot=team-bot-a")


def test_run_http_500_surfaces_error(tmp_path, call_log):
    """HTTP 500 → ok=False with http_status and the route's error
    message."""
    stub = _make_request_stub(
        call_log, status=500,
        payload={"error": "audit thread crashed"},
    )
    result = action_audit._run_handler(
        shared_dir=tmp_path,
        scope="pod",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "audit thread crashed" in result["error"]


def test_run_transport_unreachable_surfaces_error(tmp_path, call_log):
    """If the admin server is unreachable, the transport error string
    is surfaced verbatim."""
    stub = _make_request_stub(
        call_log, status=0, payload=None,
        transport_err=(
            f"admin server unreachable at {_FAKE_BASE_URL}: "
            "Connection refused"
        ),
    )
    result = action_audit._run_handler(
        shared_dir=tmp_path,
        scope="pod",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "unreachable" in result["error"].lower()
    assert "http_status" not in result


def test_run_validate_rejects_empty_scope(tmp_path):
    """validate gate refuses scope="" / None before any HTTP."""
    result = action_audit._run_validate(shared_dir=tmp_path, scope="")
    assert result["ok"] is False
    assert "scope" in result["reason"].lower()


def test_run_validate_rejects_unknown_scope_shape(tmp_path):
    """validate gate refuses scope="bogus"; mentions expected shape."""
    result = action_audit._run_validate(
        shared_dir=tmp_path, scope="everything",
    )
    assert result["ok"] is False
    assert "pod" in result["reason"] or "bot:" in result["reason"]


def test_run_handler_rejects_empty_bot_id(tmp_path, call_log):
    """scope='bot:' (empty id after prefix) → validate-style refusal,
    no HTTP call."""
    stub = _make_request_stub(call_log)
    result = action_audit._run_handler(
        shared_dir=tmp_path,
        scope="bot:",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "non-empty bot id" in result["error"]
    assert call_log == []


# ─── action.audit.mute ──────────────────────────────────────────────────────


def _journal_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit-mutes-audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_mute_success_writes_journal_with_reason(tmp_path, call_log):
    """HTTP 200 → ok=True, journal entry includes reason + finding_id."""
    stub = _make_request_stub(call_log, status=200, payload={"ok": True})
    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::Plugin X uses unpinned npm spec",
        reason="known false positive — team-bot-a pins via lockfile",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["bot_id"] == "team-bot-a"
    assert result["message"] == "Plugin X uses unpinned npm spec"
    assert result["reason"] == (
        "known false positive — team-bot-a pins via lockfile"
    )
    assert result["journaled"] is True

    # HTTP body shape
    assert len(call_log) == 1
    call = call_log[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{_FAKE_BASE_URL}/api/security/muted"
    assert call["body"] == {
        "action": "mute",
        "bot": "team-bot-a",
        "message": "Plugin X uses unpinned npm spec",
    }

    # Journal entry shape
    entries = _journal_lines(tmp_path)
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == "mute"
    assert rec["finding_id"] == (
        "team-bot-a::Plugin X uses unpinned npm spec"
    )
    assert rec["bot_id"] == "team-bot-a"
    assert rec["reason"] == (
        "known false positive — team-bot-a pins via lockfile"
    )
    assert rec["actor"].startswith("evo:")
    assert "at" in rec


def test_mute_missing_reason_rejected(tmp_path, call_log):
    """reason=None → handler refuses; no HTTP call, no journal write."""
    stub = _make_request_stub(call_log)
    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::some finding",
        reason=None,  # type: ignore[arg-type]
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "reason" in result["error"].lower()
    assert call_log == []
    assert _journal_lines(tmp_path) == []


def test_mute_whitespace_reason_rejected(tmp_path, call_log):
    """reason="   " → handler refuses (no silent mutes)."""
    stub = _make_request_stub(call_log)
    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::some finding",
        reason="   ",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "reason" in result["error"].lower()
    assert call_log == []


def test_mute_malformed_finding_id_rejected(tmp_path, call_log):
    """finding_id missing the '::' separator → handler refuses."""
    stub = _make_request_stub(call_log)
    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="no-separator-here",
        reason="real reason",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "malformed" in result["error"].lower()
    assert call_log == []


def test_mute_http_500_does_not_journal(tmp_path, call_log):
    """HTTP 500 → ok=False; the journal must NOT be written (the mute
    didn't actually land)."""
    stub = _make_request_stub(
        call_log, status=500,
        payload={"error": "muted.json write failed"},
    )
    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::Plugin X uses unpinned npm spec",
        reason="false positive",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "muted.json write failed" in result["error"]
    # Journal must NOT have an entry — we only journal after the HTTP
    # write succeeds.
    assert _journal_lines(tmp_path) == []


def test_mute_journal_failure_does_not_break_mute(tmp_path, call_log, monkeypatch):
    """If the journal append fails, mute still returns ok=True (HTTP
    route is source of truth) with journaled=False."""
    stub = _make_request_stub(call_log, status=200, payload={"ok": True})

    def boom(*args, **kwargs):
        return False  # journal append signals failure

    monkeypatch.setattr(action_audit, "_journal_append", boom)

    result = action_audit._mute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::finding",
        reason="real reason",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["journaled"] is False
    assert len(call_log) == 1


def test_mute_validate_rejects_missing_reason(tmp_path):
    """validate gate refuses calls without a real reason."""
    result = action_audit._mute_validate(
        shared_dir=tmp_path,
        finding_id="team-bot-a::finding",
        reason="",
    )
    assert result["ok"] is False
    assert "reason" in result["reason"].lower()


def test_mute_validate_rejects_malformed_finding_id(tmp_path):
    result = action_audit._mute_validate(
        shared_dir=tmp_path,
        finding_id="missing-separator",
        reason="real reason",
    )
    assert result["ok"] is False
    assert "finding_id" in result["reason"]


# ─── action.audit.unmute ────────────────────────────────────────────────────


def test_unmute_success_returns_previous_mute(tmp_path, call_log):
    """When the journal has a prior mute record, unmute returns
    previous_mute populated with reason + timestamp."""
    # Prime the journal with a mute record.
    finding_id = "team-bot-a::Plugin X uses unpinned npm spec"
    pre_mute = {
        "at": "2026-06-01T12:00:00Z",
        "actor": "evo:action.audit.mute",
        "action": "mute",
        "finding_id": finding_id,
        "bot_id": "team-bot-a",
        "message": "Plugin X uses unpinned npm spec",
        "reason": "known false positive",
    }
    (tmp_path / "audit-mutes-audit.jsonl").write_text(
        json.dumps(pre_mute) + "\n", encoding="utf-8",
    )

    stub = _make_request_stub(call_log, status=200, payload={"ok": True})
    result = action_audit._unmute_handler(
        shared_dir=tmp_path,
        finding_id=finding_id,
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["bot_id"] == "team-bot-a"
    assert result["previous_mute"] is not None
    assert result["previous_mute"]["reason"] == "known false positive"
    assert result["previous_mute"]["at"] == "2026-06-01T12:00:00Z"

    # HTTP body shape
    assert len(call_log) == 1
    call = call_log[0]
    assert call["url"] == f"{_FAKE_BASE_URL}/api/security/muted"
    assert call["body"] == {
        "action": "unmute",
        "bot": "team-bot-a",
        "message": "Plugin X uses unpinned npm spec",
    }

    # The unmute also appends to the journal for audit-trail consistency.
    entries = _journal_lines(tmp_path)
    # 1 pre-existing mute + 1 new unmute.
    assert len(entries) == 2
    assert entries[-1]["action"] == "unmute"


def test_unmute_with_no_journal_history_returns_null_previous(tmp_path, call_log):
    """If the journal has no record for this finding (operator muted
    via UI before journaling existed), previous_mute is None and the
    unmute still succeeds."""
    stub = _make_request_stub(call_log, status=200, payload={"ok": True})
    result = action_audit._unmute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::some finding never journaled",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["previous_mute"] is None
    assert len(call_log) == 1


def test_unmute_http_500_surfaces_error(tmp_path, call_log):
    stub = _make_request_stub(
        call_log, status=500,
        payload={"error": "route exploded"},
    )
    result = action_audit._unmute_handler(
        shared_dir=tmp_path,
        finding_id="team-bot-a::finding",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert result["http_status"] == 500
    assert "route exploded" in result["error"]


def test_unmute_malformed_finding_id_rejected(tmp_path, call_log):
    stub = _make_request_stub(call_log)
    result = action_audit._unmute_handler(
        shared_dir=tmp_path,
        finding_id="no-separator",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is False
    assert "malformed" in result["error"].lower()
    assert call_log == []


def test_unmute_uses_last_mute_semantics(tmp_path, call_log):
    """_journal_find_last_mute returns the most-recent mute NOT
    followed by an unmute. Sequence: mute → unmute → mute → unmute()
    finds the second mute."""
    finding_id = "team-bot-a::Repeating finding"
    journal = tmp_path / "audit-mutes-audit.jsonl"
    journal.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-05-01T00:00:00Z",
                "action": "mute",
                "finding_id": finding_id,
                "bot_id": "team-bot-a",
                "reason": "first reason (stale)",
            }),
            json.dumps({
                "at": "2026-05-15T00:00:00Z",
                "action": "unmute",
                "finding_id": finding_id,
                "bot_id": "team-bot-a",
            }),
            json.dumps({
                "at": "2026-06-01T00:00:00Z",
                "action": "mute",
                "finding_id": finding_id,
                "bot_id": "team-bot-a",
                "reason": "current reason",
            }),
            "",
        ]),
        encoding="utf-8",
    )

    stub = _make_request_stub(call_log, status=200, payload={"ok": True})
    result = action_audit._unmute_handler(
        shared_dir=tmp_path,
        finding_id=finding_id,
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )

    assert result["ok"] is True
    assert result["previous_mute"]["reason"] == "current reason"
    assert result["previous_mute"]["at"] == "2026-06-01T00:00:00Z"


# ─── Factory binding ────────────────────────────────────────────────────────


def test_make_run_handler_binds_shared_dir(tmp_path, call_log):
    """make_run_handler returns a closure that captures shared_dir +
    network_path; the resulting handler accepts model-facing kwargs
    only (scope here)."""
    handler = action_audit.make_run_handler(tmp_path, network_path=None)
    stub = _make_request_stub(call_log, status=200, payload={
        "status": "started", "total": 1,
    })

    # Closure binding works: the bound handler still accepts scope and
    # the request_fn override for testing.
    result = handler(
        scope="pod",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is True
    assert len(call_log) == 1


def test_make_mute_handler_binds_shared_dir(tmp_path, call_log):
    handler = action_audit.make_mute_handler(tmp_path, network_path=None)
    stub = _make_request_stub(call_log, status=200, payload={"ok": True})

    result = handler(
        finding_id="team-bot-a::finding",
        reason="real reason",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is True
    # Journal landed under the bound shared_dir.
    assert (tmp_path / "audit-mutes-audit.jsonl").exists()


def test_make_unmute_handler_binds_shared_dir(tmp_path, call_log):
    handler = action_audit.make_unmute_handler(tmp_path, network_path=None)
    stub = _make_request_stub(call_log, status=200, payload={"ok": True})

    result = handler(
        finding_id="team-bot-a::finding",
        request_fn=stub,
        base_url=_FAKE_BASE_URL,
    )
    assert result["ok"] is True
    assert len(call_log) == 1
