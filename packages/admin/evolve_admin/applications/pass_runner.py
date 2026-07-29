"""Coherence pass runner — hydrate v7-arc instances before running passes.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.

Production validation 2026-06-06 caught a silent-miss pattern: the
four coherence passes (A, C1, C2, C3) all operate on a manifest dict.
But four production bots (team-bot-a, security-bot, team-bot-c, personal-bot-b)
store apps as **v7-arc Instance** manifests — stub objects pointing at
a Spec in the gallery. Without hydration, Pass A reports 0 findings on
those instances because their scheduled_actions[]/files[]/etc. are
empty until the Spec is overlaid.

This module provides the one helper every caller should use BEFORE
running any pass:

    manifest = hydrate_if_needed(manifest, shared_dir)
    findings = run_pass_a(manifest)

Plus a ``run_all_passes`` convenience that does the hydration + runs
the three pure-Python passes (A, C1, orphan) in one call. C2 + C3
stay separate because they're LLM-backed and have their own dispatch
rules (Tier 3 for C2; on-demand for C3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .manifest import MANIFEST_SHAPE_V7_ARC, hydrate_v7_arc_instance


def is_v7_arc_instance(manifest: dict) -> bool:
    """True iff the manifest is a v7-arc Instance stub that needs Spec
    overlay before pass execution."""
    if not isinstance(manifest, dict):
        return False
    return manifest.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC


def hydrate_if_needed(manifest: dict, shared_dir: Path | None) -> dict:
    """Return a hydrated manifest dict, suitable for passing to any
    coherence pass.

    Three cases:
      1. Not a v7-arc instance → returned unchanged.
      2. v7-arc instance + shared_dir given → hydrated.
      3. v7-arc instance + shared_dir is None → returned unchanged
         with a warning printed; callers without shared_dir context
         can't hydrate.

    Idempotent — calling twice on a hydrated manifest is a no-op.
    """
    if not is_v7_arc_instance(manifest):
        return manifest
    if shared_dir is None:
        # Defensive: caller didn't pass shared_dir but should have.
        # Don't crash; return the instance as-is so passes still run
        # (they'll report empty findings, but that's better than a
        # NoneType error in production).
        return manifest
    return hydrate_v7_arc_instance(manifest, Path(shared_dir))


def run_all_pure_passes(
    manifest: dict,
    *,
    workspace_root: Path | None = None,
    shared_dir: Path | None = None,
) -> dict:
    """Run every pure-Python coherence + reconciliation pass on a manifest.

    Hydrates v7-arc instances first. Returns a dict with one entry
    per pass, each entry a list of findings (or a single verdict for
    orphan check):

        {
            "pass_a":   [CoherenceFinding, ...],   # PR 4
            "pass_c1":  [CoherenceFinding, ...],   # PR 15
            "orphan":    OrphanVerdict,             # PR 14
        }

    workspace_root is optional — passed through to Pass C1 (which
    needs it to read implementing source) and to orphan check (which
    uses it to glob volatile_paths). Pass A doesn't need it.

    LLM-backed passes (C2 via Tier 3 prompt splice; C3 on-demand)
    are NOT run here — those are separate dispatch paths.
    """
    # Late imports to keep this module's import cost cheap.
    from .coherence_pass_a import run_pass_a
    from .coherence_pass_c1 import run_pass_c1
    from .orphan_handling import evaluate_orphan

    manifest = hydrate_if_needed(manifest, shared_dir)

    pass_a_findings = run_pass_a(manifest)

    pass_c1_findings: list = []
    if workspace_root is not None:
        pass_c1_findings = run_pass_c1(manifest, Path(workspace_root))

    # Orphan check works without workspace_files but is more accurate
    # with them. Caller may pass workspace_files via the orphan helper
    # directly when they have a fuller picture.
    orphan_verdict = evaluate_orphan(manifest)

    return {
        "pass_a":   pass_a_findings,
        "pass_c1":  pass_c1_findings,
        "orphan":   orphan_verdict,
    }


def apply_all_pure_passes(
    manifest: dict,
    *,
    workspace_root: Path | None = None,
    shared_dir: Path | None = None,
    workspace_files: set[str] | None = None,
    now_iso: str | None = None,
) -> dict:
    """Run all pure-Python passes AND persist findings to the manifest.

    Unlike ``run_all_pure_passes`` which returns the findings, this
    mutates ``manifest.coherence.findings[]`` (Pass A + C1) and
    ``manifest.reconciliation`` (orphan check) in place. The caller
    writes the mutated manifest back to disk.

    Designed for the Tier 2 runner — single entry point that does all
    three passes with consistent provenance handling, idempotent
    re-runs (accepted signatures preserved), and v7-arc hydration.

    Returns a summary dict the caller can log:

        {
            "status":        "ok | warnings | incoherent",
            "pass_a_count":  int,
            "pass_c1_count": int,
            "orphan_state":  "not_orphan | pending | orphan",
            "by_severity":   {"critical": N, "major": N, ...},
        }
    """
    from datetime import datetime, timezone
    from .coherence_pass_a import apply_pass_a
    from .coherence_pass_c1 import (
        run_pass_c1, write_findings_to_manifest as write_c1,
    )
    from .orphan_handling import evaluate_orphan, record_orphan_verdict

    if shared_dir is not None and is_v7_arc_instance(manifest):
        # Hydration replaces the manifest dict in-place semantically; but
        # hydrate_v7_arc_instance returns a NEW dict overlaying Spec
        # fields on the instance. Re-mutate the caller's dict so the
        # Tier 2 runner's existing _stamp_manifest sees the hydrated
        # data when it writes back.
        hydrated = hydrate_if_needed(manifest, shared_dir)
        if hydrated is not manifest:
            # Copy hydrated keys back into the caller's dict so the
            # write-back persists overlay fields (description from Spec
            # etc.). Don't blow away instance-only fields.
            for k, v in hydrated.items():
                if k not in manifest:
                    manifest[k] = v

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    # Pass A — writes manifest.coherence.findings[] (replacing prior A
    # findings). apply_pass_a preserves coherence_accepted[].
    summary_a = apply_pass_a(manifest, now_iso=now_iso)

    # Pass C1 — appends C1-* findings to manifest.coherence.findings[]
    # without disturbing Pass A's entries (write_findings_to_manifest
    # filters by check_id prefix).
    pass_c1_count = 0
    if workspace_root is not None:
        c1_findings = run_pass_c1(manifest, Path(workspace_root))
        write_c1(manifest, c1_findings, checked_at=now_iso)
        pass_c1_count = len(c1_findings)

    # Orphan check — writes to manifest.reconciliation.
    verdict = evaluate_orphan(
        manifest,
        workspace_files=workspace_files,
        now_iso=now_iso,
    )
    record_orphan_verdict(manifest, verdict)

    return {
        "status":         summary_a.get("status", "ok"),
        "pass_a_count":   summary_a.get("findings_count", 0),
        "pass_c1_count":  pass_c1_count,
        "orphan_state":   verdict.verdict,
        "orphan_authored": verdict.is_authored,
        "by_severity":    summary_a.get("by_severity", {}),
    }
