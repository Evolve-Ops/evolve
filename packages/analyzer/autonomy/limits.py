"""autonomy.limits — rung-3 daily-cap counters and the pause state.

Spec: docs/spec-autonomy-ladder-2026-06-10.md §1.3 + §3.4
(``autonomy_limit_hit``).

``rules.actions_per_day`` on an ``autonomous_within_rules`` posture is
a hard daily cap, counted per integration from the bot-side outward-
action ledger (``autonomy.actions_ledger`` — OQ-3 decided bot-side).
Hitting the cap:

  1. records a **pause** for the rest of the UTC day in the per-bot
     sidecar ``{shared_dir}/bots/<bot_id>/autonomy-limits.json``,
  2. re-renders the bot so every outward tool on that integration is
     mechanically denied until the day rolls over (the renderer and the
     coherence check both consult the sidecar — render and audit must
     agree), and
  3. fires an ``autonomy_limit_hit`` finding. The Signal stays firing
     while the pause holds and sweep-resolves after the day rolls.

It does not silently queue (the per-bot ``daily_cap_usd`` breaker
precedent). The pause clears mechanically on the first evaluation of
the next UTC day, which also re-renders.

Honesty note (recorded for the audit surface): the counter is exact —
it counts what the bot's own gateway observed itself doing — but
enforcement is applied by an evolve-side evaluation pass (the 5-minute
``ai.evolve.evolve.autonomy-limits`` daemon, with the permission
monitor's audit pass as the slow backstop). Between cap-crossing and
the next evaluation the limit is instruction, not a wall; the rung's
enforcement-mode badge already says "instructed and monitored"
(spec §2.4) and the guidance block tells the bot its limit.

Concurrency: the sidecar shares the per-bot autonomy flock with
``autonomy.store`` — pause writes and posture writes may race the UI.
Mode 0644 like the intent file: ``session_surface`` (bot user) reads
it to tell a paused bot it is paused.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json, now_iso
from store_lock import locked as _flock

from . import actions_ledger as _ledger
from . import catalog as _catalog
from . import store as _store


LIMITS_FILENAME = "autonomy-limits.json"
LIMIT_SIGNAL_TYPE = "autonomy_limit_hit"


def limits_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "bots" / bot_id / LIMITS_FILENAME


def load_limits(shared_dir: Path, bot_id: str) -> dict[str, dict[str, Any]]:
    """Sidecar state: ``{integration_id: {date, count, cap, paused,
    paused_at}}``. Missing/malformed reads as empty — the sidecar is
    derived state; the ledger re-derives it on the next evaluation."""
    try:
        data = json.loads(limits_path(shared_dir, bot_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("integrations") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        str(iid): dict(entry)
        for iid, entry in entries.items()
        if isinstance(entry, dict)
    }


def _save_limits(
    shared_dir: Path, bot_id: str, entries: dict[str, dict[str, Any]],
) -> None:
    path = limits_path(shared_dir, bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {"schema_version": 1, "bot_id": bot_id,
         "integrations": dict(sorted(entries.items()))},
        mode=0o644,
    )


def paused_integrations(
    shared_dir: Path, bot_id: str, *, now: datetime | None = None,
) -> set[str]:
    """Integration ids currently paused (cap hit, same UTC day).

    A stale pause (yesterday's) reads as NOT paused even before the
    evaluation pass clears it — the renderer and coherence check call
    this, and both must stop denying the moment the day rolls, not when
    a daemon next runs.
    """
    today = _ledger.utc_today(now)
    return {
        iid for iid, entry in load_limits(shared_dir, bot_id).items()
        if entry.get("paused") and entry.get("date") == today
    }


def evaluate_bot(
    shared_dir: Path,
    bot_id: str,
    config: dict | None = None,
    *,
    home_override: Path | None = None,
    now: datetime | None = None,
    enforce: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Evaluate rung-3 caps for one bot; enforce pauses; return findings.

    Returns ``(findings, ran_ok)`` — the permission-monitor contract:
    ``ran_ok=False`` means tooling failure and the caller must keep this
    bot's existing ``autonomy_limit_hit`` Signals out of the sweep.

    Idempotent: re-evaluating the same ledger state changes nothing.
    A pause-state change (set on cap-hit, cleared on day-roll or when
    the posture left rung 3) re-renders the bot once.

    ``enforce=False`` is the dry-run path: findings are computed from
    the ledger + current sidecar but nothing is written or rendered.
    """
    now = now or datetime.now(timezone.utc)
    today = _ledger.utc_today(now)
    findings: list[dict[str, Any]] = []

    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError:
        # Malformed intent file — the coherence check owns surfacing it.
        return findings, False
    if doc is None or not doc.integrations:
        # No posture entries; clear any orphaned sidecar state.
        if enforce and load_limits(shared_dir, bot_id):
            with _flock(_store.lock_path(shared_dir, bot_id)):
                _save_limits(shared_dir, bot_id, {})
        return findings, True

    try:
        actions = _ledger.read_outward_actions(
            shared_dir, bot_id, window_days=2, now=now,
        )
    except Exception:  # noqa: BLE001 — ledger unreadable = tooling failure
        return findings, False

    state_dirty = False
    render_needed = False
    with _flock(_store.lock_path(shared_dir, bot_id)):
        entries = load_limits(shared_dir, bot_id)
        seen: set[str] = set()
        for iid, posture in sorted(doc.integrations.items()):
            cap_raw = (posture.rules or {}).get("actions_per_day")
            cap: int | None = (
                cap_raw
                if isinstance(cap_raw, int) and not isinstance(cap_raw, bool)
                and cap_raw > 0
                else None
            )
            capped = (
                posture.rung == _catalog.RUNG_AUTONOMOUS
                and cap is not None
                # Observe-only postures are never rendered, so a cap on
                # them is never enforced — don't count against it either.
                and (posture.set_by or {}).get("actor") != _store.ACTOR_BACKFILL
            )
            if not capped or cap is None:
                prior = entries.get(iid)
                if prior is None:
                    continue
                if bool(prior.get("paused")) and prior.get("date") == today:
                    # The pause SURVIVES a same-day posture change —
                    # including the demotion reflex. The day's budget was
                    # spent while autonomous; leaving rung 3 must not lift
                    # the outward-deny wall the cap (and possibly the
                    # probing evidence) earned. paused_integrations() is
                    # posture-independent, so the renderer keeps denying
                    # at the new rung until the day rolls. (Second-pass
                    # review finding: deleting here re-opened send within
                    # one pass of an auto-demotion.)
                    seen.add(iid)
                    findings.append(_limit_finding(
                        bot_id, posture,
                        count=int(prior.get("count") or 0),
                        cap=int(prior.get("cap") or 0),
                        now=now,
                    ))
                    continue
                # Stale (past-day) leftover — safe to drop; re-render so
                # yesterday's pause denies lift at the current rung.
                render_needed = render_needed or bool(prior.get("paused"))
                del entries[iid]
                state_dirty = True
                continue
            seen.add(iid)
            count = _ledger.count_for_day(actions, iid, today)
            prior = entries.get(iid) or {}
            was_paused_today = bool(prior.get("paused")) and prior.get("date") == today
            if (
                was_paused_today
                and count < cap
                and cap > int(prior.get("cap") or 0)
            ):
                # The operator raised the daily limit mid-day — exactly
                # the remedy the alert suggests. Honor it: un-pause when
                # the new, larger cap has headroom. (The pause otherwise
                # sticks for the day even if the ledger reads lower,
                # e.g. after a prune — no flap.)
                was_paused_today = False
            paused = was_paused_today or count >= cap
            entry: dict[str, Any] = {
                "date": today,
                "count": count,
                "cap": cap,
                "paused": paused,
            }
            if paused:
                entry["paused_at"] = (
                    prior.get("paused_at") if was_paused_today else now_iso()
                ) or now_iso()
            if entry != prior:
                # Only a pause-bit or day flip changes the deny slice;
                # count churn alone doesn't need a re-render.
                if bool(prior.get("paused")) != paused or prior.get("date") != today:
                    render_needed = True
                entries[iid] = entry
                state_dirty = True
            if paused:
                findings.append(_limit_finding(
                    bot_id, posture, count=count, cap=cap, now=now,
                ))
        # Drop sidecar entries whose posture vanished entirely (the
        # renderer skips absent postures, so their denies are gone
        # regardless — keeping the entry would be a lie).
        for iid in [i for i in entries if i not in seen]:
            render_needed = render_needed or bool(entries[iid].get("paused"))
            del entries[iid]
            state_dirty = True
        if state_dirty and enforce:
            _save_limits(shared_dir, bot_id, entries)

    if render_needed and enforce:
        from . import renderer as _renderer
        result = _renderer.render_bot(
            bot_id, shared_dir, home_override=home_override, now=now,
        )
        if result.write_error:
            # Pause recorded but enforcement didn't land — drift check
            # owns the gap; say so loudly rather than silently.
            import sys
            print(
                f"[autonomy/limits] {bot_id}: pause render failed: {result.write_error}",
                file=sys.stderr,
            )

    return findings, True


def _limit_finding(
    bot_id: str, posture: _store.IntegrationPosture, *, count: int, cap: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    binding = _catalog.binding_for(posture.integration_id)
    spec = _catalog.kind_spec(posture.kind)
    display = binding.display_name if binding else posture.integration_id
    noun = spec.operator_noun if spec else posture.kind
    return {
        "type": LIMIT_SIGNAL_TYPE,
        "severity": "warn",
        "signature_scope": f"{bot_id}:{posture.integration_id}",
        "title": (
            f"{bot_id}: daily {noun} limit reached — paused for today"
        ),
        "body": (
            f"{bot_id} reached its daily limit of {cap} {noun} actions on "
            f"{display} ({count} today) and is paused from acting there "
            "until tomorrow. Reading and drafting continue. No action "
            "needed unless this keeps happening — you can raise the "
            "daily limit on Security → Permissions → Autonomy."
        ),
        "details": {
            "bot_id": bot_id,
            "integration_id": posture.integration_id,
            "integration_label": f"{noun} ({display})",
            "kind": posture.kind,
            "rung": posture.rung,
            "count": count,
            "cap": cap,
            "date": _ledger.utc_today(now),
        },
    }


__all__ = [
    "LIMIT_SIGNAL_TYPE",
    "LIMITS_FILENAME",
    "evaluate_bot",
    "limits_path",
    "load_limits",
    "paused_integrations",
]
