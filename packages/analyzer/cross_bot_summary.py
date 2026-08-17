#!/usr/bin/env python3
"""cross_bot_summary.py — Gather a plain-language pod-wide summary.

This module is the data-gathering primitive for ``evo status`` / ``evo summary``
/ ``evo, what's on this week?``. It reads from the existing on-disk stores
(signals, proposals, daily metrics, manifests) and returns a structured
:class:`PodSummary` that the evo handler renders as a user-facing message.

Design constraints:
- Read-only — touches no files, makes no changes.
- No LLM calls — all data is already structured; synthesis is plain-Python.
- Fast — all reads are from /Users/Shared/evolve/ (shared_dir), not per-bot
  remote calls. Target: well under 1 second on a typical pod.
- Soft-fails — if a data source is missing or corrupt, that section is
  omitted rather than crashing.

Called by packages/admin/evolve_admin/evo/handlers/pod_summary.py which
builds a DispatchResult the evo bot speaks verbatim.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Public data shapes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BotSlice:
    """Summary data for one member bot."""

    bot_id: str
    # Signals currently firing (severity "alert" or "warn") for this bot.
    # The Signal schema enum is "info" | "warn" | "alert" — see
    # packages/analyzer/schema/signal.py line 36 and producer_severity.py.
    # Each item is a short human-readable description.
    firing_alerts: list[str] = field(default_factory=list)
    # Pending proposals (improvement suggestions) targeted at this bot.
    pending_proposals: int = 0
    # Total cost over the past 7 days, USD.
    cost_7d_usd: float = 0.0
    # Turn count over the past 7 days (activity proxy).
    turns_7d: int = 0
    # Number of manifests installed on this bot.
    app_count: int = 0
    # Names/purposes of apps that ran successfully this week (sample, max 3).
    active_apps: list[str] = field(default_factory=list)


@dataclass
class PodSummary:
    """Aggregated pod-wide summary for the evo status / what's on this week? response."""

    generated_at: str  # ISO timestamp
    bot_slices: list[BotSlice] = field(default_factory=list)
    # Pod-wide (non-bot-scoped) firing signals.
    pod_alerts: list[str] = field(default_factory=list)
    # Total pending proposals across all bots.
    total_pending_proposals: int = 0
    # Total cost this week across all bots.
    total_cost_7d_usd: float = 0.0
    # Errors encountered during data gathering (for diagnostic notes).
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — signals
# ─────────────────────────────────────────────────────────────────────────────


def _iter_firing_signals(shared_dir: Path):
    """Yield firing Signals as dicts. Soft-fails.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``signals.store.iter_active`` rather than
    globbing ``{shared_dir}/signals/firing/`` directly.
    """
    try:
        from signals import store as signals_store  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        for sig in signals_store.iter_active(shared_dir, state="firing"):
            yield sig.to_dict()
    except Exception:
        return


def _collect_alerts(
    shared_dir: Path,
) -> tuple[dict[str, list[str]], list[str]]:
    """Return (per_bot_alerts, pod_alerts).

    per_bot_alerts maps bot_id → list of short strings.
    pod_alerts is a list of pod/host-scoped alert strings.
    Severity "info" signals are filtered out — only "warn" and "alert"
    surface. The canonical Signal severity enum is "info" | "warn" |
    "alert" (schema/signal.py:36); there is no "critical".
    """
    per_bot: dict[str, list[str]] = {}
    pod_wide: list[str] = []

    for raw in _iter_firing_signals(shared_dir):
        severity = raw.get("severity", "info")
        if severity not in ("warn", "alert"):
            continue
        title = raw.get("title") or raw.get("type") or "alert"
        # Strip a redundant leading "<bot_id>:" or "<producer>:" from the
        # title — Signal titles often already begin with the bot name (e.g.
        # "admin_bot: README.md missing"), and the renderer prefixes the bot
        # display name again, which would produce "admin_bot: admin_bot: README.md
        # missing". We dedup here at the gather layer so the rendering is
        # clean regardless of where the title came from.
        scope = raw.get("scope", "pod")
        bot_id = raw.get("bot_id")
        if scope == "bot" and bot_id:
            title = _strip_redundant_bot_prefix(title, bot_id)
            per_bot.setdefault(bot_id, []).append(title)
        else:
            pod_wide.append(title)

    return per_bot, pod_wide


