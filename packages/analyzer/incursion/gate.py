"""incursion.gate — the one way these detectors ask "was this authorized?".

A thin wrapper over :func:`drift_authorization.explain` that adds the two
things every incursion caller needs and the gate itself deliberately does not
provide:

* **A gate that raises cannot silence a detector.** Any exception out of the
  gate is logged and answered ``None`` — unexplained — so the finding pages.
  A suppression gate must fail toward doing the work; this is the same
  contract (and the same shape) as ``audit._explain_drift``, duplicated here
  rather than imported because ``audit`` imports this package and the reverse
  arrow would be a cycle.
* **Read-only passes leave no trace.** ``incursion.report`` runs a full pass
  on a live pod to print a coverage table; ``memo=False`` keeps that pass from
  writing the gate's explanation memo, which is otherwise a real write under
  ``{shared_dir}/security/``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import drift_authorization

logger = logging.getLogger(__name__)


def explain(
    kind: str,
    target: str,
    shared_dir: Path,
    *,
    content_hash: str = "",
    read_only: bool = False,
) -> "drift_authorization.Explanation | None":
    """Is this change accounted for? ``None`` means no, and ``None`` pages."""
    change = drift_authorization.DriftChange(
        kind=kind, target=target, content_hash=content_hash,
    )
    try:
        return drift_authorization.explain(
            change, shared_dir, memo=not read_only,
        )
    except Exception as exc:  # noqa: BLE001 — an unusable gate must not suppress
        logger.warning(
            "incursion: authorized-change gate failed for %s (%s) — treating "
            "the change as unexplained: %s", kind, target, exc,
        )
        return None
