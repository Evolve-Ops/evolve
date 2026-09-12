"""End-to-end test: openclaw_cron_* finding → Signal → catalog event.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32
+ §17.11 (load-bearing Pass B assertion) + PR 12 (bot-side notification
wiring).

The three PRs stacked here (PR 1 schema, PR 5 Q32 assertion, PR 12
notification routing) form a single chain — manifest → audit runner →
outbox record → audit_poller → Signal store → signal_notifier →
dispatcher → operator chat. This test locks the chain together so a
future refactor of any single step surfaces immediately if it breaks
the contract.

What this test validates:

1. A tier2_finding record carrying ``assertion_id="openclaw_cron_error"``
   (or _skipped, _delivery_failure) flows through ``audit_poller.poll_bot``
   into a real Signal in ``shared/signals/firing/``.

2. The Signal's ``producer`` is ``"app_structural_verifier"`` (the
   allowlist key signal_notifier checks).

3. The Signal's ``type`` is the assertion_id (the field
   ``_catalog_event_for_signal`` keys off).

4. ``_catalog_event_for_signal`` resolves the Signal to
   ``"system.app_scheduled_work_failure"`` — the bespoke catalog event
   added in PR 12.

5. ``catalog.by_key`` for that event is registered with the right
   producer_source. (Tested in test_signal_notifier_app_structural.py
   too; included here so the e2e chain is verified end-to-end in one
   place.)

If you change any of: the runner's outbox record shape, the audit_poller's
ingest field mapping, the signal_notifier's catalog mapping, or the
catalog's app_scheduled_work_failure registration — this test catches
the drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

# Ensure the analyzer dir is importable so signals.store loads — same
# bootstrap as test_audit_poller.py. Without this, _ingest_finding's
# import of signals.store silently fails and findings_ingested stays 0.
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.alerts import catalog as cat  # noqa: E402
from evolve_admin.alerts import signal_notifier as sn  # noqa: E402
from evolve_admin.applications import audit_poller  # noqa: E402


@pytest.fixture()
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the audit-poller outbox helpers to a tmp tree.

    Mirrors the existing test_audit_poller fixture so changes to the
    upstream outbox path resolution surface in both files together.
    """
    def _audit_outbox(bot_user: str) -> Path:
        return tmp_path / bot_user / "audit_outbox"

    def _audit_ingested(bot_user: str) -> Path:
        return tmp_path / bot_user / "audit_outbox" / "_ingested"

    monkeypatch.setattr(audit_poller, "_audit_outbox_dir", _audit_outbox)
    monkeypatch.setattr(audit_poller, "_audit_outbox_ingested", _audit_ingested)
    return tmp_path


def _make_outbox(tmp_root: Path, bot_user: str) -> Path:
    outbox = tmp_root / bot_user / "audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _write_openclaw_finding(
    outbox: Path,
    *,
    bot_id: str,
    app_id: str,
    assertion_id: str,
    severity: str,
    cron_id: str,
    cron_name: str,
    summary: str,
    record_id: str = "rec-1",
) -> Path:
    """Write the outbox shape the v20 runner produces for an openclaw
    cron finding.

    Schema mirrors test_audit_poller._write_finding but with evidence
    keys that match what check_openclaw_cron_run_status emits — so the
    full chain is exercised from the actual Finding shape.
    """
    rec = {
        "record_id": record_id,
        "audit_run_id": "run-1",
        "kind": "tier2_finding",
        "ts": "2026-06-05T00:00:00Z",
        "runner_version": "1.0.0",
        "producer": "app_structural_verifier",
        "bot_id": bot_id,
        "app_id": app_id,
        # Signature shape mirrors Finding.signature() — the v20
        # openclaw_cron_* checks key the evidence on `schedule` for
        # signature stability.
        "signature": (
            f"app_structural_verifier:{assertion_id}:{bot_id}:{app_id}:"
            f"schedule=cron 0 9 * * *"
        ),
        "assertion_id": assertion_id,
        "severity": severity,
        "summary": summary,
        "evidence": {
            "openclaw_cron_id": cron_id,
            "openclaw_cron_name": cron_name,
            "schedule": "cron 0 9 * * *",
            "command": "openclaw agent run team-bot-a-task-worker",
            "last_summary": summary,
        },
    }
    p = outbox / f"{record_id}.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def _signal_to_catalog_namespace(signal_body: dict) -> SimpleNamespace:
    """Convert a Signal store JSON body into the namespace shape
    _catalog_event_for_signal reads (it pulls `.producer` and `.type`).
    """
    return SimpleNamespace(
        producer=signal_body["producer"],
        type=signal_body["type"],
    )


# ── team-bot-a-task-worker (Status: error) ────────────────────────────────────────

