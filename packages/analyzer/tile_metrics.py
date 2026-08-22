#!/usr/bin/env python3
"""
tile_metrics.py — per-bot tile data for the admin dashboard overview.

Replaces the 0-100 composite score + four 25-point bars with concrete,
interpretable signals: recent activity, recent cost, app utilisation,
and discrete health chips.

The shape of the per-bot block returned by `compute_member_tile_data`:

    {
      "activity": {
        "turns_1d":            int,
        "turns_prior_1d":      int,
        "turns_7d":            int,
        "turns_prior_7d":      int,
        "turns_28d":           int,
        "turns_prior_28d":     int,
        "sessions_1d":         int,
        "sessions_prior_1d":   int,
        "sessions_7d":         int,
        "sessions_prior_7d":   int,
        "sessions_28d":        int,
        "sessions_prior_28d":  int,
        "human_pct_7d":        float | None,   # None if no cost events
        "scheduled_pct_7d":    float | None,
        "background_pct_7d":   float | None,
        "human_pct_28d":       float | None,
        "scheduled_pct_28d":   float | None,
        "background_pct_28d":  float | None,
      },
      "cost": {
        "usd_1d":              float,
        "usd_prior_1d":        float,
        "usd_7d":              float,
        "usd_prior_7d":        float,
        "usd_28d":             float,
        "usd_prior_28d":       float,
      },
      "apps": {
        "total":               int,
        "used_7d":             int,
        "used_28d":            int,
      },
      "anchor_date": "YYYY-MM-DD",       # last day with a metrics file
      "value": {                         # latest value-rollup entry for this
        "utilization_state": "active"|"underused"|"unmeasurable",
        "state_reason":      str,        # bot (spec-value-baseline §7.1),
        "active_human_days_7d/_28d":  {...},  # or None when no rollup
        "proactive_runs_7d/_28d":     {...},  # exists yet
        "app_coverage_28d":  {...},
        "value_trend_28d":   {...},
        "as_of":  "YYYY-MM-DD",          # rollup anchor date (§5.3)
        "stale":  bool,                  # rollup older than 48h (§5.3)
      } | None,
      "health_chips": [
        {"id": str, "severity": "warn"|"critical", "label": str, "detail": str,
         "horizon": "today"|"7d"|"ongoing",
         "digest_tier": "critical_now"|"today_news"|"tile_only"},
        ...
      ],
    }

The ``horizon`` field declares the time scale the chip's evidence covers
(useful for the planned weekly bot-trends digest); the ``digest_tier``
field tells the daily Bot digest whether to surface the chip at all:

  * ``critical_now`` — operational state that needs action today.
                       Breaker tripped, gateway down, infra daemon
                       down, security audit at CRITICAL, disk ≥ 95%.
                       Always shown in the daily digest.
  * ``today_news``   — event that happened in the last ~24h and rolls
                       off naturally tomorrow (same-day cost spike).
                       Shown in the daily digest until it ages out.
  * ``tile_only``    — standing setup tasks, 7d trends, and warnings
                       the operator already knows about. Shown in the
                       admin UI tile view but NOT in the daily digest
                       (avoids the "pair WhatsApp" / "scan needed"
                       chips repeating every day forever). The weekly
                       bot-trends digest may pick up the ``horizon: 7d``
                       subset.

The horizon values:

  * ``today``   — event in the last ~24h (e.g. same-day cost spike).
  * ``7d``      — aggregate / rolling window over 7 days. Pulled in by
                  the weekly digest, not the daily.
  * ``ongoing`` — current-state condition. Stays until fixed. Default
                  when the field is absent so chips from producers that
                  don't yet declare a horizon stay visible in the tile.

The windows ("1d", "7d", "28d") are anchored on ``anchor_date`` — the
most-recent day with a metrics file — rather than on today. The
measure.py cron writes each day's aggregate at 1am the following
morning, so during the day "today" has no file yet; anchoring on the
latest data prevents the trailing empty day from diluting averages and
keeps the "1d" tile meaningful.

28 days (4 weeks) rather than 30: bots have strong weekly cycles
(humans use them more on weekdays, heartbeats run 24/7), so a 30-day
window biases the comparison depending on whether it caught 5 vs 4
Saturdays. 28d-vs-prior-28d is two clean 4-week buckets — same number
of each weekday — which makes the delta number trustworthy.

The same shape applies to the primary bot tile — the primary bot emits
its own daily metrics file (analyzer, security scans, etc), so the tile
renders from its own data, not a pod-wide rollup. Admin-specific health
chips (daemon-fleet liveness, ACL drift, repo-puller lag, disk pressure)
are appended via ``compute_admin_health_chips`` when the bot's role is
"primary".

Reads
-----
* Daily metrics: ``{shared_dir}/metrics/{YYYY-MM-DD}/{bot_id}.json`` —
  produced by ``measure.py``. Source of turn_count, session_count,
  total_cost_estimated, correction_count, unexpected_billing_turns,
  app_usage.
* Cost events: ``{shared_dir}/annotations/{bot_id}/cost_events-{date}.jsonl``
  — source of trigger_kind for the user/automation split.
* Application manifests: ``{shared_dir}/applications/{bot_id}/*.json``
* Value rollup: ``{shared_dir}/metrics/value/{YYYY-MM-DD}.json`` — nightly
  per-bot utilization rollup from ``value_baseline.py``; source of the
  tile's ``value`` block and the ``underused`` chip.
* Scan status: ``/Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json``
  (with ``{shared_dir}/applications/{bot_id}/.scan-status.json`` accepted as
  a legacy fallback, fresher mtime wins).

All inputs are read-only; this module does not write.
"""

from __future__ import annotations

import json
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Daily metrics loading
# ─────────────────────────────────────────────────────────────────────────────


def _load_daily_metric(shared_dir: Path, bot_id: str, d: date) -> dict | None:
    """Return the metrics dict for one (bot, date), or None if missing/unreadable."""
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _load_metrics_by_date(
    shared_dir: Path, bot_id: str, days: int, today: date
) -> dict[date, dict]:
    """Load up to ``days`` days of metrics, indexed by date.

    Skips days with no file. Returned dict maps ``date`` → metrics dict.
    """
    out: dict[date, dict] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        m = _load_daily_metric(shared_dir, bot_id, d)
        if m is not None:
            out[d] = m
    return out


def _window_sum(
    by_date: dict[date, dict], start: date, end_exclusive: date, key: str
) -> int | float:
    """Sum ``key`` across daily metrics in ``[start, end_exclusive)``.

    Returns 0 (int) when no data falls in the window. Caller decides whether
    to coerce to float by passing a key that yields floats.
    """
    total: int | float = 0
    d = start
    while d < end_exclusive:
        m = by_date.get(d)
        if m is not None:
            v = m.get(key, 0)
            try:
                total += v  # type: ignore[operator]
            except TypeError:
                pass
        d += timedelta(days=1)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-kind split (user vs automation)
# ─────────────────────────────────────────────────────────────────────────────


def _read_cost_events(shared_dir: Path, bot_id: str, d: date):
    """Yield cost_event records for one (bot, date).

    Reads the converter file (every line is a cost_event) and the legacy
    annotations file (filtered to ``type == cost_event``). Mirrors
    ``cost_rollup.iter_cost_events``.
    """
    annotations_dir = shared_dir / "annotations" / bot_id
    converter_path = annotations_dir / f"cost_events-{d.isoformat()}.jsonl"
    legacy_path = annotations_dir / f"{d.isoformat()}.jsonl"

    for path, legacy in ((converter_path, False), (legacy_path, True)):
        if not path.exists():
            continue
        try:
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if legacy and rec.get("type") != "cost_event":
                        continue
                    yield rec
        except OSError:
            continue


_SCHEDULED_KINDS = frozenset({"heartbeat", "cron_app"})


def _classify_split(
    shared_dir: Path, bot_id: str, start: date, end_exclusive: date
) -> tuple[int, int, int]:
    """Count cost events in the window grouped into (human, scheduled, background).

    * **human**      — ``trigger_kind == "user_turn"`` (a person typed in).
    * **scheduled**  — ``heartbeat`` or ``cron_app`` (a clock fired).
    * **background** — everything else: ``subagent`` (in-session spawn),
                       ``summarizer`` / ``classifier`` / ``task_extractor`` /
                       ``fallback`` / ``unknown`` / missing trigger_kind.
                       These are the bot's own internal scaffolding, not
                       directly initiated by either a person or a clock.
    """
    human = 0
    scheduled = 0
    background = 0
    d = start
    while d < end_exclusive:
        for rec in _read_cost_events(shared_dir, bot_id, d):
            kind = rec.get("trigger_kind")
            if kind == "user_turn":
                human += 1
            elif kind in _SCHEDULED_KINDS:
                scheduled += 1
            else:
                background += 1
        d += timedelta(days=1)
    return human, scheduled, background


def _safe_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


# ─────────────────────────────────────────────────────────────────────────────
# Apps utilisation
# ─────────────────────────────────────────────────────────────────────────────


