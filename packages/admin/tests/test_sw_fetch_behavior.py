"""Service-worker deploy-safety behaviour, proven against the real sw.js.

A fleet promote that changes admin static assets must NEVER lock an
operator out of the admin UI. The original incident: the SW's fetch
handler resolved ``respondWith`` with ``undefined`` (``caches.match('/')``
on an empty cache) for a navigation during the deploy restart window —
which throws "FetchEvent.respondWith received an error: TypeError: Load
failed" and blanks the SPA shell until the operator clears site data.

There's no JS unit runner in this package, so we exercise the actual
``sw.js`` in a mock ServiceWorkerGlobalScope via a small Node harness
(``tests/js/sw_fetch_harness.mjs``) and assert the invariants from here.
The harness is the durable, executable proof artifact; this wrapper
plugs it into the pytest suite. It skips cleanly when ``node`` isn't on
PATH (mirrors how the browser-smoke tests ``importorskip`` Playwright),
so it never reds CI on a runner without node.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "sw_fetch_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_sw_deploy_safety_invariants():
    assert _HARNESS.is_file(), f"missing SW harness at {_HARNESS}"
    result = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The harness prints one ``ok``/``FAIL`` line per invariant and exits
    # non-zero if any failed — surface its full output on failure so the
    # offending invariant is obvious without re-running by hand.
    assert result.returncode == 0, (
        "SW deploy-safety invariant(s) failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "all deploy-safety invariants hold" in result.stdout
