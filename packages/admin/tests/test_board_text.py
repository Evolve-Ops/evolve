"""board_text.js — the Board/Stack shared `when` and bot-text renderer.

Same shape as ``test_board_sheet_tracker.py``: a Node harness
(``tests/js/board_text_harness.mjs``) runs the REAL file with Intl pinned
to a fixed locale and time zone; this wrapper plugs it into pytest. Skips
cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "board_text_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_text_invariants():
    result = subprocess.run(["node", str(_HARNESS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"board_text invariant(s) failed:\n{result.stdout}\n{result.stderr}"
    assert "all board_text checks passed" in result.stdout
