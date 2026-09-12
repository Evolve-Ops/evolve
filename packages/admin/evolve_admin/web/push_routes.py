"""HTTP routes for PWA push — PWA Phase 1.2 §6.2.

Endpoints the admin UI's "Enable on this device" / "Devices receiving
push" surfaces call:

  GET  /api/push/vapid-public-key
       Returns the per-pod VAPID public key (base64url, unpadded).
       Frontend feeds it to ``pushManager.subscribe`` as
       ``applicationServerKey``. Lazy-init the keypair on first call.

  POST /api/push/subscribe
       Body: ``{endpoint, keys: {p256dh, auth}, device_label?, user_agent?}``
       Stores in ``{shared_dir}/push/subscriptions.json``. Idempotent
       on ``endpoint`` (re-subscribing the same browser refreshes the
       existing record rather than creating a duplicate). Returns
       ``{subscription_id}``.

  POST /api/push/unsubscribe
       Body: ``{subscription_id}``. Removes the row. 404 on unknown id
       so the UI can distinguish "removed it" from "already gone."

  GET  /api/push/subscriptions
       Full list for the Subscriptions tab's Devices section. Includes
       invalid (410-Gone-marked) rows so the operator can clean them.

  POST /api/push/test
       Body: ``{subscription_id?}``. Sends a one-off test push to a
       single device (or all if subscription_id is omitted). Lets the
       operator confirm a freshly-registered device actually receives.

These routes are sysadmin-only; per spec §1.2 and alert-subscriptions
§11, the PWA push surface is for the pod operator, not member-bot
users. The admin UI's existing authorization is presumed sufficient
(see feedback_ui_authorization_presumed in operator memory).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..alerts import subscriptions as _alert_subs
from ..config import load_network
from . import push_subscriptions as _subs
from . import push_vapid as _vapid

log = logging.getLogger(__name__)


def register_push_routes(app: Flask, network_path: Path) -> None:
    """Register the /api/push/* routes."""

    def _shared_dir() -> Path:
        return Path(
            load_network(network_path).get("sharedDir", "/Users/Shared/evolve")
        )

    # ── GET /api/push/vapid-public-key ───────────────────────────────────────

    @app.get("/api/push/vapid-public-key")
    def api_push_vapid_public_key() -> Response:
        try:
            pub = _vapid.get_public_key_b64url(_shared_dir())
        except OSError as exc:
            log.error("vapid keypair lazy-init failed: %s", exc)
            return jsonify({"error": f"vapid setup failed: {exc}"}), 500
        return jsonify({"public_key": pub})

    # ── POST /api/push/subscribe ─────────────────────────────────────────────

    @app.post("/api/push/subscribe")
    def api_push_subscribe() -> Response:
        body = request.get_json(silent=True) or {}
        endpoint = body.get("endpoint")
        keys = body.get("keys") or {}
        if not endpoint or not isinstance(endpoint, str):
            return jsonify({"error": "body must include endpoint (string)"}), 400
        if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
            return jsonify({
                "error": "body must include keys.p256dh and keys.auth",
            }), 400

        # Device label fallback chain: operator-supplied → UA-derived →
        # generic "Unnamed device". The storage layer also defaults if
        # we pass an empty string; we resolve here so the JSON response
        # echoes back what was actually stored.
        device_label = (body.get("device_label") or "").strip()
        user_agent = (body.get("user_agent") or request.headers.get("User-Agent") or "").strip()
        if not device_label:
            device_label = _label_from_user_agent(user_agent)

        sub = _subs.add_subscription(
            _shared_dir(),
            endpoint=endpoint,
            keys={"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
            device_label=device_label,
            user_agent=user_agent[:512],
        )
        # Default the operator's "Send to: PWA Push" channel to on the
        # first time a device subscribes — otherwise the operator
        # registers a phone and never sees a push because the dispatcher
        # still has pwa_push off. They can flip it off in the
        # Subscriptions UI if they don't want fanout.
        try:
            if not _alert_subs.read_pwa_push_enabled(_shared_dir()):
                _alert_subs.write_pwa_push_enabled(_shared_dir(), True)
        except OSError as exc:
            log.warning("could not auto-enable pwa_push channel: %s", exc)
        return jsonify({
            "subscription_id": sub.id,
            "device_label":    sub.device_label,
        })

    # ── POST /api/push/unsubscribe ───────────────────────────────────────────

    @app.post("/api/push/unsubscribe")
    def api_push_unsubscribe() -> Response:
        body = request.get_json(silent=True) or {}
        sid = body.get("subscription_id")
        if not sid or not isinstance(sid, str):
            return jsonify({
                "error": "body must include subscription_id (string)",
            }), 400
        ok = _subs.remove_subscription(_shared_dir(), sid)
        if not ok:
            return jsonify({"error": "subscription_id not found", "id": sid}), 404
        return jsonify({"removed": sid})

    # ── GET /api/push/subscriptions ──────────────────────────────────────────

    @app.get("/api/push/subscriptions")
    def api_push_subscriptions() -> Response:
        return jsonify({
            "subscriptions": [
                _subscription_view(s) for s in _subs.list_subscriptions(_shared_dir())
            ],
            "pwa_push_enabled": _alert_subs.read_pwa_push_enabled(_shared_dir()),
        })

    # ── POST /api/push/channel ───────────────────────────────────────────────
    # Toggle the operator's "Send to: PWA Push" channel from the
    # Subscriptions UI. Independent of the per-event subscription rows.

    @app.post("/api/push/channel")
    def api_push_channel_post() -> Response:
        body = request.get_json(silent=True) or {}
        if "enabled" not in body:
            return jsonify({"error": "body must include enabled (bool)"}), 400
        applied = _alert_subs.write_pwa_push_enabled(
            _shared_dir(), bool(body.get("enabled")),
        )
        return jsonify({"pwa_push_enabled": applied})

    # ── POST /api/push/test ──────────────────────────────────────────────────

    @app.post("/api/push/test")
    def api_push_test() -> Response:
        """Send a sample push to confirm a freshly-registered device works.

        Optional ``subscription_id`` targets one device; omitted fans
        out to every active subscription. The dispatcher's
        ``pwa_push_enabled`` channel-toggle is bypassed (this is the
        operator explicitly asking to test the surface).
        """
        from ..alerts import push_sender

        body = request.get_json(silent=True) or {}
        target_id = body.get("subscription_id")
        sd = _shared_dir()

        if target_id:
            sub = None
            for s in _subs.list_active(sd):
                if s.id == target_id:
                    sub = s
                    break
            if sub is None:
                return jsonify({
                    "error": "subscription_id not found or inactive",
                    "id": target_id,
                }), 404
            # Temporarily run fanout over a one-element subset — easiest
            # way to reuse the per-sub error handling without duplicating it.
            try:
                from pywebpush import webpush, WebPushException  # type: ignore
            except ImportError:
                return jsonify({"error": "pywebpush not installed"}), 500
            import json as _json
            try:
                kp = _vapid.get_or_create(sd)
            except OSError as exc:
                return jsonify({"error": f"vapid setup failed: {exc}"}), 500
            payload = _json.dumps({
                "title":     "Evolve test push",
                "body":      "This is a test from the Subscriptions tab.",
                "url":       "/",
                "event_key": "test.push",
                "severity":  "info",
            })
            try:
                webpush(
                    subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
                    data=payload,
                    vapid_private_key=kp.private_key_pem,
                    vapid_claims={"sub": kp.subject},
                    timeout=10,
                )
                _subs.record_delivery(sd, sub.endpoint)
                return jsonify({"result": "sent", "subscription_id": sub.id})
            except WebPushException as exc:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None) if resp else None
                if status == 410:
                    _subs.mark_invalid(sd, sub.endpoint)
                detail = f"http {status}" if status else str(exc)
                _subs.record_failure(sd, sub.endpoint, detail)
                return jsonify({
                    "result": "failed",
                    "subscription_id": sub.id,
                    "error":  detail,
                }), 502
            except Exception as exc:  # noqa: BLE001
                _subs.record_failure(sd, sub.endpoint, str(exc))
                return jsonify({
                    "result": "failed", "subscription_id": sub.id,
                    "error": str(exc),
                }), 502

        # Fanout mode — every device.
        result = push_sender.fanout_pwa_push(
            sd,
            title="Evolve test push",
            body="This is a test from the Subscriptions tab.",
            url="/",
            event_key="test.push",
            severity="info",
        )
        return jsonify({
            "attempted": result.attempted,
            "sent":      result.sent,
            "failed":    result.failed,
            "removed_invalid": result.removed_invalid,
        })


# ── Views ───────────────────────────────────────────────────────────────────


def _subscription_view(s: _subs.PushSubscription) -> dict[str, Any]:
    return {
        "id":             s.id,
        "device_label":   s.device_label,
        # endpoint is the unique browser-identifier — the UI compares it
        # against the locally-registered subscription before calling
        # unsubscribe() so removing *another* device's row doesn't blow
        # away the current laptop/phone's own browser subscription.
        # Not a secret; same value the browser already gave us.
        "endpoint":       s.endpoint,
        "endpoint_host":  _endpoint_host(s.endpoint),
        "created_at":     s.created_at,
        "last_delivery":  s.last_delivery,
        "last_failure":   s.last_failure,
        "invalid":        s.invalid,
    }


def _endpoint_host(endpoint: str) -> str:
    """Show the operator which push service a subscription targets.

    The full endpoint URL is long and useless to a human; the host
    alone (``push.apple.com`` / ``fcm.googleapis.com`` /
    ``updates.push.services.mozilla.com``) is the at-a-glance signal.
    """
    try:
        from urllib.parse import urlparse
        return urlparse(endpoint).hostname or endpoint[:80]
    except Exception:  # noqa: BLE001
        return endpoint[:80]


# ── User-agent fallback for device_label ─────────────────────────────────────


def _label_from_user_agent(ua: str) -> str:
    """Best-effort "Device · Browser" label from a UA string.

    Examples (real Chrome / Safari UAs):
      "...iPhone; CPU iPhone OS 17_4..." → "iPhone Safari"
      "...iPad..."                       → "iPad Safari"
      "...Android..."                    → "Android Chrome"
      "...Macintosh..."                  → "Mac Chrome"
      "...Windows..."                    → "Windows Chrome"

    Operators can rename later via the inline edit in the Devices list.
    """
    if not ua:
        return "Unnamed device"
    lowered = ua.lower()
    device = (
        "iPhone" if "iphone" in lowered
        else "iPad" if "ipad" in lowered
        else "Android" if "android" in lowered
        else "Mac" if "macintosh" in lowered or "mac os" in lowered
        else "Windows" if "windows" in lowered
        else "Linux" if "linux" in lowered
        else "Device"
    )
    browser = (
        # Order matters: Edg + EdgiOS before Chrome (Edge UAs contain both);
        # CriOS / FxiOS / EdgiOS before plain Chrome / Firefox.
        "Edge" if "edg/" in lowered or "edgios" in lowered
        else "Firefox" if "firefox" in lowered or "fxios" in lowered
        else "Chrome" if "chrome" in lowered or "crios" in lowered
        else "Safari" if "safari" in lowered
        else "Browser"
    )
    return f"{device} {browser}"


__all__ = ("register_push_routes",)
