"""Customizations admin routes — per-bot overrides review surface.

Phase 4 of ``docs/spec-openclaw-json-derived-artifact-2026-05-24.md``.

Operator-facing UI for the Phase 2 overrides file. After Phase 3 landed
the materializer + Migration A, every per-bot deviation flows into
``{shared_dir}/sandbox/overrides/<bot_id>.json`` as an
``OverrideEntry``. Some carry ``needs_review=True`` (auto-promoted
drift from the materializer, RSI proposals, Migration A bulk imports);
the operator's job is to walk those entries and accept, annotate, or
revert each one.

Until Phase 4, the only way to do that was to ``cat`` the JSON file
and edit it by hand. This module ships the four endpoints the
Customizations UI calls:

- ``GET  /api/customizations/<bot_id>`` — list overrides for a bot,
  enriched with schema metadata (description, stock_default, strength).
- ``POST /api/customizations/<bot_id>/accept`` — clear ``needs_review``
  for one key. Body: ``{"key": "<schema.path>"}``.
- ``POST /api/customizations/<bot_id>/annotate`` — set ``note`` and/or
  ``expires_at`` for one key. Body: ``{"key": "...", "note": ..., "expires_at": ...}``.
- ``POST /api/customizations/<bot_id>/revert`` — delete one override.
  Body: ``{"key": "<schema.path>"}``.
- ``POST /api/customizations/<bot_id>/set`` — create or replace an
  override directly from the admin UI (operator-authored). Body:
  ``{"key": "<schema.path>", "value": <typed value>, "note": "..."}``.

All four are wired into ``server.create_app`` via ``register_customizations_routes``.

Per ``feedback_ui_authorization_presumed`` — no per-role gating. Bind is
localhost-only; the admin URL is presumed operator-controlled.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..config import load_network
from ..config_sandbox import (
    OverrideStateError,
    OverrideValidationError,
    by_path,
    delete_override,
    mark_reviewed,
    read_bot_overrides,
    write_override,
)
from ..config_sandbox.overrides import OverrideEntry


_MAX_KEY_LEN = 256


def _shared_dir(network_path: Path) -> Path:
    return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))


def _bot_exists(bot_id: str, network_path: Path) -> bool:
    bots = (load_network(network_path).get("bots") or {})
    return bot_id in bots


def _validated_key(body: dict[str, Any]):
    """Pull ``key`` from a request body. Returns the string on success or
    a flask response tuple ``(Response, 400)`` on validation failure.

    Caller pattern (the str check distinguishes the str-success from the
    tuple-failure return):

        key = _validated_key(body)
        if not isinstance(key, str):
            return key
        # ... use key as str
    """
    key = body.get("key")
    if not isinstance(key, str):
        return jsonify({"ok": False, "error": "missing 'key' in body"}), 400
    stripped = key.strip()
    if not stripped:
        return jsonify({"ok": False, "error": "'key' is empty or whitespace"}), 400
    if len(key) > _MAX_KEY_LEN:
        return jsonify({
            "ok": False,
            "error": f"'key' too long (max {_MAX_KEY_LEN} chars)",
        }), 400
    return key


def _entry_to_dict(key: str, entry: OverrideEntry) -> dict[str, Any]:
    """Project an override entry + its schema metadata for UI rendering.

    The schema entry contributes the description, stock_default, and
    strength label so the UI can render context-rich rows without
    needing a second API call. If the override key isn't in the
    current schema (a forward-compat case — overrides file written by a
    newer admin), we still return the entry but with placeholder schema
    metadata so the UI can show it as "unknown" rather than crash.
    """
    payload = asdict(entry)
    payload["key"] = key
    schema_entry = by_path(key)
    if schema_entry is not None:
        payload["schema"] = {
            "description": schema_entry.description,
            "stock_default": schema_entry.stock_default,
            "strength": schema_entry.strength.label,
            "type_hint": schema_entry.type_hint,
            "target_path": schema_entry.target_path,
            "store": schema_entry.store,
        }
        payload["matches_default"] = (entry.value == schema_entry.stock_default)
    else:
        payload["schema"] = None
        payload["matches_default"] = False
    return payload


def register_customizations_routes(app: Flask, network_path: Path) -> None:
    """Register /api/customizations/* endpoints."""

    @app.get("/api/customizations/<bot_id>")
    def api_customizations_list(bot_id: str) -> Response:
        """Return all overrides for the bot, enriched with schema metadata.

        Sorted: ``needs_review`` entries first (operator's queue),
        otherwise alphabetical by key. Empty list when the bot has no
        overrides file (legitimate state, not an error).
        """
        if not _bot_exists(bot_id, network_path):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        try:
            bo = read_bot_overrides(_shared_dir(network_path), bot_id)
        except OverrideValidationError as e:
            # Invalid bot_id shape — caller used a name with characters
            # the overrides file system rejects.
            return jsonify({"ok": False, "error": str(e)}), 400

        entries = [_entry_to_dict(k, v) for k, v in bo.overrides.items()]
        entries.sort(key=lambda e: (not e["needs_review"], e["key"]))
        needs_review_count = sum(1 for e in entries if e["needs_review"])

        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "schema_version": bo.schema_version,
            "entries": entries,
            "needs_review_count": needs_review_count,
        })

    @app.post("/api/customizations/<bot_id>/accept")
    def api_customizations_accept(bot_id: str) -> Response:
        """Clear ``needs_review`` on an override; re-attribute set_by="operator".

        Body: ``{"key": "<schema.path>", "note": "<optional new note>"}``.
        The override's value is unchanged; only the metadata flags are
        updated. Returns the updated entry.

        Idempotent: re-accepting an entry that's already ``needs_review=False``
        AND ``set_by="operator"`` is a no-op — we don't rewrite the
        ``set_at`` timestamp on a double-click, which would lose the
        original acceptance time and degrade audit fidelity.
        """
        if not _bot_exists(bot_id, network_path):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        body = (request.get_json(silent=True) or {})
        key = _validated_key(body)
        if not isinstance(key, str):
            return key   # validation error response
        note = body.get("note")  # None preserves existing note
        shared = _shared_dir(network_path)

        # Idempotency guard: if the override is already in the
        # operator-accepted terminal state and the caller isn't trying
        # to change the note, skip the write entirely.
        existing_bo = read_bot_overrides(shared, bot_id)
        existing = existing_bo.get(key)
        if (
            existing is not None
            and not existing.needs_review
            and existing.set_by == "operator"
            and (note is None or note == existing.note)
        ):
            return jsonify({"ok": True, "entry": _entry_to_dict(key, existing), "noop": True})

        try:
            updated = mark_reviewed(
                shared, bot_id, key,
                note=note,
            )
        except OverrideStateError as e:
            return jsonify({"ok": False, "error": f"overrides file unreadable: {e}"}), 500
        except OverrideValidationError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if updated is None:
            return jsonify({
                "ok": False,
                "error": f"no override exists for {key}",
            }), 404
        return jsonify({"ok": True, "entry": _entry_to_dict(key, updated)})

    @app.post("/api/customizations/<bot_id>/annotate")
    def api_customizations_annotate(bot_id: str) -> Response:
        """Set the note and/or expires_at for an override without changing value.

        Body: ``{"key": "...", "note": "...", "expires_at": "<iso>"|null}``.

        We re-write the override via ``write_override`` with the existing
        value, preserving the user-edited metadata. ``needs_review`` is
        not cleared here — the operator typically annotates first, then
        accepts. If they want to do both in one step, the UI can call
        accept after annotate.
        """
        if not _bot_exists(bot_id, network_path):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        body = (request.get_json(silent=True) or {})
        key = _validated_key(body)
        if not isinstance(key, str):
            return key
        note = body.get("note")
        # expires_at: None means "clear it"; missing means "leave alone".
        # We use the presence of the key in the body to distinguish.
        has_expires_at_field = "expires_at" in body
        expires_at = body.get("expires_at")

        shared = _shared_dir(network_path)
        bo = read_bot_overrides(shared, bot_id)
        existing = bo.get(key)
        if existing is None:
            return jsonify({
                "ok": False,
                "error": f"no override exists for {key}",
            }), 404

        # Resolve the new expires_at value: explicit (None or string) if
        # provided, existing otherwise.
        new_expires_at = expires_at if has_expires_at_field else existing.expires_at
        # Same for note — null means clear, missing means preserve.
        has_note_field = "note" in body
        new_note = note if has_note_field else existing.note

        try:
            updated = write_override(
                shared, bot_id, key, existing.value,
                set_by=existing.set_by,
                note=new_note,
                expires_at=new_expires_at,
                needs_review=existing.needs_review,
            )
        except OverrideValidationError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OverrideStateError as e:
            return jsonify({"ok": False, "error": f"overrides file unreadable: {e}"}), 500
        return jsonify({"ok": True, "entry": _entry_to_dict(key, updated)})

    @app.post("/api/customizations/<bot_id>/revert")
    def api_customizations_revert(bot_id: str) -> Response:
        """Delete an override. Next deploy materializes the schema default.

        Body: ``{"key": "<schema.path>"}``. Idempotent — reverting an
        already-absent key returns ok=true with ``removed=false``.
        """
        if not _bot_exists(bot_id, network_path):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        body = (request.get_json(silent=True) or {})
        key = _validated_key(body)
        if not isinstance(key, str):
            return key
        try:
            removed = delete_override(_shared_dir(network_path), bot_id, key)
        except OverrideStateError as e:
            return jsonify({"ok": False, "error": f"overrides file unreadable: {e}"}), 500
        except OverrideValidationError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "removed": removed})

    @app.post("/api/customizations/<bot_id>/set")
    def api_customizations_set(bot_id: str) -> Response:
        """Create or replace an override directly from the admin UI.

        Body: ``{"key": "<schema.path>", "value": <typed value>,
                 "note": "<optional>"}``.

        Distinct from accept/annotate/revert: those operate on an
        existing entry (whose value was put there by auto-promote,
        migration, or RSI). This endpoint is the deliberate
        operator-creates-an-override path — used today by the per-bot
        Customizations page when an operator picks a new value for a
        schema-known key that has no override yet (e.g. flipping
        ``openclaw.agents.defaults.models.cacheRetention`` from "short"
        to "long").

        Always writes with ``set_by="operator"`` and ``needs_review=False``
        — an operator-authored override is by definition reviewed. The
        underlying ``write_override`` validates against the schema
        (unknown key, per_bot=False, wrong type all rejected).
        """
        if not _bot_exists(bot_id, network_path):
            return jsonify({"ok": False, "error": f"unknown bot: {bot_id}"}), 404
        body = (request.get_json(silent=True) or {})
        key = _validated_key(body)
        if not isinstance(key, str):
            return key
        if "value" not in body:
            return jsonify({"ok": False, "error": "missing 'value' in body"}), 400
        value = body["value"]
        note = body.get("note")
        try:
            entry = write_override(
                _shared_dir(network_path), bot_id, key, value,
                set_by="operator",
                note=note,
                needs_review=False,
            )
        except OverrideValidationError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OverrideStateError as e:
            return jsonify({"ok": False, "error": f"overrides file unreadable: {e}"}), 500
        return jsonify({"ok": True, "entry": _entry_to_dict(key, entry)})
