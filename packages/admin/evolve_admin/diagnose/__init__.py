"""Bundled investigation team_bot_a for the Evolve mini.

Provides standard probes for the "is the SUT actually responding at every
layer" question. Designed for incident response — at 3 AM you reach for
this, not 30 minutes of ad-hoc bash heredocs.

See internal/spec-etr-diagnose-tool-2026-04-26.md for the design rationale
(motivated by the 2026-04-25 SSH-wedge incident where investigation
tooling was rewritten ad-hoc and then lost).

Usage:
    from evolve_admin.diagnose import run_all, format_report
    result = run_all(categories=['network', 'process'])
    print(format_report(result))

Or as CLI:
    evolve-admin diagnose
    evolve-admin diagnose --json
    evolve-admin diagnose --category=network,process
"""
from .probes import (
    probe_network,
    probe_process,
    probe_subprocess,
    probe_schema,
)
from .aggregate import run_all, format_report, format_json, aggregate_verdict

__all__ = [
    "probe_network",
    "probe_process",
    "probe_subprocess",
    "probe_schema",
    "run_all",
    "format_report",
    "format_json",
    "aggregate_verdict",
]
