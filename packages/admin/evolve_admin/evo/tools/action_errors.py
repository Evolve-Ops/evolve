"""Action tool — Settings → Errors page dismiss action.

Closes one of the remaining gaps from
``docs/audit-evo-tool-coverage-2026-06-02.md``:

* ``action.errors.dismiss(snooze_minutes?)`` — POST
  ``/api/report/dismiss``. Same code path as the **Dismiss** button on
  the Errors page banner. Marks all current errors as seen, with an
  optional snooze before they can re-fire. Default 5-minute snooze
  matches the admin-ui's UI default — keeps the banner from immediately
  re-firing on the next poll if the same recurring error is still
  happening.

The matching **Execute remediation** button (``POST
/api/admin/remediation/execute``) is intentionally not wrapped here —
the remediation dispatcher needs a ``kind`` + ``params`` shape that's
handler-specific (already enumerated in
``packages/admin/evolve_admin/remediation/handlers.py::HANDLERS``); a
proper ``action.errors.remediate`` tool wants a per-kind validation
gate. Deferred to a follow-up PR; see the audit doc's medium-priority
``action.errors.{remediate,dismiss}`` entry for the partial close.

Routes through admin-ui's HTTP surface so the evo gateway honors the
``EVO_WRITE_SHARED_SUBDIRS`` contract; error-reporter state lives
under ``{shared_dir}/`` and is owned by evolve.

Risk tier: ``write_safe``. Dismissal is reversible via the matching
``POST /api/report/reset-dismissed`` (separate UI button), and the
errors themselves re-fire after the snooze if the underlying condition
persists. Bounded blast radius: operator's own banner state.
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
    """Production transport. See action_alerts._real_post_json for the
    shared shape; duplicated here to avoid cross-module imports during
    Phase 1.5 (each tool module is self-contained on the HTTP path)."""
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


def _dismiss_handler(
    snooze_minutes: int = 5,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Mark all current errors as seen with an optional snooze."""
    try:
        s = int(snooze_minutes)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": (
                f"snooze_minutes must be an integer; got "
                f"{snooze_minutes!r}"
            ),
        }
    if s < 0:
        return {
            "ok": False,
            "error": f"snooze_minutes must be >= 0; got {s}",
        }

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/report/dismiss"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(
        post_url, {"snooze_minutes": s}, 10,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the dismiss"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {"ok": False, "error": err_msg, "http_status": status}

    return {
        "ok": True,
        "snooze_minutes": s,
        "dismissed_at": (payload or {}).get("dismissed_at"),
        "message": (
            f"Errors dismissed; banner will re-evaluate after "
            f"{s}-minute snooze."
            if s > 0 else
            "Errors dismissed; banner will re-evaluate on the next poll."
        ),
        "verify_via": {
            "tool": "pod_state.errors",
            "args": {},
            "expect": (
                "the errors list still contains the underlying "
                "errors but the banner state moves to 'dismissed'"
            ),
        },
    }


def _dismiss_validate(
    snooze_minutes: int = 5,
    network_path: Path | None = None,
) -> dict[str, Any]:
    try:
        s = int(snooze_minutes)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": (
                f"snooze_minutes must be an integer; got "
                f"{snooze_minutes!r}"
            ),
        }
    if s < 0:
        return {
            "ok": False,
            "reason": f"snooze_minutes must be >= 0; got {s}",
        }
    return {"ok": True, "context": {"snooze_minutes": s}}


DISMISS_TOOL = Tool(
    name="action.errors.dismiss",
    description=(
        "Mark all current errors as seen and snooze the error banner. "
        "Same code path as the **Dismiss** button on the Errors page. "
        "Default 5-minute snooze keeps the banner from immediately "
        "re-firing on the next poll if the same recurring error is "
        "still happening. Use when the operator says 'dismiss the "
        "error banner', 'hide errors for 10 minutes', or 'snooze the "
        "error alerts'. The errors themselves stay in pod_state.errors "
        "— this is a banner-state transition, not an error deletion. "
        "Set snooze_minutes=0 to dismiss without snoozing."
    ),
    wire_description=(
        "Mark all current errors as seen and snooze the error banner "
        "(default 5 min; snooze_minutes=0 dismisses without "
        "snoozing). Use for 'dismiss the error banner' / 'hide errors "
        "for N minutes'. Banner-state transition only — the errors "
        "themselves stay in pod_state.errors, nothing is deleted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "snooze_minutes": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Minutes before the banner can re-evaluate. "
                    "Default 5. Set 0 to dismiss without snoozing."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_dismiss_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_dismiss_validate,
    tags=("action", "errors", "dismiss"),
)


register(DISMISS_TOOL)


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind the handler with shared_dir + network_path."""
    def _h(**kwargs: Any) -> dict[str, Any]:
        return _dismiss_handler(network_path=network_path, **kwargs)

    register(Tool(
        name=DISMISS_TOOL.name,
        description=DISMISS_TOOL.description,
        wire_description=DISMISS_TOOL.wire_description,
        input_schema=DISMISS_TOOL.input_schema,
        handler=_h,
        risk_tier=DISMISS_TOOL.risk_tier,
        validate=lambda **kw: _dismiss_validate(
            network_path=network_path, **kw,
        ),
        tags=DISMISS_TOOL.tags,
    ))
