"""Read tool — a bot's openclaw.json (summarized).

Exposes ``config.bot`` — reads one bot's ``/Users/<bot>/.openclaw/openclaw.json``
and projects to the operator-relevant fields. Secrets (Telegram bot
tokens, Slack signing secrets, OAuth client secrets, gateway auth
tokens) are redacted from the projection — the model gets enough to
reason about configuration shape without ever holding a credential.

Per the codebase CLAUDE.md guidance, the evolve user has macOS ACL
read access to every bot's .openclaw/ via ``set_evolve_read_acl``.
Direct read works in normal cases. Fallback to ``sudo /bin/cat`` covers
bots not yet fully provisioned through the new path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register
from .fs_tristate import ReadResult, ReadState, read_json

log = logging.getLogger(__name__)


# Field paths that get redacted from the projection. These are
# secrets the model has no use seeing — the model can ask "is X
# configured?" and we'll answer with a boolean, never the value.
# Path notation: dot-separated keys. Wildcard "*" matches any single
# key (used inside the channels.* and plugins.entries.* maps).
_REDACT_PATHS = (
    "gateway.auth.token",
    "channels.telegram.botToken",
    "channels.slack.signingSecret",
    "channels.slack.botToken",
    "channels.discord.botToken",
    "plugins.entries.*.config.api_key",
    "plugins.entries.*.config.apiKey",
    "plugins.entries.*.config.token",
    "plugins.entries.*.config.secret",
    "auth.*.api_key",
    "auth.*.apiKey",
    "auth.*.token",
    "auth.*.secret",
)


def _redact(value: Any) -> str:
    """Replace a secret with a sentinel that confirms presence + length
    without revealing content. Lets the model say 'X is set (24-char
    token)' rather than guessing whether the field exists at all."""
    if value is None:
        return "[unset]"
    if isinstance(value, str):
        return f"[redacted: {len(value)}-char secret]"
    return "[redacted]"