def _list_app_manifests(
    shared_dir: Path, bot_id: str, network: dict | None = None
) -> list[str]:
    """Return manifest IDs (filename stems) for one bot's installed apps.

    Reads bot-side first: ``/Users/<bot_user>/.openclaw/workspace/manifests/``
    — the canonical location since PR #1176 promoted manifests to per-bot
    state. This is the same source the Apps page reads via
    ``server.py::_list_manifests_as_bot``, so the tile and the Apps page
    can't disagree on the app count anymore. The evolve service user has
    ACL read access to every bot's ``.openclaw/`` directory by design
    (see CLAUDE.md and ``set_evolve_read_acl()`` in deploy.py).

    Falls back to the legacy shared-side mirror at
    ``{shared_dir}/applications/<bot_id>/`` (and the even-older
    ``capabilities/<bot_id>/`` slug) when:
      - ``network`` is unavailable, so we can't resolve a bot user
        (typical for unit-test fixtures that write to shared_dir)
      - the bot-side directory doesn't exist or isn't readable

    The filter matches the Apps page: skip hidden files AND skip
    ``*_history*`` files (transient session-history JSON the Apps page
    excludes from the install count).
    """
    # Bot-side path — canonical post-#1176. Require a *truthy* network: an
    # empty ``{}`` carries no bot→user mapping, so resolving a real bot user
    # from it is meaningless — ``get_bot_user`` would fall back to a guessed
    # ``/Users/<bot_id>/...`` path that, on a host where that user happens to
    # exist (e.g. a self-hosted CI runner reading the real ``evolve`` home),
    # silently shadows the test fixture's shared-side manifests and returns
    # whatever live state that dir holds (0 when unscanned). ``{}``/None mean
    # "no network mapping" — exactly the shared-side-fallback case the
    # docstring describes — so treat them the same.
    if network:
        try:
            from evolve_config import get_bot_user
            user = get_bot_user(bot_id, network)
            bot_side = Path(f"/Users/{user}/.openclaw/workspace/manifests")
            if bot_side.exists():
                try:
                    return sorted(
                        f.stem for f in bot_side.glob("*.json")
                        if not f.name.startswith(".")
                        and "_history" not in f.name
                    )
                except (PermissionError, OSError):
                    pass  # fall through to shared-side fallback
        except Exception:
            pass  # fall through to shared-side fallback

    # Legacy shared-side fallback — kept so test fixtures (which write
    # to shared_dir without a network mapping) and pre-#1176 pods still
    # produce a count rather than 0.
    cap_dir = shared_dir / "applications" / bot_id
    if not cap_dir.exists():
        cap_dir = shared_dir / "capabilities" / bot_id
    if not cap_dir.exists():
        return []
    return sorted(
        f.stem for f in cap_dir.glob("*.json")
        if not f.name.startswith(".") and "_history" not in f.name
    )


def _apps_used_in_window(
    by_date: dict[date, dict], start: date, end_exclusive: date
) -> set[str]:
    """Set of distinct app_ids that had at least one session in [start, end).

    Reads the ``app_usage`` field on each daily metrics record and unions
    the keys whose ``sessions`` count is > 0. ``app_usage`` is written by
    ``measure.py`` — schema:

        "app_usage": {
            "<app_id>": {
                "sessions": int,
                "correction_sessions": int,
                ...
            },
            ...
        }
    """
    used: set[str] = set()
    d = start
    while d < end_exclusive:
        m = by_date.get(d)
        if m:
            usage = m.get("app_usage") or {}
            for app_id, stats in usage.items():
                if not isinstance(stats, dict):
                    continue
                if int(stats.get("sessions", 0) or 0) > 0:
                    used.add(app_id)
        d += timedelta(days=1)
    return used


# ─────────────────────────────────────────────────────────────────────────────
# Health chips
# ─────────────────────────────────────────────────────────────────────────────


# Cost-spike chip threshold: only fire if BOTH the multiplier and the
# absolute floor are exceeded. Avoids yelling about $0.20 → $0.50.
#
# Two variants:
#  * 7d-vs-prior-7d ($5 floor, 2×) — slow-trend cost creep.
#  * 1d-vs-prior-1d ($2 floor, 3×) — same-day cost surge, catches the
#    kind of single-day spike that needs a same-day chip rather than
#    waiting for the 7d window to fill.
# Both can fire; the chip aggregator dedupes by id so they don't double.
COST_SPIKE_MULTIPLIER = 2.0
COST_SPIKE_FLOOR_USD = 5.0
COST_SPIKE_1D_MULTIPLIER = 3.0
COST_SPIKE_1D_FLOOR_USD = 2.0
HIGH_CORRECTION_RATE = 0.10
APPS_UNTESTED_DAYS = 30


def _breaker_expired(expires_at) -> bool:
    """True iff ``expires_at`` is a parseable ISO timestamp in the past.

    Mirrors ``breakers.store.is_expired`` and ``cost_opt_tiles._breaker_expired``
    without taking the cross-module import. Fail-open on parse errors.
    """
    if not expires_at:
        return False
    raw = str(expires_at)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        exp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def _bot_scan_status_path(network: dict | None, bot_id: str) -> Path | None:
    """Return the scanner-authoritative .scan-status.json path for one bot,
    or None when we can't resolve a system user (no network dict, lookup error).

    The scanner runs as the bot's macOS user and writes its progress to
    ``/Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json``.
    That file is the source of truth for "when did this bot last scan" —
    the admin server (running as ``evolve``) has macOS ACL read on every
    bot's ``.openclaw/`` for exactly this kind of cross-bot inspection.
    """
    if not network:
        return None
    try:
        from evolve_config import get_bot_user
    except Exception:
        return None
    try:
        user = get_bot_user(bot_id, network)
    except Exception:
        return None
    return Path(f"/Users/{user}/.openclaw/workspace/manifests/.scan-status.json")


