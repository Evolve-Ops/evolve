"""testing — Synthetic test harness for L1 arbiter + appliers.

Helpers for tests and local experimentation. Not used in production.
"""

from testing.harness import (
    TestHarness,
    make_investigation_proposal,
    make_config_patch_proposal,
    make_workflow_proposal,
    make_charter,
)

__all__ = [
    "TestHarness",
    "make_investigation_proposal",
    "make_config_patch_proposal",
    "make_workflow_proposal",
    "make_charter",
]
