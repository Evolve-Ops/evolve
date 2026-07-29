"""Bot-facing app capability-index route — ``/api/applications/expand``.

Spec: [docs/spec-app-invocation-just-works-2026-06-29.md](../../../docs/spec-app-invocation-just-works-2026-06-29.md)
§2.1 (the recognition layer). This is the Tier-2 half of the two-tier capability
index:

  - **Tier-1** (always-on) — the terse ``name — purpose`` menu spliced into the
    bot's AGENTS.md by ``app_registry._render_agents_md_section``. Each line ends
    ``→ expand_app("<id>")``.
  - **Tier-2** (on demand, THIS route) — the app's full command surface
    (when-to-invoke, how-to-use, hint words, example triggers, scope, CLI
    invocations), pulled into context ONLY when the model calls ``expand_app`` for
    that app. Keeps the heavy per-app detail out of every turn's token budget.

The Evolve OC plugin's ``expand_app`` tool (running as the bot's own macOS user)
reaches this route over the admin daemon's **unix socket**
(``{shared}/admin-daemon.sock``):

  POST /api/applications/expand  body ``{app_id}``  →  ``{ok, app_id, detail, ...}``

THE IDENTITY MODEL (mirrors ``directory_bot_routes`` / ``google_bot_routes`` — the
cross-bot-safe primitive):

  The calling bot is bound **server-side** from the kernel-reported peer uid of the
  unix-socket connection (``peer_auth.resolve_peer_bot_id``) — NEVER from a request
  field. A bot literally cannot present another bot's uid, so a bot can only ever
  expand an app installed in ITS OWN workspace. TCP requests (the admin-UI binding)
  and unknown peer uids get 403 — a browser session must never act as a bot.

READ-ONLY BY CONSTRUCTION:

  The route only *reads* manifests and renders a projection of fields already
  surfaced to the bot (identity/usage/scope/CLI). It performs no caller-directed
  write (``list_manifests`` may idempotently migrate the bot's OWN stale manifest
  schema on read — the same evolve-owned migration every manifest read does — but
  ``app_id`` never influences it), resolves no arbitrary path, and returns no
  credential/secret content (manifests hold none).
  A discovered (unvouched) app CAN be expanded on demand — the defined-only filter
  is on the always-on Tier-1 menu (spec OQ-3), not on this Tier-2 lookup — but the
  resolver is scoped to the caller's own visible manifests, so nothing outside the
  bot's app set is reachable.

FORWARD-COMPAT (spec §2.1):

  The payload shape (``{app_id, detail}`` where ``detail`` is a clean markdown
  string) is deliberately tool-agnostic so this can be promoted to a native
  OpenClaw deferred-tool primitive if/when one lands, without reshaping callers.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from ..applications import app_registry
from ..applications.app_registry import _VISIBLE_STATUSES
from ..applications.manifest import ApplicationManifest, list_manifests
from ..config import load_network
from . import peer_auth
from .routes_bot_users import _shared_dir_from_net
from .routes_shared import _audit_log_entry


log = logging.getLogger(__name__)

# Bound the id we accept so a runaway/garbage arg can't drive an expensive scan or
# bloat the audit log. App ids/names are short; this is generous.
MAX_APP_ID_LEN = 200


def _audit(bot_id: str, details: dict) -> None:
    """Audit one ``expand_app`` call — the recognition-layer observability signal.

    Spec OQ-4: make recognition *observable* so intent→correct-app rate and an
    over-calling regression are readable from session data without a full
    dashboard (that's E.B). Each call records the resolved app id (not PII), a
    hit/miss flag (a miss = the model reached for an app that isn't installed — a
    recognition-confusion signal), and the matched app's definition_status.
    Best-effort by contract (``_audit_log_entry`` swallows its own I/O errors).
    """
    _audit_log_entry("capability_index.expand_app", bot_id,
                     {**details, "surface": "bot_plugin"})


def _resolve_app(
    manifests: list[ApplicationManifest], app_id: str,
) -> "ApplicationManifest | None":
    """Find the visible manifest matching ``app_id`` — id / name / display_name,
    case-insensitive.

    Flexible on purpose (anti-rigidity): the Tier-1 menu shows the canonical id,
    but a model that passes the display name or the short name still hits rather
    than being told to learn the exact key. Exact-id match wins over a name/
    display-name collision. Only VISIBLE manifests are candidates (paused/hidden/
    deprecated apps are not expandable).
    """
    needle = app_id.strip().casefold()
    if not needle:
        return None
    visible = [m for m in manifests if (m.status or "active") in _VISIBLE_STATUSES]
    # Pass 1: exact id (the canonical form shown in the menu).
    for m in visible:
        if (m.id or "").strip().casefold() == needle:
            return m
    # Pass 2: name / display_name fallback.
    for m in visible:
        for cand in (m.name, m.display_name):
            if (cand or "").strip().casefold() == needle:
                return m
    return None


def register_applications_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/applications/expand`` route on the Flask app."""

    @app.post("/api/applications/expand")
    def api_applications_expand() -> ResponseReturnValue:
        """Return the Tier-2 detail for one app in the *calling* bot's workspace.

        Body: ``{"app_id": "<id | name | display_name>"}``. The acting bot is the
        unix-socket peer uid, bound server-side — the lookup can only ever read the
        caller's own installed apps. A miss returns 404 with the ids that WOULD
        resolve (defined apps), so the model can recover instead of guessing.
        Never 500s: a bad body is a 400, an internal fault degrades to a clean
        error envelope.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            # No peer-bound identity (TCP, or unknown uid) → never serve app data.
            # Fail-closed; this is the cross-bot boundary.
            return jsonify({"ok": False, "error": "caller not a recognized bot"}), 403

        body = request.get_json(silent=True) or {}
        app_id = body.get("app_id")
        if not isinstance(app_id, str) or not app_id.strip():
            return jsonify({"ok": False, "error": "app_id is required"}), 400
        app_id = app_id.strip()
        if len(app_id) > MAX_APP_ID_LEN:
            return jsonify({"ok": False, "error": "app_id is too long"}), 400

        net = load_network(network_path)
        shared = _shared_dir_from_net(net)
        try:
            manifests = list_manifests(shared, bot_id)
        except Exception as exc:  # noqa: BLE001 — never 500 a bot read; degrade cleanly
            log.warning("expand_app list_manifests for %s failed: %s", bot_id, exc)
            return jsonify({
                "ok": False,
                "error": "apps are temporarily unavailable; try again shortly",
            }), 503

        m = _resolve_app(manifests, app_id)
        if m is None:
            # Offer the ids that WOULD resolve — the defined menu the bot sees in
            # AGENTS.md — so a miss is recoverable, not a dead end. We list defined
            # only (parity with the Tier-1 menu; don't advertise churny drafts).
            available = sorted(
                app_registry.app_ref(x)
                for x in manifests
                if (x.status or "active") in _VISIBLE_STATUSES
                and app_registry.is_defined(x)
                and app_registry.app_ref(x)
            )
            _audit(bot_id, {"app_id": app_id, "hit": False})
            return jsonify({
                "ok": False,
                "error": f"no installed app matches {app_id!r}",
                "available": available,
            }), 404

        try:
            detail = app_registry.render_app_detail_section(m)
        except Exception as exc:  # noqa: BLE001 — degrade cleanly, never 500
            log.warning("expand_app render for %s/%s failed: %s", bot_id, app_id, exc)
            return jsonify({
                "ok": False,
                "error": "could not render that app's details right now",
            }), 503

        ref = app_registry.app_ref(m)
        _audit(bot_id, {
            "app_id": ref,
            "hit": True,
            "definition_status": (getattr(m, "definition_status", "") or "").strip(),
        })
        return jsonify({
            "ok": True,
            "app_id": ref,
            "name": m.display_name or m.name or m.id,
            "definition_status": (getattr(m, "definition_status", "") or "").strip(),
            "detail": detail,
        })
