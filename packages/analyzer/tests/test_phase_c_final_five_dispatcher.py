"""Phase C final four — report / validate / weekly_review /
repo_puller route through alerts.dispatcher.

One consolidated test file for the closing batch of single-site
migrations. Each pins source / catalog_event / dedup_key / severity.
``test_runner`` retired 2026-06-08 with the app-test surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ``report`` was retired 2026-06-05 (consolidated into pod_report).
# Its routing test moved to test_pod_report_dispatcher.py.
# ``test_runner`` was retired 2026-06-08 with the app-test surface.
import validate  # noqa: E402
import weekly_review  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "captured": captured,
        "shared_dir": shared,
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
        "DispatchResult": DispatchResult,
    }


# ── report ─────────────────────────────────────────────────────────────────
# Retired 2026-06-05: the per-bot daily digest was consolidated into
# pod_report. The corresponding routing test lives in
# tests/test_pod_report_dispatcher.py — it pins the
# ``summaries.daily_pod_report`` catalog_event. ``summaries.daily_bot_
# digest`` (the post-rename, pre-consolidation key) resolves to the
# same entry via ``catalog.LEGACY_KEY_MIGRATION``.


# ── test_runner ────────────────────────────────────────────────────────────
# Retired 2026-06-08 with the app-test surface.


# ── validate ───────────────────────────────────────────────────────────────


class _ValidationResult:
    def __init__(self, *, proposal_id="prop_x", result="pass",
                 recommendation="promote", validation_notes="",
                 tests_run=None):
        self.proposal_id = proposal_id
        self.result = result
        self.recommendation = recommendation
        self.validation_notes = validation_notes
        self.tests_run = tests_run or []


def test_validate_routes_manifest_validation_failed(env):
    proposal = {"id": "prop_xyz", "target_bot": "team_bot_a", "type": "config_change",
                "problem": "Lower context cap"}
    result = _ValidationResult(proposal_id="prop_xyz", result="fail",
                               recommendation="reject",
                               validation_notes="regression detected")
    validate.alert_primary(
        proposal, result, env["network"], shared_dir=env["shared_dir"],
    )
    call = env["captured"][0]
    assert call["source"] == "validate"
    assert call["catalog_event"] == "system.manifest_validation_failed"
    assert call["dedup_key"] == "validate/prop_xyz/fail"
    # FAIL → WARNING severity
    assert call["severity"] == env["Severity"].WARNING


def test_validate_pass_result_uses_info_severity(env):
    proposal = {"id": "prop_pass", "target_bot": "admin_bot", "type": "config_change",
                "problem": "Trim AGENTS.md"}
    result = _ValidationResult(proposal_id="prop_pass", result="pass",
                               recommendation="promote")
    validate.alert_primary(
        proposal, result, env["network"], shared_dir=env["shared_dir"],
    )
    call = env["captured"][0]
    assert call["severity"] == env["Severity"].INFO
    assert call["dedup_key"] == "validate/prop_pass/pass"


# ── weekly_review ──────────────────────────────────────────────────────────


def test_weekly_review_routes_weekly_rsi_review(env):
    ok = weekly_review.send_report(
        "12 proposals reviewed, 8 applied, 4 rejected",
        env["network"], shared_dir=env["shared_dir"],
    )
    assert ok is True
    call = env["captured"][0]
    assert call["source"] == "weekly_review"
    assert call["catalog_event"] == "summaries.weekly_rsi_review"
    assert call["dedup_key"].startswith("weekly_review/")
    assert call["severity"] == env["Severity"].INFO
    assert "12 proposals" in call["message"]


def test_weekly_review_truncates_oversize_body(env):
    """Telegram has a 4096-char limit — weekly_review must truncate
    gracefully rather than failing the send."""
    big = "x" * 5000
    weekly_review.send_report(
        big, env["network"], shared_dir=env["shared_dir"],
    )
    msg = env["captured"][0]["message"]
    assert len(msg) <= 4100   # truncated + appended "(report truncated)" note
    assert "(report truncated" in msg


# ── repo_puller ────────────────────────────────────────────────────────────


def test_repo_puller_routes_repo_puller_wedged(env, monkeypatch, tmp_path):
    """The wedge notifier loads network.json from disk — stub the loader
    so the test doesn't depend on /Users/Shared/evolve."""
    from evolve_admin import repo_puller as _rp

    # Stub load_config / get_shared_dir / resolve_network_path so the
    # internal evolve_config lookups don't touch the real filesystem.
    import evolve_config as _ec_mod
    monkeypatch.setattr(_ec_mod, "resolve_network_path", lambda: tmp_path / "net.json")
    monkeypatch.setattr(_ec_mod, "load_config", lambda _p: env["network"])
    monkeypatch.setattr(_ec_mod, "get_shared_dir", lambda _c: env["shared_dir"])

    issue_path = tmp_path / "issues" / "open" / "wedge-2026-05-10.md"
    issue_path.parent.mkdir(parents=True)
    issue_path.write_text("wedge details")

    sent, err = _rp._send_wedge_notification(
        issue_path, "untracked file conflict",
        repo=tmp_path / "evolve-repo",
    )
    assert sent is True
    assert err == ""

    call = env["captured"][0]
    assert call["source"] == "repo_puller"
    assert call["catalog_event"] == "system.repo_puller_wedged"
    assert call["dedup_key"] == f"repo_puller/wedge/{issue_path.name}"
    assert call["severity"] == env["Severity"].ERROR
    # Payload-driven; catalog body_template renders the wedge header
    # plus the "deploy drifting" close-out + cli_action recovery line.
    assert call["message"] is None
    assert call["payload"] == {
        "repo_name": "evolve-repo",
        "issue_filename": "wedge-2026-05-10.md",
        "incident_path": str(issue_path),
        "error_summary": "untracked file conflict",
    }


# ── All five sources registered ────────────────────────────────────────────


def test_dispatcher_recognizes_all_final_five_sources():
    from evolve_admin.alerts.dispatcher import known_sources
    sources = set(known_sources())
    # ``test_runner`` retired 2026-06-08 with the app-test surface.
    for name in ("report", "validate", "weekly_review", "repo_puller"):
        assert name in sources, f"missing dispatcher source: {name}"
