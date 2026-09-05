"""Tests for the bot-side repair runner.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.

Covers:
  - parse_repair_decisions tolerates fenced JSON / malformed bodies.
  - reinstall_cron_from_manifest is idempotent — no-op when crontab is healthy.
  - reinstall_cron_from_manifest installs missing entries and refuses to
    overwrite when crontab -l fails.
  - run_repair routes LLM picks through the allowlist; out-of-allowlist
    kinds bubble up as Proposals rather than executing.
  - process_repair_inbox loads requests, runs them, writes outbox files,
    archives inbox files.
  - check_rate_limit refusal writes a repair_failed outbox without
    dispatching the LLM.
  - Idempotency: running the same request twice produces the same on-disk
    state (cron is no-op the second time).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_repair_runner as rr  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    for sub in (
        "manifests",
        "evolve",
        "evolve/audits",
        "evolve/audit_outbox",
        "evolve/audit_inbox",
    ):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def _make_request(
    *,
    app_id: str = "myapp",
    request_id: str = "repair-abc12345",
    manifest: dict | None = None,
    findings: list | None = None,
) -> dict:
    if manifest is None:
        manifest = {
            "id":    app_id,
            "name":  "My App",
            "crons": [
                {"schedule": "0 6 * * *",
                 "script":   "python3 /tmp/myapp/run.py"},
            ],
        }
    return {
        "request_id":         request_id,
        "app_id":             app_id,
        "findings":           findings or [
            {"finding_id": "f1",
             "source":     "coherence",
             "kind":       "missing_cron",
             "evidence":   {"schedule": "0 6 * * *"}},
        ],
        "operator_rationale": "the 6am job stopped firing",
        "operator_intent":    "restore",
        "context": {
            "manifest_snapshot": manifest,
            "recent_trail":      [],
            "last_test_run":     {},
            "last_audit":        {},
        },
        "created_at": "2026-06-06T12:00:00Z",
    }


# ── parse_repair_decisions ────────────────────────────────────────────────


def test_parse_repair_decisions_plain_json():
    text = json.dumps({
        "decisions": [
            {"finding_id": "f1", "action": "apply",
             "kind": "reinstall_cron_from_manifest", "params": {}},
        ],
    })
    out = rr.parse_repair_decisions(text)
    assert len(out) == 1
    assert out[0]["kind"] == "reinstall_cron_from_manifest"


def test_parse_repair_decisions_fenced_json():
    text = (
        "```json\n"
        + json.dumps({"decisions": [{"finding_id": "x", "action": "propose",
                                     "kind": "redesign", "params": {}}]})
        + "\n```"
    )
    out = rr.parse_repair_decisions(text)
    assert len(out) == 1
    assert out[0]["action"] == "propose"


def test_parse_repair_decisions_prose_wrapped():
    text = (
        "Here is my plan:\n\n"
        + json.dumps({"decisions": [{"finding_id": "y", "action": "apply",
                                     "kind": "k", "params": {}}]})
        + "\n\nLet me know."
    )
    out = rr.parse_repair_decisions(text)
    assert len(out) == 1


def test_parse_repair_decisions_empty_text():
    assert rr.parse_repair_decisions("") == []


def test_parse_repair_decisions_malformed_returns_empty():
    # JSON-ish object without 'decisions' parses but returns [].
    out = rr.parse_repair_decisions(json.dumps({"thoughts": "no decisions key"}))
    assert out == []

    # Truncated body with no closing brace — regex finds nothing, returns [].
    out = rr.parse_repair_decisions(
        '{"decisions": [{"finding_id": "a", "kind": "k"  # bad'
    )
    assert out == []


def test_parse_repair_decisions_bad_decisions_type_raises():
    """Decisions key present but not a list → ValueError."""
    text = json.dumps({"decisions": "not a list"})
    with pytest.raises(ValueError):
        rr.parse_repair_decisions(text)


# ── Cron reinstall transformation ─────────────────────────────────────────


def _stub_crontab(monkeypatch, *, live_lines: list[str], ok: bool = True):
    """Replace _read_crontab + _write_crontab with stubs that don't shell out.

    Returns a dict the test can inspect to see what was written.
    """
    captured: dict = {"written": None}

    def _fake_read():
        return list(live_lines), ok

    def _fake_write(content: str):
        captured["written"] = content
        return True, ""

    monkeypatch.setattr(rr, "_read_crontab", _fake_read)
    monkeypatch.setattr(rr, "_write_crontab", _fake_write)
    return captured


def test_reinstall_cron_noop_when_all_present(monkeypatch):
    """Idempotent when manifest crons match live crontab — no write."""
    captured = _stub_crontab(monkeypatch, live_lines=[
        "0 6 * * * python3 /tmp/myapp/run.py",
    ])
    manifest = {
        "crons": [{"schedule": "0 6 * * *",
                   "script":   "python3 /tmp/myapp/run.py"}],
    }
    res = rr._apply_reinstall_cron_from_manifest(manifest, Path("/tmp"), {})
    assert res.applied is False
    assert "no missing cron entries" in res.summary
    assert captured["written"] is None


def test_reinstall_cron_installs_missing(monkeypatch):
    """Adds missing entries while preserving live lines."""
    captured = _stub_crontab(monkeypatch, live_lines=[
        "# user comment",
        "0 9 * * * python3 /tmp/other/run.py",
    ])
    manifest = {
        "crons": [
            {"schedule": "0 6 * * *",
             "script":   "python3 /tmp/myapp/run.py"},
            {"schedule": "0 12 * * *",
             "script":   "python3 /tmp/myapp/noon.py"},
        ],
    }
    res = rr._apply_reinstall_cron_from_manifest(manifest, Path("/tmp"), {})
    assert res.applied is True
    assert "reinstalled 2" in res.summary
    assert "0 6 * * * python3 /tmp/myapp/run.py" in captured["written"]
    assert "0 12 * * * python3 /tmp/myapp/noon.py" in captured["written"]
    # Live lines preserved.
    assert "# user comment" in captured["written"]
    assert "0 9 * * * python3 /tmp/other/run.py" in captured["written"]


def test_reinstall_cron_refuses_when_crontab_unreadable(monkeypatch):
    """Don't overwrite when we can't read the current crontab."""
    _stub_crontab(monkeypatch, live_lines=[], ok=False)
    manifest = {
        "crons": [{"schedule": "0 6 * * *",
                   "script":   "python3 /tmp/myapp/run.py"}],
    }
    res = rr._apply_reinstall_cron_from_manifest(manifest, Path("/tmp"), {})
    assert res.applied is False
    assert "crontab -l unavailable" in res.summary


def test_reinstall_cron_noop_when_manifest_empty(monkeypatch):
    _stub_crontab(monkeypatch, live_lines=[])
    res = rr._apply_reinstall_cron_from_manifest({}, Path("/tmp"), {})
    assert res.applied is False
    assert "no crons" in res.summary


def test_reinstall_cron_soft_match_skips(monkeypatch):
    """A crontab line wrapped with cd/env still satisfies a manifest entry."""
    _stub_crontab(monkeypatch, live_lines=[
        "0 6 * * * cd $HOME && python3 /tmp/myapp/run.py",
    ])
    manifest = {
        "crons": [{"schedule": "0 6 * * *",
                   "script":   "python3 /tmp/myapp/run.py"}],
    }
    res = rr._apply_reinstall_cron_from_manifest(manifest, Path("/tmp"), {})
    assert res.applied is False
    assert "no missing cron entries" in res.summary


# ── Stub transformations bubble as Proposal-friendly refusals ─────────────


@pytest.mark.parametrize("kind", [
    "reembed_heartbeat_section",
    "restore_file_from_git_history",
    "update_files_sha_after_drift_approval",
    "remove_unclaimed_crontab_entry",
    "rename_files_path",
])
def test_stub_transformations_refuse(kind):
    fn = rr._TRANSFORMATION_REGISTRY[kind]
    res = fn({}, Path("/tmp"), {})
    assert res.applied is False
    assert "not yet implemented" in res.summary


# ── run_repair end-to-end ─────────────────────────────────────────────────


def _stub_llm(monkeypatch, decisions: list[dict], *, tokens: int = 1234,
              error: str = ""):
    """Replace _dispatch_repair_llm with a fixed response."""
    text = json.dumps({"decisions": decisions})

    def _fake_dispatch(system_prompt, user_message, *, bot_id, shared_dir):
        return text, tokens, 0.0, error

    monkeypatch.setattr(rr, "_dispatch_repair_llm", _fake_dispatch)


def test_run_repair_applies_allowed_transformation(tmp_path: Path, monkeypatch):
    ws = _make_workspace(tmp_path)
    request = _make_request()

    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "apply",
        "kind":       "reinstall_cron_from_manifest",
        "params":     {},
        "rationale":  "manifest is authoritative",
    }])
    captured = _stub_crontab(monkeypatch, live_lines=[])

    result = rr.run_repair(
        request, workspace=ws, bot_id="myapp_bot",
        shared_dir=tmp_path / "shared",
    )
    assert result.status == "ok"
    assert len(result.applied_transformations) == 1
    assert result.applied_transformations[0]["kind"] == "reinstall_cron_from_manifest"
    assert result.proposals == []
    assert captured["written"] is not None


def test_run_repair_rejects_non_allowlist_kind(tmp_path: Path, monkeypatch):
    """LLM picks a kind that's not in ALLOWED_TRANSFORMATIONS → Proposal."""
    ws = _make_workspace(tmp_path)
    request = _make_request()

    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "apply",
        "kind":       "rm_rf_slash",   # not in allowlist
        "params":     {"path": "/"},
        "rationale":  "trying to be sneaky",
    }])
    # If the executor were dispatched we'd see a crontab write — assert it isn't.
    _stub_crontab(monkeypatch, live_lines=[])

    result = rr.run_repair(
        request, workspace=ws, bot_id="myapp_bot",
        shared_dir=tmp_path / "shared",
    )
    assert result.status == "ok"
    assert result.applied_transformations == []
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "transformation_rejected"
    assert result.proposals[0]["attempted_kind"] == "rm_rf_slash"


