"""Handover token store + helpers.

V2.4-5: when a primary admin (e.g. Diana's EA Maria) installs a bot for
someone else, they can generate a one-tap onboarding link that lets the
new user finish *their* part of setup — voice, preferred name, channel
ack — without ever seeing the technical configuration the installer
already handled.

Storage shape: ``{shared_dir}/handover-tokens/<token>.json`` ::

    {
      "token": "<32-char uuid hex>",
      "bot_id": "diana_personal",
      "audience": "personal_bot_user",
      "message": "Hi Diana — your assistant is ready, …",
      "created_at": "2026-05-16T12:00:00Z",
      "expires_at": "2026-05-23T12:00:00Z",
      "claimed_at": null,
      "preferences": null,
    }

All writes are atomic (write-to-temp + rename) and stay inside the
shared dir, which the ``evolve`` service user owns — no sudo / /tmp
staging required. The token IS the authentication (matches the
Plex/Tailscale invite-link UX); future hardening can add OTP later.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from evolve_util import atomic_write_json as _atomic_write

DEFAULT_EXPIRES_IN_DAYS = 7
TOKEN_LEN = 32  # uuid4().hex
KNOWN_AUDIENCES = ("personal_bot_user", "team_bot_member")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def tokens_dir(shared_dir: Path) -> Path:
    """Return the handover tokens directory, creating it if needed."""
    d = Path(shared_dir) / "handover-tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _token_path(shared_dir: Path, token: str) -> Path:
    return tokens_dir(shared_dir) / f"{token}.json"


def load_token(shared_dir: Path, token: str) -> Optional[dict]:
    """Return the token record, or None if missing/unreadable."""
    p = _token_path(shared_dir, token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_active_for_bot(shared_dir: Path, bot_id: str) -> Optional[dict]:
    """Return the existing unclaimed, unexpired token for ``bot_id`` if any."""
    now = _now()
    for f in tokens_dir(shared_dir).glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("bot_id") != bot_id:
            continue
        if rec.get("claimed_at"):
            continue
        try:
            expires = _parse_iso(rec.get("expires_at", ""))
        except (ValueError, TypeError):
            continue
        if expires <= now:
            continue
        return rec
    return None


def create_token(
    shared_dir: Path,
    bot_id: str,
    audience: str = "personal_bot_user",
    message: str = "",
    expires_in_days: int = DEFAULT_EXPIRES_IN_DAYS,
    rotate: bool = False,
) -> tuple[dict, bool]:
    """Create a handover token for ``bot_id``.

    Returns ``(record, created)`` — ``created`` is False when an
    existing active token was reused (no ``--rotate``).

    When ``rotate=True``, any existing active token for the bot is
    deleted before a new one is minted.
    """
    if audience not in KNOWN_AUDIENCES:
        # Accept unknown audiences but normalize empty/None to default.
        # v1 doesn't differentiate behavior, but we record what the caller asked for.
        if not audience:
            audience = "personal_bot_user"

    existing = find_active_for_bot(shared_dir, bot_id)
    if existing and not rotate:
        return existing, False

    if existing and rotate:
        try:
            _token_path(shared_dir, existing["token"]).unlink()
        except (KeyError, OSError):
            pass

    now = _now()
    expires = now + timedelta(days=max(1, int(expires_in_days)))
    token = uuid.uuid4().hex  # 32-char
    rec = {
        "token": token,
        "bot_id": bot_id,
        "audience": audience,
        "message": (message or "").strip(),
        "created_at": _iso(now),
        "expires_at": _iso(expires),
        "claimed_at": None,
        "preferences": None,
    }
    _atomic_write(_token_path(shared_dir, token), rec)
    return rec, True


def is_expired(rec: dict, now: Optional[datetime] = None) -> bool:
    now = now or _now()
    try:
        return _parse_iso(rec.get("expires_at", "")) <= now
    except (ValueError, TypeError):
        return True


def is_claimed(rec: dict) -> bool:
    return bool(rec.get("claimed_at"))


def is_usable(rec: dict, now: Optional[datetime] = None) -> bool:
    return not is_expired(rec, now) and not is_claimed(rec)


def claim_token(
    shared_dir: Path,
    token: str,
    preferences: dict,
) -> tuple[Optional[dict], Optional[str]]:
    """Mark a token claimed. Returns (record, error) — error is a short
    code: ``not_found`` | ``expired`` | ``already_claimed`` | ``ok``.

    The first return is the updated record on success, the stale record
    on error (so callers can read bot_id for the friendly page), or None
    when the token is unknown.
    """
    rec = load_token(shared_dir, token)
    if rec is None:
        return None, "not_found"
    if is_expired(rec):
        return rec, "expired"
    if is_claimed(rec):
        return rec, "already_claimed"
    rec["claimed_at"] = _iso(_now())
    if isinstance(preferences, dict):
        rec["preferences"] = preferences
    _atomic_write(_token_path(shared_dir, token), rec)
    return rec, None


def cleanup_expired(shared_dir: Path) -> dict:
    """Prune token files whose ``expires_at`` is in the past.

    Returns ``{"removed": int, "kept": int}``. Claimed-but-not-yet-expired
    tokens are kept so the audit trail stays available within the TTL.
    """
    now = _now()
    removed = 0
    kept = 0
    d = tokens_dir(shared_dir)
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            # Corrupt file — drop it. Idempotent.
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
            continue
        try:
            expires = _parse_iso(rec.get("expires_at", ""))
        except (ValueError, TypeError):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
            continue
        if expires <= now:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        else:
            kept += 1
    return {"removed": removed, "kept": kept}


# ── Preference-apply path ──────────────────────────────────────────────────
# When a new user submits the onboarding form, write their picks into the
# bot's workspace so the bot's own surfaces (POD_CONDUCT.md, profile,
# session greetings) pick them up. Stay scoped to fields a personal-bot
# user is implicitly authorized to set.

# Voice presets — surfaced names; the bot side maps them to actual style.
# If voice presets aren't yet implemented on the bot side, the value is
# still stored verbatim and can be read by future POD_CONDUCT generators.
VOICE_PRESETS = ("Casual", "Concierge", "Buddy", "Professional")


def normalize_preferences(raw: dict) -> dict:
    """Coerce posted form fields into the stored shape.

    Strict allowlist — anything not in this list is dropped. Keeps the
    write surface narrow.
    """
    out: dict[str, Any] = {}
    voice = (raw.get("voice") or "").strip()
    if voice in VOICE_PRESETS:
        out["voice"] = voice
    elif voice:
        # Allow free-text fallback for the case where voice presets
        # aren't yet wired; truncate aggressively.
        out["voice_freeform"] = voice[:120]
    preferred_name = (raw.get("preferred_name") or "").strip()
    if preferred_name:
        out["preferred_name"] = preferred_name[:80]
    notes = (raw.get("notes") or "").strip()
    if notes:
        out["notes"] = notes[:500]
    return out


def write_preferences_to_bot(
    bot_id: str,
    preferences: dict,
    shared_dir: Path,
    network: Optional[dict] = None,
) -> dict:
    """Write claimed preferences into the bot's workspace.

    The destination is ``<bot_home>/.openclaw/workspace/evolve/onboarding/handover-prefs.json``
    — the workspace/evolve subtree has write ACL for the ``evolve`` user
    by deploy-time setup (set_evolve_read_acl in deploy.py), so this
    works without sudo. If the directory doesn't yet exist (bot hasn't
    been deployed through the new path yet), fall back to a per-bot file
    under the shared dir so the data isn't lost.

    Returns ``{"path": <str>, "fallback": bool}``.
    """
    # Lazy import to avoid circular: config -> handover is fine,
    # but other admin modules drag in heavyweight side effects.
    try:
        from .config import bot_home, load_network

        if network is None:
            network = load_network()
        home = bot_home(bot_id, network=network)
    except Exception:
        home = Path(f"/Users/{bot_id}")

    payload = {
        "bot_id": bot_id,
        "applied_at": _iso(_now()),
        "preferences": preferences,
    }

    target = home / ".openclaw" / "workspace" / "evolve" / "onboarding"
    fallback_target = Path(shared_dir) / "handover-prefs" / bot_id
    used = target
    fallback = False
    try:
        target.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        used = fallback_target
        fallback = True
        used.mkdir(parents=True, exist_ok=True)

    out_path = used / "handover-prefs.json"
    try:
        _atomic_write(out_path, payload)
    except (OSError, PermissionError):
        # Final fallback: shared-dir copy (always writable by evolve user)
        used = fallback_target
        used.mkdir(parents=True, exist_ok=True)
        out_path = used / "handover-prefs.json"
        _atomic_write(out_path, payload)
        fallback = True

    return {"path": str(out_path), "fallback": fallback}


def pod_host(network: dict) -> tuple[str, str]:
    """Best-effort ``(scheme, host[:port])`` for the handover URL.

    Thin wrapper around :func:`evolve_admin.config.resolve_pod_host` so
    the scheme is preserved from ``adminBaseUrl`` rather than guessed
    from host shape. The legacy operator overrides (``pod.public_host``,
    ``tunnel.remote_host``) still win on host, and a bare hostname in
    either field inherits the scheme from ``adminBaseUrl``; an override
    that includes a scheme (e.g. ``"https://override.example.com"``)
    uses that scheme verbatim. See the helper's docstring for the full
    resolution order.
    """
    from .config import resolve_pod_host
    return resolve_pod_host(network)


def build_handover_url(network: dict, token: str) -> str:
    scheme, host = pod_host(network)
    return f"{scheme}://{host}/handover/{token}"
