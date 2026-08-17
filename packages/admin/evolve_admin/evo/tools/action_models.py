"""Action tool — Improve → AI Optimization model freshness check.

Closes one of the remaining gaps from
``docs/audit-evo-tool-coverage-2026-06-02.md``:

* ``action.models.check_freshness()`` — POST
  ``/api/models/check-freshness``. Same code path as the **Check Now**
  button on Improve → AI Optimization. Scans every bot's tier config
  and compares against the RECOMMENDED registry, returning advisories
  for stale models, diversity gaps, and catalog drift.

Use when the operator says "check the model freshness now", "scan for
stale model assignments", or "did the upstream release land yet". The
daily check runs anyway via a heal-loop pass; this is the on-demand
button.

Routes through admin-ui's HTTP surface — the freshness scan iterates
every bot's openclaw.json (which evo can't read directly under the
account-separation contract; admin-ui owns the multi-bot iteration).

Risk tier: ``write_safe``. The route persists a summary to
``last_check.json`` so the UI's Improve page shows the new advisories;
no bot config touched, no LLM call required (pure registry comparison).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any, Callable

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 10,
) -> tuple[int, dict[str, Any] | None, str | None]:
    data = json.dumps(body).encode("utf-8")
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


def _resolve_base_url(
    network_path: Path | None,
) -> tuple[str | None, str | None]:
    if network_path is None:
        return None, (
            "admin base URL unavailable (no network_path injected — "
            "tool may be running outside the MCP bridge)"
        )
    try:
        from evolve_admin.config import load_network, resolve_admin_base_url
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    try:
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


def _check_freshness_handler(
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Trigger the pod-wide model freshness scan."""
    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/models/check-freshness"

    transport = post_json if post_json is not None else _real_post_json
    # 30s — iterates every bot, no LLM call, but reads OC config per bot.
    status, payload, transport_err = transport(post_url, {}, 30)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the freshness check"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {"ok": False, "error": err_msg, "http_status": status}

    advisories = (payload or {}).get("advisories") or []
    diversity = (payload or {}).get("diversity_advisories") or []
    drift = (payload or {}).get("drift_findings") or []
    return {
        "ok": True,
        "checked_at": (payload or {}).get("checked_at"),
        "advisory_count": (payload or {}).get(
            "advisory_count", len(advisories) if isinstance(advisories, list) else 0,
        ),
        "diversity_count": (
            len(diversity) if isinstance(diversity, list) else 0
        ),
        "drift_count": (payload or {}).get(
            "drift_count", len(drift) if isinstance(drift, list) else 0,
        ),
        "advisories": advisories,
        "diversity_advisories": diversity,
        "drift_findings": drift,
        "message": (
            "Model freshness check complete. Advisories: "
            f"{(payload or {}).get('advisory_count', 0)} stale, "
            f"{(payload or {}).get('drift_count', 0)} drift, "
            f"{len(diversity) if isinstance(diversity, list) else 0} diversity."
        ),
    }


def _check_freshness_validate(
    network_path: Path | None = None,
) -> dict[str, Any]:
    return {"ok": True}


CHECK_FRESHNESS_TOOL = Tool(
    name="action.models.check_freshness",
    description=(
        "Run a pod-wide scan of every bot's tier config against the "
        "RECOMMENDED model registry. Returns advisories for stale "
        "models (RECOMMENDED moved past what the bot is on), "
        "diversity gaps (only one credentialed LLM provider — fragile), "
        "and catalog drift (tier references a model not in catalog). "
        "Same code path as the **Check Now** button on Improve → AI "
        "Optimization → Model freshness. Use when the operator says "
        "'check model freshness', 'are any bots on stale models', or "
        "after a known upstream model release. Cheap — pure registry "
        "comparison, no LLM call. The daily heal pass runs the same "
        "scan anyway; this is the on-demand button."
    ),
    wire_description=(
        "Run a pod-wide scan of every bot's tier config against the "
        "RECOMMENDED model registry — returns advisories for stale "
        "models, provider-diversity gaps, and catalog drift. Use for "
        "'check model freshness' or after an upstream model release. "
        "Cheap: pure registry comparison, no LLM call; same as the "
        "Check Now button on Improve → AI Optimization."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_check_freshness_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_check_freshness_validate,
    tags=("action", "models", "freshness"),
)


register(CHECK_FRESHNESS_TOOL)


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    def _h(**kwargs: Any) -> dict[str, Any]:
        return _check_freshness_handler(network_path=network_path, **kwargs)

    register(Tool(
        name=CHECK_FRESHNESS_TOOL.name,
        description=CHECK_FRESHNESS_TOOL.description,
        wire_description=CHECK_FRESHNESS_TOOL.wire_description,
        input_schema=CHECK_FRESHNESS_TOOL.input_schema,
        handler=_h,
        risk_tier=CHECK_FRESHNESS_TOOL.risk_tier,
        validate=lambda **kw: _check_freshness_validate(
            network_path=network_path, **kw,
        ),
        tags=CHECK_FRESHNESS_TOOL.tags,
    ))
