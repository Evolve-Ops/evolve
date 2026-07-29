#!/usr/bin/env python3
"""
manifest_reflex_runner.py — Manifest reflex queue processor.

Reads each bot's manifest-reflex-queue.jsonl and lands every pending row
as an ApplicationManifest under {shared_dir}/applications/{bot_id}/.
Rotates applied/failed rows to manifest-reflex-archive.jsonl and rewrites
the queue.

Runs as the `evolve` user (launchd plist). Relies on the read+write ACL
grant on each bot's `~/.openclaw/workspace/evolve/` set by deploy.py's
set_evolve_read_acl().

Design notes:
  - Manifests are written atomically by evolve_admin.applications.manifest.
    save_manifest (temp-file + rename via Path.write_text → file index rebuild).
  - Rows that fail to apply (bad app_id, save error, etc.) move to the
    archive with status=failed so the queue advances; we don't get stuck
    on a poison row.
  - update=true rows merge files/crons/inputs/outputs into the existing
    manifest and append a row to improvement_history. If no manifest
    exists with that app_id, the update flag is ignored and a fresh one
    is created (the bot may have called update on a not-yet-landed row).

Usage:
    manifest_reflex_runner.py [--network NETWORK_JSON] [--dry-run] [--once]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manifest_reflex_queue import (
    ReflexRow,
    STATUS_APPLIED,
    STATUS_FAILED,
    append_archive,
    iter_bot_queues,
    rewrite_queue,
)
from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path


def _bot_ids(network_config: dict) -> list[str]:
    """Enumerate bot ids declared in network.json. Same shape-tolerance as
    defer_runner: dict-of-bot-id-to-config OR list-of-{id,...}."""
    bots = network_config.get("bots") or {}
    if isinstance(bots, dict):
        return sorted(bots.keys())
    if isinstance(bots, list):
        return sorted(b.get("id") or b.get("bot_id") for b in bots if isinstance(b, dict))
    return []


def _shared_dir(network_config: dict) -> Path:
    """Resolve {shared_dir}. network.json may pin it; otherwise canonical."""
    sd = network_config.get("sharedDir") or network_config.get("shared_dir")
    if sd:
        return Path(sd)
    return CANONICAL_SHARED_DIR


def _normalize_files(files: list[Any], workspace: Path | None) -> list[str]:
    """Normalize file paths to workspace-relative when possible.

    Manifests store workspace-relative paths; the bot may pass absolute
    paths, paths starting with 'workspace/', or already-relative paths.
    Anything we can't relativize cleanly stays as-is so the row is still
    informative — the scanner's stamping pass can fix it later."""
    out: list[str] = []
    for f in files or []:
        s = str(f).strip()
        if not s:
            continue
        p = Path(s)
        if workspace is not None and p.is_absolute():
            try:
                rel = p.relative_to(workspace)
                out.append(str(rel))
                continue
            except ValueError:
                pass
        # Strip a leading "workspace/" prefix — bots sometimes prefix paths
        # with it because that's what they see in their cwd.
        if s.startswith("workspace/"):
            out.append(s[len("workspace/"):])
            continue
        out.append(s)
    return out


def _normalize_crons(crons: list[dict]) -> list[dict]:
    """Convert tool-side cron entries to manifest v5 cron dicts."""
    out: list[dict] = []
    for c in crons or []:
        if not isinstance(c, dict):
            continue
        schedule = str(c.get("schedule", "")).strip()
        script = str(c.get("script", "")).strip()
        if not schedule or not script:
            continue
        out.append({"schedule": schedule, "script": script, "label": "", "file_id": ""})
    return out


def _resolve_workspace(bot_id: str) -> Path | None:
    """Resolve the bot's workspace path for path-relativization. Best-effort —
    if we can't find it, file paths stay as-passed."""
    try:
        from evolve_admin.config import get_bot_workspace
        return get_bot_workspace(bot_id)
    except Exception:
        return None


