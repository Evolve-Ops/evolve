"""Global outbound circuit-breaker / rate cap for the alert dispatcher.

The hard, last-line guarantee that Evolve can **never** firehose the
operator's chat, no matter how many Signals fire or how individual
producers are configured.

Per-producer protections (settle / flap / maturity gates, the coverage
registry) are opt-in per producer — a misbehaving or brand-new producer
can still flood. This module is the **producer-agnostic backstop**: a
ceiling enforced at the single chokepoint every outbound message passes
through (``dispatcher.send`` → :func:`evaluate`).

What it does
------------

1. **Rolling-window rate cap (per channel).** Across *all* producers, at
   most ``max_per_window`` immediate sends per channel per rolling window
   (default ≤ 10 / hour). Over-cap alerts are **batched into the daily
   digest**, never dropped.

2. **Storm mode (hysteresis circuit-breaker).** When arrivals stay above
   a hard threshold (``storm_trip_count``), the channel trips to
   **digest-only** for non-critical alerts and the operator gets a single
   periodic heartbeat. It auto-resets once arrivals fall below the
   low-water mark — the trip/reset gap prevents flapping.

3. **Exactly-one storm notice per episode.** The operator gets ONE
   "🌊 Alert storm" notice when batching begins (not one per suppressed
   alert), plus an hourly heartbeat while a sustained storm persists.

4. **Critical bypass with its own ceiling.** ``is_bypass`` alerts (gateway
   down, security floor) keep delivering during a storm, but under a
   separate, higher ceiling (``critical_max_per_window``) so even a
   misfiring "critical" producer can't firehose.

Design notes
------------

- **Never drops.** A capped alert is *batched* (rolled into the daily
  digest) — the dispatcher records every one in the recent-messages log,
  so a silenced phone always has a paper trail.
- **Counts attempts, not just deliveries.** The cap gates *deliveries*
  (so batching genuinely reduces chat volume), but storm detection gates
  on *arrivals* (so a flood that's already being batched is still
  recognized as a flood). Two windows: ``sends`` / ``bypass_sends`` for
  the ceilings, ``attempts`` for storm hysteresis.
- **Fails open.** Any error reading config/state returns a permissive
  decision (``allow=True``) — the backstop must never itself become the
  thing that blocks a legitimate alert.
- State lives at ``{shared_dir}/alerts/rate-breaker-state.json``, owned by
  the evolve user (atomic temp-file + rename, same as the dispatcher's
  cooldown state). No sudo / /tmp staging — it's under ``{shared_dir}``.
- **Concurrency**: the window state is read-modify-write without a lock,
  so two daemons sending at the exact same instant could each miss the
  other's increment and let the cap leak slightly above N. This matches
  the dispatcher's existing cooldown-state design and is acceptable for a
  *ceiling* — a small overcount under heavy concurrency is fine; the
  guarantee is "never a firehose," not "exactly N." In practice the worst
  floods come from one daemon looping (signal_notifier draining a backlog),
  which is serial and races with nothing.

The dispatcher consults this module at one call site. The actual batching
(digest enqueue) + storm-notice delivery stay in ``dispatcher`` because
they need its delivery primitives; this module owns the *decision* and the
window/storm *state*.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── Compiled-in defaults (operator-tunable via config_sandbox) ──────────────
# Mirrored in config_sandbox/schema.py under ``alerts.rate_cap.*`` so the
# admin UI can surface them; the drift test pins schema ↔ these defaults.

# Master switch. Default ON — the backstop is on by construction; an
# operator can disable it but the whole point is that it's always there.
_DEFAULT_ENABLED = True

# Rolling window the cap + storm thresholds are measured over.
_DEFAULT_WINDOW_SECONDS = 3600  # 1 hour

# Normal cap: immediate (non-bypass) sends per channel per window. Past
# this, additional alerts batch into the daily digest. Conservative but
# not so low it hides a genuine multi-incident burst — a 10-alert hour
# still all delivers; the 11th starts batching.
_DEFAULT_MAX_PER_WINDOW = 10

# Secondary ceiling for bypass (critical / safety-critical) alerts. Higher
# than the normal cap so a real incident's criticals get through, but still
# finite so a misfiring "critical" producer can't firehose either.
_DEFAULT_CRITICAL_MAX_PER_WINDOW = 30

# Storm trip: when *arrivals* (all sources, this channel) reach this count
# in the window, escalate to digest-only mode for non-bypass alerts +
# emit the heartbeat. 2× the normal cap — i.e. "the cap isn't just barely
# exceeded, this is a flood."
_DEFAULT_STORM_TRIP_COUNT = 20

# Storm reset: the episode ends (and notice state clears) only when
# arrivals fall below this low-water mark. The trip/reset gap
# (20 → 5) is the hysteresis band that prevents flapping in/out of storm
# mode while volume hovers near the cap.
_DEFAULT_STORM_LOW_WATER = 5

# How often to re-emit the "still in storm mode" heartbeat while a
# sustained storm persists. One per hour by default.
_DEFAULT_STORM_HEARTBEAT_SECONDS = 3600

# Hard bound on how many timestamps we retain per window list, so a
# pathological flood can't bloat the state file unboundedly. We only ever
# need to compare counts against thresholds well below this, and a list at
# the cap already implies "far over every threshold."
_MAX_RETAINED_TIMESTAMPS = 2000


@dataclass(frozen=True)
class BreakerConfig:
    enabled: bool
    window_seconds: int
    max_per_window: int
    critical_max_per_window: int
    storm_trip_count: int
    storm_low_water: int
    storm_heartbeat_seconds: int


@dataclass(frozen=True)
class BreakerDecision:
    """Outcome of consulting the breaker for one outbound message.

    ``allow`` is the load-bearing field: True → deliver immediately;
    False → the dispatcher batches the message into the daily digest.
    ``storm_notice`` (when non-None) is operator-facing text the
    dispatcher should deliver exactly once — the episode-start notice or
    a heartbeat. ``state`` / ``window_count`` are for the audit log + the
    Dispatcher Health surface.
    """

    allow: bool
    state: str            # "normal" | "batching" | "storm" | "disabled"
    reason: str
    channel: str
    window_count: int     # immediate (non-bypass) sends in window, post-decision
    attempts_count: int   # all arrivals in window, post-decision
    bypass: bool
    storm_notice: str | None = None


# States that a channel can be reported in.
STATE_NORMAL = "normal"
STATE_BATCHING = "batching"
STATE_STORM = "storm"
STATE_DISABLED = "disabled"


# ─── Public entrypoint ───────────────────────────────────────────────────────


def evaluate(
    shared_dir: Path,
    *,
    channel: str,
    is_bypass: bool,
    now: datetime | None = None,
    config: BreakerConfig | None = None,
) -> BreakerDecision:
    """Decide whether an outbound message on ``channel`` may send now.

    Records the decision into the per-channel rolling-window state
    (atomic write). Call exactly once per message the dispatcher is about
    to deliver — *after* every per-source gate has passed and the
    recipient channel is known, *before* the actual send.

    Fails open: on any config/state error returns ``allow=True`` with
    ``state="disabled"`` so the backstop never blocks a legitimate alert
    by malfunctioning.
    """
    now = _utcnow(now)
    try:
        cfg = config or _load_config(shared_dir)
    except Exception:
        return _fail_open(channel, "config_error")

    if not cfg.enabled:
        return BreakerDecision(
            allow=True, state=STATE_DISABLED, reason="breaker_disabled",
            channel=channel, window_count=0, attempts_count=0, bypass=is_bypass,
        )

    try:
        return _evaluate_locked(shared_dir, channel, is_bypass, now, cfg)
    except Exception:
        # State read/write blew up — fail open, but don't crash the dispatch.
        return _fail_open(channel, "state_error")


def _fail_open(channel: str, reason: str) -> BreakerDecision:
    return BreakerDecision(
        allow=True, state=STATE_DISABLED, reason=reason,
        channel=channel, window_count=0, attempts_count=0, bypass=False,
    )


def _evaluate_locked(
    shared_dir: Path, channel: str, is_bypass: bool,
    now: datetime, cfg: BreakerConfig,
) -> BreakerDecision:
    state = _load_state(shared_dir)
    ch = _channel_state(state, channel)

    window_start = now.timestamp() - cfg.window_seconds

    sends = _prune(ch.get("sends", []), window_start)
    bypass_sends = _prune(ch.get("bypass_sends", []), window_start)
    attempts = _prune(ch.get("attempts", []), window_start)

    # Every arrival counts toward the storm window — including criticals,
    # since a flood of "critical" is still a flood worth recognizing.
    attempts.append(_iso(now))
    attempts = attempts[-_MAX_RETAINED_TIMESTAMPS:]
    attempts_ct = len(attempts)

    episode = ch.get("episode") or {}
    episode_active = bool(episode.get("active"))
    notice_sent = bool(episode.get("notice_sent"))
    last_heartbeat = episode.get("last_heartbeat")

    delivered = len(sends)
    bypass_delivered = len(bypass_sends)
    over_cap = delivered >= cfg.max_per_window
    hard_flood = attempts_ct >= cfg.storm_trip_count

    # ── Episode lifecycle (hysteresis) ──────────────────────────────────
    if not episode_active and (over_cap or hard_flood):
        episode_active = True
        notice_sent = False
        last_heartbeat = None
        episode["since"] = _iso(now)
    elif episode_active and attempts_ct < cfg.storm_low_water and not over_cap:
        # Volume has subsided below the low-water mark — close the episode.
        episode_active = False
        notice_sent = False
        last_heartbeat = None
        episode["since"] = None

    storm_notice: str | None = None

    # ── Decide allow/batch ──────────────────────────────────────────────
    if is_bypass:
        # Critical / safety-critical: bypass the normal cap, but enforce a
        # higher secondary ceiling so even these can't firehose.
        if bypass_delivered < cfg.critical_max_per_window:
            allow = True
            reason = "bypass"
            bypass_sends.append(_iso(now))
        else:
            allow = False
            reason = "critical_ceiling"
    elif episode_active:
        if hard_flood:
            # Digest-only: batch every non-bypass alert while the flood is
            # sustained, regardless of whether a delivery slot is free.
            allow = False
            reason = "storm_digest_only"
        else:
            # Batching (de-escalating): trickle up to the cap as old sends
            # age out, otherwise batch.
            if delivered < cfg.max_per_window:
                allow = True
                reason = "trickle"
                sends.append(_iso(now))
            else:
                allow = False
                reason = "rate_cap"
    else:
        # Normal path — should always be under cap here (over_cap would have
        # started an episode above), but guard anyway.
        if delivered < cfg.max_per_window:
            allow = True
            reason = "normal"
            sends.append(_iso(now))
        else:
            allow = False
            reason = "rate_cap"

    # ── State label for observability ───────────────────────────────────
    if not episode_active:
        label = STATE_NORMAL
    elif hard_flood:
        label = STATE_STORM
    else:
        label = STATE_BATCHING

    # ── Storm notice: one episode-start notice, then periodic heartbeats ─
    # The heartbeat fires for ANY active episode (batching or storm), not
    # only storm. Two reasons: (1) a batching episode that's steadily
    # capping alerts away must not go silent — the operator needs the
    # recurring "still batching" reminder (silent suppression is itself a
    # failure mode); (2) the episode-start notice is best-effort delivered
    # by the dispatcher and can fail (Telegram is the most likely thing
    # rate-limiting during a flood) — the heartbeat is the recovery path so
    # a dropped start notice still surfaces within one interval.
    if episode_active:
        if not notice_sent:
            storm_notice = _episode_start_notice(attempts_ct, cfg)
            notice_sent = True
            last_heartbeat = _iso(now)
        elif _heartbeat_due(last_heartbeat, now, cfg):
            storm_notice = _heartbeat_notice(attempts_ct, label, cfg)
            last_heartbeat = _iso(now)

    # Track how many we've batched this episode (observability only).
    batched_total = int(ch.get("batched_total") or 0)
    if not allow:
        batched_total += 1

    # ── Persist ─────────────────────────────────────────────────────────
    ch["sends"] = sends[-_MAX_RETAINED_TIMESTAMPS:]
    ch["bypass_sends"] = bypass_sends[-_MAX_RETAINED_TIMESTAMPS:]
    ch["attempts"] = attempts
    episode["active"] = episode_active
    episode["notice_sent"] = notice_sent
    episode["last_heartbeat"] = last_heartbeat
    ch["episode"] = episode
    ch["batched_total"] = batched_total
    ch["updated_at"] = _iso(now)
    _save_state(shared_dir, state)

    return BreakerDecision(
        allow=allow,
        state=label,
        reason=reason,
        channel=channel,
        window_count=len(sends),
        attempts_count=attempts_ct,
        bypass=is_bypass,
        storm_notice=storm_notice,
    )


# ─── Notice text ─────────────────────────────────────────────────────────────


def _episode_start_notice(attempts: int, cfg: BreakerConfig) -> str:
    # Phrased off *arrivals* (attempts), not deliveries: a storm tripped by
    # a flood of criticals delivers 0 non-critical alerts, so "sent N" would
    # read "sent 0" — confusing. Arrivals is always the meaningful count.
    hrs = _window_phrase(cfg)
    return (
        "🌊 Alert storm — "
        f"{attempts} alerts fired in the {hrs}, so Evolve is now batching "
        "non-critical alerts into your daily digest to keep this chat from "
        "flooding. Critical alerts still come through immediately."
    )


def _heartbeat_notice(attempts: int, state: str, cfg: BreakerConfig) -> str:
    hrs = _window_phrase(cfg)
    lead = "Still in storm mode" if state == STATE_STORM else "Still batching"
    return (
        f"🌊 {lead} — "
        f"{attempts} alerts have arrived in the {hrs}. Non-critical alerts "
        "are going to your daily digest until the rate subsides. "
        "(This reminder repeats hourly until then.)"
    )


def _window_phrase(cfg: BreakerConfig) -> str:
    if cfg.window_seconds == 3600:
        return "last hour"
    mins = max(1, round(cfg.window_seconds / 60))
    return f"last {mins} min"


def _heartbeat_due(last_heartbeat: str | None, now: datetime, cfg: BreakerConfig) -> bool:
    if not last_heartbeat:
        return True
    try:
        last = datetime.fromisoformat(str(last_heartbeat).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    return (now - last).total_seconds() >= cfg.storm_heartbeat_seconds


# ─── Read-only health snapshot (for the Dispatcher Health surface) ───────────


def breaker_health(
    shared_dir: Path,
    *,
    now: datetime | None = None,
    config: BreakerConfig | None = None,
) -> dict[str, Any]:
    """Read-only snapshot of the breaker's per-channel state.

    Pure / side-effect-free (does NOT prune or persist) so the Dispatcher
    Health route can call it on every panel load. Surfaces the current
    state (normal / batching / storm), the live window counts, and the
    cumulative batched count so the operator can see *why* their phone
    went quiet — silent suppression is itself a failure mode.
    """
    now = _utcnow(now)
    try:
        cfg = config or _load_config(shared_dir)
    except Exception:
        cfg = _stock_config()

    out: dict[str, Any] = {
        "enabled": cfg.enabled,
        "window_seconds": cfg.window_seconds,
        "max_per_window": cfg.max_per_window,
        "critical_max_per_window": cfg.critical_max_per_window,
        "channels": {},
        "any_storm": False,
        "any_batching": False,
    }
    if not cfg.enabled:
        return out

    try:
        state = _load_state(shared_dir)
    except Exception:
        return out

    window_start = now.timestamp() - cfg.window_seconds
    any_storm = False
    any_batching = False
    for channel, ch in (state.get("channels") or {}).items():
        if not isinstance(ch, dict):
            continue
        sends = _prune(list(ch.get("sends", [])), window_start)
        bypass_sends = _prune(list(ch.get("bypass_sends", [])), window_start)
        attempts = _prune(list(ch.get("attempts", [])), window_start)
        episode = ch.get("episode") or {}
        active = bool(episode.get("active"))
        hard_flood = len(attempts) >= cfg.storm_trip_count
        if not active:
            label = STATE_NORMAL
        elif hard_flood:
            label = STATE_STORM
        else:
            label = STATE_BATCHING
        any_storm = any_storm or (label == STATE_STORM)
        any_batching = any_batching or active
        out["channels"][channel] = {
            "state": label,
            "delivered_in_window": len(sends),
            "bypass_delivered_in_window": len(bypass_sends),
            "attempts_in_window": len(attempts),
            "episode_since": episode.get("since"),
            "batched_total": int(ch.get("batched_total") or 0),
            "updated_at": ch.get("updated_at"),
        }
    out["any_storm"] = any_storm
    out["any_batching"] = any_batching
    return out


# ─── Config ──────────────────────────────────────────────────────────────────


def _stock_config() -> BreakerConfig:
    return BreakerConfig(
        enabled=_DEFAULT_ENABLED,
        window_seconds=_DEFAULT_WINDOW_SECONDS,
        max_per_window=_DEFAULT_MAX_PER_WINDOW,
        critical_max_per_window=_DEFAULT_CRITICAL_MAX_PER_WINDOW,
        storm_trip_count=_DEFAULT_STORM_TRIP_COUNT,
        storm_low_water=_DEFAULT_STORM_LOW_WATER,
        storm_heartbeat_seconds=_DEFAULT_STORM_HEARTBEAT_SECONDS,
    )


def _load_config(shared_dir: Path) -> BreakerConfig:
    """Resolve operator-tunable knobs, falling back to compiled defaults.

    Uses the dispatcher's same defensive ``_config_lookup`` path, so the
    knobs are read identically whether or not the config file / schema
    entry exists.
    """
    def _int(path: str, default: int) -> int:
        try:
            return int(_lookup(shared_dir, path, default))
        except (TypeError, ValueError):
            return default

    window_seconds = max(1, _int("alerts.rate_cap.window_seconds", _DEFAULT_WINDOW_SECONDS))
    max_per_window = max(1, _int("alerts.rate_cap.max_per_window", _DEFAULT_MAX_PER_WINDOW))
    critical_max = max(
        max_per_window,
        _int("alerts.rate_cap.critical_max_per_window", _DEFAULT_CRITICAL_MAX_PER_WINDOW),
    )
    trip = max(1, _int("alerts.rate_cap.storm_trip_count", _DEFAULT_STORM_TRIP_COUNT))
    # storm_low_water MUST stay strictly below storm_trip_count: if an
    # operator misconfigures low_water >= trip, the hysteresis inverts and a
    # channel can wedge permanently in storm mode (it re-trips at >= trip on
    # every call but can never fall "below" the reset floor). Clamp to keep
    # a sane gap rather than honor a self-defeating config.
    low_water = _int("alerts.rate_cap.storm_low_water", _DEFAULT_STORM_LOW_WATER)
    low_water = max(0, min(low_water, trip - 1))
    return BreakerConfig(
        enabled=bool(_lookup(shared_dir, "alerts.rate_cap.enabled", _DEFAULT_ENABLED)),
        window_seconds=window_seconds,
        max_per_window=max_per_window,
        critical_max_per_window=critical_max,
        storm_trip_count=trip,
        storm_low_water=low_water,
        storm_heartbeat_seconds=max(
            1, _int("alerts.rate_cap.storm_heartbeat_seconds", _DEFAULT_STORM_HEARTBEAT_SECONDS),
        ),
    )


def _lookup(shared_dir: Path, path: str, default: Any) -> Any:
    try:
        from . import _config_lookup
        return _config_lookup.lookup(shared_dir, path, default)
    except Exception:
        return default


# ─── State file (atomic, evolve-owned) ───────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "alerts" / "rate-breaker-state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "channels": {}}
    if not isinstance(data, dict):
        return {"version": 1, "channels": {}}
    data.setdefault("channels", {})
    return data


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _channel_state(state: dict[str, Any], channel: str) -> dict[str, Any]:
    channels = state.setdefault("channels", {})
    ch = channels.get(channel)
    if not isinstance(ch, dict):
        ch = {}
        channels[channel] = ch
    return ch


def _prune(timestamps: list[Any], window_start_epoch: float) -> list[str]:
    """Keep only ISO timestamps at-or-after ``window_start_epoch``.

    Malformed entries are dropped. Returns a fresh list of ISO strings.
    """
    out: list[str] = []
    for ts in timestamps:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.timestamp() >= window_start_epoch:
            out.append(str(ts))
    return out


# ─── Time helpers ────────────────────────────────────────────────────────────


def _utcnow(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
