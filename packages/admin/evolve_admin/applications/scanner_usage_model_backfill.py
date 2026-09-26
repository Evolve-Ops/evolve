"""One-time backfill: re-classify usage.model on every existing manifest.

Confirmed pod-wide 2026-06-07: 114 firing
``app_discoverability_no_cli`` + ``app_discoverability_no_example_triggers``
alerts, every one on a heartbeat- or cron-driven app whose
scanner output had ``usage.model`` unset (None / empty).

The scanner is fixed forward, but existing manifests already on disk
need a one-time re-classification so the alerts clear without waiting
for a full re-scan of every bot (which is expensive and would also
regenerate other fields the operator may have authored).

## Usage

    python3 -m evolve_admin.applications.scanner_usage_model_backfill \
        --shared-dir /Users/Shared/evolve

    # Dry-run (default): print what would change, don't write
    # Add --apply to actually write the manifests back.

Idempotent — re-running after apply is a no-op (already-set models
aren't overwritten).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .scanner import _infer_usage_model


def _iter_manifest_files(workspace: Path) -> list[Path]:
    """Find every manifest under a bot's workspace/manifests/ dir.

    Skips ``_history/`` archives and dotfiles (``.scan-status.json``).
    """
    mdir = workspace / "manifests"
    if not mdir.exists():
        return []
    out: list[Path] = []
    for f in sorted(mdir.iterdir()):
        if f.suffix != ".json":
            continue
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        out.append(f)
    return out


def _current_model(manifest: dict) -> str:
    """Read the manifest's current usage.model — tolerating the
    top-level + identity-nested placement the verifier accepts."""
    usage = manifest.get("usage")
    if isinstance(usage, dict):
        return (usage.get("model") or "").strip()
    identity = manifest.get("identity")
    if isinstance(identity, dict):
        nested = identity.get("usage")
        if isinstance(nested, dict):
            return (nested.get("model") or "").strip()
    return ""


def _set_top_level_model(manifest: dict, model: str) -> None:
    """Write usage.model at the top level. Preserves any existing
    usage block fields (trigger_recognition, etc.)."""
    usage = manifest.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    usage["model"] = model
    manifest["usage"] = usage


def _classify_one(path: Path) -> tuple[str, str] | None:
    """Return (current_model, inferred_model) for one manifest, or
    None when the file can't be parsed.

    The caller decides whether to write based on the comparison.
    """
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    current = _current_model(manifest)
    inferred = _infer_usage_model(
        scheduled_actions=manifest.get("scheduled_actions") or [],
        heartbeat_evidence=manifest.get("heartbeat_evidence") or {},
        crons=manifest.get("crons") or [],
        cron_evidence=manifest.get("cron_evidence") or {},
    )
    return (current, inferred)


def _apply_one(path: Path, inferred: str) -> bool:
    """Write the manifest back with usage.model set to inferred.
    Returns True on success."""
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    _set_top_level_model(manifest, inferred)
    try:
        tmp = path.with_suffix(f".tmp-{path.suffix.lstrip('.')}")
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(path)
        return True
    except OSError:
        return False


def backfill(workspace: Path, *, apply: bool = False,
              skip_if_set: bool = True) -> dict:
    """Re-classify usage.model on every manifest under workspace.

    Args:
        workspace: a bot's workspace dir
            (``/Users/<bot>/.openclaw/workspace``).
        apply: when False (default), report what would change without
            writing. When True, actually persist.
        skip_if_set: when True (default), don't overwrite manifests
            that already have a non-empty model — they're either
            operator-authored or already-backfilled.

    Returns:
        Summary dict with counts: total / changed / skipped / unparseable.
    """
    summary = {
        "total":        0,
        "changed":      0,
        "skipped_set":  0,    # already has a model; preserved
        "skipped_same": 0,    # current matches inferred
        "skipped_unset_user_initiated": 0,
        "unparseable":  0,
        "details":      [],   # list of (path, current, inferred, action)
    }
    for path in _iter_manifest_files(workspace):
        summary["total"] += 1
        result = _classify_one(path)
        if result is None:
            summary["unparseable"] += 1
            continue
        current, inferred = result
        if current and skip_if_set:
            summary["skipped_set"] += 1
            summary["details"].append(
                (str(path), current, inferred, "skipped_set"),
            )
            continue
        if current == inferred:
            summary["skipped_same"] += 1
            continue
        # Don't backfill user-initiated — that's the verifier's default
        # already, and writing it would noise up the trail. Only backfill
        # when we inferred scheduled or event-driven (i.e. fixed a real
        # gap).
        if inferred == "user-initiated":
            summary["skipped_unset_user_initiated"] += 1
            continue
        if apply:
            ok = _apply_one(path, inferred)
            action = "applied" if ok else "write_failed"
        else:
            action = "would_apply"
        summary["changed"] += 1
        summary["details"].append(
            (str(path), current, inferred, action),
        )
    return summary


def _find_bot_workspaces(shared_dir: Path) -> list[tuple[str, Path]]:
    """Discover bot workspaces from /Users/<bot>/.openclaw/workspace.

    Returns ``[(bot_id, workspace_path), ...]``. Reads from the network
    config when available for the canonical bot list; falls back to
    scanning /Users for any directory with a workspace subdir.
    """
    out: list[tuple[str, Path]] = []
    users_root = Path("/Users")
    if not users_root.exists():
        return out
    for d in sorted(users_root.iterdir()):
        if not d.is_dir():
            continue
        workspace = d / ".openclaw" / "workspace"
        if (workspace / "manifests").exists():
            out.append((d.name, workspace))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill usage.model on existing manifests. Inferred from "
            "scheduled_actions + heartbeat_evidence; closes the "
            "verifier-false-positive gap for scanner-discovered apps."
        ),
    )
    parser.add_argument(
        "--shared-dir", required=True,
        help="Pod shared dir (only used as a sanity check that we're "
              "on the right machine; the backfill walks /Users/<bot>)",
    )
    parser.add_argument(
        "--bot", action="append", default=[],
        help="Limit to specific bot(s). Repeatable. Default: every "
              "bot with a workspace.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the changes. Default is dry-run.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Backfill even manifests that already have a model set.",
    )
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir)
    if not shared_dir.exists():
        print(f"ERROR: shared-dir not found: {shared_dir}", file=sys.stderr)
        return 2

    bots = _find_bot_workspaces(shared_dir)
    if args.bot:
        wanted = set(args.bot)
        bots = [(b, w) for b, w in bots if b in wanted]
    if not bots:
        print("No bot workspaces found.", file=sys.stderr)
        return 1

    print(f"{'DRY-RUN' if not args.apply else 'APPLYING'}: "
          f"{len(bots)} bot(s)")

    totals = {"total": 0, "changed": 0, "skipped_set": 0,
              "skipped_same": 0, "skipped_unset_user_initiated": 0,
              "unparseable": 0}
    for bot_id, workspace in bots:
        summary = backfill(
            workspace, apply=args.apply,
            skip_if_set=not args.force,
        )
        for k in totals:
            totals[k] += summary[k]
        print(f"\n=== {bot_id} ===")
        print(f"  total={summary['total']} changed={summary['changed']} "
              f"already_set={summary['skipped_set']} "
              f"unchanged={summary['skipped_same']} "
              f"unparseable={summary['unparseable']}")
        for path, current, inferred, action in summary["details"][:10]:
            print(f"  {action:14s} "
                  f"current={current or '<unset>':14s} → {inferred:14s} "
                  f"{Path(path).name}")
        if len(summary["details"]) > 10:
            print(f"  ... and {len(summary['details']) - 10} more")

    print(f"\nTOTAL: {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
