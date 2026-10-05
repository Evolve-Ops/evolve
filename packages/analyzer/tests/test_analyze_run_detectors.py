"""Regression test for the analyze.run_detectors / get_detector_enabled import bug.

The bug: analyze.py:853 called ``get_detector_enabled(cfg, detector_name)``
but the import block at the top only pulled ``resolve_network_path``,
``is_module_enabled``, ``sign_proposal`` (since retired), and ``bot_home``
from evolve_config. Every invocation crashed with NameError at the first
``_run(...)`` call, which broke the per-bot metrics writer chain
(observed: pod_silent alerts firing because metrics/<date>/<bot>.json
files were never written — security_bot's writer stopped after personal_bot on
2026-05-10).

This test pins the contract by smoke-testing run_detectors with all
detectors disabled (so we don't exercise the heavy detector functions
themselves, only the import-resolution path through ``_run``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import analyze  # noqa: E402


def test_get_detector_enabled_is_importable_from_analyze():
    """The name must be bound at module scope after import."""
    assert hasattr(analyze, "get_detector_enabled"), (
        "analyze.py must import get_detector_enabled from evolve_config — "
        "without it the run_detectors loop crashes on the first _run call"
    )


def test_run_detectors_with_all_disabled_returns_empty_no_error():
    """All detectors disabled → empty list, no NameError.

    This is the smoke test that would have caught the original bug. The
    _run inner function calls get_detector_enabled(cfg, name) before
    invoking the detector body, so reaching ANY detector exercises the
    import — disabling all detectors short-circuits inside _run without
    touching the detector implementations.
    """
    detector_names = [
        "zero_activity", "unexpected_billing_mode", "high_maintenance_ratio",
        "declining_resolution", "promise_breach",
        "efficiency_problems", "low_satisfaction", "application_abandonment",
        "promise_resolution", "slack_quality_drop",
    ]
    cfg = {
        "modules": {
            "analysis": {
                "detectors": {name: {"enabled": False} for name in detector_names},
            }
        }
    }
    result = analyze.run_detectors(
        bot_id="testbot",
        history=[],
        days=7,
        shared_dir="",
        network_config=cfg,
        cal=None,
    )
    assert result == []
