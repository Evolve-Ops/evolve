#!/usr/bin/env python3
"""
weekly_bot_trends.py — Weekly per-bot 7-day trend digest.

Sunday-morning sibling to ``pod_report.py`` (the daily digest). The daily
report carries today's news + current-state chips (``today`` and
``ongoing`` horizons); PR #1996 dropped chips tagged ``horizon: "7d"``
out of it because a one-day cost spike on Monday would otherwise re-fire
in every daily digest Mon→Sun until the 7d window rolled past.

7-day-window trends still matter — a sustained spend-rate change, a
week-over-week activity swing, a pushback pattern across the week. They
belong in a weekly cadence, not a daily one. This producer is that
cadence.

What it surfaces (all from ``tile_metrics`` — no new measurement):
  * **Pod rollup** — total turns + cost this week with the week-over-week
    delta, the busiest bot, and the biggest mover. The at-a-glance line.
  * **Movers** — every bot whose turns or cost moved materially vs the
    prior 7d (≥10% AND past a small absolute floor), or that has a
    ``horizon: "7d"`` chip firing. Each carries its WoW deltas plus any
    7d chip (cost_spike, high_correction, unexpected_billing).
  * **Steady** — bots within ±10% collapse to a one-line count rather
    than a roster of bare checkmarks (digest discipline).

Silent when nothing moved pod-wide — green messages just to confirm
nothing is wrong run against the operator-message-style "silence is
default" rule.

Scheduled via launchd: Sunday 03:30 (after analyze.py at 02:00 and
weekly_review.py at 03:00), no RunAtLoad.

**Duplicate-fire guard.** macOS launchd runs a *missed* StartCalendarInterval
event the moment the daemon is re-bootstrapped — so every non-Sunday
deploy/repo-pull that reinstalls this daemon used to trigger an immediate
make-up run. Combined with the live turn/cost counts drifting between
runs (which slips the dispatcher's identical-content floor), that produced
3 near-identical reports in one day on 2026-06-18. Two guards now:
  1. an ISO-week sentinel (:func:`_already_sent_this_week`) — the producer
     refuses to send twice in the same ISO week regardless of fire cause
     or content drift (full-week, mechanism-independent);
  2. the dispatcher source category is ``STATE_PERSISTS`` (24h cooldown on
     the per-ISO-week ``dedup_key`` regardless of body) as defense-in-depth.

Mute via: ``summaries.weekly_bot_trends`` subscription.
"""

from __future__ import annotations

import argparse
import json
from datetime import date as _date
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path
from tile_metrics import compute_tile_data


def _html_escape(value) -> str:
    """HTML-escape an interpolated value for Telegram parse_mode=HTML.

    Same fallback shape as ``pod_report._html_escape`` — analyzer-side
    producers can't hard-depend on the alerts package being importable
    on a partial install, so we delegate to the catalog helper when it
    loads and fall back to ``html.escape`` otherwise.
    """
    try:
        from evolve_admin.alerts.catalog import html_escape
        return html_escape(value)
    except Exception:
        import html as _html
        return _html.escape("" if value is None else str(value), quote=False)


_SEVERITY_ICON = {
    "critical": "🚨",
    "warn":     "⚠️",
}

# A bot is a "mover" when a metric moved at least this much vs the prior
# 7-day window AND cleared the matching absolute floor. The floor stops
# tiny-absolute / huge-percent noise ("$0.10 → $0.40" is +300% but not
# worth a line); the percentage stops large-absolute / tiny-percent noise.
_MOVER_PCT = 10.0
_COST_FLOOR_USD = 1.0
_TURNS_FLOOR = 20


