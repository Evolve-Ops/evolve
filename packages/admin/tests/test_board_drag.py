"""The board page's press-and-hold drag (D-BI1), proven against board.html.

The board page is one plain-JS file with no framework and no build step
(D-MB3), so there is nothing to import and no JS unit runner in this package.
Same answer as ``test_sw_fetch_behavior`` / ``test_ordered_bot_ids``: a Node
harness (``tests/js/board_drag_harness.mjs``) evaluates the REAL page script
in a mock browser scope, and this wrapper plugs it into the pytest suite.

The invariant that matters most is the first one the harness checks: a drag
onto a lane header posts EXACTLY the request the move sheet posts. The page
has one move path and one assign path; the gesture is a second way to call
them, never a second implementation that can drift.

Skips cleanly when ``node`` is not on PATH, the way the browser-smoke tests
``importorskip`` Playwright — it must never red CI on a runner without node.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "board_drag_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_board_drag_invariants():
    assert _HARNESS.is_file(), f"missing drag harness at {_HARNESS}"
    result = subprocess.run(
        ["node", str(_HARNESS)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "board drag invariant(s) failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "all board drag invariants hold" in result.stdout