def _compute_member_chips(
    shared_dir: Path,
    bot_id: str,
    bot_data: dict,
    by_date: dict[date, dict],
    today: date,
    cost_7d: float,
    cost_prior_7d: float,
    apps_total: int,
    network: dict | None = None,
    *,
    cost_1d: float = 0.0,
    cost_prior_1d: float = 0.0,
) -> list[dict]:
    """Build the list of health chips for a member bot. Empty list = healthy."""
    chips: list[dict] = []

    # ── gateway_down ──────────────────────────────────────────────────────
    # heal.py writes {shared_dir}/status/<bot>.json every 5 min with the
    # authoritative gateway probe result; the admin route overlays it onto
    # bot_data when fresh. If the gateway is unreachable right now, surface
    # it loudly — otherwise a metrics file from earlier today would leave
    # the tile silently "active" even though the bot is down.
    if (
        bot_data.get("gateway_status_fresh")
        and bot_data.get("gateway_reachable") is False
    ):
        chips.append({
            "id": "gateway_down",
            "severity": "critical",
            "label": "gateway down",
            "detail": (
                "process not running"
                if bot_data.get("gateway_running") is False
                else "gateway unreachable"
            ),
            "nav": "maintenance/status",
            "why": (
                "This bot's OpenClaw gateway isn't responding to "
                "Evolve's health probe."
            ),
            "impact": (
                "The bot can't receive messages from its channels or "
                "from evo — it's effectively offline until the gateway "
                "comes back up."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → Status",
                    "kind": "deep_link",
                    "nav": "maintenance/status",
                },
            ],
            "remediation_note": (
                "The bot tile's Actions ⋯ menu has a Restart option "
                "that kickstarts the gateway via launchd. If a restart "
                "doesn't bring it back, the Maintenance → Status page "
                "shows the heal daemon's last probe and the gateway's "
                "stderr log."
            ),
            "horizon": "ongoing",
            "digest_tier": "critical_now",
        })

    # ── breaker_tripped ───────────────────────────────────────────────────
    # When this bot's L1 cost breaker is active (tripped + not yet
    # cleared), surface it. Read directly from
    # {shared_dir}/breakers/<bot>/cost.json — the same file the
    # cost_opt_tiles.detect_breaker_tripped_chip helper reads. Inlined
    # here (rather than imported) so the main tile-chip path stays
    # self-contained: ``tile_metrics`` already owns the daily-digest-
    # facing chip list, and a cost_opt_tiles import would couple this
    # module to a sibling that has historically been admin-UI-only.
    #
    # Operator-facing rationale: the 2026-06-01 daily digest missed a
    # tripped breaker on a bot entirely because the tile-chip path
    # didn't know about breakers. Auto-turns paused is a critical
    # operational state — the digest must surface it.
    try:
        breaker_path = shared_dir / "breakers" / bot_id / "cost.json"
        if breaker_path.exists():
            data = json.loads(breaker_path.read_text())
            tripped_at = data.get("tripped_at")
            # `reset()` deletes the file, but TTL expiry doesn't — the
            # Phase-3 reaper does that, and until it runs the file lingers
            # (breakers/store.py §Semantics). Treat past-expiry as cleared
            # so the chip drops as soon as the TTL ends.
            if tripped_at and not _breaker_expired(data.get("expires_at")):
                chips.append({
                    "id": "breaker_tripped",
                    "severity": "critical",
                    "label": "breaker tripped",
                    "detail": "L1 cost breaker active — auto turns suspended",
                    "nav": "breakers",
                    "horizon": "ongoing",
                    "digest_tier": "critical_now",
                })
    except (OSError, json.JSONDecodeError, PermissionError):
        pass  # breaker file unreadable — silently skip the chip

    # ── stale_heartbeat ───────────────────────────────────────────────────
    # Driven by ``bot_data["status"]`` (already populated by network_status):
    #   online  — live HTTP probe just succeeded.
    #   active  — probe failed but a recent metrics file exists. May be
    #             a brief blip (gateway restart, lost-race) or a real outage.
    #   offline — no recent metrics, no live probe.
    #
    # Quiet when online; quiet when active + last metric is today/yesterday
    # (a normal day-rollover blip); warn when active + last metric is older
    # than that; critical when offline.
    status = bot_data.get("status")
    last_metric_date_str = bot_data.get("last_metric_date")
    if status == "offline":
        chips.append({
            "id": "stale_heartbeat",
            "severity": "critical",
            "label": "stale heartbeat",
            "detail": (
                f"last seen {last_metric_date_str}"
                if last_metric_date_str
                else "no recent metrics"
            ),
            "nav": "maintenance/status",
            "why": (
                "This bot hasn't written telemetry recently and isn't "
                "responding to live probes."
            ),
            "impact": (
                "The bot may be down, stuck, or unable to write metrics "
                "to disk. Recent activity (if any) isn't being captured."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → Status",
                    "kind": "deep_link",
                    "nav": "maintenance/status",
                },
            ],
            "remediation_note": (
                "The Status page shows the heal daemon's per-bot probe "
                "and last metrics-write timestamp. The tile's Actions ⋯ "
                "menu has Restart and Redeploy options if the bot needs "
                "to be brought back up."
            ),
            "horizon": "ongoing",
            "digest_tier": "tile_only",
        })
    elif status == "active":
        days_ago: int | None = None
        if last_metric_date_str:
            try:
                last = date.fromisoformat(last_metric_date_str)
                days_ago = (today - last).days
            except (TypeError, ValueError):
                days_ago = None
        if days_ago is None or days_ago > 1:
            detail = (
                f"last seen {days_ago}d ago"
                if days_ago is not None
                else "no recent metrics"
            )
            chips.append({
                "id": "stale_heartbeat",
                "severity": "warn",
                "label": "stale heartbeat",
                "detail": detail,
                "nav": "maintenance/status",
                "why": (
                    "This bot's live probe is failing but recent "
                    "metrics exist. May be a brief outage, a gateway "
                    "restart blip, or the start of something worse."
                ),
                "impact": (
                    "Right now, new messages to the bot may not be "
                    "answered. If the condition persists, this becomes "
                    "the critical 'offline' variant of the same chip."
                ),
                "remediations": [
                    {
                        "label": "Open Maintenance → Status",
                        "kind": "deep_link",
                        "nav": "maintenance/status",
                    },
                ],
                "remediation_note": (
                    "Often clears on its own once the gateway "
                    "reconnects. If it doesn't, the tile's Actions ⋯ "
                    "menu has a Restart option."
                ),
                "horizon": "ongoing",
                "digest_tier": "tile_only",
            })

    # ── version_drift ─────────────────────────────────────────────────────
    if bot_data.get("evolve_synced") is False:
        deployed = bot_data.get("evolve_version") or "unknown"
        chips.append({
            "id": "version_drift",
            "severity": "warn",
            "label": "version drift",
            "detail": f"deployed {deployed}",
            "nav": "maintenance/system",
            "why": (
                "This bot is running an older version of Evolve than "
                "what's currently deployed to the pod."
            ),
            "impact": (
                "Recently-fixed bugs or new behaviors may not be "
                "available on this bot until it's redeployed. Day-to-day "
                "operation usually still works."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → System",
                    "kind": "deep_link",
                    "nav": "maintenance/system",
                },
            ],
            "remediation_note": (
                "The fix is to redeploy this bot — either from the "
                "tile's Actions ⋯ menu (Redeploy) or by running "
                "``sudo evolve-admin deploy <bot>`` on the pod."
            ),
            "horizon": "ongoing",
            "digest_tier": "tile_only",
        })

    # ── unexpected_billing ────────────────────────────────────────────────
    # Sum across last 7d of metrics history.
    unexpected_total = 0
    start_7 = today - timedelta(days=7)
    end = today + timedelta(days=1)  # inclusive of today
    d = start_7
    while d < end:
        m = by_date.get(d)
        if m:
            unexpected_total += int(m.get("unexpected_billing_turns", 0) or 0)
        d += timedelta(days=1)
    if unexpected_total > 0:
        chips.append({
            "id": "unexpected_billing",
            "severity": "warn",
            "label": "unexpected billing",
            "detail": f"{unexpected_total} turn(s) in last 7d",
            "nav": "cost",
            "why": (
                "The OpenClaw plugin flagged turns as billed in an "
                "unexpected mode — typically an Anthropic MAX "
                "subscription no longer covering a turn, or a "
                "fallback dropping to an unintended billing path."
            ),
            "impact": (
                "Real per-turn API spend where the operator may have "
                "been expecting subscription-covered usage. Spend can "
                "be small or large depending on volume and tier."
            ),
            "remediations": [
                {
                    "label": "Open Usage page",
                    "kind": "deep_link",
                    "nav": "cost",
                },
            ],
            "remediation_note": (
                "The Usage page shows per-turn billing-mode detail; "
                "the affected turns are the ones marked with the "
                "unexpected-billing badge. Common causes: MAX auth "
                "lapsed (every turn bills at API rates now — see "
                "memory note), or the bot's primary tier is hitting a "
                "fallback chain. Review and reauth or adjust tier."
            ),
            "horizon": "7d",
            "digest_tier": "tile_only",
        })

    # ── high_correction ───────────────────────────────────────────────────
    total_turns = 0
    total_corrections = 0
    d = start_7
    while d < end:
        m = by_date.get(d)
        if m:
            total_turns += int(m.get("turn_count", 0) or 0)
            total_corrections += int(m.get("correction_count", 0) or 0)
        d += timedelta(days=1)
    if total_turns >= 20:  # don't flag on tiny samples
        rate = total_corrections / total_turns
        if rate > HIGH_CORRECTION_RATE:
            # NOTE: "correction" here is a coarse keyword match in the
            # plugin's TurnObserver (CORRECTION_PATTERNS in
            # packages/plugin/src/observer/TierClassifier.ts: "try again",
            # "that's wrong", "you misunderstood", etc.). It signals user
            # pushback, not a verified mistake. Label/detail reflect that
            # honestly — see follow-up task to replace the keyword proxy
            # with a smarter user-pushback signal.
            # First chip to carry the explainer fields specified in
            # docs/principle-alerts-explain-and-remediate.md — `why`,
            # `impact`, `remediations[]`. The label is the short form
            # ("pushback") because the popover carries the full
            # explanation; the chip itself doesn't need to fit a
            # sentence into a pill. No verified prescriptive remediation
            # exists yet for high pushback (it can stem from model
            # choice, system prompt, app behavior, or topic), so the
            # remediations are investigation deep-links + a
            # ``remediation_note`` explaining why we don't prescribe.
            chips.append({
                "id": "high_correction",
                "severity": "warn",
                "label": "pushback",
                "detail": (
                    f"{rate:.0%} of turns this week "
                    f"— typical <{HIGH_CORRECTION_RATE:.0%}"
                ),
                "why": (
                    "Users frequently corrected, retried, or rephrased "
                    "in this bot's recent conversations."
                ),
                "impact": (
                    "Real friction for end users — replies aren't "
                    "landing the first time, so they push back."
                ),
                "remediations": [
                    {
                        "label": "Open this bot's settings",
                        "kind": "deep_link",
                        # 3-level chipNav target — settings/bots/<bot_id>
                        # — drills to the new top-level Bots subtab and
                        # selects this bot's row via switchConfigBot()
                        # (see chipNav in web/index.html). Lands the
                        # operator directly on the cards they need to
                        # change (Customizations, Model, Compaction)
                        # rather than the generic Settings page.
                        "nav": f"settings/bots/{bot_id}",
                    },
                ],
                "remediation_note": (
                    "No verified prescriptive fix yet — pushback can "
                    "come from model choice, system prompt, app "
                    "behavior, or topic. Review recent conversations "
                    "and apps to understand the pattern before "
                    "changing config."
                ),
                "horizon": "7d",
                "digest_tier": "tile_only",
            })

    # ── scan_needed ───────────────────────────────────────────────────────
    # The user-facing operation for keeping a bot's app inventory current
    # is the application *scan* (Applications page → Run Scan), not the
    # internal test runner. Fire when the most recent scan is missing
    # (scan never run) OR older than the staleness threshold. Both
    # states call for the same fix — run a scan — so we use one chip
    # with detail text that distinguishes the two cases ("never scanned"
    # vs "last scanned Nd ago").
    #
    # Source of truth is the scanner's own status file at
    # ``/Users/<bot_user>/.openclaw/workspace/manifests/.scan-status.json``
    # — written by the scanner subprocess as the bot user, readable by
    # the admin server via macOS ACL. We also accept the legacy
    # ``{shared_dir}/applications/<bot>/.scan-status.json`` mirror as a
    # fallback (older pods, test fixtures), taking whichever path has
    # the fresher mtime. The mirror used to be the only source, written
    # by a stamping step in the admin server's scan endpoint — that
    # mirror layer was load-bearing but failed silently on OSError,
    # leaving the chip stuck with no log trail. Reading the bot-side
    # file directly removes that fragility.
    if apps_total > 0:
        candidates: list[Path] = []
        bot_side = _bot_scan_status_path(network, bot_id)
        if bot_side is not None:
            candidates.append(bot_side)
        candidates.append(shared_dir / "applications" / bot_id / ".scan-status.json")
        mtimes: list[float] = []
        for p in candidates:
            try:
                if p.exists():
                    mtimes.append(p.stat().st_mtime)
            except OSError:
                continue
        days_old: float | None = (
            (_time.time() - max(mtimes)) / 86400 if mtimes else None
        )
        if days_old is None or days_old > APPS_UNTESTED_DAYS:
            detail = (
                f"last scanned {int(days_old)}d ago"
                if days_old is not None
                else "never scanned"
            )
            chips.append({
                "id": "scan_needed",
                "severity": "warn",
                "label": "scan needed",
                "detail": detail,
                # ``apps`` is the canonical page id. ``capabilities``
                # was the legacy slug; pageRedirectRegistry resolves
                # it via nav() but chipNav does direct DOM lookup, so
                # legacy slugs in chip nav fields silently failed.
                # Use the canonical id directly.
                "nav": "apps",
                "why": (
                    "This bot's app inventory hasn't been scanned "
                    "recently. Evolve's view of which apps the bot "
                    "actually has installed may be stale."
                ),
                "impact": (
                    "New apps may not appear, removed apps may still "
                    "show, and the app audit can't run accurately "
                    "until the scan refreshes."
                ),
                "remediations": [
                    # Action button — start the scan directly. The
                    # scan_needed chip is named for this exact fix, so
                    # the action belongs front-and-center rather than
                    # one navigation step away.
                    {
                        "label": "↻ Run a scan now",
                        "kind": "action",
                        "action": "run_capability_scan",
                    },
                    {
                        "label": "Open Apps page",
                        "kind": "deep_link",
                        "nav": "apps",
                    },
                ],
                "remediation_note": (
                    "Scan takes about 30 seconds and refreshes the "
                    "bot's manifest. Watch progress on the Apps page."
                ),
                "horizon": "ongoing",
                "digest_tier": "tile_only",
            })

    # ── cost_spike ────────────────────────────────────────────────────────
    # 1d variant fires first so the more-specific same-day signal wins
    # when both match. Lower floor ($2) and higher multiplier (3×) keeps
    # quiet days from yelling — only a real same-day surge fires it.
    spike_fired = False
    if (
        cost_1d > COST_SPIKE_1D_FLOOR_USD
        and cost_prior_1d > 0
        and cost_1d > COST_SPIKE_1D_MULTIPLIER * cost_prior_1d
    ):
        chips.append({
            "id": "cost_spike",
            "severity": "warn",
            "label": "cost spike",
            "detail": f"${cost_1d:.2f} today vs ${cost_prior_1d:.2f} yesterday",
            "nav": "cost",
            "why": (
                "This bot spent meaningfully more today than yesterday "
                "(at least 3× the prior day, above the $2 floor)."
            ),
            "impact": (
                "If unexplained, the bot may be in a retry loop, on an "
                "unintended model tier, or running a misconfigured app "
                "or cron. Day-on-day spikes burn budget fastest."
            ),
            "remediations": [
                {
                    "label": "Open Usage page",
                    "kind": "deep_link",
                    "nav": "cost",
                },
            ],
            "remediation_note": (
                "The Usage page shows per-app and per-model cost "
                "breakdown for today. Check whether a single app or "
                "model is driving the spike, and whether a "
                "cost-reducing proposal is already in the queue."
            ),
            "horizon": "today",
            "digest_tier": "today_news",
        })
        spike_fired = True
    if (
        not spike_fired
        and cost_7d > COST_SPIKE_FLOOR_USD
        and cost_prior_7d > 0
        and cost_7d > COST_SPIKE_MULTIPLIER * cost_prior_7d
    ):
        chips.append({
            "id": "cost_spike",
            "severity": "warn",
            "label": "cost spike",
            "detail": f"${cost_7d:.2f} vs ${cost_prior_7d:.2f} prior 7d",
            "nav": "cost",
            "why": (
                "This bot's 7-day spend more than doubled compared to "
                "the prior 7 days (above the $5 floor)."
            ),
            "impact": (
                "A sustained higher run-rate. Could be genuine usage "
                "growth or a slow-burn retry / tier-fallback issue "
                "that's been happening for several days."
            ),
            "remediations": [
                {
                    "label": "Open Usage page",
                    "kind": "deep_link",
                    "nav": "cost",
                },
            ],
            "remediation_note": (
                "Compare the 7-day per-app breakdown to the prior "
                "period. Genuine usage growth doesn't need a fix; "
                "loop/fallback patterns usually show up as one app or "
                "one model dominating the increase."
            ),
            "horizon": "7d",
            "digest_tier": "tile_only",
        })

    # ── security_critical ────────────────────────────────────────────────
    # Sourced from the cached output of /api/security/audit. The admin
    # server overlays per-bot ``security_critical`` / ``security_warn``
    # counts onto bot_data before calling compute_tile_data. When the
    # audit hasn't run since admin start, the counts are absent and the
    # chip stays quiet.
    #
    # (The block below used to appear twice in this file — likely a
    # copy/paste artifact that would have appended the chip twice when
    # ``crit > 0``. Collapsed to a single block during the
    # principle-alerts-explain-and-remediate.md migration.)
    crit = int(bot_data.get("security_critical") or 0)
    if crit > 0:
        chips.append({
            "id": "security_critical",
            "severity": "critical",
            "label": f"{crit} critical security",
            "detail": "open Security audit for details",
            "nav": "security",   # frontend uses this to make the chip clickable
            "why": (
                f"The most recent security audit found {crit} critical "
                "finding(s) on this bot — typically an exposed "
                "credential, a permissive exec policy, or a "
                "misconfigured access control."
            ),
            "impact": (
                "Critical findings represent real security exposure. "
                "Each finding has its own severity and remediation "
                "guidance on the Security page."
            ),
            "remediations": [
                {
                    "label": "Open Security page",
                    "kind": "deep_link",
                    "nav": "security",
                },
            ],
            "remediation_note": (
                "The Security page lists each finding with its category "
                "(credential leak, exec-policy drift, ACL drift, etc.) "
                "and the recommended fix. Findings stay open until the "
                "underlying condition clears on the next audit pass."
            ),
            "horizon": "ongoing",
            "digest_tier": "critical_now",
        })

    # ── pairing_needed ───────────────────────────────────────────────────
    # Lights when the bot has a configured messaging channel but no
    # operator is paired into it (allowFrom is empty), or when the
    # bot's primary user is recorded but their channel ID hasn't
    # been approved into allowFrom yet. Replaces the install
    # wizard's broken "End-to-end echo" Check 4 — pairing is its
    # own beat now, with a dedicated modal.
    #
    # Failures are non-fatal: a missing pairing_chip module or a
    # vanished sudo grant should not break the status fetch. The
    # tile just renders without the pairing chip in that case.
    try:
        from pairing_chip import compute_pairing_chips
        chips.extend(compute_pairing_chips(network, bot_id))
    except Exception:  # noqa: BLE001
        pass

    return chips


