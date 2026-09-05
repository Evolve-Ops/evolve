"""hooks.inventory — read a bot's hook configuration from openclaw.json.

Spec: internal/spec-hook-governance-2026-05-10.md §3.2 (HookInventory).

Two surfaces in one inventory record:

  webhook_ingress — captures the openclaw.json hooks{} block. The
    block_signature hashes the field shape (enabled, path,
    allowedAgentIds, allowedSessionKeyPrefixes, mappings,
    transformsDir) but excludes the bearer ``token`` so rotation
    doesn't trip drift signals.

  plugin_typed_hooks — per-plugin hook-policy snapshot. We track
    every plugin entry (even disabled ones) because a flipped flag
    on a disabled plugin is a pre-positioning indicator, not a
    benign change.

The inventory file lands under ``{shared_dir}/hooks/inventory/<bot>.json``
so the admin UI can render the matrix without re-reading every
bot's openclaw.json each request.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_config import bot_home
from evolve_util import now_iso as _utc_now_iso


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class WebhookIngress:
    """Snapshot of the bot's openclaw.json top-level ``hooks`` block."""

    configured: bool  # whether the top-level hooks key exists at all
    enabled: bool  # hooks.enabled (false-by-default, but only meaningful when configured)
    block_signature: str | None  # sha256 over shape (sans token); None when not configured
    path: str | None = None
    allowed_agent_ids: list[str] = field(default_factory=list)
    allowed_session_key_prefixes: list[str] = field(default_factory=list)
    mapping_count: int = 0
    transforms_dir: str | None = None
    has_token: bool = False  # whether a token field is present (not its value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PluginHookPolicy:
    """Per-plugin typed-hook policy snapshot."""

    plugin_name: str
    plugin_enabled: bool
    allow_conversation_access: bool
    allow_prompt_injection: bool
    policy_signature: str  # sha256 over the two flags only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HookInventory:
    """Snapshot of a bot's full hook configuration at a point in time."""

    bot_id: str
    observed_at: str
    openclaw_config_path: str
    openclaw_config_present: bool
    webhook_ingress: WebhookIngress | None = None
    plugin_policies: list[PluginHookPolicy] = field(default_factory=list)
    self_mutation_commands_plugins: bool = False
    self_mutation_commands_mcp: bool = False  # included for the cross-surface signal
    read_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.webhook_ingress is not None:
            d["webhook_ingress"] = self.webhook_ingress.to_dict()
        d["plugin_policies"] = [
            p.to_dict() if isinstance(p, PluginHookPolicy) else p for p in self.plugin_policies
        ]
        return d


# ── Reader ────────────────────────────────────────────────────────────────────

