"""channel_driver.py — the add-bot channel connect (offer-now half).

2026-06-11 design sync decision (M4 finding 4): **offer-now,
auto-activate-later**. The wizard offers to connect a messaging channel
in-conversation when the operator has a token handy (AB_CHANNEL, right
after the briefing turn); otherwise the recorded briefing decision
auto-activates when a channel connects later (``briefing_activation``).
This module is the wizard-side half:

* **Token stash** — a validated token is held in MEMORY only, keyed by
  the wizard session, exactly like the API key at AB_CREDENTIALS (the
  state file on disk records the choice, never the secret). A daemon
  restart between the channel turn and finalize loses the stash; the
  wrap reports that honestly and the auto-activation path covers the
  recovery — no silent failure.
* **Apply at finalize** — the bot account doesn't exist when the token
  is collected, so the connect runs in the finalize defaults: the same
  write the admin UI's set-token route does (credential marker +
  ``channels.telegram`` in openclaw.json + gateway kickstart), reusing
  ``skills.telegram_install``. The openclaw.json write flows through
  the channel-registration chokepoint, but the briefing install was
  already queued by finalize, so the activation hook's in-flight check
  makes the two paths meet idempotently.
* **Recipient claim** — a single-person bot created by the admin gets
  the admin recorded as its primary user on the connected channel
  (``pod.admins.external_ids``), so the briefing's delivery route
  resolves on day one. Group bots and pods without a recorded admin id
  skip the claim; the wrap then says the remaining step out loud.

Seams (tests substitute instead of patching urllib/subprocess):

* :func:`set_verify_token` — replaces the Telegram getMe validation.
* :func:`set_connector` — replaces the marker+config+kickstart apply.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ── Token stash (in-memory only) ─────────────────────────────────────────────

_lock = threading.Lock()
_stash: dict[tuple[str, str], dict[str, str]] = {}


def stash_token(
    bot_id: str, user_key: str, *, channel: str, token: str,
) -> None:
    """Hold a validated channel token for this wizard session until
    finalize applies it. Memory only — never written to the state file."""
    with _lock:
        _stash[(bot_id, user_key)] = {"channel": channel, "token": token}


def pop_token(bot_id: str, user_key: str) -> dict[str, str] | None:
    """Take (and clear) the stashed token for this session, if any."""
    with _lock:
        return _stash.pop((bot_id, user_key), None)


def clear_token(bot_id: str, user_key: str) -> None:
    with _lock:
        _stash.pop((bot_id, user_key), None)


# ── Seams ─────────────────────────────────────────────────────────────────────

# verify(token) -> dict with at least {"ok": bool, "error": str|None,
# "bot_username": str|None} — the telegram_install.verify_bot_token shape.
VerifyToken = Callable[[str], dict]
_verify_token: VerifyToken | None = None


def set_verify_token(fn: VerifyToken | None) -> None:
    global _verify_token
    _verify_token = fn


def verify_token(token: str) -> dict:
    if _verify_token is not None:
        return _verify_token(token)
    from ...skills.telegram_install import verify_bot_token

    return verify_bot_token(token)


# connector(account, token, verify_result) -> (ok, error)
Connector = Callable[[str, str, dict], "tuple[bool, str | None]"]
_connector: Connector | None = None


def set_connector(fn: Connector | None) -> None:
    global _connector
    _connector = fn


def _default_connector(
    account: str, token: str, verify_result: dict,
) -> tuple[bool, str | None]:
    """Mirror the admin UI's set-token route: store the credential
    marker, wire ``channels.telegram`` + the plugin entry into
    openclaw.json, kickstart the gateway. The kickstart is best-effort
    there and here — token + config are on disk either way."""
    from ...skills import telegram_install as _tg

    config = {
        "bot_token": token,
        "bot_username": verify_result.get("bot_username"),
        "bot_first_name": verify_result.get("bot_first_name"),
        "can_join_groups": verify_result.get("can_join_groups"),
        "can_read_all_group_messages": verify_result.get(
            "can_read_all_group_messages"),
        "verified_at": time.time(),
    }
    ok, err = _tg.write_token_config(account, config)
    if not ok:
        return False, err or "credential store failed"
    ok, err = _tg.enable_channel_in_oc_config(account, token)
    if not ok:
        return False, err or "channel config write failed"
    kick_ok, kick_err = _tg.kickstart_gateway(account)
    if not kick_ok:
        log.warning(
            "gateway restart after channel connect failed for %s: %s "
            "(token + config are on disk)", account, kick_err,
        )
    return True, None


# ── Finalize apply ────────────────────────────────────────────────────────────


def apply_stashed_channel(
    *,
    bot_id: str,
    user_key: str,
    account: str,
    extracted: dict[str, Any],
    network: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Apply the channel decision recorded at AB_CHANNEL, after the bot
    is built. Returns a report for the success wrap::

        {
          "status": "connected" | "failed" | "lost" | "skipped" | "none",
          "channel": "telegram" | "",
          "recipient_recorded": bool,
          "network_dirty": bool,
          "error": str,            # plain words, only when failed/lost
        }

    ``lost`` is the honest daemon-restart case: the operator pasted a
    token but the in-memory stash didn't survive to finalize. The wrap
    says so and points at the admin page; auto-activation covers the
    briefing once the channel is connected there.
    """
    choice = str(extracted.get("ab_channel_connect") or "")
    report: dict[str, Any] = {
        "status": "none",
        "channel": "",
        "recipient_recorded": False,
        "network_dirty": False,
        "error": "",
    }
    if choice == "skipped":
        report["status"] = "skipped"
        return report
    if choice != "ready":
        return report

    stashed = pop_token(bot_id, user_key)
    if not stashed or not account:
        report["status"] = "lost"
        report["channel"] = str(extracted.get("_ab_channel") or "telegram")
        return report

    channel = stashed["channel"]
    report["channel"] = channel
    verify_result = {
        "bot_username": extracted.get("_ab_channel_username") or None,
    }
    try:
        connector = _connector or _default_connector
        ok, err = connector(account, stashed["token"], verify_result)
    except Exception as exc:  # noqa: BLE001
        ok, err = False, f"{exc.__class__.__name__}: {exc}"
    if not ok:
        report["status"] = "failed"
        report["error"] = str(err or "the channel hookup didn't finish")
        return report

    report["status"] = "connected"
    report["recipient_recorded"], report["network_dirty"] = _claim_recipient(
        account=account, channel=channel,
        extracted=extracted, network=network,
    )
    return report


