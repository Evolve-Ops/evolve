"""outcome.rewrite_pending_outcomes — atomic, survives an unwritable file.

Regression: the daily ``outcome`` launchd job (user=evolve) crashed with
``PermissionError`` because ``rewrite_pending_outcomes`` did
``open(path, "w")``, which truncates the existing inode and so needs write
permission on the *file*. On the live pod ``feedback/pending-outcomes.jsonl``
is owned by a legacy user mode 0755 (no group/other write), so every run —
even one with zero pending outcomes to rewrite — died at the truncate. The
fix writes a temp file and ``os.replace()``s it into place, which needs
write only on the *directory*.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import outcome  # noqa: E402


def _feedback_file(shared: Path) -> Path:
    return shared / "feedback" / "pending-outcomes.jsonl"


def test_rewrite_round_trips(tmp_path):
    recs = [{"outcome_id": "a", "x": 1}, {"outcome_id": "b", "x": 2}]
    outcome.rewrite_pending_outcomes(recs, str(tmp_path))
    assert outcome.load_pending_outcomes(str(tmp_path)) == recs


def test_rewrite_creates_missing_dir_and_file(tmp_path):
    # No feedback dir yet — rewrite must create it and the (empty) file.
    outcome.rewrite_pending_outcomes([], str(tmp_path))
    assert _feedback_file(tmp_path).exists()
    assert outcome.load_pending_outcomes(str(tmp_path)) == []


def test_rewrite_leaves_no_temp_file(tmp_path):
    outcome.rewrite_pending_outcomes([{"outcome_id": "a"}], str(tmp_path))
    leftovers = list((tmp_path / "feedback").glob(".pending-outcomes.*"))
    assert leftovers == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses the file-mode check, so the repro can't be staged",
)
def test_rewrite_survives_unwritable_existing_file(tmp_path):
    """The crash repro: existing file has no write bit, dir is writable.

    ``open(path, "w")`` raises ``PermissionError`` under this mode; the
    atomic replace must succeed because it only needs write on the
    directory, and it re-homes the inode to the writing user.
    """
    path = _feedback_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"outcome_id": "old"}\n')
    os.chmod(path, 0o444)  # read-only file, no write bit

    # Sanity: a plain truncate-open really does fail under this mode.
    with pytest.raises(PermissionError):
        open(path, "w").close()

    # The fix path succeeds and swaps the content wholesale.
    outcome.rewrite_pending_outcomes([{"outcome_id": "new"}], str(tmp_path))
    assert outcome.load_pending_outcomes(str(tmp_path)) == [{"outcome_id": "new"}]
    # Re-homed inode is writable again (0644).
    assert os.access(path, os.W_OK)