def _emit_bot_created_signal(
    row: ReflexRow,
    files_norm: list[str],
    crons_norm: list[dict],
    shared_dir: Path,
) -> None:
    """Emit a Signal when a fresh bot_created manifest lands.

    Spec: docs/spec-manifest-reflex.md Resolution §2. The Signal is the seam
    for a future "why didn't I anticipate this?" generator that reads recent
    transcripts and proposes proactive-behavior updates (SOUL.md, gallery
    suggestions). No reflection happens in the runner itself — it just
    records that a `bot_created` manifest exists, with enough context for a
    downstream generator to do the work.

    Updates (update=true with an existing manifest) don't fire — the
    "missed anticipation" moment was when the app first shipped.

    Failure-soft: if the Signal store is unavailable for any reason
    (import error, write failure, schema mismatch), we log and move on.
    A missed signal is a missed learning opportunity but the manifest
    still landed correctly, which is the user-facing requirement."""
    try:
        from signals.store import observe
    except Exception as e:
        print(f"[manifest_reflex_runner] signals.store unavailable: {e}", file=sys.stderr)
        return

    body_parts: list[str] = []
    if row.purpose:
        body_parts.append(row.purpose.strip())
    if files_norm:
        more = f" (+{len(files_norm) - 8} more)" if len(files_norm) > 8 else ""
        body_parts.append(f"Files: {', '.join(files_norm[:8])}{more}")
    if crons_norm:
        body_parts.append(f"Crons: {len(crons_norm)}")
    body = "\n".join(body_parts)[:1000]

    try:
        observe(
            shared_dir,
            signature=f"bot_created_app:{row.bot_id}:{row.app_id}",
            producer="manifest_reflex_runner",
            type="bot_created_app",
            flavor="activity",
            severity="info",
            scope="bot",
            bot_id=row.bot_id,
            title=f"{row.bot_id} built {row.name or row.app_id} mid-session",
            body=body,
            details={
                "app_id": row.app_id,
                "bot_id": row.bot_id,
                "session_id": row.session_id,
                "reflex_id": row.reflex_id,
                "files": files_norm[:32],
                "crons": crons_norm[:8],
                "purpose": row.purpose,
            },
        )
    except Exception as e:
        print(f"[manifest_reflex_runner] signal emit failed for {row.bot_id}/{row.app_id}: {e}", file=sys.stderr)


