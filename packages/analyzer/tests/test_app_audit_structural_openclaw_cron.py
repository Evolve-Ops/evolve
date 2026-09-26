"""Unit tests for the v20 openclaw-cron-run-status assertion.

Validates the load-bearing Q32 check from
internal/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32 +
§17.11. The assertion catches openclaw crons whose schedule fires but
whose actual work fails — the dominant production failure pattern
revealed by the 5-bot validation (14 of 16 scheduled jobs broken or
silently skipped).

Three classification paths are tested:

1. Status == "error" → critical finding (team-bot-a-task-worker, security-bot-weekly-
   self-audit, ping-calendar-monitor, maintenance-weekly-report all fail
   this way in production right now).

2. Status == "skipped" → major finding (personal-bot's 5 openclaw crons all
   fail this way — schedule fires, delivery=not requested, no work
   visible).

3. Status == "ok" but summary contains failure-language patterns → major
   finding. The asymmetric case: the cron's headline says success but
   the LLM-generated summary describes a side-channel failure
   (delivery broken, credentials missing, route doesn't resolve). This
   is the SINGLE MOST OPERATOR-VALUABLE check in the spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    _OC_CRON_DELIVERY_FAILURE_PATTERNS,
    _match_manifest_cron_to_live,
    check_openclaw_cron_run_status,
)


# Minimal manifest with two cron entries — one for label-based matching,
# one for script-based.
def _manifest_with_crons() -> dict:
    return {
        "id": "test-app",
        "name": "Test App",
        "bot_id": "personal-bot",
        "crons": [
            {
                "label": "personal-bot-backup",
                "script": "scripts/backup.sh",
                "schedule": "0 3 * * *",
            },
            {
                "label": "team-bot-a-task-worker",
                "script": "scripts/task_worker.py",
                "schedule": "0 9 * * *",
            },
        ],
    }


# ── ctx-missing guard ──────────────────────────────────────────────────────

def test_no_findings_when_openclaw_crons_missing_from_ctx() -> None:
    """When openclaw_crons is absent from ctx (OC CLI unavailable), the
    assertion returns no findings. Avoids false positives during transient
    CLI outages or admin-server restarts."""
    findings = check_openclaw_cron_run_status(_manifest_with_crons(), {})
    assert findings == []


def test_no_findings_when_no_manifest_crons() -> None:
    """A manifest with no crons[] produces no findings even when the
    bot has live openclaw crons."""
    findings = check_openclaw_cron_run_status(
        {"id": "x", "crons": []},
        {"openclaw_crons": [{"id": "j-1", "name": "anything"}],
         "openclaw_run_history_by_id": {"j-1": [{"status": "error"}]}},
    )
    assert findings == []


def test_no_findings_when_manifest_cron_unmatched() -> None:
    """A manifest cron that doesn't match any live openclaw cron is
    silently skipped — the cron might be a crontab or launchd entry that
    other assertions cover."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [
            {"id": "j-unrelated", "name": "different-cron",
             "command": "python3 /opt/other.py", "schedule": "0 0 * * *"}
        ],
        "openclaw_run_history_by_id": {
            "j-unrelated": [{"status": "error", "summary": "..."}]
        },
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    # No manifest cron matches — no findings even though the live cron is in error.
    assert findings == []