def _strip_redundant_bot_prefix(title: str, bot_id: str) -> str:
    """Drop a leading ``<bot_id>:`` (case-insensitive) from ``title``.

    Many Signal producers prepend the bot name to the title — e.g.
    ``"admin_bot: README.md missing"`` — but the renderer also prefixes the
    bot's display name when it lists per-bot alerts. Without this
    stripping the user sees ``"admin_bot: alert: admin_bot: README.md missing"``.

    Returns ``title`` unchanged if no redundant prefix is present.
    """
    prefix = f"{bot_id}:"
    if title.lower().startswith(prefix.lower()):
        return title[len(prefix):].lstrip()
    return title


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — proposals
# ─────────────────────────────────────────────────────────────────────────────


def _count_pending_proposals(shared_dir: Path) -> dict[str, int]:
    """Return {bot_id: count} of proposals in proposals/pending/.

    Per CLAUDE.md arbiter layout, only pending/ has actionable items;
    snoozed/ is user-deferred, so we count pending/ only.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``arbiter.store.iter_proposals`` rather than
    globbing ``{shared_dir}/proposals/pending/`` directly.
    """
    counts: dict[str, int] = {}
    try:
        from arbiter import store as arbiter_store  # type: ignore[import-not-found]
    except Exception:
        return counts
    try:
        for proposal in arbiter_store.iter_proposals(shared_dir, subdirs=("pending",)):
            raw = proposal.to_dict()
            bot_id = raw.get("bot_id") or raw.get("scope_id")
            if bot_id:
                counts[bot_id] = counts.get(bot_id, 0) + 1
    except Exception:
        return counts
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — metrics
# ─────────────────────────────────────────────────────────────────────────────


def _load_daily_metric(shared_dir: Path, bot_id: str, d: date) -> dict | None:
    p = shared_dir / "metrics" / d.isoformat() / f"{bot_id}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sum_metric_7d(
    shared_dir: Path, bot_id: str, key: str, today: date
) -> float:
    """Sum a numeric metric key over the past 7 days."""
    total: float = 0.0
    for i in range(7):
        d = today - timedelta(days=i)
        m = _load_daily_metric(shared_dir, bot_id, d)
        if m is None:
            continue
        v = m.get(key, 0)
        try:
            total += float(v)
        except (TypeError, ValueError):
            pass
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — apps / manifests
# ─────────────────────────────────────────────────────────────────────────────


