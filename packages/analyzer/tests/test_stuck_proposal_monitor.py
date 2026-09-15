"""tests/test_stuck_proposal_monitor.py — stuck_proposal_monitor producer tests.

Covers the 2026-05-20 forensic case where heal-team_bot_a-1776294604 sat in
``approved/`` for a month after a quarantine/regenerate cycle and
apply.py silently skipped it forever (apply-results entry from the
original quarantine pinned the idempotency check).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import stuck_proposal_monitor as spm  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _write_proposal(approved_dir: Path, proposal_id: str, *,
                    target_bot: str = "team_bot_a", ptype: str = "investigation",
                    problem: str = "test problem",
                    age_days: float = 0.0) -> Path:
    """Write a proposal JSON and backdate its mtime by ``age_days``."""
    approved_dir.mkdir(parents=True, exist_ok=True)
    p = approved_dir / f"{proposal_id}.json"
    p.write_text(json.dumps({
        "id": proposal_id,
        "target_bot": target_bot,
        "type": ptype,
        "problem": problem,
    }))
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


# ─────────────────────────────────────────────────────────────────────────────
# detect_stuck_proposals — pure function
# ─────────────────────────────────────────────────────────────────────────────


def test_no_signal_when_no_approved_proposals(tmp_path: Path) -> None:
    """Empty approved/ → no signal. Don't false-fire on a fresh pod."""
    assert spm.detect_stuck_proposals([], threshold_days=7) is None


def test_no_signal_when_proposals_are_fresh(tmp_path: Path) -> None:
    """Recent proposals (< threshold) are legitimately waiting — no signal."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "p-fresh", age_days=2)  # 2d < 7d threshold
    loaded = spm._load_approved_proposals(tmp_path)
    assert spm.detect_stuck_proposals(loaded, threshold_days=7) is None


def test_signal_fires_when_proposal_exceeds_threshold(tmp_path: Path) -> None:
    """A proposal older than threshold_days triggers a Signal."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "heal-team_bot_a-1776294604", target_bot="team_bot_a",
                    ptype="investigation",
                    problem="Gateway has gone down 265 times in the last 24h.",
                    age_days=30)
    loaded = spm._load_approved_proposals(tmp_path)
    spec = spm.detect_stuck_proposals(loaded, threshold_days=7)
    assert spec is not None
    assert spec["type"] == "stuck_proposal"
    assert spec["scope"] == "pod"
    assert spec["severity"] == "warn"
    assert "1 approved" in spec["title"]
    assert "heal-team_bot_a-1776294604" in spec["body"]
    assert spec["details"]["stuck_count"] == 1


def test_signal_lists_all_stuck_proposals_sorted_by_age(tmp_path: Path) -> None:
    """Multiple stuck proposals — body lists each in oldest-first order."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "p-newest-stuck", age_days=8)
    _write_proposal(approved, "p-oldest", age_days=30)
    _write_proposal(approved, "p-middle", age_days=14)
    _write_proposal(approved, "p-fresh", age_days=2)  # excluded
    loaded = spm._load_approved_proposals(tmp_path)
    spec = spm.detect_stuck_proposals(loaded, threshold_days=7)
    assert spec is not None
    assert spec["details"]["stuck_count"] == 3
    body = spec["body"]
    oldest_pos = body.index("p-oldest")
    middle_pos = body.index("p-middle")
    newest_pos = body.index("p-newest-stuck")
    assert oldest_pos < middle_pos < newest_pos, "must sort oldest-first"
    assert "p-fresh" not in body


def test_severity_magnitude_escalates_at_three_stuck(tmp_path: Path) -> None:
    """1–2 stuck = magnitude 1 (advisory); 3+ = magnitude 2 (broken pipeline)."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "p1", age_days=10)
    _write_proposal(approved, "p2", age_days=10)
    loaded = spm._load_approved_proposals(tmp_path)
    spec2 = spm.detect_stuck_proposals(loaded, threshold_days=7)
    assert spec2 is not None
    assert spec2["details"]["magnitude"] == 1

    _write_proposal(approved, "p3", age_days=10)
    loaded = spm._load_approved_proposals(tmp_path)
    spec3 = spm.detect_stuck_proposals(loaded, threshold_days=7)
    assert spec3 is not None
    assert spec3["details"]["magnitude"] == 2


def test_signature_is_pod_scoped_and_stable(tmp_path: Path) -> None:
    """Signature should dedup across runs and not vary with content —
    re-observing the same condition bumps the existing Signal rather
    than spawning duplicates."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "p1", age_days=10)
    spec_a = spm.detect_stuck_proposals(
        spm._load_approved_proposals(tmp_path), threshold_days=7,
    )
    _write_proposal(approved, "p2", age_days=15)
    spec_b = spm.detect_stuck_proposals(
        spm._load_approved_proposals(tmp_path), threshold_days=7,
    )
    assert spec_a is not None and spec_b is not None
    assert spec_a["signature"] == spec_b["signature"], (
        "adding another stuck proposal must not change the pod-scoped signature"
    )


def test_malformed_proposal_files_are_skipped(tmp_path: Path) -> None:
    """A bad JSON file in approved/ shouldn't crash the monitor — just
    silently skip it. Operators sometimes drop scratch files there."""
    approved = tmp_path / "proposals" / "approved"
    approved.mkdir(parents=True)
    (approved / "garbage.json").write_text("{ not valid json")
    (approved / "p-valid.json").write_text(json.dumps({
        "id": "p-valid", "target_bot": "team_bot_a", "type": "investigation",
    }))
    old = time.time() - 10 * 86400
    os.utime(approved / "p-valid.json", (old, old))
    loaded = spm._load_approved_proposals(tmp_path)
    assert len(loaded) == 1
    spec = spm.detect_stuck_proposals(loaded, threshold_days=7)
    assert spec is not None
    assert spec["details"]["stuck_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# run() — integration with signals.store
# ─────────────────────────────────────────────────────────────────────────────


def test_run_dry_run_does_not_write_signals(tmp_path: Path, capsys) -> None:
    """--dry-run mode prints would-fire specs without touching signals.store."""
    approved = tmp_path / "proposals" / "approved"
    _write_proposal(approved, "p1", age_days=10)
    kept, n_fired, n_resolved = spm.run(tmp_path, dry_run=True)
    assert n_fired == 1
    assert n_resolved == 0
    captured = capsys.readouterr()
    assert "would_observe" in captured.out
    # No signals dir created
    assert not (tmp_path / "signals").exists()
