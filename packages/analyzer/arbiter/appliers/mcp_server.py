"""arbiter.appliers.mcp_server — Install / Remove / Update MCP servers.

Spec: internal/spec-mcp-administration-2026-05-10.md §3.5.

Three appliers in one module because they share the openclaw.json
read/write helper and the allowlist read/write helper. Each registers
itself with the applier dispatch on import.

The actual openclaw.json write goes through ``evolve_admin.deploy.
safe_write_bot_config`` — which handles /tmp staging, schema
validation, atomic .bak preservation, and sudo /bin/cp + chown. We
don't reimplement that here.

Wrapper scripts are written under {shared_dir}/mcp/launchers/ via the
``mcp.launcher`` module — evolve-user-owned, no sudo needed.
"""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from arbiter.appliers.base import ApplyResult, RevertResult, register_applier
from schema.proposal import InstallMcpServer, RemoveMcpServer, UpdateMcpServerConfig


# ── Shared dir resolution ─────────────────────────────────────────────────────

def _shared_dir() -> Path:
    """Resolve shared_dir at apply time.

    We can't take it as a constructor arg (appliers register at module
    import, before network config is loaded). The standard pod path
    is ``/Users/Shared/evolve``; network.json override falls through
    when the path is set in the env or via a passed-in config.
    """
    # Match the pattern in pod_report.py / community_intel.py — read
    # network.json once, default to the standard mini path.
    import os
    candidates = [
        os.environ.get("EVOLVE_SHARED_DIR"),
        "/Users/Shared/evolve",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return Path("/Users/Shared/evolve")


# ── openclaw.json helpers ─────────────────────────────────────────────────────

def _read_bot_oc_config(bot_id: str) -> tuple[dict | None, str | None]:
    """Read a bot's openclaw.json via sudo /bin/cat.

    Returns (config_dict, error_str). The audit pipeline uses the same
    pattern (audit.py:367–379) — sudo is required because evolve user
    can't read another user's home directly without ACL.
    """
    from evolve_config import bot_home
    oc_path = bot_home(bot_id) / ".openclaw" / "openclaw.json"
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", str(oc_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"read_error: {exc}"
    if r.returncode != 0:
        return None, f"sudo_cat_rc={r.returncode}: {r.stderr.strip()}"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"json_decode: {exc.msg}"


def _safe_write_bot_oc_config(bot_id: str, new_config: dict, reason: str) -> tuple[bool, str]:
    """Defer to evolve_admin.deploy.safe_write_bot_config for the actual write.

    Imports lazily so the analyzer package isn't permanently coupled to
    evolve_admin at import time (matters for the worktree-test path).
    """
    try:
        from evolve_admin.deploy import safe_write_bot_config
    except ImportError as exc:
        return False, f"deploy.safe_write_bot_config not available: {exc}"
    return safe_write_bot_config(bot_id, new_config, reason=reason)


def _restart_bot_gateway(bot_id: str) -> tuple[bool, str]:
    """Restart a bot's OpenClaw gateway so it picks up the new mcp.servers config.

    Calls evolve_admin.deploy.restart_gateway which handles launchctl
    kickstart + the port-clear safety dance. Defensive — restart failure
    is reported but doesn't roll back the config write (which already
    succeeded). The operator can fall back to manual restart.

    Phase E addition (spec §5.2 step 4): we used to leave restarts to
    the operator; auto-restart closes the loop so an install proposal
    is fully effective without manual intervention.
    """
    try:
        from evolve_admin.deploy import restart_gateway
    except ImportError as exc:
        return False, f"deploy.restart_gateway not available: {exc}"
    try:
        restart_gateway(bot_id)
    except Exception as exc:  # noqa: BLE001 — restart_gateway can fail for many reasons
        return False, f"restart failed: {type(exc).__name__}: {exc}"
    return True, ""


# ── allowlist helpers (in-applier — keeps applier hermetic) ───────────────────

def _allowlist_path(shared_dir: Path) -> Path:
    return shared_dir / "policy" / "mcp-allowlist.json"


def _load_allowlist_raw(shared_dir: Path) -> dict:
    path = _allowlist_path(shared_dir)
    if not path.exists():
        return {"version": 1, "entries": []}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entries": []}


def _write_allowlist_raw(shared_dir: Path, allowlist: dict) -> None:
    path = _allowlist_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(allowlist, indent=2, sort_keys=True))
    tmp.replace(path)


