"""mcp.inventory — read a bot's MCP server configuration from openclaw.json.

Spec: internal/spec-mcp-administration-2026-05-10.md §3.3 (Inventory).

The inventory is the observed state. The monitor (mcp.monitor) compares
it against the pod-wide allowlist and emits signals on drift. The
admin UI also reads inventory files directly to render the matrix.

Schema reference: docs/schemas/oc-config-schema.txt:40235 (mcp.servers).
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
class ServerEntry:
    """One MCP server as configured on a bot."""

    name: str
    kind: str  # "stdio" | "http" | "unknown"
    command: str | None
    args: list[str]
    url: str | None
    env_keys: list[str]
    config_signature: str  # sha256 over (command, args, url, env keys); excludes env *values*

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Inventory:
    """Snapshot of a bot's MCP configuration at a point in time."""

    bot_id: str
    observed_at: str  # ISO-8601 UTC
    openclaw_config_path: str
    openclaw_config_present: bool
    servers: list[ServerEntry] = field(default_factory=list)
    self_mutation_commands_mcp: bool = False
    self_mutation_commands_plugins: bool = False
    # If we couldn't read or parse the file, this is the reason. When set,
    # `servers` is empty and `openclaw_config_present` reflects whether the
    # file exists (vs. exists-but-unreadable).
    read_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["servers"] = [s.to_dict() if isinstance(s, ServerEntry) else s for s in self.servers]
        return d


# ── Reader ────────────────────────────────────────────────────────────────────

def _read_openclaw_json(path: Path, *, timeout: float = 5.0) -> tuple[dict | None, str | None]:
    """Read openclaw.json via direct read, falling back to sudo /bin/cat.

    Returns (config_dict, error_str). On success, error_str is None. On
    failure, config_dict is None and error_str describes why.

    Pattern matches CLAUDE.md guidance: direct read first, sudo /bin/cat
    fallback for PermissionError.
    """
    # Direct read first
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "not_found"
    except PermissionError:
        text = None
    except OSError as exc:
        return None, f"os_error: {exc}"

    if text is None:
        # Fallback: sudo /bin/cat
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


def _signature_for_server(name: str, cfg: dict[str, Any]) -> str:
    """Stable sha256 over the server's launch shape, excluding env values.

    Spec §8.2: signature includes (command, args sorted, url, env keys
    sorted, cwd) — values omitted so credential rotation doesn't trip
    drift signals.
    """
    canonical = {
        "name": name,
        "command": cfg.get("command"),
        "args": sorted(cfg.get("args") or []),
        "url": cfg.get("url"),
        "env_keys": sorted((cfg.get("env") or {}).keys()),
        "cwd": cfg.get("cwd") or cfg.get("workingDirectory"),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _classify_server(cfg: dict[str, Any]) -> str:
    """Classify transport kind from the OC schema's permitted fields."""
    if cfg.get("url"):
        return "http"
    if cfg.get("command"):
        return "stdio"
    return "unknown"


def read_inventory(
    bot_id: str,
    config: "dict[str, Any] | None" = None,
) -> Inventory:
    """Read a bot's MCP inventory from its openclaw.json.

    Returns an Inventory even on read failure — `read_error` and
    `openclaw_config_present` together describe the failure mode for the
    monitor's missing-config signal.
    """
    home = bot_home(bot_id, config)
    oc_path = home / ".openclaw" / "openclaw.json"

    inv = Inventory(
        bot_id=bot_id,
        observed_at=_utc_now_iso(),
        openclaw_config_path=str(oc_path),
        openclaw_config_present=oc_path.exists(),
    )

    oc, err = _read_openclaw_json(oc_path)
    if oc is None:
        inv.read_error = err
        return inv

    mcp_block = (oc.get("mcp") or {}).get("servers") or {}
    for name, cfg in mcp_block.items():
        if not isinstance(cfg, dict):
            continue
        inv.servers.append(
            ServerEntry(
                name=name,
                kind=_classify_server(cfg),
                command=cfg.get("command"),
                args=list(cfg.get("args") or []),
                url=cfg.get("url"),
                env_keys=sorted((cfg.get("env") or {}).keys()),
                config_signature=_signature_for_server(name, cfg),
            )
        )

    commands = oc.get("commands") or {}
    inv.self_mutation_commands_mcp = bool(commands.get("mcp"))
    inv.self_mutation_commands_plugins = bool(commands.get("plugins"))

    return inv


# ── On-disk cache ─────────────────────────────────────────────────────────────

def inventory_dir(shared_dir: Path) -> Path:
    return shared_dir / "mcp" / "inventory"


def inventory_path(shared_dir: Path, bot_id: str) -> Path:
    return inventory_dir(shared_dir) / f"{bot_id}.json"


def write_inventory(inv: Inventory, shared_dir: Path) -> None:
    """Atomically write the inventory to {shared_dir}/mcp/inventory/<bot>.json."""
    target = inventory_path(shared_dir, inv.bot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(inv.to_dict(), indent=2, sort_keys=True))
    tmp.replace(target)


def load_inventory(shared_dir: Path, bot_id: str) -> dict | None:
    """Load a cached inventory; return None if absent."""
    p = inventory_path(shared_dir, bot_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
