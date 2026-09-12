"""repair_chat_routes — Flask routes for the Apps-page repair chat.

Surfaces four endpoints under
``/api/applications/<bot_id>/<app_id>/repair-chat/``:

    GET    /state            — current lock + usage + recent log entries
    POST   /message          — send one operator message, dispatch to bot LLM,
                               persist user+assistant turns to chat log
    POST   /apply            — apply a previously-proposed action by id
    POST   /end              — release the chat lock

The dispatch path runs through the bot's own OpenClaw agent (see
:mod:`evolve_admin.applications.repair_chat`); this module is the thin
HTTP wrapper that owns request validation, lock acquisition, rate-limit
gating, and chat-log book-keeping.

Operator identity is the cookie-derived browser id used by the rest of
the admin UI's per-session features (``home_chat_routes._anon_cookie``
style). We use a request-local fallback rather than depending on
``home_chat_routes`` directly — keeps the modules independent and the
cookie shape is trivially small.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, make_response, request

from ..applications import repair_chat as _rc

log = logging.getLogger(__name__)

_OPERATOR_COOKIE = "repair_chat_operator"
_COOKIE_MAX_AGE_S = 60 * 60 * 24 * 365  # 1 year — same shape as home chat
_COOKIE_VALUE_BYTES = 8


def _operator_id() -> tuple[str, bool]:
    """Return ``(id, is_new)``.

    The cookie is created lazily on first repair-chat call. A second
    operator on the same pod gets their own browser-scoped id so locks
    actually distinguish them. Falls back to a per-request token if the
    request object isn't carrying cookies for whatever reason.
    """
    existing = request.cookies.get(_OPERATOR_COOKIE) if request else None
    if existing:
        return existing, False
    new_id = secrets.token_hex(_COOKIE_VALUE_BYTES)
    return new_id, True


def _attach_operator_cookie(resp, operator_id: str, is_new: bool):
    if is_new:
        resp.set_cookie(
            _OPERATOR_COOKIE, operator_id,
            max_age=_COOKIE_MAX_AGE_S, httponly=True, samesite="Lax",
        )
    return resp


def _bot_app_path_safe(bot_id: str, app_id: str) -> tuple[str, str] | None:
    """Reject anything outside a tight slug shape. Manifests live at
    ``/Users/<bot>/.openclaw/workspace/manifests/<app>.json`` — a path
    traversal in either component would let an unauthenticated POST
    pivot to arbitrary writes. Bot ids and app ids are already
    enforced as slugs by the scanner; we re-check here so a malformed
    URL fails at the wrapper, not deep inside save_manifest.
    """
    import re
    pat = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    if not pat.match(bot_id or "") or not pat.match(app_id or ""):
        return None
    return bot_id, app_id


def register_repair_chat_routes(app: Flask, network_path: Path) -> None:
    """Mount /api/applications/<bot>/<app>/repair-chat/* on the Flask app."""
    from ..config import load_network as _load_network

    def _shared_dir() -> Path:
        return Path(
            _load_network(network_path).get(
                "sharedDir", "/Users/Shared/evolve"
            )
        )

    def _load_manifest_dict(bot_id: str, app_id: str, shared_dir: Path):
        """Best-effort: returns (manifest, error_string).

        Loading goes through ``applications.manifest.load_manifest`` so
        we pick up the same migration + hydration the scanner uses.
        Returning None for the manifest means the operator clicked
        repair-chat on an app that has no on-disk manifest yet — the
        Apps page UI should never let that happen, but we treat it as
        an error rather than crashing.
        """
        from ..applications.manifest import load_manifest
        try:
            manifest = load_manifest(app_id, bot_id, shared_dir)
        except Exception as exc:  # noqa: BLE001
            log.exception("repair_chat load_manifest failed")
            return None, f"manifest load failed: {exc}"
        if manifest is None:
            return None, "manifest not found"
        return manifest, None

    # ── GET state ──────────────────────────────────────────────────────────

    @app.get("/api/applications/<bot_id>/<app_id>/repair-chat/state")
    def repair_chat_state(bot_id: str, app_id: str):
        """Return current lock, usage, recent log entries.

        Polled when the modal opens so the UI knows whether to render
        an active session (read-only because someone else is in it) or
        a fresh one. Doesn't acquire the lock — that happens on the
        first /message call.
        """
        ids = _bot_app_path_safe(bot_id, app_id)
        if ids is None:
            return jsonify({"ok": False, "error": "bad bot_id or app_id"}), 400
        sd = _shared_dir()
        operator_id, is_new = _operator_id()
        body = {
            "ok": True,
            "lock": _rc.lock_status(sd, bot_id, app_id),
            "usage": _rc.cap_status(sd, bot_id, app_id),
            "log": _rc.read_chat_log(sd, bot_id, app_id, limit=200),
            "operator_id": operator_id,
            "estimated_cost_per_turn_usd": _rc.ESTIMATED_COST_PER_TURN_USD,
        }
        return _attach_operator_cookie(
            make_response(jsonify(body)), operator_id, is_new,
        )

    # ── POST message ──────────────────────────────────────────────────────

    @app.post("/api/applications/<bot_id>/<app_id>/repair-chat/message")
    def repair_chat_message(bot_id: str, app_id: str):
        """One operator turn → one assistant turn.

        Flow:
          1. Validate id shape, body shape.
          2. Check + acquire the per-(bot,app) lock — fail if someone
             else holds it.
          3. Check the per-day message cap — fail if exhausted.
          4. Load the manifest.
          5. Append the operator's message to the chat log.
          6. Dispatch the turn (system prompt + history + user message)
             through the bot's OC agent.
          7. Append the assistant reply (with proposals) to the chat log.
          8. Bump usage and return the assistant turn to the UI.

        Bot-offline failure mode: the dispatcher returns an error in
        ``ChatTurnResult.error``. We persist a placeholder assistant
        turn explaining the failure so the operator sees it in the log
        without losing the conversation thread.
        """
        ids = _bot_app_path_safe(bot_id, app_id)
        if ids is None:
            return jsonify({"ok": False, "error": "bad bot_id or app_id"}), 400

        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "message required"}), 400
        if len(message) > 8000:
            return jsonify({
                "ok": False, "error": "message too long (max 8000 chars)",
            }), 400

        sd = _shared_dir()
        operator_id, is_new = _operator_id()

        # Lock — refuse if someone else holds it.
        acquired, lock_state = _rc.try_acquire_lock(
            sd, bot_id, app_id, owner=operator_id,
        )
        if not acquired:
            resp = make_response(jsonify({
                "ok": False,
                "error": "another operator is in this repair session",
                "lock": lock_state,
            }), 409)
            return _attach_operator_cookie(resp, operator_id, is_new)

        # Rate limit — pre-check before any LLM cost.
        usage = _rc.cap_status(sd, bot_id, app_id)
        if not usage["allowed"]:
            resp = make_response(jsonify({
                "ok": False,
                "error": (
                    f"daily message cap reached "
                    f"({usage['used']}/{usage['daily_cap']}) — try again "
                    "tomorrow or raise the cap in network.json"
                ),
                "usage": usage,
            }), 429)
            return _attach_operator_cookie(resp, operator_id, is_new)

        manifest, err = _load_manifest_dict(bot_id, app_id, sd)
        if err:
            resp = make_response(jsonify({"ok": False, "error": err}), 404)
            return _attach_operator_cookie(resp, operator_id, is_new)
        manifest_dict = manifest.to_dict()
        app_name = manifest_dict.get("name") or manifest_dict.get("display_name") or app_id

        # Persist operator turn FIRST so a dispatcher crash doesn't lose
        # the user's input.
        _rc.append_chat_log(sd, bot_id, app_id, {
            "role": "user", "text": message, "operator_id": operator_id,
        })

        history = _build_history_for_dispatch(sd, bot_id, app_id)

        result = _rc.dispatch_chat_turn(
            shared_dir=sd, bot_id=bot_id, app_id=app_id,
            app_name=str(app_name), manifest_dict=manifest_dict,
            history=history, latest_message=message,
        )

        # Persist assistant turn — even on failure, so the operator sees
        # "{bot} is currently offline; please try again later" inline.
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "text": result.reply if result.ok else "",
            "proposals": result.proposals,
            "cost_usd": result.cost_usd,
            "tokens": result.tokens,
        }
        if not result.ok or result.error:
            assistant_entry["error"] = result.error or "(dispatch returned no text)"
            assistant_entry["text"] = _bot_offline_message(bot_id, result.error)
        if result.timed_out:
            assistant_entry["timed_out"] = True
        _rc.append_chat_log(sd, bot_id, app_id, assistant_entry)

        # Always bump usage on attempt — a failed dispatch still cost
        # tokens (the OC agent may have produced a partial response we
        # killed mid-turn). The operator can dispute that with a manual
        # cap raise; we don't want to silently let retries blow the cap.
        _rc.bump_usage(sd, bot_id, app_id)

        post_body = {
            "ok": True,
            "assistant": assistant_entry,
            "usage": _rc.cap_status(sd, bot_id, app_id),
            "lock": _rc.lock_status(sd, bot_id, app_id),
        }
        resp = make_response(jsonify(post_body))
        return _attach_operator_cookie(resp, operator_id, is_new)

    # ── POST apply ────────────────────────────────────────────────────────

    @app.post("/api/applications/<bot_id>/<app_id>/repair-chat/apply")
    def repair_chat_apply(bot_id: str, app_id: str):
        """Apply a proposal by id.

        Body: ``{proposal_id}``. We look the proposal up in the chat
        log rather than trusting a client-supplied payload — the
        operator can only apply something the LLM actually emitted, not
        arbitrary manifest writes.
        """
        ids = _bot_app_path_safe(bot_id, app_id)
        if ids is None:
            return jsonify({"ok": False, "error": "bad bot_id or app_id"}), 400

        body = request.get_json(silent=True) or {}
        proposal_id = (body.get("proposal_id") or "").strip()
        if not proposal_id:
            return jsonify({"ok": False, "error": "proposal_id required"}), 400

        sd = _shared_dir()
        operator_id, is_new = _operator_id()

        # Lock check — must hold the lock to apply. This prevents a
        # second operator from "stealing" applies from a session
        # someone else is driving.
        lock = _rc.lock_status(sd, bot_id, app_id)
        if lock["locked"] and lock["owner"] != operator_id:
            resp = make_response(jsonify({
                "ok": False, "error": "lock held by another operator",
                "lock": lock,
            }), 409)
            return _attach_operator_cookie(resp, operator_id, is_new)

        proposal = _rc.find_proposal_by_id(sd, bot_id, app_id, proposal_id)
        if not proposal:
            return jsonify({"ok": False, "error": "proposal not found"}), 404

        try:
            result = _rc.apply_proposal(
                sd, bot_id, app_id, proposal,
                by=f"ui:operator:{operator_id[:8]}",
            )
        except _rc.ApplyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            log.exception("repair_chat apply_proposal raised")
            return jsonify({"ok": False, "error": f"apply failed: {exc}"}), 500

        _rc.append_chat_log(sd, bot_id, app_id, {
            "role": "apply",
            "proposal_id": proposal_id,
            "action": proposal.get("action"),
            "operator_id": operator_id,
            "result": result,
        })

        # If the bot signaled done, release the lock automatically so the
        # next operator can pick up the same app without waiting on TTL.
        if proposal.get("action") == "done":
            _rc.release_lock(sd, bot_id, app_id, owner=operator_id)

        resp = make_response(jsonify({"ok": True, "result": result}))
        return _attach_operator_cookie(resp, operator_id, is_new)

    # ── POST end ──────────────────────────────────────────────────────────

    @app.post("/api/applications/<bot_id>/<app_id>/repair-chat/end")
    def repair_chat_end(bot_id: str, app_id: str):
        """Release the lock. Idempotent."""
        ids = _bot_app_path_safe(bot_id, app_id)
        if ids is None:
            return jsonify({"ok": False, "error": "bad bot_id or app_id"}), 400
        sd = _shared_dir()
        operator_id, is_new = _operator_id()
        released = _rc.release_lock(sd, bot_id, app_id, owner=operator_id)
        resp = make_response(jsonify({"ok": True, "released": released}))
        return _attach_operator_cookie(resp, operator_id, is_new)


# ── Helpers ────────────────────────────────────────────────────────────────

def _build_history_for_dispatch(
    shared_dir: Path, bot_id: str, app_id: str,
) -> list[dict]:
    """Project the chat log into the {role, text} shape the dispatcher
    expects. Drops apply-events and any entry missing text — only the
    user/assistant turns themselves form context for the next reply.
    """
    raw = _rc.read_chat_log(shared_dir, bot_id, app_id, limit=200)
    history: list[dict] = []
    for entry in raw:
        role = (entry.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        history.append({"role": role, "text": text})
    return history


def _bot_offline_message(bot_id: str, error: str) -> str:
    """Produce an operator-readable failure message.

    The dispatcher error is technical (subprocess exit codes, "openclaw
    binary not found", etc.); the operator sees a human sentence with
    the underlying string truncated for diagnostic context.
    """
    bot_label = bot_id or "the bot"
    err_excerpt = (error or "").strip()
    if len(err_excerpt) > 240:
        err_excerpt = err_excerpt[:240] + "…"
    if not err_excerpt:
        return (
            f"{bot_label} didn't respond on this turn. The dispatch may "
            "have timed out — please try again, or close the modal if "
            "the bot looks offline."
        )
    return (
        f"{bot_label} is currently unreachable. The dispatcher reported: "
        f"{err_excerpt}"
    )
