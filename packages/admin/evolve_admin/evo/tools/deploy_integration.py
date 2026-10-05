"""Phase 2.1 — deploy hook that registers the evo Tool registry with OC.

Wires the MCP stdio bridge (`mcp_server.py` + `__main__.py`) into evo's
``openclaw.json`` under ``mcp.servers.evo_tools`` so the OC gateway
spawns it on session start and the model can call ``pod_state.signals.firing``,
``action.signal.snooze``, etc. via the standard MCP tool-use path.

What this module is:

* A small, idempotent gap-fill that runs alongside ``ensure_plugin_config``
  during ``deploy_bot``. Confined to the primary bot — the registry is
  evo's direct-access surface (spec §1.3). Member bots get tools via the
  ``evo`` keyword plugin dispatcher; they MUST NOT see the registry.

* A CLI helper used by ``evolve-admin sync-evo-tools`` to push registry
  changes (new tools added in a code update) to evo without a full
  redeploy. The full deploy pulls the same way; this is just the fast
  path for the "I added a tool, restart the gateway" loop.

What this module is NOT:

* It does not write the manifest into evo's MEMORY/AGENTS/TOOLS files
  — those land in Phase 3 (content templates).

* It does not write per-bot MCP server entries for member bots — only
  the primary bot owns this surface. Member-bot MCP installs are a
  separate concern handled by ``arbiter.appliers.mcp_server`` for
  catalog-driven installs.

Server-block shape (matches the existing OC mcp.servers convention used
by arbiter.appliers.mcp_server and mcp_admin.launcher):

    {
      "mcp": {
        "servers": {
          "evo_tools": {
            "command": "/Users/Shared/evolve-venv/bin/python3",
            "args": ["-m", "evolve_admin.evo.tools"],
            "env": {
              "EVOLVE_SHARED_DIR": "<network.sharedDir>",
              "EVOLVE_NETWORK_PATH": "<path to network.json>"
            }
          }
        }
      }
    }

The env vars line up with ``mcp_server._default_shared_dir`` /
``_default_network_path`` — the bridge falls back to ``/Users/Shared/evolve``
when neither is set, but we always set them explicitly for transparency
in evo's config and to keep test pods on a non-default sharedDir working.

Spec references: internal/spec-evo-oc-native-2026-05-19.md §2.4 (MCP bridge)
and §7 Phase 2 (deploy hook).
"""

from __future__ import annotations

import json
import logging
import subprocess

from platform_profile import get_profile
from copy import deepcopy
from pathlib import Path
from typing import Any

from evolve_admin.config import get_bot_user as _bot_user_for

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# OC config key for this server. Stable; downstream tooling
# (admin UI's MCP inventory, audit, etc.) keys off this exact name to
# distinguish the evo-tool bridge from operator-installed MCP servers.
EVO_TOOLS_SERVER_ID = "evo_tools"

# Python interpreter for the bridge. Same as deploy.VENV_PYTHON — the
# evolve-managed venv that has the mcp SDK installed. Hardcoded rather
# than imported from deploy.py to keep this module loadable from contexts
# where the deploy module's CLI-discovery code isn't safe to import
# (e.g. unit tests run on a laptop without a real /Users/Shared/evolve-venv).
DEFAULT_VENV_PYTHON = "/Users/Shared/evolve-venv/bin/python3"


def build_server_block(
    *,
    venv_python: str,
    shared_dir: str,
    network_path: str,
) -> dict[str, Any]:
    """Construct the canonical mcp.servers.evo_tools block.

    Separated out so tests can call it directly without exercising the
    file I/O path. Pure function — same inputs always produce the same
    block, byte-for-byte (key order matters: a deepcopy + dict-equality
    check is the idempotency mechanism, so block shape must be stable).
    """
    return {
        "command": venv_python,
        "args": ["-m", "evolve_admin.evo.tools"],
        "env": {
            "EVOLVE_SHARED_DIR": shared_dir,
            "EVOLVE_NETWORK_PATH": network_path,
        },
    }


# ── openclaw.json read/write ─────────────────────────────────────────────────

def _read_bot_oc_config(bot_id: str, bot_user: str) -> tuple[dict | None, str | None]:
    """Read /Users/<bot_user>/.openclaw/openclaw.json.

    Mirrors deploy.ensure_plugin_config's direct-first-then-sudo pattern:
    the evolve user gets ACL read on .openclaw/ via set_evolve_read_acl,
    but pre-ACL bots fall through to sudo /bin/cat. Returns
    ``(config, None)`` on success or ``(None, error_str)`` otherwise.
    """
    oc_json = Path(f"/Users/{bot_user}/.openclaw/openclaw.json")
    direct_err: str | None = None
    content: str | None = None
    try:
        content = oc_json.read_text()
    except FileNotFoundError:
        direct_err = "FileNotFoundError"
    except PermissionError:
        direct_err = "PermissionError"
    except OSError as exc:
        direct_err = f"{type(exc).__name__}: {exc}"

    if content is None:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(oc_json)],
                capture_output=True, text=True, timeout=10,
                cwd=get_profile().scratch_dir,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, f"sudo_cat failed: {exc}"
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            if "No such file" in stderr:
                return None, f"{oc_json} not found"
            return None, f"sudo_cat rc={r.returncode}: {stderr or direct_err or 'empty'}"
        content = r.stdout

    try:
        return json.loads(content), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


