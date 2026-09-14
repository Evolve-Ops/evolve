"""forge_install_routes.py — Flask API routes for forge's install phase.

Spec: internal/spec-forge-side-effects-2026-06-02.md §5 (Forge install phase
"Phase 4.5: Materialize"). PR 4 wires the routes to the real install
helpers; PR 1 shipped the contract-stub-only version.

The endpoints are evo-callable (via the admin-daemon unix socket, same
mechanism that closed the evo cutover — see
internal/spec-evo-account-separation-2026-05-25.md §12) so forge's bot-side
LLM can request privileged installs without sudo.

Routes registered:
  POST /api/forge/install/launch-agent   → write a LaunchAgent plist + load it
  POST /api/forge/install/crontab        → 501 (deferred — needs sudoers)
  POST /api/forge/install/openclaw-patch → patch a bot's openclaw.json hook list

Forge's Phase 4.5 (in forge_engine.py) calls the underlying helpers in
``applications.install_helpers`` *directly* — these HTTP endpoints exist
so evo (and other clients on the daemon socket) can request installs
without sudo. The HTTP contract and the in-process helper return the
same envelope shape.
"""

# identity: see applications.app_identity.resolve_app_id (AL-1.4b). The single ``pkg_id`` is an optional
# REQUEST-BODY field naming the package an evolve-managed file marker should be
# attributed to — an attribution namespace, not this app's identity.

from __future__ import annotations

import re
from typing import Any

from flask import Flask, jsonify, request, Response

from ..applications import install_helpers


# The bot_id field is required on every endpoint — the daemon needs to
# know which bot's install surface to touch. Empty/missing returns 400.
_REQUIRED_BASE = ("bot_id",)

# JSON-pointer pattern the openclaw-patch endpoint understands. PR 4
# supports hook installation only — `/hooks/<event>/-` to append a new
# hook entry to the named event. Other pointers (replace at index,
# patch other config sections) return 501 — they're not in scope for
# this PR and have no use case from forge yet.
_HOOK_APPEND_POINTER_RE = re.compile(
    r"^/hooks/(?P<event>[A-Za-z_][A-Za-z0-9_]*)/-$"
)


def _require_fields(body: dict, fields: tuple[str, ...]) -> tuple[bool, Response | None]:
    """Validate that every field is present + non-empty in ``body``.

    Returns ``(True, None)`` on success or ``(False, error_response)`` on
    failure.
    """
    missing = [f for f in fields if not body.get(f)]
    if missing:
        return False, (jsonify({
            "ok": False,
            "error": "missing_fields",
            "missing": missing,
        }), 400)
    return True, None


def _envelope_response(method: str, body: dict, result: dict, status: int = 200) -> Response:
    """Build the standard response envelope from an install_helpers result."""
    payload: dict[str, Any] = {
        "ok": result.get("ok", False),
        "method": method,
        "bot_id": body.get("bot_id"),
        "error": result.get("error", ""),
    }
    payload.update({k: v for k, v in result.items() if k not in ("ok", "error")})
    return jsonify(payload), status


