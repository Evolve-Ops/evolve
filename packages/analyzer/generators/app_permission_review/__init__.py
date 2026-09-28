"""generators.app_permission_review — manifest permissions hygiene generator.

Initial ``permissions:`` blocks are seeded at scan time by the App
Scan's Pass C backfill in ``evolve_admin.applications.scanner`` — the
standalone ``app_permission_bootstrapper`` generator was retired on
2026-05-25 because manifest-completeness gaps should close at the
scan, not pile up as Investigation proposals. This generator keeps
the seeded (or operator-authored) blocks honest:

- **Necessary** — declared entries are still referenced by the app's scripts.
- **Sufficient** — everything the app's scripts actually do is declared.
- **Right-scoped** — wildcards aren't wider than the scripts justify.

Plus a **pod-aware consolidation pass** (parent spec §5) that cross-
references findings against sibling apps on the same bot, converting
some "remove from app A" findings into "move from app A to app B" or
annotating with sibling coverage.

All proposals are ``Investigation``-shaped in v1; operator hand-edits
the manifest. Typed manifest mutation is out of scope (Phase C
territory). See internal/spec-app-permission-review-2026-05-26.md.
"""

from generators.app_permission_review.observe import (
    AppPermissionReviewContext,
    observe,
)

__all__ = ["AppPermissionReviewContext", "observe"]
