"""Push subscription storage — PWA Phase 1.2 §6.3.

Tracks the set of browsers that have asked to receive Web Push
notifications for this pod. The dispatcher fans out to every
subscription here whenever a catalog event marked for ``pwa_push``
delivery fires.

On-disk shape (``{shared_dir}/push/subscriptions.json``)::

    {
      "version": 1,
      "subscriptions": [
        {
          "id":            "sub_a1b2c3d4",
          "endpoint":      "https://push.apple.com/...",
          "keys":          {"p256dh": "...", "auth": "..."},
          "device_label":  "Pod_admin iPhone",
          "user_agent":    "Mozilla/5.0 ...",
          "created_at":    "2026-05-19T...",
          "last_delivery": null,
          "last_failure":  null,
          "invalid":       false
        }
      ]
    }

Atomic temp-file + rename per write. Owned by the ``evolve`` user
(CLAUDE.md — ``{shared_dir}`` already has the right ACL). Mode 0644 —
the file holds no secrets; the ``endpoint``/``p256dh``/``auth`` triple
identifies a push target, but the browser controls who can deliver to
it (server keys must match the subscription, which our private VAPID
key does).
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
SUBSCRIPTION_ID_PREFIX = "sub_"


@dataclass
class PushSubscription:
    """One browser's push registration. Mutable — the dispatcher updates
    ``last_delivery`` / ``last_failure`` / ``invalid`` over the
    subscription's lifetime."""

    id: str
    endpoint: str
    keys: dict[str, str]
    device_label: str
    user_agent: str
    created_at: str
    last_delivery: str | None = None
    last_failure: str | None = None
    invalid: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "PushSubscription":
        return cls(
            id=str(body["id"]),
            endpoint=str(body["endpoint"]),
            keys={k: str(v) for k, v in (body.get("keys") or {}).items()},
            device_label=str(body.get("device_label") or "Unnamed device"),
            user_agent=str(body.get("user_agent") or ""),
            created_at=str(body.get("created_at") or ""),
            last_delivery=body.get("last_delivery"),
            last_failure=body.get("last_failure"),
            invalid=bool(body.get("invalid", False)),
        )


# ── Path + IO ───────────────────────────────────────────────────────────────


def subscriptions_path(shared_dir: Path) -> Path:
    return shared_dir / "push" / "subscriptions.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    """Return the dict in on-disk shape. Empty defaults on first run /
    parse error — never raises to the caller."""
    p = subscriptions_path(shared_dir)
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": SCHEMA_VERSION, "subscriptions": []}
    if not isinstance(body, dict):
        return {"version": SCHEMA_VERSION, "subscriptions": []}
    subs = body.get("subscriptions")
    if not isinstance(subs, list):
        body["subscriptions"] = []
    return body


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    """Atomic temp-file + rename. Mode 0644 — no secrets."""
    p = subscriptions_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utc_iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── Public API ──────────────────────────────────────────────────────────────


def list_subscriptions(shared_dir: Path) -> list[PushSubscription]:
    state = _load_state(shared_dir)
    out: list[PushSubscription] = []
    for raw in state.get("subscriptions", []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(PushSubscription.from_json(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def list_active(shared_dir: Path) -> list[PushSubscription]:
    """Active = not marked invalid. Dispatcher uses this when fanning
    out; the API ``GET /api/push/subscriptions`` returns *all* so the
    UI can render invalid devices and let the operator remove them."""
    return [s for s in list_subscriptions(shared_dir) if not s.invalid]


def find_by_endpoint(shared_dir: Path, endpoint: str) -> PushSubscription | None:
    """Look up a subscription by its push endpoint.

    The endpoint is the browser-assigned unique identifier; we dedup by
    it so re-subscribing the same browser/device updates the existing
    record (which preserves device_label edits and last_delivery
    history) instead of creating a duplicate row.
    """
    for sub in list_subscriptions(shared_dir):
        if sub.endpoint == endpoint:
            return sub
    return None


def add_subscription(
    shared_dir: Path,
    *,
    endpoint: str,
    keys: dict[str, str],
    device_label: str,
    user_agent: str = "",
) -> PushSubscription:
    """Create or refresh a subscription.

    If ``endpoint`` already exists (same browser re-subscribing), the
    existing record is refreshed: device_label updated, ``invalid``
    cleared, ``last_failure`` cleared. ID stays the same so the UI's
    per-device toggles don't bounce between IDs.
    """
    state = _load_state(shared_dir)
    subs = state.setdefault("subscriptions", [])

    for raw in subs:
        if isinstance(raw, dict) and raw.get("endpoint") == endpoint:
            raw["keys"] = keys
            if device_label:
                raw["device_label"] = device_label
            if user_agent:
                raw["user_agent"] = user_agent
            raw["invalid"] = False
            raw["last_failure"] = None
            _save_state(shared_dir, state)
            return PushSubscription.from_json(raw)

    new = PushSubscription(
        id=_new_id(),
        endpoint=endpoint,
        keys=keys,
        device_label=device_label or "Unnamed device",
        user_agent=user_agent,
        created_at=_utc_iso_now(),
    )
    subs.append(new.to_json())
    _save_state(shared_dir, state)
    return new


def remove_subscription(shared_dir: Path, subscription_id: str) -> bool:
    """Delete by id. Returns True if anything was removed."""
    state = _load_state(shared_dir)
    subs = state.get("subscriptions") or []
    before = len(subs)
    state["subscriptions"] = [
        s for s in subs
        if not (isinstance(s, dict) and s.get("id") == subscription_id)
    ]
    if len(state["subscriptions"]) == before:
        return False
    _save_state(shared_dir, state)
    return True


def remove_by_endpoint(shared_dir: Path, endpoint: str) -> bool:
    """Delete by endpoint. Used by the 410 Gone cleanup path so the
    dispatcher doesn't have to round-trip the id."""
    state = _load_state(shared_dir)
    subs = state.get("subscriptions") or []
    before = len(subs)
    state["subscriptions"] = [
        s for s in subs
        if not (isinstance(s, dict) and s.get("endpoint") == endpoint)
    ]
    if len(state["subscriptions"]) == before:
        return False
    _save_state(shared_dir, state)
    return True


