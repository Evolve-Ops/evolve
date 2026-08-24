#!/usr/bin/env python3
"""
spend_alert.py — Real-time intraday spend alert + burst detector.

Runs every 5 minutes via launchd. For each bot in the pod, checks:

  1. Today's running spend (live, sourced from turn JSONL) against the
     daily threshold. Fires once per bot per day via flag-file dedup.
  2. The last N minutes of spend ("burst window") against a separate
     burst threshold. Severity ladder: warn @ threshold, alert @ 3×.
     Fires once per bot per hour-bucket via Signal-store dedup.
  3. The hard daily cap (separate from the alert threshold) when one is
     configured. Cap enforcement actions are owned by spend_caps.py.

Live data source is ``{shared_dir}/{bot}/turns/turns-{YYYY-MM-DD}.jsonl``
via ``usage_analytics.load_turns`` — the same JSONL the Usage page reads,
so the alerter and the UI always agree. The legacy
``{shared_dir}/metrics/{today}/{bot}.json`` path was end-of-day-only and
silently returned None during the day, which is how the 2026-05-20 spike
(security_bot $16.29 + team_bot_a $12.78) went unalerted across 24 hourly invocations.

The noon gate (``alerts.spendAlertHour``) is intentionally NOT applied to
the threshold check or the burst check — a $30 spike at 03:00 must alert
at 03:00, not noon. Hour gating is retained only for the weekly summary
which is naturally a daytime artifact.

Usage:
    python3 spend_alert.py --network /Users/Shared/evolve/network.json

Config (in network.json):
    thresholds.dailySpendAlertUsd     — daily threshold (default: 5.0)
    thresholds.weeklySpendAlertUsd    — weekly summary threshold (default: 20.0)
    thresholds.burstAlertUsd          — burst window threshold (default: dailySpendAlertUsd)
    thresholds.burstAlertWindowMin    — burst window in minutes (default: 60)
    thresholds.burstCriticalMultiplier — alert tier multiplier (default: 3.0)
    alerts.spendThresholdUSD          — legacy override for daily threshold
    alerts.enabled                    — disable all spend alerts (default: true)

Flag files (dedup):
    /Users/Shared/evolve/alerts/spend-{bot_id}-{date}.flag     — daily threshold
    /Users/Shared/evolve/alerts/weekly-summary-{YYYY-Www}.flag — weekly summary
    Bursts dedup via Signal-store signature, not flag files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from evolve_util import now_iso_micro as _now_iso

try:
    from evolve_config import (
        load_config, get_shared_dir, get_members, get_alerts,
        is_module_enabled, resolve_network_path,
    )
except ImportError:
    def load_config(n=None): return json.loads(Path("/Users/Shared/evolve/network.json").read_text()) if Path("/Users/Shared/evolve/network.json").exists() else {}  # type: ignore[misc]
    def get_shared_dir(c): return Path(c.get("sharedDir", "/Users/Shared/evolve"))  # type: ignore[misc]
    def get_members(c): return c.get("members", [])  # type: ignore[misc]
    def get_alerts(c): return c.get("alerts", {})  # type: ignore[misc]
    def is_module_enabled(c, m): return True  # type: ignore[misc]
    def resolve_network_path(): return Path("/Users/Shared/evolve/network.json")  # type: ignore[misc]


# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_FILE = Path("/Users/Shared/evolve/logs/spend_alert.log")


# ── Better Engine integration ─────────────────────────────────────────────────

def _format_expires_short(expires_at_iso: str) -> str:
    """Render an ISO-8601 expiry into the catalog's expected ``YYYY-MM-DD HH:MM``
    form. Returns ``"indefinite"`` on parse failure or empty input — same
    fallback shape as breakers/runner.py's helper, kept here as a sibling
    so spend_alert doesn't need to cross-import the runner.
    """
    if not expires_at_iso:
        return "indefinite"
    try:
        dt = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "indefinite"
    return dt.strftime("%Y-%m-%d %H:%M")


def _trip_duration_hours_from_iso(
    tripped_at_iso: str | None, expires_at_iso: str | None,
) -> int:
    """Hours between trip and expiry. Defaults to 24 on parse failure —
    the auto-trip flow's standard duration, matching what the operator
    expects to see.
    """
    if not expires_at_iso:
        return 24
    try:
        t0_iso = tripped_at_iso or datetime.now(timezone.utc).isoformat()
        t0 = datetime.fromisoformat(str(t0_iso).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(expires_at_iso).replace("Z", "+00:00"))
        delta = t1 - t0
        return max(1, round(delta.total_seconds() / 3600))
    except (ValueError, AttributeError, TypeError):
        return 24


def _flag_urgent_refresh(shared_dir: Path, source: str, reason: str, **kwargs) -> None:
    """Write the Better Engine urgent-refresh flag file (§2.2)."""
    try:
        flag = shared_dir / "better-engine" / ".refresh-urgent"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"source": source, "reason": reason,
                                    "ts": _now_iso(), **kwargs}))
    except Exception:
        pass  # never let this fail the main operation


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_LOG_FILE, "a") as f:
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


# ── Cost data ─────────────────────────────────────────────────────────────────
#
# Source of truth is the turn-level JSONL written by TurnObserver into
# ``{shared_dir}/{bot}/turns/turns-{YYYY-MM-DD}.jsonl``. We sum the
# recorded cost (or token-based estimate) the same way usage_analytics
# does, so this daemon and the Usage page agree to the penny.
#
# The pre-2026-05-20 implementation read end-of-day metric snapshots
# under ``metrics/{today}/{bot}.json`` — which only exist the morning
# after the day they cover, so every intraday tick returned None and
# every threshold check fell through to "no metrics — skipping".


def _network_path() -> str:
    """Best-effort path to network.json for usage_analytics's bot discovery."""
    try:
        return str(resolve_network_path())
    except Exception:
        return "/Users/Shared/evolve/network.json"


# Module-level sentinel — returned by _load_live_turns when discovery
# fails entirely (usage_analytics import error or load_turns raised).
# Distinguishes "this bot is genuinely idle" from "I have no way to tell"
# — the daemon needs the second to surface as a self-monitoring Signal,
# not as a silent OK.
_LIVE_LOAD_FAILED: object = object()


def _load_live_turns(
    bot_id: str, *, days: int = 1, end: datetime | None = None
):
    """Load live turn records via usage_analytics.

    ``days=1`` covers today; ``days=2`` covers today + yesterday (needed
    when a burst window crosses midnight UTC).

    Returns:
      * ``list[dict]`` — successfully read (may be empty if no rows yet)
      * ``_LIVE_LOAD_FAILED`` sentinel — discovery / read raised

    The sentinel exists because returning [] in both cases is the same
    bug the metrics-file path had: "no data" indistinguishable from "I
    can't see your data." The caller must propagate the sentinel so the
    daemon can surface a self-failure Signal.
    """
    try:
        from usage_analytics import load_turns  # type: ignore[import]
    except Exception as exc:
        _log(f"[spend_alert] usage_analytics import failed: {exc}")
        return _LIVE_LOAD_FAILED
    try:
        return load_turns(
            bot_id,
            days=days,
            end_date=end,
            network_path=_network_path(),
        )
    except Exception as exc:
        _log(f"[spend_alert] load_turns({bot_id}) raised: {exc}")
        return _LIVE_LOAD_FAILED


def _turn_cost(turn: dict) -> float:
    """Recorded cost when non-zero; otherwise token-based estimate.

    Mirrors usage_analytics.compute_summary's per-turn cost rule so the
    daemon's totals and the Usage page's totals never disagree.
    """
    try:
        from usage_analytics import _estimate_turn_cost  # type: ignore[import]
    except Exception:
        _estimate_turn_cost = lambda _t: 0.0  # type: ignore[assignment]
    recorded = float(turn.get("cost") or 0)
    return recorded if recorded else _estimate_turn_cost(turn)


def _parse_turn_ts(turn: dict) -> datetime | None:
    """Parse a turn's ts field into a tz-aware UTC datetime, or None."""
    ts = (turn.get("ts") or "").strip()
    if not ts:
        return None
    # Strip trailing Z; some writers emit ...Z, some emit ...+00:00.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_today_spend(
    shared_dir: Path,  # noqa: ARG001 — kept for callers that pass it
    bot_id: str,
    today: date,
    *,
    now: datetime | None = None,
    exempt_subkinds: set[str] | None = None,
) -> float | None:
    """Return today's running spend for bot_id from live turn JSONL.

    Returns:
      * ``float`` ≥ 0.0 — number is authoritative (live JSONL was read)
      * ``None`` — discovery failed; caller should NOT treat this as "OK"
        (this is what the 2026-05-20 incident silently did wrong — the
        legacy metrics-file path returned None and the daemon printed
        "skipping" but never asked why).

    ``exempt_subkinds`` skips turns whose ``forge_subkind`` is in the
    set. Used by the daily-cap check to honour
    ``forge_install_exempt_from_daily_cap``: operator-confirmed forge
    installs are tagged ``forge_subkind="operator_confirmed_install"``
    (via forge_sessions retag, see packages/analyzer/forge_sessions.py)
    and should not trip the cap, since the operator already saw and
    accepted the projected cost.
    """
    now = now or datetime.now(timezone.utc)
    turns = _load_live_turns(bot_id, days=1, end=now)
    if turns is _LIVE_LOAD_FAILED:
        return None
    today_iso = today.isoformat()
    total = 0.0
    for t in turns:
        ts = (t.get("ts") or "")[:10]
        if ts != today_iso:
            continue
        if exempt_subkinds and t.get("forge_subkind") in exempt_subkinds:
            continue
        total += _turn_cost(t)
    return round(total, 6)


# ── Burst window ──────────────────────────────────────────────────────────────