def _apply_row(row: ReflexRow, shared_dir: Path) -> tuple[bool, str]:
    """Land one row as a manifest. Returns (ok, info)."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest,
        MANIFEST_SOURCE_BOT_CREATED,
        applications_dir,
        save_manifest,
    )

    workspace = _resolve_workspace(row.bot_id)
    files_norm = _normalize_files(row.files, workspace)
    crons_norm = _normalize_crons(row.crons)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest_path = applications_dir(shared_dir, row.bot_id) / f"{row.app_id}.json"

    if row.update and manifest_path.exists():
        # Merge into the existing manifest. Read raw JSON so we don't lose
        # fields the dataclass doesn't know about (forward-compat).
        try:
            existing = json.loads(manifest_path.read_text())
        except Exception as e:
            return False, f"failed to read existing manifest for update: {e}"

        existing_files = existing.get("files") or []
        existing_paths = set()
        for entry in existing_files:
            if isinstance(entry, str):
                existing_paths.add(entry)
            elif isinstance(entry, dict):
                p = entry.get("path", "")
                if p:
                    existing_paths.add(p)
        for fp in files_norm:
            if fp not in existing_paths:
                existing_files.append(fp)
        existing["files"] = existing_files

        if crons_norm:
            existing_crons = existing.get("crons") or []
            existing_lines = {(c.get("schedule"), c.get("script")) for c in existing_crons if isinstance(c, dict)}
            for c in crons_norm:
                if (c["schedule"], c["script"]) not in existing_lines:
                    existing_crons.append(c)
            existing["crons"] = existing_crons

        for src_field, src_list in (("inputs", row.inputs), ("outputs", row.outputs)):
            if src_list:
                cur = list(existing.get(src_field) or [])
                for item in src_list:
                    s = str(item).strip()
                    if s and s not in cur:
                        cur.append(s)
                existing[src_field] = cur

        if row.purpose and not existing.get("purpose"):
            existing["purpose"] = row.purpose
        if row.test_command and not existing.get("test_command"):
            existing["test_command"] = row.test_command

        history = list(existing.get("improvement_history") or [])
        history.append({
            "at": now_iso,
            "kind": "manifest_reflex_update",
            "session_id": row.session_id,
            "reflex_id": row.reflex_id,
            "added_files": files_norm,
            "added_crons": crons_norm,
        })
        existing["improvement_history"] = history
        existing["updated_at"] = now_iso

        try:
            manifest = ApplicationManifest.from_dict(existing)
            save_manifest(manifest, shared_dir)
        except Exception as e:
            return False, f"save_manifest (update) failed: {e}"
        return True, f"updated {row.bot_id}/{row.app_id}: +{len(files_norm)} file(s), +{len(crons_norm)} cron(s)"

    # Create-fresh path. Either update=false, or update=true but no existing
    # manifest (bot called update on something that hadn't landed yet).
    source_detail_parts = [f"reflex:{row.reflex_id[:8]}"]
    if row.session_id:
        source_detail_parts.append(f"session:{row.session_id}")
    source_detail = " ".join(source_detail_parts)

    manifest = ApplicationManifest(
        id=row.app_id,
        name=row.name or row.app_id,
        bot_id=row.bot_id,
        purpose=row.purpose or "",
        status="active",
        source=MANIFEST_SOURCE_BOT_CREATED,
        source_detail=source_detail,
        confidence=1.0,
        files=files_norm,
        crons=crons_norm,
        inputs=list(row.inputs or []),
        outputs=list(row.outputs or []),
        test_command=row.test_command or "",
        owner=row.bot_id,
        created_at=now_iso,
        updated_at=now_iso,
    )
    try:
        save_manifest(manifest, shared_dir)
    except Exception as e:
        return False, f"save_manifest failed: {e}"

    # Emit the learning Signal AFTER the manifest is durably saved — if save
    # failed, the user-facing record didn't happen and a Signal would be
    # premature. Failure-soft: signal emission errors don't fail the row.
    _emit_bot_created_signal(row, files_norm, crons_norm, shared_dir)

    return True, f"created {row.bot_id}/{row.app_id} ({len(files_norm)} file(s), {len(crons_norm)} cron(s))"


def process_bot(
    bot_id: str,
    rows: list[ReflexRow],
    shared_dir: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Process one bot's pending rows. Returns summary counts."""
    summary = {"checked": len(rows), "applied": 0, "failed": 0, "kept": 0}
    now = datetime.now(timezone.utc)
    keep: list[ReflexRow] = []
    for row in rows:
        if row.status != "pending":
            # Stray non-pending row; archive it on next rewrite, don't re-process.
            keep.append(row)
            summary["kept"] += 1
            continue

        if dry_run:
            print(f"[manifest_reflex_runner] [dry-run] would apply {bot_id} {row.reflex_id[:8]} → {row.app_id} update={row.update}")
            keep.append(row)
            summary["kept"] += 1
            continue

        ok, info = _apply_row(row, shared_dir)
        row.applied_at = now.isoformat(timespec="seconds")
        row.result = info
        if ok:
            row.status = STATUS_APPLIED
            summary["applied"] += 1
            print(f"[manifest_reflex_runner] ✓ {bot_id} {row.reflex_id[:8]} {info}")
        else:
            row.status = STATUS_FAILED
            summary["failed"] += 1
            print(f"[manifest_reflex_runner] ✗ {bot_id} {row.reflex_id[:8]} {info}")

        append_archive(bot_id, row)

    if not dry_run and (summary["applied"] or summary["failed"]):
        rewrite_queue(bot_id, keep)
    return summary


def run_once(network_config: dict, *, dry_run: bool = False) -> dict:
    """One full cycle across all bots in the network."""
    bot_ids = _bot_ids(network_config)
    shared_dir = _shared_dir(network_config)
    totals = {"bots": 0, "checked": 0, "applied": 0, "failed": 0, "kept": 0}

    for bot_id, rows in iter_bot_queues(bot_ids):
        if not rows:
            continue
        s = process_bot(bot_id, rows, shared_dir, dry_run=dry_run)
        totals["bots"] += 1
        for k in ("checked", "applied", "failed", "kept"):
            totals[k] += s[k]

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Manifest reflex queue runner")
    parser.add_argument("--network", help="Path to network.json")
    parser.add_argument("--dry-run", action="store_true", help="Don't write manifests or rewrite files")
    parser.add_argument("--once", action="store_true", default=True,
                        help="Run one cycle and exit (default; loop is launchd's job)")
    args = parser.parse_args()

    network_path = Path(args.network) if args.network else resolve_network_path()
    try:
        network_config = json.loads(Path(network_path).read_text())
    except Exception as e:
        print(f"[manifest_reflex_runner] could not load network.json from {network_path}: {e}", file=sys.stderr)
        sys.exit(2)

    totals = run_once(network_config, dry_run=args.dry_run)
    print(f"[manifest_reflex_runner] cycle complete: {totals}")


if __name__ == "__main__":
    main()
