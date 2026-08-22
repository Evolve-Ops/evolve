"""plugins.bootstrap — produce the initial pod baseline.

Spec: docs/spec-plugin-posture-rework-2026-06-06.md §3.

The v2 baseline is a thin policy file: denied_plugins (empty by
default) and expected_load_paths (the pod's plugin dir). No per-bot
overrides, no curated "expected enabled" set — those concepts were the
problem the rework retired. The original spec also included
``trusted_install_sources`` for ``plugin_unverified_source`` alerts;
that field was retired on 2026-06-06 (see spec §1.4 amendment) — see
the baseline module for the rationale.

Called once at first run by monitor.run() via write_default_if_missing().
v1 files written by the original bootstrap are read back by
``baseline.load`` with an in-place migration on next write; no separate
migration step is needed.
"""
from __future__ import annotations

from pathlib import Path

from evolve_util import now_iso as _utc_now_iso

from .baseline import (
    PluginBaseline,
    default_expected_load_paths,
    write as _write_baseline,
    baseline_path,
)


def build_default_baseline() -> PluginBaseline:
    """Produce the v2 baseline (platform-correct plugin load path)."""
    return PluginBaseline(
        bootstrapped_at=_utc_now_iso(),
        required_plugins=[],
        denied_plugins=[],
        expected_load_paths=list(default_expected_load_paths()),
    )


def write_default_if_missing(shared_dir: Path) -> bool:
    """Create the baseline file from build_default_baseline() if missing.

    Returns True if a file was written; False if one already existed.
    """
    if baseline_path(shared_dir).exists():
        return False
    _write_baseline(build_default_baseline(), shared_dir)
    return True
