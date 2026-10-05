"""Canonical bot-list ordering, proven against the real page sources.

Operator regression 2026-08-31 (evolve-stable-616): Maintenance › Status
rendered "No bots configured." while the same page's header read 9/9 bots
online. ``orderedBotIds`` filtered every id through ``isScaffoldOnlyBot``,
a predicate written for network.json CONFIG records ("no role AND no user
AND no port ⇒ scaffold-only phantom", EVO-SEP-S4). The
``/api/gateway/status`` payload is STATUS-shaped — ``{gateway_running,
gateway_reachable, ts, gateway_pid, source}`` — so every bot satisfied the
predicate and the empty-state branch fired. The same silent-[] hazard hit
every non-config caller (the posture inventories, the backup config grid).

There's no JS unit runner in this package, so — the pattern
``test_sw_fetch_behavior.py`` established — the real ``bot-detail.js`` and
``maintenance.js`` are evaluated in a mock browser scope by a small Node
harness (``tests/js/ordered_bot_ids_harness.mjs``) and the invariants are
asserted from here. The harness is the durable, executable proof artifact;
this wrapper plugs it into the pytest suite and skips cleanly when ``node``
isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "ordered_bot_ids_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ordered_bot_ids_invariants():
    assert _HARNESS.is_file(), f"missing harness at {_HARNESS}"
    result = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The harness prints one ``ok``/``FAIL`` line per invariant and exits
    # non-zero if any failed — surface its full output so the offending
    # invariant is obvious without re-running by hand.
    assert result.returncode == 0, (
        "orderedBotIds invariant(s) failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "all orderedBotIds invariants hold" in result.stdout
