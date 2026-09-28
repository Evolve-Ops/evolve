"""
evolve_admin.oauth.providers.gmail_provider — Gmail-only OAuth provider (V2.1-3).

Wraps ``evolve_admin.skills.gmail_install`` in the ``Provider`` interface.
Thin pass-through — every callable delegates directly to ``gmail_install``
functions without adding new logic.

The reader factory pattern is identical to ``gog_provider.py``: default
readers use direct-read + sudo fallback (CLAUDE.md pattern); tests inject
lightweight stubs.

Registration
------------
At import time this module appends a ``Provider`` instance to
``PROVIDER_REGISTRY``.  The guard at the bottom prevents double-registration.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


# ── Default reader factories (same pattern as gog_provider.py) ────────────────


def _default_read_auth_profiles(bot_id: str) -> dict:
    """Read auth-profiles.json for bot_id via direct read + sudo fallback."""
    from ...config import bot_home as _bot_home
    p = _bot_home(bot_id) / ".openclaw" / "auth-profiles.json"
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except PermissionError:
        pass
    except Exception:
        return {}
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return {}


def _default_read_network(shared_dir: Path) -> dict:
    """Read network.json from shared_dir's parent."""
    network_path = (shared_dir / ".." / "network.json").resolve()
    try:
        if network_path.exists():
            return json.loads(network_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def make_plugin_enabled_reader() -> Callable[[str], "bool | None"]:
    """Build the plugin-enabled reader for Gmail (checks ``google`` plugin entry)."""
    def _read(bot_id: str) -> "bool | None":
        from ...skills.gmail_install import GMAIL_PLUGIN_NAME
        try:
            from plugins import inventory as _inv  # type: ignore[import]
            inv = _inv.read_inventory(bot_id)
            if inv.read_error:
                return None
            for entry in inv.entries:
                if entry.name == GMAIL_PLUGIN_NAME:
                    return bool(entry.enabled)
            return False
        except Exception:
            return None
    return _read


def make_oauth_profile_reader() -> Callable[[str], "dict | None"]:
    """Build the Google OAuth profile reader (shared with GOG / Calendar)."""
    def _read(bot_id: str) -> "dict | None":
        auth = _default_read_auth_profiles(bot_id)
        profiles = auth.get("profiles") or {}
        profile_id = f"google_workspace:{bot_id}"
        return profiles.get(profile_id) or profiles.get("google_workspace")
    return _read


def make_oauth_client_reader(
    shared_dir: Path, bot_id: str | None = None,
) -> Callable[[], bool]:
    """Build a Google OAuth client presence checker.

    With ``bot_id`` (recommended): per-bot first
    (``network.bots[bot_id].googleOAuthClient``), legacy pod-level
    fallback (``network.googleOAuthClient``) for migration. Bots that
    have run ``evo setup-google`` get their own block; older single-bot
    pods that haven't migrated keep working via the fallback.

    Without ``bot_id`` (legacy): pod-level only.
    """
    def _check() -> bool:
        net = _default_read_network(shared_dir)
        if bot_id:
            bot_cfg = (net.get("bots") or {}).get(bot_id) or {}
            cfg = bot_cfg.get("googleOAuthClient") if isinstance(bot_cfg, dict) else None
            if isinstance(cfg, dict) and cfg.get("client_id"):
                return True
        legacy = net.get("googleOAuthClient")
        if isinstance(legacy, dict) and legacy.get("client_id"):
            return True
        return False
    return _check


# ── Provider implementation ───────────────────────────────────────────────────


def _gmail_is_satisfied(
    bot_id: str,
    *,
    read_plugin_enabled: "Callable[[str], bool | None] | None" = None,
    read_oauth_profile: "Callable[[str], dict | None] | None" = None,
    read_oauth_client_configured: "Callable[[], bool] | None" = None,
    shared_dir: "Path | None" = None,
) -> bool:
    """Return True if the bot has Gmail fully configured (plugin + OAuth profile)."""
    from ...skills.gmail_install import resolve_status

    _plugin_fn = read_plugin_enabled or make_plugin_enabled_reader()
    _profile_fn = read_oauth_profile or make_oauth_profile_reader()
    _client_fn = (
        read_oauth_client_configured
        or (make_oauth_client_reader(shared_dir, bot_id) if shared_dir else lambda: False)
    )

    try:
        status = resolve_status(
            bot_id,
            read_plugin_enabled=_plugin_fn,
            read_oauth_profile=_profile_fn,
            read_oauth_client_configured=_client_fn,
        )
        return status.status == "active"
    except Exception as exc:
        log.warning("gmail_provider.is_satisfied: check failed for %s: %s", bot_id, exc)
        return False


def _gmail_build_missing_item(
    bot_id: str,
    req: dict,
    *,
    read_plugin_enabled: "Callable[[str], bool | None] | None" = None,
    read_oauth_profile: "Callable[[str], dict | None] | None" = None,
    read_oauth_client_configured: "Callable[[], bool] | None" = None,
    shared_dir: "Path | None" = None,
) -> "dict | None":
    """Return a missing-item dict for Gmail, or None if satisfied."""
    from ...skills.gmail_install import (
        resolve_status,
        build_install_plan,
        GMAIL_SKILL_ID,
    )

    _plugin_fn = read_plugin_enabled or make_plugin_enabled_reader()
    _profile_fn = read_oauth_profile or make_oauth_profile_reader()
    _client_fn = (
        read_oauth_client_configured
        or (make_oauth_client_reader(shared_dir, bot_id) if shared_dir else lambda: False)
    )

    try:
        status = resolve_status(
            bot_id,
            read_plugin_enabled=_plugin_fn,
            read_oauth_profile=_profile_fn,
            read_oauth_client_configured=_client_fn,
        )
    except Exception as exc:
        log.warning("gmail_provider: status check failed for %s: %s", bot_id, exc)
        return {
            "integration_id": GMAIL_SKILL_ID,
            "skill_id": GMAIL_SKILL_ID,
            "display_name": req.get("display_name", "Gmail"),
            "reason": req.get("reason", "Gmail access required"),
            "status": "unknown",
            "action_url": "/api/skills/install/gmail",
            "action_label": "Set up Gmail",
            "install_plan_steps": [],
        }

    if status.status == "active":
        return None  # satisfied

    try:
        plan = build_install_plan(status)
        plan_steps = [s.to_dict() for s in plan]
    except Exception:
        plan_steps = []

    return {
        "integration_id": GMAIL_SKILL_ID,
        "skill_id": GMAIL_SKILL_ID,
        "display_name": req.get("display_name", "Gmail"),
        "reason": req.get("reason", "Gmail access required"),
        "status": status.status,
        "action_url": "/api/skills/install/gmail",
        "action_label": "Set up Gmail",
        "install_plan_steps": plan_steps,
    }


def _gmail_action_url(bot_id: str) -> str:
    return "/api/skills/install/gmail"


# ── Provider registration ─────────────────────────────────────────────────────

from . import PROVIDER_REGISTRY, Provider  # noqa: E402


def _build_provider() -> Provider:
    """Build the Gmail Provider instance with default (disk-read) callables."""
    return Provider(
        integration_ids=frozenset({"gmail"}),
        skill_id="gmail",
        is_satisfied=lambda bot_id: _gmail_is_satisfied(bot_id),
        build_missing_item=lambda bot_id, req: _gmail_build_missing_item(bot_id, req),
        action_url=_gmail_action_url,
        action_label="Set up Gmail",
    )


# Guard against double-registration if this module is somehow imported twice
if not any("gmail" in p.integration_ids for p in PROVIDER_REGISTRY):
    PROVIDER_REGISTRY.append(_build_provider())
