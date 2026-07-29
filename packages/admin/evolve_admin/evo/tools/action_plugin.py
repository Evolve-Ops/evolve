"""Action tools — plugin enable/disable.

Two write_safe action tools that wrap the admin server's existing
plugin-proposal endpoints:

* ``action.plugin.enable(bot_id, plugin_name)`` — creates an
  ``EnablePluginEntry`` proposal on the bot. POSTs to
  ``/api/plugins-admin/propose-enable`` so the proposal lands in the
  standard pipeline (validates → operator approves → applier writes
  ``openclaw.json`` + restarts the gateway).

* ``action.plugin.disable(bot_id, plugin_name)`` — creates a
  ``DisablePluginEntry`` proposal. Same pipeline. The server-side
  applier refuses if the plugin is in the baseline's required set.

Why a tool wrapper instead of evo telling the operator to click the
UI: the UI path is the right fallback when this tool isn't available,
but the tool path is correct when it IS. Operators say things like
"enable discord on team_bot_b" in chat; evo should be able to act on that
within the operator's authority tier rather than punting to a
sidebar-navigation walk-through (or worse — a ``sudo -u team_bot_b python3
-c "..."`` shell snippet, which was the 2026-05-20 operator-visible
failure that motivated this PR).

Both tools are ``WRITE_SAFE`` under the spec's risk tiering:
  * Reversible — the proposal sits pending; the operator can reject
    or snooze before the applier touches anything.
  * Bounded blast radius — one plugin entry on one bot.
  * Auto-execute under 'auto-small' and 'auto' authority tiers; stage
    a confirm button under 'ask'.

The tools POST via HTTP to the admin server. Architecturally this
keeps the admin server as the owner of plugin-proposal logic (so any
future validation / logging / notification piggybacks for free) and
avoids coupling the MCP-server child process to admin internals.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Constraints on plugin_name. Mirrors what the admin endpoint accepts
# in practice — plugin names are short ASCII identifiers. Keeping the
# validation here means we surface clearer errors than waiting for the
# server's strip-and-check to reject empty input.
_MAX_PLUGIN_NAME = 64

# ASCII-only patterns. Unicode look-alikes ("тink" — Cyrillic т) MUST
# be rejected — str.isalnum() accepts them silently and we'd end up
# passing them through to the admin endpoint where they'd either
# collide with a real bot id or fail at a layer further from the
# operator's screen. Reject up-front with a clear error.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_NAME_RE = re.compile(rf"^[A-Za-z0-9._-]{{1,{_MAX_PLUGIN_NAME}}}$")


def _resolve_admin_base_url(network_path: Path | None) -> tuple[str | None, str | None]:
    """Resolve the admin server base URL from network_path.

    Returns ``(base_url, error)``. Mirrors the pattern from
    ``pod_state_audit._read_via_http`` so the failure modes look the
    same across tools.
    """
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


def _post_json(
    url: str, body: dict[str, Any], timeout: int = 10,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST a JSON body. Returns ``(payload, error)``. Exactly one
    is non-None.

    Failure modes the caller surfaces verbatim:
      * URLError (admin server unreachable)
      * HTTPError 4xx/5xx (admin server rejected the request)
      * Non-JSON response body
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen_admin(req, timeout=timeout) as resp:
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        # Try to surface the admin server's error message body, not
        # just the status code — the operator gets a clearer pointer.
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        return None, (
            f"admin server returned HTTP {exc.code}"
            + (f": {err_body[:200]}" if err_body else "")
        )
    except urllib.error.URLError as exc:
        return None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"HTTP POST failed: {exc}"
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "admin server returned unexpected payload shape"
    return payload, None


def _is_valid_plugin_name(name: str) -> bool:
    """Plugin names are short ASCII identifiers — letters, digits,
    dots, dashes, underscores. Same shape the admin server accepts.
    ASCII-only: rejects Unicode look-alikes that str.isalnum() would
    accept."""
    return bool(name) and _PLUGIN_NAME_RE.fullmatch(name) is not None


def _is_valid_bot_id(bot_id: str) -> bool:
    """Bot id constraints mirror Unix username rules — ASCII letters,
    digits, dashes, underscores. Keeps the value safe to pass through
    HTTP without surprises and clearly distinguishes invalid input
    from genuine 'no such bot'. ASCII-only — rejects look-alikes."""
    return bool(bot_id) and _BOT_ID_RE.fullmatch(bot_id) is not None


# ─── action.plugin.enable ───────────────────────────────────────────────────


def _enable_handler(
    bot_id: str,
    plugin_name: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Create an EnablePluginEntry proposal for ``bot_id`` / ``plugin_name``.

    The proposal lands in pending; the operator approves it from the
    Recommendations page (or evo can chain ``action.proposal.apply``
    if authority allows). The applier writes the bot's openclaw.json
    and restarts the gateway — no manual file edits needed.
    """
    base_url, err = _resolve_admin_base_url(network_path)
    if err:
        return {"ok": False, "error": err}
    payload, post_err = _post_json(
        f"{base_url}/api/plugins-admin/propose-enable",
        {"bot_id": bot_id, "plugin_name": plugin_name},
    )
    if post_err:
        return {"ok": False, "error": post_err}
    # Admin server's response shape (from server.py:_operator_proposal_response):
    #   {ok: bool, proposal?: {id, ...}, error?: str}
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "admin server rejected the proposal",
        }
    proposal = payload.get("proposal") or {}
    proposal_id = proposal.get("id")
    return {
        "ok": True,
        "bot_id": bot_id,
        "plugin_name": plugin_name,
        "proposal_id": proposal_id,
        "proposal_status": proposal.get("status") or "pending",
        # Post-action verify hint (spec §3.7 reliability lever #4).
        # The model confirms the proposal landed by calling
        # pod_state.proposals.pending and looking for the proposal id.
        # AGENTS.md teaches the broader pattern.
        "verify_via": {
            "tool": "pod_state.proposals.pending",
            "args": {"proposal_id": proposal_id} if proposal_id else {},
            "expect": (
                "this proposal_id appears in pending (or operator has "
                "already approved/snoozed it)"
            ),
        },
    }


