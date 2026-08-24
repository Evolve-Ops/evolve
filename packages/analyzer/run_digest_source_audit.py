#!/usr/bin/env python3
"""Daemon entry script for ``digest_source_audit``.

Wrapper-only — all the logic lives in
``packages/analyzer/digest_source_audit.py``. Kept separate so
deploy.py's ``_install_launchd`` helper finds the standard
``run_*.py`` pattern while the logic stays import-friendly.
"""

from __future__ import annotations

import sys

from digest_source_audit import main


if __name__ == "__main__":
    sys.exit(main())