def test_run_repair_passes_through_propose_action(tmp_path: Path, monkeypatch):
    """LLM says 'propose' — pass through to proposals[]."""
    ws = _make_workspace(tmp_path)
    request = _make_request()

    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "propose",
        "kind":       "rewrite_integration",
        "params":     {},
        "rationale":  "needs design discussion",
    }])

    result = rr.run_repair(
        request, workspace=ws, bot_id="myapp_bot",
        shared_dir=tmp_path / "shared",
    )
    assert result.status == "ok"
    assert result.applied_transformations == []
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "rewrite_integration"


def test_run_repair_llm_dispatch_error_fails(tmp_path: Path, monkeypatch):
    ws = _make_workspace(tmp_path)
    request = _make_request()
    _stub_llm(monkeypatch, [], error="dispatch timeout")
    result = rr.run_repair(
        request, workspace=ws, bot_id="myapp_bot",
        shared_dir=tmp_path / "shared",
    )
    assert result.status == "failed"
    assert "dispatch timeout" in result.error


def test_run_repair_executor_refusal_becomes_proposal(tmp_path: Path, monkeypatch):
    """LLM picked a real transformation but executor refused → Proposal."""
    ws = _make_workspace(tmp_path)
    request = _make_request(manifest={"id": "myapp", "crons": []})  # no crons

    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "apply",
        "kind":       "reinstall_cron_from_manifest",
        "params":     {},
    }])
    _stub_crontab(monkeypatch, live_lines=[])

    result = rr.run_repair(
        request, workspace=ws, bot_id="myapp_bot",
        shared_dir=tmp_path / "shared",
    )
    assert result.status == "ok"
    assert result.applied_transformations == []
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "transformation_refused"


