"""generators.manifest_quality — Manifest-lifecycle responder.

Consumes manifest-lifecycle Signals from the ``compliance_scan``
monitor and emits one Investigation Proposal per firing signal.
Covers three signal types: ``stale``, ``test_failing``,
``validation_error``. ``missing_required_field`` Signals are
intentionally ignored (see signal_proposals docstring).

Migration note: replaces the manifest-shaped recs that
ComplianceAdapter used to emit by reading
``{shared_dir}/better-engine/cache/compliance-{bot}.json``. After
this generator and its workspace_inventory / workspace_security
siblings ship, ComplianceAdapter is retired.
"""

from generators.manifest_quality.observe import (
    ManifestQualityContext,
    observe,
)

__all__ = ["ManifestQualityContext", "observe"]
