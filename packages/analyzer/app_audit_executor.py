#!/usr/bin/env python3
"""
app_audit_executor.py — auto_fix executor + cross-app conflict guard.

When Stage 3b decides an observation should be ``auto_fix``, the executor:
  1. Checks cross-app conflicts (§5.6) — does the affected file appear in
     another app's manifest? If yes, the transformation is converted into
     a ``conflict_notice`` outbox record instead of being applied.
  2. Looks up the named transformation in the whitelist (§5.2). Unknown
     kinds fall back to ``propose``.
  3. In v1 calibration mode (default), the runner already demoted
     ``auto_fix`` → ``propose`` before calling here, so the actual apply
     paths are unreachable. We still implement the conflict check, since
     conflict notices are independent of calibration.

The executor is **idempotent and safe-by-default** — every transformation
function returns ``(applied: bool, summary: str)`` and refuses anything
outside its narrow contract. New transformations are added one at a time,
not by relaxing existing ones.

See docs/spec-app-audit-2026-05-16.md §5.2 (whitelist), §5.6 (conflict).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── Result types ────────────────────────────────────────────────────────────


@dataclass
class ExecutorOutcome:
    """What the executor did (or refused to do) for one auto_fix decision."""
    applied: bool                       # True iff a transformation actually ran
    transformation: str                 # Kind that was attempted
    summary: str                        # One-line human-readable result
    # When applied=False AND conflict is set, the runner emits a
    # conflict_notice outbox record listing the affected apps.
    conflict: "ConflictReport | None" = None


@dataclass
class ConflictReport:
    """Cross-app conflict surfaced by the guard."""
    file_path: str
    affected_apps: list[dict] = field(default_factory=list)
    # affected_apps entries: {pkg_id, app_id, display_name, role}
    # role is "owner" | "dependency" | "cron_script" — how the conflict app
    # references the file, so the operator can decide whether to coordinate
    # or remove the dependency.


# ── Conflict detection ──────────────────────────────────────────────────────
#
# Reads ALL manifests on the bot (the runner has direct read access since
# it runs as the bot user) and looks for any that reference the file path
# in their files[], dependencies[], or crons[] lists.


def find_conflicts(
    target_path: str,
    auditing_app_id: str,
    other_manifests: list[dict],
) -> ConflictReport | None:
    """Return a ConflictReport if any other manifest references *target_path*.

    Returns None when no other app touches the file (transformation is safe).
    The auditing app's own manifest is implicitly excluded — it's allowed
    to modify its own files. Comparison is path-normalized; leading "./" and
    leading "/" are stripped so manifest-relative variants don't trip false
    negatives.
    """
    norm_target = _normalize_path(target_path)
    affected: list[dict] = []

    for m in other_manifests:
        if not isinstance(m, dict):
            continue
        other_id = m.get("id") or ""
        if other_id == auditing_app_id:
            continue
        if m.get("status") in ("deprecated", "hidden", "dormant"):
            continue
        # Skip apps that are themselves not eligible for audits when
        # checking ownership — they're inert and unlikely to break.
        role = _references_path(m, norm_target)
        if role is None:
            continue
        affected.append({
            "pkg_id": m.get("pkg_id", ""),
            "app_id": other_id,
            "display_name": m.get("display_name") or m.get("name") or other_id,
            "role": role,
        })

    if not affected:
        return None
    return ConflictReport(file_path=target_path, affected_apps=affected)


def _normalize_path(p: str) -> str:
    s = (p or "").strip()
    if s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    return s


def _references_path(manifest: dict, norm_target: str) -> str | None:
    """Return the role string if *manifest* references *norm_target*, else None."""
    for rec in manifest.get("files") or []:
        if isinstance(rec, dict) and _normalize_path(rec.get("path", "")) == norm_target:
            return "owner"
    for rec in manifest.get("dependencies") or []:
        if isinstance(rec, dict) and _normalize_path(rec.get("path", "")) == norm_target:
            return "dependency"
    for cron in manifest.get("crons") or []:
        script_tokens: list[str] = []
        if isinstance(cron, dict):
            script_field = cron.get("script") or cron.get("script_path") or ""
            script_tokens = script_field.split() if script_field else []
        elif isinstance(cron, str):
            parts = cron.split()
            # Drop the leading 5 schedule fields (or 1 for @keyword)
            if parts and parts[0].startswith("@"):
                script_tokens = parts[1:]
            elif len(parts) >= 6:
                script_tokens = parts[5:]
        # Find any script-shaped token in the command line and match it.
        for t in script_tokens:
            t = t.strip("'\"")
            if t.endswith((".py", ".sh", ".rb", ".js")):
                if _normalize_path(t) == norm_target:
                    return "cron_script"
    return None


# ── Transformation whitelist (v1 = empty in calibration mode) ───────────────
#
# Each entry is (kind, fn). The fn takes (manifest, workspace, evidence_dict)
# and returns ExecutorOutcome. In v1 we keep the whitelist empty — calibration
# mode demotes auto_fix to propose before reaching the executor anyway.
# Adding a transformation requires:
#   1. Implement the fn here.
#   2. Add it to TRANSFORMATIONS.
#   3. Write tests covering the safe path AND the cross-app conflict path.
#   4. Document the safety contract in docs/spec-app-audit-2026-05-16.md §5.2.


# Signature for transformation functions:
#   fn(manifest: dict, workspace: Path, evidence: dict, other_manifests: list[dict])
#       -> ExecutorOutcome
TransformationFn = Callable[
    [dict, Path, dict, list[dict]], ExecutorOutcome,
]


TRANSFORMATIONS: dict[str, TransformationFn] = {
    # Whitelist intentionally empty in v1 — see module docstring.
}


# ── Execution entry point ───────────────────────────────────────────────────


def execute_auto_fix(
    *,
    transformation: str,
    manifest: dict,
    workspace: Path,
    evidence: dict,
    other_manifests: list[dict],
) -> ExecutorOutcome:
    """Run one auto_fix transformation against *manifest*.

    Pre-conditions enforced here:
      - Transformation kind is in TRANSFORMATIONS (else: not applied, summary
        names the reason; runner converts the auto_fix to a propose).
      - The target file (extracted from `evidence`) isn't shared with another
        manifest on the bot (else: not applied, conflict report attached;
        runner emits a conflict_notice outbox record).

    The runner is responsible for honoring calibration mode BEFORE calling
    this function — see ``runner._maybe_demote_auto_fix``.
    """
    # Conflict check fires BEFORE the whitelist check (§5.6): the operator
    # needs to know about a cross-app conflict regardless of whether the
    # named transformation is implementable. A conflict_notice surfaces the
    # affected apps; the whitelist check only matters if we'd otherwise
    # apply the transformation.
    target_path = _evidence_target_path(evidence)
    if target_path:
        app_id = manifest.get("id") or ""
        conflict = find_conflicts(target_path, app_id, other_manifests)
        if conflict is not None:
            return ExecutorOutcome(
                applied=False,
                transformation=transformation,
                summary=(
                    f"cross-app conflict on {target_path}: "
                    f"{len(conflict.affected_apps)} other app(s) reference this file"
                ),
                conflict=conflict,
            )

    if transformation not in TRANSFORMATIONS:
        return ExecutorOutcome(
            applied=False,
            transformation=transformation,
            summary=(
                f"transformation {transformation!r} not in whitelist; "
                "falling back to propose"
            ),
        )

    fn = TRANSFORMATIONS[transformation]
    return fn(manifest, workspace, evidence, other_manifests)


def _evidence_target_path(evidence: dict) -> str:
    """Extract the file path the transformation would touch.

    Stage 3a's evidence list is loose (file:line refs, manifest field refs).
    We look for a path-shaped string. Returning "" disables the conflict
    check for that transformation, which is only acceptable for transformations
    that don't touch any file at all.
    """
    if not isinstance(evidence, dict):
        return ""
    # Direct field
    p = evidence.get("path") or evidence.get("file_path") or ""
    if p:
        return str(p).split(":", 1)[0].strip()
    # First evidence string that looks like a path
    ev_list = evidence.get("evidence") or evidence.get("refs") or []
    if isinstance(ev_list, list):
        for v in ev_list:
            sv = str(v)
            if "/" in sv or sv.endswith((".py", ".sh", ".md", ".json")):
                return sv.split(":", 1)[0].strip()
    return ""