def test_openclaw_cron_error_flows_end_to_end(tmp_root: Path,
                                              tmp_path: Path) -> None:
    """The team-bot-a-task-worker production case, verbatim. Outbox record →
    Signal store → signal_notifier catalog mapping → bespoke catalog
    event. If any link in the chain breaks, this test fails before the
    next deploy."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_openclaw_finding(
        outbox,
        bot_id="team-bot-a",
        app_id="team-bot-a-task-management",
        assertion_id="openclaw_cron_error",
        severity="critical",
        cron_id="ae1bfa20-c256-45f4-8c7b-decf5f9446ad",
        cron_name="team-bot-a-task-worker",
        summary=(
            "openclaw cron 'team-bot-a-task-worker' last run reported error: "
            "The Slack API credentials aren't configured in the fallback "
            "messenger."
        ),
    )

    result = audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    # Audit poller ingested the finding into the Signal store.
    assert result.findings_ingested == 1
    assert result.errors == []

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    signal = json.loads(firing[0].read_text())

    # PR 12's contract: producer key the allowlist expects.
    assert signal["producer"] == "app_structural_verifier"
    assert signal["producer"] in sn._DEFAULT_PRODUCERS

    # Type carries the assertion_id so catalog routing works.
    assert signal["type"] == "openclaw_cron_error"

    # Severity mapped correctly (critical → alert).
    assert signal["severity"] == "alert"

    # bot_id + body populated so signal_notifier._render_fire produces
    # something legible for the operator.
    assert signal["bot_id"] == "team-bot-a"
    assert "team-bot-a-task-worker" in signal["body"]
    assert "Slack" in signal["body"] or "credentials" in signal["body"]

    # The cron_id is preserved in details.evidence so admin-UI deep
    # links + the catalog action template can use it.
    assert signal["details"]["evidence"]["openclaw_cron_id"] == \
        "ae1bfa20-c256-45f4-8c7b-decf5f9446ad"
    assert signal["details"]["evidence"]["openclaw_cron_name"] == \
        "team-bot-a-task-worker"

    # ── PR 12 mapping: this Signal routes to the bespoke catalog event ──
    sig_ns = _signal_to_catalog_namespace(signal)
    assert sn._catalog_event_for_signal(sig_ns) == \
        "system.app_scheduled_work_failure"

    # ── Catalog: that event is registered and renders cleanly ──
    entry = cat.by_key("system.app_scheduled_work_failure")
    assert entry is not None
    assert entry.producer_source == "app_structural_verifier"
    # Sample-payload rendering is exercised by test_alerts_catalog.py;
    # we just confirm the event exists and is wired to the right producer.


# ── personal-bot-backup (Status: skipped) ─────────────────────────────────────────

def test_openclaw_cron_skipped_flows_end_to_end(tmp_root: Path,
                                                tmp_path: Path) -> None:
    """personal-bot's 5-crons-all-skipped production case. Major severity → warn
    severity on Signal → still routes to the same bespoke catalog event."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "personal-bot")
    _write_openclaw_finding(
        outbox,
        bot_id="personal-bot",
        app_id="personal-bot-backup-system",
        assertion_id="openclaw_cron_skipped",
        severity="major",
        cron_id="4feeb7db-40e6-42df-83f2-e2930d591588",
        cron_name="personal-bot-backup",
        summary=(
            "openclaw cron 'personal-bot-backup' last run was skipped — "
            "schedule fires but work defers"
        ),
    )

    result = audit_poller.poll_bot("personal-bot", "personal-bot", shared)

    assert result.findings_ingested == 1
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    signal = json.loads(firing[0].read_text())

    assert signal["type"] == "openclaw_cron_skipped"
    assert signal["severity"] == "warn"   # major → warn
    assert "skipped" in signal["body"]

    sig_ns = _signal_to_catalog_namespace(signal)
    assert sn._catalog_event_for_signal(sig_ns) == \
        "system.app_scheduled_work_failure"


# ── team-bot-a-task-worker style "ok but delivery failed" ─────────────────────────

def test_openclaw_cron_delivery_failure_flows_end_to_end(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """The asymmetric case: Status=ok but summary contains failure
    language. This is the assertion that catches team-bot-a-task-worker's
    actual silent-failure pattern."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "team-bot-a")
    _write_openclaw_finding(
        outbox,
        bot_id="team-bot-a",
        app_id="team-bot-a-task-management",
        assertion_id="openclaw_cron_delivery_failure",
        severity="major",
        cron_id="ae1bfa20-c256-45f4-8c7b-decf5f9446ad",
        cron_name="team-bot-a-task-worker",
        summary=(
            "openclaw cron 'team-bot-a-task-worker' reports Status=ok but summary "
            "indicates delivery failure ('message failed'): "
            "Slack credentials missing."
        ),
    )

    result = audit_poller.poll_bot("team-bot-a", "team-bot-a", shared)

    assert result.findings_ingested == 1
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    signal = json.loads(firing[0].read_text())

    assert signal["type"] == "openclaw_cron_delivery_failure"
    assert signal["severity"] == "warn"

    sig_ns = _signal_to_catalog_namespace(signal)
    assert sn._catalog_event_for_signal(sig_ns) == \
        "system.app_scheduled_work_failure"


# ── Non-cron structural finding for contrast ──────────────────────────────

def test_non_cron_structural_finding_routes_to_audit_finding(
    tmp_root: Path, tmp_path: Path,
) -> None:
    """A regular file_missing finding (the kind that's been firing for
    a while) routes to the existing security.audit_finding umbrella —
    not the new scheduled-work event. Confirms PR 12's mapping doesn't
    accidentally cross over and re-route existing assertion types."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_outbox(tmp_root, "personal-bot")

    rec = {
        "record_id": "rec-fm",
        "audit_run_id": "run-1",
        "kind": "tier2_finding",
        "ts": "2026-06-05T00:00:00Z",
        "runner_version": "1.0.0",
        "producer": "app_structural_verifier",
        "bot_id": "personal-bot",
        "app_id": "daily-cost-check",
        "signature": "app_structural_verifier:file_missing:personal-bot:daily-cost-check:path=scripts/foo.py",
        "assertion_id": "file_missing",
        "severity": "critical",
        "summary": "manifest claims file 'scripts/foo.py' but it is missing",
        "evidence": {"path": "scripts/foo.py"},
    }
    (outbox / "rec-fm.json").write_text(json.dumps(rec))

    result = audit_poller.poll_bot("personal-bot", "personal-bot", shared)
    assert result.findings_ingested == 1

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    signal = json.loads(firing[0].read_text())
    assert signal["type"] == "file_missing"

    sig_ns = _signal_to_catalog_namespace(signal)
    assert sn._catalog_event_for_signal(sig_ns) == "security.audit_finding"
