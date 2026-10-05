"""mcp.monitor — diff observed inventory vs. allowlist; emit Signals.

Spec: internal/spec-mcp-administration-2026-05-10.md §3.3 (Signals) +
§5.2 (drift detection).

Phase A scope:
  - For each bot in network.json: read inventory, write cached file,
    diff against pod-wide allowlist, emit per-finding Signal via
    signals.store.observe().
  - After all bots done, sweep-resolve any active signals from this
    producer that didn't recur (the spec's "cleared condition →
    auto-archive" pattern).

Signal types (per §3.3):
  - mcp_unknown_server (high)        — server present, not in allowlist
  - mcp_server_changed (medium)      — allowlist hit but signature drift
  - mcp_self_mutation_enabled (med)  — commands.mcp or commands.plugins true
  - mcp_openclaw_config_missing (low) — bot has no openclaw.json
  - mcp_server_unhealthy (medium)    — N consecutive probe failures (Phase C)
  - mcp_server_credential_invalid (med) — wrapper script reports missing
                                          keystore key (Phase C)
  - mcp_server_cve_match (severity from advisory) — installed server's
                                          catalog package has an open
                                          GitHub Security Advisory (Phase D)

Producer name is "mcp_monitor" — distinct from "audit" so the existing
audit sweep doesn't touch these signals.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import inventory as _inv
from . import allowlist as _al
from . import probe as _probe
from . import advisories as _adv


def _read_http_headers_for(inv: _inv.Inventory, server_name: str) -> "dict[str, str] | None":
    """Re-read the bot's openclaw.json to fetch the headers for an http server.

    Inventory caches drop the literal header values (they're embedded
    secrets — keeping them out of {shared_dir}/mcp/inventory limits
    blast radius if that cache file leaks). The http probe path needs
    the real headers to talk to the server, so we sudo-cat the bot's
    openclaw.json at probe time. Returns None on any read failure
    (probe falls back to header-less request, which the server will
    typically reject with 401 → classified as credential_invalid).
    """
    import json
    import subprocess
    try:
        r = subprocess.run(
            ["sudo", "/bin/cat", inv.openclaw_config_path],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        oc = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    block = ((oc.get("mcp") or {}).get("servers") or {}).get(server_name) or {}
    headers = block.get("headers")
    if isinstance(headers, dict):
        # Coerce values to strings — schema allows number/bool but openclaw
        # treats them as strings when building HTTP requests.
        return {str(k): str(v) for k, v in headers.items()}
    return None

# Optional signals dependency — degrades to no-op for unit tests.
try:
    from signals import store as _signals_store
    from schema.signal import make_signature as _make_signature
except ImportError:  # pragma: no cover — tests stub these
    _signals_store = None  # type: ignore[assignment]
    _make_signature = None  # type: ignore[assignment]


PRODUCER = "mcp_monitor"


# ── Diff result ───────────────────────────────────────────────────────────────

def _diff_one_bot(
    inv: _inv.Inventory,
    allowlist: _al.Allowlist,
) -> list[dict[str, Any]]:
    """Compute the list of findings for a single bot's inventory.

    Each finding is a dict {type, severity, signature, title, body,
    details}; the caller turns these into Signal observations.
    """
    findings: list[dict[str, Any]] = []
    bot_id = inv.bot_id

    # Missing config (low) — team_bot_b today
    if not inv.openclaw_config_present:
        findings.append({
            "type": "mcp_openclaw_config_missing",
            "severity": "info",
            "signature_scope": bot_id,
            "title": f"{bot_id}: openclaw.json not found",
            "body": (
                f"Bot {bot_id} is in network.json but has no openclaw.json at "
                f"{inv.openclaw_config_path}. MCP inventory cannot be checked."
            ),
            "details": {
                "bot_id": bot_id,
                "openclaw_config_path": inv.openclaw_config_path,
                "read_error": inv.read_error,
            },
        })
        return findings  # no point checking servers if no config

    # Self-mutation gate (medium) — fires once per affected bot per cycle
    if inv.self_mutation_commands_mcp or inv.self_mutation_commands_plugins:
        which = []
        if inv.self_mutation_commands_mcp:
            which.append("commands.mcp")
        if inv.self_mutation_commands_plugins:
            which.append("commands.plugins")
        findings.append({
            "type": "mcp_self_mutation_enabled",
            "severity": "warn",
            "signature_scope": f"{bot_id}:self-mutation",
            "title": f"{bot_id}: bot can self-mutate MCP / plugins config",
            "body": (
                f"Bot {bot_id} has {' + '.join(which)} = true. This lets the "
                "bot add or change MCP server configuration via its /mcp or "
                "/plugins slash command, bypassing the proposal pipeline."
            ),
            "details": {
                "bot_id": bot_id,
                "commands_mcp": inv.self_mutation_commands_mcp,
                "commands_plugins": inv.self_mutation_commands_plugins,
            },
        })

    # Per-server checks
    for server in inv.servers:
        allow = allowlist.entry_for(bot_id, server.name)
        if allow is None:
            findings.append({
                "type": "mcp_unknown_server",
                "severity": "alert",
                "signature_scope": f"{bot_id}:{server.name}",
                "title": f"{bot_id}: unknown MCP server {server.name!r}",
                "body": (
                    f"MCP server {server.name!r} is configured on {bot_id} "
                    f"but has no allowlist entry. Transport: {server.kind}. "
                    "Add an entry via the catalog (Phase B) or remove the "
                    "server from openclaw.json."
                ),
                "details": {
                    "bot_id": bot_id,
                    "server_name": server.name,
                    "kind": server.kind,
                    "command": server.command,
                    "url": server.url,
                    "env_keys": server.env_keys,
                    "config_signature": server.config_signature,
                },
            })
            continue
        # Allowlist hit — compare signature if one is recorded
        if allow.config_signature and allow.config_signature != server.config_signature:
            findings.append({
                "type": "mcp_server_changed",
                "severity": "warn",
                "signature_scope": f"{bot_id}:{server.name}:changed",
                "title": f"{bot_id}: MCP server {server.name!r} configuration drifted",
                "body": (
                    f"The launch shape for {server.name!r} on {bot_id} differs "
                    "from the allowlist record. Compare and either update the "
                    "allowlist (via proposal) or revert the bot's openclaw.json."
                ),
                "details": {
                    "bot_id": bot_id,
                    "server_name": server.name,
                    "kind": server.kind,
                    "observed_signature": server.config_signature,
                    "allowlist_signature": allow.config_signature,
                },
            })

    return findings


# ── Public entry point ────────────────────────────────────────────────────────

def _probe_one_inventory(
    inv: _inv.Inventory,
    shared_dir: Path,
    *,
    timeout: float,
    unhealthy_threshold: int,
) -> list[dict[str, Any]]:
    """Probe each stdio server in this bot's inventory; emit health findings.

    Phase C scope: stdio only (servers with a ``command``). Http-transport
    probing is deferred until the http credential-binding design lands.

    For each probe:
      1. Run probe_stdio() with the inventory's command + args.
      2. Append result to {shared_dir}/mcp/health/<bot>/<server>.jsonl.
      3. If the *most recent* N probes (default 3) all failed, emit
         either mcp_server_credential_invalid (when the most recent
         classifies as credential_invalid) or mcp_server_unhealthy
         (otherwise).
      4. If the latest probe succeeded, no new health finding — the
         signal store's sweep_resolve handles auto-clearing.
    """
    findings: list[dict[str, Any]] = []
    bot_id = inv.bot_id

    for server in inv.servers:
        if server.kind == "stdio" and server.command:
            result = _probe.probe_stdio(
                server.command,
                list(server.args or []),
                bot_id=bot_id,
                server_id=server.name,
                timeout=timeout,
            )
        elif server.kind == "http" and server.url:
            # Phase E: http transport probes. Headers stored in
            # openclaw.json are literal values (not keystore refs),
            # so we read them from the inventory record directly.
            # The inventory doesn't currently surface header values to
            # avoid leaking secrets through the cache file — for http
            # probes we re-read openclaw.json to get the headers.
            headers = _read_http_headers_for(inv, server.name)
            result = _probe.probe_http(
                server.url, headers,
                bot_id=bot_id, server_id=server.name, timeout=timeout,
            )
        else:
            continue
        try:
            _probe.append_health(result, shared_dir)
        except OSError:
            # Disk-write failure shouldn't break the monitor cycle
            pass

        # Read back the last N entries (this one + prior) and decide signal
        recent, ok_count, fail_count = _probe.last_n_status(
            shared_dir, bot_id, server.name, n=unhealthy_threshold,
        )
        if len(recent) < unhealthy_threshold:
            # Not enough history to fire a confidence signal yet
            continue
        if fail_count < unhealthy_threshold:
            # Some recent success — no sustained-failure signal
            continue

        # All of the last N failed. Pick signal type from the most-recent
        # error class so the operator sees the specific failure mode.
        latest = recent[0]
        latest_class = str(latest.get("error_class") or "")
        if latest_class == "credential_invalid":
            findings.append({
                "type": "mcp_server_credential_invalid",
                "severity": "warn",
                "signature_scope": f"{bot_id}:{server.name}:credential",
                "title": f"{bot_id}: MCP server {server.name!r} credential invalid",
                "body": (
                    f"The wrapper for {server.name!r} on {bot_id} reports a missing "
                    "keystore key — the server cannot start. Re-bind credentials via "
                    "the Update flow, or add the missing key in Credentials."
                ),
                "details": {
                    "bot_id": bot_id,
                    "server_name": server.name,
                    "error_class": latest_class,
                    "error_detail": str(latest.get("error_detail") or "")[:240],
                    "failures_in_window": fail_count,
                    "probes_in_window": len(recent),
                },
            })
        else:
            findings.append({
                "type": "mcp_server_unhealthy",
                "severity": "warn",
                "signature_scope": f"{bot_id}:{server.name}:unhealthy",
                "title": f"{bot_id}: MCP server {server.name!r} unhealthy",
                "body": (
                    f"The last {unhealthy_threshold} probes against {server.name!r} on "
                    f"{bot_id} failed (most recent: {latest_class}). The server may be "
                    "unable to start, mis-configured, or the protocol handshake is "
                    "failing. Inspect the recent probe log."
                ),
                "details": {
                    "bot_id": bot_id,
                    "server_name": server.name,
                    "error_class": latest_class,
                    "error_detail": str(latest.get("error_detail") or "")[:240],
                    "latency_ms": latest.get("latency_ms"),
                    "failures_in_window": fail_count,
                    "probes_in_window": len(recent),
                },
            })

    return findings


def run(
    shared_dir: Path,
    bot_ids: list[str],
    config: "dict[str, Any] | None" = None,
    *,
    emit_signals: bool = True,
    run_probes: bool = True,
    probe_timeout: float = 10.0,
    unhealthy_threshold: int = 3,
    refresh_advisories: bool = True,
    force_advisory_refresh: bool = False,
) -> dict[str, Any]:
    """Run the MCP monitor across the given bots.

    Side effects:
      - Writes inventory cache files to {shared_dir}/mcp/inventory/<bot>.json.
      - Ensures the allowlist file exists at {shared_dir}/policy/mcp-allowlist.json
        (writes an empty default if missing).
      - Runs an MCP-initialize probe against each installed stdio server
        when ``run_probes`` is True (Phase C). Appends to
        {shared_dir}/mcp/health/<bot>/<server>.jsonl.
      - Emits Signals via signals.store.observe() for each finding (skipped
        when emit_signals=False, e.g. unit tests).
      - Sweep-resolves any prior signals from PRODUCER no longer present.

    Returns a summary dict for the audit log:
      {
        "bots_checked": int,
        "findings": [ {type, bot_id, ...}, ... ],
        "probes_run": int,
        "swept_resolved": int,
      }
    """
    _al.write_default_if_missing(shared_dir)
    allowlist = _al.load(shared_dir)

    findings: list[dict[str, Any]] = []
    kept_signatures: set[str] = set()
    probes_run = 0
    inventories_by_bot: dict[str, dict[str, Any] | None] = {}

    for bot_id in bot_ids:
        inv = _inv.read_inventory(bot_id, config)
        _inv.write_inventory(inv, shared_dir)
        inventories_by_bot[inv.bot_id] = inv.to_dict() if inv.openclaw_config_present else None
        for finding in _diff_one_bot(inv, allowlist):
            finding["bot_id"] = inv.bot_id
            findings.append(finding)
        if run_probes and inv.openclaw_config_present and inv.servers:
            try:
                health_findings = _probe_one_inventory(
                    inv, shared_dir,
                    timeout=probe_timeout,
                    unhealthy_threshold=unhealthy_threshold,
                )
                probes_run += sum(
                    1 for s in inv.servers
                    if (s.kind == "stdio" and s.command) or (s.kind == "http" and s.url)
                )
                for hf in health_findings:
                    hf["bot_id"] = inv.bot_id
                    findings.append(hf)
            except Exception:  # noqa: BLE001 — probe failure can't break inventory
                pass

    # ── Phase D: advisory refresh + CVE cross-reference ───────────────────────
    advisories_refreshed: list[dict[str, Any]] = []
    if refresh_advisories:
        try:
            from .catalog import load as _load_cat
            cat = _load_cat(shared_dir)
            # Refresh (rate-limited to 24h; force= when explicitly asked)
            advisories_refreshed = _adv.refresh_catalog(
                shared_dir, catalog=cat, force=force_advisory_refresh,
            )
            # Cross-reference installed servers against cached advisories
            cve_findings = _adv.find_findings(shared_dir, inventories_by_bot, cat)
            findings.extend(cve_findings)
        except Exception:  # noqa: BLE001 — advisory pipeline can't break monitor
            pass

    swept_resolved = 0
    if emit_signals and _signals_store is not None and _make_signature is not None:
        emitted_types: set[str] = set()
        for f in findings:
            signature = _make_signature(PRODUCER, f["type"], f["signature_scope"])
            kept_signatures.add(signature)
            emitted_types.add(f["type"])
            try:
                _signals_store.observe(
                    shared_dir,
                    signature=signature,
                    producer=PRODUCER,
                    type=f["type"],
                    flavor="maintenance",
                    severity=f["severity"],
                    scope="bot",
                    bot_id=f.get("bot_id"),
                    title=f["title"],
                    body=f["body"],
                    details=f.get("details") or {},
                )
            except Exception:  # noqa: BLE001 — defensive; one bad signal can't break the rest
                continue
        # Sweep-resolve any prior signals from this producer not in keep set.
        # Scope to the four types we own so we don't accidentally clear
        # someone else's signals if PRODUCER is reused later.
        try:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                reason="auto-resolve: MCP condition cleared on next monitor run",
                types={
                    "mcp_unknown_server",
                    "mcp_server_changed",
                    "mcp_self_mutation_enabled",
                    "mcp_openclaw_config_missing",
                    "mcp_server_unhealthy",
                    "mcp_server_credential_invalid",
                    "mcp_server_cve_match",
                },
            )
            swept_resolved = len(swept)
        except Exception:  # noqa: BLE001
            swept_resolved = 0

    return {
        "bots_checked": len(bot_ids),
        "findings": findings,
        "probes_run": probes_run,
        "advisories_refreshed": advisories_refreshed,
        "swept_resolved": swept_resolved,
    }
