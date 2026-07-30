"""Signal-store transition notifier — Phase 4.

Watches the Signal store for new ``firing`` Signals (with debounce) and
``firing → resolved`` transitions for previously-announced Signals, and
pushes them to the operator's chat channel via the alert dispatcher.

Phase 4 of ``docs/spec-alert-notifier-2026-05-09.md``. **Enabled by
default** — ``alerts.signal_notifier.enabled`` has carried
``stock_default=True`` since 2026-05-20 (Phase 7, after the burn-in
window). The flag remains operator-tunable; only the default changed. It
was documented here as "default-off in v1" until 2026-07-29, which cost
real debugging time chasing a silence the flag was not responsible for.

**Transitions, not inventory — and what covers the gap.** This module
announces state *changes*. It deliberately says nothing about the set of
Signals firing right now, and its cold-start guard (see
``_sync_firing_as_known`` / ``run_once``) makes that permanent for any
backlog that existed at first run: those signatures are marked
already-alerted without a push, stay firing, never re-fire, and so are
never announced. Measured 2026-07-29: 95 of 99 firing Signals on the mini
and 34 of 36 on the Linux VPS had never reached the operator. The guard is
correct and stays (it prevents a flood, and the suppressed-log runaway it
also bounds filled 306 MB on 2026-06-01). The inventory channel is
``alerts.standing_alerts`` — a compact standing-alerts section in the daily
pod report. Every cold-start sync now leaves a ``cold_start`` trail in the
state file so a future debugger can tell backlog-silence from a delivery
failure.

Producer routing — deny-list-by-default
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Every producer's Signals reach operator chat by default. Only an explicit,
small deny-list of *direct-dispatch* producers is excluded — those already
call ``dispatcher.send`` on their own path; routing them here a second time
would double-message the operator.

The deny-list is the single source of truth for excluded producers:
``_DIRECT_DISPATCH_PRODUCERS``. The operator-facing config key is
``alerts.signal_notifier.excluded_producers``.

This replaces the previous allowlist model (``_DEFAULT_PRODUCERS``,
``alerts.signal_notifier.producers``) where new monitors were silently
excluded by default. The inversion means a brand-new monitor that writes
Signals to the store is loud automatically — no config edit needed.

State machine — Security_bot-style flap suppression:

  - **Per-severity grace** (spec-delta-transient-delivery-grace-2026-06-26,
    L3). A ``firing`` Signal younger than its *severity grace* is skipped —
    the operator's literal "time threshold for issues". ``alert`` grace is
    clamped to zero (a critical is never delayed); ``warn``/``info`` get a
    digest-window-sized grace so a transient that self-resolves inside it
    never pages. This generalizes the previous hardcoded ~240s debounce; the
    threshold table lives in ``alerts.grace`` (operator-tunable via
    ``alerts.grace_seconds_by_severity``).
  - **Fire push.** When the grace expires for an unannounced Signal, push
    once and record ``alerted_for_signal_id``.
  - **Cooldown.** Repeat fires for the same signature respect the
    dispatcher's per-source cooldown (``alerts.signal_notifier.cooldown_seconds``,
    default 600s). Operator-tunable from the admin UI.
  - **Recovery.** When a previously-announced Signal moves to ``resolved``,
    push immediately with no cooldown. The fire-pushed gate prevents
    orphan recovery messages for Signals we never announced.

State is per-signature in ``{shared_dir}/signals/notifier-state.json``:

::

    {
      "version": 1,
      "signatures": {
        "pod_health:pod_health_gateways:team_bot_a:gateway": {
          "alerted_for_signal_id": "sig_abc123",
          "last_fire_pushed_at": "2026-05-09T18:32:00Z",
          "last_resolve_pushed_at": null
        }
      }
    }

Only signatures we've actively pushed for live in this file. Empty
entries are pruned on save.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import _config_lookup
from . import dispatcher as _dispatcher
from . import grace as _grace
from .catalog import html_escape

_log = logging.getLogger(__name__)

# ── Defaults (mirror Phase 2 schema) ────────────────────────────────────────

# Direct-dispatch producers — these call dispatcher.send() on their own path
# and must NOT also be routed through signal_notifier, or the operator gets
# two chat messages per cost event.
#
# This is the SINGLE source of truth for excluded producers. The schema
# mirrors it in alerts.signal_notifier.excluded_producers stock_default.
# See also: packages/admin/evolve_admin/config_sandbox/schema.py
_DIRECT_DISPATCH_PRODUCERS: frozenset[str] = frozenset({
    # Both call dispatcher.send(catalog_event="cost.*") directly when their
    # detectors fire. Routing them here as well would double-message the
    # operator on every cost event (the two dedup_keys — signal.signature
    # vs the producer's per-bot key — don't overlap).
    "cost_watchdog",
    "spend_alert",
})

# Signal.severity → dispatcher.Severity. CRITICAL is reserved for future
# use; today every Signal is alert/warn/info.
_SEVERITY_MAP: dict[str, _dispatcher.Severity] = {
    "alert": _dispatcher.Severity.ERROR,
    "warn": _dispatcher.Severity.WARNING,
    "info": _dispatcher.Severity.INFO,
}

_EMOJI_BY_SEVERITY: dict[str, str] = {
    "alert": "🔴",
    "warn": "⚠️",
    "info": "ℹ️",
}
_RECOVERY_EMOJI = "🟢"

# Fire-message body cap. 200 was beheading the plain-English remediation
# mid-command ("sudo evolve-admi…" — VPS, 2026-07-29): the voice-guide body
# shape is plain what/impact/action first, "Technical detail: <raw>" last, so
# the cap must fit the full plain part and truncation should only ever eat
# the technical tail. Telegram's wire limit is 4096; 600 keeps messages
# glanceable while never cutting the sentence the operator must act on.
_BODY_EXCERPT_CHARS = 600


def _truncate_at_boundary(text: str, limit: int) -> str:
    """Cut ``text`` to ≤ ``limit`` chars at a text boundary, appending ``…``.

    Prefers the last paragraph break, then the last space, inside the limit —
    a mid-word (or mid-command) cut reads as a rendering bug and can destroy
    the one string the operator needs to copy. Falls back to a hard cut only
    when the text has no usable boundary in the back half of the window.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    for sep in ("\n\n", "\n", " "):
        pos = window.rfind(sep)
        if pos > limit // 2:
            return window[:pos].rstrip() + "…"
    return window + "…"


# ── Signal-store import (deferred — evolve-analyzer is installed at runtime) ──


def _signals_store():
    """Lazy import so unit tests / lint don't need the analyzer package."""
    return importlib.import_module("signals.store")


def _auto_remediating():
    """Lazy import of the auto-remediating registry (analyzer package).

    Deferred for the same reason as ``_signals_store`` — the admin package
    must import cleanly without evolve-analyzer present (it's installed at
    runtime)."""
    return importlib.import_module("signals.auto_remediating")


# ── Config readers ──────────────────────────────────────────────────────────


def _read_excluded_producers(shared_dir: Path) -> frozenset[str]:
    """Return the set of producers whose Signals this notifier will NOT push.

    The deny-list defaults to ``_DIRECT_DISPATCH_PRODUCERS`` — the two
    producers that already call ``dispatcher.send`` directly. Operators can
    extend the list via ``alerts.signal_notifier.excluded_producers`` in
    ``better-engine-config.json`` to silence any producer they don't want
    surfaced in chat.

    The old ``alerts.signal_notifier.producers`` allowlist key is no longer
    read; this is the single source of truth.
    """
    raw = _config_lookup.lookup(
        shared_dir,
        "alerts.signal_notifier.excluded_producers",
        list(_DIRECT_DISPATCH_PRODUCERS),
    )
    if isinstance(raw, list):
        return frozenset(str(x) for x in raw)
    return _DIRECT_DISPATCH_PRODUCERS