def test_run_repair_missing_required_fields():
    """Defense-in-depth: bad input → failed result."""
    res = rr.run_repair(
        {"request_id": "r1"}, workspace=Path("/tmp"),
        bot_id="b", shared_dir=Path("/tmp/shared"),
    )
    assert res.status == "failed"
    assert "no app_id" in res.error or "no findings" in res.error


# ── process_repair_inbox + rate limit + idempotency ───────────────────────


def test_process_repair_inbox_writes_outbox(tmp_path: Path, monkeypatch):
    ws = _make_workspace(tmp_path)
    inbox_dir = ws / "evolve" / "audit_inbox"
    outbox_dir = ws / "evolve" / "audit_outbox"

    request = _make_request()
    (inbox_dir / f"{request['request_id']}.json").write_text(
        json.dumps(request),
    )

    # Stub the bot_workspace + LLM + crontab so we exercise the real flow.
    monkeypatch.setattr(
        "app_audit_runner._bot_workspace", lambda: ws, raising=False,
    )
    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "apply",
        "kind":       "reinstall_cron_from_manifest",
        "params":     {},
    }])
    _stub_crontab(monkeypatch, live_lines=[])

    result = rr.process_repair_inbox(
        ws, bot_id="myapp_bot", shared_dir=tmp_path / "shared",
    )
    assert result["processed"] == 1
    assert result["applied"] == 1
    assert result["failed"] == 0

    # Outbox should carry one repair-applied-*.json
    outbox_files = list(outbox_dir.glob("repair-applied-*.json"))
    assert len(outbox_files) == 1
    payload = json.loads(outbox_files[0].read_text())
    assert payload["kind"] == "repair_applied"
    assert payload["status"] == "ok"
    assert payload["request_id"] == request["request_id"]

    # Inbox should have been archived.
    assert not (inbox_dir / f"{request['request_id']}.json").exists()


