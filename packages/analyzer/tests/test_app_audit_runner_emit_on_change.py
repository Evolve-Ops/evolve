"""Tests for the Tier-2 emit-on-change cut + upstream observational gate.

Footprint cut, 2026-06-28 (docs/footprint-disk-output-audit-2026-06-28.md).

Covers the runner's two source-cut behaviors:
  - Change A (emit-on-change): an UNCHANGED finding writes one outbox record
    across repeated runs; a CHANGED finding re-writes; a CLEARED finding drops
    out of the run-summary ``kept_signatures`` so the admin sweep_resolve still
    resolves its Signal (emit-suppression never strands a stale Signal).
  - Change B (upstream observational gate): a finding on an observational
    manifest field is never written to the outbox at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

import app_audit_runner as runner  # noqa: E402
from app_audit_structural import Finding  # noqa: E402


# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _make_workspace(tmp_path: Path, *, provenance: dict | None = None) -> Path:
    ws = tmp_path / "workspace"
    for sub in ("manifests", "evolve", "evolve/audits", "evolve/audit_outbox"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "app1",
        "status": "active",
        "provenance": provenance or {
            "field_origins": {
                "files": {"source": "forge_built"},   # authored → emits
                "crons": {"source": "observational"},  # observational → muted
            }
        },
    }
    (ws / "manifests" / "app1.json").write_text(json.dumps(manifest))
    return ws


@pytest.fixture
def stub_assertions(monkeypatch):
    """Drive ``run_structural_assertions`` from a mutable holder so each run
    can return a different finding set. Also stub the coherence pass (admin
    tree isn't reachable in the analyzer test env) for determinism."""
    holder: dict[str, list[Finding]] = {"findings": []}

    def _fake(manifest, ctx):
        return list(holder["findings"])

    monkeypatch.setattr(runner, "run_structural_assertions", _fake)
    monkeypatch.setattr(runner, "_run_coherence_passes", lambda **kw: None)
    return holder


def _finding_records(outbox_dir: Path) -> list[dict]:
    out = []
    for f in outbox_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("kind") == "tier2_finding":
            out.append(rec)
    return out


def _trail_lines(audits_dir: Path, app_id: str, kind: str | None = None) -> list[dict]:
    trail = audits_dir / app_id / "trail.jsonl"
    if not trail.exists():
        return []
    out = []
    for line in trail.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if kind is None or rec.get("kind") == kind:
            out.append(rec)
    return out


def _run_summary_for(outbox_dir: Path, run_id: str) -> dict:
    for f in outbox_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(rec, dict)
            and rec.get("kind") == "tier2_run_summary"
            and rec.get("audit_run_id") == run_id
        ):
            return rec
    raise AssertionError(f"no run_summary for {run_id}")


_AUTHORED = Finding(
    assertion_id="file_missing",
    severity="major",
    summary="a file the app needs is missing",
    evidence={"path": "scripts/x.py"},
)
_OBSERVATIONAL = Finding(
    assertion_id="openclaw_cron_error",  # → "crons" field (observational)
    severity="major",
    summary="a scheduled job is failing",
    evidence={"command": "do-thing"},
)


def _run(ws: Path, tmp_path: Path) -> dict:
    return runner.run_tier2(
        ws, bot_id="bot1", shared_dir=tmp_path / "shared",
    )


# ── Change B: upstream observational gate ─────────────────────────────────────


def test_observational_finding_never_written(tmp_path, stub_assertions):
    ws = _make_workspace(tmp_path)
    outbox = runner._audit_outbox_dir(ws)
    stub_assertions["findings"] = [_OBSERVATIONAL]

    result = _run(ws, tmp_path)

    assert _finding_records(outbox) == []
    assert result["records_written"] == 0
    assert result["findings_observational_suppressed"] == 1
    # The observational finding is still in kept_signatures (harmless — it has
    # no Signal) and still recorded in the per-app trail.
    summary = _run_summary_for(outbox, result["audit_run_id"])
    assert any("openclaw_cron_error" in s for s in summary["kept_signatures"])
    trail = (runner._audits_dir(ws) / "app1" / "trail.jsonl").read_text()
    assert "openclaw_cron_error" in trail


# ── Change A: emit-on-change ──────────────────────────────────────────────────


def test_unchanged_finding_written_once_across_runs(tmp_path, stub_assertions):
    ws = _make_workspace(tmp_path)
    outbox = runner._audit_outbox_dir(ws)
    stub_assertions["findings"] = [_AUTHORED, _OBSERVATIONAL]

    r1 = _run(ws, tmp_path)
    assert r1["records_written"] == 1               # authored only
    assert r1["findings_observational_suppressed"] == 1
    assert len(_finding_records(outbox)) == 1

    # Second run, identical findings → no new record.
    r2 = _run(ws, tmp_path)
    assert r2["records_written"] == 0
    assert r2["records_suppressed_unchanged"] == 1
    assert len(_finding_records(outbox)) == 1       # still exactly one

    # The authored finding stays in kept_signatures across both runs, so its
    # Signal is never swept-resolved while it still fires.
    summary = _run_summary_for(outbox, r2["audit_run_id"])
    assert any("file_missing" in s for s in summary["kept_signatures"])


