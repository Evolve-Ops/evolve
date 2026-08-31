"""routes_app_install — the deterministic install + update writes (AL-3.2).

  POST /api/apps/<app_id>/install
      Body: { bot_id, dry_run?: true, source_bot?, snapshot_if_needed?: true }
      Engine: ``applications.app_install.install_app_to_bot``

  POST /api/apps/<app_id>/update
      Body: { bot_id, dry_run?: true, confirm_overwrite?: false }
      Engine: ``applications.app_install.update_app_on_bot``

WHY A NEW MODULE, AGAIN. ``routes_apps`` states its contract in its own
docstring — *"NO WRITES. Every action the surface offers goes to the endpoint
that already owns it"* — and folding a mutation in would falsify an invariant a
reader is entitled to rely on. This is the sibling of ``routes_app_snapshot``,
which split out for the same reason and for the same surface; the two are kept
apart from each other because they are different acts on different trees (a
snapshot writes only under ``{shared_dir}``; an install writes into a BOT).

**``dry_run`` DEFAULTS TO TRUE on both routes**, matching the snapshot route
and for the stated reason: the read half is what a UI wants, the write half is
the rare deliberate act, and a mutation that happens because a field was
omitted is the wrong failure to be one keystroke away from. The Apps page uses
it as a preview-then-apply pair — the operator sees the file list, the
predicted digests and (for an update) exactly what would be overwritten, before
anything is written.

WHAT THESE CAN WRITE, AND WHERE. Into ONE bot's workspace: the files the app's
verified pack declares, at the paths it declares, plus the instance manifest at
``manifests/<app_id>.json``. Files land through
``marker_embed_helper --install`` when the daemon's own ACL cannot reach them —
a fixed-path root helper that re-validates containment, the expected bot, and
``can_app_own`` itself, and that CANNOT overwrite without being handed the
destination's current digest. The manifest lands through
``manifest._write_manifest_bytes``, under the grant that names exactly that
path. Nothing else is reachable from here.

CSRF and auth ride the app-wide ``before_request`` hooks (``web.csrf`` +
``admin_auth``), the same as every other mutating route on this daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from ..applications.app_install import install_app_to_bot, update_app_on_bot


def _err(error: str, status: int, **extra: Any) -> "tuple[Response, int]":
    return jsonify({"ok": False, "error": error, **extra}), status


#: Error-class prefix -> HTTP status. A REFUSAL — an app already installed, an
#: app with no pack and nothing to snapshot from, an update that would flatten
#: local work — is 4xx and never 500: it is a well-formed request about a state
#: that cannot be acted on, and rendering it as a server error is how a healthy
#: decline starts reading like an outage (AL-1.5b's review).
_STATUS_BY_PREFIX: "tuple[tuple[tuple[str, ...], int], ...]" = (
    (("missing_bot_id", "invalid_app_id"), 400),
    (("workspace_missing",), 404),
    (("workspace_unreadable", "read_denied"), 403),
    (("already_installed", "not_installed", "target_collision",
      "would_overwrite_local_changes", "ambiguous_source"), 409),
    (("no_pack", "no_source", "no_spec", "empty_pack", "source_not_eligible",
      "pkg_id_unresolved", "nothing_to_pack", "not_a_defined_app",
      "draft_refused"), 422),
)


def _status_for(result: dict) -> int:
    error = str(result.get("error") or "")
    for prefixes, status in _STATUS_BY_PREFIX:
        if error.startswith(prefixes):
            return status
    # An unclassified refusal is still a refusal — 422, not 500. Only a
    # non-refusal with no known prefix is a server error.
    return 422 if result.get("refused") else 500


def register_app_install_routes(app: Flask, network_path: Path) -> None:
    """Mount the install + update writes on ``app``."""

    def _run(app_id: str, engine: Callable[..., dict], **kwargs: Any) -> "tuple[Response, int]":
        """Shared shell: load the network, run the engine, map the envelope.

        One shell for both routes so the status mapping cannot drift between
        them — the two engines share an envelope shape precisely so the HTTP
        layer does not need two answers to the same question.
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

        result = engine(
            app_id, bot_id, network=network,
            dry_run=bool(body.get("dry_run", True)), **kwargs,
        )
        if result.get("ok"):
            return jsonify(result), 200
        return jsonify(result), _status_for(result)

    @app.post("/api/apps/<app_id>/install")
    def api_app_install(app_id: str) -> "tuple[Response, int]":
        """Install one app onto a bot that does not have it.

        Body:
          bot_id: str             — required; the TARGET bot.
          dry_run: bool           — DEFAULT TRUE. Plan + predict; write nothing.
          source_bot: str         — which bot to snapshot a pack from when the
                                    app has none yet. Required only when more
                                    than one bot has it defined with files.
          snapshot_if_needed: bool — default true.
        """
        body = request.get_json(silent=True) or {}
        return _run(
            app_id, install_app_to_bot,
            source_bot=str(body.get("source_bot") or "").strip(),
            snapshot_if_needed=bool(body.get("snapshot_if_needed", True)),
        )

    @app.post("/api/apps/<app_id>/update")
    def api_app_update(app_id: str) -> "tuple[Response, int]":
        """Move a bot's copy of an app to the Spec's current version.

        A MERGE (D-L3): files proven untouched since install are replaced,
        files that carry local changes are reported and left alone unless
        ``confirm_overwrite`` is passed — and even then the replacement is
        checked against the digest the preview measured, so a stale
        confirmation refuses rather than flattening.

        Body:
          bot_id: str            — required; the bot to move.
          dry_run: bool          — DEFAULT TRUE.
          confirm_overwrite: bool — default false; replace locally-changed files.
        """
        body = request.get_json(silent=True) or {}
        return _run(
            app_id, update_app_on_bot,
            confirm_overwrite=bool(body.get("confirm_overwrite", False)),
        )
