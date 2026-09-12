"""PWA Push delivery channel — PWA Phase 1.2 §6.5.

A 4th delivery channel alongside slack/telegram/discord (which all go
out through the openclaw subprocess). The dispatcher imports
:func:`fanout_pwa_push` and calls it after the primary chat delivery
when ``pwa_push`` is in the operator's active channel set. Each call
sends one push per active subscription.

Failure handling, distilled from spec §6.5 + the silent-failure
checklist in the task prompt:

- **410 Gone** — the user uninstalled the PWA or revoked notifications.
  Mark the subscription invalid; the dispatcher's next fanout will skip
  it. We deliberately mark-as-invalid rather than delete so the
  operator can see *what* went stale in the Subscriptions UI.

- **Other errors** — log + stamp ``last_failure`` on the subscription
  and continue. One bad endpoint never blocks the others.

- **No subscriptions** — return early with a clean
  ``PushFanoutResult(attempted=0, sent=0, failed=0, removed=0)``. The
  dispatcher logs the empty fanout for audit but it isn't a failure.

- **pywebpush import failure** — Phase 1.2 added pywebpush as a hard
  dep; if it's missing on a deployment, log + return all-failed so
  operators see the gap in the activity log. We *don't* crash the
  primary chat send.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..web import push_subscriptions as _subs
from ..web import push_vapid as _vapid

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushFanoutResult:
    """Aggregate outcome of a fanout.

    The dispatcher folds the per-channel results into its audit log;
    callers (the dispatcher + the test endpoint) use the fields to
    surface "sent to N devices" status.
    """

    attempted:        int
    sent:             int
    failed:           int
    removed_invalid:  int   # endpoints marked invalid this run (410 Gone)


# ── Public API ──────────────────────────────────────────────────────────────


def fanout_pwa_push(
    shared_dir: Path,
    *,
    title: str,
    body: str,
    url: str = "/",
    event_key: str | None = None,
    severity: str | None = None,
) -> PushFanoutResult:
    """Push one notification to every active subscription.

    Returns the rolled-up counts. The dispatcher uses these to mark the
    audit log entry; the test endpoint surfaces "Sent to N devices" in
    the UI.

    The payload shape mirrors what the SW handler in ``sw.js`` parses:
    ``{title, body, url, event_key, severity}``. ``event_key`` populates
    the notification ``tag`` so repeated firings of the same event
    collapse instead of stacking on the user's phone.
    """
    actives = _subs.list_active(shared_dir)
    if not actives:
        return PushFanoutResult(0, 0, 0, 0)

    try:
        kp = _vapid.get_or_create(shared_dir)
    except OSError as exc:
        log.warning("pwa_push: VAPID key unavailable (%s); dropping fanout", exc)
        return PushFanoutResult(len(actives), 0, len(actives), 0)

    payload = json.dumps({
        "title":     title,
        "body":      body,
        "url":       url,
        "event_key": event_key,
        "severity":  severity,
    })

    try:
        from pywebpush import webpush, WebPushException  # type: ignore
    except ImportError:
        log.warning(
            "pwa_push: pywebpush not installed; subscriptions skipped. "
            "Add pywebpush to admin deps and redeploy."
        )
        return PushFanoutResult(len(actives), 0, len(actives), 0)

    sent = 0
    failed = 0
    removed = 0

    for sub in actives:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
                data=payload,
                vapid_private_key=kp.private_key_pem,
                vapid_claims={"sub": kp.subject},
                timeout=10,
            )
            _subs.record_delivery(shared_dir, sub.endpoint)
            sent += 1
        except WebPushException as exc:
            status = _status_of(exc)
            if status == 410:
                _subs.mark_invalid(shared_dir, sub.endpoint)
                removed += 1
                log.info(
                    "pwa_push: subscription %s gone (410); marked invalid",
                    sub.id,
                )
            else:
                failed += 1
                _subs.record_failure(
                    shared_dir, sub.endpoint,
                    f"http {status}" if status else str(exc),
                )
                log.warning(
                    "pwa_push: send to %s failed (%s)",
                    sub.id, status or exc,
                )
        except Exception as exc:  # noqa: BLE001
            # Network blip / DNS / etc. — log and keep going. The next
            # fanout will retry; persistent failures will stack
            # ``last_failure`` so the operator sees the device degrading
            # in the Subscriptions UI.
            failed += 1
            _subs.record_failure(shared_dir, sub.endpoint, str(exc))
            log.warning("pwa_push: unexpected error sending to %s: %s",
                        sub.id, exc)

    return PushFanoutResult(
        attempted=len(actives),
        sent=sent,
        failed=failed,
        removed_invalid=removed,
    )


def _status_of(exc: Any) -> int | None:
    """Pull the HTTP status off a WebPushException without crashing if
    pywebpush's internals change."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    return None


__all__ = (
    "PushFanoutResult",
    "fanout_pwa_push",
)
