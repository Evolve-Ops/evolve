"""gallery_verify — live install-verify harness for the gallery.

Runs ON a pod as the ``evolve`` user. For each gallery package it drives the
local admin API to install on a test bot, asserts the install is born-healthy
via direct reads, then (by default) tears the app back down and asserts the
pod is residue-free. Invoked as::

    python3 -m evolve_admin.gallery_verify --help

BUILD/UNIT-TEST surface only — the live fleet sweep is an operator Gate-2 step
(see the two runbook commands in the module PR body). The heavy pod imports
live behind the ``LivePodProbe`` / ``LiveAdminApi`` seams so the pure pipeline
and assertion logic import cleanly off-pod for unit tests.
"""

from .model import (  # noqa: F401
    BLOCKED_OAUTH,
    FAIL,
    PASS,
    SKIPPED,
    AppResult,
    Check,
    RunReport,
    StepResult,
)
from .pipeline import run_sweep, verify_one_app  # noqa: F401

__all__ = [
    "run_sweep",
    "verify_one_app",
    "RunReport",
    "AppResult",
    "StepResult",
    "Check",
    "PASS",
    "FAIL",
    "BLOCKED_OAUTH",
    "SKIPPED",
]