# ─────────────────────────────────────────────────────────────────────────────
# Live "today" overlay
# ─────────────────────────────────────────────────────────────────────────────
#
# The 7d/28d windows are correctly anchored on the most-recent rolled-up
# metrics day (measure.py writes each day's aggregate the morning after).
# The 1d window can't be — anchoring on "yesterday" means "Today" silently
# shows yesterday's totals while the Usage Summary card shows live ones.
# This helper sources today + yesterday from the same turn JSONL the
# Usage page reads, so the tile and the summary card never disagree.


# Mtime-based cache for the live overlay. Turn JSONL is append-only, so
# (bot_id, today's-file-mtime) is a perfect cache-bust signal — same
# mtime → same content → same overlay. Without this every status
# refresh re-reads up to 2 days × N bots of JSONL.
_LIVE_OVERLAY_CACHE: dict[tuple[str, str, float], dict] = {}
_LIVE_OVERLAY_CACHE_MAX = 64

# Path A cache: per-date sums computed from raw JSONL for the full
# 7d/28d window. Keyed on bot + today + days + today's JSONL mtime so
# the cache busts when new turns land but older immutable days stay hot.
_LIVE_WINDOW_CACHE: dict[tuple[str, str, int, float], dict] = {}
_LIVE_WINDOW_CACHE_MAX = 32


def _today_jsonl_mtime(bot_id: str, today: date) -> float:
    """Return today's turn-JSONL mtime for cache-busting (0.0 if absent)."""
    fname = f"turns-{today.isoformat()}.jsonl"
    try:
        from usage_analytics import _find_turns_dirs  # type: ignore[import]
        for d in _find_turns_dirs(bot_id):
            p = Path(d) / fname
            try:
                return p.stat().st_mtime
            except OSError:
                continue
    except Exception:
        pass
    return 0.0