def _read_openclaw_json(path: Path, *, timeout: float = 5.0) -> tuple[dict | None, str | None]:
    """Direct read first, sudo /bin/cat fallback. Same pattern as the other
    inventory modules under packages/analyzer/."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        text = None
    except OSError as exc:
        return None, f"os_error: {exc}"
    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError as exc:
            return None, f"sudo_error: {exc}"
        if r.returncode != 0:
            return None, f"sudo_rc={r.returncode}"
        text = r.stdout
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


def _webhook_signature(hooks_block: dict[str, Any]) -> str:
    """Hash the shape of an ingress block, excluding the bearer ``token``.

    Spec §8.1: signature stability — token rotation should not trip
    drift signals. Other fields (path, allowed lists, mappings,
    transformsDir) ARE included so any meaningful shape change fires
    hook_webhook_mapping_changed.
    """
    canonical = {
        "enabled": bool(hooks_block.get("enabled", False)),
        "path": hooks_block.get("path"),
        "defaultSessionKey": hooks_block.get("defaultSessionKey"),
        "allowRequestSessionKey": bool(hooks_block.get("allowRequestSessionKey", False)),
        "allowedSessionKeyPrefixes": sorted(hooks_block.get("allowedSessionKeyPrefixes") or []),
        "allowedAgentIds": sorted(hooks_block.get("allowedAgentIds") or []),
        "maxBodyBytes": hooks_block.get("maxBodyBytes"),
        "presets": sorted(hooks_block.get("presets") or []),
        "transformsDir": hooks_block.get("transformsDir"),
        # Mappings: hash canonical form with mapping id as the sort key
        "mappings": sorted(
            [
                {
                    "id": m.get("id", ""),
                    "match": m.get("match") or {},
                    "action": m.get("action"),
                }
                for m in (hooks_block.get("mappings") or [])
                if isinstance(m, dict)
            ],
            key=lambda m: m.get("id") or json.dumps(m, sort_keys=True),
        ),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _policy_signature(allow_conversation_access: bool, allow_prompt_injection: bool) -> str:
    canonical = {
        "allowConversationAccess": allow_conversation_access,
        "allowPromptInjection": allow_prompt_injection,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def read_inventory(bot_id: str, config: "dict[str, Any] | None" = None) -> HookInventory:
    """Read a bot's hook inventory from its openclaw.json."""
    home = bot_home(bot_id, config)
    oc_path = home / ".openclaw" / "openclaw.json"

    inv = HookInventory(
        bot_id=bot_id,
        observed_at=_utc_now_iso(),
        openclaw_config_path=str(oc_path),
        openclaw_config_present=oc_path.exists(),
    )
    oc, err = _read_openclaw_json(oc_path)
    if oc is None:
        inv.read_error = err
        return inv

    # ── Webhook ingress block ────────────────────────────────────────────────
    raw_hooks = oc.get("hooks")
    if isinstance(raw_hooks, dict):
        inv.webhook_ingress = WebhookIngress(
            configured=True,
            enabled=bool(raw_hooks.get("enabled", False)),
            block_signature=_webhook_signature(raw_hooks),
            path=raw_hooks.get("path"),
            allowed_agent_ids=list(raw_hooks.get("allowedAgentIds") or []),
            allowed_session_key_prefixes=list(raw_hooks.get("allowedSessionKeyPrefixes") or []),
            mapping_count=len(raw_hooks.get("mappings") or []),
            transforms_dir=raw_hooks.get("transformsDir"),
            has_token=bool(raw_hooks.get("token")),
        )
    else:
        inv.webhook_ingress = WebhookIngress(
            configured=False, enabled=False, block_signature=None,
        )

    # ── Per-plugin hook policies ─────────────────────────────────────────────
    plugins = oc.get("plugins") or {}
    entries = plugins.get("entries") or {}
    for name, cfg in sorted(entries.items()):
        if not isinstance(cfg, dict):
            continue
        # plugin is enabled unless explicitly disabled (matches plugins.inventory
        # semantics — None or True → enabled)
        is_enabled = cfg.get("enabled") is not False
        hooks_policy = cfg.get("hooks") or {}
        allow_conv = bool(hooks_policy.get("allowConversationAccess", False))
        allow_inj = bool(hooks_policy.get("allowPromptInjection", False))
        inv.plugin_policies.append(
            PluginHookPolicy(
                plugin_name=name,
                plugin_enabled=is_enabled,
                allow_conversation_access=allow_conv,
                allow_prompt_injection=allow_inj,
                policy_signature=_policy_signature(allow_conv, allow_inj),
            )
        )

    commands = oc.get("commands") or {}
    inv.self_mutation_commands_plugins = bool(commands.get("plugins"))
    inv.self_mutation_commands_mcp = bool(commands.get("mcp"))

    return inv


# ── On-disk cache ─────────────────────────────────────────────────────────────

def inventory_dir(shared_dir: Path) -> Path:
    return shared_dir / "hooks" / "inventory"


def inventory_path(shared_dir: Path, bot_id: str) -> Path:
    return inventory_dir(shared_dir) / f"{bot_id}.json"


def write_inventory(inv: HookInventory, shared_dir: Path) -> None:
    target = inventory_path(shared_dir, inv.bot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(inv.to_dict(), indent=2, sort_keys=True))
    tmp.replace(target)


def load_inventory(shared_dir: Path, bot_id: str) -> dict | None:
    p = inventory_path(shared_dir, bot_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
