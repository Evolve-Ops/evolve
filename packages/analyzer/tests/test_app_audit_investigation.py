"""Tests for the bot-side investigation runner (Workstream C).

We stub the LLM dispatcher so tests are deterministic and don't shell
out to OpenClaw. The test inputs are realistic — a synthetic
protein-reminder-style failure scenario plus a clean baseline.

Spec: internal/spec-audit-extensions-2026-05-17.md §5.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow ``from app_audit_investigation import ...`` from the analyzer dir.
_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_investigation as inv_mod  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_bot_workspace(tmp_path: Path) -> Path:
    """Lay out a minimal bot workspace: manifests/ + evolve/."""
    ws = tmp_path / "bot_workspace"
    (ws / "manifests").mkdir(parents=True)
    (ws / "evolve" / "audits").mkdir(parents=True)
    (ws / "evolve" / "investigations").mkdir(parents=True)
    return ws


def _write_manifest(ws: Path, app_id: str, body: dict) -> Path:
    body.setdefault("id", app_id)
    p = ws / "manifests" / f"{app_id}.json"
    p.write_text(json.dumps(body))
    return p


def _make_shared(tmp_path: Path) -> Path:
    shared = tmp_path / "shared"
    (shared / "signals" / "firing").mkdir(parents=True)
    (shared / "signals" / "snoozed").mkdir(parents=True)
    (shared / "signals" / "log").mkdir(parents=True)
    (shared / "watchdog").mkdir(parents=True)
    return shared


def _make_stub_dispatcher(stage1_payload, stage2_payload):
    """Build a dispatcher that returns each stage's response in order.

    Signature matches what run_investigation expects:
      (system_prompt, user_message, *, timeout_s) → (text, tokens, error)
    """
    calls = {"count": 0}

    def _stub(system, user, *, timeout_s):
        calls["count"] += 1
        if calls["count"] == 1:
            return json.dumps(stage1_payload), 1234, ""
        return json.dumps(stage2_payload), 5678, ""

    return _stub, calls


# ── parse_triage_output ────────────────────────────────────────────────────


def test_parse_triage_handles_valid_output() -> None:
    raw = json.dumps({
        "candidates": [
            {
                "element_type": "app",
                "element_id": "morning-briefing",
                "confidence": "high",
                "justification": "scheduled action stopped at 7 AM",
            },
            {
                "element_type": "skill",
                "element_id": "gmail",
                "confidence": "medium",
                "justification": "token rotation in window",
            },
        ],
        "top_candidate": {
            "element_type": "app",
            "element_id": "morning-briefing",
            "confidence": "high",
            "justification": "scheduled action stopped",
        },
        "rationale": "The briefing app is the closest match.",
    })
    result = inv_mod.parse_triage_output(raw)
    assert len(result.candidates) == 2
    assert result.top_candidate is not None
    assert result.top_candidate.element_type == "app"
    assert result.top_candidate.element_id == "morning-briefing"
    assert result.top_candidate.confidence == "high"


def test_parse_triage_tolerates_malformed_json() -> None:
    result = inv_mod.parse_triage_output("not json at all { ")
    assert result.candidates == []
    assert result.top_candidate is None
    assert result.error == "triage_output_not_json"


def test_parse_triage_skips_invalid_element_types() -> None:
    raw = json.dumps({
        "candidates": [
            {"element_type": "made_up", "element_id": "x", "confidence": "high"},
            {"element_type": "app", "element_id": "ok", "confidence": "low"},
        ],
        "top_candidate": None,
        "rationale": "nothing clear",
    })
    result = inv_mod.parse_triage_output(raw)
    assert len(result.candidates) == 1
    assert result.candidates[0].element_id == "ok"


def test_parse_triage_strips_code_fences() -> None:
    """LLM sometimes wraps output in ```json ... ``` fences."""
    raw = (
        "```json\n"
        + json.dumps({
            "candidates": [],
            "top_candidate": None,
            "rationale": "ok",
        })
        + "\n```"
    )
    result = inv_mod.parse_triage_output(raw)
    assert result.rationale == "ok"


# ── parse_diagnosis_output ─────────────────────────────────────────────────


def test_parse_diagnosis_high_confidence() -> None:
    raw = json.dumps({
        "diagnosis": "Gmail token expired three days ago.",
        "suggested_fix": "Regenerate the gmail token in Settings.",
        "confidence": "high",
        "evidence": ["auth-profiles.json:gmail", "signal:abc123"],
        "what_i_checked": ["recent signals", "gmail trail"],
    })
    d = inv_mod.parse_diagnosis_output(raw)
    assert d.diagnosis == "Gmail token expired three days ago."
    assert d.suggested_fix == "Regenerate the gmail token in Settings."
    assert d.confidence == "high"
    assert len(d.evidence) == 2


def test_parse_diagnosis_null_diagnosis() -> None:
    raw = json.dumps({
        "diagnosis": None,
        "suggested_fix": "Try evo audit morning-briefing full.",
        "confidence": "low",
        "evidence": [],
        "what_i_checked": ["briefing trail", "gmail skill"],
    })
    d = inv_mod.parse_diagnosis_output(raw)
    assert d.diagnosis is None
    assert d.confidence == "low"
    assert "briefing trail" in d.what_i_checked


def test_parse_diagnosis_normalizes_invalid_confidence() -> None:
    raw = json.dumps({
        "diagnosis": "x",
        "confidence": "very-high",  # not valid
    })
    d = inv_mod.parse_diagnosis_output(raw)
    assert d.confidence == "low"


# ── run_investigation end-to-end ───────────────────────────────────────────


def test_run_investigation_high_confidence_e2e(tmp_path: Path) -> None:
    """Synthetic protein-reminder failure: Stage 1 picks app, Stage 2 diagnoses."""
    ws = _make_bot_workspace(tmp_path)
    shared = _make_shared(tmp_path)
    _write_manifest(ws, "morning-briefing", {
        "description": "Daily morning summary at 7 AM",
        "files": [{"path": "scripts/briefing.py", "purpose": "main"}],
        "scheduled_actions": [
            {
                "id": "morning-7am",
                "trigger": {"kind": "heartbeat", "schedule": "07:00 daily"},
                "summary": "Posts the morning briefing.",
            }
        ],
    })

    stage1 = {
        "candidates": [
            {
                "element_type": "app",
                "element_id": "morning-briefing",
                "confidence": "high",
                "justification": "scheduled action stopped",
            }
        ],
        "top_candidate": {
            "element_type": "app",
            "element_id": "morning-briefing",
            "confidence": "high",
            "justification": "scheduled action stopped",
        },
        "rationale": "Briefing is the natural candidate.",
    }
    stage2 = {
        "diagnosis": "The 7 AM heartbeat that triggers the briefing got clobbered.",
        "suggested_fix": "Restore HEARTBEAT.md from the last good commit.",
        "confidence": "high",
        "evidence": ["HEARTBEAT.md", "scripts/briefing.py:42"],
        "what_i_checked": [
            "morning-briefing manifest",
            "recent watchdog events",
        ],
    }
    dispatcher, calls = _make_stub_dispatcher(stage1, stage2)

    out = inv_mod.run_investigation(
        investigation_id="inv-test1",
        bot_id="team_bot_a", workspace=ws, shared_dir=shared,
        user_description="morning briefing didn't arrive today",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T10:00:00Z",
        dispatch_fn=dispatcher,
    )

    assert calls["count"] == 2
    assert out.status == "ok"
    assert out.chosen_candidate is not None
    assert out.chosen_candidate.element_id == "morning-briefing"
    assert "clobbered" in out.diagnosis.diagnosis
    assert out.diagnosis.confidence == "high"
    assert "Restore" in out.diagnosis.suggested_fix


def test_run_investigation_no_diagnosis_path(tmp_path: Path) -> None:
    """Stage 1 picks nothing → status='no_diagnosis', Stage 2 is skipped."""
    ws = _make_bot_workspace(tmp_path)
    shared = _make_shared(tmp_path)

    stage1 = {
        "candidates": [],
        "top_candidate": None,
        "rationale": "Nothing in the trail matches the description.",
    }
    dispatcher, calls = _make_stub_dispatcher(stage1, {})

    out = inv_mod.run_investigation(
        investigation_id="inv-test2",
        bot_id="team_bot_a", workspace=ws, shared_dir=shared,
        user_description="things feel off but I can't say what",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T10:00:00Z",
        dispatch_fn=dispatcher,
    )

    # Only Stage 1 should have been called.
    assert calls["count"] == 1
    assert out.status == "no_diagnosis"
    assert out.chosen_candidate is None
    assert out.diagnosis.diagnosis is None
    assert out.diagnosis.what_i_checked  # populated by fallback


def test_run_investigation_low_confidence_diagnosis_is_still_ok(
    tmp_path: Path,
) -> None:
    """Diagnosis present + low confidence → status='ok', renderer chooses template."""
    ws = _make_bot_workspace(tmp_path)
    shared = _make_shared(tmp_path)

    stage1 = {
        "candidates": [
            {"element_type": "skill", "element_id": "gmail",
             "confidence": "low", "justification": "guess"}
        ],
        "top_candidate": {
            "element_type": "skill", "element_id": "gmail",
            "confidence": "low", "justification": "guess",
        },
        "rationale": "rough guess",
    }
    stage2 = {
        "diagnosis": "Probably gmail; not sure.",
        "suggested_fix": "Run a full gmail skill audit.",
        "confidence": "low",
        "evidence": [],
        "what_i_checked": ["gmail audit trail"],
    }
    dispatcher, _ = _make_stub_dispatcher(stage1, stage2)

    out = inv_mod.run_investigation(
        investigation_id="inv-test3",
        bot_id="team_bot_a", workspace=ws, shared_dir=shared,
        user_description="emails feel slow",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T10:00:00Z",
        dispatch_fn=dispatcher,
    )
    # Diagnosis present → status ok, even at low confidence. The
    # renderer is the one that decides which template to use.
    assert out.status == "ok"
    assert out.diagnosis.diagnosis is not None
    assert out.diagnosis.confidence == "low"


def test_run_investigation_dispatcher_exception_returns_failed(
    tmp_path: Path,
) -> None:
    ws = _make_bot_workspace(tmp_path)
    shared = _make_shared(tmp_path)

    def _bad_dispatch(system, user, *, timeout_s):
        raise RuntimeError("LLM exploded")

    out = inv_mod.run_investigation(
        investigation_id="inv-fail",
        bot_id="team_bot_a", workspace=ws, shared_dir=shared,
        user_description="x",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T10:00:00Z",
        dispatch_fn=_bad_dispatch,
    )
    # Stage 1 dispatch failed → triage carries the error and the
    # diagnosis path falls back to no_diagnosis.
    assert "LLM exploded" in (out.triage.error or "")
    assert out.status == "no_diagnosis"


# ── Notification template rendering ────────────────────────────────────────


def test_render_notification_high_confidence_uses_diagnosed_form() -> None:
    record = {
        "user_description": "morning briefing didn't arrive",
        "diagnosis": "Gmail token expired three days ago.",
        "suggested_fix": "Regenerate the gmail token.",
        "confidence": "high",
        "what_i_checked": ["gmail token state", "briefing trail"],
    }
    body = inv_mod.render_notification_detail(
        record, is_pod_admin=False,
    )
    assert "I checked" in body
    assert "Gmail token expired" in body
    assert "Regenerate the gmail token." in body
    assert "evo fail flag" in body


def test_render_notification_low_confidence_uses_no_diagnosis_form() -> None:
    record = {
        "user_description": "things feel off",
        "diagnosis": None,
        "confidence": "low",
        "what_i_checked": [
            "Recent signals on this bot",
            "Audit trails for apps and skills",
        ],
    }
    body = inv_mod.render_notification_detail(
        record, is_pod_admin=False,
    )
    assert "couldn't pinpoint a single cause" in body
    assert "Recent signals on this bot" in body
    assert "evo fail flag" in body


def test_render_notification_includes_trail_link_only_for_admins() -> None:
    record = {
        "user_description": "x",
        "diagnosis": "y",
        "suggested_fix": "z",
        "confidence": "high",
    }
    body_user = inv_mod.render_notification_detail(
        record, is_pod_admin=False, trail_link="https://example/trail",
    )
    body_admin = inv_mod.render_notification_detail(
        record, is_pod_admin=True, trail_link="https://example/trail",
    )
    assert "https://example/trail" not in body_user
    assert "https://example/trail" in body_admin


# ── Trail + outbox record rendering ────────────────────────────────────────


def test_trail_entry_records_full_structure() -> None:
    out = inv_mod.InvestigationOutput(
        investigation_id="inv-x",
        bot_id="team_bot_a",
        user_description="things broke",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T09:00:00Z",
        started_at="2026-05-17T09:00:01Z",
        completed_at="2026-05-17T09:01:00Z",
        triage=inv_mod.TriageResult(
            candidates=[
                inv_mod.TriageCandidate(
                    element_type="app", element_id="m",
                    confidence="high", justification="x",
                )
            ],
            top_candidate=inv_mod.TriageCandidate(
                element_type="app", element_id="m",
                confidence="high", justification="x",
            ),
            tokens_used=100,
        ),
        diagnosis=inv_mod.Diagnosis(
            diagnosis="found it",
            suggested_fix="fix it",
            confidence="high",
            evidence=["scripts/x.py:10"],
            tokens_used=200,
        ),
        chosen_candidate=inv_mod.TriageCandidate(
            element_type="app", element_id="m",
            confidence="high", justification="x",
        ),
        related_signal_ids=["sig-1", "sig-2"],
        status="ok",
    )
    entry = inv_mod.render_investigation_trail_entry(out)
    assert entry["kind"] == "investigation"
    assert entry["investigation_id"] == "inv-x"
    assert entry["user_description"] == "things broke"
    assert entry["chosen_candidate"]["element_id"] == "m"
    assert entry["diagnosis"] == "found it"
    assert entry["confidence"] == "high"
    assert entry["status"] == "ok"
    assert entry["tokens_total"] == 300
    assert len(entry["triage_candidates"]) == 1


def test_outbox_record_kind_is_investigation_diagnosis() -> None:
    out = inv_mod.InvestigationOutput(
        investigation_id="inv-y",
        bot_id="team_bot_a",
        user_description="x",
        requesting_user="pod:pod_admin_user",
        requested_at="2026-05-17T09:00:00Z",
        started_at="2026-05-17T09:00:01Z",
        completed_at="2026-05-17T09:01:00Z",
        triage=inv_mod.TriageResult(),
        diagnosis=inv_mod.Diagnosis(diagnosis=None, confidence="low"),
        status="no_diagnosis",
    )
    record = inv_mod.render_outbox_record(out, runner_version="1.3.0")
    assert record["kind"] == "investigation_diagnosis"
    assert record["status"] == "no_diagnosis"
    assert record["diagnosis"] is None
    assert record["investigation_id"] == "inv-y"
    assert record["runner_version"] == "1.3.0"