def _read_grace_by_severity(shared_dir: Path) -> dict[str, int]:
    """Per-severity delivery grace (seconds), defaults-merged + alert-clamped.

    Generalizes the previous hardcoded ~240s debounce into the operator's
    "time threshold for issues" (spec-delta-transient-delivery-grace L3). A
    firing Signal younger than ``table[severity]`` is held; ``alert`` is
    clamped to 0 so a critical is never delayed. Delegates to the shared
    ``alerts.grace`` policy so signal_notifier and digest_dispatcher agree on
    one definition of "within grace". Operator-tunable via
    ``alerts.grace_seconds_by_severity``."""
    return _grace.read_grace_seconds_by_severity(shared_dir)


_DEFAULT_FLAP_WINDOW_SECONDS = 1800   # 30 min — see _read_flap_window_seconds
_STATE_RETENTION_SECONDS = 86_400     # keep resolved-signature entries 24h

# A fire OR resolve dispatch counts as "delivered" (and so must be state-marked
# to stop re-pushing) for any outcome that reached a handling lane — not just
# SENT. DEFERRED (digest queue) and BATCHED_RATE_CAP (storm rate-breaker) are
# handled: the message is in the digest/batch queue and the operator gets it on
# the next flush. Marking ONLY on SENT (or SENT+DEFERRED, missing BATCHED) meant
# that in storm mode — where every send is deferred or rate-batched — the
# signature was never marked, so the SAME fire / "Cleared …" resolve re-pushed
# every ~70s tick. The re-pushes kept the dispatch rate above the storm
# threshold, which kept batching, which kept them unmarked: a storm that fed
# itself. Used by Phase A (fires), Phase B/B2 (resolves), and the delivery-down
# recovery probe. FAILED (transient) and SUPPRESSED_* (disabled / cooldown)
# intentionally stay unmarked so the message retries / re-announces later.
_HANDLED_DISPATCH_RESULTS = (
    _dispatcher.DispatchResult.SENT,
    _dispatcher.DispatchResult.DEFERRED,
    _dispatcher.DispatchResult.BATCHED_RATE_CAP,
)


def _read_flap_window_seconds(shared_dir: Path) -> int:
    """Per-signature flap-suppression window.

    After a recovery is announced, a fresh fire of the same signature
    within this window is treated as flap and stays out of chat
    (Alerts page still records every transition). The window resets
    on the next clean resolve outside it.

    Default 1800s (30 min) matches the observed flap cadence on
    real-world Node-25-degraded gateways (see signal_notifier
    investigation 2026-05-21). Operator-tunable via
    ``alerts.signal_notifier.flap_window_seconds``.
    """
    raw = _config_lookup.lookup(
        shared_dir,
        "alerts.signal_notifier.flap_window_seconds",
        _DEFAULT_FLAP_WINDOW_SECONDS,
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_FLAP_WINDOW_SECONDS


# ── State file ──────────────────────────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "signals" / "notifier-state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "signatures": {}}


def _save_state(shared_dir: Path, state: dict[str, Any], *,
                now: datetime | None = None) -> None:
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Keep entries that are either currently-alerted, have a recent
    # resolve we still need for flap-window suppression, or carry a
    # recent permanent-failure marker we need to keep silencing retries.
    # Prune anything older than _STATE_RETENTION_SECONDS to bound the
    # file size.
    now = now or datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=_STATE_RETENTION_SECONDS)).isoformat()
    sigs = state.get("signatures", {})
    kept: dict[str, Any] = {}
    for k, v in sigs.items():
        if v.get("alerted_for_signal_id"):
            kept[k] = v
            continue
        if v.get("permanent_failure_signal_id"):
            # Keep permanent-failure markers within the retention window
            # so the next minute's tick doesn't retry the same broken
            # signal id. They age out naturally with last_permanent_failure_at.
            last_perm = v.get("last_permanent_failure_at")
            if last_perm and last_perm >= cutoff_iso:
                kept[k] = v
                continue
        last_resolve = v.get("last_resolve_pushed_at")
        if last_resolve and last_resolve >= cutoff_iso:
            kept[k] = v
    state["signatures"] = kept
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Stats (returned from run_once for tests + observability) ────────────────


@dataclass
class NotifierStats:
    fired: int = 0
    deferred: int = 0
    debounced: int = 0
    flap_suppressed: int = 0
    # Auto-remediating suppression (spec-delta-transient-delivery-grace L2): a
    # registered self-healing condition (e.g. pod_perms_drift) still inside its
    # self-heal window — page on the FAILURE of the auto-fix, not the condition.
    auto_remediating_suppressed: int = 0
    permanent_failure: int = 0
    permanent_failure_skipped: int = 0
    recovered: int = 0
    unannounced_recovered: int = 0  # M2: resolve pushed for a never-announced fire
    escalation_announced: int = 0   # M2: warn→alert re-announce (one per incident)
    orphan_recoveries_suppressed: int = 0
    cooldown_suppressed: int = 0
    disabled_suppressed: int = 0
    other_suppressed: int = 0
    failed: int = 0
    skipped_direct_dispatch: int = 0  # producer is on the deny-list (direct-dispatch)
    # Digest-on-recovery (#3152): the delivery target went down (chat
    # not found / blocked / bad token); we stopped per-signal pushes and
    # switched to a single quiet digest on recovery.
    delivery_down_skipped: int = 0    # firing signals not pushed while target down
    delivery_recovered: int = 0       # 1 when this tick delivered the recovery digest
    delivery_digest_count: int = 0    # backlog size announced in that digest


# ── Rendering ───────────────────────────────────────────────────────────────


_CHECK_ID_PAREN_RE = re.compile(r"^\(\s*([\w.\-]+)\s*\)\s*:\s*")


def _strip_bot_prefix(text: str, bot_id: str | None) -> str:
    """Defend against producers that bake the bot name into Signal.title /
    Signal.body. Without this, ``_render_fire`` prepends another
    ``{bot_id}:`` on top of an already-bot-prefixed title, producing
    ``team_bot_c: team_bot_c (gateway...)``-style chat messages.

    Strips a leading ``{bot_id} `` or ``{bot_id}:`` (case-insensitive,
    optional trailing colon/space) from the start of ``text``. Returns
    ``text`` unchanged when there's no match. ``bot_id`` of ``None`` is
    a pod-scoped Signal — no stripping applies.

    The producer-side fix (don't put bot_id in titles) is the right
    long-term answer; this is the render-side defense so old producers
    don't spam the operator while the producer-side audit lands.
    """
    if not bot_id or not text:
        return text
    lower = text.lower()
    prefix = bot_id.lower()
    if not lower.startswith(prefix):
        return text
    tail = text[len(prefix):]
    # Only strip a BARE prefix — bot_id followed by a separator. A
    # possessive ("team_bot's 7:00 briefing didn't go out…", the
    # delivery-monitor §9.2 copy) is prose, not a redundant prefix;
    # stripping it would mangle the body to "'s 7:00 briefing…".
    if tail and tail[0] not in " :\t":
        return text
    # Strip any single ":" or whitespace separator between bot_id and rest.
    while tail and tail[0] in " :\t":
        tail = tail[1:]
    return tail or text  # don't collapse to empty — fall back to original


