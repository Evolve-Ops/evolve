"""routes_alias.py — Standalone editor for the `correspondence` alias block.

The path-C Google wizard owns the bulk of `network.json[bots][<id>]`
write surface — workspace SA, subject, scopes, *and* the correspondence
block. That coupling means an operator who just wants to fix the alias
name (e.g. rename "Jane" to "Janet") has to walk the full multi-screen
wizard. This module exposes a narrow read/write surface for *only* the
correspondence block, intended for the standalone "Email alias" card on
the bot's Identity/Settings surface.

The disclosure values and JSON shape match
``docs/spec-correspondence-persona-2026-05-30.md`` §3 — same fields the
wizard writes, just isolated from the wizard's other writes so the
operator can rename the alias without re-running pre-flight or
re-uploading the SA JSON.

Endpoints
---------
GET  /api/bot/<bot_id>/alias
    Returns ``{ok, bot_id, alias, primary_user, multi_user,
    suggested_name, has_workspace_mailbox}``. ``suggested_name`` is
    ``primary_user.name`` when present — the natural default for
    EA-style bots (alias = the user we're corresponding on behalf of,
    not a fictional third party). ``has_workspace_mailbox`` tells the
    UI whether the bot can actually send mail today.

PUT  /api/bot/<bot_id>/alias
    Body: ``{name?, email_address?, disclosure?,
    disclosure_override_reason?}``. Writes only the ``correspondence``
    block; never touches ``google_integration`` or any sibling. Pass
    ``{name: null}`` (or empty string) to clear the alias entirely
    (bot becomes internal-only). Returns the updated alias block.

Validation rules
----------------
- ``disclosure`` must be ``"explicit"`` | ``"soft"`` | ``"none"`` when
  present.
- ``disclosure: "none"`` requires ``disclosure_override_reason`` (free
  text). Mirrors the wizard's audit-log requirement from the persona
  spec §6.
- ``email_address`` must contain ``@`` when present.
- ``name`` is allowed to be empty/null — that's the "clear alias /
  make bot internal-only" path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..config import load_network, save_network


_log = logging.getLogger(__name__)


_DISCLOSURE_VALUES = ("explicit", "soft", "none")


def _alias_view(bot_entry: dict[str, Any]) -> dict[str, Any]:
    """Build the operator-facing alias-state view for one bot.

    The shape matches the wizard's ``_bot_google_state`` so both
    surfaces produce identical fields — letting the standalone card
    and the wizard share a frontend renderer where convenient.
    """
    corr = bot_entry.get("correspondence") or {}
    primary_user = bot_entry.get("primary_user") or {}
    gi = bot_entry.get("google_integration") or {}
    return {
        "alias": {
            "name": corr.get("name"),
            "email_address": corr.get("email_address"),
            "disclosure": corr.get("disclosure"),
            "disclosure_override_reason": corr.get("disclosure_override_reason"),
            "signature": corr.get("signature"),
        } if corr else None,
        "primary_user": {
            "name": primary_user.get("name"),
            "email_address": primary_user.get("email_address"),
            "pod_user": primary_user.get("pod_user"),
        } if primary_user else None,
        "multi_user": bool(bot_entry.get("multiUser", False)),
        "suggested_name": primary_user.get("name") or None,
        "has_workspace_mailbox": bool(gi.get("subject")),
        "workspace_mailbox": gi.get("subject"),
        "display_name": bot_entry.get("display_name"),
    }


def _validate_alias_payload(
    body: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Pull + validate a PUT body; return (errors, cleaned).

    `cleaned` is the dict we'll write under
    ``bot_entry["correspondence"]`` (or ``{}`` to signal "clear the
    block entirely"). Validation rules mirror the wizard's
    ``_validate_configure_payload`` so the two write paths stay in
    lockstep — a future operator who switches between the wizard and
    the standalone editor never gets validation surprises.
    """
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    raw_name = body.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""

    # Empty name = clear the block. Return early with sentinel.
    if not name:
        # When the operator clears the alias, every other field becomes
        # meaningless — there's no alias to attach them to. Return an
        # explicit empty dict so the caller can delete the block.
        return errors, {}

    cleaned["name"] = name

    disclosure_raw = body.get("disclosure")
    disclosure = (disclosure_raw or "soft").strip().lower() if isinstance(disclosure_raw, str) else "soft"
    if disclosure not in _DISCLOSURE_VALUES:
        errors.append(
            f"disclosure must be one of {', '.join(_DISCLOSURE_VALUES)}"
        )
    cleaned["disclosure"] = disclosure

    if disclosure == "none":
        reason_raw = body.get("disclosure_override_reason")
        reason = reason_raw.strip() if isinstance(reason_raw, str) else ""
        if not reason:
            errors.append(
                "disclosure='none' requires disclosure_override_reason "
                "(free text, recorded in the audit log)"
            )
        else:
            cleaned["disclosure_override_reason"] = reason

    email_raw = body.get("email_address")
    if email_raw is not None:
        email = email_raw.strip() if isinstance(email_raw, str) else ""
        if email:
            if "@" not in email:
                errors.append(
                    f"email_address {email!r} doesn't look like an email"
                )
            cleaned["email_address"] = email

    signature_raw = body.get("signature")
    if signature_raw is not None:
        if not isinstance(signature_raw, str):
            errors.append("signature must be a string if provided")
        elif signature_raw.strip():
            cleaned["signature"] = signature_raw

    return errors, cleaned


