"""migrations.proposal_v1_to_v2 — One-shot migration of v1 proposals to v2.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §9.

Reads every ``{shared_dir}/proposals/**/*.json``; for files that don't
already look v2-shaped, converts them in place using a deterministic
mapping. After this migration runs, all proposals on disk are v2.

Mapping (spec §9):
  - v1 ``type`` → v2 ``action`` variant:
      "config_change" → ConfigPatch (extracting proposed_change)
      "workflow_change" → WorkflowInstruction
      "investigation" → Investigation
      "script_change" → WorkflowInstruction (pragmatic — script_change was
                        rare; closest v2 equivalent is a workflow doc)
  - v1 ``bot_id`` → v2 ``bot_id``
  - v1 ``pattern_key`` / ``problem`` → v2 ``problem``
  - synthesize ``generator_id = "legacy_analyzer"`` for all migrated
  - synthesize ``dimension = "substrate_health"`` for sysadmin-shaped
    proposals (everything else maps to "utility" as a safe default)
  - ``claim = None`` (legacy proposals have no claim; they route to human
    approval regardless)
  - ``risk_tag`` set to conservative values forcing human approval
  - ``approval_audience = "pod_operator"``
  - preserve original ``id``, ``created_at``, ``evolve_sig`` (as ``signature``)

Idempotent: files already carrying ``schema_version: 2`` are skipped.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write
from evolve_util import now_iso_offset as _now_iso

from schema.provenance import Provenance
from schema.proposal import (
    PROPOSAL_SCHEMA_VERSION,
    ConfigPatch,
    Investigation,
    Proposal,
    RiskTag,
    WorkflowInstruction,
)


# Dimensions to assign based on v1 ``type`` + other heuristics
_SYSADMIN_TYPES = {"config_change", "script_change"}


def _build_action(v1_type: str, v1_data: dict):
    """Convert the v1 ``type`` + ``proposed_change`` payload into a v2 Action."""
    proposed = v1_data.get("proposed_change") or {}
    bot_id = str(v1_data.get("bot_id") or v1_data.get("bot") or "")

    if v1_type == "config_change":
        target_path = str(
            proposed.get("target_path") or proposed.get("path") or "openclaw.json"
        )
        operation = str(proposed.get("operation") or "set")
        value = proposed.get("value")
        return ConfigPatch(
            target_path=target_path, operation=operation, value=value
        )

    if v1_type in ("workflow_change", "script_change"):
        path = str(proposed.get("path") or proposed.get("file") or "evolve/workflow.md")
        content = str(
            proposed.get("content") or proposed.get("text") or "Migrated workflow instruction."
        )
        return WorkflowInstruction(bot_id=bot_id, path=path, content=content)

    # Default: everything else (investigation, unknown, etc.) → Investigation
    context = str(
        v1_data.get("problem")
        or v1_data.get("rationale")
        or "Legacy proposal migrated without action context."
    )
    return Investigation(context=context)


def _build_risk_tag(v1_type: str) -> RiskTag:
    """Choose a conservative risk tag for a migrated proposal.

    Legacy proposals had no claims and no revert plans, so they cannot
    auto-revert. Routing rules require autonomy to have reversibility=auto
    AND claim present — neither holds for migrated items. They always go to
    human approval.
    """
    return RiskTag(
        blast_radius="bot",
        reversibility="manual",
        touches=["config"] if v1_type in _SYSADMIN_TYPES else ["unknown"],
    )


def is_v2(data: dict) -> bool:
    """Does this dict look like a v2 proposal already?"""
    return int(data.get("schema_version", 0)) >= PROPOSAL_SCHEMA_VERSION


def migrate_one(data: dict) -> dict:
    """Convert a v1 proposal dict to a v2 proposal dict.

    Returns the v2 dict ready for write. If the input already looks v2,
    returns it unchanged.
    """
    if is_v2(data):
        return data

    v1_type = str(data.get("type") or "investigation")
    dimension = "substrate_health" if v1_type in _SYSADMIN_TYPES else "utility"

    action = _build_action(v1_type, data)
    risk_tag = _build_risk_tag(v1_type)

    provenance = Provenance(
        technique="legacy_migration",
        signals={
            "original_type": v1_type,
            "original_pattern_key": data.get("pattern_key"),
            "original_confidence": data.get("confidence"),
        },
        confidence=float(data.get("confidence", 0.5) or 0.5),
    )

    proposal = Proposal(
        id=str(data.get("id") or _synthesize_id(data)),
        bot_id=str(data.get("bot_id") or data.get("bot") or "unknown"),
        generator_id="legacy_analyzer",
        dimension=dimension,
        trigger_observations=[],
        provenance=provenance,
        problem=str(
            data.get("problem")
            or data.get("pattern_key")
            or "Legacy proposal migrated without description."
        ),
        action=action,
        risk_tag=risk_tag,
        claim=None,
        revert_on_failure=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=_summary_from_v1(data),
        conversational_pitch=None,
        status="pending",
        created_at=str(
            data.get("created_at") or data.get("generated_at") or _now_iso()
        ),
        signature=str(data.get("evolve_sig") or data.get("signature") or ""),
    )
    return proposal.to_dict()


def _summary_from_v1(data: dict) -> str:
    """Best-effort one-liner from v1 fields."""
    candidate = (
        data.get("title")
        or data.get("pattern_key")
        or data.get("problem")
        or data.get("type")
        or "Legacy proposal"
    )
    summary = str(candidate).strip()
    if len(summary) > 120:
        summary = summary[:117] + "..."
    return summary


def _synthesize_id(data: dict) -> str:
    import uuid

    return str(uuid.uuid4())


def migrate_directory(
    proposals_root: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Walk ``proposals_root`` and migrate every v1 file to v2.

    Returns a report dict:
        {
            "scanned": int,
            "already_v2": int,
            "migrated": int,
            "errors": [(path, reason), ...],
            "changes": [(path, prev_type, new_action_kind), ...],
        }
    """
    scanned = 0
    already_v2 = 0
    migrated = 0
    errors: list[tuple[str, str]] = []
    changes: list[tuple[str, str, str]] = []

    if not proposals_root.exists():
        return {
            "scanned": 0,
            "already_v2": 0,
            "migrated": 0,
            "errors": [],
            "changes": [],
        }

    for path in sorted(proposals_root.rglob("*.json")):
        scanned += 1
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            errors.append((str(path), f"read/parse failed: {e}"))
            continue

        if is_v2(data):
            already_v2 += 1
            continue

        try:
            v2 = migrate_one(data)
        except Exception as e:  # noqa: BLE001 — migration errors surface per-file
            errors.append((str(path), f"migrate failed: {e}"))
            continue

        action_kind = v2.get("action", {}).get("kind", "?")
        changes.append((str(path), str(data.get("type", "?")), action_kind))
        migrated += 1

        if not dry_run:
            try:
                _atomic_write(path, v2, sort_keys=True)
            except OSError as e:
                errors.append((str(path), f"write failed: {e}"))

    return {
        "scanned": scanned,
        "already_v2": already_v2,
        "migrated": migrated,
        "errors": errors,
        "changes": changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Better Engine proposals from v1 to v2 schema."
    )
    parser.add_argument(
        "--proposals-root",
        type=Path,
        default=Path("/Users/Shared/evolve/proposals"),
        help="Root directory containing proposal JSON files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing any files.",
    )
    args = parser.parse_args(argv)

    report = migrate_directory(args.proposals_root, dry_run=args.dry_run)

    print(f"Scanned:    {report['scanned']}")
    print(f"Already v2: {report['already_v2']}")
    print(f"Migrated:   {report['migrated']}")
    if report["errors"]:
        print(f"Errors:     {len(report['errors'])}")
        for path, reason in report["errors"]:
            print(f"  {path}: {reason}")
    if args.dry_run:
        print("(dry run — no files written)")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