def _live_today_overlay(bot_id: str, today: date) -> dict:
    """Return a dict with today + yesterday cost/turn/session counts.

    ``ok=True`` means "live JSONL was successfully consulted" — distinct
    from "no spend happened today." The caller uses ``ok`` to decide
    whether to fall back to the lagged metrics aggregate; setting ok=True
    when discovery actually failed would reintroduce the silent-zero
    failure mode this whole PR is trying to eliminate. We therefore set
    ok=True only when at least one of the candidate turn-dir entries was
    physically locatable on disk, not just because ``load_turns`` returned
    cleanly (which it does even for an unreadable bot — returning ``[]``).
    """
    cache_key = (bot_id, today.isoformat(), _today_jsonl_mtime(bot_id, today))
    cached = _LIVE_OVERLAY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    out = {
        "ok": False,
        "cost_today": 0.0, "cost_yesterday": 0.0,
        "turns_today": 0, "turns_yesterday": 0,
        "sessions_today": 0, "sessions_yesterday": 0,
    }
    try:
        from usage_analytics import (
            load_turns,
            _estimate_turn_cost,
            _find_turns_dirs,
        )  # type: ignore[import]
    except Exception:
        return out  # ok stays False — caller falls back to lagged aggregate

    # Verify at least one candidate turn dir exists. If none do, we have
    # no way to tell "bot is idle" from "I can't see this bot's data" —
    # leave ok=False so the caller falls back to the lagged aggregate
    # (which will at least show non-zero historical numbers for an
    # established bot).
    fname_today = f"turns-{today.isoformat()}.jsonl"
    fname_yest = f"turns-{(today - timedelta(days=1)).isoformat()}.jsonl"
    any_dir_readable = False
    any_file_present = False
    try:
        for d in _find_turns_dirs(bot_id):
            try:
                if Path(d).is_dir():
                    any_dir_readable = True
                    if (Path(d) / fname_today).exists() or (Path(d) / fname_yest).exists():
                        any_file_present = True
                        break
            except OSError:
                continue
    except Exception:
        pass

    try:
        turns = load_turns(bot_id, days=2)
    except Exception:
        return out

    today_iso = today.isoformat()
    yest_iso = (today - timedelta(days=1)).isoformat()
    sids_today: set[str] = set()
    sids_yest: set[str] = set()
    for t in turns:
        d = (t.get("ts") or "")[:10]
        recorded = float(t.get("cost") or 0)
        cost = recorded if recorded else _estimate_turn_cost(t)
        sid = t.get("session_id") or ""
        if d == today_iso:
            out["cost_today"] += cost
            out["turns_today"] += 1
            if sid:
                sids_today.add(sid)
        elif d == yest_iso:
            out["cost_yesterday"] += cost
            out["turns_yesterday"] += 1
            if sid:
                sids_yest.add(sid)
    out["cost_today"] = round(out["cost_today"], 6)
    out["cost_yesterday"] = round(out["cost_yesterday"], 6)
    out["sessions_today"] = len(sids_today)
    out["sessions_yesterday"] = len(sids_yest)
    # ok=True iff we actually saw a turn dir or a turn file — otherwise
    # we have no basis for saying "this is live data."
    out["ok"] = any_dir_readable and (any_file_present or bool(turns))

    if len(_LIVE_OVERLAY_CACHE) >= _LIVE_OVERLAY_CACHE_MAX:
        _LIVE_OVERLAY_CACHE.clear()
    _LIVE_OVERLAY_CACHE[cache_key] = out
    return out


def _live_window_costs(bot_id: str, today: date, days: int) -> dict:
    """Per-date cost / turn / session-id data from raw JSONL.

    Returns::

        {
            "ok": bool,                          # JSONL was readable
            "per_date": {                        # one entry per non-empty day
                "YYYY-MM-DD": {"cost": float, "turns": int},
                ...
            },
            "session_ids_per_date": {            # session_id sets per day
                "YYYY-MM-DD": set[str],
                ...
            },
        }

    Used by ``compute_tile_data`` to source 7d/28d cost/turn/session
    counts directly from the same JSONL the Usage Summary card reads.
    Replaces the ``by_date`` aggregator-based reads, which silently
    undercount by ~$5/day on average (2026-05-20 incident follow-up).

    Cached on ``(bot_id, today_iso, days, today_jsonl_mtime)``. Today's
    JSONL is the only file that changes during a status-refresh cycle;
    older files are immutable so the same key catches subsequent calls
    until today's mtime advances.
    """
    cache_key = (bot_id, today.isoformat(), days, _today_jsonl_mtime(bot_id, today))
    cached = _LIVE_WINDOW_CACHE.get(cache_key)
    if cached is not None:
        return cached

    out: dict = {"ok": False, "per_date": {}, "session_ids_per_date": {}}
    try:
        from usage_analytics import (  # type: ignore[import]
            load_turns,
            _estimate_turn_cost,
            _find_turns_dirs,
        )
    except Exception:
        return out

    # Mirror the _live_today_overlay presence-check: only claim ok=True
    # when we can actually see a turn dir AND either a file in it or
    # non-empty load_turns output. Otherwise downstream falls back to
    # the by_date aggregate (which is the right behavior for tests
    # that don't write JSONL, and for bots without a turn dir yet).
    any_dir_readable = False
    any_file_present = False
    fname_today = f"turns-{today.isoformat()}.jsonl"
    fname_yest = f"turns-{(today - timedelta(days=1)).isoformat()}.jsonl"
    try:
        for dpath in _find_turns_dirs(bot_id):
            try:
                p = Path(dpath)
                if p.is_dir():
                    any_dir_readable = True
                    if (p / fname_today).exists() or (p / fname_yest).exists():
                        any_file_present = True
                        break
            except OSError:
                continue
    except Exception:
        pass

    try:
        turns = load_turns(bot_id, days=days)
    except Exception:
        return out

    for t in turns:
        ts = t.get("ts") or ""
        if not isinstance(ts, str) or len(ts) < 10:
            continue
        d = ts[:10]
        recorded = float(t.get("cost") or 0)
        cost = recorded if recorded else _estimate_turn_cost(t)
        slot = out["per_date"].setdefault(d, {"cost": 0.0, "turns": 0})
        slot["cost"] += cost
        slot["turns"] += 1
        sid = t.get("session_id") or ""
        if sid:
            out["session_ids_per_date"].setdefault(d, set()).add(sid)

    # ok=True iff we actually saw a turn dir or a turn file — otherwise
    # we have no basis for saying "this is live data" vs "bot is idle."
    out["ok"] = any_dir_readable and (any_file_present or bool(turns))

    if len(_LIVE_WINDOW_CACHE) >= _LIVE_WINDOW_CACHE_MAX:
        _LIVE_WINDOW_CACHE.clear()
    _LIVE_WINDOW_CACHE[cache_key] = out
    return out


def _live_window_sum(live_window: dict, start: date, end: date, field: str) -> float:
    """Sum ``field`` (``cost`` | ``turns``) over [start, end) from per_date."""
    total = 0.0
    d = start
    while d < end:
        slot = live_window.get("per_date", {}).get(d.isoformat())
        if slot is not None:
            total += slot.get(field, 0)
        d += timedelta(days=1)
    return total


def _live_window_sessions(live_window: dict, start: date, end: date) -> int:
    """Unique session_ids touching any day in [start, end)."""
    sids: set[str] = set()
    d = start
    while d < end:
        day_sids = live_window.get("session_ids_per_date", {}).get(d.isoformat())
        if day_sids:
            sids.update(day_sids)
        d += timedelta(days=1)
    return len(sids)


# ─────────────────────────────────────────────────────────────────────────────
# Value block + underused chip (spec docs/spec-value-baseline-2026-06-10.md §7.1)
# ─────────────────────────────────────────────────────────────────────────────

# The §5.2 per-bot rollup entry minus internals: ``age_days`` (predicate
# gate input) and ``usage_breadth_28d`` (Value-view context) stay out of
# the tile block.
_TILE_VALUE_FIELDS = (
    "utilization_state",
    "state_reason",
    "active_human_days_7d",
    "active_human_days_28d",
    "proactive_runs_7d",
    "proactive_runs_28d",
    "app_coverage_28d",
    "value_trend_28d",
)


def _underused_chip(entry: dict) -> dict:
    """The ``underused`` health chip (spec §7.1, copy per §7.3).

    Renders only when the latest rollup's ``utilization_state`` says
    ``underused`` — the active/underused/unmeasurable predicate lives in
    ``value_baseline._utilization_state`` and is NOT re-implemented here.
    Chip severity vocabulary is warn|critical only, so the Signal's
    ``info`` tier maps to ``warn``; ``tile_only`` keeps this standing
    state out of the daily digest.
    """
    w28 = entry.get("active_human_days_28d") or {}
    measurable = w28.get("measurable_days")
    window = w28.get("window_days") or 28
    records_note = (
        f" ({measurable} of the last {window} days have usage records)"
        if isinstance(measurable, int)
        else ""
    )
    return {
        "id": "underused",
        "severity": "warn",
        "label": "Idle 4 weeks",
        "detail": (
            "No one has used this bot recently and it isn't delivering "
            "anything on a schedule."
        ),
        "horizon": "ongoing",
        "digest_tier": "tile_only",
        "why": (
            "Nobody has used this bot in the last 4 weeks, and it has no "
            f"scheduled jobs delivering anything{records_note}."
        ),
        "impact": (
            "If it's waiting on setup, connect it to a chat channel so "
            "people can reach it. If it's not needed, you can remove it. "
            "If it's deliberately dormant, dismiss the matching alert and "
            "it won't come up again."
        ),
    }


