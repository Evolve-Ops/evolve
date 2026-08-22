"""gallery_promote_routes.py — admin daemon endpoints for files-pack promotion.

Spec: docs/spec-files-pack-hybrid-2026-06-03.md §8.
Sister of: docs/runbook-snapshot-install-to-gallery-2026-06-03.md
Engine: ``applications.snapshot_engine.snapshot_installed_app``

Wraps the snapshot engine in an HTTP surface so the admin UI's
Apps-tab "Promote to gallery" button (F-P.7.b, follows) can drive
the snapshot workflow without operators needing to SSH into the
mini and run the CLI by hand.

Routes registered:

  POST /api/gallery/promote/snapshot
      Body: { bot_id, pkg_id, auto_detect?: true, force?: false }
      Returns: snapshot envelope with per-file placeholders and
               bare-token counts for operator review.

The endpoint writes the candidate files-pack to a staging directory
under ``/tmp/`` (NOT the gallery dir directly — operators commit via
their dev clone). Returns the staging path + metadata so the UI can
render a review modal. A future endpoint
(``POST /api/gallery/promote/finalize``) will package the staging
dir as a zip the operator downloads; for now operators ``scp`` from
the staging path.

Privilege model: this endpoint runs in-process on the admin daemon
(``evolve`` user). The snapshot engine reads via ACL — no sudo. The
daemon never writes to the gallery dir itself; commit-to-repo is
always operator-mediated.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). ``pkg_id`` is a REQUIRED request-body field
# naming the gallery package to snapshot, validated for presence and used as
# the temp-dir prefix. It selects which install to export, so the package key
# is the question — not which app the manifest happens to resolve to.

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from ..applications import snapshot_engine


def _err(error: str, status: int = 400, **extra: Any) -> tuple[Response, int]:
    """Failure envelope shaped like other admin daemon endpoints."""
    payload = {"ok": False, "error": error, **extra}
    return jsonify(payload), status


def _ok(payload: dict, status: int = 200) -> tuple[Response, int]:
    """Success envelope. ``payload`` carries the engine result fields."""
    return jsonify({"ok": True, "error": "", **payload}), status


def register_gallery_promote_routes(app: Flask) -> None:
    """Mount the gallery-promotion endpoints on ``app``.

    Called from web/server.py alongside the other ``register_*_routes``
    helpers. Side-effect free until a request lands.
    """

    @app.post("/api/gallery/promote/snapshot")
    def api_gallery_promote_snapshot() -> tuple[Response, int]:
        """Snapshot an installed app to a staging directory.

        Body:
          bot_id: str     — required, source bot whose install gets
                            captured (must be in network.json)
          pkg_id: str     — required, gallery package id (e.g. p-9bfa1c84)
          auto_detect: bool   — default true; placeholder substitution
                                (workspace path, bot_user path,
                                com.<bot_id>., bare \\b<bot_id>\\b)
          force: bool     — default false; clobber a non-empty
                            staging dir (rare; the staging dir is
                            fresh each call so this is mainly for
                            tests)
          out_dir: str    — optional explicit staging path. When
                            omitted the daemon allocates one under
                            tempfile.gettempdir().

        Returns (200 success):
          ok: true
          out_dir: str                 — absolute path on the daemon
                                         host where the files-pack
                                         landed
          files_count: int
          format_version: str
          snapshot_at: ISO-8601 UTC
          snapshot_source_pkg_version: str (echoed from installed manifest)
          sha256: str                  — top-level files-pack SHA
          per_file: [                  — for operator review
            { path, mode, sha256, size_bytes,
              placeholders, bare_token_count }, ...
          ]
          skipped: [                   — files that couldn't be read
            { path, reason }, ...
          ]

        Returns (400/404/409) on:
          - missing required fields
          - unknown bot_id (not in network.json)
          - missing workspace (bot not deployed)
          - no installed manifest with that pkg_id
          - non-empty staging dir without force=true (409)
        """
        body = request.get_json(silent=True) or {}

        missing = [f for f in ("bot_id", "pkg_id") if not body.get(f)]
        if missing:
            return _err(
                "missing_fields", status=400, missing=missing,
            )

        bot_id = str(body["bot_id"])
        pkg_id = str(body["pkg_id"])
        auto_detect = bool(body.get("auto_detect", True))
        force = bool(body.get("force", False))

        # Staging dir: explicit if given, else daemon-allocated. The
        # daemon path is operator-readable (the evolve user owns
        # /tmp/snapshot-* by virtue of being the creator).
        out_dir_raw = body.get("out_dir")
        if out_dir_raw:
            out_dir = Path(str(out_dir_raw))
        else:
            out_dir = Path(tempfile.mkdtemp(
                prefix=f"snapshot-{pkg_id}-", suffix="-files",
            ))

        result = snapshot_engine.snapshot_installed_app(
            bot_id=bot_id,
            pkg_id=pkg_id,
            out_dir=out_dir,
            auto_detect=auto_detect,
            force=force,
        )

        if not result.get("ok"):
            err = result.get("error", "snapshot_failed")
            # Pick an HTTP status that matches the error class.
            if err.startswith("missing_fields"):
                status = 400
            elif err.startswith("unknown_bot"):
                status = 404
            elif err.startswith(("workspace_missing", "manifest_not_found",
                                 "empty_manifest")):
                status = 404
            elif err.startswith("out_dir_not_empty"):
                status = 409
            else:
                status = 502
            return _err(err, status=status, out_dir=str(out_dir))

        # Success — strip engine-internal keys we don't expose:
        payload = {k: v for k, v in result.items() if k not in ("ok", "error")}
        return _ok(payload)