def _enumerate_bots(network: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for bot_id, spec in (network.get("bots") or {}).items():
        if not isinstance(spec, dict):
            out.append((bot_id, {}))
            continue
        out.append((bot_id, spec))
    return out


def _is_7d_chip(chip: dict) -> bool:
    """A chip belongs in the weekly digest iff its horizon is ``7d``.

    Per PR #1996, every chip carries a ``horizon`` of ``today`` / ``7d``
    / ``ongoing``. The daily filters to today+ongoing; this producer is
    the mirror image. Chips without a horizon field are treated as
    not-7d (defensive against any chip the schema retrofit missed).
    """
    return chip.get("horizon") == "7d"


def _pct(cur: float, prior: float) -> float | None:
    """Week-over-week percent change, or None when prior is zero/absent
    (an infinite/undefined ratio — the caller renders that as "new")."""
    if prior is None or prior <= 0:
        return None
    return (cur - prior) / prior * 100.0


def _delta_str(cur: float, prior: float) -> str:
    """Compact WoW delta token: ``↑12%`` / ``↓4%`` / ``new`` / ``flat``.

    ``new`` when there was no prior-window activity but there is now;
    ``flat`` when the change rounds to 0%; empty when there's nothing on
    either side (both windows zero)."""
    pct = _pct(cur, prior)
    if pct is None:
        return "new" if cur > 0 else ""
    rounded = round(pct)
    if rounded == 0:
        return "flat"
    return f"{'↑' if rounded > 0 else '↓'}{abs(rounded)}%"


def _magnitude(cur: float, prior: float) -> float:
    """Ranking magnitude for "biggest mover". A bot with no prior-window
    activity (a genuine new mover) sorts above same-percent steady bots."""
    pct = _pct(cur, prior)
    if pct is None:
        return 100.0 if cur > 0 else 0.0
    return abs(pct)


def _is_material(cur: float, prior: float, floor: float) -> bool:
    """True when a metric moved enough to call the bot a mover: the
    absolute change clears ``floor`` AND (vs a non-zero prior) the
    percentage clears the threshold, or it's fresh activity above floor."""
    if abs(cur - prior) < floor:
        return False
    pct = _pct(cur, prior)
    if pct is None:               # no prior window → new activity
        return cur >= floor
    return abs(pct) >= _MOVER_PCT


def _fmt_turns(n: int) -> str:
    return f"{int(n):,} turns"


def _fmt_usd(x: float) -> str:
    return f"${x:.2f}"


def build_report(
    shared_dir: str, network: dict, *, today: _date | None = None,
) -> tuple[str, bool]:
    """Build the weekly trends message.

    Returns ``(message, should_send)``. ``should_send`` is True iff at
    least one bot is a mover (material WoW change or a 7d-horizon chip).
    Silent-when-clean is intentional — see module docstring +
    operator-message-style.md.

    Shape: a pod rollup header (totals + WoW deltas, busiest bot, biggest
    mover), a **Movers** section with per-bot WoW deltas and any 7d chips,
    then a collapsed **Steady** count for bots within ±10%.
    """
    today = today or _date.today()
    shared_dir_path = Path(shared_dir)

    # Per-bot rows: turns/cost for this 7d window and the prior one, the
    # 7d chips, mover classification, and a ranking magnitude.
    rows: list[dict] = []

    for bot_id, spec in _enumerate_bots(network):
        bot_data = {
            "role": (spec or {}).get("role") or "member",
            # No live_data: we deliberately ignore ``stale_heartbeat`` and
            # other ``ongoing`` chips even if compute_tile_data emitted
            # them — they aren't ``horizon: "7d"`` and would be filtered
            # anyway. Mirrors pod_report's offline posture.
            "live_data": {},
        }
        try:
            tile = compute_tile_data(
                shared_dir=shared_dir_path,
                bot_id=bot_id,
                bot_data=bot_data,
                network=network,
            )
        except Exception:
            continue  # one bad bot shouldn't sink the whole report

        cost = tile.get("cost", {}) or {}
        activity = tile.get("activity", {}) or {}
        cost_7d = float(cost.get("usd_7d", 0.0) or 0.0)
        cost_prior = float(cost.get("usd_prior_7d", 0.0) or 0.0)
        turns_7d = int(activity.get("turns_7d", 0) or 0)
        turns_prior = int(activity.get("turns_prior_7d", 0) or 0)

        chips_7d = [c for c in (tile.get("health_chips") or []) if _is_7d_chip(c)]

        cost_material = _is_material(cost_7d, cost_prior, _COST_FLOOR_USD)
        turns_material = _is_material(turns_7d, turns_prior, _TURNS_FLOOR)
        is_mover = bool(chips_7d) or cost_material or turns_material

        rows.append({
            "bot_id": bot_id,
            "turns_7d": turns_7d,
            "turns_prior": turns_prior,
            "cost_7d": cost_7d,
            "cost_prior": cost_prior,
            "chips_7d": chips_7d,
            "is_mover": is_mover,
            "magnitude": max(_magnitude(cost_7d, cost_prior),
                             _magnitude(turns_7d, turns_prior)),
        })

    movers = [r for r in rows if r["is_mover"]]
    steady = [r for r in rows if not r["is_mover"]]
    should_send = bool(movers)

    week_label = today.strftime("W%V")
    lines = [f"📊 <b>Bot trends</b> — Weekly ({week_label})\n"]

    if not should_send:
        # Caller drops the message (should_send=False); return the header
        # only so a debug ``--always`` run still has something to print.
        return "\n".join(lines), False

    # Movers ranked by magnitude of change, biggest first; chip-bearing
    # bots get a nudge above pure-delta movers of equal magnitude.
    movers.sort(key=lambda r: (bool(r["chips_7d"]), r["magnitude"]), reverse=True)

    # ── Pod rollup ──────────────────────────────────────────────────────────
    tot_turns = sum(r["turns_7d"] for r in rows)
    tot_turns_prior = sum(r["turns_prior"] for r in rows)
    tot_cost = sum(r["cost_7d"] for r in rows)
    tot_cost_prior = sum(r["cost_prior"] for r in rows)

    pod_turns_delta = _delta_str(tot_turns, tot_turns_prior)
    pod_cost_delta = _delta_str(tot_cost, tot_cost_prior)
    pod_line = (
        f"Pod: {_fmt_turns(tot_turns)}"
        + (f" {pod_turns_delta}" if pod_turns_delta else "")
        + f" · {_fmt_usd(tot_cost)}"
        + (f" {pod_cost_delta}" if pod_cost_delta else "")
        + " vs prior 7d"
    )
    lines.append(pod_line)

    extras = []
    busiest = max(rows, key=lambda r: r["turns_7d"], default=None)
    if busiest and busiest["turns_7d"] > 0:
        extras.append(
            f"Busiest: {_html_escape(busiest['bot_id'])} "
            f"({busiest['turns_7d']:,})"
        )
    top = movers[0]
    # Name the biggest mover by its dominant dimension.
    if _magnitude(top["cost_7d"], top["cost_prior"]) >= \
            _magnitude(top["turns_7d"], top["turns_prior"]):
        mv_delta, mv_dim = _delta_str(top["cost_7d"], top["cost_prior"]), "cost"
    else:
        mv_delta, mv_dim = _delta_str(top["turns_7d"], top["turns_prior"]), "turns"
    if mv_delta and mv_delta != "flat":
        extras.append(
            f"Biggest mover: {_html_escape(top['bot_id'])} ({mv_dim} {mv_delta})"
        )
    if extras:
        lines.append(" · ".join(extras))
    lines.append("")

    # ── Movers ──────────────────────────────────────────────────────────────
    lines.append("<b>Movers:</b>")
    for r in movers:
        segs = []
        if r["turns_7d"] or r["turns_prior"]:
            t_delta = _delta_str(r["turns_7d"], r["turns_prior"])
            segs.append(_fmt_turns(r["turns_7d"]) + (f" {t_delta}" if t_delta else ""))
        if r["cost_7d"] or r["cost_prior"]:
            c_delta = _delta_str(r["cost_7d"], r["cost_prior"])
            segs.append(_fmt_usd(r["cost_7d"]) + (f" {c_delta}" if c_delta else ""))
        suffix = f" {_html_escape(' · '.join(segs))}" if segs else ""
        lines.append(f"<b>{_html_escape(r['bot_id'])}</b>{suffix}")
        for c in r["chips_7d"]:
            icon = _SEVERITY_ICON.get(c.get("severity"), "•")
            label = c.get("label", c.get("id", "issue"))
            detail = c.get("detail")
            line = f"  {icon} {_html_escape(label)}"
            if detail:
                line += f" ({_html_escape(detail)})"
            lines.append(line)

    # ── Steady (collapsed) ──────────────────────────────────────────────────
    if steady:
        lines.append("")
        n = len(steady)
        lines.append(
            f"{n} other{'s' if n != 1 else ''} steady "
            f"(within ±{int(_MOVER_PCT)}% of prior week)"
        )

    return "\n".join(lines), should_send


# ── ISO-week sentinel (duplicate-fire guard) ────────────────────────────────


def _sentinel_path(shared_dir: str | Path) -> Path:
    """Where the last-sent ISO week is recorded. Under ``{shared_dir}``
    which the evolve daemon user owns — plain write, no sudo/staging."""
    return Path(shared_dir) / "state" / "weekly_bot_trends" / "last_sent_week"


def _already_sent_this_week(shared_dir: str | Path, iso_week: str) -> bool:
    """True if a report for ``iso_week`` was already sent. Robust against
    launchd make-up fires and manual re-runs regardless of content drift.
    Fails open (returns False) if the sentinel can't be read — better a
    rare duplicate than a silently-suppressed weekly."""
    try:
        return _sentinel_path(shared_dir).read_text(encoding="utf-8").strip() == iso_week
    except (FileNotFoundError, OSError):
        return False


def _mark_sent_this_week(shared_dir: str | Path, iso_week: str) -> None:
    """Record ``iso_week`` as sent. Atomic temp-file + rename. Non-fatal
    on failure — the dispatcher's STATE_PERSISTS 24h cooldown is the
    backstop if the sentinel can't be written."""
    import sys as _sys
    path = _sentinel_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(iso_week, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(
            f"[evolve/weekly_bot_trends] could not write sentinel ({exc}); "
            "relying on dispatcher cooldown",
            file=_sys.stderr,
        )


def send_report(
    message: str, network_config: dict, *, shared_dir: str | Path,
) -> None:
    """Push the weekly trends digest via the alerts dispatcher.

    Stays on the legacy ``message=`` path like ``pod_report.send_report``
    and ``weekly_review.send_report`` — the multi-section body (pod rollup,
    movers, steady count) is not something the catalog ``body_template``
    can usefully abstract.

    Dedup keyed on ISO week so a re-run inside the same week is suppressed
    instead of double-firing; the ISO-week sentinel is stamped only on a
    real SENT so a no-recipient run can retry later.
    """
    import sys as _sys
    iso_week = _date.today().strftime("%G-W%V")
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity, DispatchResult,
        )
    except Exception:
        print(
            "[evolve/weekly_bot_trends] dispatcher import failed; "
            "printing to stdout:",
            file=_sys.stderr,
        )
        print(message)
        return

    try:
        outcome = _dispatch_send(
            shared_dir=Path(shared_dir),
            network=network_config,
            source="weekly_bot_trends",
            message=message,
            severity=Severity.INFO,
            dedup_key=f"weekly_bot_trends/{iso_week}",
            catalog_event="summaries.weekly_bot_trends",
        )
    except Exception as exc:
        print(
            f"[evolve/weekly_bot_trends] dispatcher.send raised: {exc}; "
            "printing to stdout:",
            file=_sys.stderr,
        )
        print(message)
        return

    if outcome.result == DispatchResult.SENT:
        _mark_sent_this_week(shared_dir, iso_week)
        print("[evolve/weekly_bot_trends] Report sent")
    else:
        print(
            f"[evolve/weekly_bot_trends] Not sent ({outcome.result.value}): "
            f"{outcome.error or 'no error detail'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evolve weekly per-bot 7-day trend digest"
    )
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument(
        "--network", default=str(resolve_network_path()),
        help="Path to network.json",
    )
    parser.add_argument(
        "--always", action="store_true",
        help="Send even if nothing is trending AND bypass the once-per-week "
             "sentinel (operator/debug use)",
    )
    args = parser.parse_args()

    shared_dir = args.shared_dir
    network_config = {"alerts": {}}

    if args.network:
        network_config = json.loads(Path(args.network).read_text())
        shared_dir = network_config.get("sharedDir", shared_dir)

    iso_week = _date.today().strftime("%G-W%V")
    if not args.always and _already_sent_this_week(shared_dir, iso_week):
        print(
            f"[evolve/weekly_bot_trends] Already sent for {iso_week} — "
            "skipping (once-per-week sentinel)"
        )
        return

    message, should_send = build_report(shared_dir, network_config)

    if should_send or args.always:
        send_report(message, network_config, shared_dir=shared_dir)
    else:
        print(
            "[evolve/weekly_bot_trends] No bot trending — no report sent"
        )


if __name__ == "__main__":
    main()