def test_no_findings_when_no_run_history() -> None:
    """Recently-installed crons with no recorded runs don't fire."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *"}],
        "openclaw_run_history_by_id": {"j-1": []},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert findings == []


# ── Status == "error" → critical ────────────────────────────────────────────

def test_status_error_emits_critical() -> None:
    """The team-bot-a-task-worker production case: last run was an explicit error."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-team-bot-a-1", "name": "team-bot-a-task-worker",
                            "command": "python3 scripts/task_worker.py",
                            "schedule": "0 9 * * *"}],
        "openclaw_run_history_by_id": {"j-team-bot-a-1": [
            {"status": "error",
             "error": "⚠️ ✉️ Message failed",
             "summary": "The Slack API credentials aren't configured in the "
                        "fallback messenger. The cron job ran successfully and "
                        "marked the tasks as cancelled..."}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.assertion_id == "openclaw_cron_error"
    assert f.severity == SEVERITY_CRITICAL
    assert "team-bot-a-task-worker" in f.summary
    assert "Message failed" in f.summary or "credentials" in f.summary
    assert f.evidence["openclaw_cron_id"] == "j-team-bot-a-1"
    assert f.evidence["openclaw_cron_name"] == "team-bot-a-task-worker"


def test_error_status_does_not_also_fire_delivery_failure() -> None:
    """An error-status run with delivery-failure language fires the error
    finding only, not both (avoid double-counting)."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "team-bot-a-task-worker",
                            "command": "python3 scripts/task_worker.py",
                            "schedule": "0 9 * * *"}],
        "openclaw_run_history_by_id": {"j-1": [
            {"status": "error",
             "summary": "Message failed: no route to delivery channel"}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].assertion_id == "openclaw_cron_error"


# ── Status == "skipped" → major ─────────────────────────────────────────────

def test_status_skipped_emits_major() -> None:
    """personal-bot's production case: schedule registered but work defers
    (`delivery: not requested (not requested)`)."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-personal-bot-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *",
                            "delivery": "not requested (not requested)"}],
        "openclaw_run_history_by_id": {"j-personal-bot-1": [
            {"status": "skipped",
             "summary": ""}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.assertion_id == "openclaw_cron_skipped"
    assert f.severity == SEVERITY_MAJOR
    assert "personal-bot-backup" in f.summary
    assert "skipped" in f.summary
    assert f.evidence["delivery"] == "not requested (not requested)"


# ── Status == "ok" but failure-language in summary → major ─────────────────

def test_ok_status_with_message_failed_emits_delivery_failure() -> None:
    """Verbatim team-bot-a-task-worker recovery: 'Message failed' in summary."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-team-bot-a-2", "name": "team-bot-a-task-worker",
                            "command": "python3 scripts/task_worker.py",
                            "schedule": "0 9 * * *"}],
        "openclaw_run_history_by_id": {"j-team-bot-a-2": [
            {"status": "ok",
             "summary": "2 expired pending_todos auto-pruned. Message failed; "
                        "fallback messenger needs Slack credentials configured."}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.assertion_id == "openclaw_cron_delivery_failure"
    assert f.severity == SEVERITY_MAJOR
    assert "team-bot-a-task-worker" in f.summary
    assert f.evidence["matched_pattern"] == "message failed"


def test_ok_status_with_no_route_emits_delivery_failure() -> None:
    """Evolve's security:cve-scan production case: 'announce -> last
    (last -> no route, will fail-closed)'."""
    manifest = {
        "id": "test", "bot_id": "evolve",
        "crons": [{"label": "security:cve-scan",
                   "script": "procedures/security-cve-scan.md",
                   "schedule": "0 9 * * *"}],
    }
    ctx = {
        "openclaw_crons": [{"id": "j-cve", "name": "security:cve-scan",
                            "command": "...", "schedule": "0 9 * * *",
                            "delivery": "announce -> last (last -> no route)"}],
        "openclaw_run_history_by_id": {"j-cve": [
            {"status": "ok",
             "summary": "Scan completed; delivery attempted; no route; "
                        "will fail-closed."}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    f = findings[0]
    assert f.assertion_id == "openclaw_cron_delivery_failure"
    # First pattern hit in the order they appear in the list — both "delivery
    # attempted" and "no route" and "fail-closed" are in the summary; the
    # assertion picks the first one matching from _OC_CRON_DELIVERY_FAILURE_PATTERNS.
    assert f.evidence["matched_pattern"] in {"delivery attempted", "no route", "fail-closed"}


def test_ok_status_with_clean_summary_no_finding() -> None:
    """A genuinely-clean run: status ok, summary describes success. No finding."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *"}],
        "openclaw_run_history_by_id": {"j-1": [
            {"status": "ok",
             "summary": "Backup completed: 47 files committed and pushed."}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert findings == []


def test_ok_status_no_summary_no_finding() -> None:
    """A successful run with no summary doesn't trigger pattern matching."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *"}],
        "openclaw_run_history_by_id": {"j-1": [{"status": "ok"}]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert findings == []


# ── Failure-language pattern coverage ──────────────────────────────────────

def test_each_failure_pattern_is_recognized() -> None:
    """Every pattern in _OC_CRON_DELIVERY_FAILURE_PATTERNS must trigger
    a finding when it appears alone in a summary. This guards against
    typos or pattern drift; if a pattern stops matching, this fails."""
    manifest = _manifest_with_crons()
    for pattern in _OC_CRON_DELIVERY_FAILURE_PATTERNS:
        ctx = {
            "openclaw_crons": [{"id": "j-p", "name": "personal-bot-backup",
                                "command": "bash scripts/backup.sh",
                                "schedule": "0 3 * * *"}],
            "openclaw_run_history_by_id": {"j-p": [
                {"status": "ok", "summary": f"Worked but {pattern} happened."}
            ]},
        }
        findings = check_openclaw_cron_run_status(manifest, ctx)
        assert len(findings) == 1, f"pattern {pattern!r} did not fire a finding"
        assert findings[0].assertion_id == "openclaw_cron_delivery_failure"
        assert findings[0].evidence["matched_pattern"] == pattern


def test_failure_pattern_match_is_case_insensitive() -> None:
    """Production summaries have mixed case (e.g., 'Message failed' with
    capital M). The pattern matcher normalizes to lowercase."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *"}],
        "openclaw_run_history_by_id": {"j-1": [
            {"status": "ok", "summary": "Done. MESSAGE FAILED!"}
        ]},
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 1
    assert findings[0].evidence["matched_pattern"] == "message failed"


# ── Multi-cron handling ────────────────────────────────────────────────────

def test_multiple_manifest_crons_each_evaluated_independently() -> None:
    """A manifest with several crons gets a finding per failing cron.
    Tests the mixed-state production reality where some crons work and
    others don't."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [
            {"id": "j-1", "name": "personal-bot-backup", "command": "bash scripts/backup.sh",
             "schedule": "0 3 * * *"},
            {"id": "j-2", "name": "team-bot-a-task-worker", "command": "python3 scripts/task_worker.py",
             "schedule": "0 9 * * *"},
        ],
        "openclaw_run_history_by_id": {
            "j-1": [{"status": "skipped"}],          # → openclaw_cron_skipped
            "j-2": [{"status": "error",              # → openclaw_cron_error
                     "summary": "auth configuration"}],
        },
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    assert len(findings) == 2
    ids = sorted(f.assertion_id for f in findings)
    assert ids == ["openclaw_cron_error", "openclaw_cron_skipped"]


# ── _match_manifest_cron_to_live() helper ──────────────────────────────────

def test_match_by_label() -> None:
    """Primary match: manifest.label == live.name."""
    manifest_cron = {"label": "personal-bot-backup", "script": "anything.sh"}
    live = [{"id": "j-1", "name": "personal-bot-backup", "command": "irrelevant"}]
    assert _match_manifest_cron_to_live(manifest_cron, live)["id"] == "j-1"


def test_match_by_script_substring_in_command() -> None:
    """Fallback match: manifest.script appears in live.command."""
    manifest_cron = {"label": "", "script": "scripts/backup.sh"}
    live = [{"id": "j-1", "name": "different-name",
             "command": "cd /Users/personal-bot && bash scripts/backup.sh"}]
    assert _match_manifest_cron_to_live(manifest_cron, live)["id"] == "j-1"


def test_match_by_script_basename_in_command() -> None:
    """Match by basename when the full path differs between manifest and command."""
    manifest_cron = {"label": "", "script": "scripts/backup.sh"}
    live = [{"id": "j-1", "name": "x",
             "command": "/usr/local/bin/bash /Users/personal-bot/backup.sh"}]
    # script path doesn't substring-match, but basename "backup.sh" does
    assert _match_manifest_cron_to_live(manifest_cron, live)["id"] == "j-1"


def test_no_match_returns_none() -> None:
    """Distinct manifest cron + distinct live cron set returns None."""
    manifest_cron = {"label": "unique-label", "script": "scripts/unique.py"}
    live = [{"id": "j-1", "name": "other", "command": "python3 other.py"}]
    assert _match_manifest_cron_to_live(manifest_cron, live) is None


def test_empty_inputs_return_none() -> None:
    """An entry with no label and no script can't match anything."""
    assert _match_manifest_cron_to_live({"label": "", "script": ""},
                                        [{"id": "j", "name": "x"}]) is None
    # Empty live list → None.
    assert _match_manifest_cron_to_live({"label": "x", "script": "y"}, []) is None


# ── Signature stability ───────────────────────────────────────────────────

def test_finding_signature_is_stable_across_runs() -> None:
    """Two findings about the same cron get the same signature (Signal dedup)."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [{"id": "j-1", "name": "personal-bot-backup",
                            "command": "bash scripts/backup.sh",
                            "schedule": "0 3 * * *"}],
        "openclaw_run_history_by_id": {"j-1": [{"status": "error",
                                                "summary": "first"}]},
    }
    f1 = check_openclaw_cron_run_status(manifest, ctx)[0]

    # Same cron, different summary detail — signature should still match.
    ctx["openclaw_run_history_by_id"]["j-1"] = [{"status": "error",
                                                 "summary": "second"}]
    f2 = check_openclaw_cron_run_status(manifest, ctx)[0]

    assert f1.signature("personal-bot", "test-app") == f2.signature("personal-bot", "test-app")


def test_finding_signature_distinct_per_cron() -> None:
    """Different crons under the same app get distinct signatures."""
    manifest = _manifest_with_crons()
    ctx = {
        "openclaw_crons": [
            {"id": "j-1", "name": "personal-bot-backup", "command": "bash scripts/backup.sh",
             "schedule": "0 3 * * *"},
            {"id": "j-2", "name": "team-bot-a-task-worker", "command": "python3 scripts/task_worker.py",
             "schedule": "0 9 * * *"},
        ],
        "openclaw_run_history_by_id": {
            "j-1": [{"status": "error", "summary": "..."}],
            "j-2": [{"status": "error", "summary": "..."}],
        },
    }
    findings = check_openclaw_cron_run_status(manifest, ctx)
    sigs = {f.signature("personal-bot", "test-app") for f in findings}
    assert len(sigs) == 2  # distinct signatures for distinct crons
