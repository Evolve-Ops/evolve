"""Tests for the audit poller's new infra-record dispatch (Workstream B-infra).

Covers:
  - infra_finding → Proposal via _ingest_infra_finding
  - infra_run_summary → sweep_resolve called with the right kept set
  - infra_run_failed → Signal in firing/ via _ingest_infra_run_failed
  - Processed files archived under _ingested/
  - Unknown infra-kinds left in outbox (forward-compat)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_PACKAGES_DIR = _ADMIN_DIR.parent
sys.path.insert(0, str(_PACKAGES_DIR / "analyzer"))

from evolve_admin.applications import audit_poller  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_infra_outbox(shared_dir: Path) -> Path:
    outbox = shared_dir / "infra_audit_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _finding_rec(
    *, element: str = "daemons",
    category: str = "daemon_not_loaded",
    severity: str = "critical",
    record_id: str = "infra-rec-1",
    audit_run_id: str = "infra-run-1",
    signature: str | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "infra_finding",
        "ts": "2026-05-17T12:00:00Z",
        "runner_version": "1.0.0",
        "producer": "infra_audit",
        "element": element,
        "category": category,
        "severity": severity,
        "signature": signature or f"infra_audit:{element}:{category}:abc123",
        "outcome": "propose",
        "description": f"({element}) {category}",
        "evidence": {"label": "ai.evolve.evolve.admin-ui"},
        "suggested_fix": "bootstrap it",
        "rationale": "synth",
    }


def _summary_rec(
    *, audit_run_id: str = "infra-run-1",
    record_id: str = "infra-sum-1",
    kept_signatures: list[str] | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "audit_run_id": audit_run_id,
        "kind": "infra_run_summary",
        "ts": "2026-05-17T12:01:00Z",
        "runner_version": "1.0.0",
        "producer": "infra_audit",
        "completed_at": "2026-05-17T12:01:00Z",
        "elements_checked": ["daemons"],
        "findings_count": 0,
        "outcomes": {"dismiss": 0, "propose": 0, "auto_fix": 0},
        "kept_signatures": kept_signatures or [],
    }


# ── Tests ───────────────────────────────────────────────────────────────────


def test_infra_finding_writes_proposal(tmp_path: Path) -> None:
    """infra_finding record → Proposal in arbiter store."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_infra_outbox(shared)
    rec = _finding_rec()
    (outbox / "rec.json").write_text(json.dumps(rec))

    result = audit_poller.poll_infra(shared)

    assert result.findings_ingested == 1
    assert result.files_processed == 1

    # A proposal landed in pending/.
    proposals = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text())
    assert body["generator_id"] == "infra_audit"
    assert body["dimension"] == "reliability"
    # The context body mentions the element + category.
    ctx_text = json.dumps(body)
    assert "daemons" in ctx_text
    assert "daemon_not_loaded" in ctx_text


def test_infra_finding_is_idempotent(tmp_path: Path) -> None:
    """Re-ingesting the same trigger doesn't duplicate Proposals."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_infra_outbox(shared)

    (outbox / "rec1.json").write_text(json.dumps(_finding_rec(record_id="r1")))
    audit_poller.poll_infra(shared)

    (outbox / "rec2.json").write_text(json.dumps(_finding_rec(record_id="r2")))
    audit_poller.poll_infra(shared)

    proposals = list((shared / "proposals" / "pending").glob("*.json"))
    assert len(proposals) == 1  # dedup'd by trigger_observation


def test_infra_run_summary_sweep_resolves(tmp_path: Path) -> None:
    """infra_run_summary calls sweep_resolve on infra_audit signals."""
    shared = tmp_path / "shared"
    shared.mkdir()
    # Seed an existing infra signal directly so sweep_resolve has something to do.
    from signals import store as signals_store
    signals_store.observe(
        shared_dir=shared,
        signature="infra_audit_run_failed:dead-run",
        producer="infra_audit",
        type="infra_audit_run_failed",
        flavor="maintenance",
        severity="alert",
        scope="pod",
        bot_id="",
        title="stale infra signal",
        body="from a previous run",
    )
    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1

    outbox = _make_infra_outbox(shared)
    (outbox / "sum.json").write_text(json.dumps(_summary_rec(kept_signatures=[])))
    result = audit_poller.poll_infra(shared)

    assert result.summaries_processed == 1
    # The stale signal was swept (not in kept_signatures).
    firing_after = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing_after) == 0


def test_infra_run_failed_emits_signal(tmp_path: Path) -> None:
    """infra_run_failed → alert-severity Signal in firing/."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_infra_outbox(shared)
    (outbox / "fail.json").write_text(json.dumps({
        "record_id": "rec-fail-1",
        "audit_run_id": "infra-run-broken",
        "kind": "infra_run_failed",
        "ts": "2026-05-17T12:00:00Z",
        "runner_version": "1.0.0",
        "producer": "infra_audit",
        "error": "diagnostics gatherer crashed",
    }))

    result = audit_poller.poll_infra(shared)
    assert result.signals_emitted == 1
    assert result.files_processed == 1

    firing = list((shared / "signals" / "firing").glob("*.json"))
    assert len(firing) == 1
    body = json.loads(firing[0].read_text())
    assert body["producer"] == "infra_audit"
    assert body["severity"] == "alert"
    assert "diagnostics gatherer crashed" in body["body"]


def test_unknown_infra_kind_left_in_outbox(tmp_path: Path) -> None:
    """A future record kind is logged as unhandled, not silently archived."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_infra_outbox(shared)
    p = outbox / "future.json"
    p.write_text(json.dumps({
        "kind": "infra_quantum_finding",
        "audit_run_id": "x",
    }))
    result = audit_poller.poll_infra(shared)
    assert result.files_processed == 0
    assert any("unhandled infra kind" in e for e in result.errors)
    assert p.exists()


def test_tick_polls_infra_outbox(tmp_path: Path, monkeypatch) -> None:
    """audit_poller.tick() also drains the infra outbox into TickResult."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outbox = _make_infra_outbox(shared)
    (outbox / "rec.json").write_text(json.dumps(_finding_rec()))

    agg = audit_poller.tick(shared, network=None, bot_users={})
    assert agg.infra.findings_ingested == 1
    assert agg.total_infra_findings == 1