def _allowlist_upsert(allowlist: dict, *, name: str, bot_id: str, signature: str | None) -> None:
    """Add/replace an entry in-place. Preserves other entries' order."""
    entries = allowlist.setdefault("entries", [])
    for e in entries:
        if e.get("name") == name and e.get("bot_id") == bot_id:
            e["config_signature"] = signature
            return
    entries.append({"name": name, "bot_id": bot_id, "config_signature": signature})


def _allowlist_remove(allowlist: dict, *, name: str, bot_id: str) -> bool:
    """Drop the matching entry. Returns True if removed; False if absent."""
    entries = allowlist.get("entries") or []
    before = len(entries)
    allowlist["entries"] = [
        e for e in entries
        if not (e.get("name") == name and e.get("bot_id") == bot_id)
    ]
    return len(allowlist["entries"]) < before


# ── Catalog lookup ────────────────────────────────────────────────────────────

def _load_catalog_entry(shared_dir: Path, catalog_id: str):
    """Load the catalog and find one entry, or None if not found."""
    from mcp_admin.catalog import load as _cat_load
    cat = _cat_load(shared_dir)
    return cat.find(catalog_id)


# ── Signature recompute ───────────────────────────────────────────────────────

def _server_signature(server_block: dict) -> str:
    """Recompute the spec's config_signature from an openclaw.json mcp.servers entry.

    Matches mcp.inventory._signature_for_server exactly.
    """
    from mcp_admin.inventory import _signature_for_server
    # mcp.inventory takes (name, cfg) but only uses the cfg fields — name
    # is included in the canonical form for namespacing.
    return _signature_for_server(server_block.get("_name", ""), server_block)


# ─────────────────────────────────────────────────────────────────────────────
# InstallMcpServer applier
# ─────────────────────────────────────────────────────────────────────────────