def _strip_check_id_paren(text: str) -> str:
    """Drop a leading "({check_id}):" parenthetical from a title.

    OC security audit findings flow in with ``message="{bot_id}
    ({check_id}): {description}"`` (see audit.py::audit_oc_security).
    After ``_strip_bot_prefix`` removes the bot_id, the "({check_id}):"
    paren is left dangling at the head of the line — visually noisy
    and confusing in chat ("🟢 team_bot_c: (gateway.probe_failed): Gateway
    probe failed (deep) — resolved"). Strip it so the title leads
    with the description.

    Conservative match: requires the parenthetical to look like a
    check_id (word chars, dots, hyphens — no spaces) so legitimate
    titles that happen to start with a parenthetical aside aren't
    eaten by accident.
    """
    if not text:
        return text
    return _CHECK_ID_PAREN_RE.sub("", text, count=1)


def _clean_title(title: str, bot_id: str | None) -> str:
    """Normalize a Signal.title for chat: strip bot prefix + check_id
    parenthetical. Both fire and resolve paths use this so the two
    sides of a transition share the same headline shape."""
    return _strip_check_id_paren(_strip_bot_prefix(title or "", bot_id))


def _render_fire(sig: Any) -> str:
    """Build the fire-state chat message.

    Returns HTML-safe text — every value derived from the Signal
    (title, body, bot_id) is escaped via ``catalog.html_escape`` so
    arbitrary content can't break the dispatcher's parse_mode="HTML"
    send. The dispatcher sets parse_mode at the wire layer; producers
    don't need to repeat the choice here, but they DO need to emit
    HTML-safe text in case the dispatcher is in HTML mode (which it is
    as of PR 2 of the dispatcher-safety rework).
    """
    emoji = _EMOJI_BY_SEVERITY.get(sig.severity, "⚠️")
    title = html_escape(_clean_title(sig.title or "", sig.bot_id))
    bot_id = html_escape(sig.bot_id) if sig.bot_id else None
    head = f"{emoji} {bot_id}: {title}" if bot_id else f"{emoji} {title}"

    body = (sig.body or "").strip()
    if body:
        body_norm = _strip_check_id_paren(
            _strip_bot_prefix(body, sig.bot_id)
        ).strip()
        # Skip body when it's substantively the same as the title — the
        # producer wrote the same string twice. (Common pattern in
        # audit / pod_health / security_warden producers as of 2026-05.)
        if body_norm and body_norm.lower() != _clean_title(sig.title or "", sig.bot_id).strip().lower():
            body_norm = _truncate_at_boundary(body_norm, _BODY_EXCERPT_CHARS)
            return f"{head}\n{html_escape(body_norm)}"
    return head


def _render_resolve(sig: Any) -> str:
    """Recovery messages frame the news affirmatively.

    The firing-side title is producer-authored for the *broken* state
    ("Gateway probe failed (deep)") — pairing that with "— resolved"
    on a green dot reads as a contradiction. We keep the producer's
    title (so the operator recognizes which finding cleared) but lead
    with "Cleared on {bot}:" so the recovery framing reaches the
    operator before the failure-tense title does.

    Signals that carry ``details.recovery`` (the delivery monitor writes
    it before resolving — spec §7/§9.2) render the truthful late-delivery
    story instead of the generic "Cleared" line.

    HTML-escaped on the way out — see ``_render_fire`` for rationale.
    """
    details = getattr(sig, "details", None) or {}
    recovery = details.get("recovery") or {}
    if isinstance(recovery, dict) and recovery.get("summary") and details.get("app_name"):
        return _render_delivery_recovery(sig, details, recovery)
    title = html_escape(_clean_title(sig.title or "", sig.bot_id))
    if sig.bot_id:
        bot_id = html_escape(sig.bot_id)
        return f"{_RECOVERY_EMOJI} Cleared on {bot_id}: {title}"
    return f"{_RECOVERY_EMOJI} Cleared: {title}"


def _render_delivery_recovery(sig: Any, details: dict, recovery: dict) -> str:
    """§9.2 flagship copy, rendered from ``details.recovery``.

    The producer composed ``recovery.summary`` from what actually
    happened ("Evolve restarted it, and it was delivered at 7:40." /
    "The computer was asleep during the window; …") — this renderer owns
    only the frame. Plex test: app display name + schedule, no
    producer/launchd/path jargon.
    """
    app = html_escape(str(details.get("app_name")))
    sched = html_escape(str(details.get("schedule_human") or "scheduled"))
    summary = html_escape(str(recovery.get("summary")))
    head = f"{_RECOVERY_EMOJI} {app} — late today, now delivered"
    if sig.bot_id:
        whose = f"{html_escape(sig.bot_id)}'s {sched}"
    else:
        whose = f"The {sched}"
    return (
        f"{head}\n"
        f"{whose} {app} didn't go out on time. {summary}\n"
        "No action needed."
    )


# ── Catalog-event mapping (Phase D) ─────────────────────────────────────────