def _value_block_and_chip(
    shared_dir: Path, bot_id: str
) -> tuple[dict | None, dict | None]:
    """(tile ``value`` block, ``underused`` chip or None) for one bot.

    Reads the latest nightly value rollup. ``None`` block when no rollup
    exists yet (fresh pod / measure job hasn't run) or the bot isn't in
    it — absence renders as nothing, never as fake zeros.
    """
    try:
        # Lazy import: value_baseline imports tile helpers at module
        # level, so a top-level import here would be a cycle.
        from value_baseline import load_latest_rollup, rollup_is_stale
    except ImportError:
        return None, None
    rollup = load_latest_rollup(shared_dir)
    if not rollup:
        return None, None
    bots = rollup.get("bots")
    entry = bots.get(bot_id) if isinstance(bots, dict) else None
    if not isinstance(entry, dict):
        return None, None
    block = {k: entry.get(k) for k in _TILE_VALUE_FIELDS}
    # Spec §5.3: surfaces show the rollup's anchor date and flag >48h.
    block["as_of"] = rollup.get("anchor_date")
    block["stale"] = rollup_is_stale(rollup)
    chip = (
        _underused_chip(entry)
        if entry.get("utilization_state") == "underused"
        else None
    )
    return block, chip


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def compute_tile_data(
    shared_dir: Path,
    bot_id: str,
    bot_data: dict,
    network: dict | None = None,
    today: date | None = None,
) -> dict:
    """Compute the activity / cost / apps / chip block for one bot tile.

    Works for both member bots and the primary bot — the primary emits the
    same daily metrics file shape (turn_count, session_count, app_usage, ...)
    so the activity/cost/apps blocks render identically. Primary-specific
    chips (daemon-fleet, ACL drift, repo-puller lag, disk pressure) are
    appended when the bot's role is "primary".

    ``bot_data`` is the per-bot dict produced by ``status.network_status``,
    augmented with sync state and live_data fields. We pass it in rather
    than re-deriving heartbeat / version data, to keep this module a pure
    metrics aggregator (no daemon probes outside the admin chip helpers).
    """
    today = today or date.today()
    shared_dir = Path(shared_dir)

    # Load 60 days so we have current + prior windows for both 7d and 28d.
    by_date = _load_metrics_by_date(shared_dir, bot_id, days=60, today=today)

    # Anchor the windows on the most-recent day that has a metrics file
    # rather than on `today`. The measure.py cron writes each day's
    # aggregate at 1am the following morning, so during the day "today"
    # has no file yet and a today-anchored 7d window would be 6 real days
    # plus a zero-padding day. Anchoring on the latest day with data
    # keeps "1d" meaningful and stops the trailing empty day from
    # diluting the 7d/28d averages. Falls back to today when no metrics
    # exist (fresh pod / never-active bot) so the tiles still render.
    anchor = max(by_date.keys()) if by_date else today
    end = anchor + timedelta(days=1)  # window is inclusive of anchor day
    cur_1_start = end - timedelta(days=1)
    prior_1_start = cur_1_start - timedelta(days=1)
    cur_7_start = end - timedelta(days=7)
    prior_7_start = cur_7_start - timedelta(days=7)
    cur_28_start = end - timedelta(days=28)
    prior_28_start = cur_28_start - timedelta(days=28)

    turns_1d_lagged = int(_window_sum(by_date, cur_1_start, end, "turn_count"))
    turns_prior_1d_lagged = int(_window_sum(by_date, prior_1_start, cur_1_start, "turn_count"))
    turns_7d = int(_window_sum(by_date, cur_7_start, end, "turn_count"))
    turns_prior_7d = int(_window_sum(by_date, prior_7_start, cur_7_start, "turn_count"))
    turns_28d = int(_window_sum(by_date, cur_28_start, end, "turn_count"))
    turns_prior_28d = int(_window_sum(by_date, prior_28_start, cur_28_start, "turn_count"))

    sess_1d_lagged = int(_window_sum(by_date, cur_1_start, end, "session_count"))
    sess_prior_1d_lagged = int(_window_sum(by_date, prior_1_start, cur_1_start, "session_count"))
    sess_7d = int(_window_sum(by_date, cur_7_start, end, "session_count"))
    sess_prior_7d = int(_window_sum(by_date, prior_7_start, cur_7_start, "session_count"))
    sess_28d = int(_window_sum(by_date, cur_28_start, end, "session_count"))
    sess_prior_28d = int(_window_sum(by_date, prior_28_start, cur_28_start, "session_count"))

    cost_1d_lagged = float(_window_sum(by_date, cur_1_start, end, "total_cost_estimated"))
    cost_prior_1d_lagged = float(_window_sum(by_date, prior_1_start, cur_1_start, "total_cost_estimated"))
    cost_7d = float(_window_sum(by_date, cur_7_start, end, "total_cost_estimated"))
    cost_prior_7d = float(_window_sum(by_date, prior_7_start, cur_7_start, "total_cost_estimated"))
    cost_28d = float(_window_sum(by_date, cur_28_start, end, "total_cost_estimated"))
    cost_prior_28d = float(_window_sum(by_date, prior_28_start, cur_28_start, "total_cost_estimated"))

    # ── Live 1d (today + yesterday) overlay ───────────────────────────────
    # The 1d "Today" view used to silently show yesterday's aggregate
    # while the Usage Summary card showed live totals — that's exactly
    # the discrepancy that let the 2026-05-20 spike pass unnoticed
    # (tile said $6.44, summary said $33.67). Overlay live cost/turn/
    # session figures sourced from the same JSONL the Usage page reads.
    live = _live_today_overlay(bot_id, today)
    cost_1d = live["cost_today"]
    cost_prior_1d = live["cost_yesterday"]
    turns_1d = live["turns_today"]
    turns_prior_1d = live["turns_yesterday"]
    sess_1d = live["sessions_today"]
    sess_prior_1d = live["sessions_yesterday"]
    live_overlay_ok = live["ok"]
    # Fall back to the lagged aggregate if the live overlay failed
    # entirely (no turn JSONL, import error). Better to under-report
    # one window than to silently zero out the 1d tile during a JSONL
    # outage.
    if not live_overlay_ok:
        cost_1d = cost_1d_lagged
        cost_prior_1d = cost_prior_1d_lagged
        turns_1d = turns_1d_lagged
        turns_prior_1d = turns_prior_1d_lagged
        sess_1d = sess_1d_lagged
        sess_prior_1d = sess_prior_1d_lagged

    # ── Live 7d / 28d full-window read (Path A from incident discussion) ──
    # The by_date-based sums above use ``metrics/{date}/{bot}.json`` files
    # written by the daily aggregator cron. Those files silently
    # undercount vs raw turn JSONL by ~$5/day on average — the 7d gap on
    # 2026-05-21 was: tile $21.13 vs Summary $88.19 ($67 delta). PR
    # #1409 closed the today+yesterday portion of the gap by overlaying
    # live JSONL on top of the by_date sums. This block goes the rest
    # of the way: read the entire 56-day window from raw JSONL (same
    # source as the Usage Summary card) and replace cost / turn /
    # session counts for 1d / 7d / 28d windows. The by_date data is
    # still used for apps_used and trigger-kind classification, where
    # no live alternative exists.
    #
    # When JSONL is unreadable (sudo grant missing, no turn dir, etc.)
    # fall back to the by_date sums populated above. The Usage Summary
    # card has the same fallback shape.
    live_today_7d = False
    live_today_28d = False
    live_window = _live_window_costs(bot_id, today, days=56)
    if live_window["ok"]:
        live_end = today + timedelta(days=1)
        cur_1_start_live = live_end - timedelta(days=1)
        prior_1_start_live = cur_1_start_live - timedelta(days=1)
        cur_7_start_live = live_end - timedelta(days=7)
        prior_7_start_live = cur_7_start_live - timedelta(days=7)
        cur_28_start_live = live_end - timedelta(days=28)
        prior_28_start_live = cur_28_start_live - timedelta(days=28)

        cost_7d = _live_window_sum(live_window, cur_7_start_live, live_end, "cost")
        cost_prior_7d = _live_window_sum(live_window, prior_7_start_live, cur_7_start_live, "cost")
        cost_28d = _live_window_sum(live_window, cur_28_start_live, live_end, "cost")
        cost_prior_28d = _live_window_sum(live_window, prior_28_start_live, cur_28_start_live, "cost")

        turns_7d = int(_live_window_sum(live_window, cur_7_start_live, live_end, "turns"))
        turns_prior_7d = int(_live_window_sum(live_window, prior_7_start_live, cur_7_start_live, "turns"))
        turns_28d = int(_live_window_sum(live_window, cur_28_start_live, live_end, "turns"))
        turns_prior_28d = int(_live_window_sum(live_window, prior_28_start_live, cur_28_start_live, "turns"))

        sess_7d = _live_window_sessions(live_window, cur_7_start_live, live_end)
        sess_prior_7d = _live_window_sessions(live_window, prior_7_start_live, cur_7_start_live)
        sess_28d = _live_window_sessions(live_window, cur_28_start_live, live_end)
        sess_prior_28d = _live_window_sessions(live_window, prior_28_start_live, cur_28_start_live)

        live_today_7d = True
        live_today_28d = True

    human_7d, sched_7d, bg_7d = _classify_split(shared_dir, bot_id, cur_7_start, end)
    human_28d, sched_28d, bg_28d = _classify_split(shared_dir, bot_id, cur_28_start, end)
    total_7d = human_7d + sched_7d + bg_7d
    total_28d = human_28d + sched_28d + bg_28d

    # apps.total spans both manifests AND apps that actually ran in the
    # widest displayed window — otherwise an app that ran but lacks a
    # manifest (legacy / unscanned) would push used > total.
    #
    # network is threaded through so _list_app_manifests can read from
    # the canonical bot-side manifests dir
    # (/Users/<bot_user>/.openclaw/workspace/manifests) rather than the
    # stale shared-side mirror. Pre-#1176 the shared-side was the
    # source of truth; the tile kept reading from it after the
    # promotion and silently returned 0 for many bots, suppressing the
    # apps chip entirely.
    manifest_ids = set(_list_app_manifests(shared_dir, bot_id, network))
    used_7d_ids = _apps_used_in_window(by_date, cur_7_start, end)
    used_28d_ids = _apps_used_in_window(by_date, cur_28_start, end)
    total_ids = manifest_ids | used_28d_ids

    chips = _compute_member_chips(
        shared_dir=shared_dir,
        bot_id=bot_id,
        bot_data=bot_data,
        by_date=by_date,
        today=today,
        cost_7d=cost_7d,
        cost_prior_7d=cost_prior_7d,
        apps_total=len(manifest_ids),  # chip rule cares about installed apps
        network=network,
        cost_1d=cost_1d,
        cost_prior_1d=cost_prior_1d,
    )

    if bot_data.get("role") == "primary":
        chips.extend(_compute_admin_chips(shared_dir, bot_data, network or {}, today))

    value_block, underused_chip = _value_block_and_chip(shared_dir, bot_id)
    if underused_chip is not None:
        chips.append(underused_chip)

    return {
        "activity": {
            "turns_1d": turns_1d,
            "turns_prior_1d": turns_prior_1d,
            "turns_7d": turns_7d,
            "turns_prior_7d": turns_prior_7d,
            "turns_28d": turns_28d,
            "turns_prior_28d": turns_prior_28d,
            "sessions_1d": sess_1d,
            "sessions_prior_1d": sess_prior_1d,
            "sessions_7d": sess_7d,
            "sessions_prior_7d": sess_prior_7d,
            "sessions_28d": sess_28d,
            "sessions_prior_28d": sess_prior_28d,
            "human_pct_7d":      _safe_pct(human_7d, total_7d),
            "scheduled_pct_7d":  _safe_pct(sched_7d, total_7d),
            "background_pct_7d": _safe_pct(bg_7d, total_7d),
            "human_pct_28d":      _safe_pct(human_28d, total_28d),
            "scheduled_pct_28d":  _safe_pct(sched_28d, total_28d),
            "background_pct_28d": _safe_pct(bg_28d, total_28d),
        },
        "cost": {
            "usd_1d": round(cost_1d, 2),
            "usd_prior_1d": round(cost_prior_1d, 2),
            "usd_7d": round(cost_7d, 2),
            "usd_prior_7d": round(cost_prior_7d, 2),
            "usd_28d": round(cost_28d, 2),
            "usd_prior_28d": round(cost_prior_28d, 2),
            # True when usd_1d / usd_prior_1d came from live turn JSONL
            # (matches the Usage Summary card). False when it fell back
            # to the lagged metrics aggregate — the UI shows "as of
            # <anchor>" in that case so the operator knows why the
            # number might lag the summary card.
            "live_today": bool(live_overlay_ok),
            # True when usd_7d / usd_28d had live cost added for days
            # that hadn't yet been rolled up into a per-day aggregate
            # (typically today + yesterday). False when no overlay was
            # applied — either because the aggregator is caught up or
            # because the live overlay couldn't read the JSONL.
            "live_today_7d": bool(live_today_7d),
            "live_today_28d": bool(live_today_28d),
        },
        "anchor_date": str(anchor),
        "apps": {
            "total": len(total_ids),
            "used_7d": len(used_7d_ids),
            "used_28d": len(used_28d_ids),
        },
        # Latest value-rollup entry for this bot (spec §7.1), or None when
        # no rollup exists yet. The underused chip above is derived from
        # the same entry's utilization_state.
        "value": value_block,
        "health_chips": chips,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Evolve admin chips
# ─────────────────────────────────────────────────────────────────────────────
#
# The evolve admin tile uses the same activity/cost/apps blocks as member bots
# (evolve emits its own daily metrics file) — these chip rules are appended on
# top to surface daemon-fleet, ACL, and disk concerns that don't apply to
# member bots.


def _compute_admin_chips(
    shared_dir: Path, bot_data: dict, network: dict, today: date
) -> list[dict]:
    """Health chips for the evolve admin tile."""
    chips: list[dict] = []

    # ── repo_puller_stale ─────────────────────────────────────────────────
    # Use the deploy checkout's .git/FETCH_HEAD mtime as a liveness signal
    # for the repo-puller daemon. FETCH_HEAD is updated on every `git
    # fetch`, including when the fetch finds no new commits, so it tracks
    # whether the puller is actually running rather than whether new work
    # has landed. (Earlier rule looked for a .puller-heartbeat file the
    # puller never wrote — a chip-firing-for-the-wrong-reason bug.)
    # Platform-keyed deploy checkout: /Users/Shared/evolve-repo (macOS) or
    # /var/lib/evolve/repo (Linux). Hardcoding the macOS path made this fire a
    # false "no deploy checkout found" on Linux pods (the real checkout lives
    # elsewhere) AND blinded the liveness branch to a genuinely stale Linux
    # puller. Mirror repo_puller.DEFAULT_REPO (also _PROFILE.deploy_checkout_default).
    from platform_profile import get_profile
    deploy_repo = Path(get_profile().deploy_checkout_default)
    fetch_head = deploy_repo / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        try:
            age_min = (_time.time() - fetch_head.stat().st_mtime) / 60
            if age_min > 20:
                chips.append({
                    "id": "repo_puller_stale",
                    "severity": "warn",
                    "label": "repo-puller stale",
                    "detail": f"last fetch {int(age_min)}m ago",
                    "nav": "maintenance/system",
                    "why": (
                        "The repo-puller daemon should run ``git "
                        "fetch`` every 15 minutes, but the deploy "
                        "checkout's FETCH_HEAD hasn't updated in over "
                        "20 minutes."
                    ),
                    "impact": (
                        "Code merged to origin/main isn't reaching the "
                        "pod. Bots keep running whatever they had at "
                        "the last successful pull until the puller "
                        "catches up."
                    ),
                    "remediations": [
                        {
                            "label": "Open Maintenance → Infra Jobs",
                            "kind": "deep_link",
                            "nav": "maintenance/infrajobs",
                        },
                    ],
                    "remediation_note": (
                        "Infra Jobs page shows each daemon's status and "
                        "last-run time. Restart from there, or run "
                        "``sudo /bin/launchctl kickstart -k "
                        "system/ai.evolve.evolve.repo-puller``. "
                        "Persistent staleness usually means a network "
                        "issue or expired git credentials."
                    ),
                    "horizon": "ongoing",
                    "digest_tier": "tile_only",
                })
        except OSError:
            pass
    # No deploy checkout at all → puller can't run. Only flag if we know
    # the daemon is supposed to be installed (install.json exists).
    elif (shared_dir / "install.json").exists():
        chips.append({
            "id": "repo_puller_stale",
            "severity": "warn",
            "label": "repo-puller stale",
            "detail": "no deploy checkout found",
            "nav": "maintenance/system",
            "why": (
                f"Evolve expects a deploy checkout at "
                f"``{deploy_repo}``, but its ``.git`` "
                f"directory isn't there."
            ),
            "impact": (
                "The repo-puller daemon can't run — the pod can't "
                "auto-update, and ``sudo evolve-admin deploy`` may "
                "also fail until the checkout is recreated."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → System",
                    "kind": "deep_link",
                    "nav": "maintenance/system",
                },
            ],
            "remediation_note": (
                f"Recreate the checkout with ``git clone "
                f"<repo-url> {deploy_repo}`` as the evolve "
                f"service user, then restart the repo-puller daemon."
            ),
            "horizon": "ongoing",
            "digest_tier": "tile_only",
        })

    # ── infra_daemon_down ─────────────────────────────────────────────────
    # heal / verify run as LaunchDaemons. Best-effort liveness check via
    # `launchctl list` parsing. We surface a single chip if any expected
    # daemon is missing.
    missing = _check_infra_daemons()
    if missing:
        chips.append({
            "id": "infra_daemon_down",
            "severity": "critical",
            "label": "infra daemon down",
            "detail": ", ".join(missing),
            "nav": "maintenance/infrajobs",
            "why": (
                "One or more of the LaunchDaemons that keep the pod "
                "running (heal, verify, repo-puller) isn't loaded."
            ),
            "impact": (
                "Whichever daemon is missing isn't doing its job — "
                "gateway health probes (heal), test/audit dispatch "
                "(verify), or auto-pulls (repo-puller). Different "
                "downstream effects depending on which is down."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → Infra Jobs",
                    "kind": "deep_link",
                    "nav": "maintenance/infrajobs",
                },
            ],
            "remediation_note": (
                "Infra Jobs page shows each daemon's load status and "
                "last-run time. To reinstall the full set, run "
                "``sudo evolve-admin install-infra-jobs`` on the pod."
            ),
            "horizon": "ongoing",
            "digest_tier": "critical_now",
        })

    # ── acl_drift ─────────────────────────────────────────────────────────
    # Walk each member bot's .openclaw/ and confirm the evolve user has
    # an ACL read entry. Cheap (one stat per bot).
    drift = _check_acl_drift(network)
    if drift:
        chips.append({
            "id": "acl_drift",
            "severity": "warn",
            "label": "ACL drift",
            "detail": f"{len(drift)} bot(s): {', '.join(drift[:3])}",
            "nav": "maintenance/system",
            "why": (
                "The evolve user lost its macOS ACL read access to one "
                "or more bots' ``.openclaw/`` directories. This grant "
                "is normally installed by ``deploy.py``."
            ),
            "impact": (
                "Evolve can't read those bots' config files directly. "
                "Pages and audits that depend on the read fall back to "
                "slower sudo-based paths, and some may fail outright."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → System",
                    "kind": "deep_link",
                    "nav": "maintenance/system",
                },
            ],
            "remediation_note": (
                "Run ``sudo evolve-admin repair-acls <bot>`` (or use "
                "the ``action.bot.repair_acls`` tool from evo) to "
                "reinstall the grant. It's also reapplied automatically "
                "on the next deploy of the affected bot(s)."
            ),
            "horizon": "ongoing",
            "digest_tier": "tile_only",
        })

    # ── disk_high ─────────────────────────────────────────────────────────
    pct = _shared_disk_pct(shared_dir)
    if pct is not None and pct >= 90:
        chips.append({
            "id": "disk_high",
            "severity": "warn" if pct < 95 else "critical",
            "label": "disk high",
            "detail": f"{pct}% used",
            "nav": "maintenance/system",
            "why": (
                "The shared volume that holds Evolve's metrics, "
                "signals, proposals, and bot transcripts is filling up."
            ),
            "impact": (
                "When it fills, bots will fail to write turn logs, the "
                "signal store will reject new signals, and backups may "
                "fail. Warn at 90% used; critical at 95%."
            ),
            "remediations": [
                {
                    "label": "Open Maintenance → System",
                    "kind": "deep_link",
                    "nav": "maintenance/system",
                },
            ],
            "remediation_note": (
                "Most growth comes from old turn JSONLs and archived "
                "proposals/signals. Retention pruning is automatic but "
                "can be triggered manually with ``python3 -m "
                "signals.retention --shared-dir /Users/Shared/evolve``. "
                "Review ``metrics/`` and ``proposals/archived/`` for "
                "stale data if pruning alone doesn't clear enough."
            ),
            "horizon": "ongoing",
            "digest_tier": "critical_now" if pct >= 95 else "tile_only",
        })

    return chips


