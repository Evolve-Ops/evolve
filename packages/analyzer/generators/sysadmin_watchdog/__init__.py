"""generators.sysadmin_watchdog — platform-failure monitor + ACL-drift fixer.

Spec: internal/spec-alerts-signal-store-2026-05-07.md (Phase 1b — sysadmin_watchdog
migration to the Signal store).

Reads:
  - Metrics from the metric registry (gateway.up, acl.evolve_read, etc.)
  - Directly checks by resolving each metric via the registry, not by
    re-implementing probes here.

Emits Signals (Alerts page) for:
  - Gateway down (warn → alert when chronic)
  - Evolve plugin not loaded
  - LaunchDaemon not loaded
  - openclaw.json malformed
  - macOS user missing
  - Version drift

Emits Proposals (Self-Improvement) for:
  - ACL drift on a bot's .openclaw/ — autonomous-eligible ConfigPatch with
    a `motivating_signals` link back to the paired ``acl_drift`` Signal.
"""

from generators.sysadmin_watchdog.observe import observe, observe_signals

__all__ = ["observe", "observe_signals"]