def _catalog_event_for_signal(sig: Any) -> str:
    """Map a Signal to a catalog event_key for subscription gating.

    Phase D wires per-Signal subscription preferences into signal_notifier
    so an operator who mutes ``system.gateway_state_change`` doesn't get
    gateway-down chat messages even though the signal_notifier source is
    enabled.

    **Totality invariant (subscription-completeness keystone, spec-
    subscription-completeness-2026-06-24):** this function NEVER returns
    ``None``. Every dispatched message must carry a ``catalog_event`` so the
    operator has a subscription handle for it — no message reaches a channel
    unsubscribable. Any producer/type that doesn't match a specific mapping
    falls through to the ``meta.unclassified`` catch-all, which since the
    digest-default work (spec-subscription-digest-default-2026-06-28) batches
    into the daily digest rather than pushing loud-immediate — the operator
    still sees an unmapped condition, but it can't drive a push flood. The
    producer-totality test in ``test_alerts_signal_notifier`` enforces this
    against every known producer.

    Mapping is by producer + (loose) type-substring matching. Adjust as
    the catalog grows; an unknown mapping is never an error.
    """
    producer = getattr(sig, "producer", None)
    sig_type = getattr(sig, "type", "") or ""

    if producer == "pod_health":
        # Crash-loop / port-collision: the gateway is effectively down and
        # won't come back on its own — the "didn't auto-restart" event is the
        # operator subscription that fits (more apt than the up/down toggle).
        if "crashloop" in sig_type or "port_collision" in sig_type:
            return "system.gateway_autorestart_failed"
        if "gateway" in sig_type:
            return "system.gateway_state_change"
        # Other pod_health categories (file_security, launchd, runtime,
        # repo_ownership, etc.) — watchdog-flavored from operator POV.
        return "system.watchdog_event"
    if producer == "host_health":
        return "system.watchdog_event"
    if producer == "integration_probe":
        return "system.watchdog_event"
    if producer == "error_reporter":
        return "system.daemon_error_spike"
    if producer == "audit":
        return "security.audit_finding"
    if producer == "security_warden":
        return "security.audit_finding"
    if producer == "watchdog":
        return "system.watchdog_event"
    if producer == "pod_report":
        # pod_report rolls heal incidents + audit + cost + telemetry into
        # pod-wide Signals. Map to the catalog event whose subscription
        # gate the operator would expect to control each type. Unmapped
        # types fall through to source-level gating (no per-event mute).
        if "gateway" in sig_type:
            return "system.gateway_state_change"
        if sig_type.startswith("audit"):
            return "security.audit_finding"
        if sig_type in ("metrics_outage", "pod_silent"):
            return "system.watchdog_event"
        # Unmapped pod_report types: watchdog-shaped from the operator's POV
        # (pod_report rolls pod-wide conditions). Falls under the same
        # umbrella as the host/runtime categories above rather than the
        # source-wide toggle, so the operator keeps a per-event handle.
        return "system.watchdog_event"
    # Producers added 2026-06-03 as part of the outage-allowlist
    # broadening. Each mapping picks the closest existing umbrella
    # catalog event (or a bespoke entry — see plugin/exec/stuck/session
    # below) so subscription gating works per-event instead of
    # collapsing to the source-wide signal_notifier toggle.
    if producer == "permission_monitor":
        # Autonomy-ladder types get their own subscription gates
        # (spec-autonomy-ladder §3.4); the perm_* family stays under
        # the security-findings umbrella.
        if sig_type == "autonomy_posture_drift":
            return "security.autonomy_posture_drift"
        if sig_type == "autonomy_backfill_review":
            return "security.autonomy_review"
        if sig_type == "autonomy_limit_hit":
            return "security.autonomy_limit_hit"
        if sig_type == "autonomy_demoted":
            return "security.autonomy_demoted"
        if sig_type == "autonomy_promotion_candidate":
            return "security.autonomy_promotion_candidate"
        return "security.audit_finding"
    if producer == "monitor_coverage":
        # monitor_silent is the meta-alert: another monitor stopped
        # emitting heartbeats. Watchdog-shaped from the operator's POV.
        return "system.watchdog_event"
    if producer == "oc_cli":
        # cli_misinvocation has an exact catalog entry; this is the
        # cleanest 1:1 mapping in the new batch.
        return "system.oc_cli_misinvocation"
    if producer == "bot_log_monitor":
        # max_auth_failure / discord_target_invalid / tool_delivery_failing
        # all describe a service logging an error pattern at elevated rate.
        return "system.daemon_error_spike"
    if producer == "bot_recovery_monitor":
        return "system.watchdog_event"
    # Bespoke catalog entries (2026-06-03 follow-up). plugin_monitor
    # emits 11 plugin-posture signal types under one umbrella event;
    # exec_outcome_watchdog's four detector families all live under one
    # "exec outcomes failing" subscription; stuck_proposal_monitor and
    # session_cost_monitor each have a 1:1 mapping.
    if producer == "plugin_monitor":
        return "system.plugin_health_issue"
    if producer == "exec_outcome_watchdog":
        return "system.exec_outcome_failure"
    if producer == "stuck_proposal_monitor":
        return "system.stuck_proposal"
    if producer == "session_cost_monitor":
        return "cost.session_budget_exceeded"
    # forge_cost_guard: emits via signals.store.observe (not dispatcher.send),
    # so routing here is safe. Maps to cost.forge_session_cap so the operator
    # can mute forge cost events independently of other cost monitors.
    if producer == "forge_cost_guard":
        return "cost.forge_session_cap"
    # forge_engine: apply-time outcomes. The scheduled-action materialize
    # failure (audit slate S2) is install-shaped — the app shipped but part
    # of its schedule never went live — so it rides system.app_install_failed
    # (same app_delivery subscription the operator already has for install
    # honesty). Deliberately NOT system.app_scheduled_work_failure: that is
    # the structural verifier's "cron fires but the work errors" runtime
    # finding. Other forge_engine types (cost overrun) stay on the catch-all.
    if producer == "forge_engine" and "scheduled_action" in sig_type:
        return "system.app_install_failed"
    # app_structural_verifier: openclaw cron assertions → scheduled-work
    # failure; other structural findings (file_missing, sha drift, etc.) →
    # security.audit_finding (the same umbrella used for audit + perm_monitor).
    if producer == "app_structural_verifier":
        if "openclaw_cron" in sig_type:
            return "system.app_scheduled_work_failure"
        return "security.audit_finding"
    # app_script_failure_audit: an agent-invoked app script failed at runtime
    # (the "(agent) failed" exec chip). Sibling producer to the agent-bypass
    # audit; both walk the same transcripts. Bespoke catalog event so the
    # operator can mute script-failure alerts independently. NOT direct
    # dispatch — loud by default under deny-list-by-default.
    if producer == "app_script_failure_audit":
        return "system.app_script_failure"
    # repo_puller_sudoers: sudoers-drift findings from the repo puller.
    # Dedicated IMMEDIATE event, NOT system.watchdog_event — the latter is
    # DAILY_DIGEST (flap-prone host warnings), which left the 2026-06-10
    # firing signal undelivered for 24h+ (deliveries: []). Dormant sudo
    # grants silently break features, so the operator needs an immediate
    # ping with the one-command recovery, not a digest line.
    if producer == "repo_puller_sudoers":
        return "system.sudoers_refresh_failed"
    # credential_access: Evolve is locked out of a bot's provider-credential
    # store and its own inline ACL heal already failed (#3477). Dedicated
    # IMMEDIATE event for the SAME reason as sudoers above — the digest-default
    # neighbours (system.watchdog_event, system.bot_file_unreadable) would bury
    # it, and on the primary bot this is the engine dark with a one-command fix.
    if producer == "credential_access":
        return "system.credential_store_unreadable"
    # cron_exit_monitor: a managed calendar job's last launchd run exited
    # non-zero (or the probe itself couldn't escalate). Infra-maintenance
    # alert — watchdog-shaped from the operator's POV.
    if producer == "cron_exit_monitor":
        return "system.watchdog_event"
    # delivery_monitor (U2.1): per-window delivery outcomes for scheduled
    # user-facing apps. Two bespoke catalog entries so the operator can
    # mute delivery alerts per-event without silencing the producer.
    # PR 2 of the spec adds the M2 announce_unannounced_resolve flag on
    # the missed event (the fast-heal single-🟢 path).
    if producer == "delivery_monitor":
        if sig_type == "app_delivery_unmeasurable":
            return "system.app_delivery_unmeasurable"
        # Pod-scope many-apps-many-bots escalation — must route before
        # the per-app catch-all so the operator can tune it separately.
        if sig_type == "pod_delivery_regression":
            return "system.pod_delivery_regression"
        return "system.app_delivery_missed"
    # send_surface_probe: the post-OC-upgrade delivery verification found
    # the send path broken (emitted from `evolve-admin menu upgrade`).
    if producer == "send_surface_probe":
        return "system.send_surface_broken"
    # oc_surface_drift: update_watcher diffed a candidate OC release's surface
    # against the installed one. Maps to the dedicated Updates catalog event so
    # operators can tune it independently of the version-available alerts.
    # Signal-store-only producer (never direct dispatch) — loud by default.
    if producer == "oc_surface_drift":
        return "updates.openclaw_surface_drift"
    # ── Subscription-completeness producers (2026-06-24) ────────────────
    # Three producers that previously fell through to the final `return
    # None` (~19% of live dispatches were catalog_event=None). Each now
    # carries a subscription handle so the operator can mute it per-event.
    if producer == "alerts_loop_monitor":
        # Meta-alert about the alerting system itself: a source is sending
        # the same message on repeat ("X is on repeat"), or the dispatcher
        # is failing to deliver. Both live under the new meta.* namespace.
        if sig_type == "dispatcher_failures":
            return "meta.dispatcher_health"
        return "meta.alert_repeat_loop"
    if producer == "content_scan":
        # "evolve can't read this file" is a distinct condition from "the file
        # is gone": almost always a transient permission/ACL flap, not content
        # loss. Its own (lower-severity, digest-default) catalog event so the
        # access-flap doesn't ride the louder identity-doc-missing subscription
        # and can be tuned/collapsed independently.
        if sig_type == "content_scan_file_unreadable":
            return "system.bot_file_unreadable"
        # Required per-bot identity/doc files (USER/IDENTITY/README/SOUL/
        # MEMORY/HEARTBEAT/TOOLS.md) genuinely missing, plus structural
        # anomalies / catalog drift in the bot's content. An identity-doc
        # gap means the bot is mis-set-up — its own catalog event so the
        # operator can tune it independently of the audit umbrella.
        return "system.identity_doc_missing"
    if producer == "pod_perms_drift":
        # The pod-wide permission/ownership tree drifted from the deployed
        # state between deploys (hourly drift monitor). Same operator
        # mental model as config drift — reuse that subscription.
        return "security.config_drift"
    # ── Digest-default classification (D4, spec-subscription-digest-default-
    # 2026-06-28) ────────────────────────────────────────────────────────
    # Four producers that previously fell through to meta.unclassified and,
    # because that catch-all WAS loud-immediate, drove much of the 2026-06-28
    # evo-vps push flood. Each now binds to a real, properly-categorized class
    # that defaults to DAILY_DIGEST (never a loud page) — see the catalog
    # entries for the rationale per class.
    if producer == "deploy_drift_monitor":
        # A bot is on a newer/older Evolve version than the admin server.
        # Self-clears on the next pull — batched, never paged.
        return "updates.version_skew"
    if producer == "session_economics":
        # Cache-efficiency / low-usage tuning hints (cache_invalidation_elevated,
        # cache_hit_rate_low, bot_unused). Cost-shaped, no enforcement.
        return "cost.session_economics"
    if producer == "code_quality_monitor":
        # Repo dev-process KPIs (fix-heavy scope, revert rate, same-day
        # fix-on-feat). A background development-health trend.
        return "meta.dev_health"
    if producer == "cascade_audit":
        # cascade_audit emits several types; only the silent-telemetry one is
        # a pod-health-shaped maintenance condition with a known one-command
        # fix. Other cascade_audit types (tier-routing disagreements, anomaly_*)
        # stay on the digested catch-all until they get their own class.
        if sig_type == "plugin_telemetry_failure":
            return "system.plugin_telemetry_silent"
        return "meta.unclassified"
    # ── Totality catch-all (subscription-completeness keystone) ─────────
    # Any producer/type not matched above routes here rather than returning
    # None. This is what guarantees every dispatched message carries a
    # subscription handle. New producers are digest-by-default (meta.unclassified
    # defaults on, DAILY_DIGEST) so an unmapped condition still reaches the
    # operator (batched, never a loud page) and is visible in the Subscriptions
    # UI, prompting a real mapping. The producer-totality test pins this
    # invariant.
    return "meta.unclassified"


