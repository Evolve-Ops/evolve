#!/usr/bin/env python3
"""
manifest_reflex_runner.py — Manifest reflex queue processor.

Reads each bot's manifest-reflex-queue.jsonl and lands every pending row as
BOTH artifacts design §5 splits an app into:

  * the **Instance** — an ApplicationManifest, written by
    ``evolve_admin.applications.manifest.save_manifest`` into the bot's own
    workspace (``<bot home>/.openclaw/workspace/manifests/``; note this is
    what ``applications_dir`` actually resolves to, NOT the
    ``{shared_dir}/applications/{bot_id}/`` this docstring claimed for years);
  * the **Spec** — the portable intent, written by
    ``evolve_admin.applications.app_spec_store.write_spec`` into the pod-wide
    ``{shared_dir}/apps/specs/<app_id>.json`` (AL-1.5b).

Both, not either. Design §10's migration strategy is "migrate on read, write
v-next": every existing reader keeps finding its manifest exactly where it
always was, and the Spec is the new artifact alongside it. Dropping the
manifest write is AL-1.5c's decision to make, not this runner's.

Rotates applied/failed rows to manifest-reflex-archive.jsonl and rewrites
the queue.

Runs as the `evolve` user (launchd plist). Relies on the read+write ACL
grant on each bot's `~/.openclaw/workspace/evolve/` set by deploy.py's
set_evolve_read_acl().

Design notes:
  - Manifests are written atomically by evolve_admin.applications.manifest.
    save_manifest (temp-file + rename via Path.write_text → file index rebuild).
  - The Spec write DEPENDS on `record_application` being born DEFINED, which
    PR #3699 fixed just ahead of this one. Design §3 says a discovered draft
    carries a `draft_id` and no `app_id`, and a Spec is the portable intent of
    a *defined* app — so writing one for a manifest that still read
    `discovered` would have been writing portable identity for something the
    lifecycle says has none. #3699 is create-time only: pre-existing
    `bot_created` manifests on disk stay `discovered` (a bulk promote is an
    operator act, not a backfill), so this runner must not assume every
    existing bot-created manifest reads `defined`. It does not — the Spec is
    written from the artifact that just landed, and `_write_vnext_spec` gates
    on the resolved `app_id`, not on `definition_status`.
  - The Spec write is failure-SOFT and never fails the row: the manifest is
    the user-facing record and losing a row to a Spec-write error would be
    the worse outcome. It is not silent, though — the outcome is appended to
    the row's `result` string (so the archive carries it) and printed. A
    quiet degrade here would leave the census reporting a pod that never went
    v-next with no trace of why.
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


def _write_vnext_spec(manifest_path: Path, shared_dir: Path) -> str:
    """Write the v-next Spec for a just-saved manifest. Returns a status word.

    AL-1.5b (docs/build-AL-1.5-spec-vnext.md §3; design §5 + §7.3). The Spec is
    derived from the manifest AS IT LANDED ON DISK, not from the in-memory
    dataclass: ``save_manifest`` stamps ``app_id`` into the payload on its way
    out (``ensure_app_id``), so reading the file back is what makes the Spec's
    identity the same identity every other reader will resolve. It is also why
    there is no local ``spec_id or id`` chain here (identity: see
    resolve_app_id — that phrase is the AL-1.4b anti-pattern being named, not a
    read; this module resolves no legacy id field itself) —
    ``spec_from_manifest`` gets its ``app_id`` from
    ``app_identity.resolve_app_id`` and nothing else.

    Returns "ok", "skipped:<why>" or "FAILED:<err>" for the caller to fold into
    the row result. Never raises: see the module docstring on failure-softness.
    """
    if not manifest_path:
        # save_manifest returns the path it wrote; a caller (or a test) that
        # stubbed it out has no artifact to derive from. Named rather than
        # left to surface as an AttributeError on None.
        return "skipped:no manifest path"
    try:
        from evolve_admin.applications.app_spec_store import (  # pyright: ignore[reportMissingImports]
            spec_from_artifact, write_spec,
        )

        data = json.loads(manifest_path.read_text())
        # ``spec_from_artifact``, not the pure ``spec_from_manifest``: it
        # injects the disk-resolved files-pack shas, which is the field
        # design §6's deterministic install verifies against. It is also the
        # exact call ``migrate-specs`` makes, so the Spec this writes and the
        # Spec the census derives are the same object.
        spec = spec_from_artifact(data)
        if not spec.app_id:
            # Design §3: no identity, no portable intent. The only way to get
            # here from this runner is a manifest that resolved no conforming
            # app_id at all — the manifest still landed, which is the honest
            # outcome, and the census will report it as blocked.
            return "skipped:no app_id resolved"
        write_spec(spec, shared_dir)
        return "ok"
    except ValueError as e:
        # write_spec REFUSES rather than fails for an id it cannot confer
        # (design §3). That is the rule working, not the write breaking, and
        # collapsing the two into one word is how a healthy decline starts
        # reading like an outage in the archive.
        return f"skipped:{e}"
    except Exception as e:  # noqa: BLE001 — the manifest already landed
        print(f"[manifest_reflex_runner] spec write failed for "
              f"{manifest_path}: {e}", file=sys.stderr)
        return f"FAILED:{e}"


def _apply_row(row: ReflexRow, shared_dir: Path) -> tuple[bool, str]:
    """Land one row as a manifest + a v-next Spec. Returns (ok, info)."""
    from evolve_admin.applications.manifest import (
        ApplicationManifest,
        MANIFEST_SOURCE_BOT_CREATED,
        applications_dir,
        born_definition_status,
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
            saved_path = save_manifest(manifest, shared_dir)
        except Exception as e:
            return False, f"save_manifest (update) failed: {e}"
        # Refresh the Spec too — an update that added files or crons changed
        # the portable intent, and a stale Spec next to a fresh manifest is
        # the two-sources-of-truth problem the discriminator exists to avoid.
        spec_result = _write_vnext_spec(saved_path, shared_dir)
        return True, (f"updated {row.bot_id}/{row.app_id}: +{len(files_norm)} "
                      f"file(s), +{len(crons_norm)} cron(s); spec={spec_result}")

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
        # The bot vouched by calling record_application, so this app is born
        # DEFINED (design-app-spec-and-discovery-2026-08-15 §4/§7.3). Without
        # the explicit stamp it took the dataclass default `discovered`, which
        # is the inert scanner-draft value — and a draft is exactly what design
        # §3 says must never be conferred an identity. `born_definition_status`
        # is a create-time helper ("call it once, at the creation site, before
        # first write"); `save_manifest` stamps only `app_id`, never this.
        definition_status=born_definition_status(MANIFEST_SOURCE_BOT_CREATED),
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
        saved_path = save_manifest(manifest, shared_dir)
    except Exception as e:
        return False, f"save_manifest failed: {e}"

    # The Spec lands right after the manifest and before the Signal: an app is
    # defined the moment the bot vouches for it, so its portable intent should
    # not wait for a later sweep to exist.
    spec_result = _write_vnext_spec(saved_path, shared_dir)

    # Emit the learning Signal AFTER the manifest is durably saved — if save
    # failed, the user-facing record didn't happen and a Signal would be
    # premature. Failure-soft: signal emission errors don't fail the row.
    _emit_bot_created_signal(row, files_norm, crons_norm, shared_dir)

    return True, (f"created {row.bot_id}/{row.app_id} ({len(files_norm)} "
                  f"file(s), {len(crons_norm)} cron(s)); spec={spec_result}")


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
