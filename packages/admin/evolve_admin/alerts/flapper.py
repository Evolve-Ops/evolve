"""Recurring-flapper demotion — the multi-hour-flap layer ABOVE the grace.

Spec: internal/spec-subscription-digest-default-2026-06-28.md (workstream D3).
Composes with — does NOT replace — the single-transient suppression layers:

  - internal/spec-transient-signal-suppression-2026-06-23.md (producer-side umbrella)
  - internal/spec-delta-transient-delivery-grace-2026-06-26.md (per-severity grace,
    ``grace.py``; recurring-flap window, ``signal_notifier._read_flap_window_seconds``)

The gap this closes (pipeline-map #4)
-------------------------------------
The deployed defences each cancel ONE fire+clear cycle within their own
lifetime:

  * **Per-severity grace** (``grace.py``, default warn/info 900s) holds a
    *first* fire and elides a fire+clear pair only when the Signal's *lifetime*
    was shorter than the grace.
  * **Recurring-flap window** (``signal_notifier``, default 1800s) suppresses a
    *re-fire* that lands within the window of the last resolve.

A condition that oscillates every ~couple hours with ~15–20 min lifetimes
evades BOTH: each lifetime (~900–1200s) EXCEEDS the 900s grace, so the pair is
never cancelled; and each re-fire lands ~2h after the prior resolve, well
OUTSIDE the 1800s flap window, so it is never suppressed. The result is a fresh
page on every cycle — 4–8×/day for the evo-vps ``pod_perms_drift`` / audit
findings that motivated this layer.

What this layer does
--------------------
Tracks, per ``(source, coalesce_key)``, the number of *oscillations* (distinct
fires that reached the dispatch chokepoint) inside a rolling 24h window. When a
signature has flapped ``≥ count`` times (default 4), it is **auto-demoted**:

  * subsequent **fires** route to the daily digest (``DispatchResult.DEFERRED``,
    the same path a ``daily_digest`` subscription takes) instead of paging, and
  * its standalone **clears** are taken off the immediate-push path too,

until the signature has been **stable** (no new fire) for a cooldown (default
6h), at which point the demotion **self-lifts** and the next fire pages normally
again. The dispatcher is the one call site; routing/logging live there, the
window + episode *state* live here — exactly the ``rate_breaker.py`` split.

Invariant — ``alert``/critical is never demoted
-----------------------------------------------
An ``alert``-severity signature is EXEMPT: a must-page condition is never
demoted, never has its clear suppressed, and is not even counted (so a brief
escalation of an otherwise-flappy signature to ``alert`` pages immediately and
un-demotes it for free). The dispatcher passes the Signal's OWN severity
(``digest_meta["signal_severity"]``) — not the INFO wrapper a resolve send
uses — so this gate sees the truth.

Distinct from the rate breaker
------------------------------
``rate_breaker.py`` is a per-*channel* volume ceiling (≤ N immediate sends/hour
across ALL producers). This is per-*signature* and based on *oscillation*, not
volume: a signature that flaps 4× in a day is demoted even if the channel is
nowhere near its rate cap. The two compose at the same chokepoint.

State lives at ``{shared_dir}/alerts/flap-demotion-state.json``, owned by the
evolve user (atomic temp-file + rename, same as the rate breaker's state). No
sudo / /tmp staging — it's under ``{shared_dir}``. Fails open: any config/state
error yields a permissive PASS so the demotion logic can never itself swallow a
legitimate alert.
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

# ─── Compiled-in defaults (operator-tunable via alerts.flap_demote.*) ────────
# Mirrored in config_sandbox/schema.py under ``alerts.flap_demote.*`` so the
# admin UI surfaces them, in the same shape as the rate_cap / grace knobs.

# Master switch. Default ON — this layer is the residual fix for the cases the
# grace + flap-window layers structurally cannot catch (multi-hour flap).
_DEFAULT_ENABLED = True

# Oscillations (distinct fires reaching the chokepoint) within the window that
# trip demotion. K=4: the evo-vps motivating case flaps 4–8×/day, so 4 catches
# it within the first day while leaving a genuine fire-clear-fire blip (≤3
# cycles) alone.
_DEFAULT_FLAP_COUNT = 4

# Rolling window the oscillation count is measured over. 24h: the motivating
# cadence is "a few times a day", so the natural accounting period is the day.
_DEFAULT_WINDOW_SECONDS = 86_400  # 24h

# Stability cooldown: once demoted, the signature must go this long with NO new
# fire before the demotion self-lifts. 6h ≈ 3× the ~2h motivating flap cadence,
# so a couple of missed cycles confirm the condition has genuinely settled
# before the operator is paged on it again.
_DEFAULT_COOLDOWN_SECONDS = 21_600  # 6h

# Hard bound on retained fire timestamps per signature so a pathological flood
# can't bloat the state file. Far above any real flap count; a list at the cap
# already implies "demoted many times over".
_MAX_RETAINED_TIMESTAMPS = 500


# ─── Actions / decision ──────────────────────────────────────────────────────

# What the caller should do with this dispatch.
ACTION_PASS = "pass"                     # deliver normally (not demoted)
ACTION_DEMOTE_FIRE = "demote_fire"       # route the fire to the daily digest
ACTION_SUPPRESS_CLEAR = "suppress_clear"  # take the standalone clear off immediate push


@dataclass(frozen=True)
class FlapperConfig:
    enabled: bool
    flap_count: int
    window_seconds: int
    cooldown_seconds: int


@dataclass(frozen=True)
class FlapperDecision:
    """Outcome of consulting the flapper for one outbound fire/clear.

    ``action`` is the load-bearing field — ``ACTION_PASS`` means the caller
    delivers as usual; the two demote actions tell the caller to route the
    message to the daily digest instead of paging. ``newly_demoted`` is True
    only on the call that crossed the threshold (so the caller records the
    demotion once, for the Reports surface), and ``lifted`` is True on the call
    that self-lifted a prior demotion. The rest are for the audit log + the
    Dispatcher Health snapshot.
    """

    action: str
    demoted: bool
    newly_demoted: bool
    lifted: bool
    reason: str
    source: str
    coalesce_key: str
    severity: str
    flap_count: int


# ─── Public entrypoint ───────────────────────────────────────────────────────


def evaluate(
    shared_dir: Path,
    *,
    source: str,
    coalesce_key: str,
    kind: str,
    severity: str,
    now: datetime | None = None,
    config: FlapperConfig | None = None,
) -> FlapperDecision:
    """Decide whether a fire/clear for ``(source, coalesce_key)`` is demoted.

    Records the fire into the per-signature rolling window (atomic write).
    Call exactly once per fire OR clear the dispatcher is about to deliver,
    after the per-source / subscription gates have passed and the event is
    confirmed immediate.

    ``kind`` is ``"fire"`` or ``"resolve"``. Only fires count toward the
    oscillation threshold and reset the stability clock; a resolve is gated
    by the current demotion state but does not advance it.

    ``severity`` is the Signal's OWN severity — ``alert`` is exempt (never
    demoted, never counted). Fails open: any error yields ``ACTION_PASS``.
    """
    now = _utcnow(now)
    sev = (severity or "").strip().lower()

    # Alert-severity is exempt end-to-end — never demoted, never counted, no
    # state mutation. A must-page condition is never quieted by this layer.
    if sev == "alert":
        return _pass(source, coalesce_key, sev, reason="alert_exempt", flap_count=0)

    try:
        cfg = config or _load_config(shared_dir)
    except Exception:
        return _pass(source, coalesce_key, sev, reason="config_error", flap_count=0)

    if not cfg.enabled:
        return _pass(source, coalesce_key, sev, reason="disabled", flap_count=0)

    if kind not in ("fire", "resolve"):
        # Unknown lifecycle phase — don't touch state, just pass.
        return _pass(source, coalesce_key, sev, reason="unknown_kind", flap_count=0)

    try:
        return _evaluate_locked(shared_dir, source, coalesce_key, kind, sev, now, cfg)
    except Exception:
        return _pass(source, coalesce_key, sev, reason="state_error", flap_count=0)


def _pass(
    source: str, coalesce_key: str, severity: str, *, reason: str, flap_count: int,
) -> FlapperDecision:
    return FlapperDecision(
        action=ACTION_PASS, demoted=False, newly_demoted=False, lifted=False,
        reason=reason, source=source, coalesce_key=coalesce_key,
        severity=severity, flap_count=flap_count,
    )


def _evaluate_locked(
    shared_dir: Path, source: str, coalesce_key: str, kind: str,
    severity: str, now: datetime, cfg: FlapperConfig,
) -> FlapperDecision:
    state = _load_state(shared_dir)
    key = _sig_key(source, coalesce_key)
    entry = _entry(state, key)

    window_start = now.timestamp() - cfg.window_seconds
    fires = _prune(entry.get("fires", []), window_start)

    demoted = bool(entry.get("demoted"))
    last_fire_at = _parse_iso(entry.get("last_fire_at"))
    demote_count = int(entry.get("demote_count") or 0)

    # ── Self-lift (hysteresis): stable past the cooldown → release ──────────
    # Checked BEFORE this event so a fire arriving after a long quiet spell is
    # treated as a fresh start (window cleared) rather than counted against the
    # stale pre-quiet oscillations.
    lifted = False
    if demoted and last_fire_at is not None:
        stable_for = (now - last_fire_at).total_seconds()
        if stable_for >= cfg.cooldown_seconds:
            demoted = False
            lifted = True
            fires = []  # fresh accounting after a self-lift

    newly_demoted = False

    if kind == "fire":
        fires.append(_iso(now))
        fires = fires[-_MAX_RETAINED_TIMESTAMPS:]
        last_fire_at = now
        flap_count = len(fires)
        if not demoted and flap_count >= cfg.flap_count:
            demoted = True
            newly_demoted = True
            demote_count += 1
            entry["demoted_since"] = _iso(now)
        if demoted:
            action = ACTION_DEMOTE_FIRE
            reason = "newly_demoted" if newly_demoted else "demoted"
        else:
            action = ACTION_PASS
            reason = "below_threshold"
    else:  # resolve
        flap_count = len(fires)
        if demoted:
            action = ACTION_SUPPRESS_CLEAR
            reason = "demoted"
        else:
            action = ACTION_PASS
            reason = "not_demoted"

    # ── Persist ─────────────────────────────────────────────────────────────
    entry["fires"] = fires
    entry["demoted"] = demoted
    entry["last_fire_at"] = _iso(last_fire_at) if last_fire_at else None
    entry["severity"] = severity
    entry["demote_count"] = demote_count
    if not demoted:
        entry["demoted_since"] = None
    entry["updated_at"] = _iso(now)
    # Drop entries that carry no signal anymore so the file doesn't grow
    # unbounded: not demoted AND no fires left in the window.
    if not demoted and not fires:
        state.get("signatures", {}).pop(key, None)
    _save_state(shared_dir, state)

    return FlapperDecision(
        action=action,
        demoted=demoted,
        newly_demoted=newly_demoted,
        lifted=lifted,
        reason=reason,
        source=source,
        coalesce_key=coalesce_key,
        severity=severity,
        flap_count=flap_count,
    )


# ─── Read-only health snapshot (for the Reports / Dispatcher Health surface) ─


def flap_health(
    shared_dir: Path,
    *,
    now: datetime | None = None,
    config: FlapperConfig | None = None,
) -> dict[str, Any]:
    """Read-only snapshot of currently-demoted signatures.

    Pure / side-effect-free (does NOT prune or persist) so the Reports route
    can call it on every panel load. Surfaces which signatures are demoted to
    the digest and since when, so a quiet phone is explained by *visible* state
    rather than silent suppression — the same "show why it went quiet" contract
    the rate breaker's ``breaker_health`` honors.
    """
    now = _utcnow(now)
    try:
        cfg = config or _load_config(shared_dir)
    except Exception:
        cfg = _stock_config()

    out: dict[str, Any] = {
        "enabled": cfg.enabled,
        "flap_count": cfg.flap_count,
        "window_seconds": cfg.window_seconds,
        "cooldown_seconds": cfg.cooldown_seconds,
        "demoted": [],
        "any_demoted": False,
    }
    if not cfg.enabled:
        return out

    try:
        state = _load_state(shared_dir)
    except Exception:
        return out

    window_start = now.timestamp() - cfg.window_seconds
    demoted: list[dict[str, Any]] = []
    for key, entry in (state.get("signatures") or {}).items():
        if not isinstance(entry, dict) or not entry.get("demoted"):
            continue
        source, coalesce_key = _split_key(key)
        demoted.append({
            "source": source,
            "coalesce_key": coalesce_key,
            "severity": entry.get("severity"),
            "demoted_since": entry.get("demoted_since"),
            "flap_count": len(_prune(entry.get("fires", []), window_start)),
            "last_fire_at": entry.get("last_fire_at"),
            "demote_count": int(entry.get("demote_count") or 0),
        })
    demoted.sort(key=lambda d: d.get("demoted_since") or "", reverse=True)
    out["demoted"] = demoted
    out["any_demoted"] = bool(demoted)
    return out


# ─── Config ──────────────────────────────────────────────────────────────────


def _stock_config() -> FlapperConfig:
    return FlapperConfig(
        enabled=_DEFAULT_ENABLED,
        flap_count=_DEFAULT_FLAP_COUNT,
        window_seconds=_DEFAULT_WINDOW_SECONDS,
        cooldown_seconds=_DEFAULT_COOLDOWN_SECONDS,
    )


def _load_config(shared_dir: Path) -> FlapperConfig:
    """Resolve operator-tunable knobs, falling back to compiled defaults.

    Uses the same defensive ``_config_lookup`` path as the rate breaker / grace
    knobs, so the values read identically whether or not the config file /
    schema entry exists.
    """
    def _int(path: str, default: int, *, floor: int = 1) -> int:
        try:
            return max(floor, int(_lookup(shared_dir, path, default)))
        except (TypeError, ValueError):
            return default

    return FlapperConfig(
        enabled=bool(_lookup(shared_dir, "alerts.flap_demote.enabled", _DEFAULT_ENABLED)),
        flap_count=_int("alerts.flap_demote.count", _DEFAULT_FLAP_COUNT, floor=2),
        window_seconds=_int("alerts.flap_demote.window_seconds", _DEFAULT_WINDOW_SECONDS),
        cooldown_seconds=_int(
            "alerts.flap_demote.cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS, floor=0,
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
    return Path(shared_dir) / "alerts" / "flap-demotion-state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "signatures": {}}
    if not isinstance(data, dict):
        return {"version": 1, "signatures": {}}
    data.setdefault("signatures", {})
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


def _entry(state: dict[str, Any], key: str) -> dict[str, Any]:
    sigs = state.setdefault("signatures", {})
    entry = sigs.get(key)
    if not isinstance(entry, dict):
        entry = {}
        sigs[key] = entry
    return entry


# The composite per-signature key. The unit-separator (U+001F) can't appear in
# a source id or a Signal signature, so split is unambiguous.
_KEY_SEP = "\x1f"


def _sig_key(source: str, coalesce_key: str) -> str:
    return f"{source}{_KEY_SEP}{coalesce_key}"


def _split_key(key: str) -> tuple[str, str]:
    source, _, coalesce_key = key.partition(_KEY_SEP)
    return source, coalesce_key


def _prune(timestamps: list[Any], window_start_epoch: float) -> list[str]:
    """Keep only ISO timestamps at-or-after ``window_start_epoch``.

    Malformed entries are dropped. Returns a fresh list of ISO strings.
    """
    out: list[str] = []
    for ts in timestamps:
        dt = _parse_iso(ts)
        if dt is not None and dt.timestamp() >= window_start_epoch:
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


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


__all__ = (
    "evaluate",
    "flap_health",
    "FlapperConfig",
    "FlapperDecision",
    "ACTION_PASS",
    "ACTION_DEMOTE_FIRE",
    "ACTION_SUPPRESS_CLEAR",
)