def _safe_write_bot_oc_config(
    bot_id: str, new_config: dict, *, reason: str, bot_user: str | None,
) -> tuple[bool, str]:
    """Defer to deploy.safe_write_bot_config for atomic write + schema validation.

    Imported lazily so unit tests can stub it out without dragging the
    full deploy module into their import graph.
    """
    try:
        from evolve_admin.deploy import safe_write_bot_config
    except ImportError as exc:
        return False, f"deploy.safe_write_bot_config not available: {exc}"
    return safe_write_bot_config(bot_id, new_config, reason=reason, bot_user=bot_user)


# ── Primary-bot gate ─────────────────────────────────────────────────────────

def _primary_bot_id(network: dict[str, Any]) -> str | None:
    """Resolve the primary bot id from network.json.

    Imported lazily so this module loads cleanly in worktree-test contexts
    where the analyzer package isn't importable.
    """
    try:
        from primary_bot import primary_bot_id
    except ImportError:
        # Fallback inline — keeps the gate workable when analyzer isn't
        # importable. Logic mirrors analyzer/primary_bot.py.
        pid = network.get("primary")
        if isinstance(pid, str) and pid:
            return pid
        bots = network.get("bots") or {}
        if isinstance(bots, dict):
            for bid, cfg in bots.items():
                if isinstance(cfg, dict) and cfg.get("role") == "primary":
                    return bid
            if "evolve" in bots:
                return "evolve"
        return None
    return primary_bot_id(network)


# ── Main entry point ─────────────────────────────────────────────────────────

def ensure_evo_tools_mcp_server(
    bot_id: str,
    network: dict[str, Any],
    *,
    network_path: Path | str,
    venv_python: str = DEFAULT_VENV_PYTHON,
) -> tuple[bool, str]:
    """Ensure ``mcp.servers.evo_tools`` is correctly registered in bot_id's
    openclaw.json. Idempotent gap-fill — runs on every deploy.

    Returns ``(changed, status)``:

    * ``(False, "skipped:not-primary")`` — bot_id isn't the primary bot.
      The registry is evo's direct-access surface only; member bots get
      tools via the indirect plugin path. No write attempted.

    * ``(False, "unchanged")`` — primary bot already has the correct
      block. No write attempted.

    * ``(True, "created")`` — primary bot had no mcp.servers.evo_tools
      entry; one was added.

    * ``(True, "updated")`` — primary bot had a stale entry (different
      command / args / env); rewritten to match the canonical block.

    * ``(False, "error: <detail>")`` — read/write failed. The caller
      logs and continues — a missing MCP registration is non-fatal for
      the deploy (the bot still runs; only the tool surface is dark).

    Parameters
    ----------
    bot_id:
        Bot being deployed. Function returns early when this isn't the
        primary bot.

    network:
        Loaded network.json dict. Source of truth for sharedDir +
        primary-bot identity.

    network_path:
        Filesystem path to network.json. Passed into the bridge via
        the EVOLVE_NETWORK_PATH env var so it loads the same network
        config the deploy uses.

    venv_python:
        Path to the python3 interpreter for the bridge. Default matches
        deploy.VENV_PYTHON.
    """
    primary = _primary_bot_id(network)
    if primary is None or bot_id != primary:
        log.debug(
            "ensure_evo_tools_mcp_server: skipping %s (not primary; primary=%s)",
            bot_id, primary,
        )
        return False, "skipped:not-primary"

    bot_user = _bot_user_for(bot_id, network)
    shared_dir = network.get("sharedDir") or "/Users/Shared/evolve"
    network_path_str = str(network_path)

    desired = build_server_block(
        venv_python=venv_python,
        shared_dir=shared_dir,
        network_path=network_path_str,
    )

    oc, err = _read_bot_oc_config(bot_id, bot_user)
    if oc is None:
        return False, f"error: read failed: {err}"

    # Compare against current entry, if any. We want exact-match
    # idempotency — any drift (e.g. operator hand-edit, stale sharedDir)
    # triggers a rewrite. Other servers in mcp.servers stay untouched.
    mcp_block = oc.get("mcp") or {}
    servers_block = mcp_block.get("servers") or {}
    current = servers_block.get(EVO_TOOLS_SERVER_ID)
    if current == desired:
        return False, "unchanged"

    status = "updated" if current is not None else "created"
    new_cfg = deepcopy(oc)
    new_mcp = new_cfg.setdefault("mcp", {})
    new_servers = new_mcp.setdefault("servers", {})
    new_servers[EVO_TOOLS_SERVER_ID] = desired

    ok, msg = _safe_write_bot_oc_config(
        bot_id, new_cfg,
        reason=f"evo tool registry: {status} mcp.servers.{EVO_TOOLS_SERVER_ID}",
        bot_user=bot_user,
    )
    if not ok:
        return False, f"error: write failed: {msg}"
    log.info(
        "ensure_evo_tools_mcp_server: %s mcp.servers.%s for %s",
        status, EVO_TOOLS_SERVER_ID, bot_id,
    )
    return True, status
