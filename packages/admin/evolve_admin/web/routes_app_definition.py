"""
routes_app_definition — operator promote/demote of an app's *source-of-truth*
status (the Defined/Discovered manifest lifecycle, Bite 1).

Four mutating endpoints::

    POST /api/applications/<bot_id>/<app_id>/definition/promote
    POST /api/applications/<bot_id>/<app_id>/definition/demote
    POST /api/applications/<bot_id>/<app_id>/definition/drift/review
    POST /api/applications/<bot_id>/<app_id>/definition/promotion-shield

Promote flips ``definition_status`` to ``defined`` — the operator vouches the
manifest as the authoritative contract: it is then existence-shielded from L3
archival and (on legacy shape) its anchored-identity fields are marked authored.
Demote reverses it. The pure manifest mutations live in
``applications.coherence_actions.{promote_to_defined,demote_to_discovered}``;
this module is the thin privileged web wrapper (read → mutate → write).

Distinct from the two pre-existing "Promote" affordances (spec
internal/spec-apps-meta-2026-06-13.md §9.6 bite 1): the gallery files-pack
``↥ Promote`` (export) and the coherence "Promote to authored" field-provenance
flip (``POST …/promote``). These definition routes own a different verb on a
different axis; they are namespaced under ``/definition/`` so they never collide.

**Authorization** is enforced globally by the admin server's ``before_request``
device-auth + CSRF gates (``server._enforce_device_auth`` / ``_enforce_csrf``),
the same as every other mutating ``/api/applications`` route — there is no
per-route decorator to add. Per spec §9.5 the pod-admin is the only promoter for
now; the end-user-via-evo path routes out to a future bite. The ``by`` stamp
records the actor on the provenance trail.

The write is atomic — ``_write_manifest_as_bot`` uses same-dir temp + ``os.replace``
onto the evolve-writable ``workspace/manifests`` path (no sudo), which also bumps
the manifests-dir mtime so a running gateway's trigger cache invalidates.

All operations are **idempotent** and **reversible**; only ever
operator-initiated (these routes) — never run automatically at scan or deploy.

**Identifier resolution (spec §9.7 Bite-1 finding #5).** The Apps grid hands
the UI a manifest's internal ``id`` field, which can diverge from the on-disk
filename stem (gallery-installed v7-arc-pre manifests). Read AND write must hit
the SAME file, so every route resolves the real stem via
``_resolve_manifest_stem`` before touching disk — passing the internal id still
lands on the right file, though the Bite-4 UI passes the stem directly.

Spec: internal/spec-apps-meta-2026-06-13.md §9 (impl notes §9.7, §9.8).
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). The ``id`` / ``instance_id`` comparison below is
# a permissive MATCHER, not a resolution — the same shape as
# ``server._find_manifest_by_id_field``. The caller holds an id of unknown
# shape (the filename stem and the internal id diverge on gallery-installed
# v7-arc-pre manifests), so narrowing to the one field the resolver picks would
# 404 the very manifests this fallback exists to find.

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request


def _resolve_manifest_stem(bot_id: str, app_id: str) -> str:
    """Resolve the on-disk filename STEM for ``app_id``.

    ``app_id`` may be the filename stem itself (the common case) OR the
    manifest's internal ``id`` / ``instance_id`` field — on gallery-installed
    v7-arc-pre manifests the two diverge (file ``atlas-article-capture.json``,
    id ``app_atlas_article_capture``). A mutation endpoint MUST write back to
    the SAME file it read (spec §9.7 Bite-1 finding #5), so the write path
    needs the real stem, not whatever identifier the UI happened to hand back.

    All error handling is delegated to the (already-hardened) server helpers
    ``_read_manifest_as_bot`` / ``_list_manifests_as_bot`` — this module adds no
    silent swallow of its own. Returns ``app_id`` unchanged when a
    ``{app_id}.json`` file exists (fast path) or when no id-field match is found
    (the caller's read then 404s).
    """
    from .server import _read_manifest_as_bot, _list_manifests_as_bot
    # Fast path: app_id IS the filename stem.
    if _read_manifest_as_bot(bot_id, app_id) is not None:
        return app_id
    # Fall back: find the file whose internal id / instance_id matches.
    for mf_path in _list_manifests_as_bot(bot_id):
        stem = Path(mf_path).stem
        data = _read_manifest_as_bot(bot_id, stem)
        if isinstance(data, dict) and (
            # identity: see resolve_app_id — a permissive MATCHER, not a resolution (see the module note above).
            data.get("id") == app_id or data.get("instance_id") == app_id
        ):
            return stem
    return app_id


def register_app_definition_routes(app: Flask) -> None:
    """Register the definition promote/demote/drift-review/shield routes."""

    @app.post("/api/applications/<bot_id>/<app_id>/definition/promote")
    def promote_application_definition(
        bot_id: str, app_id: str
    ) -> "Response | tuple[Response, int]":
        """Promote a manifest to definition_status="defined" (operator vouch).

        Reversible via the /definition/demote sibling; idempotent.
        """
        from .server import _read_manifest_as_bot, _write_manifest_as_bot
        from ..applications.coherence_actions import promote_to_defined
        stem = _resolve_manifest_stem(bot_id, app_id)
        manifest = _read_manifest_as_bot(bot_id, stem)
        if manifest is None:
            return jsonify({"ok": False, "error": "manifest not found"}), 404
        result = promote_to_defined(manifest, by="ui:operator")
        if not _write_manifest_as_bot(bot_id, stem, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/definition/demote")
    def demote_application_definition(
        bot_id: str, app_id: str
    ) -> "Response | tuple[Response, int]":
        """Demote a manifest back to definition_status="discovered" — reverses
        a promotion. Idempotent."""
        from .server import _read_manifest_as_bot, _write_manifest_as_bot
        from ..applications.coherence_actions import demote_to_discovered
        stem = _resolve_manifest_stem(bot_id, app_id)
        manifest = _read_manifest_as_bot(bot_id, stem)
        if manifest is None:
            return jsonify({"ok": False, "error": "manifest not found"}), 404
        result = demote_to_discovered(manifest, by="ui:operator")
        if not _write_manifest_as_bot(bot_id, stem, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({"ok": True, **result})

    @app.post("/api/applications/<bot_id>/<app_id>/definition/promotion-shield")
    def set_application_promotion_shield(
        bot_id: str, app_id: str
    ) -> "Response | tuple[Response, int]":
        """Set or clear design §7.2's "never ask me about this again" shield.

        Body: ``{"shielded": false}`` to clear, ``{"shielded": true}`` to set.

        **Why this exists** (AL-1.7 brief §8.3 step 6). The shield is written by
        a classifier over free English on the bot's promotion hop, so an
        acceptance phrased with a never-clause can set it by mistake. Until this
        route existed nothing anywhere unset it: the sibling ``/definition/
        promote`` ignores the shield, so an operator could still promote the
        draft outright, but there was no way to put it back in the queue to be
        *asked* about — the user's own answer was the one thing that could not
        be revised.

        Deliberately NOT a promotion. Clearing the shield restores eligibility
        to be OFFERED; whether the draft is then offered still runs the whole
        gate (readiness, cadence cap, snooze), and whether it is promoted is
        still someone's answer to a question. An operator wanting to skip all
        of that already has ``/definition/promote``.

        Idempotent in both directions; reversible by calling it with the other
        value.
        """
        from .server import _read_manifest_as_bot, _write_manifest_as_bot
        from ..applications.app_promotion import (
            DO_NOT_OFFER_FIELD,
            set_promotion_shield,
        )
        payload = request.get_json(silent=True) or {}
        shielded = payload.get("shielded")
        if not isinstance(shielded, bool):
            # No default. "shielded" is the whole request, and guessing it would
            # mean a malformed body silently picking one of two opposite
            # outcomes — one of which is irreversible-by-the-user, which is the
            # defect this route exists to undo.
            return jsonify({
                "ok": False,
                "error": 'body must carry a boolean "shielded"',
            }), 400
        stem = _resolve_manifest_stem(bot_id, app_id)
        manifest = _read_manifest_as_bot(bot_id, stem)
        if manifest is None:
            return jsonify({"ok": False, "error": "manifest not found"}), 404
        was = bool(manifest.get(DO_NOT_OFFER_FIELD))
        set_promotion_shield(manifest, shielded=shielded, by="ui:operator")
        if not _write_manifest_as_bot(bot_id, stem, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({
            "ok": True,
            "app_id": stem,
            "shielded": shielded,
            "changed": was != shielded,
            "by": "ui:operator",
        })

    @app.post("/api/applications/<bot_id>/<app_id>/definition/drift/review")
    def review_application_drift(
        bot_id: str, app_id: str
    ) -> "Response | tuple[Response, int]":
        """Mark scanner-drift entries reviewed on a ``defined`` app's manifest
        (clears the Bite-4 unreviewed-changes badge).

        Body (all optional)::

            {"targets": ["<ts>", ...],   # which entries; omit/None = all
             "reviewed": true}            # false to un-review (reversible)

        Additive + reversible: flips the ``reviewed`` flag only — never deletes
        an entry, so a genuine recurrence can still re-narrate. Idempotent (a
        no-op flip skips the write). The mutation itself lives in
        ``drift_classifier.mark_drift_reviewed`` (the route is the thin
        privileged wrapper, mirroring promote/demote).
        """
        from .server import _read_manifest_as_bot, _write_manifest_as_bot
        from ..applications.drift_classifier import (
            mark_drift_reviewed, count_unreviewed_drift,
        )
        body = request.get_json(silent=True) or {}
        reviewed = bool(body.get("reviewed", True))
        targets = body.get("targets")
        if targets is not None and not isinstance(targets, list):
            return (
                jsonify({"ok": False,
                         "error": "targets must be a list of ts strings"}),
                400,
            )
        stem = _resolve_manifest_stem(bot_id, app_id)
        manifest = _read_manifest_as_bot(bot_id, stem)
        if manifest is None:
            return jsonify({"ok": False, "error": "manifest not found"}), 404
        changed = mark_drift_reviewed(manifest, reviewed=reviewed, targets=targets)
        if changed and not _write_manifest_as_bot(bot_id, stem, manifest):
            return jsonify({"ok": False, "error": "write failed"}), 500
        return jsonify({
            "ok": True,
            "updated": changed,
            "unreviewed_count": count_unreviewed_drift(manifest),
        })