def _announce_unannounced_resolve(event_key: str | None) -> bool:
    """The M2 honest-lifecycle flag for a mapped catalog event (§9.1)."""
    if not event_key:
        return False
    from .catalog import by_key
    event = by_key(event_key)
    return bool(event and event.announce_unannounced_resolve)


# How far back Phase B2 looks for resolved-but-never-announced Signals.
# Must comfortably exceed the tick cadence (1 min) so nothing slips
# between ticks; bounded so a notifier outage doesn't replay stale
# "now delivered" news hours later (state-marking dedups within the
# window; the bound handles beyond it).
_M2_RESOLVE_LOOKBACK_SECONDS = 3600


def _iter_recently_resolved(store: Any, shared_dir: Path, *, now: datetime):
    """Resolved Signals from the last hour, oldest files first.

    mtime-prefiltered (the archived file is written at resolve time) so
    the 1-minute tick stats the directory instead of parsing the whole
    90-day archive — same pattern as the store's own
    ``_find_recent_resolved_by_signature``.
    """
    root = store.signals_root(shared_dir) / "archived"  # store-access-lint: store-internal mtime-prefiltered archived scan (same pattern as store._find_recent_resolved_by_signature)
    if not root.exists():
        return
    cutoff_dt = now - timedelta(seconds=_M2_RESOLVE_LOOKBACK_SECONDS)
    cutoff_ts = cutoff_dt.timestamp()
    for path in sorted(root.glob("*.json")):
        try:
            if path.stat().st_mtime < cutoff_ts:
                continue
        except OSError:
            continue
        sig = store.load_signal_file(path)
        if sig is None or sig.state != "resolved":
            continue
        resolved_at = _parse_iso(getattr(sig, "resolved_at", None))
        if resolved_at is None or resolved_at < cutoff_dt:
            continue
        yield sig


# ── Time helpers ────────────────────────────────────────────────────────────


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _digest_pair_meta(sig: Any, *, kind: str) -> dict[str, Any]:
    """Cancellation metadata for the digest fire/clear pairing (L1).

    A digest-routed fire or resolve carries this so
    ``digest_dispatcher`` can pair the two and elide a within-grace
    transient (spec-delta-transient-delivery-grace L1). The pairing key is
    the Signal ``signature`` (stable across the fire→resolve lifecycle); the
    ``signal_severity`` is the Signal's OWN severity (not the INFO wrapper a
    resolve send uses) so the digest's never-cancel-an-alert gate sees the
    truth. The resolve side also carries ``lifetime_seconds`` (created→
    resolved) — the digest cancels only when the lifetime was shorter than
    the severity grace.

    ``bot_id`` (None for pod/host-scoped signals) rides on both the fire and
    the resolve so the digest's net-state recovered-flap roll-up
    (``digest_dispatcher._rollup_recovered_flaps``) can group several
    same-event recoveries on one bot into a single retrospective line. Pure
    metadata; ignored on the immediate-push path."""
    meta: dict[str, Any] = {
        "coalesce_key": sig.signature,
        "kind": kind,
        "signal_severity": sig.severity,
        "bot_id": getattr(sig, "bot_id", None),
    }
    if kind == "resolve":
        created = _parse_iso(getattr(sig, "created_at", None))
        resolved = _parse_iso(getattr(sig, "resolved_at", None))
        if created is not None and resolved is not None:
            meta["lifetime_seconds"] = max(
                0.0, (resolved - created).total_seconds()
            )
    return meta


def _iso_z(now: datetime) -> str:
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Main entrypoint ─────────────────────────────────────────────────────────


def _sync_firing_as_known(
    store: Any,
    shared_dir: Path,
    state: dict[str, Any],
    denylist: frozenset[str],
    now: datetime,
    *,
    stats: NotifierStats | None = None,
    reason: str = "first_run",
) -> int:
    """Record every currently-firing, non-denylisted Signal as already-
    alerted *without* pushing. Shared by the first-run cold-start guard and
    the digest-on-recovery resync — both want the same outcome: "the current
    backlog is known; only announce transitions from here." Returns the
    number of signatures synced.

    Uses the canonical cold-start entry shape (``cold_start_synced: True``)
    so the two call sites stay in lockstep.

    **Observability trail (2026-07-29).** Every signature this synthesizes
    is a Signal the operator will never be told about individually — it is
    firing, so it never re-fires, so Phase A never announces it. That is a
    deliberate anti-flood choice, but for two months it was
    indistinguishable from a broken delivery path during debugging. Each
    entry now carries ``cold_start_synced_at`` / ``never_announced`` /
    ``cold_start_reason``, a pod-level ``cold_start`` record is appended to
    the state file (bounded to the last few syncs), and the count is logged
    at WARNING. ``standing_alerts`` reads the same markers to report "N of
    these were never announced". Do NOT drop the markers on a later real
    push — Phase A rewrites the entry from scratch, which is what flips a
    signature from silenced to announced.
    """
    synced = 0
    for sig in store.iter_signals(shared_dir, subdirs=("firing",)):
        if sig.producer in denylist:
            if stats is not None:
                stats.skipped_direct_dispatch += 1
            continue
        state["signatures"][sig.signature] = {
            "alerted_for_signal_id": sig.id,
            "last_fire_pushed_at": _iso_z(now),
            "last_resolve_pushed_at": None,
            "cold_start_synced": True,
            # Trail: this signature was silenced, not delivered.
            "cold_start_synced_at": _iso_z(now),
            "cold_start_reason": reason,
            "never_announced": True,
        }
        synced += 1
    if synced:
        _record_cold_start(state, now, synced=synced, reason=reason)
        _log.warning(
            "signal_notifier cold-start (%s): %d firing Signal(s) recorded as "
            "already-known WITHOUT any operator message at %s. They will not "
            "be announced individually — the standing-alerts section of the "
            "daily pod report is what reports them.",
            reason, synced, _iso_z(now),
        )
    return synced


