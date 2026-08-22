"""Action tool — umbrella for one-shot pod-wide scans / refreshes.

Exposes one tool:

* ``action.scan.run(scope, kind)`` — dispatch a one-shot trigger to
  the matching ``POST /api/<thing>`` route on admin-ui. Closes the
  bulk of Pattern A in
  ``docs/audit-evo-tool-coverage-2026-06-02.md``: the "rescan", "force
  refresh", and "run me now" buttons that pepper the Plugins, Security,
  Maintenance, and Reports pages, all of which previously had no evo
  tool. Operators say "rescan the integrations", "force a fresh alert
  pass", "refresh recommendations" — the natural shape is one tool
  with two enum arguments rather than nine identical wrappers.

Why HTTP, not direct producer invocation: the producers
(integration_probe, plugin monitor, content scanner, etc.) write to
the Signal store + various inventory caches under ``{shared_dir}/``,
some of which are evolve-only (``alerts/``, ``profiles/``,
``observations/`` per deploy.py's ``EVO_WRITE_SHARED_SUBDIRS``
contract). Even when a producer's outputs are co-owned, the admin
server already has the producer entrypoints wired (with ACL, locking,
and error envelopes); duplicating that wiring inside evo would drift
out of sync. Same pattern as PR #1982 for ``action.subscriptions.set``:
the evo gateway POSTs to admin-ui, which (running as ``evolve``) owns
the write.

Risk tier: ``write_safe`` — each dispatch re-runs a producer that has
its own dedup / lock / sweep semantics. Re-running is idempotent and
bounded by the producer's wall-clock budget (typically 5–30s). No
config writes, no secret access, no destructive side effects. The
classification matches what the recommendation §3 of the audit
("Authorization-tier classification → anyone but throttled") would
allow.

Dispatch table is the source of truth — adding a new (scope, kind)
pair is a one-line change here plus a row in AGENTS.md's ops table.
Unknown combos return a clear error enumerating valid options so the
model can self-correct without paging the operator.

Spec: ``docs/audit-evo-tool-coverage-2026-06-02.md`` §Pattern A.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from evolve_util import now_iso as _utc_now_iso

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Transport seam — tests inject a stub here to avoid hitting localhost.
# Signature: (url, body_dict, timeout) -> (status_code, response_body_dict | None, error_str | None)
PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


# ─── Dispatch table ─────────────────────────────────────────────────────────
#
# Each entry maps a (scope, kind) pair onto the admin-ui route that
# implements the matching button in the UI. ``path_template`` is a
# string the dispatcher .format()s with the bound args (``bot_id`` for
# per-bot scopes). ``human`` is the operator-facing label echoed in the
# response so the model can confirm to the operator what landed.

_SCAN_DISPATCH: dict[tuple[str, str], dict[str, str]] = {
    # ── Pattern A gaps from docs/audit-evo-tool-coverage-2026-06-02.md ──
    #
    # Three "refresh a producer" gaps:
    ("pod", "recommendations"): {
        "method": "POST",
        "path_template": "/api/better/refresh",
        "human": "Better Engine recommendations refresh",
    },
    ("pod", "signals"): {
        "method": "POST",
        "path_template": "/api/signals/refresh",
        "human": "live alert producers re-run (integration_probe, host_health, error_reporter, pod_health)",
    },
    ("pod", "infra_audit"): {
        "method": "POST",
        "path_template": "/api/pod-health/infra-audit/run",
        "human": "infra audit pass",
    },
    # Six "re-scan" gaps — these are the six routes the Plugins /
    # Security / Reports pages' ↻ Re-scan buttons hit. Each fans out
    # across all bots, writes inventory caches, and emits/sweeps
    # Signals just like the audit-driven run.
    ("pod", "integrations"): {
        "method": "POST",
        "path_template": "/api/integrations/scan",
        "human": "pod-wide integration scan",
    },
    ("pod", "plugins"): {
        "method": "POST",
        "path_template": "/api/plugins-admin/scan",
        "human": "pod-wide plugin posture scan",
    },
    ("pod", "mcp"): {
        "method": "POST",
        "path_template": "/api/mcp-admin/scan",
        "human": "pod-wide MCP-server scan",
    },
    ("pod", "content"): {
        "method": "POST",
        "path_template": "/api/content-scan/scan",
        "human": "pod-wide content (secret/PII) scan",
    },
    ("pod", "permissions"): {
        "method": "POST",
        "path_template": "/api/permissions/scan",
        "human": "pod-wide permission baseline scan",
    },
    ("pod", "hooks"): {
        "method": "POST",
        "path_template": "/api/hooks-admin/scan",
        "human": "pod-wide hook posture scan",
    },
    # ── Per-bot scope ───────────────────────────────────────────────
    #
    # ``action.bot.rescan_apps`` already exists for the per-bot
    # applications scan — we also surface it under the umbrella so the
    # natural-language path ("rescan apps on team-bot-a") resolves
    # under one tool. The two share the same route; the per-bot
    # applications scan endpoint takes a ?bot= query string.
    ("bot", "applications"): {
        "method": "POST",
        "path_template": "/api/applications/scan?bot={bot_id}",
        "human": "per-bot applications scan",
    },
}


# Sentinel for the per-bot scope: any "bot:<id>" prefix matches "bot".
_BOT_SCOPE_PREFIX = "bot:"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 30,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Production transport. POST a JSON body, return
    ``(status_code, payload, error)``. Longer default timeout than
    action_subscriptions because some producers (content scan, infra
    audit) routinely run 15-25s before responding.

    On non-2xx, returns ``(status, parsed_body_if_any, None)`` so the
    caller can surface the route's error envelope verbatim. On
    network failure, returns ``(0, None, error_str)``.
    """
    data = json.dumps(body).encode("utf-8") if body else b""
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen_admin(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        parsed: dict[str, Any] | None = None
        if err_body:
            try:
                obj = json.loads(err_body)
                if isinstance(obj, dict):
                    parsed = obj
            except json.JSONDecodeError:
                parsed = {"raw": err_body[:500]}
        return exc.code, parsed, None
    except urllib.error.URLError as exc:
        return 0, None, f"admin server unreachable at {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return 0, None, f"HTTP POST failed: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return status, None, f"admin server returned non-JSON: {exc}"
    if not isinstance(payload, dict):
        return status, None, "admin server returned unexpected payload shape"
    return status, payload, None


def _resolve_base_url(network_path: Path | None) -> tuple[str | None, str | None]:
    """Resolve the admin-ui base URL (no trailing slash).

    Returns ``(base_url, error)`` — exactly one is non-None. Mirrors
    the sibling pattern in ``action_subscriptions._resolve_post_url``
    but without baking in a specific path so the dispatcher can append
    the route-specific tail.
    """
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import (
            load_network,
            resolve_admin_base_url,
        )
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        base_url = resolve_admin_base_url(net).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"
    return base_url, None


def _normalize_scope(scope: str) -> tuple[str, str | None]:
    """Map operator-supplied scope to a dispatch key + optional bot_id.

    Accepts ``"pod"`` and ``"bot:<id>"``. Returns ``(scope_key,
    bot_id_or_none)``. Caller is responsible for handling an unknown
    scope (we return ``("", None)`` and let dispatch surface the
    helpful error).
    """
    if scope == "pod":
        return "pod", None
    if scope.startswith(_BOT_SCOPE_PREFIX):
        bot_id = scope[len(_BOT_SCOPE_PREFIX):].strip()
        if bot_id:
            return "bot", bot_id
    return "", None


def _valid_options_summary() -> dict[str, list[str]]:
    """Render the dispatch table as a "here's what I can do" map.

    Used in error envelopes so the model can self-correct without
    fetching the catalog separately.
    """
    by_scope: dict[str, list[str]] = {}
    for (scope, kind), _ in _SCAN_DISPATCH.items():
        by_scope.setdefault(scope, []).append(kind)
    for scope in by_scope:
        by_scope[scope] = sorted(by_scope[scope])
    return by_scope


# ─── Handler ─────────────────────────────────────────────────────────────────


def _handler(
    scope: str,
    kind: str,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Dispatch a single (scope, kind) trigger to admin-ui.

    Local steps (no HTTP needed): scope/kind validation against the
    dispatch table, base-URL resolution. HTTP step: POST to
    ``{base_url}{path_template}`` so admin-ui (running as the evolve
    user) owns the producer invocation.

    Test seam: pass ``post_json`` to override the urllib transport
    and/or ``base_url`` to skip network.json resolution.
    """
    if not isinstance(scope, str) or not scope.strip():
        return {
            "ok": False,
            "error": "`scope` is required (e.g. 'pod' or 'bot:<id>')",
            "valid_options": _valid_options_summary(),
        }
    if not isinstance(kind, str) or not kind.strip():
        return {
            "ok": False,
            "error": (
                f"`kind` is required. Valid kinds for scope={scope!r}: "
                f"{_valid_options_summary().get(scope, [])}"
            ),
            "valid_options": _valid_options_summary(),
        }

    scope_key, bot_id = _normalize_scope(scope)
    if not scope_key:
        return {
            "ok": False,
            "error": (
                f"unknown scope {scope!r}. Valid scopes: 'pod' or "
                f"'bot:<bot_id>'."
            ),
            "valid_options": _valid_options_summary(),
        }

    entry = _SCAN_DISPATCH.get((scope_key, kind))
    if entry is None:
        valid_for_scope = sorted(
            k for (s, k) in _SCAN_DISPATCH if s == scope_key
        )
        return {
            "ok": False,
            "error": (
                f"unknown (scope, kind) = ({scope!r}, {kind!r}). "
                f"Valid kinds for scope='{scope_key}': {valid_for_scope}"
            ),
            "valid_options": _valid_options_summary(),
        }

    # Per-bot dispatch requires a bot_id to format into the path.
    try:
        if scope_key == "bot":
            # urlencode the bot_id so a stray apostrophe / slash can't
            # break the path; bot ids are normally lowercase letters
            # but be defensive.
            quoted = urllib.parse.quote(bot_id or "", safe="")
            path = entry["path_template"].format(bot_id=quoted)
        else:
            path = entry["path_template"]
    except KeyError as exc:
        # Defensive — would mean a template referenced a placeholder
        # the handler didn't supply.
        return {
            "ok": False,
            "error": f"internal: dispatch template missing arg {exc}",
        }

    # Resolve admin-ui base URL (unless the caller supplied one — test
    # seam).
    if base_url is None:
        base_url, url_err = _resolve_base_url(network_path)
        if url_err:
            return {"ok": False, "error": url_err}

    url = f"{base_url}{path}"
    transport = post_json if post_json is not None else _real_post_json
    # 60s ceiling — generous because content scan + infra audit are
    # the slowest producers and a 5s budget would routinely time out.
    status, payload, transport_err = transport(url, {}, 60)
    if transport_err is not None:
        return {
            "ok": False,
            "error": transport_err,
            "scope": scope,
            "kind": kind,
        }

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the scan trigger"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        result: dict[str, Any] = {
            "ok": False,
            "error": err_msg,
            "http_status": status,
            "scope": scope,
            "kind": kind,
        }
        if isinstance(payload, dict):
            result["http_body"] = payload
        return result

    # Success — surface the producer's response (each route returns a
    # different shape: signals/refresh returns per-monitor outcomes;
    # plugin/permissions/hooks/mcp/content scan returns a summary with
    # bots_checked + findings_count; better/refresh returns
    # recommendations[]). The model gets the raw payload back so it
    # can quote what landed.
    return {
        "ok": True,
        "scope": scope,
        "kind": kind,
        "message": f"Triggered {entry['human']}.",
        "ran_at": _utc_now_iso(),
        "result": payload if isinstance(payload, dict) else {},
    }


# ─── Validate ────────────────────────────────────────────────────────────────


def _validate(
    scope: str,
    kind: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Pre-flight gate. Catches obvious mistakes (unknown scope,
    unknown kind, missing bot_id) before the proxy renders a confirm
    button. Same shape contract as the rest of the validate family —
    returns ``{ok: True}`` on pass and ``{ok: False, reason: str}``
    otherwise.
    """
    if not isinstance(scope, str) or not scope.strip():
        return {"ok": False, "reason": "`scope` is required"}
    if not isinstance(kind, str) or not kind.strip():
        return {"ok": False, "reason": "`kind` is required"}

    scope_key, bot_id = _normalize_scope(scope)
    if not scope_key:
        return {
            "ok": False,
            "reason": (
                f"unknown scope {scope!r}; valid scopes are 'pod' or "
                f"'bot:<bot_id>'"
            ),
        }
    if scope_key == "bot" and not bot_id:
        return {
            "ok": False,
            "reason": (
                "`scope` 'bot:' must include a bot id (e.g. "
                "'bot:team-bot-a')"
            ),
        }
    if (scope_key, kind) not in _SCAN_DISPATCH:
        valid_for_scope = sorted(
            k for (s, k) in _SCAN_DISPATCH if s == scope_key
        )
        return {
            "ok": False,
            "reason": (
                f"unknown kind {kind!r} for scope='{scope_key}'; "
                f"valid: {valid_for_scope}"
            ),
        }
    return {
        "ok": True,
        "context": {
            "scope": scope_key,
            "kind": kind,
            "bot_id": bot_id,
            "human": _SCAN_DISPATCH[(scope_key, kind)]["human"],
        },
    }


# ─── Tool definition ────────────────────────────────────────────────────────


_VALID_HINT = (
    "scope='pod' kinds: recommendations, signals, infra_audit, "
    "integrations, plugins, mcp, content, permissions, hooks. "
    "scope='bot:<id>' kinds: applications."
)


TOOL = Tool(
    name="action.scan.run",
    description=(
        "Umbrella tool for one-shot pod-wide producer / scan / refresh "
        "triggers. Closes Pattern A from "
        "docs/audit-evo-tool-coverage-2026-06-02.md: the ↻ Re-scan / "
        "Refresh / Run-now buttons on the Plugins, Security, Reports, "
        "Maintenance, and Recommendations pages. Takes a `scope` "
        "('pod' or 'bot:<id>') and a `kind` (one of the catalog "
        "below). Dispatches to the corresponding admin-ui POST route "
        "so the UI and evo share one trigger path. "
        + _VALID_HINT + " "
        "Operators say things like: 'rescan the integrations', "
        "'refresh recommendations', 'force a fresh alert pass', "
        "'rerun the content scan', 'rescan apps on team-bot-a'. "
        "Returns the route's own response payload so the model can "
        "report what changed (counts, sweep-resolved counts, "
        "advisory_count, etc.). Unknown (scope, kind) combinations "
        "return a clear error enumerating valid options."
    ),
    wire_description=(
        "Umbrella for one-shot pod-wide scan / refresh / run-now "
        "triggers (the ↻ Re-scan and Refresh buttons across the admin "
        "pages). " + _VALID_HINT + " Use for 'rescan the "
        "integrations' or 'refresh recommendations'. Returns the "
        "route's own response payload."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": (
                    "'pod' for pod-wide producers / scans, or "
                    "'bot:<bot_id>' for per-bot scans (e.g. "
                    "'bot:team-bot-a')."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Which producer to trigger. " + _VALID_HINT
                ),
            },
        },
        "required": ["scope", "kind"],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_validate,
    tags=("action", "scan", "refresh"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)


register(TOOL)


def make_handler(network_path: Path | None = None):
    """Bind ``network_path`` into a tool handler closure. Mirrors the
    pod_state.* tools' wiring pattern — admin server startup can
    pre-bind without forcing callers to thread network_path through.
    """
    def handler(**kwargs: Any) -> dict[str, Any]:
        return _handler(network_path=network_path, **kwargs)
    return handler


def make_validate(network_path: Path | None = None):
    def validate(**kwargs: Any) -> dict[str, Any]:
        return _validate(network_path=network_path, **kwargs)
    return validate


def init_for_network(network_path: Path | None = None) -> None:
    """Rebind handler + validate with a real network_path."""
    bound_handler = make_handler(network_path)
    bound_validate = make_validate(network_path)
    register(Tool(
        name=TOOL.name,
        description=TOOL.description,
        wire_description=TOOL.wire_description,
        input_schema=TOOL.input_schema,
        handler=lambda **kwargs: bound_handler(**kwargs),
        risk_tier=TOOL.risk_tier,
        validate=lambda **kwargs: bound_validate(**kwargs),
        tags=TOOL.tags,
    ))
