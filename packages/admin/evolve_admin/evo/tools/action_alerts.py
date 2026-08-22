"""Action tools — Reports / Alerts page surface gaps.

Closes four of the remaining gaps from
``docs/audit-evo-tool-coverage-2026-06-02.md`` that target the
Reports → Alerts / Subscriptions surface:

* ``action.subscriptions.test(key)`` — POST
  ``/api/alerts/subscriptions/test`` to send a sample message for one
  catalog event_key. The same code path the **Test** button on the
  Subscriptions tab uses. Operator says "send me a test alert for
  security.config_drift" → fires a real message through the dispatcher
  so wiring (channel auth, routing, formatting) is verified end-to-end.

* ``action.subscriptions.reset()`` — POST
  ``/api/alerts/subscriptions/reset`` to wipe every operator override.
  Mirrors the **Reset to defaults** button. The audit's high-priority
  follow-up to PR #1979/#1982: subscription-state-aware writes shipped
  but the bulk "remove all my overrides" path was awkward (operator had
  to re-call ``action.subscriptions.set`` for each event).

* ``action.alerts.set_digest_hour(hour)`` — POST
  ``/api/alerts/digest-hour`` to set the local hour the digest flusher
  fires. Body ``{"hour": 0..23}`` interpreted via ``network.timezone``.
  Takes effect on the next hourly tick, no daemon restart.

* ``action.alerts.send_test_report()`` — POST
  ``/api/reports-alerts/send-test`` to trigger a real pod_report.py run
  and dispatch. Diagnostic — the **Send test alert** button on the
  Reports → Alerts → Configure subtab. Useful when an operator says
  "send the morning briefing now" outside the daily cron.

All four route through admin-ui's HTTP surface (same shape PR #1982
established for ``action.subscriptions.set``) so the evo MCP gateway
honors the ``EVO_WRITE_SHARED_SUBDIRS = ("proposals", "signals")``
contract — ``{shared_dir}/alerts/`` stays evolve-only.

Risk tiers:
  * ``action.subscriptions.test`` — ``write_safe``. Sends a real
    message to the operator's configured channel, no on-disk state
    change. Reversible (just ignore it).
  * ``action.subscriptions.reset`` — ``write_safe``. Wipes overrides
    back to catalog defaults; operator can re-flip individual keys.
    Logged in subscriptions-audit.jsonl by the admin-ui route.
  * ``action.alerts.set_digest_hour`` — ``write_safe``. Single pod-wide
    integer; trivially reversible.
  * ``action.alerts.send_test_report`` — ``write_safe``. Sends a real
    pod report; bounded blast radius.
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


# Transport seam — tests inject a stub. Production: stdlib urllib.
PostJSONFn = Callable[
    [str, dict[str, Any], int],
    tuple[int, "dict[str, Any] | None", "str | None"],
]


def _real_post_json(
    url: str, body: dict[str, Any], timeout: int = 10,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Production transport. POST a JSON body, return
    ``(status_code, payload, error)``. On non-2xx, returns
    ``(status, parsed_body, None)``. On network failure,
    ``(0, None, error_str)``."""
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
    """Resolve the admin-ui base URL. Returns ``(base_url, error)`` —
    exactly one is non-None."""
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
        return resolve_admin_base_url(net).rstrip("/"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not resolve admin base URL: {exc}"


# ─── action.subscriptions.test ───────────────────────────────────────────────


def _test_handler(
    key: str,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Fire a sample message for ``key``. Calls
    ``POST /api/alerts/subscriptions/test`` with
    ``{"event_key": key}``. The dispatcher renders the catalog's
    sample_payload and routes through the operator's configured
    channel."""
    if not key:
        return {"ok": False, "error": "key is required"}

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/alerts/subscriptions/test"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(
        post_url, {"event_key": key}, 15,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the test"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {
            "ok": False, "error": err_msg, "http_status": status,
            "key": key,
        }

    return {
        "ok": True,
        "key": key,
        "channel": (payload or {}).get("channel"),
        "result": (payload or {}).get("result"),
        "rendered": (payload or {}).get("rendered"),
        "error": (payload or {}).get("error"),
        "message": (
            f"Test message for '{key}' dispatched — check the operator's "
            f"configured channel."
        ),
    }


def _test_validate(
    key: str, network_path: Path | None = None,
) -> dict[str, Any]:
    if not key or not isinstance(key, str):
        return {"ok": False, "reason": "`key` is required"}
    try:
        from evolve_admin.alerts import catalog as _cat
    except Exception:  # noqa: BLE001
        return {"ok": True, "context": {"key": key}}
    resolved = _cat.LEGACY_KEY_MIGRATION.get(key, key)
    entry = _cat.by_key(resolved)
    if entry is None:
        return {
            "ok": False,
            "reason": (
                f"unknown subscription key {key!r}; call "
                f"pod_state.subscriptions first to discover valid keys"
            ),
        }
    return {"ok": True, "context": {"key": resolved}}


TEST_TOOL = Tool(
    name="action.subscriptions.test",
    description=(
        "Send a sample message for one catalog event_key to verify "
        "channel wiring. Same code path as the **Test** button on "
        "Reports → Subscriptions. The dispatcher renders the catalog's "
        "sample_payload, prepends a clear test marker, and routes "
        "through the operator's configured channel WITHOUT applying "
        "subscription gating (source-level toggles still apply, though "
        "— a SUPPRESSED_DISABLED result means the operator turned the "
        "source off as a kill-switch). Use when the operator says "
        "'send me a test alert for X', 'is my Slack wiring working?', "
        "or 'try sending the security.config_drift alert'. Returns the "
        "dispatcher's outcome (channel + result code + rendered text)."
    ),
    wire_description=(
        "Send a sample message for one catalog event_key to verify "
        "channel wiring end-to-end. Use when the operator says 'send "
        "me a test alert for X' or 'is my Slack wiring working?'. "
        "Subscription gating is bypassed but source-level toggles "
        "still apply (SUPPRESSED_DISABLED = source turned off). "
        "Returns channel + result code + rendered text."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Catalog event key in dotted form (e.g. "
                    "'security.config_drift'). Discover via "
                    "pod_state.subscriptions."
                ),
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    handler=_test_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_test_validate,
    tags=("action", "subscriptions", "alerts", "test"),
)


register(TEST_TOOL)


# ─── action.subscriptions.reset ──────────────────────────────────────────────


def _reset_handler(
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Wipe every operator override. Calls
    ``POST /api/alerts/subscriptions/reset`` with an empty body. The
    route's ``_subs.reset(shared_dir)`` zeroes the overrides map and
    leaves every event back at its catalog default."""
    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/alerts/subscriptions/reset"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(post_url, {}, 10)
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the reset"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {"ok": False, "error": err_msg, "http_status": status}

    # Count overrides cleared from the returned payload shape — the
    # route returns the full subscriptions payload after reset; if the
    # overrides list is empty (the default), we report success.
    rows = (payload or {}).get("rows") or []
    return {
        "ok": True,
        "message": (
            "All operator subscription overrides cleared — every alert "
            "is back at its catalog default."
        ),
        "rows_count": len(rows) if isinstance(rows, list) else None,
        "verify_via": {
            "tool": "pod_state.subscriptions",
            "args": {},
            "expect": (
                "every row's is_overridden is False; effective state "
                "matches each catalog entry's default_enabled / "
                "default_frequency"
            ),
        },
    }


def _reset_validate(network_path: Path | None = None) -> dict[str, Any]:
    return {"ok": True}


RESET_TOOL = Tool(
    name="action.subscriptions.reset",
    description=(
        "Wipe every operator subscription override — sets every alert "
        "back to its catalog default. Same code path as the **Reset to "
        "defaults** button on Reports → Subscriptions. Use when the "
        "operator says 'reset my alert preferences', 'I overrode too "
        "much, start fresh', or 'undo all my subscription tweaks'. "
        "Reversible per-event via action.subscriptions.set. Returns "
        "ok=true plus a verify_via pointer."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_reset_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_reset_validate,
    tags=("action", "subscriptions", "alerts", "reset"),
)


register(RESET_TOOL)


# ─── action.alerts.set_digest_hour ───────────────────────────────────────────


def _digest_hour_handler(
    hour: int,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Set the local hour at which the digest flusher fires."""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"hour must be an integer 0..23; got {hour!r}",
        }
    if h < 0 or h > 23:
        return {
            "ok": False,
            "error": f"hour must be in 0..23 (local time); got {h}",
        }

    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/alerts/digest-hour"

    transport = post_json if post_json is not None else _real_post_json
    status, payload, transport_err = transport(
        post_url, {"hour": h}, 10,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the digest-hour update"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {"ok": False, "error": err_msg, "http_status": status}

    return {
        "ok": True,
        "hour": h,
        "applied": (payload or {}).get("applied", h),
        "message": (
            f"Digest hour set to {h:02d}:00 local time. Takes effect on "
            f"the next hourly tick — no daemon restart needed."
        ),
    }


def _digest_hour_validate(
    hour: int, network_path: Path | None = None,
) -> dict[str, Any]:
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return {"ok": False, "reason": f"hour must be an integer; got {hour!r}"}
    if h < 0 or h > 23:
        return {"ok": False, "reason": f"hour must be 0..23; got {h}"}
    return {"ok": True, "context": {"hour": h}}


DIGEST_HOUR_TOOL = Tool(
    name="action.alerts.set_digest_hour",
    description=(
        "Set the local hour at which the alerts digest flushes (the "
        "daily/weekly digest cadence for events whose frequency is set "
        "to daily_digest or weekly_digest). Interpreted via "
        "network.timezone. Takes effect on the next hourly tick — no "
        "daemon restart needed. Same code path as the **Save** button "
        "on Reports → Alerts → Configure → Digest hour. Use when the "
        "operator says 'move the digest to 8am', 'I want morning digests "
        "at 7', etc. Single pod-wide integer; trivially reversible."
    ),
    wire_description=(
        "Set the local hour (0..23, via network.timezone) at which "
        "the daily/weekly alert digests flush. Use when the operator "
        "says 'move the digest to 8am'. Takes effect on the next "
        "hourly tick — no restart needed. Pod-wide; trivially "
        "reversible."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "hour": {
                "type": "integer",
                "minimum": 0,
                "maximum": 23,
                "description": (
                    "Local hour (0..23) at which the digest fires. "
                    "Interpreted via network.timezone."
                ),
            },
        },
        "required": ["hour"],
        "additionalProperties": False,
    },
    handler=_digest_hour_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_digest_hour_validate,
    tags=("action", "alerts", "digest"),
)


register(DIGEST_HOUR_TOOL)


# ─── action.alerts.send_test_report ──────────────────────────────────────────


def _send_test_report_handler(
    real_send: bool = True,
    network_path: Path | None = None,
    *,
    post_json: PostJSONFn | None = None,
    post_url: str | None = None,
) -> dict[str, Any]:
    """Trigger ``pod_report.py --force`` to send a real report.

    Same diagnostic path the **Send test alert** button on the
    Reports → Alerts → Configure subtab uses. Set ``real_send=False`` to
    use ``--dry-run`` (returns the preview without dispatching)."""
    if post_url is None:
        base_url, err = _resolve_base_url(network_path)
        if err:
            return {"ok": False, "error": err}
        post_url = f"{base_url}/api/reports-alerts/send-test"

    transport = post_json if post_json is not None else _real_post_json
    # Note: 60s timeout — pod_report.py runs a real LLM call.
    status, payload, transport_err = transport(
        post_url, {"real_send": bool(real_send)}, 60,
    )
    if transport_err is not None:
        return {"ok": False, "error": transport_err}

    if status < 200 or status >= 300:
        err_msg = "admin server rejected the send-test"
        if isinstance(payload, dict) and payload.get("error"):
            err_msg = str(payload["error"])
        return {"ok": False, "error": err_msg, "http_status": status}

    return {
        "ok": True,
        "sent": (payload or {}).get("sent", False),
        "preview": (payload or {}).get("preview", ""),
        "overall": (payload or {}).get("overall"),
        "real_send": bool(real_send),
        "error": (payload or {}).get("error"),
        "message": (
            "Test report dispatched via pod_report.py."
            if real_send else
            "Test report previewed (dry-run, not dispatched)."
        ),
    }


def _send_test_report_validate(
    real_send: bool = True,
    network_path: Path | None = None,
) -> dict[str, Any]:
    return {"ok": True}


SEND_TEST_REPORT_TOOL = Tool(
    name="action.alerts.send_test_report",
    description=(
        "Trigger pod_report.py to generate and dispatch a real test "
        "report (the morning briefing path). Same code path as the "
        "**Send test alert** button on Reports → Alerts → Configure. "
        "Use when the operator says 'send the morning briefing now', "
        "'fire a test pod report', or 'I want to see what tomorrow's "
        "report would look like' (with real_send=False for the "
        "preview-only path). Returns the preview text plus delivery "
        "status. NOTE: dispatches via real channel routing — use "
        "real_send=False if the operator wants the content WITHOUT "
        "messaging anyone."
    ),
    wire_description=(
        "Trigger pod_report.py to generate and dispatch a real test "
        "report (the morning-briefing path). Use when the operator "
        "says 'send the morning briefing now' or 'fire a test pod "
        "report'. Dispatches via real channel routing — pass "
        "real_send=false to preview the content WITHOUT messaging "
        "anyone."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "real_send": {
                "type": "boolean",
                "description": (
                    "True (default) → dispatch through the configured "
                    "channel. False → dry-run; return the preview only, "
                    "no channel send."
                ),
            },
        },
        "additionalProperties": False,
    },
    handler=_send_test_report_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_send_test_report_validate,
    tags=("action", "alerts", "report", "test"),
)


register(SEND_TEST_REPORT_TOOL)


# ─── Closure factory for shared_dir / network_path binding ──────────────────


def init_for_shared_dir(
    shared_dir: Path, network_path: Path | None = None,
) -> None:
    """Rebind handlers with shared_dir + network_path. Mirrors the
    pod_state.* tools' wiring pattern."""
    def _test_h(**kwargs: Any) -> dict[str, Any]:
        return _test_handler(network_path=network_path, **kwargs)

    def _reset_h(**kwargs: Any) -> dict[str, Any]:
        return _reset_handler(network_path=network_path, **kwargs)

    def _digest_h(**kwargs: Any) -> dict[str, Any]:
        return _digest_hour_handler(network_path=network_path, **kwargs)

    def _send_test_h(**kwargs: Any) -> dict[str, Any]:
        return _send_test_report_handler(network_path=network_path, **kwargs)

    register(Tool(
        name=TEST_TOOL.name,
        description=TEST_TOOL.description,
        wire_description=TEST_TOOL.wire_description,
        input_schema=TEST_TOOL.input_schema,
        handler=_test_h,
        risk_tier=TEST_TOOL.risk_tier,
        validate=lambda **kw: _test_validate(network_path=network_path, **kw),
        tags=TEST_TOOL.tags,
    ))
    register(Tool(
        name=RESET_TOOL.name,
        description=RESET_TOOL.description,
        wire_description=RESET_TOOL.wire_description,
        input_schema=RESET_TOOL.input_schema,
        handler=_reset_h,
        risk_tier=RESET_TOOL.risk_tier,
        validate=lambda **kw: _reset_validate(network_path=network_path, **kw),
        tags=RESET_TOOL.tags,
    ))
    register(Tool(
        name=DIGEST_HOUR_TOOL.name,
        description=DIGEST_HOUR_TOOL.description,
        wire_description=DIGEST_HOUR_TOOL.wire_description,
        input_schema=DIGEST_HOUR_TOOL.input_schema,
        handler=_digest_h,
        risk_tier=DIGEST_HOUR_TOOL.risk_tier,
        validate=lambda **kw: _digest_hour_validate(network_path=network_path, **kw),
        tags=DIGEST_HOUR_TOOL.tags,
    ))
    register(Tool(
        name=SEND_TEST_REPORT_TOOL.name,
        description=SEND_TEST_REPORT_TOOL.description,
        wire_description=SEND_TEST_REPORT_TOOL.wire_description,
        input_schema=SEND_TEST_REPORT_TOOL.input_schema,
        handler=_send_test_h,
        risk_tier=SEND_TEST_REPORT_TOOL.risk_tier,
        validate=lambda **kw: _send_test_report_validate(
            network_path=network_path, **kw,
        ),
        tags=SEND_TEST_REPORT_TOOL.tags,
    ))