def mark_invalid(shared_dir: Path, endpoint: str) -> bool:
    """Flag a subscription as invalid (used after 410 Gone).

    Soft-marks rather than deleting outright so the UI's "last failure"
    column can show the operator what happened. The dispatcher's
    ``list_active`` skips invalid rows on the next dispatch; operators
    can remove them via the Devices list.
    """
    state = _load_state(shared_dir)
    for raw in state.get("subscriptions", []):
        if isinstance(raw, dict) and raw.get("endpoint") == endpoint:
            raw["invalid"] = True
            raw["last_failure"] = _utc_iso_now() + " (410 Gone)"
            _save_state(shared_dir, state)
            return True
    return False


def record_delivery(shared_dir: Path, endpoint: str) -> None:
    """Stamp ``last_delivery`` after a successful push. Best-effort —
    failure to write the stamp does not affect the delivery outcome."""
    try:
        state = _load_state(shared_dir)
        for raw in state.get("subscriptions", []):
            if isinstance(raw, dict) and raw.get("endpoint") == endpoint:
                raw["last_delivery"] = _utc_iso_now()
                # Successful delivery clears any prior failure note —
                # the device is plainly working again.
                raw["last_failure"] = None
                _save_state(shared_dir, state)
                return
    except OSError:
        pass


def record_failure(shared_dir: Path, endpoint: str, detail: str) -> None:
    """Stamp ``last_failure`` after a non-410 dispatch error. Best-effort."""
    try:
        state = _load_state(shared_dir)
        for raw in state.get("subscriptions", []):
            if isinstance(raw, dict) and raw.get("endpoint") == endpoint:
                # Truncate detail so a verbose pywebpush traceback doesn't
                # bloat the file. 240 chars is enough for "http 502" /
                # "connection reset" style notes.
                raw["last_failure"] = f"{_utc_iso_now()} ({detail[:240]})"
                _save_state(shared_dir, state)
                return
    except OSError:
        pass


# ── ID generator ────────────────────────────────────────────────────────────


def _new_id() -> str:
    return SUBSCRIPTION_ID_PREFIX + secrets.token_hex(6)


__all__ = (
    "PushSubscription",
    "subscriptions_path",
    "list_subscriptions",
    "list_active",
    "find_by_endpoint",
    "add_subscription",
    "remove_subscription",
    "remove_by_endpoint",
    "mark_invalid",
    "record_delivery",
    "record_failure",
)
