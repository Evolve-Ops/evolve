"""Read tool — pod-level network.json (summarized).

Exposes ``config.network`` — reads the pod's network.json and projects
to the operator-relevant fields. Secrets (Telegram chat IDs in alerts,
ssh tokens, primary-bot keys, etc.) are redacted.

The model uses this for pod-shape queries — what bots are configured,
who's primary, what's the shared_dir, what's the network identity.
For per-bot detail, the model should call ``config.bot`` instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _project_network(net: dict[str, Any]) -> dict[str, Any]:
    """Pick the model-relevant fields from network.json.

    Keeps:
      - identity (networkId, sharedDir, primary)
      - membership (members list)
      - per-bot static config (role, port, user) — NOT secrets
      - pod_report thresholds (operator-facing tuning surface)
      - home_chat config — surfaces the daily-cap + model knobs
      - alerts config (channel, but NOT chatId)
      - operational module flags

    Drops / redacts:
      - alerts.chatId (specific Telegram chat — operator privacy)
      - any per-bot `googleOAuthClient` (client_secret lives in bot's
        auth-profiles.json which we never read; client_id is OK)
      - any nested token / key / secret fields

    Whole-shape walk would catch arbitrary secrets, but the network.json
    schema is pretty narrow. We hand-pick what's allowed; everything
    else is dropped to avoid accidental leakage of fields added later.
    """
    out: dict[str, Any] = {
        "network_id": net.get("networkId"),
        "shared_dir": net.get("sharedDir"),
        "primary": net.get("primary"),
        "members": list(net.get("members") or []),
    }

    # Per-bot static config — role + port only. user gets the macOS
    # account name which is fine to surface; we keep it because the
    # model occasionally needs to know "team_bot_a lives on the team_bot_a_bot
    # account" for filesystem-path reasoning.
    bots_raw = net.get("bots") or {}
    out["bots"] = {
        bid: {
            "role": (cfg or {}).get("role"),
            "port": (cfg or {}).get("port"),
            "user": (cfg or {}).get("user"),
            # googleOAuthClient — keep just client_id (public),
            # drop client_secret if anyone ever stashed one here.
            **(
                {"google_oauth_client_id": (cfg or {}).get("googleOAuthClient", {}).get("client_id")}
                if isinstance(cfg, dict) and (cfg.get("googleOAuthClient") or {}).get("client_id")
                else {}
            ),
        }
        for bid, cfg in bots_raw.items()
        if isinstance(cfg, dict)
    }

    # Operational flags / tuning surfaces — non-secret config.
    if net.get("pod_report"):
        pod_report = net.get("pod_report") or {}
        out["pod_report"] = {
            "thresholds": pod_report.get("thresholds") or {},
        }
    if net.get("home_chat"):
        hc = net.get("home_chat") or {}
        out["home_chat"] = {
            "daily_cap": hc.get("daily_cap"),
            "model": hc.get("model"),
            "max_history_turns": hc.get("max_history_turns"),
            # narrative_model + narrative_cache_ttl_seconds visible too
            "narrative_model": hc.get("narrative_model"),
            "narrative_cache_ttl_seconds": hc.get("narrative_cache_ttl_seconds"),
        }
    # Alerts channel name only — never chatId. The presence of an
    # alerts block tells the model "alerts are routed somewhere," not
    # where exactly.
    alerts = net.get("alerts") or {}
    if alerts:
        out["alerts"] = {
            "channel": alerts.get("channel"),
            "chat_configured": bool(alerts.get("chatId")),
        }

    # Security bot reference, if present (this is a network-level
    # pointer, not a secret).
    if net.get("security_bot"):
        sb = net.get("security_bot") or {}
        out["security_bot"] = {"bot": sb.get("bot")}

    return out


def _handler(network_path: Path) -> dict[str, Any]:
    """Read network.json and project."""
    try:
        from evolve_admin.config import load_network
    except ImportError as exc:
        log.warning("config.network: load_network unavailable: %s", exc)
        return {"available": False, "error": f"config module unavailable: {exc}"}

    try:
        net = load_network(network_path)
    except FileNotFoundError:
        return {
            "available": False,
            "error": f"network.json not found at {network_path}",
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("config.network: load_network failed")
        return {
            "available": False,
            "error": f"failed to load network.json: {exc}",
        }

    return {
        "available": True,
        "config": _project_network(net),
    }


TOOL = Tool(
    name="config.network",
    description=(
        "Read the pod's network.json, summarized. Returns network "
        "identity (networkId, sharedDir, primary bot id), membership "
        "list, per-bot static config (role, port, user — no secrets), "
        "operational tuning surfaces (pod_report thresholds, "
        "home_chat config, alerts channel), and security_bot pointer. "
        "Secrets are redacted: alerts.chatId surfaces as "
        "'chat_configured: true/false' instead of the value; OAuth "
        "client secrets are never returned. Use this for "
        "'who's primary?', 'what bots exist?', 'is X tuning enabled?', "
        "or before recommending a network-level config change."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("config", "network"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(TOOL)
