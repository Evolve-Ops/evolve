"""reconcile_audit — Signal-producing daily audit for scheduled_actions[] drift.

Why this exists
---------------

The 2026-06-04 Atlas Daily Digest incident was caught reactively:
"why hasn't a digest message arrived in 6 days?" The trio of PRs
that landed before this (#2164 / #2167 / #2168 / #2171) closed the
install pipeline, the namespace, the per-app remediation primitive,
and the pod-wide CLI surface. This daemon closes the proactive
detection gap: every day, it runs ``reconcile_actions(apply=False)``
and emits one Signal per drifted (bot, app) pair.

Had this daemon existed on 2026-06-04, the incident would have
shown up as a Signal on the Alerts page the morning after the
manifest migration landed — instead of waiting six days for the
operator to notice silence.

What it does
------------

Once per day (04:30 UTC, after retention at 03:30 and proposal
auto-resolve at 03:45, before the morning anthropic-admin-ingest at
04:15... actually the latter precedes this one — the order matters
to avoid bunching):

  1. Call ``reconcile_actions(shared_dir, apply=False)`` to walk
     every installed manifest and classify drift against the
     current gallery.

  2. For each ``shape_drift``, ``missing_in_installed``, or
     ``missing_in_gallery`` entry, build a Signal spec with
     signature ``reconcile_audit:scheduled_actions_drift:{bot}:{app}``
     so re-reads don't double-fire and the Alerts UI can collapse
     the right entries together.

  3. Call ``signals.store.observe()`` for each.

  4. ``signals.store.sweep_resolve()`` clears any prior Signals for
     (bot, app) pairs whose drift is gone (operator reconciled,
     or the gallery moved back into shape).

Producer: ``reconcile_audit``
Signal type: ``scheduled_actions_drift``
Scope: pod (one signal per (bot, app), but scope is pod-wide
because the underlying state is shared-dir-relative, not
per-bot).
Severity: ``warn`` — operationally important (a daemon hasn't been
installed that the manifest says should be) but not page-the-
operator urgent. The user can run ``evolve-admin reconcile-actions
--apply`` to fix.

Pure Python, no LLM. Runs as the ``evolve`` user.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "reconcile_audit"
SIGNAL_TYPE = "scheduled_actions_drift"

# Classifications that warrant a Signal. Keep in sync with
# reconcile_actions._REMEDIABLE + MISSING_IN_GALLERY (which is also a
# drift, just one the daemon refuses to auto-remediate — the operator
# still needs to see and decide).
_REPORTABLE_CLASSIFICATIONS: frozenset[str] = frozenset({
    "shape_drift",
    "missing_in_installed",
    "missing_in_gallery",
})


# ─────────────────────────────────────────────────────────────────────────────
# Spec builders — pure functions
# ─────────────────────────────────────────────────────────────────────────────


def _spec_for_drift_report(report: dict) -> dict:
    """Render a Signal spec for one drifted (bot, app) entry.

    ``report`` is the dict shape from ``AppDriftReport.to_dict()``. The
    signature embeds bot + app so different apps on different bots get
    their own Signal — the operator's mental model is per-(bot, app),
    not aggregated, and the Alerts UI can collapse drift across many
    bots-of-the-same-app into a coalesced view if desired.
    """
    bot_id = report.get("bot_id", "?")
    app_id = report.get("app_id", "?")
    classification = report.get("classification", "?")
    detail = report.get("detail", "")
    drifted_ids = report.get("drifted_action_ids") or []

    # signature is the dedup key for signals.store.observe — same key
    # → existing Signal updated, not a new one created. Embedding
    # bot + app means clearing one bot doesn't accidentally resolve
    # another bot's drift.
    signature = make_signature(
        PRODUCER, SIGNAL_TYPE,
        scope_key=f"{bot_id}:{app_id}",
    )

    # Human-readable title — the Alerts page renders this prominently.
    title = (
        f"{app_id} on {bot_id}: scheduled_actions[] drift "
        f"({classification})"
    )

    # Body is the operator's "what do I do" message. Always end with
    # the fix command so paging the operator doesn't require a doc trip.
    body_lines = [
        # identity: see applications.app_identity.resolve_app_id — the
        # reconcile report's own gallery-package column, printed ALONGSIDE
        # the already-resolved ``app_id`` because the drift is measured
        # against a specific gallery package. Not a second answer to "which
        # app is this?"; changing it would change the Signal body.
        f"**{app_id}** on bot **{bot_id}** has scheduled_actions[] drift "
        f"relative to the current gallery package "
        f"(pkg_id `{report.get('pkg_id') or '?'}`):",
        "",
        f"_{detail}_",
        "",
    ]
    if drifted_ids:
        body_lines.append("Drifted action ids:")
        for aid in drifted_ids[:10]:  # cap for readability
            body_lines.append(f"  - `{aid}`")
        if len(drifted_ids) > 10:
            body_lines.append(f"  - ... +{len(drifted_ids) - 10} more")
        body_lines.append("")

    # Auto-remediable cases get a one-command fix; ambiguous cases get
    # the explanation so the operator can decide.
    if classification == "missing_in_gallery":
        body_lines.append(
            "This drift is **not auto-remediated** — actions present in the "
            "installed manifest but not in the gallery may be deliberate "
            "operator augmentations. Decide whether to keep, remove, or "
            "promote into the gallery."
        )
    else:
        body_lines.append("Fix:")
        body_lines.append(
            f"  `sudo evolve-admin apply-actions {bot_id} {app_id} "
            f"--from-gallery`"
        )
        body_lines.append("")
        body_lines.append(
            "Or fix everything drifted pod-wide at once:"
        )
        body_lines.append(
            "  `sudo evolve-admin reconcile-actions --apply`"
        )

    return dict(
        signature=signature,
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details={
            "bot_id":               bot_id,
            "app_id":               app_id,
            # identity: see resolve_app_id — the gallery-package column of
            # this Signal's details schema, as in the body above.
            "pkg_id":               report.get("pkg_id", ""),
            "classification":       classification,
            "drifted_action_ids":   drifted_ids,
            "installed_action_ids": report.get("installed_action_ids") or [],
            "gallery_action_ids":   report.get("gallery_action_ids") or [],
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(shared_dir: Path, *, network: dict | None = None) -> list[dict]:
    """Run reconcile_actions and return Signal specs for reportable drift.

    ``network`` overrides network.json for tests. The reconcile-actions
    call itself is read-only.
    """
    from evolve_admin.applications.reconcile_actions import reconcile_actions

    result = reconcile_actions(
        shared_dir,
        apply=False,
        network=network,
    )

    specs: list[dict] = []
    for report in result.to_dict().get("reports", []):
        if report.get("classification") in _REPORTABLE_CLASSIFICATIONS:
            specs.append(_spec_for_drift_report(report))
    return specs


def run(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    network: dict | None = None,
) -> tuple[set[str], int, int]:
    """Collect drift specs, write Signals, sweep-resolve cleared entries.

    Returns ``(kept_signatures, n_fired, n_resolved)`` — same shape as
    every other Signal-producing monitor in packages/analyzer/, so the
    monitor_coverage daemon's KPIs work without special-casing.
    """
    specs = collect(shared_dir, network=network)
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[reconcile_audit] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: scheduled_actions[] drift cleared",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[reconcile_audit] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point (used by run_reconcile_audit.py and tests)
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "reconcile_audit — daily Signal producer for "
            "scheduled_actions[] drift on installed bots."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Pod-wide shared dir (default: /Users/Shared/evolve).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "No-op flag for compatibility with the standard "
            "_install_launchd invocation shape; this daemon is "
            "already a single-shot."
        ),
    )
    args = parser.parse_args(argv)

    kept, n_fired, n_resolved = run(
        args.shared_dir,
        dry_run=args.dry_run,
    )
    print(
        f"[reconcile_audit] fired={n_fired} "
        f"resolved={n_resolved} kept={len(kept)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