# How many cold-start records to retain in the state file. Enough to see a
# pattern (a notifier whose state file keeps getting wiped, a delivery
# target that flaps down/up) without letting the list grow forever.
_COLD_START_TRAIL_MAX = 10


def _record_cold_start(
    state: dict[str, Any], now: datetime, *, synced: int, reason: str,
) -> None:
    """Append a pod-level record of a cold-start sync to the state file.

    The per-signature markers say *which* Signals were silenced; this says
    *when and how many*, and survives the per-signature pruning in
    ``_save_state``. A debugger asking "did the operator ever get told?"
    reads this first.
    """
    trail = state.get("cold_start")
    if not isinstance(trail, list):
        trail = []
    trail.append({
        "at": _iso_z(now),
        "reason": reason,
        "synced_count": synced,
        "announced": False,
        "note": (
            "Recorded as already-known without pushing; reported by the "
            "standing-alerts section of the daily pod report."
        ),
    })
    state["cold_start"] = trail[-_COLD_START_TRAIL_MAX:]


def _attempt_delivery_recovery(
    store: Any,
    shared_dir: Path,
    network: dict[str, Any] | None,
    state: dict[str, Any],
    denylist: frozenset[str],
    now: datetime,
    stats: NotifierStats,
) -> NotifierStats:
    """Probe a down delivery target and, on recovery, emit ONE quiet digest.

    Called only when a prior tick flagged ``state["delivery_down"]`` (a
    channel-level permanent failure — bot not /start-ed, blocked, kicked,
    bad token). While the target is down we deliberately do NOT run the
    normal per-signal phases: every send would fail against the dead target,
    and the moment the operator fixes it the whole accumulated backlog would
    announce at once (the #3152 flood).

    The digest doubles as the recovery probe: while the target is down this
    single send FAILS and reaches no one (so still no flood); the first tick
    it SUCCEEDS is, by definition, the down→up transition. On that success we
    re-sync the firing backlog as already-known (cold-start style) so it is
    never replayed individually — only NEW fires announce from here.
    """
    backlog = [
        sig for sig in store.iter_signals(shared_dir, subdirs=("firing",))
        if sig.producer not in denylist
    ]
    n = len(backlog)
    if n == 0:
        # Nothing accumulated during the outage — clear the marker and let
        # normal processing resume next tick. If the target is in fact still
        # down, the next tick's Phase A re-arms the marker on its first
        # failed send. (No probe sent: there's nothing to digest, and a bare
        # "recovered" ping for an empty backlog would be noise.)
        state.pop("delivery_down", None)
        return stats

    digest = (
        f"📦 {n} alert{'s' if n != 1 else ''} accumulated while delivery "
        "was down — see the Alerts page."
    )
    outcome = _dispatcher.send(
        shared_dir=shared_dir,
        network=network,
        source="signal_notifier",
        message=digest,
        severity=_dispatcher.Severity.INFO,
        dedup_key=None,   # no cooldown — the probe must be free to retry each tick
        # Subscription-completeness: this is a meta message ABOUT the
        # alerting system (a backlog accumulated while delivery was down),
        # so it carries the meta.digest handle. meta.digest is IMMEDIATE-
        # only + default-on, so binding it can't suppress this recovery
        # probe nor re-enqueue it into a digest queue.
        catalog_event="meta.digest",
    )
    if outcome.result in _HANDLED_DISPATCH_RESULTS:
        _sync_firing_as_known(
            store, shared_dir, state, denylist, now,
            reason="delivery_recovery",
        )
        state.pop("delivery_down", None)
        stats.delivery_recovered = 1
        stats.delivery_digest_count = n
    else:
        # Still down (probe failed) or no recipient — stay silent, keep the
        # marker, probe again next tick. No operator message went out.
        stats.delivery_down_skipped += n
    return stats


