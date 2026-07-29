"""Pod-state read tool — audit.

Exposes ``pod_state.audit`` — most recent OC security-audit results
per bot, projected to a per-bot summary the model can reason about.

Read order (cross-process correct):

  1. **In-process module-global** (``evolve_admin.audit_state._state``)
     — used when the tool runs inside the admin server's own process
     (tests, in-process invocations). Cheap, no network round-trip.

  2. **HTTP fetch** from the admin server's
     ``GET /api/security/audit`` — used when the tool runs in the MCP
     server's child process (the production path). The module-global
     in THAT process is **never populated** — the admin server's
     background audit thread writes to its own process's memory only.
     The HTTP endpoint is the canonical read path across the process
     boundary; both the UI's Security page and this tool now read the
     same source of truth.

Before this fix (issue surfaced 2026-05-19 from a Security-page chat
transcript): the tool always read step 1 and reported ``stale=true``
in production because nothing populates the MCP server's local cache.
Symptom: evo would confidently tell operators "the audit cache is
stale" while the same data was visibly rendered on the page right
next to the chat drawer.

When both paths fail — admin server unreachable AND module-global
empty — the tool returns ``{count: 0, audits: [], stale: true,
error: "..."}`` so the model can tell the operator audits aren't
gathered yet rather than fabricating clean state.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _project_audit(bot_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pick the model-relevant fields from one bot's audit result.

    Two shapes possible:

    * Successful audit — has ``summary`` (critical/warn/info counts) and
      ``findings`` (list of finding dicts). We keep the counts and a
      capped finding list (top 10 by severity).
    * Unavailable — has ``unavailable: True`` and an ``error`` /
      ``error_type``. We pass that through verbatim so the model knows
      WHY the audit didn't produce data.
    """
    if result.get("unavailable"):
        return {
            "bot_id": bot_id,
            "available": False,
            "error_type": result.get("error_type"),
            "error": result.get("error"),
        }

    summary = result.get("summary") or {}
    findings = result.get("findings") or []

    # Severity ordering: critical > warning > info. The model usually
    # wants the worst things first; sort accordingly and cap to 10.
    sev_order = {"critical": 0, "warning": 1, "info": 2}

    def _key(f):
        return (sev_order.get(f.get("severity", "info"), 99),
                f.get("checkId", ""))

    top = sorted(findings, key=_key)[:10]
    projected_findings = [
        {
            "checkId": f.get("checkId"),
            "severity": f.get("severity"),
            "title": f.get("title"),
            "category": f.get("category"),
            # remediation is one line of guidance — keep it.
            "remediation": f.get("remediation"),
        }
        for f in top
    ]
    return {
        "bot_id": bot_id,
        "available": True,
        "critical": int(summary.get("critical", 0) or 0),
        "warn": int(summary.get("warn", 0) or 0),
        "info": int(summary.get("info", 0) or 0),
        "findings_top": projected_findings,
        "findings_total": len(findings),
        "findings_truncated": len(findings) > len(projected_findings),
    }


def _read_in_process() -> tuple[dict[str, Any], float | None]:
    """Read from the in-process audit cache. Returns ``(data, cached_at)``.

    Empty ``data`` means either we're in a process that doesn't share
    the writer's memory (production MCP server case) OR the writer
    hasn't run yet. The caller falls through to disk and HTTP paths
    either way.
    """
    try:
        from evolve_admin.audit_state import snapshot as _audit_snapshot
    except ImportError as exc:
        log.debug("pod_state.audit: audit_state not importable: %s", exc)
        return {}, None
    snap = _audit_snapshot()
    return snap.get("data") or {}, snap.get("cached_at")


def _read_from_disk(
    shared_dir: Path | None,
) -> tuple[dict[str, Any], float | None]:
    """Read from the disk-mirror at ``{shared_dir}/security/audit-cache.json``.

    This is the canonical cross-process path since the audit cache was
    moved to disk. The admin server persists incrementally as audits
    complete; the MCP server child-process can read the same file
    without any RPC.

    Empty ``data`` means the file is missing (no audit has run yet,
    or shared_dir mismatch) or unreadable. Caller falls through to
    HTTP as defense-in-depth.
    """
    if shared_dir is None:
        return {}, None
    try:
        from evolve_admin.audit_state import snapshot as _audit_snapshot
    except ImportError as exc:
        log.debug("pod_state.audit: audit_state not importable: %s", exc)
        return {}, None
    snap = _audit_snapshot(shared_dir)
    return snap.get("data") or {}, snap.get("cached_at")