def _collect_app_info(
    shared_dir: Path, bot_id: str
) -> tuple[int, list[str]]:
    """Return (total_app_count, active_app_names).

    active_app_names: up to 3 app names whose last_test_result == "pass"
    or that have run this week (have a non-None last_run_at within 7 days).
    """
    apps_dir = shared_dir / "applications" / bot_id
    if not apps_dir.exists():
        return 0, []

    total = 0
    active: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for path in apps_dir.glob("*.json"):
        # Skip hidden / dotfile JSON like ".scan-status.json" — these are
        # bookkeeping files written by the application scanner, not real
        # manifests. Pathlib's glob("*.json") matches them on macOS.
        if path.name.startswith("."):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total += 1
        name = raw.get("name") or raw.get("id") or path.stem

        # Mark as active if last test passed or it ran recently.
        last_result = raw.get("last_test_result")
        last_run = raw.get("last_run_at") or raw.get("last_tested_at")
        recently_ran = False
        if last_run:
            try:
                ran_at = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                recently_ran = ran_at >= cutoff
            except (ValueError, AttributeError):
                pass

        if last_result == "pass" or recently_ran:
            active.append(name)

    return total, active[:3]


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def gather(
    shared_dir: Path,
    network: dict[str, Any],
    *,
    today: date | None = None,
) -> PodSummary:
    """Gather a pod-wide summary from existing on-disk stores.

    ``network`` is the parsed network.json dict (must include ``"bots"``).
    ``today`` defaults to the current UTC date; injectable for tests.

    Returns a :class:`PodSummary` with data for every member bot (role
    != "admin") found in network["bots"].
    """
    if today is None:
        today = date.today()

    errors: list[str] = []

    # Signals: gather once for the whole pod.
    try:
        per_bot_alerts, pod_wide_alerts = _collect_alerts(shared_dir)
    except Exception as e:
        per_bot_alerts, pod_wide_alerts = {}, []
        errors.append(f"signals read failed: {e}")

    # Proposals: gather once.
    try:
        proposal_counts = _count_pending_proposals(shared_dir)
    except Exception as e:
        proposal_counts = {}
        errors.append(f"proposals read failed: {e}")

    # Build per-bot slices for member bots only.
    bots_cfg = network.get("bots") or {}
    member_bot_ids = [
        bot_id
        for bot_id, cfg in bots_cfg.items()
        if cfg.get("role") != "primary"
    ]

    slices: list[BotSlice] = []
    total_cost = 0.0
    total_proposals = 0

    for bot_id in sorted(member_bot_ids):
        sl = BotSlice(bot_id=bot_id)

        # Alerts.
        sl.firing_alerts = per_bot_alerts.get(bot_id, [])

        # Proposals.
        sl.pending_proposals = proposal_counts.get(bot_id, 0)
        total_proposals += sl.pending_proposals

        # Metrics.
        try:
            sl.cost_7d_usd = _sum_metric_7d(shared_dir, bot_id, "total_cost_estimated", today)
            sl.turns_7d = int(_sum_metric_7d(shared_dir, bot_id, "turn_count", today))
        except Exception as e:
            errors.append(f"{bot_id} metrics failed: {e}")
        total_cost += sl.cost_7d_usd

        # Apps.
        try:
            sl.app_count, sl.active_apps = _collect_app_info(shared_dir, bot_id)
        except Exception as e:
            errors.append(f"{bot_id} apps read failed: {e}")

        slices.append(sl)

    return PodSummary(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        bot_slices=slices,
        pod_alerts=pod_wide_alerts,
        total_pending_proposals=total_proposals,
        total_cost_7d_usd=total_cost,
        errors=errors,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


def render_summary(summary: PodSummary, *, bot_names: dict[str, str] | None = None) -> str:
    """Render a PodSummary as a plain-language, Telegram-friendly message.

    ``bot_names`` optionally maps bot_id → display name. Falls back to bot_id.

    Format is Team_bot_a-style: short header, one fact per line, conversational
    close-out. Fits in one Telegram message (<4096 chars).
    """
    lines: list[str] = []

    def display(bot_id: str) -> str:
        return (bot_names or {}).get(bot_id, bot_id)

    # Header.
    lines.append("Pod summary")
    lines.append("")

    # Pod-wide alerts (critical infra issues).
    if summary.pod_alerts:
        lines.append("Pod alerts:")
        for a in summary.pod_alerts[:5]:
            lines.append(f"  - {a}")
        lines.append("")

    # Per-bot slices.
    has_anything = False
    bots_needing_attention = [
        sl for sl in summary.bot_slices
        if sl.firing_alerts or sl.pending_proposals > 0
    ]
    quiet_bots = [
        sl for sl in summary.bot_slices
        if not sl.firing_alerts and sl.pending_proposals == 0
    ]

    # First: bots that need attention.
    if bots_needing_attention:
        has_anything = True
        for sl in bots_needing_attention:
            parts: list[str] = []
            if sl.firing_alerts:
                alert_str = ", ".join(sl.firing_alerts[:2])
                if len(sl.firing_alerts) > 2:
                    alert_str += f" (+{len(sl.firing_alerts) - 2} more)"
                parts.append(f"alert: {alert_str}")
            if sl.pending_proposals:
                p = sl.pending_proposals
                parts.append(f"{p} suggestion{'s' if p != 1 else ''} pending")
            detail = " — ".join(parts)
            lines.append(f"  {display(sl.bot_id)}: {detail}")

        lines.append("")

    # Activity snapshot — cost and turns for all bots.
    active_bots = [sl for sl in summary.bot_slices if sl.turns_7d > 0]
    if active_bots:
        has_anything = True
        lines.append("This week:")
        for sl in sorted(active_bots, key=lambda s: s.turns_7d, reverse=True):
            app_part = ""
            if sl.active_apps:
                app_part = f" · {', '.join(sl.active_apps[:2])}"
            cost_part = f"  ${sl.cost_7d_usd:.2f}" if sl.cost_7d_usd > 0 else ""
            lines.append(f"  {display(sl.bot_id)}: {sl.turns_7d} turns{cost_part}{app_part}")
        lines.append("")

    # Quiet bots — consolidate into one line to save space.
    if quiet_bots and not active_bots:
        quiet_names = ", ".join(display(sl.bot_id) for sl in quiet_bots)
        lines.append(f"All quiet: {quiet_names}")
        lines.append("")

    # Pod totals.
    totals_parts = []
    if summary.total_cost_7d_usd > 0:
        totals_parts.append(f"${summary.total_cost_7d_usd:.2f} this week")
    if summary.total_pending_proposals > 0:
        p = summary.total_pending_proposals
        totals_parts.append(f"{p} suggestion{'s' if p != 1 else ''} waiting")
    if totals_parts:
        lines.append("Pod total: " + " · ".join(totals_parts))
        lines.append("")

    if not has_anything and not summary.pod_alerts and not summary.total_pending_proposals:
        lines.append("All quiet across your pod.")
        lines.append("")

    # Close-out.
    lines.append("Type `evo better` for a specific suggestion, or check the dashboard for details.")

    return "\n".join(lines).rstrip()