def register_forge_install_routes(app: Flask) -> None:
    """Register all three forge-install endpoints on ``app``.

    Called from web/server.py alongside the other ``register_*_routes``
    helpers (evo_routes, gallery_routes, etc.). Side-effect free until
    routed traffic arrives.
    """

    @app.post("/api/forge/install/launch-agent")
    def api_forge_install_launch_agent() -> Response:
        """Write a LaunchAgent plist for the bot and bootstrap it.

        Body:
            bot_id: str              — bot whose ~/Library/LaunchAgents/ to touch
            label: str               — plist Label (must follow com.{bot}.* convention)
            plist_xml: str           — full plist payload to write
            bootstrap: bool          — default true; set false to skip launchctl

        Returns:
            ok: bool
            artifact: str            — path to written plist on success
            loaded: bool             — whether ``launchctl bootstrap`` succeeded
            label: str               — echoed label
            error: str
        """
        body = request.get_json(silent=True) or {}
        ok, err = _require_fields(body, _REQUIRED_BASE + ("label", "plist_xml"))
        if not ok:
            return err
        result = install_helpers.install_launch_agent(
            body["bot_id"], body["label"], body["plist_xml"],
            bootstrap=bool(body.get("bootstrap", True)),
        )
        status = 200 if result.get("ok") else 502
        response = _envelope_response("install_launch_agent", body, result, status)
        # Echo the label field for client convenience.
        payload = response[0].get_json()
        payload["label"] = body.get("label")
        return jsonify(payload), status

    @app.post("/api/forge/install/crontab")
    def api_forge_install_crontab() -> Response:
        """Install a per-bot crontab entry.

        DEFERRED — see install_helpers.install_crontab_entry docstring.
        The `evolve` user lacks a `sudo -u <bot> crontab` grant; adding
        one requires an /etc/sudoers.d/evolve edit. Spec §4.1 lists
        crontab as a legacy mechanism (LaunchAgent or OC hooks preferred),
        so this is deferred to a follow-up PR with the sudoers change.

        Body:
            bot_id, label, schedule, command (all required)
        Returns:
            ok: false, error: "...", entry_id: null, label: echoed
        """
        body = request.get_json(silent=True) or {}
        ok, err = _require_fields(
            body, _REQUIRED_BASE + ("label", "schedule", "command")
        )
        if not ok:
            return err
        result = install_helpers.install_crontab_entry(
            body["bot_id"], body["schedule"], body["command"], body["label"],
        )
        response_data = {
            "ok": False,
            "method": "install_crontab_entry",
            "bot_id": body.get("bot_id"),
            "error": result.get("error", ""),
            "entry_id": None,
            "label": body.get("label"),
        }
        return jsonify(response_data), 501

    @app.post("/api/forge/install/heartbeat-instruction")
    def api_forge_install_heartbeat_instruction() -> Response:
        """v17 install endpoint — write an evolve-managed section to
        the bot's HEARTBEAT.md (or AGENTS.md) so the bot LLM reads it
        on the next heartbeat / session turn and executes the instruction.

        Spec: internal/spec-heartbeat-instruction-2026-06-03.md §7.

        Body:
            bot_id: str              — bot whose workspace markdown to patch
            file: str                — typically "HEARTBEAT.md" or "AGENTS.md"
            section_anchor: str      — markdown heading, e.g. "## Task Manager — Check"
            body: str                — natural-language instruction text
            pkg_id: str (optional)   — package id for the evolve-managed marker
            job_id: str (optional)   — forge job id for the marker

        Returns:
            ok: bool
            artifact: str            — ``{file}#{section_anchor_text}`` on success
            already_present: bool    — True when the section was already up to date
            created_file: bool       — True when forge had to create the markdown file
            error: str
        """
        body = request.get_json(silent=True) or {}
        ok, err = _require_fields(
            body, _REQUIRED_BASE + ("file", "section_anchor", "body"),
        )
        if not ok:
            return err
        result = install_helpers.install_heartbeat_instruction(
            body["bot_id"],
            body["file"],
            body["section_anchor"],
            body["body"],
            # identity: see resolve_app_id — request-body field naming the package a file marker is attributed to.
            pkg_id=str(body.get("pkg_id", "") or ""),
            job_id=str(body.get("job_id", "") or ""),
        )
        status = 200 if result.get("ok") else 502
        response = _envelope_response(
            "install_heartbeat_instruction", body, result, status,
        )
        payload = response[0].get_json()
        payload["file"] = body.get("file")
        return jsonify(payload), status

    @app.post("/api/forge/install/openclaw-patch")
    def api_forge_install_openclaw_patch() -> Response:
        """DEPRECATED v17 — see /api/forge/install/heartbeat-instruction.

        OpenClaw has no top-level ``hooks`` field; the prior hook-append
        path was structurally wrong (spec-heartbeat-instruction §1).
        Endpoint kept for one schema version so external callers see a
        clear remediation; removed in v18.

        Always returns 410 (Gone) with a clear pointer at the replacement.
        """
        body = request.get_json(silent=True) or {}
        ok, err = _require_fields(body, _REQUIRED_BASE + ("json_pointer",))
        if not ok:
            return err
        if "value" not in body:
            return jsonify({
                "ok": False,
                "error": "missing_fields",
                "missing": ["value"],
            }), 400
        return jsonify({
            "ok": False,
            "method": "patch_openclaw_json",
            "bot_id": body.get("bot_id"),
            "json_pointer": body.get("json_pointer"),
            "error": (
                "openclaw-patch hook installation is removed in v17 — "
                "OpenClaw has no hooks.heartbeat[] array. "
                "Use POST /api/forge/install/heartbeat-instruction with "
                "{file, section_anchor, body} instead. "
                "See internal/spec-heartbeat-instruction-2026-06-03.md."
            ),
        }), 410