def _read_via_http(
    network_path: Path | None,
) -> tuple[dict[str, Any], float | None, str | None]:
    """Read from the admin server's ``GET /api/security/audit``.

    Returns ``(data, cached_at, error)``. Exactly one of ``data`` /
    ``error`` is meaningful. ``cached_at`` is whatever the endpoint
    sends back (a unix timestamp from the writer; ``None`` if the
    cache is empty on that side too).

    Failure modes the caller surfaces verbatim to the model:
      * Missing ``network_path`` (can't resolve admin base URL)
      * ``adminBaseUrl`` configured but server isn't reachable
      * HTTP error response from the admin server
      * Non-JSON response body
    """
    if network_path is None:
        return {}, None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )

    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return {}, None, f"evolve_admin.config not importable: {exc}"

    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return {}, None, f"could not load network.json: {exc}"

    try:
        base_url = resolve_admin_base_url(net)
    except Exception as exc:  # noqa: BLE001
        return {}, None, f"could not resolve admin base URL: {exc}"

    url = f"{base_url.rstrip('/')}/api/security/audit"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json"},
        )
        with urlopen_admin(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {}, None, f"admin server returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return {}, None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return {}, None, f"HTTP fetch failed: {exc}"

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {}, None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return {}, None, "admin server returned unexpected payload shape"

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    return data, payload.get("cached_at"), None


def _handler(
    bot_id: str | None = None,
    network_path: Path | None = None,
    shared_dir: Path | None = None,
) -> dict[str, Any]:
    """Return per-bot OC audit results.

    Filters: ``bot_id`` (optional). When omitted, returns every bot
    present in the cache.

    ``shared_dir`` and ``network_path`` are injected by the MCP
    bridge (see ``mcp_server.py:_wrap_handler``). When this tool runs
    inside the admin server process either may be absent and the
    in-process read covers it; that's what tests rely on.

    Read order (most-direct → fallback chain):
      1. **In-process module-global** — admin-server in-process /
         tests that poke ``audit_state._state`` directly. Cheap.
      2. **Disk mirror** — ``{shared_dir}/security/audit-cache.json``
         written by the admin server. Canonical cross-process path
         since the disk move in Sprint 2b — no RPC needed.
      3. **HTTP fetch** from the admin server's ``GET
         /api/security/audit``. Defense-in-depth fallback for the
         pre-Sprint-2b code path; useful when the disk mirror is
         missing because the admin server hasn't run a sweep yet.
    """
    # Path 1: in-process module-global. Hits in tests + any future
    # in-process invocation; misses in production (different process).
    data, cached_at = _read_in_process()
    source = "in_process" if data else None
    http_error: str | None = None

    # Path 2: disk mirror. The canonical cross-process path after
    # Sprint 2b. Both reader and writer just touch a file on the
    # shared dir; no RPC + no in-memory coupling.
    if not data:
        disk_data, disk_cached_at = _read_from_disk(shared_dir)
        if disk_data:
            data = disk_data
            cached_at = disk_cached_at
            source = "disk"

    # Path 3: HTTP fetch from the admin server. Kept as defense-in-
    # depth — if the disk mirror is missing (admin server hasn't
    # finished its first sweep, or shared_dir mismatch), the HTTP
    # endpoint still reports the server's in-memory state.
    if not data:
        http_data, http_cached_at, http_error = _read_via_http(network_path)
        if http_data:
            data = http_data
            cached_at = http_cached_at
            source = "admin_server"
            http_error = None  # don't surface error when fallback succeeded

    # Filter to one bot if requested.
    if bot_id:
        result = data.get(bot_id)
        if result is None:
            # Two real cases here, collapsed into one error string for
            # the model's benefit. (1) unknown bot id, (2) no audit
            # data for this bot yet — covers both "bot doesn't exist"
            # AND "admin restarted, audits haven't re-gathered yet".
            err_parts = [
                f"no cached audit for '{bot_id}' yet",
            ]
            if http_error:
                err_parts.append(f"admin-server fetch: {http_error}")
            else:
                err_parts.append(
                    "(admin server may have restarted; audits gather on a "
                    "background schedule — try `Run Audit` on the "
                    "Security page if this is urgent)"
                )
            return {
                "count": 0,
                "audits": [],
                "stale": not data,
                "source": source or "none",
                "error": " — ".join(err_parts),
            }
        return {
            "count": 1,
            "audits": [_project_audit(bot_id, result)],
            "stale": False,
            "source": source or "admin_server",
            "cached_at": cached_at,
        }

    audits = [_project_audit(bid, res) for bid, res in sorted(data.items())]
    out: dict[str, Any] = {
        "count": len(audits),
        "audits": audits,
        "stale": not data,
        "source": source or "none",
        "cached_at": cached_at,
    }
    if http_error and not data:
        # Both paths failed — surface the HTTP error so the model can
        # explain "audit isn't reachable" rather than report empty
        # results as if the pod just happened to be perfectly clean.
        out["error"] = http_error
    return out


TOOL = Tool(
    name="pod_state.audit",
    description=(
        "Return the most recent OC security-audit results per bot. "
        "Each entry has critical/warn/info counts and up to 10 findings "
        "(severity-sorted). Filter to one bot with bot_id. "
        "Reads from the admin server's audit cache — primarily via "
        "a disk mirror at {shared_dir}/security/audit-cache.json "
        "(same source the Security page renders from). When the "
        "admin server has just restarted and no audit has run yet, "
        "returns stale=true and count=0 — the bot should tell the "
        "operator audits aren't gathered yet rather than report clean. "
        "The ``source`` field in the response says how the data was "
        "fetched (``disk``, ``admin_server``, ``in_process``, or "
        "``none``). Use this for 'is personal_bot's audit clean?', 'which "
        "bots have critical findings?', or before recommending a fix "
        "that depends on audit state."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": (
                    "Restrict to one bot's audit result. Omit to "
                    "include every bot present in the cache."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "audit"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(TOOL)
