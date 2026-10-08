"""The board detail sheet's D-TM2/5/8 rendering, proven against board.html.

Same shape as ``test_board_drag.py``: the board page is one plain-JS file
with no framework and no build step (D-MB3), so there is nothing to import
and no JS unit runner in this package. A Node harness
(``tests/js/board_sheet_tracker_harness.mjs``) evaluates the REAL page
script in a mock browser scope and this wrapper plugs it into the pytest
suite. Skips cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "board_sheet_tracker_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_sheet_tracker_invariants():
    assert _HARNESS.is_file(), f"missing sheet-tracker harness at {_HARNESS}"
    result = subprocess.run(
        ["node", str(_HARNESS)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "board sheet tracker invariant(s) failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "all board sheet tracker invariants hold" in result.stdout
