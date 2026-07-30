"""Action tools — security baseline operations.

Currently exposes:

* ``action.security.accept_drift(bot_id, reason?)`` — commits the
  bot's live ``openclaw.json`` as the new backup-baseline. Wraps the
  same ``POST /api/security/accept-drift`` endpoint behind every
  "Accept as baseline" button on the Security → Backups subtab.

This module exists because of a 2026-05-20 transcript: operator on
the Backups subtab asked "can you accept all of the current configs
as baseline?" Evo, lacking a tool to call this endpoint, confabulated
that the request mapped to audit-finding Mute buttons (it doesn't —
they're separate mechanisms on the same Security page) and to the
``audit.py --reset-baselines`` CLI path (also wrong — that's an audit
baseline, not a backup baseline). The operator wanted "Yes, taking
care of that now. … done." Got several paragraphs of speculation
instead. This tool closes the action gap; ``pod_state.config_drift``
closes the enumeration gap.

Bulk semantics: the model iterates over drifted bots returned by
``pod_state.config_drift``. WRITE_RISKY single-bot rather than a
bulk variant because each call is a security-baseline write — the
operator can reasonably want one confirmation per bot under ``ask``,
and under ``auto`` the loop fires cleanly.

Risk tier: ``write_risky``. Backup-baseline acceptance changes what
the next ``heal`` pass treats as the "trusted" config. It's not
destructive (the next nightly backup commit re-stamps anyway, and
the model can always reset via a deploy), but it does shift a
security-relevant invariant — confirmation is the right default.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _bot_exists(network_path: Path, bot_id: str) -> bool:
    """Cheap sanity check: is bot_id in network.json::bots?"""
    try:
        from evolve_admin.config import load_network
        net = load_network(network_path)
        return bot_id in (net.get("bots") or {})
    except Exception:  # noqa: BLE001
        return False


def _resolve_base_url(network_path: Path) -> str | None:
    try:
        from evolve_admin.config import load_network, resolve_admin_base_url
        net = load_network(network_path)
        return resolve_admin_base_url(net).rstrip("/")
    except Exception:  # noqa: BLE001
        return None


def _fetch_drifted_keys(base_url: str, bot_id: str) -> list[str] | None:
    """Return the list of drifted keys for ``bot_id``, or ``None`` on
    fetch failure. Used by the validate gate so we can reject calls
    for bots that don't actually have drift.

    The admin server exposes a single-bot endpoint at
    ``/api/security/drift-detail?botId=<id>`` that runs the same
    ``heal.detect_backup_drift_keys`` comparison the UI uses. Empty
    list = no drift.
    """
    url = f"{base_url}/api/security/drift-detail?botId={bot_id}"
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"Accept": "application/json"},
        )
        with urlopen_admin(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    keys = (payload or {}).get("drifted_keys")
    return keys if isinstance(keys, list) else None


# ─── action.security.accept_drift ───────────────────────────────────────────


def _accept_drift_handler(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """POST ``/api/security/accept-drift`` for ``bot_id``.

    Same code path the **Accept as baseline** button uses on the
    Security → Backups subtab. The endpoint calls
    ``analyzer/backup.py::commit_baseline_local`` which:

      1. Copies the bot's live openclaw.json into
         ``/Users/<bot>/.openclaw/workspace/evolve-backup/openclaw.json``
      2. Commits with the ``[backup]`` prefix (no push — local only)
      3. The next ``heal.detect_backup_drift_keys`` pass sees no diff
    """
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "error": f"bot '{bot_id}' is not registered in network.json",
        }

    base_url = _resolve_base_url(network_path)
    if base_url is None:
        return {
            "ok": False,
            "error": "could not resolve admin base URL from network.json",
        }

    url = f"{base_url}/api/security/accept-drift"
    try:
        req = urllib.request.Request(
            url, method="POST",
            headers={
                "Accept": "application/json",
                "Content-type": "application/json",
            },
            data=json.dumps({"botId": bot_id}).encode("utf-8"),
        )
        with urlopen_admin(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        return {
            "ok": False,
            "error": (
                f"admin server returned HTTP {exc.code}"
                + (f": {err_body[:200]}" if err_body else "")
            ),
            "bot_id": bot_id,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"admin server unreachable at {url}: {exc.reason}",
            "bot_id": bot_id,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"HTTP POST failed: {exc}",
            "bot_id": bot_id,
        }

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"admin server returned non-JSON: {exc}",
            "bot_id": bot_id,
        }

    if not (payload or {}).get("ok"):
        return {
            "ok": False,
            "error": (payload or {}).get("error") or f"unexpected response: {payload!r}",
            "bot_id": bot_id,
        }

    return {
        "ok": True,
        "bot_id": bot_id,
        "message": (payload or {}).get("message", "baseline committed"),
        "reason": reason or "evo accepted drift baseline via tool",
        # Post-action verify (spec §3.7 lever #4). After acceptance the
        # bot should fall out of the drifted_bots list — call config_drift
        # to confirm.
        "verify_via": {
            "tool": "pod_state.config_drift",
            "args": {},
            "expect": (
                f"'{bot_id}' moves from drifted_bots[] to clean_bots[] "
                f"(next baseline-vs-live comparison sees no diff)"
            ),
        },
    }


def _accept_drift_validate(
    network_path: Path,
    bot_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: bot must exist AND actually have drift to accept.

    The drift check is on the validate path (not just the handler) so
    the proxy can refuse to render the confirm button when there's
    nothing to accept — better than letting the operator click
    "confirm" only to get back "no changes — already at baseline".
    """
    if not bot_id:
        return {"ok": False, "reason": "bot_id is required"}
    if not _bot_exists(network_path, bot_id):
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' is not registered in network.json; "
                "operator may have typo'd the id"
            ),
        }
    base_url = _resolve_base_url(network_path)
    if base_url is None:
        # Don't block validate on URL-resolution failure — handler will
        # surface a clearer error if the admin server is genuinely
        # unreachable. Validate's job is the "is this action sensible
        # right now" check, not infrastructure.
        return {"ok": True, "context": {"bot_id": bot_id}}

    keys = _fetch_drifted_keys(base_url, bot_id)
    if keys is None:
        # Couldn't read drift state — let the handler try anyway.
        return {"ok": True, "context": {"bot_id": bot_id}}
    if not keys:
        return {
            "ok": False,
            "reason": (
                f"bot '{bot_id}' has no config drift to accept "
                f"(baseline already matches live openclaw.json)"
            ),
            "context": {"bot_id": bot_id, "drifted_keys": []},
        }
    return {
        "ok": True,
        "context": {"bot_id": bot_id, "drifted_keys": keys},
    }


ACCEPT_DRIFT_TOOL = Tool(
    name="action.security.accept_drift",
    description=(
        "Accept a bot's live ``openclaw.json`` as the new "
        "backup-baseline. Same code path as the **Accept as baseline** "
        "button on the Security → Backups subtab — calls "
        "``analyzer/backup.py::commit_baseline_local`` which commits "
        "the live config into ``evolve-backup/`` with a ``[backup]`` "
        "prefix. The next ``heal`` pass sees no diff, so the "
        "config_drift signal clears. Use when the operator says "
        "'accept the new config as baseline', 'reset the drift baseline "
        "for X', or 'accept all configs as baseline' (enumerate first "
        "with ``pod_state.config_drift``, then iterate this tool per "
        "drifted bot). This is **distinct from audit-finding Mute** — "
        "Mute suppresses an info-tier audit advisory; accept_drift "
        "updates the backup baseline. Same Security page, different "
        "mechanisms."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log (e.g. 'operator "
                    "approved schema v7 migration; baseline reset')."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_accept_drift_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_accept_drift_validate,
    tags=("action", "security", "backup", "baseline"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(ACCEPT_DRIFT_TOOL)