class InstallMcpServerApplier:
    """Install an MCP server on a bot from a catalog entry.

    Steps:
      1. Look up the catalog entry.
      2. Generate a wrapper script under {shared_dir}/mcp/launchers/<bot>/<srv>.
      3. Read the bot's openclaw.json; add mcp.servers[<srv>] pointing at the wrapper.
      4. Write openclaw.json via safe_write_bot_config (validates schema; preserves .bak).
      5. Add a pod-allowlist entry with the new config signature.
    """

    def capture_snapshot(self, action: InstallMcpServer, bot_id: str) -> dict:
        shared = _shared_dir()
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_server = None
        if oc is not None:
            prior_server = ((oc.get("mcp") or {}).get("servers") or {}).get(action.server_id)
        allowlist_before = _load_allowlist_raw(shared)
        return {
            "action_kind": "InstallMcpServer",
            "bot_id": action.bot_id,
            "server_id": action.server_id,
            "catalog_id": action.catalog_id,
            "env_bindings": dict(action.env_bindings),
            "prior_server_existed": prior_server is not None,
            "prior_server_block": prior_server,
            "allowlist_before": allowlist_before,
            "shared_dir": str(shared),
            "read_error": err,
        }

    def apply(self, action: InstallMcpServer, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        from mcp_admin.launcher import LauncherSpec, write_launcher, launcher_path

        # 1. Catalog lookup
        entry = _load_catalog_entry(shared, action.catalog_id)
        if entry is None:
            return ApplyResult(
                ok=False,
                details={"error": f"catalog entry {action.catalog_id!r} not found"},
                message=f"Catalog entry {action.catalog_id!r} not found in pod catalog.",
            )
        if entry.vetting_status == "withdrawn":
            return ApplyResult(
                ok=False,
                details={"error": "vetting_status=withdrawn"},
                message=f"Catalog entry {action.catalog_id!r} is withdrawn; refusing install.",
            )

        # 2. Validate env_bindings cover required_env
        required_names = {e.name for e in entry.required_env}
        provided_names = set(action.env_bindings.keys())
        missing = required_names - provided_names
        if missing:
            return ApplyResult(
                ok=False,
                details={"missing_env": sorted(missing)},
                message=f"InstallMcpServer missing required env bindings: {sorted(missing)}",
            )

        # 3. Build the server config block — branch on transport
        if entry.transport == "stdio":
            try:
                wrapper = write_launcher(
                    LauncherSpec(catalog_entry=entry, env_bindings=action.env_bindings),
                    bot_id=action.bot_id,
                    server_id=action.server_id,
                    shared_dir=shared,
                )
            except ValueError as exc:
                return ApplyResult(
                    ok=False,
                    details={"error": str(exc)},
                    message=f"wrapper-script generation failed: {exc}",
                )
            # extra_args (per-install) flow through to the real binary via the
            # wrapper's trailing "$@" expansion. The catalog entry's own args
            # are baked into the wrapper script itself; extra_args goes through
            # openclaw.json so it can vary per (bot, server) install without
            # re-publishing the catalog. Used by skills that bind a vetted
            # catalog server to a user-supplied scope (e.g. obsidian → filesystem
            # MCP scoped to the vault path).
            new_server_block = {
                "command": str(wrapper),
                "args": list(action.extra_args or []),
            }
        elif entry.transport == "http":
            # Phase E: http transport. Resolve keystore references at
            # install time and embed the literal header values in
            # openclaw.json. Trade-off (spec §5.3): credentials in
            # openclaw.json; rotation requires an UpdateMcpServerConfig
            # proposal vs. stdio which rotates in the keystore alone.
            if not entry.url:
                return ApplyResult(
                    ok=False,
                    details={"error": "http catalog entry missing url"},
                    message=f"Catalog entry {action.catalog_id!r} is http but has no url.",
                )
            from mcp_admin.launcher import resolve_http_headers
            headers, err = resolve_http_headers(entry, action.env_bindings)
            if headers is None:
                return ApplyResult(
                    ok=False,
                    details={"error": err},
                    message=f"http credential resolution failed: {err}",
                )
            new_server_block = {"url": entry.url}
            if headers:
                new_server_block["headers"] = headers
        else:
            return ApplyResult(
                ok=False,
                details={"transport": entry.transport},
                message=(
                    f"Unknown transport {entry.transport!r} — catalog entries must "
                    "specify 'stdio' or 'http'."
                ),
            )

        # 4. Patch openclaw.json
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(
                ok=False,
                details={"error": err},
                message=f"cannot read {action.bot_id}'s openclaw.json: {err}",
            )
        oc = deepcopy(oc)
        mcp_block = oc.setdefault("mcp", {})
        servers = mcp_block.setdefault("servers", {})
        servers[action.server_id] = new_server_block

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id,
            oc,
            reason=f"InstallMcpServer {action.catalog_id} on {action.bot_id}",
        )
        if not ok:
            # Clean up the wrapper we wrote — config write failed
            try:
                if entry.transport == "stdio":
                    launcher_path(shared, action.bot_id, action.server_id).unlink(missing_ok=True)
            except OSError:
                pass
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        # 5. Update allowlist with the new config signature
        signature = _server_signature({"_name": action.server_id, **new_server_block})
        allowlist = _load_allowlist_raw(shared)
        _allowlist_upsert(
            allowlist,
            name=action.server_id,
            bot_id=action.bot_id,
            signature=signature,
        )
        _write_allowlist_raw(shared, allowlist)

        # 6. Restart the gateway so it picks up the new server.
        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)

        wrap_path_value = (
            str(launcher_path(shared, action.bot_id, action.server_id))
            if entry.transport == "stdio"
            else None
        )

        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "server_id": action.server_id,
                "catalog_id": action.catalog_id,
                "transport": entry.transport,
                "wrapper_path": wrap_path_value,
                "config_signature": signature,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Installed MCP server {action.server_id!r} ({action.catalog_id}) on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        server_id = snapshot.get("server_id") or ""
        shared = Path(snapshot.get("shared_dir") or "/Users/Shared/evolve")
        prior_existed = bool(snapshot.get("prior_server_existed"))
        prior_block = snapshot.get("prior_server_block")
        allowlist_before = snapshot.get("allowlist_before") or {"version": 1, "entries": []}

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        servers = (oc.get("mcp") or {}).get("servers") or {}
        if prior_existed:
            servers[server_id] = prior_block
        else:
            servers.pop(server_id, None)

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert InstallMcpServer {server_id} on {bot}"
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)

        # Remove launcher if we wrote one (only when prior didn't exist)
        if not prior_existed:
            try:
                from mcp_admin.launcher import launcher_path
                launcher_path(shared, bot, server_id).unlink(missing_ok=True)
            except OSError:
                pass

        # Restore allowlist exactly
        _write_allowlist_raw(shared, allowlist_before)

        return RevertResult(
            ok=True,
            details={"bot_id": bot, "server_id": server_id, "restored_prior": prior_existed},
            message=f"Reverted InstallMcpServer {server_id!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# RemoveMcpServer applier
# ─────────────────────────────────────────────────────────────────────────────

class RemoveMcpServerApplier:
    """Remove an MCP server from a bot's openclaw.json and the allowlist."""

    def capture_snapshot(self, action: RemoveMcpServer, bot_id: str) -> dict:
        shared = _shared_dir()
        oc, err = _read_bot_oc_config(action.bot_id)
        prior_server = None
        if oc is not None:
            prior_server = ((oc.get("mcp") or {}).get("servers") or {}).get(action.server_id)
        return {
            "action_kind": "RemoveMcpServer",
            "bot_id": action.bot_id,
            "server_id": action.server_id,
            "prior_server_existed": prior_server is not None,
            "prior_server_block": prior_server,
            "allowlist_before": _load_allowlist_raw(shared),
            "shared_dir": str(shared),
            "read_error": err,
        }

    def apply(self, action: RemoveMcpServer, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        oc, err = _read_bot_oc_config(action.bot_id)
        if oc is None:
            return ApplyResult(ok=False, details={"error": err}, message=err or "")
        oc = deepcopy(oc)
        servers = (oc.get("mcp") or {}).get("servers") or {}
        if action.server_id not in servers:
            # No-op — server already absent
            return ApplyResult(
                ok=True,
                details={"no_op": True},
                message=f"MCP server {action.server_id!r} not present on {action.bot_id}; nothing to do.",
            )
        del servers[action.server_id]
        # Prune the empty parent dict to keep openclaw.json tidy
        if not servers and "mcp" in oc:
            oc["mcp"].pop("servers", None)
            if not oc["mcp"]:
                oc.pop("mcp", None)

        ok, msg = _safe_write_bot_oc_config(
            action.bot_id, oc,
            reason=f"RemoveMcpServer {action.server_id} on {action.bot_id}",
        )
        if not ok:
            return ApplyResult(ok=False, details={"error": msg}, message=msg)

        # Remove the wrapper script if there was one
        try:
            from mcp_admin.launcher import delete_launcher
            delete_launcher(action.bot_id, action.server_id, shared)
        except OSError:
            pass

        # Drop the allowlist entry
        allowlist = _load_allowlist_raw(shared)
        _allowlist_remove(allowlist, name=action.server_id, bot_id=action.bot_id)
        _write_allowlist_raw(shared, allowlist)

        # Restart so OC stops trying to talk to the removed server
        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)

        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "server_id": action.server_id,
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Removed MCP server {action.server_id!r} from {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        bot = snapshot.get("bot_id") or bot_id
        server_id = snapshot.get("server_id") or ""
        shared = Path(snapshot.get("shared_dir") or "/Users/Shared/evolve")
        prior_existed = bool(snapshot.get("prior_server_existed"))
        prior_block = snapshot.get("prior_server_block")
        allowlist_before = snapshot.get("allowlist_before") or {"version": 1, "entries": []}

        if not prior_existed:
            # Nothing to restore — already absent before
            return RevertResult(
                ok=True,
                details={"no_op": True},
                message=f"RemoveMcpServer revert no-op: {server_id!r} wasn't present before.",
            )

        oc, err = _read_bot_oc_config(bot)
        if oc is None:
            return RevertResult(ok=False, message=f"cannot read openclaw.json: {err}")
        oc = deepcopy(oc)
        servers = oc.setdefault("mcp", {}).setdefault("servers", {})
        servers[server_id] = prior_block

        ok, msg = _safe_write_bot_oc_config(
            bot, oc, reason=f"Revert RemoveMcpServer {server_id} on {bot}"
        )
        if not ok:
            return RevertResult(ok=False, details={"error": msg}, message=msg)
        _write_allowlist_raw(shared, allowlist_before)
        return RevertResult(
            ok=True,
            details={"bot_id": bot, "server_id": server_id},
            message=f"Restored MCP server {server_id!r} on {bot}.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# UpdateMcpServerConfig applier
# ─────────────────────────────────────────────────────────────────────────────

class UpdateMcpServerConfigApplier:
    """Mutate env_bindings for an installed MCP server.

    Re-renders the wrapper script with the new bindings. The openclaw.json
    block doesn't change (it just points at the wrapper). The allowlist
    signature is recomputed because the wrapper path is identical but
    env_keys may have changed (a new GITHUB_TOKEN_RO key, say).
    """

    def capture_snapshot(self, action: UpdateMcpServerConfig, bot_id: str) -> dict:
        shared = _shared_dir()
        from mcp_admin.launcher import launcher_path
        wrap_path = launcher_path(shared, action.bot_id, action.server_id)
        prior_script = None
        try:
            prior_script = wrap_path.read_text() if wrap_path.exists() else None
        except OSError:
            prior_script = None
        return {
            "action_kind": "UpdateMcpServerConfig",
            "bot_id": action.bot_id,
            "server_id": action.server_id,
            "new_env_bindings": dict(action.env_bindings),
            "prior_wrapper_script": prior_script,
            "wrapper_path": str(wrap_path),
            "allowlist_before": _load_allowlist_raw(shared),
            "shared_dir": str(shared),
        }

    def apply(self, action: UpdateMcpServerConfig, bot_id: str) -> ApplyResult:
        shared = _shared_dir()
        from mcp_admin.launcher import LauncherSpec, write_launcher, launcher_path

        # Need the catalog entry — but we don't have catalog_id on the
        # action. Recover it by reading the allowlist entry's signature
        # is too indirect. Simpler: take a catalog_id stamped on the
        # wrapper itself (in the auto-gen comment). Phase B v1 keeps it
        # simple — re-render assuming the existing catalog entry whose
        # id matches server_id (which is the typical case for v1 since
        # we install with server_id == catalog_id).
        entry = _load_catalog_entry(shared, action.server_id)
        if entry is None:
            return ApplyResult(
                ok=False,
                details={"error": f"catalog entry {action.server_id!r} not found"},
                message=(
                    f"Cannot update — catalog entry {action.server_id!r} not found. "
                    "Phase B assumes server_id == catalog_id; install with a different id "
                    "is a future feature."
                ),
            )

        # Validate env_bindings cover required_env
        required = {e.name for e in entry.required_env}
        missing = required - set(action.env_bindings)
        if missing:
            return ApplyResult(
                ok=False,
                details={"missing_env": sorted(missing)},
                message=f"UpdateMcpServerConfig missing required env: {sorted(missing)}",
            )

        try:
            write_launcher(
                LauncherSpec(catalog_entry=entry, env_bindings=action.env_bindings),
                bot_id=action.bot_id,
                server_id=action.server_id,
                shared_dir=shared,
            )
        except ValueError as exc:
            return ApplyResult(
                ok=False, details={"error": str(exc)}, message=f"wrapper rewrite failed: {exc}"
            )

        # Recompute allowlist signature (path unchanged, args unchanged,
        # but env keys may have shifted)
        wrap_path = launcher_path(shared, action.bot_id, action.server_id)
        new_block = {"_name": action.server_id, "command": str(wrap_path), "args": []}
        signature = _server_signature(new_block)
        allowlist = _load_allowlist_raw(shared)
        _allowlist_upsert(
            allowlist, name=action.server_id, bot_id=action.bot_id, signature=signature,
        )
        _write_allowlist_raw(shared, allowlist)

        # Restart so OC re-launches the server with the new wrapper
        restart_ok, restart_err = _restart_bot_gateway(action.bot_id)

        return ApplyResult(
            ok=True,
            details={
                "bot_id": action.bot_id,
                "server_id": action.server_id,
                "env_keys": sorted(action.env_bindings.keys()),
                "gateway_restarted": restart_ok,
                "gateway_restart_error": restart_err or None,
            },
            message=(
                f"Updated env bindings for {action.server_id!r} on {action.bot_id}. "
                + ("Gateway restarted." if restart_ok else
                   f"Gateway restart failed: {restart_err}. Restart manually.")
            ),
        )

    def revert(self, snapshot: dict, bot_id: str) -> RevertResult:
        shared = Path(snapshot.get("shared_dir") or "/Users/Shared/evolve")
        wrap_path = Path(snapshot.get("wrapper_path") or "")
        prior_script = snapshot.get("prior_wrapper_script")
        allowlist_before = snapshot.get("allowlist_before") or {"version": 1, "entries": []}

        if prior_script is None:
            # The wrapper didn't exist before — remove what we wrote
            try:
                if wrap_path.exists():
                    wrap_path.unlink()
            except OSError as e:
                return RevertResult(ok=False, message=f"unlink failed: {e}")
        else:
            try:
                wrap_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = wrap_path.with_suffix(".tmp")
                tmp.write_text(prior_script)
                import stat as _stat
                tmp.chmod(
                    _stat.S_IRWXU | _stat.S_IRGRP | _stat.S_IXGRP
                    | _stat.S_IROTH | _stat.S_IXOTH
                )
                tmp.replace(wrap_path)
            except OSError as e:
                return RevertResult(ok=False, message=f"restore wrapper failed: {e}")

        _write_allowlist_raw(shared, allowlist_before)
        return RevertResult(
            ok=True,
            details={"bot_id": snapshot.get("bot_id"), "server_id": snapshot.get("server_id")},
            message=(
                f"Reverted env bindings for {snapshot.get('server_id')!r} "
                f"on {snapshot.get('bot_id')}."
            ),
        )


# ── Registration ──────────────────────────────────────────────────────────────

register_applier("InstallMcpServer", InstallMcpServerApplier())
register_applier("RemoveMcpServer", RemoveMcpServerApplier())
register_applier("UpdateMcpServerConfig", UpdateMcpServerConfigApplier())