# Scheduler-seam probe handle for _check_infra_daemons. A dedicated adapter
# instance, not get_scheduler()'s default: these probes run during admin-UI
# tile rendering, where the historical 5s per-probe timeout keeps a wedged
# launchctl from hanging page loads (the seam default is 30s). Tests
# monkeypatch this with LaunchdScheduler(runner=<fake>, ...).
#
# Portability (bite 3): the per-label rc=113 / "Could not find service" tri-
# state is launchd-verbatim raw() — status() folds every non-zero rc into
# managed=False, which would turn a sudo escalation gap into a false chip, so
# there is no clean Protocol verb to swap to (a systemd job-visibility verb is
# a GA follow-up, not this bite). On a Linux pod _check_infra_daemons gates OUT
# and returns [] — the "infra daemon down" chip simply doesn't render (the same
# silent-skip outcome the pre-seam code produced on a non-rc-113 result, but
# now intentional and crash-free, rather than a launchctl-not-found exception
# swallowed per-label). On macOS the probe routes through get_launchd_scheduler
# (the FAIL-FAST launchd-typed accessor — raises loudly on a non-launchd
# adapter, impossible behind the macOS gate).
_infra_probe_scheduler = None


def _check_infra_daemons() -> list[str]:
    """Return label-stems of expected daemons that are NOT loaded.

    Probes ``sudo /bin/launchctl list <label>`` per daemon through the
    Scheduler seam (rc=0 → loaded, rc=113 → not found). The no-sudo bulk
    ``launchctl list`` only shows the current user's LaunchAgents on macOS —
    it cannot see system-domain LaunchDaemons, which is what these are. The
    sudoers grant ``evolve ALL=(root) NOPASSWD: /bin/launchctl list *``
    covers this call.

    raw() escape hatch (counted S2 debt), not status(): the quiet-on-doubt
    rule needs the rc-113 / "Could not find service" tri-state — status()
    folds every non-zero rc into managed=False, which would turn a sudo
    escalation gap into a false "infra daemon down" critical chip.

    Linux: the launchd-only probe has no systemd analogue yet, so this gates
    OUT and returns ``[]`` — the chip doesn't render (no false-clean, no
    crash). The infra daemons exist as systemd units on a Linux pod; a
    systemd-aware presence check is the GA follow-up.
    """
    from platform_profile import get_profile

    if get_profile().name != "macos":
        return []

    from runtime.scheduler import LaunchdScheduler, get_launchd_scheduler

    global _infra_probe_scheduler
    if _infra_probe_scheduler is None:
        # FAIL-FAST launchd-typed accessor: raises on a non-launchd adapter
        # (impossible behind the macOS gate above). Reuse a test-injected
        # fake runner so no real launchctl is reached; otherwise build the
        # historical 5s adapter (sudo-default — the bulk list needs root to
        # see system-domain daemons).
        base = get_launchd_scheduler()
        if base._custom_runner:
            _infra_probe_scheduler = LaunchdScheduler(
                runner=base._runner, timeout=5.0,
            )
        else:
            _infra_probe_scheduler = LaunchdScheduler(timeout=5.0)

    expected = [
        "ai.evolve.evolve.repo-puller",
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.verify",
    ]
    missing = []
    for label in expected:
        try:
            rc, _out, err = _infra_probe_scheduler.raw("list", label)
            if rc == 113 or "Could not find service" in err:
                missing.append(label.rsplit(".", 1)[-1])
            # Non-zero for any other reason (sudo gap, launchd error) → skip;
            # we'd rather stay quiet than fire a false alert.
        except Exception:
            continue
    return missing