def burst_window_spend(
    bot_id: str,
    *,
    now: datetime | None = None,
    window_minutes: int = 60,
    exempt_subkinds: set[str] | None = None,
) -> tuple[float, list[dict]]:
    """Return (cost, turns) for the last ``window_minutes`` for ``bot_id``.

    Loads 2 days of turns when the window straddles UTC midnight so a
    burst that starts at 23:50 and continues past 00:00 isn't truncated.

    On discovery failure returns ``(-1.0, [])`` — the negative cost is
    the load-failure sentinel; callers MUST treat it as "did not run"
    rather than "no burst." The daemon's per-bot loop checks for it
    explicitly so a transient JSONL outage doesn't silently turn into
    "every bot is fine."

    ``exempt_subkinds`` filters out forge-tagged turns (currently only
    ``operator_confirmed_install``) so a confirmed install — which IS a
    legitimate burst — doesn't trip the burst alert path. Symmetric with
    ``load_today_spend``'s exemption.
    """
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    days = 1 if window_start.date() == now.date() else 2
    turns = _load_live_turns(bot_id, days=days, end=now)
    if turns is _LIVE_LOAD_FAILED:
        return -1.0, []
    selected: list[dict] = []
    total = 0.0
    for t in turns:
        dt = _parse_turn_ts(t)
        if dt is None:
            continue
        if window_start <= dt <= now:
            if exempt_subkinds and t.get("forge_subkind") in exempt_subkinds:
                continue
            cost = _turn_cost(t)
            if cost:
                total += cost
                selected.append({**t, "_cost": cost, "_ts_dt": dt})
    return round(total, 6), selected


def _top_sessions(turns: list[dict], n: int = 2) -> list[dict]:
    """Return the top ``n`` sessions by cost from a list of turn records.

    Each return record carries ``session_id`` (short prefix), ``channel``,
    ``cost``, ``turns_count`` for the burst-alert body. Sessions without
    a session_id collapse into a single "?" bucket so the loop doesn't
    crash on legacy records.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {"cost": 0.0, "turns_count": 0, "channel": "unknown"}
    )
    for t in turns:
        sid = (t.get("session_id") or "?") or "?"
        b = buckets[sid]
        b["cost"] += float(t.get("_cost") or 0)
        b["turns_count"] += 1
        if not b.get("channel") or b["channel"] == "unknown":
            ch = (t.get("channel") or "").strip()
            if ch:
                b["channel"] = ch
    out = [
        {
            "session_id": sid[:8] if sid != "?" else "?",
            "channel": v["channel"],
            "cost": round(float(v["cost"]), 4),
            "turns_count": int(v["turns_count"]),
        }
        for sid, v in buckets.items()
    ]
    out.sort(key=lambda s: -s["cost"])
    return out[:n]


def _hour_bucket(dt: datetime) -> str:
    """ISO hour bucket key for Signal dedup signature.

    A burst that straddles 19:55 → 20:05 will produce two Signals (one
    per hour bucket) rather than one — that's intentional: each bucket
    is a fresh "this is still bad" notification, and the Signal store's
    observe() updates observation_count + details on each tick within
    a bucket so the operator sees the rolling latest figure.

    The previous-hour Signal is auto-archived by ``main()``'s
    sweep_resolve call once a new bucket fires (or the burst stops
    altogether), so straddling no longer leaves duplicates stranded
    in firing/ — see ``_burst_signature`` + the sweep call in main().
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _burst_signature(bot_id: str, now: datetime) -> str | None:
    """Compute the ``cost_burst`` Signal signature for ``bot_id`` at ``now``.

    Single source of truth shared by ``emit_burst_signal`` (which writes
    the Signal) and ``main()`` (which sweeps stale firing Signals not in
    this tick's keep-set). Returns None if the schema import fails — the
    sweep treats that as "no kept signature for this bot" and the writer
    aborts entirely.
    """
    try:
        from schema.signal import make_signature  # type: ignore[import]
    except Exception:
        return None
    return make_signature(
        "spend_alert", "cost_burst", f"{bot_id}/{_hour_bucket(now)}"
    )


# ── Signal emission ───────────────────────────────────────────────────────────


def emit_burst_signal(
    *,
    shared_dir: Path,
    bot_id: str,
    cost_usd: float,
    threshold_usd: float,
    critical_multiplier: float,
    window_minutes: int,
    top_sessions: list[dict],
    now: datetime,
) -> str | None:
    """Write a ``cost_burst`` Signal via signals.store.observe.

    Severity ladder:
      cost ≥ critical_multiplier * threshold  → ``alert``  (Signal severity)
      cost ≥ threshold                        → ``warn``

    Returns the Signal id on success, None on import / write failure.
    Failures are logged but never crash the daemon — the dispatcher alert
    is the load-bearing notification path; the Signal is the observation
    log + Alerts-page citizen.
    """
    if cost_usd < threshold_usd:
        return None
    try:
        # The admin server has ACL read on {shared_dir} so the write
        # succeeds without sudo.
        from signals import store as signals_store  # type: ignore[import]
    except Exception as exc:
        _log(f"[spend_alert] signals import failed; burst Signal skipped: {exc}")
        return None

    hour_key = _hour_bucket(now)
    signature = _burst_signature(bot_id, now)
    if signature is None:
        _log(f"[spend_alert] make_signature unavailable; burst Signal skipped")
        return None
    severity = "alert" if cost_usd >= critical_multiplier * threshold_usd else "warn"

    # Window range label — disambiguates the rare legitimate case where
    # two cost_burst Signals coexist (mid-straddle of a UTC hour
    # boundary; see _hour_bucket). Each Signal's last-observed body
    # carries its own window, so the operator can tell them apart at a
    # glance without expanding raw details. The post-loop sweep in
    # main() archives stranded straddles once the burst stops; this
    # label only matters while both are firing.
    window_start = now - timedelta(minutes=window_minutes)
    window_label = (
        f"{window_start.strftime('%Y-%m-%d %H:%M')}–"
        f"{now.strftime('%H:%M')} UTC"
    )

    top_line = ""
    if top_sessions:
        s = top_sessions[0]
        top_line = (
            f" biggest session id={s['session_id']} "
            f"channel={s['channel']} cost=${s['cost']:.2f}"
        )
    body = (
        f"Burst window: {window_label}\n"
        f"{bot_id} spent ${cost_usd:.2f} in last {window_minutes}m "
        f"(threshold ${threshold_usd:.2f}, "
        f"critical ${critical_multiplier * threshold_usd:.2f})."
        f"{top_line}"
    )

    try:
        sig = signals_store.observe(
            shared_dir,
            signature=signature,
            producer="spend_alert",
            type="cost_burst",
            flavor="activity",
            severity=severity,
            scope="bot",
            bot_id=bot_id,
            category="cost",
            title=f"cost burst: {bot_id} ${cost_usd:.2f} / {window_minutes}m",
            body=body,
            details={
                "bot_id": bot_id,
                "cost_usd": round(cost_usd, 4),
                "threshold_usd": float(threshold_usd),
                "critical_multiplier": float(critical_multiplier),
                "window_minutes": int(window_minutes),
                "window_start_iso": window_start.isoformat(timespec="seconds"),
                "window_end_iso": now.isoformat(timespec="seconds"),
                "window_label": window_label,
                "top_sessions": top_sessions,
                "hour_bucket": hour_key,
                "what_it_means": (
                    f"{bot_id} spent ${cost_usd:.2f} in the last "
                    f"{window_minutes} minutes — above the burst "
                    f"threshold of ${threshold_usd:.2f}"
                    + (
                        f" and "
                        f"{cost_usd / threshold_usd:.1f}× the "
                        f"critical multiplier "
                        f"(${critical_multiplier * threshold_usd:.2f})"
                        if cost_usd >= critical_multiplier * threshold_usd
                        else ""
                    )
                    + ". Distinct from the daily threshold check: "
                    "this fires on a short rolling window so a "
                    "runaway session is caught within minutes, not "
                    "at end-of-day. The 2026-05-20 cost-alerting "
                    "blackout was the smoking-gun case: a stuck "
                    "heartbeat-retry storm that would have been "
                    "caught at ~19:10 UTC if this detector existed."
                ),
                "fix_steps": (
                    f"1. Open the top session in Usage → Bot detail "
                    f"for `{bot_id}`"
                    + (
                        f" (session_id `{top_sessions[0]['session_id']}`"
                        + (
                            f", channel "
                            f"`{top_sessions[0]['channel']}`"
                            if top_sessions[0].get("channel")
                            else ""
                        )
                        + ")"
                        if top_sessions
                        else ""
                    )
                    + "\n"
                    "2. Scan the turn timeline for the repeat "
                    "pattern (retry storm, fallback walk, runaway "
                    "subagent)\n"
                    "3. If the bleed is ongoing, halt with the "
                    "cost breaker:\n"
                    f"   ssh pod_admin_user@mini sudo evolve-admin "
                    f"breaker trip {bot_id} cost --duration 24h\n"
                    "4. Then root-cause the underlying loop before "
                    "untripping\n"
                    "5. Long-term: set a per-bot cap so this "
                    "self-trips next time — "
                    f"`bots.{bot_id}.daily_cap_usd` in network.json"
                ),
            },
        )
        return sig.id
    except Exception as exc:
        _log(f"[spend_alert] signals.observe({bot_id}) raised: {exc}")
        return None