def _claim_recipient(
    *,
    account: str,
    channel: str,
    extracted: dict[str, Any],
    network: Optional[dict[str, Any]],
) -> tuple[bool, bool]:
    """Record the admin as the new bot's primary user on the connected
    channel, when that's unambiguous: the add-bot chain is admin-only,
    so a single-person bot's audience IS the admin — and the pod
    already knows the admin's id on the channel
    (``pod.admins.external_ids``). Group-audience bots and pods without
    a recorded admin id skip the claim (the wrap states the remaining
    step). Returns ``(recipient_recorded, network_dirty)``.
    """
    from .. import identity as _identity
    from . import phases as _phases

    if not isinstance(network, dict):
        return False, False
    if _phases.ab_group_audience(extracted):
        return False, False
    admin_ids = (
        ((network.get("pod") or {}).get("admins") or {}).get("external_ids")
        or {}
    )
    candidates = admin_ids.get(channel) if isinstance(admin_ids, dict) else None
    if isinstance(candidates, (list, tuple)):
        admin_id = str(candidates[0]) if candidates else ""
    else:
        admin_id = str(candidates or "")
    if not admin_id.strip():
        return False, False
    try:
        _identity.claim_primary(
            network, account, channel=channel, external_id=admin_id,
        )
    except _identity.ClaimError as exc:
        log.warning(
            "recipient claim skipped for %s on %s: %s", account, channel, exc,
        )
        return False, False
    return True, True
