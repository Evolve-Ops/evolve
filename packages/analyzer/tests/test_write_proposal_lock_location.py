"""Tests for analyze.write_proposal lockfile placement.

Pins the fix in [docs/proposal-root-cause-audit-2026-06-09.md] Finding 4:
the per-pattern-key flock target lives in ``proposals/.locks/`` rather than
inside ``proposals/pending/``. Putting it inside pending/ left a 0-byte
orphan that every ``os.listdir(pending_dir)`` consumer had to learn to
skip (see audit.py:1786). Keeping it out of pending/ removes that burden.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import analyze  # noqa: E402


def test_lockfile_lives_outside_pending(tmp_path: Path):
    proposal_id = analyze.write_proposal(
        {
            "pattern_key": "bot-a/high_maintenance_ratio",
            "title": "test",
            "summary": "test",
        },
        str(tmp_path),
    )
    assert proposal_id, "write_proposal should return a non-empty id"

    pending = tmp_path / "proposals" / "pending"
    locks = tmp_path / "proposals" / ".locks"

    # pending/ contains exactly the proposal JSON — no .lock-* orphan.
    pending_entries = sorted(p.name for p in pending.iterdir())
    assert pending_entries == [f"{proposal_id}.json"], (
        f"pending/ should only contain the proposal JSON, got {pending_entries}"
    )

    # .locks/ contains the per-pattern-key lockfile under its sanitized name
    # ("/" sanitized to "_").
    assert locks.is_dir(), "proposals/.locks/ should be created"
    lock_entries = sorted(p.name for p in locks.iterdir())
    assert lock_entries == ["bot-a_high_maintenance_ratio.lock"], (
        f"unexpected lockfile layout under .locks/: {lock_entries}"
    )


def test_lockfile_for_empty_pattern_key(tmp_path: Path):
    """Proposals without a pattern_key still acquire a generic lock."""
    proposal_id = analyze.write_proposal(
        {"title": "no pattern key", "summary": "test"},
        str(tmp_path),
    )
    assert proposal_id

    pending = tmp_path / "proposals" / "pending"
    locks = tmp_path / "proposals" / ".locks"

    assert sorted(p.name for p in pending.iterdir()) == [f"{proposal_id}.json"]
    assert sorted(p.name for p in locks.iterdir()) == ["_generic.lock"]
