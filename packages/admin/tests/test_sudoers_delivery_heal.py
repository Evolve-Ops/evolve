"""Sudoers grants for the delivery-monitor heal path (U2.2).

Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §8. The heal
path in packages/analyzer/delivery_monitor.py (Probes.launchctl_print /
launchctl_kickstart / launchctl_bootstrap) issues, as the evolve user:

    sudo -n /bin/launchctl print system/ai.evolve.<bot>.<app>
    sudo -n /bin/launchctl kickstart system/ai.evolve.<bot>.<app>
    sudo -n /bin/launchctl bootstrap system /Library/LaunchDaemons/ai.evolve.<...>.plist

sudoers matches the FULL argv, so the pre-existing `kickstart -k
system/ai.evolve.*` grant (repo-puller daemon restarts) does NOT cover
the heal path's no-``-k`` form — one-shot calendar jobs must not be
kickstart-killed. These tests pin the grant↔probe alignment shut the
same way test_writer_sudoers_alignment.py does for the config writers:
if a future edit drops or reshapes a grant, the failure message names
the probe that breaks.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.setup_wizard import _render_evolve_sudoers  # noqa: E402


@pytest.fixture(scope="module")
def sudoers_content() -> str:
    content = _render_evolve_sudoers()
    assert content is not None, (
        "_render_evolve_sudoers returned None — openclaw not discoverable "
        "at test time. Test environment needs openclaw installed."
    )
    return content


# The exact grant lines the heal probes rely on. Full binary paths, no
# backslash-escaped dots, no trailing /* (macOS visudo rejects it).
HEAL_GRANTS = [
    "evolve ALL=(root) NOPASSWD: /bin/launchctl print system/ai.evolve.*",
    "evolve ALL=(root) NOPASSWD: /bin/launchctl kickstart system/ai.evolve.*",
    # Pre-existing §9 grant the bootstrap leg of heal reuses — pinned here
    # so a refactor of section 9 can't silently strand the heal path.
    "evolve ALL=(root) NOPASSWD: /bin/launchctl bootstrap system "
    "/Library/LaunchDaemons/ai.evolve.*.plist",
]


def test_heal_grants_present(sudoers_content: str) -> None:
    lines = {ln.strip() for ln in sudoers_content.splitlines()}
    missing = [g for g in HEAL_GRANTS if g not in lines]
    assert not missing, (
        f"Missing delivery-monitor heal grant(s): {missing}. "
        "delivery_monitor.Probes (launchctl_print/_kickstart/_bootstrap) "
        "probes these with sudo -n; without the grant every heal reports "
        "result=no_grant and the operator gets 'couldn't attempt the "
        "restart' on every miss."
    )


def test_heal_kickstart_grant_has_no_dash_k(sudoers_content: str) -> None:
    """The heal kickstart is one-shot: `-k` would kill an in-flight run.

    Both forms legitimately coexist (the -k form is the repo-puller's
    daemon-restart grant); this pins that the no-``-k`` form is present
    as its own line and wasn't 'simplified' into the -k one.
    """
    plain = [
        ln for ln in sudoers_content.splitlines()
        if ln.strip().endswith("/bin/launchctl kickstart system/ai.evolve.*")
    ]
    assert plain, "no-dash-k kickstart grant for system/ai.evolve.* missing"
    assert all("-k" not in ln for ln in plain)


def test_heal_grants_hygiene(sudoers_content: str) -> None:
    """House sudoers rules: no escaped dots, no trailing /* wildcard."""
    for line in sudoers_content.splitlines():
        if "launchctl" not in line or line.lstrip().startswith("#"):
            continue
        assert "\\." not in line, f"backslash-escaped dot in grant: {line!r}"
        assert not line.rstrip().endswith("/*"), f"trailing /* in grant: {line!r}"


def test_rendered_sudoers_passes_visudo(sudoers_content: str) -> None:
    """Syntax-validate the render with visudo -c, as the installers do."""
    visudo = Path("/usr/sbin/visudo")
    if not visudo.exists():
        pytest.skip("visudo not available on this host")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "evolve"
        tmp.write_text(sudoers_content)
        tmp.chmod(stat.S_IRUSR | stat.S_IRGRP)  # 0440 — visudo rejects loose modes
        r = subprocess.run(
            [str(visudo), "-c", "-f", str(tmp)], capture_output=True, text=True
        )
        assert r.returncode == 0, (
            f"visudo rejected rendered sudoers:\n{r.stderr or r.stdout}"
        )
