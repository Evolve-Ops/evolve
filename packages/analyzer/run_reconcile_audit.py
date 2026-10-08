#!/usr/bin/env python3
"""Daemon entry script for ``reconcile_audit``.

Invoked by the ``ai.evolve.evolve.reconcile-audit`` LaunchDaemon
(installed via ``_install_launchd_reconcile_audit`` in
``packages/admin/evolve_admin/deploy.py``).

Wrapper-only — all the logic lives in
``packages/analyzer/reconcile_audit.py``. Kept separate so the
daemon's entry point matches the standard ``run_*.py`` pattern that
deploy.py's ``_install_launchd`` helper discovers, while the logic
stays import-friendly for tests and ad-hoc CLI invocation.
"""

from __future__ import annotations

import sys

from reconcile_audit import main


if __name__ == "__main__":
    sys.exit(main())
