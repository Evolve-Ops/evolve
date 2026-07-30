"""
extend_application — in-session primitive for adding a capability to a v7-arc App Instance.

Per docs/spec-manifest-v7-2026-05-20.md §8.3. The bot's LLM calls this during a
session when it wants to add a new file, cron, or event_trigger to an existing
app rather than hand-editing the Instance JSON.

Write-order contract (so failures roll back cleanly):
  1. Stage the new file in /tmp/evolve-extend-<uuid>/
  2. Stamp the marker on the staged file (provenance.embed_marker keyword='spec')
  3. Atomic rename staged → final workspace path
  4. Atomic write the updated Instance JSON (temp-file + rename)
  5. Append the change_log entry (folded into step 4 — the change_log lives
     inside the Instance JSON, so steps 4 and 5 are atomic together)

If any step fails, the staging dir remains (for debugging) and prior steps
roll back logically: step 1 fail → nothing on disk; step 2 fail → nothing
in workspace; step 3 fail → file in staging only; step 4 fail → file in
workspace but Instance JSON unchanged → Reflect will catch it as an orphan
on the next pass.

Usage:
    # As a Python API
    from evolve_admin.applications.extend_application import (
        extend_application, FileDescriptor
    )
    result = extend_application(
        instance_id="i-abcd1234",
        bot_id="team_bot_a",
        capability_summary="Add weekly summary script",
        file=FileDescriptor(
            path="scripts/summary.py",
            role="vital_to_blueprint",
            intent="Generate weekly journal summary",
            language="python",
            content="#!/usr/bin/env python3\\nprint('hi')\\n",
        ),
        user_intent_quote="I'd like a weekly recap",
    )

    # As a CLI (read JSON params from stdin)
    cat extend.json | python3 -m evolve_admin.applications.extend_application
"""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso

from ..config import bot_home
# can_app_own — the ONE shared "may an application own this path?" gate
# (app_ownership_policy.py). Top-level import is cycle-safe: app_ownership_policy
# imports scanner at module load, and scanner imports neither this module nor
# migrate_v7. We gate the realized_files[] append below so a capability-add
# can never bind a never-ownable path (secret, append-only log, manifest-store
# self-ref, telemetry index, OC-standard file) into an Instance's claims
# (F-B1 writer-hygiene; same predicate the read/classify side uses).
from .app_ownership_policy import can_app_own


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FileDescriptor:
    """A file the bot wants to add to its app."""
    path: str                                 # workspace-relative
    role: str                                 # vital_to_blueprint | instance_specific | reference_only
    intent: str                               # natural-language description
    language: str = ""
    content: str = ""                         # initial file content (may be empty)


@dataclass
class ExtendResult:
    """Outcome of an extend_application call."""
    success: bool
    file_id: Optional[str] = None
    file_path: Optional[Path] = None
    instance_path: Optional[Path] = None
    change_log_entry_id: Optional[str] = None
    error: str = ""
    # When True, the file is on disk but the Instance JSON wasn't updated —
    # Reflect's orphan detection will catch this on its next pass. The operator
    # should not panic: the file is stamped, just not yet bound to the Instance.
    file_orphaned: bool = False


_VALID_ROLES = ("vital_to_blueprint", "instance_specific", "reference_only")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_file_id() -> str:
    return f"f-{secrets.token_hex(4)}"


def _new_entry_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"log-{ts}-{secrets.token_hex(2)}"


# ── Core ──────────────────────────────────────────────────────────────────────