def _enable_validate(
    bot_id: str,
    plugin_name: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot_id + plugin_name are syntactically valid.

    We don't pre-check whether the bot actually has the plugin
    configured — the admin endpoint validates that during proposal
    creation, and we'd rather surface its error verbatim than
    duplicate the logic here.
    """
    if not _is_valid_bot_id(bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot_id {bot_id!r} is invalid — must be letters / "
                "digits / underscores / dashes, up to 64 chars"
            ),
        }
    if not _is_valid_plugin_name(plugin_name):
        return {
            "ok": False,
            "reason": (
                f"plugin_name {plugin_name!r} is invalid — must be "
                "letters / digits / dots / underscores / dashes, "
                f"up to {_MAX_PLUGIN_NAME} chars"
            ),
        }
    return {"ok": True}


ENABLE_TOOL = Tool(
    name="action.plugin.enable",
    description=(
        "Propose enabling an OC plugin on one bot. Creates a pending "
        "EnablePluginEntry proposal — the operator approves it from "
        "the Recommendations page (you can also chain "
        "action.proposal.apply if the operator's authority tier "
        "allows). The applier writes the bot's openclaw.json and "
        "restarts the gateway automatically, so the operator never "
        "has to run shell commands. Use this when the operator says "
        "'enable <plugin> on <bot>' or similar — DO NOT walk them "
        "through editing openclaw.json by hand."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — Unix username, e.g. 'team_bot_a', 'team_bot_b'.",
            },
            "plugin_name": {
                "type": "string",
                "description": (
                    "Plugin name as it appears under openclaw.json's "
                    "``plugins`` key — e.g. 'discord', 'slack', "
                    "'gmail-google'. Get the canonical list from "
                    "pod_state.bots or the bot's existing "
                    "openclaw.json."
                ),
            },
        },
        "required": ["bot_id", "plugin_name"],
        "additionalProperties": False,
    },
    handler=_enable_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_enable_validate,
    tags=("action", "plugin"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(ENABLE_TOOL)


# ─── action.plugin.disable ──────────────────────────────────────────────────


def _disable_handler(
    bot_id: str,
    plugin_name: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Create a DisablePluginEntry proposal.

    The server-side applier refuses to apply this if the plugin is
    in the baseline's required_plugins (a safety rail). The proposal
    creation here doesn't pre-check that — the operator sees the
    error if/when they approve.
    """
    base_url, err = _resolve_admin_base_url(network_path)
    if err:
        return {"ok": False, "error": err}
    payload, post_err = _post_json(
        f"{base_url}/api/plugins-admin/propose-disable",
        {"bot_id": bot_id, "plugin_name": plugin_name},
    )
    if post_err:
        return {"ok": False, "error": post_err}
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "admin server rejected the proposal",
        }
    proposal = payload.get("proposal") or {}
    proposal_id = proposal.get("id")
    return {
        "ok": True,
        "bot_id": bot_id,
        "plugin_name": plugin_name,
        "proposal_id": proposal_id,
        "proposal_status": proposal.get("status") or "pending",
        "verify_via": {
            "tool": "pod_state.proposals.pending",
            "args": {"proposal_id": proposal_id} if proposal_id else {},
            "expect": (
                "this proposal_id appears in pending (or operator has "
                "already approved/snoozed/rejected it)"
            ),
        },
    }


def _disable_validate(
    bot_id: str,
    plugin_name: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm bot_id + plugin_name are syntactically valid.
    Mirrors _enable_validate."""
    if not _is_valid_bot_id(bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot_id {bot_id!r} is invalid — must be letters / "
                "digits / underscores / dashes, up to 64 chars"
            ),
        }
    if not _is_valid_plugin_name(plugin_name):
        return {
            "ok": False,
            "reason": (
                f"plugin_name {plugin_name!r} is invalid — must be "
                "letters / digits / dots / underscores / dashes, "
                f"up to {_MAX_PLUGIN_NAME} chars"
            ),
        }
    return {"ok": True}


DISABLE_TOOL = Tool(
    name="action.plugin.disable",
    description=(
        "Propose disabling an OC plugin on one bot. Creates a pending "
        "DisablePluginEntry proposal — the operator approves it from "
        "the Recommendations page. The applier refuses to disable "
        "plugins in the baseline's required_plugins (a safety rail "
        "so you can't accidentally take down core capabilities). "
        "Use this when the operator says 'disable <plugin> on <bot>' "
        "or 'remove <plugin> from <bot>'. The applier handles the "
        "openclaw.json edit and gateway restart automatically — "
        "DO NOT generate shell snippets for the operator to run."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id — Unix username, e.g. 'team_bot_a', 'team_bot_b'.",
            },
            "plugin_name": {
                "type": "string",
                "description": (
                    "Plugin name as it appears under openclaw.json's "
                    "``plugins`` key — e.g. 'codex', 'discord'."
                ),
            },
        },
        "required": ["bot_id", "plugin_name"],
        "additionalProperties": False,
    },
    handler=_disable_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_disable_validate,
    tags=("action", "plugin"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(DISABLE_TOOL)