def run_once(
    shared_dir: Path,
    network: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> NotifierStats:
    """Single notifier tick. Idempotent — safe to call from a 1-min cron.

    Phase B (recovery) runs first so a cleared signature releases its
    state slot before Phase A (fire) checks debounce on a re-fire of the
    same signature.

    **Cold-start protection** — when ``notifier-state.json`` is absent
    (first ever run after the source was enabled), every currently-
    firing Signal is recorded as already-alerted *without* pushing. The
    debounce window only protects against signal-side flap; without a
    notifier-side cold-start guard, enabling signal_notifier on a pod
    that's been quietly accumulating Signals would dump the entire
    backlog to the operator's chat in one tick. signal_notifier is for
    transitions, not inventory.

    The consequence of that guard is permanent for the synced set: those
    Signals stay firing, so they never re-fire, so Phase A never announces
    them. "Operators see the backlog on the Alerts page" was the original
    justification and it was not enough — nobody opens a page they have no
    reason to open. The **standing-alerts section of the daily pod report**
    (``evolve_admin.alerts.standing_alerts``) is the inventory channel that
    now covers this, and ``_sync_firing_as_known`` leaves a
    ``cold_start`` trail plus per-signature ``never_announced`` markers so
    the silence is legible after the fact.
    """
    now = now or datetime.now(timezone.utc)

    # Early-exit when the operator has disabled the signal_notifier
    # source. The dispatcher's per-source enable gate would suppress
    # every dispatch anyway, but every suppression appends a record to
    # ``alerts/dispatcher-suppressed/<day>.jsonl``. At ~25 firing
    # Signals × 1440 ticks/day this generates ~36k log lines/day even
    # though no chat message ever goes out — the 2026-06-01 disk-fillup
    # incident root cause (803,266 suppressed lines / 306 MB over 22
    # days, 98.8% from source_disabled:signal_notifier).
    #
    # The dispatcher gate stays in place as defense-in-depth for direct
    # callers; this short-circuits the producer-side fanout that turned
    # a quiet operator preference into runaway log growth.
    if not _config_lookup.lookup(
        shared_dir, "alerts.signal_notifier.enabled", True,
    ):
        return NotifierStats()

    cold_start = not _state_path(shared_dir).exists()
    state = _load_state(shared_dir)
    state.setdefault("signatures", {})
    denylist = _read_excluded_producers(shared_dir)
    grace_by_severity = _read_grace_by_severity(shared_dir)
    flap_window_seconds = _read_flap_window_seconds(shared_dir)
    stats = NotifierStats()

    store = _signals_store()

    if cold_start:
        # First run after enable. Don't push anything — just sync state
        # so subsequent ticks treat the backlog as known. New fires that
        # happen after this point go through the normal Phase A path.
        # Direct-dispatch producers are still excluded from the backlog
        # sync (they already messaged via their own path).
        _sync_firing_as_known(
            store, shared_dir, state, denylist, now, stats=stats,
        )
        # M2 backlog: mark recent resolves of opted-in events as already
        # announced, or the very next tick's Phase B2 would replay up to
        # an hour of pre-enable "now delivered" news. Same inventory-vs-
        # transitions rationale as the firing sync above.
        for sig in _iter_recently_resolved(store, shared_dir, now=now):
            if sig.producer in denylist:
                continue
            if not _announce_unannounced_resolve(_catalog_event_for_signal(sig)):
                continue
            entry = state["signatures"].setdefault(sig.signature, {
                "alerted_for_signal_id": None,
                "last_fire_pushed_at": None,
                "cold_start_synced": True,
            })
            entry["last_resolve_pushed_at"] = _iso_z(now)
            entry["resolve_pushed_for_signal_id"] = sig.id
        _save_state(shared_dir, state, now=now)
        return stats

    # ── Digest-on-recovery — quiet re-sync when a down target heals (#3152) ──
    # If a prior tick flagged the delivery target as down (chat-level
    # permanent failure — bot not /start-ed, blocked, kicked, bad token),
    # skip the normal per-signal phases entirely: every send would just fail
    # against the dead target, and on recovery the accumulated backlog would
    # all announce at once. Probe with ONE digest instead.
    if state.get("delivery_down"):
        stats = _attempt_delivery_recovery(
            store, shared_dir, network, state, denylist, now, stats,
        )
        _save_state(shared_dir, state, now=now)
        return stats

    # ── Phase B: announce recoveries for previously-pushed Signals ──────
    for signature, st in list(state["signatures"].items()):
        sig_id = st.get("alerted_for_signal_id")
        if not sig_id:
            continue
        found = store.find_signal(shared_dir, sig_id)
        if found is None:
            # Signal vanished (manual delete, retention prune). Drop the
            # state entry so a future re-fire isn't blocked on stale state.
            state["signatures"].pop(signature, None)
            continue
        sig, _path, _subdir = found
        if sig.state == "firing":
            continue  # still active; nothing to announce
        if sig.state == "snoozed":
            continue  # snoozed by operator; recovery announcement deferred
        # resolved or dismissed → push recovery (no dedup_key → no cooldown)
        outcome = _dispatcher.send(
            shared_dir=shared_dir,
            network=network,
            source="signal_notifier",
            message=_render_resolve(sig),
            severity=_dispatcher.Severity.INFO,
            dedup_key=None,
            catalog_event=_catalog_event_for_signal(sig),
            digest_meta=_digest_pair_meta(sig, kind="resolve"),
        )
        if outcome.result in _HANDLED_DISPATCH_RESULTS:
            # Retain the state entry instead of popping. Setting
            # alerted_for_signal_id=None marks "no current alert"; the
            # last_resolve_pushed_at timestamp drives flap-window
            # suppression on any subsequent re-fire of this signature.
            # resolve_pushed_for_signal_id keeps Phase B2 (M2) from
            # announcing the same resolve a second time. _save_state
            # ages out entries older than 24h, so this doesn't grow the
            # file unboundedly.
            state["signatures"][signature] = {
                "alerted_for_signal_id": None,
                "last_fire_pushed_at": st.get("last_fire_pushed_at"),
                "last_resolve_pushed_at": _iso_z(now),
                "resolve_pushed_for_signal_id": sig_id,
            }
            stats.recovered += 1
        elif outcome.result == _dispatcher.DispatchResult.FAILED:
            stats.failed += 1
            # Leave state in place so we retry next tick.
        else:
            stats.other_suppressed += 1
            # Suppressed (disabled / cooldown): leave state so the recovery
            # is announced once the operator re-enables. (DEFERRED and
            # BATCHED_RATE_CAP are handled above — they reached the digest/
            # batch lane, so they must NOT fall here or Phase B re-pushes
            # the same resolve every tick.)

    # ── Phase B2 (M2): announce resolves whose fire was never pushed ────
    # spec-proactive-delivery-monitor §9.1: a miss that heals inside the
    # debounce window resolves before Phase A ever announced it. The
    # orphan-suppression gate would keep that recovery silent — which is
    # exactly the "fixed itself, silently" experience the delivery spec
    # exists to kill. Catalog events that opt in via
    # announce_unannounced_resolve get the resolve pushed anyway,
    # rendered from details.recovery, exactly once per signal id
    # (state-marked), bounded to the last hour of resolves.
    for sig in _iter_recently_resolved(store, shared_dir, now=now):
        if sig.producer in denylist:
            continue
        event_key = _catalog_event_for_signal(sig)
        if not _announce_unannounced_resolve(event_key):
            continue
        entry = state["signatures"].get(sig.signature) or {}
        if entry.get("resolve_pushed_for_signal_id") == sig.id:
            continue  # already announced (by either phase)
        if entry.get("alerted_for_signal_id") == sig.id:
            continue  # announced fire — Phase B owns (and retries) its resolve
        outcome = _dispatcher.send(
            shared_dir=shared_dir,
            network=network,
            source="signal_notifier",
            message=_render_resolve(sig),
            severity=_dispatcher.Severity.INFO,
            dedup_key=None,
            catalog_event=event_key,
            digest_meta=_digest_pair_meta(sig, kind="resolve"),
        )
        if outcome.result in _HANDLED_DISPATCH_RESULTS:
            state["signatures"][sig.signature] = {
                **entry,
                "alerted_for_signal_id": None,
                "last_resolve_pushed_at": _iso_z(now),
                "resolve_pushed_for_signal_id": sig.id,
            }
            stats.unannounced_recovered += 1
        elif outcome.result == _dispatcher.DispatchResult.FAILED:
            stats.failed += 1  # transient — unmarked, retried next tick
        else:
            stats.other_suppressed += 1

    # ── Phase A: announce new fires (with debounce + flap suppression) ──
    for sig in store.iter_signals(shared_dir, subdirs=("firing",)):
        if sig.producer in denylist:
            # Direct-dispatch producer — already messages the operator on
            # its own path. Skip to avoid double-messaging.
            stats.skipped_direct_dispatch += 1
            continue
        if state.get("delivery_down"):
            # The target went down earlier in THIS tick (a channel-level
            # permanent failure flipped the marker mid-loop). Stop hammering
            # the dead target with per-signal sends — bound the burst to the
            # one send that detected the outage. The next tick's recovery
            # probe handles the whole backlog at once.
            stats.delivery_down_skipped += 1
            continue
        existing = state["signatures"].get(sig.signature) or {}
        escalation_repush = False
        if existing.get("alerted_for_signal_id") == sig.id:
            # Already pushed for this Signal id. M2 events get ONE
            # re-announce when the producer escalates the same Signal to
            # alert (delivery monitor §8: "the restart didn't work" — a
            # binding follow-up, so it can't stay chat-silent). Everyone
            # else keeps escalate-without-re-paging. Entries written
            # before alerted_severity existed (None) are treated as
            # already-alert so a deploy doesn't re-page old incidents.
            escalation_repush = (
                sig.severity == "alert"
                and existing.get("alerted_severity") in ("info", "warn")
                and _announce_unannounced_resolve(_catalog_event_for_signal(sig))
            )
            if not escalation_repush:
                continue  # already pushed for this Signal id

        # Permanent-failure suppression — if a prior push for THIS exact
        # signal id failed with an HTTP 4xx-class error (parse error,
        # invalid chat_id, revoked token), retrying the same message
        # body will fail the same way. Skip until the signal id changes
        # (audit's next sweep_resolve + re-fire creates a fresh id) or
        # the operator intervenes. This is the cure for the per-minute
        # retry loop signal_notifier hit on 2026-05-21 when the catalog
        # subscription footer's underscores broke Telegram Markdown parsing.
        if existing.get("permanent_failure_signal_id") == sig.id:
            stats.permanent_failure_skipped += 1
            continue

        # Flap suppression — if this signature was recently resolved
        # and is now firing again inside the operator-configured window,
        # treat it as flap and stay out of chat. The Alerts page still
        # records every transition; chat is for stable transitions
        # only. Real-world case: gateway.probe_failed flapped every
        # 1.5-2h on a Node-25-degraded gateway (2026-05-20 team_bot_c).
        # Without this, every cycle generated a ⚠️ + 🟢 pair.
        last_resolve = _parse_iso(existing.get("last_resolve_pushed_at"))
        if last_resolve is not None and flap_window_seconds > 0:
            since_resolve = (now - last_resolve).total_seconds()
            if 0 <= since_resolve < flap_window_seconds:
                stats.flap_suppressed += 1
                continue

        created = _parse_iso(sig.created_at)
        if created is None:
            continue  # malformed — skip rather than crash
        age = (now - created).total_seconds()

        # Auto-remediating suppression — "page on the failure of the auto-fix,
        # not on the condition" (spec-delta-transient-delivery-grace L2).
        # A registered self-healing type (e.g. pod_perms_drift — the next
        # deploy's ensure_pod_perms re-applies the contract within ~15 min) is
        # withheld from chat until it has been firing LONGER than its self-heal
        # window and is STILL firing — i.e. until the scheduled fix has
        # demonstrably failed. A condition that self-clears inside the window
        # is recorded on the Alerts page (we never touch the store JSON) but
        # never pushed. This COMPOSES with flap_gate (producer-side dwell
        # proved the condition real; this proves it durable past its own
        # auto-fix) and is age-driven off the same created_at the grace check
        # below uses. alert/critical is NEVER suppressed — the registry helper
        # guards severity itself, so a critical that happened to share a
        # registered type still pages immediately.
        #
        # PRECEDENCE (the reason this runs BEFORE the per-severity grace gate):
        # the self-heal window (default 1800s) is wider than the warn/info
        # grace (default 900s), so a config_drift inside its window would
        # otherwise fall into the grace/``debounced`` branch and never reach
        # this check. Running it first means an auto-remediating condition is
        # classified as ``auto_remediating_suppressed`` (page-on-auto-fix-
        # failure semantics), not as a generic short-lived ``debounced`` blip —
        # exactly #3285's "don't page until it outlives its self-heal window".
        # The two compose: a registered type past its window falls through here
        # (suppress=False) and is then subject to the normal grace gate.
        #
        # ACT-SIDE FOLLOW-UP (out of scope, owned by edr/deploy): proactively
        # run ensure_pod_perms the moment drift is detected between deploys,
        # so the condition is *remediated* in minutes rather than merely
        # suppressed here until the next deploy. See the spec's §L2 act-side
        # note and signals/auto_remediating.py.
        try:
            if _auto_remediating().should_suppress(
                type_=getattr(sig, "type", None),
                severity=sig.severity,
                firing_age_seconds=age,
            ):
                stats.auto_remediating_suppressed += 1
                continue
        except Exception as exc:  # noqa: BLE001
            # Fail open — a registry import/logic error must never swallow a
            # firing signal. Log (don't silently swallow) and fall through to
            # the normal push path.
            _log.warning(
                "auto-remediating gate errored for signal %s; failing open: %s",
                getattr(sig, "id", "?"),
                exc,
            )

        # Per-severity grace (L3) — the operator's "time threshold for
        # issues". A firing Signal younger than its severity's grace is
        # held: if it self-resolves before the grace elapses, Phase B
        # never fires (we never set alerted_for), so the transient is
        # never pushed — exactly the "nothing to do" blip we want silent.
        # ``alert`` grace is clamped to 0 upstream, so a critical is never
        # delayed by this gate (the invariant). Runs AFTER the auto-
        # remediating gate above so a self-healing condition is never
        # mis-classified as a generic ``debounced`` blip.
        grace_seconds = grace_by_severity.get(
            sig.severity, grace_by_severity.get("warn", 0)
        )
        if age < grace_seconds:
            stats.debounced += 1
            continue

        severity = _SEVERITY_MAP.get(sig.severity, _dispatcher.Severity.WARNING)
        # No explicit cooldown_seconds — let the dispatcher read its
        # per-source default from config (alerts.signal_notifier.cooldown_seconds,
        # 600s). Operator can tune from the admin UI.
        outcome = _dispatcher.send(
            shared_dir=shared_dir,
            network=network,
            source="signal_notifier",
            message=_render_fire(sig),
            severity=severity,
            dedup_key=sig.signature,
            catalog_event=_catalog_event_for_signal(sig),
            # L1 pairing metadata — lets a digest-routed fire be matched
            # against its resolve and elided as a within-grace transient.
            digest_meta=_digest_pair_meta(sig, kind="fire"),
        )

        if outcome.result in _HANDLED_DISPATCH_RESULTS:
            # DEFERRED = accepted via the digest queue; BATCHED_RATE_CAP =
            # accepted via the storm rate-breaker. From signal_notifier's POV
            # both are the same as SENT — the operator gets it on the next
            # digest/batch flush. Without marking on these, the fire re-pushes
            # one record per ~70s tick for any firing Signal that is digest-
            # only (integration_probe Google_Workspace was the original canary,
            # ~1440/day) OR rate-batched in storm mode (the 2026-06-25 evo-vps
            # version-drift + error_spike fires: BATCHED was missing here even
            # after #3251 fixed the resolve paths — this is the fire-side twin).
            state["signatures"][sig.signature] = {
                "alerted_for_signal_id": sig.id,
                "alerted_severity": sig.severity,
                "last_fire_pushed_at": _iso_z(now),
                "last_resolve_pushed_at": existing.get("last_resolve_pushed_at"),
                "resolve_pushed_for_signal_id": existing.get(
                    "resolve_pushed_for_signal_id"
                ),
            }
            if outcome.result != _dispatcher.DispatchResult.SENT:
                stats.deferred += 1
            elif escalation_repush:
                stats.escalation_announced += 1
            else:
                stats.fired += 1
        elif outcome.result == _dispatcher.DispatchResult.SUPPRESSED_COOLDOWN:
            stats.cooldown_suppressed += 1
        elif outcome.result == _dispatcher.DispatchResult.SUPPRESSED_DISABLED:
            stats.disabled_suppressed += 1
        elif outcome.result == _dispatcher.DispatchResult.FAILED:
            if getattr(outcome, "is_permanent_failure", False):
                # Permanent failure — record the signal id so we don't
                # retry every minute. Stays in state until either the
                # producer creates a new signal id (audit cycle) or the
                # operator clears the entry manually.
                state["signatures"][sig.signature] = {
                    **existing,
                    "permanent_failure_signal_id": sig.id,
                    "last_permanent_failure_at": _iso_z(now),
                }
                stats.permanent_failure += 1
                # Channel-level permanent failure (chat not found / blocked /
                # kicked / bad token) means EVERY send to this target fails,
                # not just this message. Flip the pod-level delivery-down
                # marker so the next tick switches to quiet digest-on-recovery
                # instead of hammering each backlogged signal (the #3152
                # flood). A message-level permanent failure (HTML parse error,
                # bare http 4xx) leaves this unset → today's per-signal skip.
                if getattr(outcome, "is_delivery_target_down", False):
                    state.setdefault(
                        "delivery_down", {"down_since": _iso_z(now)},
                    )
            else:
                # Transient — state stays unchanged so next tick retries.
                stats.failed += 1
        else:
            stats.other_suppressed += 1

    _save_state(shared_dir, state, now=now)
    return stats


__all__ = ("run_once", "NotifierStats", "_DIRECT_DISPATCH_PRODUCERS")
