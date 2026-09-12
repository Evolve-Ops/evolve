"""
routes_applications_reliability — the ``Make reliable`` action for installed apps.

One mutating endpoint::

    POST /api/applications/<bot_id>/<app_id>/make-reliable

It re-forges a script-backed application's installed manifest from
``invocation_mode: "agent_invokes"`` to ``"plugin_intercept"`` so the OpenClaw
plugin runs the script deterministically instead of letting the LLM freelance
the call (and leak a raw failure or confabulate a fake success). The pure
synthesis + validation lives in
``applications.plugin_intercept_migration.migrate_to_plugin_intercept``; this
module adds the privileged, filesystem-aware parts:

  * read the bot's installed manifest (sanctioned read helper),
  * confirm every script the migrated contract points at actually exists on
    the bot's disk — refusing the change rather than switching to a mode that
    would fail *worse* at runtime than the one it replaces,
  * persist via the sanctioned per-bot manifest write (temp-file + rename, so
    the plugin's trigger cache invalidates on the manifests-dir mtime).

Authorization is enforced globally by the admin server's ``before_request``
device-auth + CSRF gates (``server._enforce_device_auth`` / ``_enforce_csrf``),
the same as every other mutating ``/api/applications`` route — there is no
per-route decorator to add.

The operation is **idempotent**: re-running on an app that is already in
reliable mode writes nothing and returns ``changed: false``. It is only ever
operator-initiated (this route) — never run automatically at scan or deploy.

Spec: internal/spec-agent-freelance-bypass-phase2-2026-06-06.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, Response, jsonify


def _confirm_missing_scripts(bot_id: str, manifest: dict) -> list[str]:
    """Return the relative paths of invocation scripts *confirmed* absent on disk.

    Resolves each ``event_triggers[].invocation.script`` against the bot's
    workspace and decides one of three states per script:

      * **present**  — the file exists (the common case: the ``evolve`` user
        has inherited ACL read on the bot's ``.openclaw/`` subtree, so a direct
        ``Path.exists()`` resolves);
      * **confirmed absent** — the file is missing *and* its parent directory
        is readable, so we know it's a real absence, not an access failure;
      * **unknown** — we can't read the parent (EACCES under a 0700 parent on a
        pre-ACL bot — the classic false-missing trap) or a path escapes the
        workspace; left out of the result.

    Only *confirmed-absent* scripts are returned. We refuse the migration on a
    definite absence (plugin_intercept would then fail every message), but a
    state we can't prove must NOT block a legitimate fix — false-refusal is the
    worse outcome here. No sudo: there is no ``/bin/cat`` grant for
    ``workspace/scripts/*.py``, so a root probe would always fail-closed and
    manufacture false absences.
    """
    # Lazy import: server.py imports this module's register fn, so importing
    # server at module load would be circular.
    from .server import _resolve_bot_user, resolve_bot_paths

    try:
        user = _resolve_bot_user(bot_id)
        workspace = Path(resolve_bot_paths(bot_id, user=user)["workspace"]).resolve()
    except Exception:
        # Can't resolve the workspace → can't prove anything missing.
        return []

    triggers = manifest.get("event_triggers") or []
    rels: list[str] = []
    for t in triggers:
        if not isinstance(t, dict):
            continue
        inv = t.get("invocation")
        if isinstance(inv, dict):
            script = inv.get("script")
            if isinstance(script, str) and script.strip() and script not in rels:
                rels.append(script.strip())

    missing: list[str] = []
    for rel in rels:
        try:
            abs_path = (workspace / rel).resolve()
        except OSError:
            continue  # unresolvable → unknown, don't block
        # Defensive: a script path that escapes the workspace is malformed.
        # Treat as confirmed-bad so the operator gets a clear refusal rather
        # than a silent pass on an out-of-tree path.
        if workspace != abs_path and workspace not in abs_path.parents:
            missing.append(rel)
            continue
        try:
            if abs_path.exists():
                continue  # present
            parent = abs_path.parent
            if parent.is_dir() and os.access(parent, os.R_OK):
                missing.append(rel)  # parent readable + file absent → confirmed
            # else: EACCES / unreadable parent → unknown, don't block
        except OSError:
            continue  # can't tell → unknown, don't block
    return missing


def register_applications_reliability_routes(
    app: Flask, network_path: Path, shared_dir: Path
) -> None:
    """Register the Make-reliable mutation route on ``app``."""

    @app.post("/api/applications/<bot_id>/<app_id>/make-reliable")
    def api_applications_make_reliable(bot_id: str, app_id: str) -> "Response | tuple[Response, int]":
        """Migrate an installed app's manifest to plugin_intercept mode.

        Returns 200 in every *handled* outcome so the UI can render the
        operator-facing ``reason`` uniformly:
          * {ok:true,  changed:true}  — migrated + persisted
          * {ok:true,  changed:false} — already reliable (no write)
          * {ok:false, changed:false} — can't be made reliable (reason set)
        404 when the manifest doesn't exist; 500 on an unexpected error or a
        write failure.
        """
        from .server import _read_manifest_as_bot, _write_manifest_as_bot
        from ..applications.plugin_intercept_migration import (
            migrate_to_plugin_intercept,
        )

        try:
            manifest = _read_manifest_as_bot(bot_id, app_id)
            if manifest is None:
                return jsonify({
                    "ok": False,
                    "error": "not_found",
                    "reason": f"No installed manifest found for {bot_id}/{app_id}.",
                }), 404

            result = migrate_to_plugin_intercept(manifest)

            # Refused / can't-migrate / pre-broken — a handled outcome.
            if not result["ok"]:
                return jsonify({
                    "ok": False,
                    "changed": False,
                    "reason": result["reason"],
                    "blocked_triggers": result.get("blocked_triggers", []),
                })

            # Idempotent: already reliable, nothing to write.
            if not result["changed"]:
                return jsonify({
                    "ok": True,
                    "changed": False,
                    "reason": result["reason"],
                    "invocation_mode": "plugin_intercept",
                })

            migrated = result["manifest"]

            # Privileged disk guard: plugin_intercept runs these scripts
            # itself, so a missing script fails every message instead of just
            # the occasional freelance. Refuse rather than ship that.
            missing = _confirm_missing_scripts(bot_id, migrated)
            if missing:
                return jsonify({
                    "ok": False,
                    "changed": False,
                    "reason": (
                        "Reliable mode runs the app's script directly, but "
                        + ("this file isn't" if len(missing) == 1 else "these files aren't")
                        + " on the bot's disk: " + ", ".join(missing)
                        + ". Re-install or re-forge the app to place its files, "
                        "then try again."
                    ),
                })

            # Stamp + persist via the sanctioned per-bot write path.
            import time as _t
            migrated["updated_at"] = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
            if not _write_manifest_as_bot(bot_id, app_id, migrated):
                return jsonify({
                    "ok": False,
                    "error": "write_failed",
                    "reason": "The updated manifest could not be written to the bot's workspace.",
                }), 500

            return jsonify({
                "ok": True,
                "changed": True,
                "reason": result["reason"],
                "invocation_mode": "plugin_intercept",
                "migrated_triggers": result.get("migrated_triggers", []),
            })
        except Exception as exc:  # noqa: BLE001 — surface as JSON, never 500-HTML
            return jsonify({"ok": False, "error": str(exc)}), 500