def _walk_redact(node: Any, path_parts: tuple[str, ...] = ()) -> Any:
    """Recursively walk the openclaw config and redact secrets in place.

    Matches each leaf path against the _REDACT_PATHS patterns (with "*"
    wildcards for dict-key positions). Returns a deep-projected copy.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            new_path = path_parts + (k,)
            if _is_redacted_path(new_path):
                out[k] = _redact(v)
            else:
                out[k] = _walk_redact(v, new_path)
        return out
    if isinstance(node, list):
        return [_walk_redact(item, path_parts) for item in node]
    return node


def _is_redacted_path(parts: tuple[str, ...]) -> bool:
    """Does the dotted path match any _REDACT_PATHS pattern?

    "*" in the pattern matches any single key. Pattern length must
    equal parts length for a match.
    """
    parts_lower = tuple(p.lower() for p in parts)
    for pat in _REDACT_PATHS:
        pat_parts = pat.split(".")
        if len(pat_parts) != len(parts_lower):
            continue
        match = True
        for pat_p, real_p in zip(pat_parts, parts_lower):
            if pat_p == "*":
                continue
            if pat_p.lower() != real_p:
                match = False
                break
        if match:
            return True
    return False


def _read_openclaw_json(bot_id: str, network_path: Path) -> "tuple[ReadResult, dict[str, Any] | None]":
    """Read a bot's openclaw.json TRI-STATE, with a sudo /bin/cat fallback.

    Returns ``(ReadResult, parsed | None)``. The ``ReadResult.state`` is
    authoritative: ``exists`` (parsed config follows), ``absent`` (the file
    genuinely isn't there — ENOENT), or ``indeterminate`` (a permission
    wall — EACCES/EPERM — or a missing sudoers grant; we CANNOT say whether
    the file exists). Never folds a permission denial into "absent": that
    conflation is exactly the ``exists() lies under 0700 parents`` bug that
    led Evo to confabulate "the file doesn't exist yet" for a 0700-shielded
    file that was actually present.

    The evolve user has ACL read on every bot's .openclaw/ via deploy.py's
    set_evolve_read_acl; the sudo fallback handles fresh-install or
    hand-modified ACL cases (and a missing grant surfaces as
    ``indeterminate``, not silent absence).
    """
    try:
        from evolve_admin.config import bot_home, load_network
    except ImportError as exc:
        log.warning("config.bot: config module unavailable: %s", exc)
        return ReadResult(ReadState.INDETERMINATE, None, f"config module unavailable: {exc}"), None

    try:
        network = load_network(network_path)
        bot_home_path = bot_home(bot_id, network)
        oc_json_path = bot_home_path / ".openclaw" / "openclaw.json"
    except Exception as exc:  # noqa: BLE001
        log.warning("config.bot: failed to resolve bot home for %s: %s", bot_id, exc)
        return ReadResult(ReadState.INDETERMINATE, None, f"could not resolve bot home: {exc}"), None

    return read_json(oc_json_path)


def _project_bot_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Pick the operator-relevant fields, redact secrets.

    Picks:
      - gateway shape: port, bind, auth mode (token value redacted)
      - agents.defaults headline: primary model + fallbacks, tier hints
        (shorter than the full dict), context controls (heartbeat,
        compaction reserve, contextPruning mode + ttl, bootstrap caps)
      - channels: per-channel enabled/policy/streaming (no tokens)
      - plugins: enabled plugin names + their non-secret config
      - tools.exec: security + ask mode (real values; not secrets)
      - tools.web: search/fetch enabled bits
      - commands: native + nativeSkills + ownerAllowFrom (channel IDs
        kept because they're not credentials — sender IDs, like phone
        numbers, vary by sensitivity expectation; flag for follow-up)

    Drops:
      - gateway.auth.token (redacted in walk)
      - everything under wizard / state / cron internals
      - bootstrapTotalMaxChars values when not interesting (kept for
        the model — context budgeting matters)
    """
    # Walk-redact the whole config first; then project.
    redacted = _walk_redact(cfg)

    out: dict[str, Any] = {}

    gw = redacted.get("gateway") or {}
    out["gateway"] = {
        "port": gw.get("port"),
        "bind": gw.get("bind"),
        "auth_mode": (gw.get("auth") or {}).get("mode"),
        "mode": gw.get("mode"),
    }

    agents_def = (redacted.get("agents") or {}).get("defaults") or {}
    model = agents_def.get("model") or {}
    out["agents"] = {
        "model_primary": model.get("primary"),
        "model_fallbacks": list(model.get("fallbacks") or []),
        "thinking_default": agents_def.get("thinkingDefault"),
        "heartbeat": agents_def.get("heartbeat"),
        "context_pruning": agents_def.get("contextPruning"),
        "compaction": agents_def.get("compaction"),
        "bootstrap_total_max_chars": agents_def.get("bootstrapTotalMaxChars"),
    }

    channels_raw = redacted.get("channels") or {}
    out["channels"] = {
        name: {
            "enabled": (cfg or {}).get("enabled"),
            "dm_policy": (cfg or {}).get("dmPolicy"),
            "group_policy": (cfg or {}).get("groupPolicy"),
            "streaming_mode": ((cfg or {}).get("streaming") or {}).get("mode"),
            # botToken is already redacted; show the sentinel so the
            # model knows the channel IS configured.
            "auth_marker": (cfg or {}).get("botToken")
            or (cfg or {}).get("signingSecret")
            or "[no secret]",
        }
        for name, cfg in channels_raw.items()
        if isinstance(cfg, dict)
    }

    plugins_raw = (redacted.get("plugins") or {}).get("entries") or {}
    out["plugins"] = {
        name: {
            "enabled": bool((entry or {}).get("enabled")),
            "version": (entry or {}).get("version"),
        }
        for name, entry in plugins_raw.items()
        if isinstance(entry, dict)
    }

    tools_section = redacted.get("tools") or {}
    out["tools"] = {
        "exec_security": (tools_section.get("exec") or {}).get("security"),
        "exec_ask": (tools_section.get("exec") or {}).get("ask"),
        "web_search_enabled": (
            (tools_section.get("web") or {}).get("search") or {}
        ).get("enabled"),
        "web_fetch_enabled": (
            (tools_section.get("web") or {}).get("fetch") or {}
        ).get("enabled"),
    }

    commands = redacted.get("commands") or {}
    out["commands"] = {
        "native": commands.get("native"),
        "nativeSkills": commands.get("nativeSkills"),
        # ownerAllowFrom is a list of channel-scoped sender ids (like
        # "telegram:123456789"). Not strictly a secret, but high-PII
        # signal. Keep it — the model needs to know who's allowed —
        # but if we ever decide it shouldn't surface, add the path to
        # _REDACT_PATHS.
        "owner_allow_from": commands.get("ownerAllowFrom"),
    }

    return out