def test_process_repair_inbox_idempotent(tmp_path: Path, monkeypatch):
    """Running the same request twice produces matching applied outcomes —
    the second run sees the cron already installed and no-ops at the
    executor layer (no double-write)."""
    ws = _make_workspace(tmp_path)
    inbox_dir = ws / "evolve" / "audit_inbox"
    outbox_dir = ws / "evolve" / "audit_outbox"

    request = _make_request(request_id="repair-aaaa1111")

    _stub_llm(monkeypatch, [{
        "finding_id": "f1",
        "action":     "apply",
        "kind":       "reinstall_cron_from_manifest",
        "params":     {},
    }])

    # Simulate a "growing" crontab — first run sees empty, second run sees
    # the line we'd have written.
    state = {"live": []}

    def _fake_read():
        return list(state["live"]), True

    def _fake_write(content):
        state["live"] = [
            ln for ln in content.splitlines() if ln.strip()
        ]
        return True, ""

    monkeypatch.setattr(rr, "_read_crontab", _fake_read)
    monkeypatch.setattr(rr, "_write_crontab", _fake_write)

    # First run.
    (inbox_dir / f"{request['request_id']}.json").write_text(json.dumps(request))
    rr.process_repair_inbox(
        ws, bot_id="myapp_bot", shared_dir=tmp_path / "shared",
    )

    # Second run with the same request_id — gets dropped into the inbox
    # again, processed, but the cron is already installed so we expect
    # no transformation applied (the proposal-refused path).
    (inbox_dir / f"{request['request_id']}.json").write_text(json.dumps(request))
    result2 = rr.process_repair_inbox(
        ws, bot_id="myapp_bot", shared_dir=tmp_path / "shared",
    )

    # Second run: still processed, but the transformation didn't apply
    # (idempotent no-op at the executor layer means it bubbles as a
    # refused proposal so the operator can see what happened).
    assert result2["processed"] == 1
    # Crontab content should be unchanged — only one fresh line ever written.
    assert sum(1 for ln in state["live"] if "myapp/run.py" in ln) == 1


