"""evolve_admin.tools — diagnostic + audit utilities.

Submodules in this package are user-facing diagnostics that read pod
state and report drift against documented invariants. They are
intentionally separate from the appliers in :mod:`evolve_admin.deploy`
so the rule table can serve as an independent source of truth — if
the auditor and the applier ever disagree, that disagreement is a
bug worth surfacing.
"""
