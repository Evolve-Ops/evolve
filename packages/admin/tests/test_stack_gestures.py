"""The Stack page's swipe gestures (D-ST3/D-ST4/D-ST9), proven against the
real stack.html.

Same shape as test_board_drag.py: the page is one plain-JS file with no
framework and no build step (D-MB3), so there is no module to import and no
JS unit runner in this package. A Node harness
(``tests/js/stack_gesture_harness.mjs``) evaluates the REAL page script in
a mock browser scope; this wrapper plugs it into the pytest suite.

Skips cleanly when ``node`` is not on PATH, the same way the browser-smoke
tests ``importorskip`` Playwright — it must never red CI on a runner
without node.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "stack_gesture_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_stack_gesture_invariants():
    assert _HARNESS.is_file(), f"missing stack gesture harness at {_HARNESS}"
    result = subprocess.run(
        ["node", str(_HARNESS)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "stack gesture invariant(s) failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "all stack gesture invariants hold" in result.stdout
