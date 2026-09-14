"""routes_app_snapshot — the snapshot write for a defined app (AL-3.1).

  POST /api/apps/<app_id>/snapshot
      Body: { bot_id, dry_run?: true, auto_detect?: true, update_spec?: true }
      Engine: ``applications.app_snapshot.snapshot_app``

WHY A NEW MODULE RATHER THAN ``routes_apps``. That file's contract is stated
in its own docstring and in its brief: *"NO WRITES. Every action the surface
offers goes to the endpoint that already owns it. This chip adds no
privileged route and no new mutation path."* This IS a new mutation path, so
folding it in would quietly falsify a documented invariant that a reader is
entitled to rely on. It lives beside ``gallery_promote_routes`` instead,
which is the same shape — an HTTP shell over a snapshot engine — for the
pkg_id-keyed sibling.

WHAT IT CAN WRITE, AND WHERE. Two destinations, both under ``{shared_dir}``
and both owned by the ``evolve`` user this daemon already runs as:
``apps/packs/<app_id>/`` (the pack) and ``apps/specs/<app_id>.json`` (the
Spec's ``package{}``). It writes nothing into a bot's workspace, nothing
outside ``{shared_dir}``, and runs no subprocess: the reads are ACL reads and
the writes are plain file writes as ``evolve``. There is no ``sudo`` in this
path and no ``/tmp`` staging, because neither is needed for a file the
service user owns.

**``dry_run`` DEFAULTS TO TRUE.** A caller that forgets the flag gets the
report and no write. That is the opposite of the usual REST default and it is
deliberate: this endpoint's read half is the interesting one for a UI, the
write half is the rare deliberate act, and a mutation that happens because a
field was omitted is the wrong failure to be one keystroke away from.

CSRF and auth ride the app-wide ``before_request`` hooks (``web.csrf`` +
``admin_auth``); a mutating route needs no per-route work to get them, and
adding some here would be a second answer to a question the daemon already
answers once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..applications.app_snapshot import snapshot_app


def _err(error: str, status: int, **extra: Any) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": error, **extra}), status


def register_app_snapshot_routes(app: Flask, network_path: Path) -> None:
    """Mount ``POST /api/apps/<app_id>/snapshot`` on ``app``."""

    @app.post("/api/apps/<app_id>/snapshot")
    def api_app_snapshot(app_id: str) -> tuple[Response, int]:
        """Snapshot one defined app into a files-pack.

        Body:
          bot_id: str       — required; the bot whose workspace holds the
                              app's files.
          dry_run: bool     — DEFAULT TRUE. Report only; write nothing.
          auto_detect: bool — default true; reverse-substitute the known
                              per-bot tokens into placeholders.
          update_spec: bool — default true; also point the app's Spec
                              ``package.files[].sha256`` at the pack's
                              source digests. Ignored when ``dry_run``.

        Returns 200 with the engine envelope on success.

        Status mapping, and the distinction it preserves: a REFUSAL — a
        draft, an app that is still ``discovered``, an app this bot does not
        have — is **422**, not 500. It is a well-formed request about a thing
        that cannot be snapshotted, and rendering it as a server error is how
        a healthy decline starts reading like an outage.
        """
        from ..config import load_network

        body = request.get_json(silent=True) or {}
        bot_id = str(body.get("bot_id") or "").strip()
        if not bot_id:
            return _err("missing_fields", 400, missing=["bot_id"])

        try:
            network = load_network(network_path)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            return _err(f"network_load_failed: {exc}", 500)

        result = snapshot_app(
            bot_id, app_id,
            network=network,
            auto_detect=bool(body.get("auto_detect", True)),
            update_spec=bool(body.get("update_spec", True)),
            dry_run=bool(body.get("dry_run", True)),
        )
        if result.get("ok"):
            return jsonify(result), 200

        error = str(result.get("error") or "snapshot_failed")
        if result.get("refused"):
            status = 400 if error.startswith(
                ("missing_bot_id", "invalid_app_id")) else 422
        elif error.startswith("workspace_missing"):
            status = 404
        elif error.startswith(("read_denied", "workspace_unreadable")):
            # 403, not 500: the daemon is fine, the ACL is not. The operator
            # sentence is "re-grant the read ACL", which is a different
            # remediation from "the server broke".
            status = 403
        elif error.startswith("no_packable_files"):
            status = 409
        else:
            status = 500
        return jsonify(result), status
