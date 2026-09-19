"""generators.auth_drift_filler — Permission-config drift proposal author.

Consumes ``perm_config_drift`` Signals from ``permission_monitor`` and
emits one ``ConfigPatch`` Proposal per drifted field, restoring the
field to its baseline value. Same pattern as ``cron_caps_filler`` —
closes the §13 resolver-pattern gap on auth/permission config drift.

Why one proposal per field (not one per bot):
- Operators routinely accept some drifts as intentional changes (e.g.
  tightening tools.exec.security from 'full' to 'low' even though the
  pod default is still 'full') and reject others. Per-field proposals
  let the operator approve à la carte; a bundled proposal would force
  all-or-nothing.
- The applier (ConfigPatch) handles one ``target_path`` at a time, so
  the per-field shape is also the natural unit for the apply pipeline.

The signal's ``details.diffs`` carries the {field: {expected, observed}}
map already, so this generator doesn't need to re-read openclaw.json.
"""

from generators.auth_drift_filler.observe import (
    AuthDriftFillerContext,
    observe,
)

__all__ = ["AuthDriftFillerContext", "observe"]