def extend_application(
    instance_id: str,
    bot_id: str,
    capability_summary: str,
    file: Optional[FileDescriptor] = None,
    user_intent_quote: Optional[str] = None,
    session_id: str = "",
    shared_dir: Path = Path("/Users/Shared/evolve"),
) -> ExtendResult:
    """
    Add a capability (currently: a new file) to an existing v7-arc Instance.

    Refuses if the Instance is not v7-arc shaped — bots running pre-migration
    must use the existing tool path. Returns ExtendResult with success / error
    / rollback state.
    """
    # ── Pre-flight: locate + validate the Instance ──
    instance_dir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    instance_path = instance_dir / f"{instance_id}.json"
    if not instance_path.is_file():
        return ExtendResult(success=False, error=f"instance not found: {instance_path}")

    try:
        instance = json.loads(instance_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return ExtendResult(success=False, error=f"failed to read instance: {e}")

    if instance.get("manifest_shape") != "v7-arc":
        return ExtendResult(
            success=False,
            error=(
                f"extend_application requires a v7-arc Instance; got "
                f"manifest_shape={instance.get('manifest_shape')!r}. "
                f"Run `migrate_v7 --apply` to migrate this bot's manifests."
            ),
        )

    provenance = instance.get("provenance") or {}
    spec_id = provenance.get("spec_id")
    spec_version = provenance.get("spec_version")
    if not spec_id or not spec_version:
        return ExtendResult(
            success=False,
            error=f"instance missing provenance.spec_id / spec_version",
        )

    if file is None:
        return ExtendResult(
            success=False,
            error="no file descriptor provided; nothing to do (cron/event_trigger paths not yet implemented)",
        )

    if file.role not in _VALID_ROLES:
        return ExtendResult(
            success=False,
            error=f"role must be one of {_VALID_ROLES}; got {file.role!r}",
        )

    if not capability_summary:
        return ExtendResult(success=False, error="capability_summary is required")

    # ── Ownership-policy gate (F-B1 writer-hygiene) ──
    # Refuse a never-ownable target up front — before staging, stamping, or
    # moving — so we never embed a marker into (or bind a realized_files[] claim
    # to) a secret, append-only log, manifest-store self-ref, telemetry index,
    # or OC-standard file. file.path is workspace-relative, the exact shape
    # can_app_own expects.
    if not can_app_own(file.path):
        return ExtendResult(
            success=False,
            error=(
                f"path {file.path!r} is not ownable by an application "
                f"(denied_by=ownership_policy); refusing to add it as a capability"
            ),
        )

    # ── Step 1: stage the file in /tmp ──
    staging_dir = Path(tempfile.mkdtemp(prefix="evolve-extend-"))
    staged_path = staging_dir / Path(file.path).name
    try:
        staged_path.write_text(file.content or "")
    except OSError as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return ExtendResult(success=False, error=f"staging write failed: {e}")

    # ── Step 2: stamp the marker on the staged file ──
    file_id_bare = _new_file_id()
    try:
        from .provenance import embed_marker

        embed_marker(
            staged_path,
            pkg_ids=[spec_id],
            file_id=file_id_bare,
            pkg_versions={spec_id: spec_version},
            file_version=spec_version,
            keyword="spec",
            merge=False,
        )
    except Exception as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return ExtendResult(success=False, error=f"marker stamp failed: {e}")

    # ── Step 3: atomic move to workspace ──
    workspace = bot_home(bot_id) / ".openclaw" / "workspace"
    final_path = workspace / file.path
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_path), str(final_path))
    except OSError as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return ExtendResult(success=False, error=f"move to workspace failed: {e}")
    finally:
        # Best-effort cleanup of the empty staging dir
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except OSError:
            pass

    file_ref = f"{file_id_bare}@{spec_version}"

    # ── Step 4 + 5: update Instance JSON + append change_log atomically ──
    realized = instance.setdefault("realized_files", [])
    realized.append({
        "logical_name": Path(file.path).stem,
        "path": str(final_path),
        "file_id": file_ref,
        "marker_state": "OWNED",
        "created_in_session": session_id,
    })

    entry_id = _new_entry_id()
    change_entry = {
        "entry_id": entry_id,
        "timestamp": _now_iso(),
        "kind": "capability_added",
        "who": "bot",
        "session_id": session_id,
        "description": capability_summary,
        "file_changes": [{
            "action": "created",
            "path": str(final_path),
            "file_id": file_ref,
        }],
    }
    if user_intent_quote:
        change_entry["user_intent_quote"] = user_intent_quote

    instance.setdefault("change_log", []).append(change_entry)

    try:
        # mode=0o644: the pre-consolidation helper wrote via Path.write_text
        # (umask-default perms); the Instance JSON in the bot's workspace must
        # stay readable by the bot user, not mkstemp's 0o600.
        _atomic_write_json(instance_path, instance, mode=0o644)
    except OSError as e:
        # File is on disk + stamped, but Instance JSON didn't update.
        # Reflect's orphan detection will catch and propose re-binding on next pass.
        return ExtendResult(
            success=False,
            file_id=file_ref,
            file_path=final_path,
            instance_path=instance_path,
            error=f"Instance JSON write failed: {e}",
            file_orphaned=True,
        )

    return ExtendResult(
        success=True,
        file_id=file_ref,
        file_path=final_path,
        instance_path=instance_path,
        change_log_entry_id=entry_id,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    """
    Read JSON params from stdin, invoke extend_application, write JSON result
    to stdout. Exit code 0 on success, 1 on failure.

    Expected stdin payload:
    {
      "bot_id": "team_bot_a",
      "instance_id": "i-abcd1234",
      "capability_summary": "Add weekly summary script",
      "user_intent_quote": "I'd like a weekly recap",
      "session_id": "sess-xyz",
      "file": {
        "path": "scripts/summary.py",
        "role": "vital_to_blueprint",
        "intent": "Generate weekly summary",
        "language": "python",
        "content": "..."
      }
    }
    """
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "error": f"invalid stdin JSON: {e}"}))
        return 1

    if not isinstance(payload, dict):
        print(json.dumps({"success": False, "error": "stdin must be a JSON object"}))
        return 1

    file_descriptor = None
    if isinstance(payload.get("file"), dict):
        f = payload["file"]
        file_descriptor = FileDescriptor(
            path=f.get("path", ""),
            role=f.get("role", ""),
            intent=f.get("intent", ""),
            language=f.get("language", ""),
            content=f.get("content", ""),
        )

    result = extend_application(
        instance_id=payload.get("instance_id", ""),
        bot_id=payload.get("bot_id", ""),
        capability_summary=payload.get("capability_summary", ""),
        file=file_descriptor,
        user_intent_quote=payload.get("user_intent_quote"),
        session_id=payload.get("session_id", ""),
        shared_dir=Path(payload.get("shared_dir", "/Users/Shared/evolve")),
    )

    # Serialize result with Path → str
    result_dict = asdict(result)
    for k, v in list(result_dict.items()):
        if isinstance(v, Path):
            result_dict[k] = str(v)
    print(json.dumps(result_dict, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