def _handler(
    network_path: Path,
    bot_id: str,
) -> dict[str, Any]:
    """Read and project one bot's openclaw.json.

    Required arg: ``bot_id``. The model is expected to know which bot it
    cares about (likely from a prior ``pod_state.bots`` call). Returns
    {error: ...} when the bot or its config can't be located.

    Phase E.3.1 path: first try the admin daemon's unix-socket endpoint
    so the read goes through the trusted admin daemon (which retains
    cross-bot ACL access post-Phase-E.2.b). Falls back to the legacy
    direct-fs path on socket failure — this preserves operation during
    the migration window where evo still runs as the ``evolve`` user
    and CAN do the direct read, but lets us start exercising the new
    path immediately.

    Once Phase E.2.b cuts over (evo runs as the ``evo`` user with no
    cross-bot ACLs), the direct-fs fallback returns None for non-self
    bots and the admin daemon path becomes the only working one. The
    fallback then quietly becomes dead code; the operator never
    notices the transition.
    """
    if not bot_id:
        return {"error": "bot_id is required"}

    # Primary path: HTTP over the admin daemon's unix socket.
    try:
        from ..admin_client import AdminDaemonUnavailable, get_json
        status, body = get_json(f"/api/admin/bot/{bot_id}/openclaw-config")
        if status == 200 and isinstance(body, dict):
            # Admin daemon returns the same shape this tool used to.
            return body
        if status == 404 and isinstance(body, dict):
            return body  # bot not found / no read access — same shape
        # Other status codes (403 etc.) fall through to the fs fallback;
        # log so a regression surfaces in evo's gateway log.
        log.warning(
            "config.bot: admin daemon returned status %d for bot %s; "
            "falling back to direct-fs read",
            status, bot_id,
        )
    except AdminDaemonUnavailable as exc:
        # WARNING, not DEBUG — mute fallbacks hid the 2026-07-28 outage
        # for 10 days. log_daemon_fallback carries socket path + errno.
        from ..admin_client import log_daemon_fallback
        log_daemon_fallback(
            f"config.bot GET /api/admin/bot/{bot_id}/openclaw-config", exc,
        )
    except Exception as exc:  # noqa: BLE001 — never let one bad call torpedo the tool
        log.warning("config.bot: admin daemon call raised: %s", exc)

    # Fallback path: legacy direct-fs read — now tri-state so a permission
    # wall is never reported as "not found". The model must be able to tell
    # "this bot has no openclaw.json" (absent) from "I cannot read it"
    # (indeterminate) — the latter is a tooling/ACL problem to surface, not
    # a fact about the bot's config.
    read_res, cfg = _read_openclaw_json(bot_id, network_path)
    if read_res.state is ReadState.ABSENT:
        return {
            "bot_id": bot_id,
            "available": False,
            "read_state": read_res.state.value,
            "error": f"openclaw.json not found for {bot_id} (file is genuinely absent)",
        }
    if read_res.state is ReadState.EXISTS and cfg is None:
        # File was readable but did not parse — distinct from a permission
        # wall. Report it as present-but-malformed, not "read denied".
        return {
            "bot_id": bot_id,
            "available": False,
            "read_state": ReadState.EXISTS.value,
            "error": (
                f"{bot_id}'s openclaw.json exists but could not be parsed. "
                f"Detail: {read_res.detail}"
            ),
        }
    if read_res.state is ReadState.INDETERMINATE or cfg is None:
        return {
            "bot_id": bot_id,
            "available": False,
            "read_state": ReadState.INDETERMINATE.value,
            "error": (
                f"could NOT determine {bot_id}'s openclaw.json — read denied "
                f"(permission), not confirmed absent. Do not conclude the file "
                f"is missing. Detail: {read_res.detail}"
            ),
        }

    return {
        "bot_id": bot_id,
        "available": True,
        "read_state": read_res.state.value,
        "config": _project_bot_config(cfg),
    }


TOOL = Tool(
    name="config.bot",
    description=(
        "Read a bot's openclaw.json, summarized. Returns gateway shape "
        "(port, bind, auth mode), agents defaults (primary model + "
        "fallbacks, heartbeat, compaction, context pruning), channels "
        "(enabled, policies, no tokens — secrets are redacted to "
        "sentinels confirming presence + length without revealing "
        "content), plugins enabled, tools.exec security mode, "
        "tools.web feature flags, and commands native/skills settings. "
        "Secrets are always redacted in the returned data. Use this "
        "to answer 'what model is X running?', 'is Slack configured "
        "on Y?', 'what's Z's exec policy?', or before recommending a "
        "config change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Bot to read config for (e.g. 'personal_bot', 'team_bot_a', "
                    "'evolve'). Required."
                ),
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("config", "bot"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(TOOL)