def test_changed_finding_rewrites(tmp_path, stub_assertions):
    ws = _make_workspace(tmp_path)
    outbox = runner._audit_outbox_dir(ws)
    stub_assertions["findings"] = [_AUTHORED]
    r1 = _run(ws, tmp_path)
    assert r1["records_written"] == 1

    # Same signature (same assertion_id + path) but changed payload (summary).
    stub_assertions["findings"] = [
        Finding(
            assertion_id="file_missing",
            severity="critical",                       # changed severity
            summary="the file is STILL missing (escalated)",  # changed summary
            evidence={"path": "scripts/x.py"},
        )
    ]
    r2 = _run(ws, tmp_path)
    assert r2["records_written"] == 1
    assert r2["records_suppressed_unchanged"] == 0
    # Two finding records now exist (the original + the re-emit).
    assert len(_finding_records(outbox)) == 2


def test_cleared_finding_drops_from_kept_signatures(tmp_path, stub_assertions):
    ws = _make_workspace(tmp_path)
    outbox = runner._audit_outbox_dir(ws)
    stub_assertions["findings"] = [_AUTHORED]
    r1 = _run(ws, tmp_path)
    sig = _AUTHORED.signature("bot1", "app1")
    summary1 = _run_summary_for(outbox, r1["audit_run_id"])
    assert sig in summary1["kept_signatures"]

    # Finding clears — no longer reported.
    stub_assertions["findings"] = []
    r2 = _run(ws, tmp_path)
    summary2 = _run_summary_for(outbox, r2["audit_run_id"])
    # Absent from the keep-set → the admin sweep_resolve archives its Signal.
    assert sig not in summary2["kept_signatures"]
    # The cursor self-pruned the cleared signature.
    cursor = json.loads((outbox / ".emitted.json").read_text())
    assert sig not in cursor["signatures"]


# ── Change C: trail source-cut (dedup unchanged finding lines) ────────────────


def test_unchanged_finding_trail_line_written_once_across_runs(tmp_path, stub_assertions):
    """A persistent finding re-fires every run but its tier2_finding trail line
    is appended at most once; the per-run audit_run summary line is still
    written every run, so operators still see that audits ran."""
    ws = _make_workspace(tmp_path)
    audits = runner._audits_dir(ws)
    stub_assertions["findings"] = [_AUTHORED]

    r1 = _run(ws, tmp_path)
    r2 = _run(ws, tmp_path)

    # One tier2_finding line total across the two runs (deduped on run 2).
    finding_lines = _trail_lines(audits, "app1", kind="tier2_finding")
    assert len(finding_lines) == 1
    assert finding_lines[0]["assertion_id"] == "file_missing"
    # The run was counted as suppressing one unchanged trail line.
    assert r2["trail_lines_suppressed_unchanged"] == 1
    assert r1["trail_lines_suppressed_unchanged"] == 0
    # The audit_run summary line is still written every run (operators see runs).
    assert len(_trail_lines(audits, "app1", kind="audit_run")) == 2


def test_changed_finding_appends_new_trail_line(tmp_path, stub_assertions):
    """When the finding payload changes, a fresh tier2_finding trail line is
    appended (readers see the new state, not just the stale first one)."""
    ws = _make_workspace(tmp_path)
    audits = runner._audits_dir(ws)
    stub_assertions["findings"] = [_AUTHORED]
    _run(ws, tmp_path)

    stub_assertions["findings"] = [
        Finding(
            assertion_id="file_missing",
            severity="critical",
            summary="the file is STILL missing (escalated)",
            evidence={"path": "scripts/x.py"},
        )
    ]
    _run(ws, tmp_path)

    finding_lines = _trail_lines(audits, "app1", kind="tier2_finding")
    assert len(finding_lines) == 2
    # The most-recent line (what bounded-tail readers see) reflects the change.
    assert finding_lines[-1]["severity"] == "critical"


def test_trail_soft_capped_to_most_recent(tmp_path):
    """A trail past the soft cap is rewritten down to the most-recent 500 lines,
    newest preserved — bounded-tail readers keep their latest data."""
    audits = tmp_path / "audits"
    app_dir = audits / "app1"
    app_dir.mkdir(parents=True)
    trail = app_dir / "trail.jsonl"
    # Seed 1001 realistic-shaped lines (the existing unbounded sediment).
    with trail.open("w") as fh:
        for i in range(1001):
            fh.write(json.dumps({"kind": "tier2_finding", "seq": i}) + "\n")

    # One more append triggers the in-writer cap.
    runner._append_trail(audits, "app1", {"kind": "audit_run", "seq": 1001})

    lines = [json.loads(x) for x in trail.read_text().splitlines() if x.strip()]
    assert len(lines) == runner._TRAIL_CAP_KEEP
    # Newest preserved (the just-appended line is last; the head was dropped).
    assert lines[-1]["seq"] == 1001
    assert lines[0]["seq"] == 1001 + 1 - runner._TRAIL_CAP_KEEP


def test_cursor_robust_to_outbox_record_deletion(tmp_path, stub_assertions):
    """The cursor is self-contained: deleting shipped finding records (as the
    companion delete-on-ingest sweep does) must not cause re-emission of an
    unchanged finding on the next run."""
    ws = _make_workspace(tmp_path)
    outbox = runner._audit_outbox_dir(ws)
    stub_assertions["findings"] = [_AUTHORED]
    _run(ws, tmp_path)

    # Simulate the companion chip deleting ingested finding records.
    for rec in outbox.glob("rec-*.json"):
        rec.unlink()
    assert _finding_records(outbox) == []

    r2 = _run(ws, tmp_path)
    assert r2["records_written"] == 0               # cursor still suppresses
    assert r2["records_suppressed_unchanged"] == 1
