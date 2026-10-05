"""ALPHA-2 producer side — the sync result stops claiming a scan it did not run.

Audit finding B2a (``internal/audit-alpha-journey-2026-08.md`` §4.1). On the
three-bot fixture pod with no resolvable provider key,
``POST /api/applications/sync/pod`` returned::

    {"escalated_bots": 3, "total_discovered": 0,
     "reason": "Found 9 uncovered code file(s)/dir(s) → ran full scan;
                discovered 0 app(s)"}

in 0.5 seconds, because Phase 2 and Phase 4 never ran. The scanner had written
``llm_degraded`` into its own status file and nothing read it.

The three things pinned here:

  * the cheap path is NOT a degradation. It ran no model phase because none was
    needed; conflating that with "ran without a model" would make every healthy
    pod look broken.
  * the escalated path reads the scan's OWN record and reports what it says.
  * a record we cannot read yields ``unknown``, never ``ran`` — claiming the
    model phase happened because we could not check is the bug being fixed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import sync as sync_mod  # noqa: E402
from evolve_admin.applications.reflect import ReflectResult  # noqa: E402


class _Inventory:
    python_scripts: list = []
    shell_scripts: list = []
    named_dirs: list = []
    user = "bot-a"


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    """Point ``scan_status_path`` at a tmp file and hand back a writer."""
    path = tmp_path / ".scan-status.json"

    monkeypatch.setattr(sync_mod, "scan_status_path", lambda bot, shared: path)

    def write(payload) -> None:
        """dict → JSON, str → verbatim (for the corrupt-record case), None → gone."""
        if payload is None:
            path.unlink(missing_ok=True)
        elif isinstance(payload, str):
            path.write_text(payload)
        else:
            path.write_text(json.dumps(payload))

    return write


def _prepass(bot_id: str, workspace: Path, uncovered: list[str]):
    return sync_mod.PrePass(
        bot_id=bot_id, workspace=workspace, inventory=_Inventory(),
        instances=[], reflect_result=ReflectResult(bot_id=bot_id),
        uncovered=uncovered,
    )


@pytest.fixture
def escalating_pod(tmp_path, monkeypatch):
    """A bot whose pre-pass always escalates, with the scan itself stubbed out.

    Only the two expensive I/O calls are replaced. ``_scan_provenance_fields``
    — the code under test — runs for real against the tmp status file.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(
        sync_mod, "cheap_prepass",
        lambda bot, shared, user=None: _prepass(bot, ws, ["notes.py"]),
    )
    monkeypatch.setattr(sync_mod, "run_full_scan",
                        lambda *a, **kw: 0)
    monkeypatch.setattr(sync_mod, "reflect",
                        lambda bot, shared: ReflectResult(bot_id=bot))
    monkeypatch.setattr(sync_mod, "_load_bot_instances", lambda bot: [])
    monkeypatch.setattr(sync_mod, "_count_manifest_files", lambda bot: 0)
    return tmp_path


# ── the cheap path is not a degradation ──────────────────────────────────────


def test_cheap_path_reports_no_model_phase_and_no_degradation(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(
        sync_mod, "cheap_prepass",
        lambda bot, shared, user=None: _prepass(bot, ws, []),
    )
    result = sync_mod.sync_bot("bot-a", tmp_path, {})
    assert result["path"] == "cheap"
    assert result["llm_phase"] == sync_mod.LLM_PHASE_NOT_RUN
    assert result["llm_degraded"] is False
    assert result["llm_degraded_reason"] is None
    # And the sentence makes no claim about a scan having run.
    assert "scan" not in result["reason"].lower()


# ── the escalated path reports what the scan's own record says ───────────────


def test_escalated_with_a_healthy_record_still_says_ran_full_scan(
    escalating_pod, status_file,
):
    status_file({"status": "done", "updated_at": "2026-08-23T09:00:00Z"})
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["llm_phase"] == sync_mod.LLM_PHASE_RAN
    assert result["llm_degraded"] is False
    assert "ran full scan" in result["reason"]


def test_escalated_without_a_provider_key_stops_saying_ran_full_scan(
    escalating_pod, status_file,
):
    """The audit's exact sentence, corrected."""
    status_file({"status": "done", "llm_degraded": True,
                 "llm_degraded_reason": "no_llm_provider_key"})
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["llm_phase"] == sync_mod.LLM_PHASE_SKIPPED_DEGRADED
    assert result["llm_degraded"] is True
    assert result["llm_degraded_reason"] == "no_llm_provider_key"
    assert result["llm_degraded_note"] and result["llm_degraded_remedy"]
    assert "ran full scan" not in result["reason"], (
        "this is the claim the operator was given while Phase 2 never ran"
    )
    assert "structural" in result["reason"]
    # "discovered 0" is still reported — it is just no longer a finding.
    assert "discovered 0 app(s)" in result["reason"]


def test_escalated_with_an_unreadable_record_is_unknown_never_ran(
    escalating_pod, status_file,
):
    status_file("{not json")
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["llm_phase"] == sync_mod.LLM_PHASE_UNKNOWN
    assert result["llm_degraded"] is False
    assert "ran full scan" not in result["reason"]


def test_a_missing_record_after_an_escalated_scan_is_unknown_not_ran(
    escalating_pod, status_file,
):
    """A scan that wrote no record told us nothing about what it did."""
    status_file(None)
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["llm_phase"] == sync_mod.LLM_PHASE_UNKNOWN


def test_a_path_resolution_failure_degrades_to_unknown_not_to_a_500(
    escalating_pod, monkeypatch,
):
    def boom(bot, shared):
        raise RuntimeError("no such user")

    monkeypatch.setattr(sync_mod, "scan_status_path", boom)
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["llm_phase"] == sync_mod.LLM_PHASE_UNKNOWN


def test_a_concurrent_scan_is_unknown_not_not_run(escalating_pod, status_file):
    """We did not run a model phase AND we cannot speak for the one in flight."""
    status_file({"status": "done"})
    with sync_mod.scan_lock("bot-a") as acquired:
        assert acquired
        result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    assert result["already_running"] is True
    assert result["llm_phase"] == sync_mod.LLM_PHASE_UNKNOWN


# ── every result carries the fields, so a reader never has to guess ──────────


def test_the_llm_fields_are_always_present_on_every_path(escalating_pod, status_file):
    status_file({"status": "done"})
    result = sync_mod.sync_bot("bot-a", escalating_pod, {})
    for key in ("llm_phase", "llm_degraded", "llm_degraded_reason",
                "llm_degraded_note", "llm_degraded_remedy"):
        assert key in result, f"{key} missing — a reader would have to infer it"


def test_the_default_result_shape_under_claims_rather_than_over_claims():
    """A caller that forgets the fields must not accidentally assert a good scan."""
    out = sync_mod._result("bot-a", "cheap", "why", 0, [], ReflectResult(bot_id="bot-a"))
    assert out["llm_phase"] == sync_mod.LLM_PHASE_NOT_RUN
    assert out["llm_degraded"] is False