def register_routes(app: Flask, network_path: Path) -> None:
    """Register ``/api/bot/<bot_id>/alias`` endpoints on the Flask app."""

    @app.get("/api/bot/<bot_id>/alias")
    def api_alias_get(bot_id: str) -> Response:
        network = load_network(network_path)
        bots = network.get("bots") or {}
        bot_entry = bots.get(bot_id)
        if not bot_entry:
            return jsonify({
                "ok": False,
                "error": f"bot {bot_id!r} not found in network.json",
            }), 404
        view = _alias_view(bot_entry)
        view["ok"] = True
        view["bot_id"] = bot_id
        return jsonify(view)

    @app.put("/api/bot/<bot_id>/alias")
    def api_alias_put(bot_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        errors, cleaned = _validate_alias_payload(body)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400

        network = load_network(network_path)
        bots = network.get("bots") or {}
        bot_entry = bots.get(bot_id)
        if not bot_entry:
            return jsonify({
                "ok": False,
                "error": f"bot {bot_id!r} not found in network.json",
            }), 404

        # Snapshot google_integration so we can assert in tests that we
        # never touch it. Defensive in prod too — surfaces accidental
        # nested writes as a clear test failure.
        gi_before = bot_entry.get("google_integration")

        if not cleaned:
            # Clear path: delete the block entirely if present.
            removed = bot_entry.pop("correspondence", None)
            if removed is None and "correspondence" not in bot_entry:
                # Already absent; idempotent no-op write to avoid spinning
                # the disk on a redundant call from a stale UI.
                view = _alias_view(bot_entry)
                view["ok"] = True
                view["bot_id"] = bot_id
                view["cleared"] = False
                return jsonify(view)
        else:
            bot_entry["correspondence"] = cleaned

        # Sanity check: google_integration must be unchanged.
        # `assert` here would be wrong (it raises AssertionError, not a
        # 5xx) — surface as a 500 with a clear message so any bug shows
        # up loudly in dev and prod.
        gi_after = bot_entry.get("google_integration")
        if gi_before != gi_after:
            _log.error(
                "alias write touched google_integration for bot %s — "
                "this is a bug. before=%r after=%r",
                bot_id, gi_before, gi_after,
            )
            return jsonify({
                "ok": False,
                "error": (
                    "internal: alias write would have modified "
                    "google_integration; refusing to save"
                ),
            }), 500

        try:
            save_network(network, network_path)
        except Exception as exc:
            _log.exception("alias write: save_network failed for %s", bot_id)
            return jsonify({
                "ok": False,
                "error": f"writing network.json failed: {exc}",
            }), 500

        view = _alias_view(bot_entry)
        view["ok"] = True
        view["bot_id"] = bot_id
        view["cleared"] = not cleaned
        return jsonify(view)
