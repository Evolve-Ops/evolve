"""Wiring gate for the app growth-log backstop daemon.

``ai.evolve.evolve.growth-sweep`` has to be BOTH in ``expected_plist_labels``
and installed by ``install_evolve_infra_jobs``. Getting only one of the two
right is the ``autonomy-limits`` failure recorded in deploy.py's own label-set
comment: the installer ran but the label was missing, so the orphan-sweeper
deleted the plist on every ``evolve-admin upgrade`` and the daemon sat dead
until something happened to re-run the installer.

The installer check is source-level on purpose. Actually calling
``install_evolve_infra_jobs`` writes to /Library/LaunchDaemons; the thing worth
pinning here is the pairing, and an AST walk of the function body pins it
without root.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.deploy import (  # noqa: E402
    expected_plist_labels,
    install_evolve_infra_jobs,
)

LABEL = "ai.evolve.evolve.growth-sweep"
INSTALLER = "_install_launchd_growth_sweep"


def test_growth_sweep_label_is_expected_so_the_orphan_sweeper_spares_it():
    network = {"members": ["team_bot_a", "admin_bot", "evolve"], "bots": {}}
    assert LABEL in expected_plist_labels(network)


def test_install_evolve_infra_jobs_actually_installs_it():
    tree = ast.parse(inspect.getsource(install_evolve_infra_jobs).lstrip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert INSTALLER in called, (
        f"{LABEL} is in expected_plist_labels but nothing installs it — the "
        "label alone only tells the orphan-sweeper to leave a plist that never "
        "gets written."
    )


def test_the_sweep_script_the_plist_points_at_exists():
    # A plist whose ProgramArguments name a missing script fails at 03:40 with
    # a launchd spawn error nobody reads.
    script = Path(__file__).parents[3] / "packages" / "analyzer" / "app_growth_sweep.py"
    assert script.is_file(), f"missing sweep script: {script}"