def test_process_repair_inbox_rate_limit_refusal(tmp_path: Path, monkeypatch):
    """Three recent outboxes for the same app → fourth request refused
    without dispatching the LLM."""
    ws = _make_workspace(tmp_path)
    inbox_dir = ws / "evolve" / "audit_inbox"
    outbox_dir = ws / "evolve" / "audit_outbox"

    # Pre-populate three recent repair outbox files for the same app.
    for i in range(3):
        rid = f"repair-existing-{i}"
        (outbox_dir / f"repair-applied-{rid}.json").write_text(json.dumps({
            "kind":       "repair_applied",
            "request_id": rid,
            "app_id":     "myapp",
            "status":     "ok",
        }))

    request = _make_request(request_id="repair-new-1")
    (inbox_dir / f"{request['request_id']}.json").write_text(json.dumps(request))

    # Sentinel: if the LLM dispatch fires, this raises.
    def _explode(*a, **kw):
        raise AssertionError("LLM dispatch should NOT fire when rate-limited")

    monkeypatch.setattr(rr, "_dispatch_repair_llm", _explode)

    result = rr.process_repair_inbox(
        ws, bot_id="myapp_bot", shared_dir=tmp_path / "shared",
    )
    assert result["processed"] == 0  # rate limit before run_repair
    assert result["failed"] == 1

    failed_files = list(outbox_dir.glob("repair-failed-*.json"))
    assert len(failed_files) == 1
    payload = json.loads(failed_files[0].read_text())
    assert payload["status"] == "failed"
    assert "rate limit" in payload["error"].lower()


def test_process_repair_inbox_ignores_non_repair_files(tmp_path: Path, monkeypatch):
    """audit_inbox/audit-req-foo.json (audit request, not repair) is left
    alone by --repair mode."""
    ws = _make_workspace(tmp_path)
    inbox_dir = ws / "evolve" / "audit_inbox"

    # An audit request (different prefix) and a repair request side by side.
    (inbox_dir / "audit-req-abc.json").write_text(json.dumps({
        "request_id": "audit-req-abc", "kind": "tier3_audit",
    }))
    repair_request = _make_request(request_id="repair-bbbb2222")
    (inbox_dir / f"{repair_request['request_id']}.json").write_text(
        json.dumps(repair_request),
    )

    _stub_llm(monkeypatch, [{
        "finding_id": "f1", "action": "apply",
        "kind": "reinstall_cron_from_manifest", "params": {},
    }])
    _stub_crontab(monkeypatch, live_lines=[])

    result = rr.process_repair_inbox(
        ws, bot_id="myapp_bot", shared_dir=tmp_path / "shared",
    )
    assert result["processed"] == 1
    # The non-repair request must still be in the inbox.
    assert (inbox_dir / "audit-req-abc.json").exists()


# ── Outbox record shape matches admin-side validator ──────────────────────


def test_outbox_record_passes_validator():
    """The runner's output should validate against repair_dispatch's
    validate_repair_outbox schema — the bridge between bot and admin
    is contract-compliant."""
    from evolve_admin.applications.repair_dispatch import (
        validate_repair_outbox,
    )

    result = rr.RepairResult(
        request_id="repair-test",
        app_id="myapp",
        status="ok",
        applied_transformations=[{
            "kind":         "reinstall_cron_from_manifest",
            "finding_id":   "f1",
            "summary":      "installed",
            "trail_entry":  {},
            "applied_at":   "2026-06-06T12:00:00Z",
        }],
        proposals=[],
    )
    record = result.to_outbox_record()
    ok, errors = validate_repair_outbox(record)
    assert ok, errors


def test_outbox_record_rejects_unknown_transformation_kind():
    """validate_repair_outbox rejects applied_transformations[] entries
    whose kind is outside ALLOWED_TRANSFORMATIONS."""
    from evolve_admin.applications.repair_dispatch import (
        validate_repair_outbox,
    )
    record = {
        "request_id": "r1",
        "status":     "ok",
        "applied_transformations": [
            {"kind": "rm_rf_slash", "finding_id": "f1"},
        ],
        "proposals": [],
    }
    ok, errors = validate_repair_outbox(record)
    assert ok is False
    assert any("ALLOWED_TRANSFORMATIONS" in e for e in errors)


# ── Allowlist stays in sync between bot + admin ───────────────────────────


def test_in_module_fallback_allowlist_matches_admin():
    """Drift detector: the bot-side fallback must agree with the admin's
    canonical ALLOWED_TRANSFORMATIONS. Otherwise a partial-install pod
    would silently disagree about what's a mechanical transformation."""
    from evolve_admin.applications.repair_dispatch import (
        ALLOWED_TRANSFORMATIONS,
    )
    assert rr._FALLBACK_ALLOWED_TRANSFORMATIONS == ALLOWED_TRANSFORMATIONS
