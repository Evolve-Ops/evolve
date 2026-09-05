"""``evo integrations`` — what's wired into this bot (or the pod).

Reads each bot's ``openclaw.json`` directly: ``channels.*`` for messaging
integrations (telegram/slack/discord/whatsapp), and ``plugins.entries[].name``
filtered against a known list of capability plugins (brave, github,
google, etc.). MCP servers also surfaced since they're an integration
flavor in practice.

We deliberately don't live-probe credentials here — that's slow and
duplicates the admin Integrations page. This handler answers "what is
this bot connected to", not "is each token valid right now". For health
of those integrations, ``evo alerts`` already surfaces failing probes
written by the security warden.

Scope: per-bot. The sysadmin ``evolve`` bot lists every bot's
integrations as a compact matrix.
"""

from __future__ import annotations

import json
import pwd
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import is_pod_wide_caller, pod_member_bots, speak


# Plugin names treated as "integrations" (capability providers, not core
# Anthropic/OpenAI LLM credentials). Mirrors the categories the admin
# Integrations page surfaces.
_INTEGRATION_PLUGINS = {
    "brave", "github", "google", "google_workspace", "discord",
    "telegram", "slack", "whatsapp", "linear", "notion", "perplexity",
    "tavily",
}


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    if is_pod_wide_caller(bot_id, network):
        body = _render_pod(network)
    else:
        body = _render_bot(bot_id, network)
    return speak("integrations", body, role)


def _render_bot(bot_id: str, network: dict[str, Any]) -> str:
    inv = _scan_bot(bot_id, network)
    if inv is None:
        return (
            f"**Integrations — {bot_id}**\n\n"
            "Couldn't read this bot's OpenClaw config. Ask your pod admin "
            "to redeploy if this keeps happening."
        )
    lines = [f"**Integrations — {bot_id}**", ""]
    if inv["channels"]:
        lines.append("Channels: " + ", ".join(sorted(inv["channels"])))
    else:
        lines.append("Channels: (none)")
    if inv["plugins"]:
        lines.append("Plugins:  " + ", ".join(sorted(inv["plugins"])))
    if inv["mcp"]:
        lines.append("MCP:      " + ", ".join(sorted(inv["mcp"])))
    return "\n".join(lines)


def _render_pod(network: dict[str, Any]) -> str:
    members = pod_member_bots(network)
    rows: list[tuple[str, dict]] = []
    for bot in members:
        inv = _scan_bot(bot, network)
        if inv is not None:
            rows.append((bot, inv))

    if not rows:
        return ("**Integrations — pod**\n\n"
                "Couldn't read any bot's OpenClaw config.")

    lines = ["**Integrations — pod**", ""]
    for bot, inv in rows:
        parts = []
        if inv["channels"]:
            parts.append("ch:" + "/".join(sorted(inv["channels"])))
        if inv["plugins"]:
            parts.append("ix:" + "/".join(sorted(inv["plugins"])))
        if inv["mcp"]:
            parts.append("mcp:" + "/".join(sorted(inv["mcp"])))
        line = f"{bot}: " + ("  ".join(parts) if parts else "(no integrations)")
        lines.append(line)
    return "\n".join(lines)


def _scan_bot(bot_id: str, network: dict[str, Any]) -> dict | None:
    """Return ``{channels, plugins, mcp}`` for ``bot_id`` or None if unreadable.

    Tries a direct read first (ACL grants evolve user read on ``.openclaw/``);
    falls back to ``sudo /bin/cat`` if the ACL hasn't been applied yet.
    """
    bot_user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
    try:
        home = Path(pwd.getpwnam(bot_user).pw_dir)
    except KeyError:
        home = Path(f"/Users/{bot_user}")
    path = home / ".openclaw" / "openclaw.json"

    text = _read_with_fallback(path)
    if text is None:
        return None
    try:
        oc = json.loads(text)
    except json.JSONDecodeError:
        return None

    channels_block = oc.get("channels") or {}
    channels = sorted(
        c for c in channels_block.keys()
        if isinstance(c, str) and c
    )

    plugin_entries = (oc.get("plugins") or {}).get("entries") or []
    plugin_names: set[str] = set()
    for entry in plugin_entries:
        if isinstance(entry, dict):
            name = entry.get("name")
        elif isinstance(entry, str):
            name = entry
        else:
            name = None
        if isinstance(name, str) and name in _INTEGRATION_PLUGINS:
            plugin_names.add(name)

    mcp_servers = (oc.get("mcp") or {}).get("servers") or {}
    if isinstance(mcp_servers, dict):
        mcp_names = sorted(mcp_servers.keys())
    elif isinstance(mcp_servers, list):
        mcp_names = sorted(
            s.get("name") for s in mcp_servers
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        )
    else:
        mcp_names = []

    return {
        "channels": channels,
        "plugins": sorted(plugin_names),
        "mcp": mcp_names,
    }


def _read_with_fallback(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        pass
    import subprocess
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None