def _check_acl_drift(network: dict) -> list[str]:
    """Bots whose .openclaw/ may have lost the evolve read ACL.

    We use a cheap heuristic: try to read .openclaw/openclaw.json directly
    as the current user (which is the evolve user when the admin server
    runs in production). A PermissionError implies the ACL was stripped.

    This is best-effort — if the bot is on a different user or the path
    layout differs, we silently skip.
    """
    from evolve_config import user_home

    drifted: list[str] = []
    bots = network.get("bots") or {}
    for bot_id, spec in bots.items():
        user = (spec or {}).get("user") or bot_id
        if user == "evolve":
            continue
        # Resolve via the platform seam (pwd-first, profile fallback) — a
        # `/Users/` literal probes a path that never exists on a Linux pod,
        # so every bot would look "not deployed" and drift would go unseen.
        candidate = user_home(user) / ".openclaw" / "openclaw.json"
        try:
            # We don't care about the contents, only whether we can read.
            with candidate.open("rb") as f:
                f.read(1)
        except PermissionError:
            drifted.append(bot_id)
        except (FileNotFoundError, NotADirectoryError, OSError):
            # Bot may not be deployed yet — not a drift, skip.
            continue
    return drifted


def _shared_disk_pct(shared_dir: Path) -> int | None:
    """Return percent-used of the volume holding ``shared_dir``, or None
    if the check fails.

    Uses psutil.disk_usage() (same as host_health.py) so the reading is
    consistent with the host overview panel. os.statvfs() on macOS APFS
    under-reports free space (ignores purgeable space) and can produce a
    reading that diverges wildly from what the OS actually considers used.
    """
    try:
        import psutil
        du = psutil.disk_usage(str(shared_dir))
        return int(round(du.percent))
    except Exception:
        return None