def _projected_full_day_spend(spend_usd: float, now: datetime) -> tuple[float, float]:
    """Return (projected_full_day, hours_elapsed_today_used_for_projection).

    Linear projection: ``spend * (24 / elapsed)``. Hours elapsed is floored at
    1.0 so a spike in the first hour doesn't multiply 24× off a small actual.
    The cap of 24.0 means projection ≥ now's elapsed → returns at least the
    current spend (the day can't go backwards).
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_elapsed = max(1.0, (now - midnight).total_seconds() / 3600.0)
    projected = spend_usd * (24.0 / hours_elapsed)
    return projected, hours_elapsed


def emit_velocity_forecast_signal(
    *,
    shared_dir: Path,
    bot_id: str,
    spend_usd: float,
    cap_usd: float,
    warn_fraction: float,
    today: date,
    now: datetime,
) -> str | None:
    """Fire ``cost_velocity_forecast`` Signal when projected daily spend
    crosses ``warn_fraction`` of the bot's cap.

    Distinct from the burst window (last N min spike) and from
    ``daily_spend_high`` (actuals crossed a threshold): this one is the
    early-warning *projection* — at this rate, today will hit the cap by
    midnight. Gives the operator a same-day knob to turn before the L1
    breaker trip.

    Per-(bot, day) signature so the Signal stays open across ticks; severity
    upgrades from warn → alert as projection approaches 100% of cap.
    """
    if cap_usd <= 0:
        return None
    projected, hours_elapsed = _projected_full_day_spend(spend_usd, now)
    fraction = projected / cap_usd
    if fraction < warn_fraction:
        return None
    # If actuals already crossed the cap, the breaker path handles it; no
    # need to fire the projection signal redundantly.
    if spend_usd >= cap_usd:
        return None
    try:
        from signals import store as signals_store  # type: ignore[import]
        from schema.signal import make_signature  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] signals import failed; velocity Signal skipped: {exc}"
        )
        return None
    signature = make_signature(
        "spend_alert", "cost_velocity_forecast", f"{bot_id}/{today.isoformat()}"
    )
    severity = "alert" if fraction >= 1.00 else "warn"
    body = (
        f"{bot_id} has spent ${spend_usd:.2f} in {hours_elapsed:.1f}h of "
        f"today. At the current rate, projected end-of-day spend is "
        f"${projected:.2f} — {fraction:.0%} of its ${cap_usd:.2f} cap. "
        "The L1 cost breaker trips at the cap; this signal fires before "
        "the trip so the operator can intervene (raise cadence, trim "
        "workload, or knowingly let the cap trip). Distinct from the "
        "burst-window signal (which catches short-rolling spikes) — this "
        "one catches the slow-bleed shape that compounds across the day."
    )
    try:
        sig = signals_store.observe(
            shared_dir,
            signature=signature,
            producer="spend_alert",
            type="cost_velocity_forecast",
            flavor="activity",
            severity=severity,
            scope="bot",
            bot_id=bot_id,
            title=(
                f"{bot_id} projecting ${projected:.2f}/day "
                f"({fraction:.0%} of ${cap_usd:.2f} cap)"
            ),
            body=body,
            details={
                "bot_id": bot_id,
                "spend_so_far_usd": round(spend_usd, 4),
                "projected_daily_usd": round(projected, 2),
                "cap_usd": float(cap_usd),
                "cap_fraction": round(fraction, 3),
                "hours_elapsed": round(hours_elapsed, 2),
                "today_iso": today.isoformat(),
                "vector": "cost",
                "magnitude": 3 if severity == "alert" else 2,
                "what_it_means": (
                    f"At current rate, {bot_id} will hit "
                    f"${projected:.2f} today — "
                    f"{fraction:.0%} of its ${cap_usd:.2f} cap. "
                    "Acting now keeps the breaker from tripping."
                ),
                "fix_steps": (
                    "1. Open Usage → Bot detail to see which sessions "
                    "are driving the rate.\n"
                    "2. If a runaway session is responsible, halt "
                    "manually:\n"
                    f"   ssh pod-admin-user@mini sudo evolve-admin breaker "
                    f"trip {bot_id} cost --duration 24h\n"
                    "3. If the cost is by-design (HEARTBEAT.md workload, "
                    "high-cadence cron), consider raising "
                    f"`bots.{bot_id}.daily_cap_usd` or lowering cadence.\n"
                    "4. If a workload was just introduced today, that's "
                    "the most likely culprit — revert it and re-deploy."
                ),
            },
        )
        return sig.id
    except Exception as exc:
        _log(
            f"[spend_alert] signals.observe({bot_id} velocity_forecast) "
            f"raised: {exc}"
        )
        return None


def send_burst_alert(
    bot_id: str,
    cost_usd: float,
    threshold_usd: float,
    *,
    critical_multiplier: float,
    window_minutes: int,
    top_sessions: list[dict],
    shared_dir: Path,
    network: dict,
    hour_bucket: str,
) -> bool:
    """Dispatch the operator-facing burst alert through the catalog.

    Dedup is per (bot, hour-bucket) — the cost_watchdog hour bucket aligns
    with the Signal-store signature so the chat alert and the Alerts-page
    Signal trace each other. Cooldown_seconds=0 because the hour-bucket
    dedup_key already gives the dispatcher a meaningful key without the
    24h source-level wait that gates ``cost.daily_threshold``.
    """
    is_critical = cost_usd >= critical_multiplier * threshold_usd
    severity_name = "critical" if is_critical else "warning"
    top1 = top_sessions[0] if top_sessions else {
        "session_id": "?", "channel": "unknown", "cost": 0.0
    }
    top2_line = ""
    if len(top_sessions) > 1:
        s2 = top_sessions[1]
        top2_line = (
            f"\nNext: session {s2['session_id']} "
            f"channel={s2['channel']} ${s2['cost']:.2f}."
        )

    # warn vs. critical share a single catalog entry (cost.burst_detected)
    # since the 2026-05-29 collapse; producer pre-renders the per-tier
    # title pieces so one operator subscription covers both tiers.
    if is_critical:
        level_emoji = "🔴"
        event_phrase = "cost runaway"
        threshold_clause = f"(≥ {critical_multiplier:g}× ${threshold_usd:.2f} threshold)"
    else:
        level_emoji = "💰"
        event_phrase = "burst"
        threshold_clause = f"(threshold ${threshold_usd:.2f})"

    sent = _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/burst/{bot_id}/{hour_bucket}",
        severity_name=severity_name,
        catalog_event="cost.burst_detected",
        payload={
            "bot_id": bot_id,
            "cost": float(cost_usd),
            "threshold": float(threshold_usd),
            "window_minutes": int(window_minutes),
            "level_emoji": level_emoji,
            "event_phrase": event_phrase,
            "threshold_clause": threshold_clause,
            "top_session_id": top1["session_id"],
            "top_session_channel": top1["channel"],
            "top_session_cost": float(top1.get("cost") or 0.0),
            "top2_line": top2_line,
        },
    )
    if sent:
        _log(
            f"[spend_alert] {bot_id}: BURST alert sent "
            f"(${cost_usd:.2f} in {window_minutes}m, "
            f"{'critical' if is_critical else 'warning'})"
        )
    return sent


# ── Flag file dedup ───────────────────────────────────────────────────────────

def _flag_path(shared_dir: Path, bot_id: str, today: date) -> Path:
    return shared_dir / "alerts" / f"spend-{bot_id}-{today}.flag"


def alert_already_sent(shared_dir: Path, bot_id: str, today: date) -> bool:
    return _flag_path(shared_dir, bot_id, today).exists()


def write_flag(shared_dir: Path, bot_id: str, today: date, amount: float, threshold: float) -> None:
    """Atomically write a dedup flag file (644)."""
    alerts_dir = shared_dir / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(alerts_dir, 0o755)
    except OSError:
        pass

    fp = _flag_path(shared_dir, bot_id, today)
    tmp = fp.with_suffix(".flag.tmp")
    payload = json.dumps({
        "bot_id": bot_id,
        "date": str(today),
        "amount_usd": round(amount, 4),
        "threshold_usd": threshold,
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2)
    tmp.write_text(payload)
    os.replace(tmp, fp)
    try:
        os.chmod(fp, 0o644)
    except OSError:
        pass


# ── Alert delivery ────────────────────────────────────────────────────────────

def _dispatch(
    *,
    shared_dir: Path,
    network: dict,
    dedup_key: str,
    catalog_event: str,
    payload: dict,
    severity_name: str = "warning",
) -> bool:
    """Route a spend-alert event through the alerts dispatcher.

    Phase F3: passes structured payload; catalog body_template renders
    the final message. Returns True iff the dispatcher reported SENT
    (caller writes the daily flag-file only on True, so suppressed /
    failed sends leave the flag absent and the next eligible tick can
    retry once the operator re-enables).

    Operator-tunable enable/cooldown via
    ``alerts.spend_alert.{enabled, cooldown_seconds}``; per-event
    subscriptions via ``cost.daily_threshold`` / ``cost.hard_cap_hit``
    / ``cost.weekly_summary``.
    """
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception as exc:
        _log(f"[spend_alert] dispatcher import failed; alert dropped: {exc}")
        return False

    severity_map = {
        "critical": Severity.CRITICAL,
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
    }
    try:
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="spend_alert",
            severity=severity_map.get(severity_name, Severity.WARNING),
            dedup_key=dedup_key,
            catalog_event=catalog_event,
            payload=payload,
        )
    except Exception as exc:
        _log(f"[spend_alert] dispatcher.send raised; alert dropped: {exc}")
        return False
    return outcome.result == DispatchResult.SENT


def send_alert(
    bot_id: str,
    amount: float,
    threshold: float,
    *,
    shared_dir: Path,
    network: dict,
    today_iso: str | None = None,
) -> bool:
    """Daily intraday spend-threshold alert. Returns True on SENT."""
    if today_iso is None:
        from pod_time import pod_today_str
        today_iso = pod_today_str()
    sent = _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/threshold/{bot_id}/{today_iso}",
        severity_name="warning",
        catalog_event="cost.daily_threshold",
        payload={"bot_id": bot_id, "amount": amount, "threshold": threshold},
    )
    if sent:
        _log(f"[spend_alert] {bot_id}: alert sent (${amount:.2f} > ${threshold:.2f})")
    return sent


# ── Per-bot cap (breaker-based) ──────────────────────────────────────────────

# Rung names of the remediation ladder, in escalation order. Mirrors
# ``better_engine_config.BUDGET_LADDER_FIELDS`` — duplicated here only so the
# all-None fallback below doesn't need BE config importable; a test pins the
# two lists equal.
_LADDER_RUNGS = (
    "daily_warn",
    "weekly_warn",
    "tier_downgrade",
    "l1_breaker",
    "l2_breaker",
    "per_session",
)


def _resolve_per_bot_cap(
    bot_id: str, bots_cfg: dict, shared_dir: Path | None = None,
    *, now: datetime | None = None,
) -> float | None:
    """Per-bot daily hard cap (the L1 rung) from ``better-engine-config.json``.

    Returns the cap as a float, or None when no rung applies. Resolution —
    monthly derivation, then ``bots.<bot>.budget.per_bot_daily_hard_usd``, then
    the graduated new-bot default, then ``pod_defaults.budget`` — lives in
    ``BetterEngineConfig.resolve_budget_ladder``; see ``_resolve_per_bot_caps``.
    Zero / negative / unparseable at any layer means "no cap" — the same
    convention as the legacy global ``dailySpendCapUsd``.

    ``shared_dir`` is required; when omitted (or BE config can't be loaded),
    returns None. ``bots_cfg`` is retained in the signature for backward
    compatibility with callers; the legacy ``network.json::daily_cap_usd``
    path was removed in Phase 4 of the cost-cap normalization (2026-06).

    Equivalent to ``_resolve_per_bot_caps(...)["l1_breaker"]``; callers
    that need the full ladder should use the plural form added in Phase 6.
    """
    _ = bots_cfg  # legacy param; retained for caller signature compat
    caps = _resolve_per_bot_caps(bot_id, shared_dir, now=now)
    return caps.get("l1_breaker")


def _resolve_per_bot_caps(
    bot_id: str, shared_dir: Path | None, *, now: datetime | None = None,
) -> dict[str, float | None]:
    """Resolve every threshold in the per-bot remediation ladder.

    Returns a dict with keys:
        - daily_warn      (alert tier; per_bot_daily_warn_usd)
        - weekly_warn     (alert tier; weekly_warn_usd; Phase 5 NEW)
        - tier_downgrade  (remediation tier; Phase 5 NEW)
        - l1_breaker      (remediation tier; per_bot_daily_hard_usd)
        - l2_breaker      (remediation tier; Phase 5 NEW)
        - per_session     (sentinel; per_bot_session_cost_cap_usd)

    Each value is the positive float threshold or ``None`` (no enforcement
    at that tier). Reads from better-engine-config; when shared_dir is
    omitted or BE config can't be loaded, every value is None.

    Resolution is delegated wholesale to
    ``BetterEngineConfig.resolve_budget_ladder`` — the single reader shared
    with the UI (``GET /api/spend-caps``), ``cost_watchdog``, and the evo
    cost-cap tools. That includes the ``pod_defaults.budget`` fallback: a bot
    with no explicit override inherits the pod-wide rung rather than resolving
    to ``None``. Before the fallback existed this function returned an all-None
    ladder for every un-overridden bot, so ``if cap is not None`` in the caller
    below silently skipped ALL enforcement while the UI displayed the pod cap.

    Phase 6 spec: internal/spec-cost-caps-2026-06-05.md §"Enforcement
    implementation".
    """
    empty: dict[str, float | None] = {rung: None for rung in _LADDER_RUNGS}
    if shared_dir is None:
        return empty
    try:
        from better_engine_config import load as _load_be  # type: ignore
        be = _load_be(shared_dir)
    except Exception:
        return empty
    return be.resolve_budget_ladder(bot_id, now=now)


def _breaker_already_tripped(
    shared_dir: Path, bot_id: str,
) -> bool:
    """True iff the bot already has a live L1 cost breaker tripped.

    Used to dedup the per-bot cap auto-trip across ticks — once we've
    tripped the breaker, the next ticks should see "already in effect"
    and skip rather than retrip + re-enforce + re-alert. The 24h TTL
    or an explicit ``evolve-admin breaker reset`` clears the file.

    Failures (import error, corrupt breaker JSON) return False — caller
    proceeds as if no trip is active and writes a fresh one. That's the
    fail-open contract: better to over-trip than to silently sit on a
    bleed because the dedup file got mangled. The failures are LOGGED
    so a recurring problem (a daily corrupt-file event would mean
    spend_alert re-trips every tick) surfaces in the daemon log rather
    than staying silent forever — the exact failure pattern that caused
    the 2026-05-20 incident.
    """
    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] _breaker_already_tripped({bot_id}): "
            f"breakers.store import failed ({exc}); "
            f"proceeding as not-tripped (fail-open)"
        )
        return False
    try:
        rec = _bstore.read_trip(shared_dir, bot_id, "cost")
    except Exception as exc:
        _log(
            f"[spend_alert] _breaker_already_tripped({bot_id}): "
            f"breakers.store.read_trip raised ({exc}); "
            f"proceeding as not-tripped (fail-open). "
            f"If this repeats, inspect "
            f"{shared_dir}/breakers/{bot_id}/cost.json for corruption."
        )
        return False
    if rec is None:
        return False
    return not _bstore.is_expired(rec)


def _write_spend_cap_flag(
    shared_dir: Path, bot_id: str, *, spend: float, cap: float, today_iso: str,
) -> None:
    """Write today's spend-cap enforcement flag for ``bot_id``.

    The flag at ``{shared_dir}/spend-caps/<bot>-<YYYY-MM-DD>.json`` is read by
    two consumers the breaker record alone doesn't reach:

      * the OpenClaw plugin's ``ModelRouter.isSpendCapActive`` — an in-band
        per-turn downgrade to the ``fast`` role. The L1 breaker pauses
        heartbeat + background sessions but leaves user chat on the bot's
        normal (expensive) model; this is what caps the cost of the turns
        that keep running.
      * ``GET /api/spend-caps`` — the ``active_enforcement`` and
        ``recent_enforcement_history`` panels on the Cost Measures page.

    It used to be written by the pod-wide ``thresholds.dailySpendCapUsd``
    branch, which the Phase-8 migration left unreachable — so on every
    migrated pod both consumers went dark. Writing it from the L1 trip puts it
    behind the same single resolved cap as every other rung.

    ``action`` is pinned to ``"downgrade-tier"``: that is the only value
    ModelRouter acts on, and it matches what the legacy branch wrote under its
    default ``spendCapAction``. The heavier legacy actions (``pause-crons``,
    ``suspend-bot``) are owned by the L2 rung now, not by this flag.

    ``today_iso`` is the caller's pod-local day, not UTC — the flag filename
    must roll at the same midnight the caps do (and the same one
    ``ModelRouter.localDateYMD`` uses to find it).

    Failures are logged, never raised — a missing flag costs the ModelRouter
    downgrade, not the breaker trip that has already been written.
    """
    try:
        import spend_caps as _spend_caps  # type: ignore[import]
    except ImportError:
        _log(
            f"[spend_alert] {bot_id}: spend_caps module not importable — "
            f"enforcement flag not written; ModelRouter will not downgrade "
            f"this bot's turns (L1 breaker is still tripped)"
        )
        return
    try:
        _spend_caps.write_enforcement_flag(
            shared_dir, bot_id, "downgrade-tier", spend, cap,
            date.fromisoformat(today_iso),
        )
    except Exception as exc:  # noqa: BLE001
        _log(
            f"[spend_alert] {bot_id}: enforcement flag write failed: {exc}. "
            f"L1 breaker is tripped but ModelRouter will not downgrade "
            f"this bot's turns."
        )


def _trip_breaker_for_cost_cap(
    *,
    bot_id: str,
    spend: float,
    cap: float,
    shared_dir: Path,
    network: dict,
    today_iso: str,
) -> None:
    """Auto-trip the L1 cost breaker for ``bot_id`` and run enforcement.

    Dedup: skips when an L1 cost breaker is already tripped for this
    bot. The breaker file itself is the dedup primitive — no
    additional ``alerts/`` flag-file pair. This matches the breaker
    spec's "file existence = trip is in effect" semantics.

    Sequence:
      1. Write the breaker record (24h TTL, initiated_by="auto:spend_alert").
      2. Write the spend-cap enforcement flag (see ``_write_spend_cap_flag``).
      3. Run synchronous enforcement (heartbeat-disable + gateway kickstart).
      4. Send the Telegram alert via the ``cost.breaker_tripped`` catalog
         event — the breaker trip is the news, the cap only the cause (Fix B,
         2026-05-28; see the dispatch call below).

    Failures at any step are logged but never raised — the daemon must
    continue checking other bots even if one step fails.
    """
    if _breaker_already_tripped(shared_dir, bot_id):
        _log(
            f"[spend_alert] {bot_id}: per-bot cap ${cap:.2f} crossed "
            f"(${spend:.2f}) but L1 cost breaker already tripped — "
            f"skipping retrip"
        )
        return

    from datetime import timedelta as _td

    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] breakers.store import failed; "
            f"{bot_id} cap auto-trip dropped: {exc}"
        )
        return

    reason = (
        f"per-bot daily cap exceeded: ${spend:.2f} ≥ ${cap:.2f}"
    )
    try:
        rec = _bstore.trip(
            shared_dir=shared_dir,
            scope=bot_id,
            breaker_type="cost",
            duration=_td(hours=24),
            initiated_by="auto:spend_alert",
            reason=reason,
        )
    except Exception as exc:
        _log(
            f"[spend_alert] {bot_id}: breaker trip raised; cap auto-trip "
            f"dropped: {exc}"
        )
        return

    _write_spend_cap_flag(
        shared_dir, bot_id, spend=spend, cap=cap, today_iso=today_iso,
    )

    # Synchronous enforcement: heartbeat-disable + gateway kickstart.
    # admin path import is best-effort; the breaker file is already
    # written so an import failure leaves a tripped-but-not-enforced
    # breaker that the operator can inspect via the CLI.
    enforce_result = None
    enforce_raised = False
    try:
        from evolve_admin import breakers_enforce as _be  # type: ignore[import]
        enforce_result = _be.enforce_trip(
            scope=bot_id, breaker_type="cost", network=network,
            shared_dir=shared_dir,
            reason=reason, expires_at_iso=rec.expires_at,
        )
    except Exception as exc:
        enforce_raised = True
        _log(
            f"[spend_alert] {bot_id}: cost-breaker enforce raised: {exc}. "
            f"Breaker file written; operator can run "
            f"'evolve-admin breaker status' to inspect."
        )

    # Reason MUST reflect reality — we are dispatching to Telegram
    # below and must not tell the operator "heartbeat disabled" if it
    # wasn't. The 2026-05-20 incident's defining failure mode was
    # "daemon says everything is fine, system is silently broken"; we
    # do not get to perpetuate that here. Each branch builds the
    # `reason` field that cost.breaker_tripped's body template echoes.
    base_reason = f"Spent ${spend:.2f} of ${cap:.2f} daily cap"
    if enforce_raised:
        reason_text = (
            f"{base_reason}. Enforcement FAILED — "
            f"run 'evolve-admin breaker status' and check daemon logs."
        )
    elif enforce_result is not None and not enforce_result.ok:
        reason_text = (
            f"{base_reason}. Enforcement partially failed — "
            f"run 'evolve-admin breaker status'."
        )
    elif enforce_result is not None and enforce_result.no_op:
        reason_text = (
            f"{base_reason}. (No heartbeat scheduled — nothing to disable.)"
        )
    else:
        reason_text = base_reason

    _log(
        f"[spend_alert] {bot_id}: HARD CAP (per-bot) ${spend:.2f} ≥ ${cap:.2f} — "
        f"L1 cost breaker tripped (trip_id={rec.trip_id[:8]}, expires={rec.expires_at})"
    )

    # Fix B (2026-05-28): dispatch the dedicated cost.breaker_tripped
    # catalog event instead of cost.hard_cap_hit. The cap is the
    # cause; the breaker trip is the news — operator sees the ⚡ +
    # bold "BREAKER TRIPPED" lead instead of "hit daily spending cap"
    # with the breaker mention buried as a sub-line. Verified live on
    # the 2026-05-28 Security_bot incident where the buried framing made
    # the trip easy to miss.
    expires_iso = rec.expires_at or ""
    expires_short = _format_expires_short(expires_iso)
    duration_hours = _trip_duration_hours_from_iso(
        getattr(rec, "tripped_at", None), expires_iso,
    )
    _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/percap/{bot_id}/{today_iso}",
        severity_name="critical",
        catalog_event="cost.breaker_tripped",
        payload={
            "bot_id": bot_id,
            "breaker_type": "cost",
            "reason": reason_text,
            "expires_at_short": expires_short,
            "duration_hours": duration_hours,
            "trip_id_short": rec.trip_id[:8],
        },
    )
    _flag_urgent_refresh(
        shared_dir, source="spend_alert",
        reason="per_bot_hard_cap_hit", bot_id=bot_id,
    )


# ── Phase 6: graduated remediation tiers (tier_downgrade, L2) ────────────────
# spec: internal/spec-cost-caps-2026-06-05.md §"Enforcement implementation".


# Module-level handles for the helpers used by Phase 6 tier_downgrade +
# L2 enforcement (cost_profiles is a sibling analyzer module;
# breakers_enforce is admin-side). Lazily resolved at first call so
# tests can override without needing the modules importable in the
# test env.
_TIER_DOWNGRADE_WRITER = None
_TIER_DOWNGRADE_READER = None
_L2_ENFORCE_TRIP = None


def _get_tier_downgrade_writer():
    """Return the openclaw.json writer callable, or None if unavailable."""
    global _TIER_DOWNGRADE_WRITER
    if _TIER_DOWNGRADE_WRITER is not None:
        return _TIER_DOWNGRADE_WRITER
    try:
        from cost_profiles import write_openclaw_cost_settings  # type: ignore
        _TIER_DOWNGRADE_WRITER = write_openclaw_cost_settings
        return _TIER_DOWNGRADE_WRITER
    except Exception:
        return None


def _get_tier_downgrade_reader():
    """Return the openclaw.json primary-model reader, or None if unavailable."""
    global _TIER_DOWNGRADE_READER
    if _TIER_DOWNGRADE_READER is not None:
        return _TIER_DOWNGRADE_READER
    try:
        from cost_profiles import read_openclaw_primary_model  # type: ignore
        _TIER_DOWNGRADE_READER = read_openclaw_primary_model
        return _TIER_DOWNGRADE_READER
    except Exception:
        return None


def _get_l2_enforce_trip():
    """Return the breakers_enforce.enforce_trip callable, or None."""
    global _L2_ENFORCE_TRIP
    if _L2_ENFORCE_TRIP is not None:
        return _L2_ENFORCE_TRIP
    try:
        from evolve_admin.breakers_enforce import enforce_trip  # type: ignore
        _L2_ENFORCE_TRIP = enforce_trip
        return _L2_ENFORCE_TRIP
    except Exception:
        return None


def _tier_downgrade_already_applied(
    shared_dir: Path, bot_id: str, today_iso: str | None = None
) -> bool:
    """True iff a tier_downgrade dedup marker is in effect for ``bot_id``.

    The marker is a plain flag file at
    ``{shared_dir}/cost_remediations/<bot>/tier_downgrade.flag`` with
    a ``YYYY-MM-DD`` body identifying the day the downgrade was applied.
    Removed at midnight by ``apply_tier_downgrade`` callers when the date
    rolls over (or via ``evolve-admin breaker reset``).

    ``today_iso`` is injectable for tests. In production it defaults to
    the pod-local date — the marker rolls at pod-local midnight, NOT
    UTC. (UTC midnight would roll caps a day early for a US-Pacific
    operator.)
    """
    flag = shared_dir / "cost_remediations" / bot_id / "tier_downgrade.flag"
    if not flag.exists():
        return False
    try:
        recorded = flag.read_text().strip()
    except OSError:
        return False
    if today_iso is None:
        from pod_time import pod_today_str
        today_iso = pod_today_str()
    if recorded == today_iso:
        return True
    # Stale (yesterday's downgrade) — auto-revert by removing the marker.
    try:
        flag.unlink()
    except OSError:
        pass
    return False


def _apply_tier_downgrade(
    *, bot_id: str, shared_dir: Path, network: dict, spend: float,
    cap: float, today_iso: str,
) -> None:
    """Switch ``bot_id`` to a tier-3 model for the rest of the day.

    Writes the tier-3 model id into the bot's
    ``openclaw.json::agents.defaults.model.primary`` slot via the admin
    helper, then alerts the operator. Dedupes once per day so a steady
    over-spend doesn't re-write the same value every tick.

    Phase 6 keeps the implementation minimal: the dedup marker is a
    flag file under ``{shared_dir}/cost_remediations/<bot>/``. Reset is
    automatic at midnight (date roll) and the operator can also clear
    it explicitly. Spec: internal/spec-cost-caps-2026-06-05.md.
    """
    if _tier_downgrade_already_applied(shared_dir, bot_id, today_iso):
        _log(
            f"[spend_alert] {bot_id}: tier_downgrade cap ${cap:.2f} crossed "
            f"(${spend:.2f}) but tier_downgrade already applied today — "
            f"skipping re-apply"
        )
        return

    # Resolve a tier-3 model id. Reads pod-wide model catalog when
    # available; falls back to a hardcoded conservative default.
    tier3_model = _resolve_tier3_model(network)

    # Capture the pre-downgrade primary BEFORE the write so the midnight
    # revert sweep knows what to restore. Best-effort: None when the
    # reader is unavailable or the file has no explicit primary.
    prev_primary = None
    try:
        reader = _get_tier_downgrade_reader()
        if reader is not None:
            prev_primary = reader(bot_id)
    except Exception:  # noqa: BLE001
        prev_primary = None

    # Best-effort openclaw.json write. The cost_profiles helper handles
    # the /tmp + sudo /bin/cp path. Failures land in the alert reason so
    # the operator knows what to fix. The writer is resolved through
    # ``_get_tier_downgrade_writer()`` so tests can monkeypatch the
    # module-level handle without needing cost_profiles importable.
    write_ok = False
    write_detail = ""
    try:
        writer = _get_tier_downgrade_writer()
        if writer is None:
            write_detail = "writer unavailable (cost_profiles not importable)"
        else:
            write_ok, write_detail = writer(
                bot_id,
                {"agents": {"defaults": {"model": {"primary": tier3_model}}}},
            )
    except Exception as exc:  # noqa: BLE001
        write_detail = f"writer raised: {exc}"

    # Record what to restore at midnight — only when the downgrade
    # actually landed (a failed write changed nothing worth reverting).
    if write_ok:
        _record_tier_downgrade_restore(
            shared_dir, bot_id, today_iso=today_iso,
            original_model=prev_primary, applied_model=tier3_model,
        )

    # Mark the day's downgrade applied even if the write partially
    # failed — re-running would be a no-op for the same target. Operator
    # can reset via the breaker CLI.
    flag_dir = shared_dir / "cost_remediations" / bot_id
    try:
        flag_dir.mkdir(parents=True, exist_ok=True)
        (flag_dir / "tier_downgrade.flag").write_text(today_iso)
    except OSError as exc:
        _log(f"[spend_alert] {bot_id}: tier_downgrade flag write failed: {exc}")

    base_reason = (
        f"Spent ${spend:.2f} of ${cap:.2f} tier_downgrade cap; "
        f"switched primary model to {tier3_model}"
    )
    if not write_ok:
        reason_text = f"{base_reason}. WRITE FAILED: {write_detail or 'unknown'}"
    else:
        reason_text = f"{base_reason}. Auto-reverts at midnight."

    _log(
        f"[spend_alert] {bot_id}: TIER DOWNGRADE ${spend:.2f} ≥ ${cap:.2f} — "
        f"new primary={tier3_model} (ok={write_ok})"
    )

    _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/tier_downgrade/{bot_id}/{today_iso}",
        severity_name="warning",
        catalog_event="cost.tier_downgrade_active",
        payload={
            "bot_id": bot_id,
            "tier3_model": tier3_model,
            "reason": reason_text,
        },
    )
    _flag_urgent_refresh(
        shared_dir, source="spend_alert",
        reason="tier_downgrade_active", bot_id=bot_id,
    )


def _tier_downgrade_restore_path(shared_dir: Path, bot_id: str) -> Path:
    return (
        shared_dir / "cost_remediations" / bot_id
        / "tier_downgrade_restore.json"
    )


def _record_tier_downgrade_restore(
    shared_dir: Path, bot_id: str, *, today_iso: str,
    original_model: str | None, applied_model: str,
) -> None:
    """Persist what the midnight revert should write back.

    Clobber guard: if a record already exists (yesterday's revert never
    ran because the writer was unavailable, or a mid-day reset re-armed
    the downgrade), its ``original_model`` is the true pre-downgrade
    model — the fresh read may have captured the already-downgraded
    model instead. Keep the recorded original in that case.
    """
    path = _tier_downgrade_restore_path(shared_dir, bot_id)
    if path.exists():
        try:
            prev = json.loads(path.read_text())
            recorded = prev.get("original_model")
            if isinstance(recorded, str) and recorded:
                original_model = recorded
        except (OSError, ValueError) as exc:
            _log(
                f"[spend_alert] {bot_id}: existing tier_downgrade restore "
                f"record unreadable ({exc}) — recording fresh original"
            )
    payload = {
        "date": today_iso,
        "original_model": original_model,
        "applied_model": applied_model,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        _log(
            f"[spend_alert] {bot_id}: tier_downgrade restore record write "
            f"failed: {exc} — midnight auto-revert will not run"
        )


def _maybe_revert_tier_downgrade(
    *, bot_id: str, shared_dir: Path, today_iso: str,
) -> None:
    """Restore the pre-downgrade primary model once the downgrade lapses.

    The tier_downgrade write is a one-way openclaw.json edit; the dedup
    flag rolling at midnight only re-ARMS the downgrade for the new day.
    This sweep performs the "Auto-reverts at midnight" promise on the
    first tick where the flag no longer covers today — either the date
    rolled, or the operator/evo ``reset_remediation`` removed the flag
    mid-day. Runs every tick for every bot; no-op unless a restore
    record exists. On a failed restore write the record is kept so the
    next tick retries.
    """
    restore_path = _tier_downgrade_restore_path(shared_dir, bot_id)
    if not restore_path.exists():
        return
    flag = restore_path.parent / "tier_downgrade.flag"
    flag_date = None
    if flag.exists():
        try:
            flag_date = flag.read_text().strip()
        except OSError:
            flag_date = None
    if flag_date == today_iso:
        return  # downgrade still active today
    try:
        rec = json.loads(restore_path.read_text())
    except (OSError, ValueError) as exc:
        _log(
            f"[spend_alert] {bot_id}: tier_downgrade restore record "
            f"unreadable ({exc}) — clearing; restore the primary model "
            f"manually if the bot is still downgraded"
        )
        try:
            restore_path.unlink()
        except OSError as unlink_exc:
            _log(
                f"[spend_alert] {bot_id}: could not clear unreadable "
                f"restore record: {unlink_exc}"
            )
        return
    original = rec.get("original_model")
    applied = rec.get("applied_model")
    if isinstance(original, str) and original:
        # If the operator changed the primary since the downgrade,
        # restoring would clobber their choice — skip and just clean up.
        current = None
        try:
            reader = _get_tier_downgrade_reader()
            if reader is not None:
                current = reader(bot_id)
        except Exception:  # noqa: BLE001
            current = None
        if (
            current is not None
            and isinstance(applied, str) and applied
            and current != applied
        ):
            _log(
                f"[spend_alert] {bot_id}: tier_downgrade revert skipped — "
                f"primary is {current!r}, not the downgraded {applied!r} "
                f"(changed since); clearing marker"
            )
        else:
            writer = _get_tier_downgrade_writer()
            if writer is None:
                _log(
                    f"[spend_alert] {bot_id}: tier_downgrade revert pending "
                    f"— writer unavailable (cost_profiles not importable); "
                    f"will retry next tick"
                )
                return
            try:
                ok, detail = writer(
                    bot_id,
                    {"agents": {"defaults": {"model": {"primary": original}}}},
                )
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"writer raised: {exc}"
            if not ok:
                _log(
                    f"[spend_alert] {bot_id}: tier_downgrade revert WRITE "
                    f"FAILED: {detail or 'unknown'} — will retry next tick"
                )
                return
            _log(
                f"[spend_alert] {bot_id}: tier_downgrade reverted — "
                f"primary restored to {original}"
            )
    else:
        _log(
            f"[spend_alert] {bot_id}: tier_downgrade lapsed but no original "
            f"model was recorded — cannot auto-restore; clearing marker"
        )
    try:
        restore_path.unlink()
    except OSError as exc:
        _log(
            f"[spend_alert] {bot_id}: tier_downgrade restore record "
            f"cleanup failed: {exc}"
        )
    if flag_date is not None:
        # Stale flag from a previous day — remove it alongside the record.
        try:
            flag.unlink()
        except OSError as exc:
            _log(
                f"[spend_alert] {bot_id}: stale tier_downgrade flag "
                f"cleanup failed: {exc}"
            )


def _resolve_tier3_model(network: dict) -> str:
    """Pick the tier-3 model id to fall back to on tier_downgrade.

    Reads ``network.json::classifiers.tier.tier`` (the pod's chosen
    tier-3 classifier tier — typically already a Haiku-class model)
    when present; falls back to a conservative Anthropic default.
    The fallback string must match a model id the deploy materializer
    will accept; downstream OC config validation catches typos at
    the next gateway reload.
    """
    try:
        choice = (network.get("classifiers") or {}).get("tier", {}).get("tier")
        if isinstance(choice, str) and choice.startswith("tier"):
            # Some installs encode "tier3" as a logical label rather
            # than a model id; map to the conservative Haiku default
            # for those cases.
            pass
    except Exception:
        pass
    return "anthropic/claude-haiku-4-5"


def _l2_breaker_already_tripped(shared_dir: Path, bot_id: str) -> bool:
    """True iff an L2 cost breaker is currently in effect for ``bot_id``.

    Mirrors ``_breaker_already_tripped`` but checks the cost_l2 key
    (Phase 6 — distinct from L1's "cost"). L2 has no auto-reset; the
    breaker stays tripped until the operator runs
    ``evolve-admin breaker reset <bot> cost_l2``.
    """
    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] _l2_breaker_already_tripped({bot_id}): "
            f"breakers.store import failed ({exc}); "
            f"proceeding as not-tripped (fail-open)"
        )
        return False
    try:
        rec = _bstore.read_trip(shared_dir, bot_id, "cost_l2")
    except Exception as exc:
        _log(
            f"[spend_alert] _l2_breaker_already_tripped({bot_id}): "
            f"breakers.store.read_trip raised ({exc}); fail-open"
        )
        return False
    if rec is None:
        return False
    return not _bstore.is_expired(rec)


def _trip_l2_breaker_for_cost_cap(
    *, bot_id: str, spend: float, cap: float, shared_dir: Path,
    network: dict, today_iso: str,
) -> None:
    """Auto-trip the L2 cost breaker for ``bot_id`` — bootouts the bot's
    gateway entirely. Spec: internal/spec-cost-caps-2026-06-05.md.

    Distinct from L1 (which only pauses heartbeat / background and
    leaves user chat working). L2 stops the gateway entirely; the user
    sees a Phase-4c OOO message in their chat channel and the bot is
    effectively offline until the operator manually resets.

    Dedup: skips when an L2 cost breaker is already tripped. The
    breaker file itself is the dedup primitive; L2 has no auto-reset
    so subsequent ticks always see it tripped until manual reset.
    """
    if _l2_breaker_already_tripped(shared_dir, bot_id):
        _log(
            f"[spend_alert] {bot_id}: L2 cap ${cap:.2f} crossed "
            f"(${spend:.2f}) but L2 cost breaker already tripped — skipping"
        )
        return

    # L2 trips have no auto-reset; we set a long expiry as a courtesy
    # so the breakers UI doesn't show "expired" while the gateway is
    # still down. The actual lifetime is "until manual reset".
    from datetime import timedelta as _td

    try:
        from breakers import store as _bstore  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] breakers.store import failed; "
            f"{bot_id} L2 auto-trip dropped: {exc}"
        )
        return

    reason = (
        f"per-bot L2 cap exceeded: ${spend:.2f} ≥ ${cap:.2f}"
    )
    try:
        rec = _bstore.trip(
            shared_dir=shared_dir,
            scope=bot_id,
            breaker_type="cost_l2",
            # 30 days is an arbitrary "long expiry" — operator must
            # manually reset for the gateway to come back up. Treating
            # the breaker file's existence as the source of truth.
            duration=_td(days=30),
            initiated_by="auto:spend_alert",
            reason=reason,
        )
    except Exception as exc:
        _log(
            f"[spend_alert] {bot_id}: L2 breaker trip raised: {exc}"
        )
        return

    # Synchronous enforcement: launchctl bootout via the existing
    # breakers_enforce path. Posts an OOO message to the bot's user
    # channel before tearing down the gateway.
    enforce_result = None
    enforce_raised = False
    enforce_fn = _get_l2_enforce_trip()
    if enforce_fn is None:
        enforce_raised = True
        _log(
            f"[spend_alert] {bot_id}: L2 enforce_trip unavailable; "
            f"breaker file written but bootout did NOT run"
        )
    else:
        try:
            enforce_result = enforce_fn(
                scope=bot_id, breaker_type="cost_l2", network=network,
                shared_dir=shared_dir,
                reason=reason, expires_at_iso=rec.expires_at,
            )
        except Exception as exc:
            enforce_raised = True
            _log(
                f"[spend_alert] {bot_id}: L2 enforce raised: {exc}. "
                f"Breaker file written; operator should inspect."
            )

    base_reason = f"Spent ${spend:.2f} of ${cap:.2f} L2 cap; gateway stopped"
    if enforce_raised:
        reason_text = (
            f"{base_reason}. Enforcement FAILED — "
            f"run 'evolve-admin breaker status' and check daemon logs."
        )
    elif enforce_result is not None and not enforce_result.ok:
        reason_text = f"{base_reason}. Enforcement partially failed."
    else:
        reason_text = (
            f"{base_reason}. Manual reset required: "
            f"'evolve-admin breaker reset {bot_id} cost_l2'."
        )

    _log(
        f"[spend_alert] {bot_id}: L2 CAP ${spend:.2f} ≥ ${cap:.2f} — "
        f"L2 cost breaker tripped (trip_id={rec.trip_id[:8]})"
    )

    _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/l2cap/{bot_id}/{today_iso}",
        severity_name="critical",
        catalog_event="cost.gateway_stopped",
        payload={
            "bot_id": bot_id,
            "breaker_type": "cost_l2",
            "reason": reason_text,
            "trip_id_short": rec.trip_id[:8],
        },
    )
    _flag_urgent_refresh(
        shared_dir, source="spend_alert",
        reason="l2_breaker_tripped", bot_id=bot_id,
    )


# ── Hard cap alert ───────────────────────────────────────────────────────────
#
# ``_send_cap_alert`` (dispatching ``cost.hard_cap_hit``) used to live here. Its
# only caller was the pod-wide ``thresholds.dailySpendCapUsd`` branch, removed
# with the second cap reader — that branch had been unreachable on every
# migrated pod anyway. Re-pointing it at the L1 trip would double-alert: that
# path deliberately dispatches ``cost.breaker_tripped`` INSTEAD of
# ``cost.hard_cap_hit`` (Fix B, 2026-05-28 — the breaker trip is the news, the
# cap is only the cause). The ``cost.hard_cap_hit`` catalog entry and its
# operator subscriptions are left in place; retiring them is a separate call.

# ── Daily-spend alert threshold (single source) ─────────────────────────────────

# Product default for the daily per-bot spend *alert* (the "alert at $N/day"
# the operator sees fire). Overridable per-pod via network.json.
DEFAULT_DAILY_SPEND_ALERT_USD = 5.0


def daily_spend_alert_threshold_usd(config: dict) -> float:
    """The daily per-bot spend *alert* threshold this alerter fires on.

    Single source of truth for "alert at $N/day", resolved from the network
    config: ``alerts.spendThresholdUSD`` override → ``thresholds.
    dailySpendAlertUsd`` → :data:`DEFAULT_DAILY_SPEND_ALERT_USD`. ``main()``
    uses this same accessor, so anything that wants to *project* the alert
    (e.g. the add-bot wizard's cost panel) reads the exact value that fires
    and can never drift from it.

    ``config`` is the parsed network.json dict (as returned by ``load_config``
    / the admin ``load_network``).
    """
    alerts_cfg = get_alerts(config)
    thresholds_cfg = config.get("thresholds", {})
    return float(
        alerts_cfg.get("spendThresholdUSD")
        or thresholds_cfg.get("dailySpendAlertUsd", DEFAULT_DAILY_SPEND_ALERT_USD)
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve real-time spend alert")
    parser.add_argument("--network", default=str(resolve_network_path()),
                        help="Path to network.json config")
    args = parser.parse_args()

    config = load_config(args.network)

    if not is_module_enabled(config, "cost"):
        print("[spend_alert] cost module disabled — exiting.")
        sys.exit(0)

    alerts_cfg = get_alerts(config)

    # Top-level spend alert toggle in alerts block
    if not alerts_cfg.get("enabled", True):
        print("[spend_alert] alerts.enabled=false — exiting.")
        sys.exit(0)

    thresholds_cfg = config.get("thresholds", {})
    threshold = daily_spend_alert_threshold_usd(config)
    weekly_threshold = float(thresholds_cfg.get("weeklySpendAlertUsd", 20.0))
    # Burst defaults: same dollar floor as the daily threshold, 60-min
    # rolling window, alert (vs warn) tier at 3× the floor. These are the
    # numbers that would have caught the 2026-05-20 spike at ~19:10 UTC
    # for security_bot and ~20:15 UTC for team_bot_a — within one polling cycle of
    # the runaway sessions crossing $5/hour.
    burst_threshold = float(thresholds_cfg.get("burstAlertUsd", threshold))
    burst_window_min = int(thresholds_cfg.get("burstAlertWindowMin", 60))
    burst_critical_mult = float(
        thresholds_cfg.get("burstCriticalMultiplier", 3.0)
    )

    now = datetime.now(timezone.utc)
    # `today` is the day boundary for cap rollover + weekly bucketing — it
    # must be pod-local, not UTC. A Pacific-TZ pod that ticks at 23:00 PT
    # is at 06:00 UTC the next day, so UTC `today` would roll caps a day
    # early and bucket weekly summaries into the wrong ISO week. Log
    # timestamps still use `now` (UTC); see spec-cost-caps-2026-06-05.md
    # §"Day boundary".
    from pod_time import pod_today
    today = pod_today()

    # No noon gate: cost runaways at 03:00 must alert at 03:00, not noon.
    # The legacy ``alerts.spendAlertHour`` setting is intentionally
    # ignored for threshold + burst checks; it survives only on the
    # weekly summary path which is naturally a daytime artifact.

    shared_dir = get_shared_dir(config)
    members = get_members(config)

    if not members:
        print("[spend_alert] No members configured — nothing to check.", file=sys.stderr)
        sys.exit(0)

    # Hard cap config comes from ONE reader: ``_resolve_per_bot_caps`` →
    # ``BetterEngineConfig.resolve_budget_ladder``, resolved per bot inside the
    # loop below. The per-bot ladder now inherits ``pod_defaults.budget``, so
    # the pod-wide cap reaches every bot through the same rungs.
    #
    # Removed here: a second, disagreeing reader —
    # ``thresholds_cfg.get("dailySpendCapUsd")`` off raw
    # ``network.json::thresholds``, gating a parallel
    # ``spend_caps.execute_enforcement_action`` branch. The Phase-8 migration
    # (``migrations/cost_caps_normalize.py``) empties that key, so on every
    # migrated pod the branch was unreachable — and on an un-migrated pod it
    # would now double-enforce the same dollar figure the L1 rung already
    # owns. Its one live side effect, the spend-cap enforcement flag that
    # ModelRouter and ``GET /api/spend-caps`` read, moved onto the L1 trip
    # (see ``_trip_breaker_for_cost_cap``).
    bots_cfg = config.get("bots", {})

    discovery_failures: list[str] = []
    # Per-tick state for the post-loop sweep that auto-archives stale
    # cost_burst Signals. See ``_sweep_stale_burst_signals``: bots whose
    # discovery succeeded but who have no firing burst this tick get
    # their previous-hour stranded Signals archived. Bots whose
    # discovery failed are NOT swept — their cost_burst history is
    # untouched until the next clean tick.
    kept_burst_sigs: set[str] = set()
    checked_bots: set[str] = set()

    for bot_id in members:
        # ── Tier-downgrade midnight revert (Phase 6) ──────────────────
        # Runs before the spend checks so a lapsed downgrade is undone
        # even when today's JSONL discovery fails below. No-op unless a
        # restore record from a previous downgrade exists.
        _maybe_revert_tier_downgrade(
            bot_id=bot_id, shared_dir=shared_dir, today_iso=str(today),
        )

        # Per-bot exemption: operator-confirmed forge installs (tagged
        # forge_subkind=operator_confirmed_install during turn retag) are
        # filtered out of spend totals when the bot has
        # forge_install_exempt_from_daily_cap=True (default). The operator
        # saw the projected cost and confirmed — a tight daily_cap_usd
        # protecting against background runaway should not also block
        # informed operator actions. See packages/admin/evolve_admin/
        # config.py::get_forge_install_exempt_from_daily_cap.
        _bot_cfg = bots_cfg.get(bot_id, {}) if isinstance(bots_cfg, dict) else {}
        _exempt_raw = _bot_cfg.get("forge_install_exempt_from_daily_cap")
        if _exempt_raw is None:
            _is_exempt = True  # default-on per the chip
        elif isinstance(_exempt_raw, bool):
            _is_exempt = _exempt_raw
        elif isinstance(_exempt_raw, str):
            _is_exempt = _exempt_raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            try:
                _is_exempt = bool(int(_exempt_raw))
            except (TypeError, ValueError):
                _is_exempt = True
        exempt_subkinds = (
            {"operator_confirmed_install"} if _is_exempt else None
        )

        # ── Burst check (every tick; per-(bot, hour) Signal dedup) ──
        burst_cost, burst_turns = burst_window_spend(
            bot_id, now=now, window_minutes=burst_window_min,
            exempt_subkinds=exempt_subkinds,
        )
        if burst_cost < 0:
            # Discovery failed (see _LIVE_LOAD_FAILED). Track for the
            # self-monitoring Signal emitted below; do NOT silently
            # treat this as "no burst."
            discovery_failures.append(bot_id)
        else:
            # Discovery succeeded — this bot is eligible for the
            # post-loop sweep. Tracked regardless of whether the burst
            # is firing: a sub-threshold bot with a still-firing
            # signal from an earlier window is exactly what we want to
            # auto-archive.
            checked_bots.add(bot_id)
            if burst_cost >= burst_threshold:
                top = _top_sessions(burst_turns, n=2)
                emit_burst_signal(
                    shared_dir=shared_dir,
                    bot_id=bot_id,
                    cost_usd=burst_cost,
                    threshold_usd=burst_threshold,
                    critical_multiplier=burst_critical_mult,
                    window_minutes=burst_window_min,
                    top_sessions=top,
                    now=now,
                )
                # Keep the current-hour signature so the sweep below
                # doesn't archive the Signal we just (re-)observed.
                sig_keep = _burst_signature(bot_id, now)
                if sig_keep is not None:
                    kept_burst_sigs.add(sig_keep)
                send_burst_alert(
                    bot_id, burst_cost, burst_threshold,
                    critical_multiplier=burst_critical_mult,
                    window_minutes=burst_window_min,
                    top_sessions=top,
                    shared_dir=shared_dir,
                    network=config,
                    hour_bucket=_hour_bucket(now),
                )

        # ── Daily threshold + hard cap (run on every tick, not gated by hour) ──
        spend = load_today_spend(
            shared_dir, bot_id, today, now=now,
            exempt_subkinds=exempt_subkinds,
        )
        if spend is None:
            _log(
                f"[spend_alert] {bot_id}: live JSONL discovery failed — "
                f"skipping threshold check (self-failure Signal will fire)"
            )
            continue

        # ── Per-bot graduated remediation ladder (Phase 6) ─────────────
        # Spec: internal/spec-cost-caps-2026-06-05.md.
        # Each tier is independent — multiple tiers can fire on the same
        # tick if the operator's spend crosses several thresholds at once
        # (e.g. catching up after a missed window). Tier fires in order
        # of escalation so the catalog notifications surface in the
        # operator's chat with the right severity ladder.
        # Post-fix this ladder inherits ``pod_defaults.budget`` per rung, so a
        # bot with no explicit override is enforced at the pod cap the UI
        # displays — it no longer resolves to an all-None (= no enforcement
        # anywhere) ladder.
        ladder = _resolve_per_bot_caps(bot_id, shared_dir, now=now)
        # The L1 threshold is the cap of record for the velocity forecast.
        per_bot_cap = ladder["l1_breaker"]

        # 1. tier_downgrade: switch primary model for the rest of the day.
        td_cap = ladder["tier_downgrade"]
        if td_cap is not None and spend >= td_cap:
            _apply_tier_downgrade(
                bot_id=bot_id, shared_dir=shared_dir, network=config,
                spend=spend, cap=td_cap, today_iso=str(today),
            )

        # 2. L1 breaker: pause heartbeat + background; user chat keeps working.
        if per_bot_cap is not None and spend >= per_bot_cap:
            _trip_breaker_for_cost_cap(
                bot_id=bot_id, spend=spend, cap=per_bot_cap,
                shared_dir=shared_dir, network=config, today_iso=str(today),
            )

        # 3. L2 breaker: stop the bot gateway entirely. Manual reset only.
        l2_cap = ladder["l2_breaker"]
        if l2_cap is not None and spend >= l2_cap:
            _trip_l2_breaker_for_cost_cap(
                bot_id=bot_id, spend=spend, cap=l2_cap,
                shared_dir=shared_dir, network=config, today_iso=str(today),
            )

        # ── Velocity forecast (intraday projection vs cap) ────────────
        # Fires a same-day warn Signal when today's spend rate, projected
        # to midnight, will cross ``velocity_forecast_fraction`` of the cap.
        # Distinct from burst (last N min spike) and from daily_spend_high
        # (actuals over threshold) — this catches the slow-bleed shape so
        # the operator has a knob to turn before the breaker trip.
        # Per-(bot, day) signature dedups across ticks; severity upgrades
        # warn → alert as projection approaches the cap.
        velocity_cap = per_bot_cap if per_bot_cap is not None else 0.0
        if velocity_cap > 0 and spend > 0:
            emit_velocity_forecast_signal(
                shared_dir=shared_dir,
                bot_id=bot_id,
                spend_usd=spend,
                cap_usd=velocity_cap,
                warn_fraction=float(
                    thresholds_cfg.get("velocityForecastWarnFraction", 0.70)
                ),
                today=today,
                now=now,
            )

        if spend <= threshold:
            print(f"[spend_alert] {bot_id}: OK (${spend:.4f} <= ${threshold:.2f})")
            continue

        if alert_already_sent(shared_dir, bot_id, today):
            print(f"[spend_alert] {bot_id}: already alerted today — skipping")
            continue

        # Threshold exceeded and not yet alerted — fire
        sent = send_alert(
            bot_id, spend, threshold,
            shared_dir=shared_dir, network=config, today_iso=str(today),
        )
        if sent:
            write_flag(shared_dir, bot_id, today, spend, threshold)
            # Flag urgent Better Engine refresh — spend threshold exceeded
            _flag_urgent_refresh(shared_dir, source="spend_alert",
                                 reason="spend_threshold_exceeded", bot_id=bot_id)

    # ── Auto-archive stale cost_burst Signals ─────────────────────────
    # Comprehensive sweep contract: any firing/snoozed cost_burst Signal
    # from a bot we successfully checked, whose signature isn't in this
    # tick's keep-set, is no longer above threshold and should be
    # archived. Without this, a burst that crossed an hour boundary
    # (e.g. 19:55 → 20:05) leaves two near-identical Signals stranded
    # in firing/ forever once the bleed stops — the operator sees
    # "2 findings" duplicates on the Alerts page days later. Fixed
    # 2026-06-06; the comment on _hour_bucket explains the by-design
    # straddling that this sweep complements.
    if checked_bots:
        _sweep_stale_burst_signals(shared_dir, checked_bots, kept_burst_sigs)

    # ── Self-monitoring: surface live-JSONL discovery failures ────────
    # If any bot's turn JSONL couldn't be located, fire a Signal so the
    # operator finds out via the same Alerts channel that's supposed to
    # surface bursts. Without this, an evolve-user ACL drift on one bot
    # would silently re-enable the original "spend_alert ran 24× and saw
    # nothing" failure mode for that bot.
    if discovery_failures:
        _emit_self_failure_signal(shared_dir, discovery_failures, now)

    # Weekly summary (Mondays, once per week)
    if today.weekday() == 0:  # Monday
        _maybe_send_weekly_summary(shared_dir, members, today, weekly_threshold, config)


def _sweep_stale_burst_signals(
    shared_dir: Path, checked_bots: set[str], kept_signatures: set[str],
) -> None:
    """Archive cost_burst Signals not in the keep-set for checked bots.

    Wraps ``signals.store.sweep_resolve`` with the spend_alert producer
    + cost_burst type + bot-id filter. Failures are logged but never
    raised — a sweep miss leaves a stale Signal for one extra tick,
    which is recoverable; a thrown exception would crash the daemon.
    """
    try:
        from signals import store as _signals_store  # type: ignore[import]
    except Exception as exc:
        _log(
            f"[spend_alert] sweep_resolve: signals.store import failed; "
            f"stale cost_burst Signals NOT archived this tick: {exc}"
        )
        return
    try:
        resolved = _signals_store.sweep_resolve(
            shared_dir,
            producer="spend_alert",
            types={"cost_burst"},
            bot_ids=checked_bots,
            kept_signatures=kept_signatures,
            reason="auto-resolve: burst window no longer above threshold",
        )
    except Exception as exc:
        _log(f"[spend_alert] sweep_resolve raised; continuing: {exc}")
        return
    if resolved:
        _log(
            f"[spend_alert] auto-archived {len(resolved)} stale cost_burst "
            f"Signal(s) for bots: "
            f"{', '.join(sorted({s.bot_id or '?' for s in resolved}))}"
        )


def _emit_self_failure_signal(
    shared_dir: Path, failed_bot_ids: list[str], now: datetime
) -> None:
    """Emit a single ``spend_alert/self_failure/<day>`` Signal.

    Signature is keyed on date (not hour) so a persistent failure
    re-observes within a single Signal across the day rather than
    spawning 288 per-tick duplicates. The Alerts page then shows one
    sticky entry the operator can act on, with observation_count
    tracking how many ticks have hit the same problem.
    """
    try:
        from signals import store as signals_store  # type: ignore[import]
        from schema.signal import make_signature  # type: ignore[import]
    except Exception as exc:
        _log(f"[spend_alert] self-failure Signal import failed: {exc}")
        return

    day_key = now.date().isoformat()
    signature = make_signature(
        "spend_alert", "self_failure_jsonl_discovery", day_key
    )
    bots_str = ", ".join(sorted(failed_bot_ids))
    try:
        signals_store.observe(
            shared_dir,
            signature=signature,
            producer="spend_alert",
            type="self_failure_jsonl_discovery",
            flavor="maintenance",
            severity="alert",   # this IS the silent-failure class
            scope="pod",
            title=(
                f"spend_alert can't read turn JSONL for "
                f"{len(failed_bot_ids)} bot(s)"
            ),
            body=(
                f"Live turn JSONL discovery failed for: {bots_str}.\n"
                "Burst + threshold checks did not run for these bots. "
                "Check that the evolve user has read access to "
                "/Users/Shared/evolve/{bot}/turns/ and that "
                "usage_analytics imports cleanly."
            ),
            details={
                "failed_bots": sorted(failed_bot_ids),
                "observed_at": now.isoformat(timespec="seconds"),
                "what_it_means": (
                    "spend_alert tried to read turn JSONL for "
                    f"{len(failed_bot_ids)} bot(s) — "
                    f"{bots_str} — and the read failed. This is "
                    "the silent-failure surface that the 2026-05-20 "
                    "incident retrofitted: when the daemon can't "
                    "see a bot's spend it MUST page rather than "
                    "default to 'no burst'. While this Signal is "
                    "firing, those bots have zero burst + daily "
                    "threshold coverage — they could be burning "
                    "money right now and no one would know."
                ),
                "fix_steps": (
                    "1. Verify the evolve user can read the bots' "
                    "turn directories:\n"
                    "   ssh pod_admin_user@mini sudo -u evolve ls "
                    "/Users/Shared/evolve/<bot>/turns/\n"
                    "2. If permission-denied: ACL is missing. "
                    "Re-apply by redeploying the affected bot:\n"
                    "   ssh pod_admin_user@mini sudo evolve-admin deploy "
                    "<bot>\n"
                    "3. If 'No such file or directory': the bot's "
                    "turn-collector isn't writing — check that the "
                    "gateway is up via Maintenance → Health\n"
                    "4. If the issue is usage_analytics imports: "
                    "tail /Users/Shared/evolve/logs/spend_alert.log "
                    "for the import-failed stderr\n"
                    "5. Once fixed, the next spend_alert tick "
                    "(every 5min) will auto-resolve this Signal"
                ),
            },
        )
    except Exception as exc:
        _log(f"[spend_alert] self-failure Signal observe raised: {exc}")


# ── Weekly summary ────────────────────────────────────────────────────────────

def _weekly_spend(shared_dir: Path, members: list[str], today: date) -> tuple[float, dict[str, float]]:
    """Return (total_7d, {bot_id: spend}) for the last 7 days."""
    from datetime import timedelta
    per_bot: dict[str, float] = {}
    for bot_id in members:
        bot_total = 0.0
        for i in range(7):
            day = (today - timedelta(days=i)).isoformat()
            mf = shared_dir / "metrics" / day / f"{bot_id}.json"
            if mf.exists():
                try:
                    data = json.loads(mf.read_text())
                    bot_total += float(data.get("total_cost_estimated", 0.0))
                except (json.JSONDecodeError, ValueError):
                    pass
        per_bot[bot_id] = round(bot_total, 4)
    return round(sum(per_bot.values()), 4), per_bot


def _maybe_send_weekly_summary(
    shared_dir: Path, members: list[str], today: date,
    weekly_threshold: float, network: dict,
) -> None:
    """Send a weekly spend summary on Mondays, once per week."""
    import datetime as _dt
    iso_week = today.strftime("%G-W%V")
    flag = shared_dir / "alerts" / f"weekly-summary-{iso_week}.flag"
    if flag.exists():
        return

    total, per_bot = _weekly_spend(shared_dir, members, today)
    _log(f"[spend_alert] weekly summary: ${total:.2f} over 7d (threshold ${weekly_threshold:.2f})")

    per_bot_breakdown = "\n".join(f"  {b}: ${v:.2f}" for b, v in sorted(per_bot.items()))
    if total > weekly_threshold:
        per_bot_breakdown = (
            f"⚠️ above ${weekly_threshold:.2f} weekly threshold\n" + per_bot_breakdown
        )
    sent = _dispatch(
        shared_dir=shared_dir,
        network=network,
        dedup_key=f"spend_alert/weekly/{iso_week}",
        severity_name="info",
        catalog_event="cost.weekly_summary",
        payload={
            "iso_week": iso_week,
            "total": total,
            "per_bot_breakdown": per_bot_breakdown,
        },
    )
    if sent:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"week": iso_week, "total_usd": total,
                                    "sent_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}))
        _log(f"[spend_alert] weekly summary sent for {iso_week}")


if __name__ == "__main__":
    main()
