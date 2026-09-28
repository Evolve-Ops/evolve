"""Atlas — config loaders.

Reads:
- /Users/Shared/evolve/network.json  → pod-wide config, per-bot capabilities
- /Users/{bot_id}/.openclaw/openclaw.json → bot model + gateway + skill configs
- /Users/{bot_id}/.openclaw/skills/telegram.json → Telegram bot token
- workspace/atlas/sources.json → digest sources
- workspace/atlas/research-config.json → research limits/budget

LLM calls do NOT route through this module — see ``atlas_lib.oc_dispatch``.
Per the ``apps-inherit-bot-llm`` principle, Atlas has no per-app LLM
credentials; the bot's configured provider/model/caps govern every call.

All readers return empty defaults on failure (errors logged to stderr).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[atlas:config] {msg}", file=sys.stderr)


def workspace_root(bot_id: str) -> Path:
    return Path(f"/Users/{bot_id}/.openclaw/workspace")


def read_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"read failed: {path}: {exc}")
        return default


def write_json_atomic(path: Path, data) -> None:
    """Atomic write via temp-file + rename (no /tmp staging — workspace is bot-writable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    os.replace(tmp, path)


def network_config() -> dict:
    return read_json(Path("/Users/Shared/evolve/network.json"))


def bot_config(bot_id: str) -> dict:
    return read_json(Path(f"/Users/{bot_id}/.openclaw/openclaw.json"))


def telegram_token(bot_id: str) -> str:
    """Resolve Telegram bot token from the canonical OC skill file.

    Lookup order:
      1. ``~/.openclaw/skills/telegram.json :: bot_token`` (canonical —
         this is what ``openclaw skills install telegram`` writes).
      2. Same file's legacy ``token`` field.
      3. ``openclaw.json :: plugins.entries.telegram.token`` (forge-built
         bot pattern).
      4. ``openclaw.json :: integrations.telegram.token`` (older legacy).
      5. ``TELEGRAM_BOT_TOKEN`` env var.

    The 2026-06-04 Atlas Daily Digest incident surfaced that pre-fix
    this function read ``bot_token`` second instead of first, missing
    the canonical location on every fresh install — the cron would
    refuse to post even with a fully-configured Telegram skill.
    """
    skill_path = Path(f"/Users/{bot_id}/.openclaw/skills/telegram.json")
    if skill_path.exists():
        data = read_json(skill_path)
        if isinstance(data, dict):
            token = data.get("bot_token") or data.get("token") or ""
            if token:
                return token
    oc = bot_config(bot_id)
    return (
        oc.get("plugins", {}).get("entries", {}).get("telegram", {}).get("token")
        or oc.get("integrations", {}).get("telegram", {}).get("token")
        or oc.get("skills", {}).get("telegram", {}).get("token")
        or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        or ""
    )


def capability_config(bot_id: str, app_name: str) -> dict:
    """Read per-bot per-app config from network.json:
        bots[bot_id].capabilities[app_name]
    Returns {} if not configured.
    """
    net = network_config()
    return (
        net.get("bots", {})
        .get(bot_id, {})
        .get("capabilities", {})
        .get(app_name, {})
    )


def sources_config(bot_id: str) -> dict:
    """Read workspace/atlas/sources.json."""
    return read_json(workspace_root(bot_id) / "atlas" / "sources.json", default={
        "rss": [], "github_releases": [], "brave_queries": []
    })


def research_config(bot_id: str) -> dict:
    return read_json(workspace_root(bot_id) / "atlas" / "research-config.json", default={
        "per_member_rate": {"queries_per_hour": 3, "queries_per_day": 10},
        "pod_budget": {"max_cost_usd_per_day": 1.00, "max_queries_per_day": 100},
        "refusal_templates": {},
    })


def brave_api_key(bot_id: str) -> str:
    """Resolve Brave Search API key from the bot's openclaw.json or env.

    OpenClaw nests creds under ``plugins.entries.brave.config.webSearch.apiKey``
    (camelCase, two levels deep — separate from the plugin's top-level
    ``enabled`` flag). Pre-2026-06-05 this function only checked
    ``integrations.brave.api_key`` (flat, snake_case), which the forge
    install pattern never writes — silently skipping every Brave query
    even when the key was correctly configured on the bot.

    Lookup order:
      1. ``plugins.entries.brave.config.webSearch.apiKey`` (canonical OC)
      2. ``plugins.entries.brave.config.apiKey``           (older OC)
      3. ``plugins.entries.brave.apiKey``                  (legacy flat)
      4. ``plugins.entries.brave.api_key``                 (snake_case legacy)
      5. ``integrations.brave.api_key`` / ``.token``       (oldest legacy)
      6. ``BRAVE_API_KEY`` env var.
    """
    oc = bot_config(bot_id)
    brave = oc.get("plugins", {}).get("entries", {}).get("brave", {})
    brave_cfg = brave.get("config") if isinstance(brave.get("config"), dict) else {}
    web_search = brave_cfg.get("webSearch") if isinstance(brave_cfg.get("webSearch"), dict) else {}
    integ = oc.get("integrations", {}).get("brave", {})
    return (
        web_search.get("apiKey")
        or brave_cfg.get("apiKey")
        or brave.get("apiKey")
        or brave.get("api_key")
        or integ.get("api_key")
        or integ.get("token")
        or os.environ.get("BRAVE_API_KEY", "")
        or ""
    )


def github_token(bot_id: str) -> str:
    """Resolve GitHub token from the bot's openclaw.json or env.

    Same nesting pattern as :func:`brave_api_key` — OC stores creds
    under ``plugins.entries.github.config`` rather than at the top of
    the entry.
    """
    oc = bot_config(bot_id)
    gh = oc.get("plugins", {}).get("entries", {}).get("github", {})
    gh_cfg = gh.get("config") if isinstance(gh.get("config"), dict) else {}
    integ = oc.get("integrations", {}).get("github", {})
    return (
        gh_cfg.get("token")
        or gh_cfg.get("apiKey")
        or gh_cfg.get("api_key")
        or gh.get("token")
        or gh.get("apiKey")
        or gh.get("api_key")
        or integ.get("token")
        or os.environ.get("GITHUB_TOKEN", "")
        or ""
    )
