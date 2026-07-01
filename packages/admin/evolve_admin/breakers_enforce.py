"""evolve_admin.breakers_enforce — synchronous L2 enforcement.

Spec: docs/spec-circuit-breakers-2026-05-21.md §3.5, §5.2.

Phase 3a wires up L2 (full halt) enforcement using the existing
launchctl bootout/bootstrap primitives in recovery.py. L1 (cost
breaker) enforcement is the TS plugin's job and lands in Phase 3b
— here, type="cost" trips are no-ops (the file is written, the
plugin will read it later).

Two-layer design (spec §3.5):

  1. SYNCHRONOUS ENFORCEMENT (this module). When the admin or evo
     trips a breaker via the CLI/UI, enforce_trip() runs immediately
     so the bot is down within seconds, not minutes. Same for
     enforce_reset(): bootstrap the gateway right away.

  2. RECONCILING ENFORCEMENT (heal.py). On its 5-min cycle, heal
     reconciles state-file → reality: any L2 trip whose gateway is
     still running gets bootout (catches auto-trips and external
     writes); any expired trip gets cleared from disk (the TTL
     reaper); after the reap, the gateway naturally restarts on the
     same cycle.

This module is the SYNCHRONOUS path. Heal's role is the next PR's
focus but they coexist — calling enforce_trip from the CLI does NOT
duplicate work heal will later do, because heal sees the gateway is
already down and skips.

Scope rules:
  - "pod" → all bots in the network (full bootout, mirrors today's
    pause_all behavior)
  - <bot_id> → exactly that bot
  - unknown bot_id → ValueError

L1 (cost) callers receive an EnforceResult with no_op=True so the
CLI can render an honest "intent recorded; plugin enforces in
Phase 3b" message without lying about what happened.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evolve_util import now_iso_micro as _now_iso

from . import recovery
# Aliased so the test monkeypatching pattern
# (``monkeypatch.setattr(breakers_enforce, "_bot_home_dir", ...)``)
# keeps working without every test having to reach into ``config``.
from .config import bot_home as _bot_home_dir
from .config import get_bot_user as _bot_user
from .recovery import PerBotResult


log = logging.getLogger(__name__)


# Default channel priority for user-channel OOO posting. Matches the
# alerts dispatcher's _DEFAULT_CHANNEL_PRIORITY but kept local because
# we resolve recipients per-bot (different from the dispatcher's
# operator-only resolution path).
_USER_CHANNEL_PRIORITY: tuple[str, ...] = (
    "telegram", "signal", "whatsapp", "slack", "discord",
)

# Per-bot timeout for the user-channel OOO post. The dispatcher's
# default (60s) is sized for the operator-alerts path; for the
# emergency-halt path the OOO message is best-effort courtesy and
# must NOT delay the bootout. The success marker normally arrives in
# 3-5s, so 10s leaves margin without blocking N×60s on an N-bot
# pod-wide trip where some channels are unhealthy (the exact scenario
# you'd reach for an emergency trip in).
_NOTIFY_TIMEOUT_SECONDS: int = 10

# Concurrency for cross-bot notify dispatch. Mirrors the
# _PAUSE_MAX_WORKERS cap used by recovery.pause_all — same shape
# (parallel per-bot subprocess calls, independent across bots) so the
# same upper bound makes sense. Serial-per-token rationale only
# applies within a single bot, not across bots in a pod-wide trip.
_NOTIFY_MAX_WORKERS: int = 8


@dataclass
class NotifyResult:
    """Outcome of one bot's OOO-message attempt.

    Phase 4c posts a short status message to each affected bot's
    primary user channel before L2 bootout (and after L2 bootstrap).
    Best-effort: failures land here but never block the enforce.
    """

    bot_id: str
    action: str                          # "trip" | "reset"
    attempted: bool                      # True if we tried to post
    sent: bool = False                   # True iff the post succeeded
    skip_reason: str = ""                # populated when attempted=False
    channel: str = ""                    # resolved channel, if any
    chat_id: str = ""                    # resolved chat_id, if any
    error: str = ""                      # populated when attempted=True and sent=False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnforceResult:
    """Outcome of an enforce_trip / enforce_reset call."""

    action: str                          # "trip" | "reset"
    scope: str                           # bot_id or "pod"
    breaker_type: str                    # "cost" | "full"
    ok: bool
    no_op: bool = False                  # True for L1 (no enforcement in Phase 3a)
    no_op_reason: str = ""               # why no_op (for CLI rendering)
    per_bot: list[PerBotResult] = field(default_factory=list)
    notifications: list[NotifyResult] = field(default_factory=list)
    dry_run: bool = False
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_bot"] = [r.to_dict() for r in self.per_bot]
        d["notifications"] = [n.to_dict() for n in self.notifications]
        return d


# ── Phase 4c: user-channel OOO posting ───────────────────────────────────────


def _resolve_user_recipient(
    network: dict, bot_id: str,
) -> tuple[str, str] | None:
    """Return ``(channel, chat_id)`` for one bot's primary user, or None.

    Reads the per-bot ``primary_user.external_ids`` block in network.json
    — the same shape ``alerts.dispatcher.resolve_recipient`` uses for the
    operator, but indexed by bot_id rather than always-the-primary. Falls
    back to None when:
      - The bot is missing from network
      - The bot has no primary_user block
      - external_ids contains no channel from _USER_CHANNEL_PRIORITY

    Explicit ``primary_channel`` (when present and matched in
    external_ids) wins over the default priority order. Mirrors the
    operator-recipient logic so a future field-rename ripples cleanly.
    """
    bots = (network.get("bots") or {}) if isinstance(network, dict) else {}
    bot_cfg = bots.get(bot_id) or {}
    if not bot_cfg:
        return None
    primary_user = bot_cfg.get("primary_user") or {}
    external_ids = primary_user.get("external_ids") or {}
    if not external_ids:
        return None

    explicit = bot_cfg.get("primary_channel")
    if explicit and external_ids.get(explicit):
        return str(explicit), str(external_ids[explicit])

    for ch in _USER_CHANNEL_PRIORITY:
        if external_ids.get(ch):
            return ch, str(external_ids[ch])
    return None


def _render_trip_message(
    *, reason: str, expires_at_iso: str | None,
) -> str:
    """The "out of office" message users see when an L2 trip hits.

    Plain text, no HTML — keeps the message readable on every channel
    OC supports (telegram, slack, discord, signal). Spec §3.6.
    """
    bits = [
        "🔴 I've been halted (circuit breaker tripped).",
        "I won't reply to messages until I'm reactivated.",
    ]
    if expires_at_iso:
        bits.append(f"Auto-recovery: {expires_at_iso}")
    else:
        bits.append("No auto-recovery scheduled — admin will reset manually.")
    if reason:
        bits.append(f"Reason: {reason}")
    return " ".join(bits)


def _render_reset_message() -> str:
    return "🟢 I'm back online — circuit breaker reset."


def _render_cost_trip_message(
    *, reason: str, expires_at_iso: str | None,
) -> str:
    """The user-facing message when the L1 cost breaker trips.

    Distinct from the L2 ``_render_trip_message`` — the L1 trip keeps
    user-driven turns working (the gateway stays up, exec passthrough
    is widened for read-only commands). The user can still talk to the
    bot; what's paused is background work (heartbeats + auto-crons).
    Spec §3.6 of the alerts/breakers UX work.
    """
    bits = [
        "💰 I've paused my background work because today's spending hit "
        "the cost cap.",
        "You can still talk to me — I'll respond to your messages — "
        "but heartbeats and scheduled jobs are off until tomorrow.",
    ]
    if expires_at_iso:
        bits.append(f"Auto-recovery: {expires_at_iso}.")
    if reason:
        bits.append(f"Reason: {reason}")
    return " ".join(bits)


def _render_cost_reset_message() -> str:
    return (
        "🟢 Background work is back on — the cost breaker reset."
    )


def _notify_one_bot(
    *,
    bot_id: str,
    action: str,
    network: dict,
    message: str,
    dry_run: bool,
) -> NotifyResult:
    """Best-effort: post the OOO/return message to bot_id's primary user.

    Returns a NotifyResult — never raises. Caller can ignore the result
    (the enforce path does), or surface it for forensics (the CLI/UI
    show notification counts to make "we tried" auditable).
    """
    recipient = _resolve_user_recipient(network, bot_id)
    if recipient is None:
        return NotifyResult(
            bot_id=bot_id, action=action, attempted=False,
            skip_reason="no primary_user.external_ids on this bot",
        )
    channel, chat_id = recipient

    if dry_run:
        return NotifyResult(
            bot_id=bot_id, action=action, attempted=False,
            skip_reason="dry-run",
            channel=channel, chat_id=chat_id,
        )

    # Lazy import — keeps enforce-only call sites lightweight and avoids
    # circular import risk if the dispatcher ever grows a back-reference.
    try:
        from .alerts.dispatcher import _dispatch_via_openclaw
        from .config import get_bot_port
    except ImportError as exc:
        log.warning("breakers.notify: dispatcher unavailable: %s", exc)
        return NotifyResult(
            bot_id=bot_id, action=action, attempted=False,
            skip_reason=f"dispatcher import failed: {exc}",
            channel=channel, chat_id=chat_id,
        )

    # Use the per-bot gateway port — the OC CLI defaults to 18789 but
    # each bot has its own listener (see _resolve_gateway_port docstring
    # in dispatcher.py).
    try:
        gateway_port = get_bot_port(bot_id, network)
    except Exception:  # noqa: BLE001
        gateway_port = None

    try:
        sent, err = _dispatch_via_openclaw(
            channel=channel, chat_id=chat_id, message=message,
            gateway_port=gateway_port,
            # Cap the per-bot wait at _NOTIFY_TIMEOUT_SECONDS. Without
            # this, an unhealthy channel can block the emergency halt
            # for up to the dispatcher's 60s default — and on a pod-
            # wide trip the bootout would be delayed by sum(per-bot
            # timeouts). We accept "OOO message might not arrive" in
            # exchange for "bootout happens promptly."
            timeout_seconds=_NOTIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("breakers.notify: dispatch raised for %s", bot_id)
        return NotifyResult(
            bot_id=bot_id, action=action, attempted=True, sent=False,
            channel=channel, chat_id=chat_id,
            error=f"dispatch raised: {type(exc).__name__}: {exc}",
        )

    return NotifyResult(
        bot_id=bot_id, action=action, attempted=True, sent=bool(sent),
        channel=channel, chat_id=chat_id,
        error="" if sent else (err or "unknown failure"),
    )


def _notify_user_channels(
    *,
    bots: list[tuple[str, str]],
    action: str,
    network: dict,
    message: str,
    dry_run: bool,
) -> list[NotifyResult]:
    """Post the OOO/return message to every bot's primary user channel.

    PARALLEL across bots — each ``_notify_one_bot`` uses a distinct
    gateway port, bot token, and chat. Serial-per-token rate limits
    apply within a single bot, not across bots in a pod-wide trip, so
    parallelizing here changes worst-case from ``sum(per-bot timeout)``
    to ``max(per-bot timeout)``. Critical for the emergency-halt path:
    the bootout that follows ``_notify_user_channels`` must not be
    delayed by an unhealthy channel.

    Failures are isolated per-bot and never propagate: a single bot's
    notification failure does not block others or the subsequent
    bootout/bootstrap.

    Preserves input order in the returned list so callers can map
    notifications[i] ↔ bots[i] if they want to.
    """
    if not bots:
        return []
    results: dict[str, NotifyResult] = {}
    workers = min(_NOTIFY_MAX_WORKERS, max(1, len(bots)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _notify_one_bot,
                bot_id=bot_id, action=action, network=network,
                message=message, dry_run=dry_run,
            ): bot_id
            for bot_id, _user in bots
        }
        for fut in as_completed(futures):
            bot_id = futures[fut]
            try:
                results[bot_id] = fut.result()
            except Exception as exc:  # noqa: BLE001 — safety net
                log.exception("breakers.notify: outer guard for %s", bot_id)
                results[bot_id] = NotifyResult(
                    bot_id=bot_id, action=action, attempted=True, sent=False,
                    error=f"unhandled: {type(exc).__name__}: {exc}",
                )
    # Preserve input order.
    return [results[bot_id] for bot_id, _ in bots]


def _resolve_target_bots(
    scope: str, network: dict,
) -> list[tuple[str, str]]:
    """Return [(bot_id, os_user)] for the bots a (scope, …) trip affects.

    - "pod" → all bots in the network
    - <bot_id> → that single bot (validated against network)
    Raises ValueError when bot_id is unknown.
    """
    if scope == "pod":
        return recovery._iter_bots(network)

    # Per-bot scope — confirm the bot exists in network.
    all_bots = recovery._iter_bots(network)
    for bot_id, user in all_bots:
        if bot_id == scope:
            return [(bot_id, user)]
    raise ValueError(
        f"unknown bot {scope!r}; not present in network.json "
        f"(bots: {[b for b, _ in all_bots]})"
    )


def _enforce_full(
    *,
    action: str,        # "trip" or "reset"
    scope: str,
    network: dict,
    dry_run: bool,
    reason: str = "",
    expires_at_iso: str | None = None,
) -> EnforceResult:
    """Do the actual launchctl work for type=full, with user-channel
    OOO posting (Phase 4c) ordered around it.

    Ordering invariants:

      action="trip"  → notify FIRST, then bootout.
        Posting requires a live gateway; bootout kills it. If we
        bootout first the OOO message can never reach the user, who
        is then left wondering why the bot stopped responding.

      action="reset" → bootstrap FIRST, then notify.
        The "I'm back" message requires the gateway to be up. We give
        the OC CLI its standard 60s timeout window to connect; in
        practice fresh-bootstrap-then-post lands within a few seconds.

    Best-effort: notification failures are recorded on the result but
    do NOT change the overall ok status — the operator-visible action
    is the bootout/bootstrap, not the user-side courtesy message.
    """
    import time
    t0 = time.monotonic()

    targets = _resolve_target_bots(scope, network)
    if not targets:
        return EnforceResult(
            action=action, scope=scope, breaker_type="full",
            ok=True, dry_run=dry_run, elapsed_ms=0,
            per_bot=[],
            no_op=True, no_op_reason="no bots in network",
        )

    if action == "trip":
        fn = recovery._bootout_gateway
    elif action == "reset":
        fn = recovery._bootstrap_gateway
    else:
        raise ValueError(f"unknown action {action!r}; expected 'trip' or 'reset'")

    notifications: list[NotifyResult] = []

    # ── PHASE 4c notify (trip): BEFORE bootout, while the gateway can
    # still reach the user channel. Skipped on dry-run; failures here
    # never block the bootout.
    if action == "trip":
        msg = _render_trip_message(reason=reason, expires_at_iso=expires_at_iso)
        notifications = _notify_user_channels(
            bots=targets, action="trip", network=network,
            message=msg, dry_run=dry_run,
        )

    per_bot = recovery._dispatch_per_bot(targets, fn, dry_run)
    ok = all(r.ok for r in per_bot)

    # ── PHASE 4c notify (reset): AFTER bootstrap, so the gateway is
    # back up to deliver the message. Filter on ``r.ok and r.rc is not
    # None`` — that includes both rc=0 (we brought the gateway up) and
    # rc=37 ("service already loaded"; goal state met, heal or another
    # operator beat us to it), but excludes the plist-not-found path
    # where rc is None and the gateway was never up. Matches
    # ``_bootstrap_gateway``'s ok=True contract for rc=37 (see its
    # docstring in recovery.py) — without this fix the heal-raced
    # reset would silently swallow the "I'm back" message.
    if action == "reset":
        msg = _render_reset_message()
        recovered = [
            (r.bot_id, "")  # second tuple field is os_user (unused downstream)
            for r in per_bot
            if r.ok and r.rc is not None
        ]
        notifications = _notify_user_channels(
            bots=recovered, action="reset", network=network,
            message=msg, dry_run=dry_run,
        )

    elapsed = int((time.monotonic() - t0) * 1000)

    return EnforceResult(
        action=action, scope=scope, breaker_type="full",
        ok=ok, dry_run=dry_run, elapsed_ms=elapsed,
        per_bot=per_bot,
        notifications=notifications,
    )


# ── L1 cost enforcement (heartbeat-disable / restore) ──────────────────────
#
# When a bot's L1 cost breaker trips, we synchronously remove the
# ``agents.defaults.heartbeat.every`` field from its openclaw.json and
# kickstart the gateway so the change takes effect immediately. The
# original value is stashed alongside the breaker file
# (``{shared_dir}/breakers/<bot>/heartbeat-stash.json``) so reset can
# restore it.
#
# This is partial Phase 3b. The spec's longer-term plan is for the OC TS
# plugin to read the breaker file on each non-user-channel turn and
# veto — that veto-style approach is cheaper (no openclaw.json rewrite)
# and finer-grained (blocks crons too). Until that ships we ship the
# openclaw.json edit because Pod_admin did exactly this manually during the
# 2026-05-20 incident and it provably stops the bleed.
#
# Idempotency contract:
#   - trip with no heartbeat.every present → no edit, no stash, ok=True
#   - trip when stash already exists       → preserve original stash,
#                                            don't overwrite with current
#                                            (which may already be unset)
#   - reset with no stash present          → no edit, ok=True
#   - reset with stash present             → restore + delete stash
#
# Failures (openclaw.json unreadable, sudo cp fails, etc.) propagate
# through the EnforceResult as ok=False per-bot but never raise — same
# isolation contract as the L2 path.


@dataclass
class CostEnforcePerBot:
    """Per-bot result of an L1 cost enforce_trip / enforce_reset."""

    bot_id: str
    action: str            # "trip" | "reset"
    ok: bool
    no_op: bool = False    # True when nothing to do (no heartbeat / no stash)
    no_op_reason: str = ""
    detail: str = ""
    prior_every: Any = None  # stashed value (trip) or restored value (reset)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _breakers_root(shared_dir: Path) -> Path:
    """Mirror of breakers.store.breakers_dir without importing it.

    breakers.store lives in the evolve-analyzer package; this module
    can simply derive the path without the import.
    """
    return Path(shared_dir) / "breakers"


def _heartbeat_stash_path(shared_dir: Path, bot_id: str) -> Path:
    """The stash file lives next to ``<bot>/cost.json`` in the breaker store."""
    return _breakers_root(shared_dir) / bot_id / "heartbeat-stash.json"


def _passthrough_stash_path(shared_dir: Path, bot_id: str) -> Path:
    """Stash for the exec-approvals entries added during trip.

    Companion to the heartbeat stash — records the exec-approvals
    state at trip time so reset can subtract the added entries
    without clobbering anything the operator added between trip and
    reset (we add only the patterns whose ids start with the trip
    marker prefix).
    """
    return _breakers_root(shared_dir) / bot_id / "exec-approvals-stash.json"


# Trip-mode passthrough: narrow allowlist additions so the bot can answer
# operator status questions during a tripped breaker without needing exec
# approval. Per the user's guidance after the 2026-05-28 Security_bot trip
# incident: the breaker stops auto-turns; user-driven turns keep working,
# including sending agents as needed. This is the minimum set that gets
# "what's your status?" questions answered — file reads via cat. Operator
# can add broader patterns through the normal exec-approvals flow if a
# specific bot needs more during trip mode.
#
# Each entry's id carries a stable prefix so reset can find and remove
# exactly the patterns the trip added — without disturbing anything the
# operator (or another generator) added between trip and reset.
_TRIP_PASSTHROUGH_ID_PREFIX = "breaker-trip-passthrough-"
_TRIP_PASSTHROUGH_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (id_suffix, pattern, comment)
    ("cat-bin",     "/bin/cat *",     "breaker-trip passthrough: read files"),
    ("cat-usr-bin", "/usr/bin/cat *", "breaker-trip passthrough: read files"),
    ("ls-bin",      "/bin/ls *",      "breaker-trip passthrough: list files"),
    ("ls-usr-bin",  "/usr/bin/ls *",  "breaker-trip passthrough: list files"),
)


def _trip_passthrough_entry(id_suffix: str, pattern: str, comment: str) -> dict:
    """Build one OC-shaped exec-approval entry for the passthrough set."""
    return {
        "id": f"{_TRIP_PASSTHROUGH_ID_PREFIX}{id_suffix}",
        "pattern": pattern,
        "comment": comment,
    }


def _writer():
    """Lazy import of permissions.writer — only needed when actually editing."""
    from permissions import writer as _w  # noqa: WPS433
    return _w


def _read_openclaw_json_for_enforce(
    bot_id: str, network: dict, *, home_override: Path | None = None,
) -> dict | None:
    writer = _writer()
    home = home_override or _bot_home_dir(bot_id, network)
    return writer.read_openclaw_json(home / ".openclaw" / "openclaw.json")


def _disable_heartbeat_one_bot(
    *,
    bot_id: str,
    shared_dir: Path,
    network: dict,
    dry_run: bool,
    home_override: Path | None = None,
) -> CostEnforcePerBot:
    """Stash heartbeat.every, remove it from openclaw.json, kickstart.

    Test mode (``home_override`` set) skips the kickstart and writes
    directly so unit tests don't shell out.

    No-op when the bot has no ``heartbeat.every`` field set — heartbeats
    weren't scheduled to fire, so disabling them is meaningless. Returns
    ok=True with ``no_op=True`` so the trip flow still succeeds.
    """
    stash_path = _heartbeat_stash_path(shared_dir, bot_id)
    current = _read_openclaw_json_for_enforce(
        bot_id, network, home_override=home_override,
    )
    if current is None:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=False,
            detail="openclaw.json unreadable",
        )

    defaults = (current.get("agents") or {}).get("defaults") or {}
    heartbeat = defaults.get("heartbeat") or {}
    every = heartbeat.get("every") if isinstance(heartbeat, dict) else None
    if not every:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=True, no_op=True,
            no_op_reason="no agents.defaults.heartbeat.every to disable",
        )

    if dry_run:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=True,
            detail=f"dry-run: would stash every={every!r} and remove",
            prior_every=every,
        )

    # Preserve any existing stash — don't overwrite (e.g. re-trip
    # before reset would replace the original heartbeat block with
    # the current-now-empty one, losing the restore target).
    if not stash_path.exists():
        stash_path.parent.mkdir(parents=True, exist_ok=True)
        stash_payload = {"every": every, "stashed_at": _now_iso()}
        stash_path.write_text(json.dumps(stash_payload, indent=2))

    writer = _writer()
    ok, msg = writer.write_openclaw_fields(
        bot_id,
        {},
        field_unsets=["agents.defaults.heartbeat.every"],
        bot_user=_bot_user(bot_id, network),
        home_override=home_override,
        kickstart=home_override is None,
    )
    if not ok:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=False,
            detail=f"write_openclaw_fields failed: {msg}",
        )
    return CostEnforcePerBot(
        bot_id=bot_id, action="trip", ok=True,
        detail=f"heartbeat.every removed (stashed value: {every!r}); gateway kickstarted",
        prior_every=every,
    )


def _restore_heartbeat_one_bot(
    *,
    bot_id: str,
    shared_dir: Path,
    network: dict,
    dry_run: bool,
    home_override: Path | None = None,
) -> CostEnforcePerBot:
    """Restore heartbeat.every from the stash, kickstart gateway, delete stash.

    No-op when no stash exists — the bot either never had its heartbeat
    disabled by this mechanism or it was already restored.
    """
    stash_path = _heartbeat_stash_path(shared_dir, bot_id)
    if not stash_path.exists():
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=True, no_op=True,
            no_op_reason="no heartbeat stash to restore",
        )

    try:
        stash = json.loads(stash_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=False,
            detail=f"stash unreadable: {exc}",
        )
    every = stash.get("every")
    if not every:
        # Malformed stash — delete it so subsequent ticks aren't blocked,
        # and surface the warning. Don't restore an empty value (that's
        # already the post-trip state).
        try:
            stash_path.unlink()
        except OSError:
            pass
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=False,
            detail="stash had no 'every' value; deleted",
        )

    if dry_run:
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=True,
            detail=f"dry-run: would restore every={every!r}",
            prior_every=every,
        )

    writer = _writer()
    ok, msg = writer.write_openclaw_fields(
        bot_id,
        {"agents.defaults.heartbeat.every": every},
        bot_user=_bot_user(bot_id, network),
        home_override=home_override,
        kickstart=home_override is None,
    )
    if not ok:
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=False,
            detail=f"write_openclaw_fields failed: {msg}",
        )
    try:
        stash_path.unlink()
    except OSError:
        pass
    return CostEnforcePerBot(
        bot_id=bot_id, action="reset", ok=True,
        detail=f"heartbeat.every restored to {every!r}; gateway kickstarted",
        prior_every=every,
    )


def _add_passthrough_one_bot(
    *,
    bot_id: str,
    shared_dir: Path,
    network: dict,
    dry_run: bool,
    home_override: Path | None = None,
) -> CostEnforcePerBot:
    """Stash exec-approvals, append narrow trip-mode passthrough patterns, write.

    Reads the bot's exec-approvals.json, captures it to a stash file in
    the breaker store, then writes back the original *plus* the patterns
    in ``_TRIP_PASSTHROUGH_PATTERNS``. On reset the stash is read back
    and the trip-added patterns are subtracted (identified by their
    ``breaker-trip-passthrough-*`` ids — they survive idempotently
    regardless of what the operator did to the allowlist in between).

    No-op when exec-approvals.json is missing (some bots haven't been
    deployed through the L2 path yet) or when the passthrough stash
    already exists (re-trip without an intervening reset).
    """
    stash_path = _passthrough_stash_path(shared_dir, bot_id)
    if stash_path.exists():
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=True, no_op=True,
            no_op_reason="passthrough stash already present (re-trip)",
        )

    writer = _writer()
    current = writer.read_exec_approvals(bot_id, home_override=home_override)
    if current is None:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=True, no_op=True,
            no_op_reason="exec-approvals.json not present",
        )

    # Locate (or create) the agents.main.allowlist list. OC's
    # exec-approvals shape always has this section once the bot has
    # been deployed through L2; we tolerate either path.
    agents = current.setdefault("agents", {})
    main = agents.setdefault("main", {})
    allowlist = main.setdefault("allowlist", [])
    if not isinstance(allowlist, list):
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=False,
            detail="agents.main.allowlist is not a list",
        )

    # Stash the pre-trip allowlist contents (the whole exec-approvals
    # dict) so reset has the full pre-trip state to restore to.
    if dry_run:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=True,
            detail=(
                f"dry-run: would stash {len(allowlist)} prior entries and "
                f"append {len(_TRIP_PASSTHROUGH_PATTERNS)} passthrough patterns"
            ),
        )

    stash_path.parent.mkdir(parents=True, exist_ok=True)
    stash_path.write_text(json.dumps(
        {"exec_approvals": current, "stashed_at": _now_iso()},
        indent=2,
    ))

    # Drop any prior trip-marker entries (shouldn't happen since we
    # checked stash_path above, but defensive in case of operator-
    # imported old state). Then append the trip-mode set.
    allowlist[:] = [
        e for e in allowlist
        if not (
            isinstance(e, dict)
            and str(e.get("id") or "").startswith(_TRIP_PASSTHROUGH_ID_PREFIX)
        )
    ]
    for id_suffix, pattern, comment in _TRIP_PASSTHROUGH_PATTERNS:
        allowlist.append(_trip_passthrough_entry(id_suffix, pattern, comment))

    ok, msg = writer.write_exec_approvals(
        bot_id, current,
        home_override=home_override,
        kickstart_gateway=home_override is None,
    )
    if not ok:
        return CostEnforcePerBot(
            bot_id=bot_id, action="trip", ok=False,
            detail=f"write_exec_approvals failed: {msg}",
        )
    return CostEnforcePerBot(
        bot_id=bot_id, action="trip", ok=True,
        detail=(
            f"appended {len(_TRIP_PASSTHROUGH_PATTERNS)} passthrough patterns "
            f"to exec-approvals.json; gateway kickstarted"
        ),
    )


def _restore_passthrough_one_bot(
    *,
    bot_id: str,
    shared_dir: Path,
    network: dict,
    dry_run: bool,
    home_override: Path | None = None,
) -> CostEnforcePerBot:
    """Remove trip-added patterns from exec-approvals; delete the stash.

    Filters the live exec-approvals.json's ``agents.main.allowlist`` for
    entries whose id has the ``breaker-trip-passthrough-`` prefix and
    drops them. The stash is the authoritative pre-trip state and is
    consulted only if the live allowlist is missing entirely (in which
    case we restore from stash) — otherwise we keep whatever the
    operator did between trip and reset, just minus our additions.
    """
    stash_path = _passthrough_stash_path(shared_dir, bot_id)
    if not stash_path.exists():
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=True, no_op=True,
            no_op_reason="no passthrough stash to restore from",
        )

    writer = _writer()
    current = writer.read_exec_approvals(bot_id, home_override=home_override)
    if current is None:
        # Live file gone — fall back to the stashed pre-trip state.
        try:
            stash = json.loads(stash_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return CostEnforcePerBot(
                bot_id=bot_id, action="reset", ok=False,
                detail=f"stash unreadable: {exc}",
            )
        prior = stash.get("exec_approvals") or {}
        if not isinstance(prior, dict):
            try:
                stash_path.unlink()
            except OSError:
                pass
            return CostEnforcePerBot(
                bot_id=bot_id, action="reset", ok=False,
                detail="stash had no usable exec_approvals payload; deleted",
            )
        current = prior

    agents = current.get("agents") or {}
    main = agents.get("main") or {}
    allowlist = main.get("allowlist")
    if not isinstance(allowlist, list):
        # Nothing to filter — write the file as-is and delete stash.
        if dry_run:
            return CostEnforcePerBot(
                bot_id=bot_id, action="reset", ok=True,
                detail="dry-run: would delete stash (no allowlist to filter)",
            )
        try:
            stash_path.unlink()
        except OSError:
            pass
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=True, no_op=True,
            no_op_reason="no agents.main.allowlist to filter",
        )

    pre_count = len(allowlist)
    allowlist[:] = [
        e for e in allowlist
        if not (
            isinstance(e, dict)
            and str(e.get("id") or "").startswith(_TRIP_PASSTHROUGH_ID_PREFIX)
        )
    ]
    removed = pre_count - len(allowlist)

    if dry_run:
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=True,
            detail=f"dry-run: would remove {removed} passthrough entries",
        )

    ok, msg = writer.write_exec_approvals(
        bot_id, current,
        home_override=home_override,
        kickstart_gateway=home_override is None,
    )
    if not ok:
        return CostEnforcePerBot(
            bot_id=bot_id, action="reset", ok=False,
            detail=f"write_exec_approvals failed: {msg}",
        )
    try:
        stash_path.unlink()
    except OSError:
        pass
    return CostEnforcePerBot(
        bot_id=bot_id, action="reset", ok=True,
        detail=(
            f"removed {removed} trip-passthrough entries from "
            f"exec-approvals.json; gateway kickstarted"
        ),
    )


def _enforce_cost(
    *,
    action: str,
    scope: str,
    network: dict,
    shared_dir: Path,
    dry_run: bool,
    home_override: Path | None = None,
    reason: str = "",
    expires_at_iso: str | None = None,
) -> EnforceResult:
    """L1 cost enforcement: heartbeat-disable (trip) / restore (reset).

    Resolves the affected bot(s) via ``_resolve_target_bots`` (same
    semantics as the L2 path — "pod" expands to every bot). Each bot's
    edit is independent; one bot's failure doesn't block the rest.

    User-channel notification (post-2026-06-04 security-bot incident): on
    ``action="trip"``, after the heartbeat-disable + passthrough-widen
    actions land, post a brief "I've paused background work" message
    to each affected bot's primary user channel. Distinct from the L2
    OOO message — L1 keeps user-driven turns working, so the message
    explicitly says "you can still talk to me." Best-effort: failures
    are recorded but never block the enforcement actions.
    """
    import time
    t0 = time.monotonic()

    targets = _resolve_target_bots(scope, network)
    if not targets:
        return EnforceResult(
            action=action, scope=scope, breaker_type="cost",
            ok=True, dry_run=dry_run, elapsed_ms=0,
            no_op=True, no_op_reason="no bots in network",
        )

    per_bot_cost: list[CostEnforcePerBot] = []
    for bot_id, _user in targets:
        if action == "trip":
            # Step 1: stop auto-turns (heartbeat scheduler).
            res = _disable_heartbeat_one_bot(
                bot_id=bot_id, shared_dir=shared_dir, network=network,
                dry_run=dry_run, home_override=home_override,
            )
            per_bot_cost.append(res)
            # Step 2: open the narrow user-turn exec passthrough so the
            # bot can still answer status questions without approval
            # round-trips. Per the user's guidance after the Security_bot
            # trip incident: the breaker's contract is "stop auto-turns,
            # keep user-driven turns working." File-read patterns
            # (cat / ls) are enough for the common "what's your state?"
            # case; operator can extend per-bot via network.json.
            res_pt = _add_passthrough_one_bot(
                bot_id=bot_id, shared_dir=shared_dir, network=network,
                dry_run=dry_run, home_override=home_override,
            )
            per_bot_cost.append(res_pt)
        elif action == "reset":
            # Step 1 (reverse order is intentional — restore the more
            # constrained state first so a partial failure leaves the
            # bot in the safer of the two configurations).
            res_pt = _restore_passthrough_one_bot(
                bot_id=bot_id, shared_dir=shared_dir, network=network,
                dry_run=dry_run, home_override=home_override,
            )
            per_bot_cost.append(res_pt)
            res = _restore_heartbeat_one_bot(
                bot_id=bot_id, shared_dir=shared_dir, network=network,
                dry_run=dry_run, home_override=home_override,
            )
            per_bot_cost.append(res)
        else:
            raise ValueError(
                f"unknown action {action!r}; expected 'trip' or 'reset'"
            )

    elapsed = int((time.monotonic() - t0) * 1000)

    # ── Best-effort user-channel notification (post-2026-06-04) ───────
    # On trip: tell the bot's primary user that background work is paused
    # but conversation still works. On reset: tell them background work
    # is back. Order is intentional — for trip, notify AFTER the actions
    # land so the message accurately reflects current state. The gateway
    # stays up through L1 enforcement so timing is safe either way.
    # Failures are isolated per-bot; the enforcement result's ok status
    # never flips on a notification failure (operator action is the L1
    # actions, not the user-side courtesy message).
    notifications: list[NotifyResult] = []
    if action == "trip":
        msg = _render_cost_trip_message(
            reason=reason, expires_at_iso=expires_at_iso,
        )
        notifications = _notify_user_channels(
            bots=targets, action="trip", network=network,
            message=msg, dry_run=dry_run,
        )
    elif action == "reset":
        msg = _render_cost_reset_message()
        notifications = _notify_user_channels(
            bots=targets, action="reset", network=network,
            message=msg, dry_run=dry_run,
        )

    # Promote cost-per-bot results into the standard PerBotResult shape
    # so the JSON wire format stays uniform across L1/L2. ``rc`` and
    # ``stderr`` are L2 launchctl artifacts that don't apply here, so
    # we leave them as the L1-native CostEnforcePerBot record instead
    # of fabricating empty fields.
    return EnforceResult(
        action=action, scope=scope, breaker_type="cost",
        ok=all(r.ok for r in per_bot_cost),
        dry_run=dry_run, elapsed_ms=elapsed,
        per_bot=[],  # PerBotResult is L2-shaped; keep it empty for L1
        notifications=notifications,
        no_op=all(r.no_op for r in per_bot_cost),
        no_op_reason=(
            "; ".join(
                f"{r.bot_id}: {r.no_op_reason}"
                for r in per_bot_cost if r.no_op_reason
            ) if all(r.no_op for r in per_bot_cost) else ""
        ),
    ), per_bot_cost


def enforce_trip(
    *,
    scope: str,
    breaker_type: str,
    network: dict,
    shared_dir: Path | None = None,
    dry_run: bool = False,
    reason: str = "",
    expires_at_iso: str | None = None,
    home_override: Path | None = None,
) -> EnforceResult:
    """Synchronously enforce a freshly-tripped breaker.

    For type="full": post an OOO message to each affected bot's
    primary user channel (Phase 4c), then bootout the gateway.
    For type="cost": stash + remove ``agents.defaults.heartbeat.every``
    on each affected bot's openclaw.json and kickstart the gateway
    (partial Phase 3b; see module docstring for rationale).

    ``shared_dir`` is required for cost enforcement (location of the
    breaker store dir for the heartbeat stash). It's optional for
    backwards compatibility with the L2 callers that omit it.

    ``home_override`` is a test hook — bypasses sudo when set, so
    unit tests can drive the cost path against a synthetic /Users
    tree without shelling out.

    ``reason`` and ``expires_at_iso`` are echoed into the OOO message
    for L2 trips. Both default to empty/None — L1 trips ignore them.

    Idempotent: bootout returns rc=36 ("already stopped") as ok, so
    calling enforce_trip when the gateway is already down is fine.
    """
    if breaker_type == "cost":
        if shared_dir is None:
            return EnforceResult(
                action="trip", scope=scope, breaker_type="cost",
                ok=False, dry_run=dry_run,
                no_op_reason="shared_dir required for cost enforcement",
            )
        result, cost_per_bot = _enforce_cost(
            action="trip", scope=scope, network=network,
            shared_dir=shared_dir, dry_run=dry_run,
            home_override=home_override,
            reason=reason, expires_at_iso=expires_at_iso,
        )
        # Attach the cost-side per-bot detail in notifications[] so the
        # CLI/JSON consumers see what happened on each bot, even though
        # we don't fabricate L2-shaped PerBotResult records.
        # Append (don't overwrite) so the user-channel notifications that
        # ``_enforce_cost`` already populated stay in place — they sit
        # alongside the enforcement-action records under the same field.
        user_channel_notifications = list(result.notifications)
        result.notifications = [
            NotifyResult(
                bot_id=r.bot_id,
                action="trip",
                attempted=not r.no_op,
                sent=r.ok and not r.no_op,
                skip_reason=r.no_op_reason if r.no_op else "",
                error=r.detail if (not r.ok and not r.no_op) else "",
            )
            for r in cost_per_bot
        ] + user_channel_notifications
        return result
    if breaker_type == "cost_l2":
        # Phase 6 of the 2026-06 cost-cap normalization: cost-driven L2
        # trip — same launchctl bootout as the operator-driven "full"
        # breaker, but scoped to a single bot and recorded under the
        # cost_l2 breaker key (separate from L1 "cost"). The bootout
        # behavior + user-channel OOO message are reused unchanged.
        # Spec: docs/spec-cost-caps-2026-06-05.md.
        return _enforce_full(
            action="trip", scope=scope, network=network, dry_run=dry_run,
            reason=reason, expires_at_iso=expires_at_iso,
        )
    if breaker_type == "full":
        return _enforce_full(
            action="trip", scope=scope, network=network, dry_run=dry_run,
            reason=reason, expires_at_iso=expires_at_iso,
        )
    raise ValueError(
        f"unknown breaker type {breaker_type!r}; "
        f"valid: ['cost', 'cost_l2', 'full']"
    )


def enforce_reset(
    *,
    scope: str,
    breaker_type: str,
    network: dict,
    shared_dir: Path | None = None,
    dry_run: bool = False,
    home_override: Path | None = None,
) -> EnforceResult:
    """Synchronously bring a bot back up after its breaker is reset.

    For type="full": bootstrap the affected bot(s), then post an
    "I'm back" message to each one's primary user channel (Phase 4c).
    For type="cost": restore ``agents.defaults.heartbeat.every`` from
    the stash on each affected bot and kickstart the gateway. No-op
    when no stash exists.

    rc=37 ("service already loaded") is treated as ok — if heal
    or someone else already restarted the gateway, the goal state
    holds.
    """
    if breaker_type == "cost":
        if shared_dir is None:
            return EnforceResult(
                action="reset", scope=scope, breaker_type="cost",
                ok=False, dry_run=dry_run,
                no_op_reason="shared_dir required for cost enforcement",
            )
        result, cost_per_bot = _enforce_cost(
            action="reset", scope=scope, network=network,
            shared_dir=shared_dir, dry_run=dry_run,
            home_override=home_override,
        )
        user_channel_notifications = list(result.notifications)
        result.notifications = [
            NotifyResult(
                bot_id=r.bot_id,
                action="reset",
                attempted=not r.no_op,
                sent=r.ok and not r.no_op,
                skip_reason=r.no_op_reason if r.no_op else "",
                error=r.detail if (not r.ok and not r.no_op) else "",
            )
            for r in cost_per_bot
        ] + user_channel_notifications
        return result
    if breaker_type == "cost_l2":
        # Phase 6: L2 cost-reset reuses the launchctl bootstrap path
        # the operator-driven "full" breaker uses. Manual operator action
        # required — there's no auto-reset for L2.
        return _enforce_full(
            action="reset", scope=scope, network=network, dry_run=dry_run,
        )
    if breaker_type == "full":
        return _enforce_full(
            action="reset", scope=scope, network=network, dry_run=dry_run,
        )
    raise ValueError(
        f"unknown breaker type {breaker_type!r}; "
        f"valid: ['cost', 'cost_l2', 'full']"
    )
