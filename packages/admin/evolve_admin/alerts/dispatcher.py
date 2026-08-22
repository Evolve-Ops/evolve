"""Sysadmin alert dispatcher.

One public entrypoint: :func:`send`. Every source that pushes to the
operator's chat channel routes through it so the operator has one place
to mute, throttle, and audit pod-wide notifications.

Per ``docs/spec-alert-notifier-2026-05-09.md``:

- per-source enable check (config-driven; default-on for all existing
  sources, default-off for ``signal_notifier`` until the operator opts in)
- per-``dedup_key`` cooldown (config-driven; sane defaults reflect the
  observed noise patterns — ``audit`` is 24h, ``signal_notifier`` is 10m)
- atomic state file under ``{shared_dir}/alerts/dispatcher-state.json``
- JSONL audit log + suppression log under the same dir
- recipient resolution from ``network.json::alerts`` with fallback to
  ``bots.<primary>.primary_channel`` then ``primary_user.external_ids``
  (primary bot resolved via ``primary_bot.primary_bot_id``)
- subprocess dispatch via ``openclaw message send`` — the existing send
  path used today by forge_engine and the operator-test endpoint

Phase 1 of the alert-notifier spec. The dispatcher is a library; sources
import it and call :func:`send`. No daemon of its own.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .. import channel_registry as _channel_registry
from .. import external_ids as _external_ids

# ─── Severity ────────────────────────────────────────────────────────────────


class Severity(str, Enum):
    """Severity levels carried on a dispatch call.

    Used as metadata for the audit log and (eventually) for severity-floor
    filtering. The dispatcher does NOT prepend severity emojis to the
    message — sources own their full message body. Severity exists so the
    audit log is queryable and so future per-severity gating is possible
    without re-plumbing every source.
    """

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# ─── Result ─────────────────────────────────────────────────────────────────


class DispatchResult(str, Enum):
    SENT = "sent"
    SUPPRESSED_DISABLED = "suppressed_disabled"
    SUPPRESSED_COOLDOWN = "suppressed_cooldown"
    # Identical-content floor (spec-alert-no-repeat-2026-06-01.md). Same
    # (source, dedup_key, body_hash) within 24h → suppressed regardless
    # of cooldown_seconds. STATE_TRACKED sources are exempt; everyone
    # else gets the floor as a structural guard against operator-set
    # short cooldowns plus a producer that keeps emitting identical
    # text. Anchored to the 2026-05-31 gateway-spam pattern
    # (6 identical messages in 73 minutes at the prior 600s heal cooldown).
    SUPPRESSED_IDENTICAL = "suppressed_identical"
    NO_RECIPIENT = "no_recipient"
    FAILED = "failed"
    # Phase A2: catalog-event with frequency=daily_digest/weekly_digest →
    # the message is appended to the digest queue and will be flushed
    # by the digest daemon (Phase G). The send call is "successful" in
    # the sense that the operator's preference was honored — they asked
    # for a digest, so they're getting one — but no chat push happened
    # yet for this individual event.
    DEFERRED = "deferred"
    # Global rate breaker (rate_breaker.py): the per-channel rolling-window
    # cap was exceeded (or the channel is in storm mode), so this alert was
    # rolled into the daily digest instead of pushed immediately. NOT a
    # drop — the message is enqueued to the digest and recorded in the
    # recent-messages log. The producer-agnostic backstop that no
    # per-producer gate can leak past.
    BATCHED_RATE_CAP = "batched_rate_cap"


# ─── Source categories (spec-alert-no-repeat-2026-06-01.md) ──────────────────


class SourceCategory(str, Enum):
    """Classifies a source by how identical-message repetition should
    be handled. Every entry in :data:`_DEFAULT_SOURCE_ENABLED` must
    have a matching :data:`_DEFAULT_SOURCE_CATEGORY` row; the test
    ``test_every_source_has_a_declared_category`` pins this.

    Picking a category answers two questions at once:

    1. *Does the underlying condition naturally produce a fresh event,
       or does the same condition persist while the producer keeps
       ticking?*
    2. *Does the source maintain its own per-item dedup state, or is
       it relying on the dispatcher's per-(source, dedup_key) state?*

    A binary "has cooldown / doesn't" mixes those two questions and
    permits incoherent declarations — heal's pre-PR-1904 600s cooldown
    is the canonical example. The category enum forces a coherent
    answer.
    """

    STATE_PERSISTS = "state_persists"
    """The condition stays true for long stretches; the producer ticks
    every few minutes; re-firing the same alert without operator action
    is noise. Default cooldown: 24h. Identical-content suppression: ON.

    Examples: audit findings, spend-over-threshold, stalled cron,
    gateway-down (heal), heartbeat-session bloat (cost_watchdog).
    """

    PER_EVENT_UNIQUE = "per_event_unique"
    """Each fire is a distinct event; dedup_keys naturally cycle per
    event (job ID, proposal ID, trip ID, comment ID, version, ISO
    week). Default cooldown: 0. Identical-content suppression: ON as
    a safety net only — it should never trigger in practice; if it
    does, the producer has a bug.

    Examples: forge job ready, proposal review/apply/outcome, breaker
    trip, repo-puller wedge, upstream-issue comment.
    """

    STATE_TRACKED = "state_tracked"
    """Source maintains its own per-item dedup state and decides when
    a re-announcement is warranted. Identical-content suppression:
    **OFF** — this is the only category that opts out of the 24h
    floor.

    Today only ``signal_notifier`` qualifies: it tracks
    ``alerted_for_signal_id`` per signature in ``notifier-state.json``
    so the same signature firing under a fresh Signal id (audit cycles
    create new ids) gets re-announced.

    Adding a new STATE_TRACKED source requires a justifying comment;
    "convenience" or "I want shorter cooldown" is not a justification —
    that's PER_EVENT_UNIQUE with an explicit ``cooldown_seconds`` override.
    """


_CATEGORY_DEFAULT_COOLDOWN_SECONDS: dict[SourceCategory, int] = {
    SourceCategory.STATE_PERSISTS: 86_400,
    SourceCategory.PER_EVENT_UNIQUE: 0,
    SourceCategory.STATE_TRACKED: 600,
}


# Identical-content suppression floor. The dispatcher enforces this
# floor on every (source, dedup_key, body_hash) tuple for non-
# STATE_TRACKED sources, regardless of the configured cooldown_seconds.
# An operator who sets alerts.heal.cooldown_seconds=60 for a flap-debug
# session still doesn't see the same identical message every minute.
_IDENTICAL_CONTENT_FLOOR_SECONDS = 86_400


# ─── Compiled-in defaults ────────────────────────────────────────────────────
# Phase 2 of the spec adds matching schema entries to config_sandbox so the
# admin UI surfaces these as toggles. Until those land, the dispatcher reads
# config defensively (see _read_config_value) and falls back to these
# values — meaning Phase 1 is fully usable on its own.

_DEFAULT_SOURCE_ENABLED: dict[str, bool] = {
    "audit": True,
    "pod_report": True,
    "spend_alert": True,
    "cron_alert": True,
    "forge_engine": True,
    "signal_notifier": True,   # Phase 7: flipped on 2026-05-20 after burn-in
    "heal": True,              # Phase C: config drift, restart failed, watchdog
    # "review" retired 2026-08-14 with review.py (#3641) — no emitter left.
    "cost": True,              # Phase C: key rotation overdue (spend duplicate retired)
    "cost_watchdog": True,     # heartbeat-session bloat + model-override leaks
    "analyze": True,           # Phase C: proposal-ready batch
    "apply": True,             # Phase C: apply result (success/rollback/fail)
    "outcome": True,           # Phase C: 7-day "did this help?" checkin
    "report": True,            # Phase C: ad-hoc analyzer report
    "test_runner": True,       # Phase C: weekly test failures
    "validate": True,          # Phase C: forge/manifest validation result
    "weekly_review": True,     # Phase C: weekly RSI process-health report
    "weekly_bot_trends": True, # Sunday per-bot 7d-horizon trend digest (sibling to `report`'s daily)
    "repo_puller": True,       # Phase C: repo-puller wedge notification
    "update_watcher": True,    # Phase E2: openclaw / evolve repo / plugin updates
    "digest_dispatcher": True, # Phase G: bundled daily/weekly digest flusher
    # Power feature (developer profile): activity on upstream issues we filed.
    # Default-enabled at the source layer; opt-in still happens at the catalog
    # subscription layer (updates.upstream_issue_activity defaults to off).
    "upstream_issues_watcher": True,
    "breakers_runner": True,           # L1 cost breaker trips — operator-critical
    # Post-OC-upgrade end-to-end delivery proof: one real "your bots can
    # still message you" send per upgrade (the message IS the probe).
    "send_surface_probe": True,
    # Canary *soak* send-probe — decoupled from send_surface_probe so its
    # operator-visibility is tuned independently of the post-OC-upgrade
    # proof (which stays default-ON and operator-visible). DEFAULT-OFF: on
    # a healthy soak the operator must see NOTHING — the verdict is read
    # programmatically from the probe, so the green chat message was
    # vestigial in the soak context. soak_probe._probe_send no longer sends
    # to the live operator channel; the soak send-proof now rests on the
    # contract probe (safe_upgrade.probe_send_surface) + the always-on
    # gateway-liveness probe (D5). This source is the reserved, already-
    # silent surface for a future live per-candidate re-target send (e.g.
    # via recipient_override to a non-operator self/loopback target) once
    # OpenClaw grows a self-deliverable destination; registering it now
    # keeps the decoupling explicit and the operator toggle ready.
    "soak_send_probe": False,
    # Post-wrap app-install outcomes: pack-install failures after the
    # add-bot wrap, and briefing auto-activation on first channel
    # connect (U1 activation fix, 2026-06-11).
    "app_install": True,
    # Daily CVE scan findings (security-cve-scan/finalize.py → catalog
    # event security.cve_finding). The producer keeps a direct-Telegram
    # fallback so a broken admin package can't silence a finding; this
    # registration just gives the operator a tunable Security toggle and
    # stops the per-batch warning_unknown_catalog_event row.
    "security_cve_scan": True,
    # Global rate breaker's own storm/heartbeat notice (rate_breaker.py).
    # This is the ONE "🌊 Alert storm — batching into the digest" message
    # the operator gets per storm episode (+ hourly heartbeat). It is
    # itself exempt from the breaker (a meta-notice can't be capped by the
    # cap it's announcing) — see _BREAKER_EXEMPT_SOURCES. Operator-visible
    # toggle so it can be muted, but default-ON: the whole point is the
    # operator learns WHY their phone went quiet.
    "alert_rate_breaker": True,
}

# Authoritative per-source category declaration. Every source in
# _DEFAULT_SOURCE_ENABLED MUST appear here too — enforced by
# test_every_source_has_a_declared_category. Picking a category forces
# the author to answer "does this condition persist while the producer
# ticks?" up front, which is the question heal.py got wrong before
# PR #1904 (the 600s cooldown on a 5-min-tick gateway-down condition
# produced the 2026-05-31 gateway-spam identical-message pattern).
#
# Default cooldown is derived from the category via
# _CATEGORY_DEFAULT_COOLDOWN_SECONDS. Identical-content suppression
# applies to STATE_PERSISTS and PER_EVENT_UNIQUE; STATE_TRACKED opts
# out (see SourceCategory docstring).
_DEFAULT_SOURCE_CATEGORY: dict[str, SourceCategory] = {
    # Audit scans every 15 min; a finding the operator hasn't fixed
    # shouldn't re-page on every scan. Content-hashed dedup_key
    # (audit/batch/{content_hash}) plus 24h identical-content floor
    # together give "one announcement per distinct finding-set per day."
    "audit": SourceCategory.STATE_PERSISTS,
    # Daily report; label embeds the date so each day is naturally
    # a unique dedup_key.
    "pod_report": SourceCategory.PER_EVENT_UNIQUE,
    # Spend-over-threshold persists; daily cadence on the alert.
    "spend_alert": SourceCategory.STATE_PERSISTS,
    # Stalled cron persists until the operator restarts it.
    "cron_alert": SourceCategory.STATE_PERSISTS,
    # Forge job IDs are unique per job; identical-content floor is
    # a no-op in practice.
    "forge_engine": SourceCategory.PER_EVENT_UNIQUE,
    # signal_notifier maintains per-Signal-id state in
    # notifier-state.json (see signal_notifier.run_once) and re-
    # announces deliberately when a fresh Signal id with the same
    # signature appears after an audit cycle. The identical-content
    # floor would silence those legitimate re-announcements; the
    # 600s cooldown is the backstop instead. This is currently the
    # only STATE_TRACKED source — adding a second one requires a
    # justifying comment, not just convenience.
    "signal_notifier": SourceCategory.STATE_TRACKED,
    # Gateway down / config drift / watchdog events all persist while
    # heal keeps ticking every 5 min. STATE_PERSISTS makes the 24h
    # identical-content floor the structural guard. Anchored to the
    # 2026-05-31 gateway-spam incident (six identical messages in 73 min
    # at the prior 600s cooldown). Mirrors what _alert_backup_drift
    # was already doing via explicit cooldown_seconds=86_400 override.
    "heal": SourceCategory.STATE_PERSISTS,
    # "review" retired 2026-08-14 with review.py (#3641) — no emitter left.
    # cost.py key-rotation-overdue uses a content-hash of the stale-
    # bots set as the dedup_key. Same bots stale across runs = same
    # dedup_key = same body = identical-content floor catches it.
    # Category change vs. pre-PR migration: was treated as
    # PER_EVENT_UNIQUE-shaped (cooldown=0); the per-event-unique label
    # was wrong because the "event" is "set of stale bots," which
    # persists.
    "cost": SourceCategory.STATE_PERSISTS,
    # Heartbeat-session bloat persists; producer's dedup_key embeds
    # {today}. Matches the 2026-05-24 incident shape.
    "cost_watchdog": SourceCategory.STATE_PERSISTS,
    # Per-batch dedup_keys cycle per analyze run.
    "analyze": SourceCategory.PER_EVENT_UNIQUE,
    # Per-proposal dedup_keys.
    "apply": SourceCategory.PER_EVENT_UNIQUE,
    # Per-outcome_id dedup_keys.
    "outcome": SourceCategory.PER_EVENT_UNIQUE,
    # Ad-hoc operator-invoked; per-fingerprint dedup_key.
    "report": SourceCategory.PER_EVENT_UNIQUE,
    # test_runner failures persist across weekly runs when the same
    # tests keep failing; failures_summary fingerprint stable until
    # tests change. Category change vs. pre-PR migration: was 0
    # cooldown; weekly cadence makes the 24h floor effectively a
    # no-op, but the declaration captures the intent ("same failures
    # ≠ a new event").
    "test_runner": SourceCategory.STATE_PERSISTS,
    # Per-proposal dedup_keys.
    "validate": SourceCategory.PER_EVENT_UNIQUE,
    # Per-ISO-week dedup_key, but the body embeds a live "Generated:"
    # timestamp that drifts between runs — so the identical-content floor
    # (which PER_EVENT_UNIQUE would lean on, with a 0s cooldown) does NOT
    # catch a same-week re-fire. A launchd make-up run on a fresh-pod
    # re-bootstrap delivered the review twice ~15 min apart (2026-06-23).
    # The producer's ISO-week sentinel (weekly_review._already_sent_this_week)
    # is the real full-week guard; STATE_PERSISTS gives the dedup_key a 24h
    # cooldown as a backstop for the rare case the sentinel write fails.
    # (Symmetric with weekly_bot_trends, the sibling Sunday weekly.)
    "weekly_review": SourceCategory.STATE_PERSISTS,
    # Per-ISO-week dedup_key, but the body embeds live turn/cost counts
    # that drift between runs — so the identical-content floor (which
    # PER_EVENT_UNIQUE leans on, with a 0s cooldown) does NOT catch a
    # re-fire in the same week. A launchd make-up run on every non-Sunday
    # re-bootstrap then delivered 3 near-identical reports in one day
    # (2026-06-18). STATE_PERSISTS gives the dedup_key a 24h cooldown
    # regardless of body; the producer's ISO-week sentinel is the
    # full-week guard. (Symmetric with weekly_review.)
    "weekly_bot_trends": SourceCategory.STATE_PERSISTS,
    # Per-wedge-issue dedup_keys.
    "repo_puller": SourceCategory.PER_EVENT_UNIQUE,
    # Per-version dedup_keys.
    "update_watcher": SourceCategory.PER_EVENT_UNIQUE,
    # Per-flush-date dedup_keys.
    "digest_dispatcher": SourceCategory.PER_EVENT_UNIQUE,
    # Per-comment dedup_keys.
    "upstream_issues_watcher": SourceCategory.PER_EVENT_UNIQUE,
    # Per-trip dedup_keys (trip_id is unique). Breaker trips are
    # operator-critical; per-event-unique semantics get every trip
    # to the operator immediately. The 24h identical-content floor
    # is a safety net — a second trip with literally identical
    # rendered content would be a producer bug worth catching.
    "breakers_runner": SourceCategory.PER_EVENT_UNIQUE,
    # One send per OC upgrade; the body embeds the new version, so the
    # 24h identical-content floor only bites on a same-version reinstall
    # (where a second proof message genuinely is redundant).
    "send_surface_probe": SourceCategory.PER_EVENT_UNIQUE,
    # Per-candidate soak send-probe. PER_EVENT_UNIQUE: each release
    # candidate (SHA) is a distinct event, so the natural dedup_key would
    # cycle per candidate. Default-OFF (see _DEFAULT_SOURCE_ENABLED), so
    # this category only governs the reserved future live re-target send;
    # today the source emits nothing.
    "soak_send_probe": SourceCategory.PER_EVENT_UNIQUE,
    # Per-job dedup_keys: each install job completes (or fails) exactly
    # once, and a briefing activation is a one-shot moment per bot.
    "app_install": SourceCategory.PER_EVENT_UNIQUE,
    # CVE findings persist until the operator patches or mutes them; the
    # daily scan re-finds the same set every run. Content-hashed dedup_key
    # (security_cve_scan/<sha1 of the sorted CVE-id list>) + the 24h
    # identical-content floor give "one announcement per distinct
    # finding-set per day" — exactly the audit shape. A new or changed
    # finding gets a fresh dedup_key and delivers immediately.
    "security_cve_scan": SourceCategory.STATE_PERSISTS,
    # Storm notice / heartbeat. Per-episode unique (the breaker controls
    # "exactly once + hourly heartbeat" itself via its episode state); the
    # dispatcher passes dedup_key=None so no extra cooldown/identical gate
    # applies. PER_EVENT_UNIQUE keeps the dispatcher's own gates out of the
    # breaker's way.
    "alert_rate_breaker": SourceCategory.PER_EVENT_UNIQUE,
}


# Derived from the category map. Kept as a public name for the existing
# schema-drift test (test_dispatcher_compiled_defaults_match_schema)
# and for downstream callers; the underlying source of truth is now
# the category declaration above.
_DEFAULT_SOURCE_COOLDOWN_SECONDS: dict[str, int] = {
    source: _CATEGORY_DEFAULT_COOLDOWN_SECONDS[category]
    for source, category in _DEFAULT_SOURCE_CATEGORY.items()
}

_DEFAULT_DISPATCHER_ENABLED = True

# Recipient priority order when falling back from explicit overrides.
# Projection of the channel registry's ``notify_priority`` column — the same
# derivation breakers_enforce._USER_CHANNEL_PRIORITY uses, so the two orders
# are identical BY CONSTRUCTION rather than by hand-syncing two tuples
# (invariant 7 — no channel-id literals outside the registry).
_DEFAULT_CHANNEL_PRIORITY = _channel_registry.by_notify_priority()

# How many chars of the message body to include in audit log entries.
_AUDIT_EXCERPT_CHARS = 240


# ─── Public API ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DispatchOutcome:
    """Return value from :func:`send`. ``result`` is the load-bearing field;
    other fields are populated for the SENT path or when useful for
    debugging suppressed/failed dispatches."""

    result: DispatchResult
    source: str
    severity: Severity
    dedup_key: str | None
    channel: str | None = None
    chat_id: str | None = None
    error: str | None = None
    # Phase A2: when the caller passed catalog_event, surface it for the
    # audit log + tests + future analytics.
    catalog_event: str | None = None
    # Classifies a FAILED outcome as permanent (HTTP 4xx-class — the
    # message will never succeed as-is) vs transient (network/timeout/
    # 5xx — worth retrying). Callers like signal_notifier use this to
    # avoid per-minute retry loops on permanent failures. ``False`` for
    # any non-FAILED outcome.
    is_permanent_failure: bool = False
    # Subset of permanent failures where the delivery TARGET itself is
    # unreachable (chat not found, bot blocked/kicked, deactivated user,
    # wrong/revoked token) — a channel-level outage, as opposed to a
    # per-message format error (HTTP 400 parse). signal_notifier reads
    # this to switch the whole target into quiet digest-on-recovery mode
    # rather than treating each backlogged signal individually. ``False``
    # for any non-FAILED outcome and for message-level permanent failures.
    is_delivery_target_down: bool = False


def send(
    *,
    shared_dir: Path,
    network: dict[str, Any] | None,
    source: str,
    message: str | None = None,
    severity: Severity = Severity.INFO,
    dedup_key: str | None = None,
    cooldown_seconds: int | None = None,
    recipient_override: tuple[str, str] | None = None,
    now: datetime | None = None,
    catalog_event: str | None = None,
    payload: dict[str, Any] | None = None,
    digest_meta: dict[str, Any] | None = None,
) -> DispatchOutcome:
    """Send a sysadmin alert through the configured channel.

    The caller provides either:
      * ``message`` (legacy / Phase 1–E) — already-rendered text the
        source owns end-to-end, or
      * ``payload`` + ``catalog_event`` (Phase F) — structured fields
        that the catalog's ``body_template`` + ``action`` renders into
        the final message. One catalog template → consistent format
        across every source.

    When both ``message`` and a renderable ``catalog_event`` + ``payload``
    are present, the catalog render wins (Phase F migration in progress).

    Parameters
    ----------
    shared_dir:
        Pod-wide shared dir (typically ``/Users/Shared/evolve``). Holds
        ``alerts/dispatcher-state.json`` and the JSONL logs.
    network:
        Parsed ``network.json`` dict (caller's responsibility — load once,
        pass in). Used for recipient resolution.
    source:
        Identifier for the calling source. Must be in
        :data:`_DEFAULT_SOURCE_ENABLED` to be recognized; unknown sources
        are still dispatched (with default cooldown 0) but their
        enable/disable cannot be toggled — log a warning so the gap is
        visible.
    message:
        Already-rendered text. Pre-Phase-F path. Sources own their
        templates (including any emoji). The dispatcher does not modify
        the body. Required unless ``payload`` + ``catalog_event`` are
        provided.
    severity:
        Metadata for audit log and future severity-floor filtering. Does
        NOT change rendering.
    dedup_key:
        If set, the cooldown check uses this key. Same key + same source
        within ``cooldown_seconds`` → ``SUPPRESSED_COOLDOWN``. ``None``
        skips the cooldown check entirely (use for genuinely unique
        events like job completions).
    cooldown_seconds:
        Override the per-source default. If ``None``, the dispatcher
        reads the source's compiled-in default (see
        :data:`_DEFAULT_SOURCE_COOLDOWN_SECONDS`).
    recipient_override:
        ``(channel, chat_id)`` tuple. Skips network.json lookup. Rare —
        used for testing or per-call routing.
    now:
        Override the clock (testing). Defaults to ``datetime.now(UTC)``.
    catalog_event:
        Optional alert-catalog event key (e.g. ``"cost.daily_threshold"``).
        When set, the dispatcher applies operator subscription gating
        (enabled / frequency) from the catalog before existing source-
        level checks. ``None`` keeps Phase 1 behavior — pure source-level
        gating, no catalog awareness.
    payload:
        Phase F. When set alongside ``catalog_event``, the dispatcher
        renders the final message via the catalog's ``body_template`` +
        ``action`` rather than using the caller's ``message``. The
        payload dict must contain every ``{placeholder}`` the catalog
        template references. Missing placeholders fall back to the
        caller's ``message`` (with a warning logged) — never crashes
        the dispatch.
    digest_meta:
        Optional extra fields merged into the digest queue record when this
        send is DEFERRED to a daily/weekly digest. Carries the L1 fire/clear
        pairing keys (``coalesce_key``, ``kind``, ``signal_severity``,
        ``lifetime_seconds``) so ``digest_dispatcher`` can elide a within-
        grace transient pair (spec-delta-transient-delivery-grace). Ignored
        entirely on the immediate-push path — pure digest-render metadata,
        never affects cooldown, gating, or the wire message.
    """
    now = now or datetime.now(timezone.utc)

    # Phase F: catalog-side rendering. When the caller provides both
    # catalog_event and payload, render via the catalog template and use
    # that as the final message. If rendering fails (missing placeholder,
    # catalog import issue, unknown event), fall back to the caller's
    # message — silent fallback so a misconfigured payload never blocks
    # the alert.
    #
    # Inject pod-derived context (ssh_prefix, admin_user, etc.) into the
    # payload before rendering so catalog action commands can reference
    # those without hardcoding host aliases. See _resolve_pod_context
    # for the field list. Caller's payload wins on collision.
    if catalog_event is not None and payload is not None:
        pod_ctx = _resolve_pod_context(network)
        merged_payload = {**pod_ctx, **payload}
        rendered = _render_via_catalog(catalog_event, merged_payload)
        if rendered is not None:
            message = rendered
        elif message is None:
            # No legacy fallback to use — log + abort with a clear outcome.
            outcome = DispatchOutcome(
                result=DispatchResult.FAILED,
                source=source, severity=severity, dedup_key=dedup_key,
                catalog_event=catalog_event,
                error=f"catalog render failed for {catalog_event} and no fallback message provided",
            )
            _append_log(shared_dir, _log_filename_for(outcome),
                        _audit_record(now, outcome, ""))
            return outcome

    if message is None:
        outcome = DispatchOutcome(
            result=DispatchResult.FAILED,
            source=source, severity=severity, dedup_key=dedup_key,
            catalog_event=catalog_event,
            error="dispatcher.send called without message or renderable payload",
        )
        _append_log(shared_dir, _log_filename_for(outcome),
                    _audit_record(now, outcome, ""))
        return outcome

    # Bind-complete contract (spec-subscription-completeness-2026-06-24
    # workstream A "bind-complete", design-reviewed + approved 2026-06-28).
    # EVERY dispatched message must resolve to a real catalog_event so the
    # operator has a subscription handle for it. The old "catalog_event=None ⇒
    # skip subscription gating" escape hatch is gone: a sender that needs
    # guaranteed delivery no longer goes UNBOUND to dodge the gates — it binds
    # to a catalog event flagged ``is_safety_critical``, which is now the
    # gating MODIFIER (it exempts the message from the global rate cap via
    # ``_is_bypass_alert`` so a storm can't silence a gateway-down or a
    # security floor). Safety is expressed by the flag, not by being unbound.
    #
    # A literal ``catalog_event=None`` reaching here is therefore a producer
    # contract VIOLATION (a legacy message-only send that never picked up a
    # catalog handle). The producer-side ratchets keep it from shipping out of
    # alerts/ (test_alerts_signal_notifier::test_catalog_event_mapping_is_total
    # — the signal→catalog mapper never returns None — and
    # ::test_no_alerts_send_passes_literal_catalog_event_none — no alerts/ call
    # site passes a literal None). At runtime we fail OPEN: record the
    # violation to its OWN stream (so it's visible without polluting the
    # suppressed-log the operator tooling counts), then deliver via the
    # source-level gates below rather than drop a real message. A missed
    # binding degrades to "ungated delivery", never to silence.
    if catalog_event is None:
        _append_log(
            shared_dir, "dispatcher-binding-violations.jsonl",
            {
                "ts": _iso(now),
                "source": source,
                "result": "binding_violation_catalog_event_none",
                "message_excerpt": _excerpt(message),
            },
        )

    # Subscription-id footer. When a catalog_event is set, append
    # ``subscription: <event_key>`` so the operator can find the exact
    # toggle that produced the message on the configure tab. Idempotent —
    # catalog.render_event already appends the same footer, so payload-
    # driven sends pass through untouched. This covers bare ``message=``
    # callers (signal_notifier, future legacy sources) that bypass
    # render_event.
    if catalog_event is not None:
        footer = f"subscription: {catalog_event}"
        if footer not in message:
            message = f"{message}\n\n{footer}"

    if not _read_dispatcher_enabled(shared_dir):
        return _suppressed(
            DispatchResult.SUPPRESSED_DISABLED,
            source, severity, dedup_key, message,
            shared_dir, now, reason="dispatcher_disabled",
            catalog_event=catalog_event,
        )

    if not _read_source_enabled(shared_dir, source):
        return _suppressed(
            DispatchResult.SUPPRESSED_DISABLED,
            source, severity, dedup_key, message,
            shared_dir, now, reason=f"source_disabled:{source}",
            catalog_event=catalog_event,
        )

    # Notify gate (subscription-completeness Phase 2 chip 3,
    # spec-subscription-completeness-2026-06-24.md "Removed/internalized").
    # A handful of internal events were marked notify=False in the catalog:
    # the Signal still fires and any internal record it feeds still gets
    # written, but the standalone CHAT push is suppressed. This runs before
    # subscription resolution because notify is a fixed catalog property —
    # not an operator-tunable override — so an internalized event never
    # reaches the chat channel regardless of (absent) subscription state.
    # Fails open: an unknown key resolves to notify=True (see
    # catalog.notify_for), matching the unknown-event fall-through below.
    if catalog_event is not None and not _resolve_catalog_notify(catalog_event):
        return _suppressed(
            DispatchResult.SUPPRESSED_DISABLED,
            source, severity, dedup_key, message,
            shared_dir, now,
            reason=f"notify_off:{catalog_event}",
            catalog_event=catalog_event,
        )

    # Catalog gating (Phase A2). Only applies when the caller passed
    # catalog_event. Unknown event keys are tolerated — log a warning and
    # fall through to source-level checks so a typo can't block delivery.
    catalog_floor_cooldown = 0
    if catalog_event is not None:
        catalog_decision = _resolve_catalog_subscription(
            shared_dir, catalog_event,
        )
        if catalog_decision is None:
            # Unknown event key. Log to suppression file as a warning so
            # the operator can spot misconfigured callers, but proceed
            # with the existing source-level path.
            _append_log(
                shared_dir, "dispatcher-suppressed.jsonl",
                {
                    "ts": _iso(now),
                    "source": source,
                    "result": "warning_unknown_catalog_event",
                    "catalog_event": catalog_event,
                    "message_excerpt": _excerpt(message),
                },
            )
        else:
            enabled, frequency = catalog_decision
            if not enabled:
                return _suppressed(
                    DispatchResult.SUPPRESSED_DISABLED,
                    source, severity, dedup_key, message,
                    shared_dir, now,
                    reason=f"subscription_off:{catalog_event}",
                    catalog_event=catalog_event,
                )
            # Digest modes short-circuit cooldown — append to the queue
            # and return DEFERRED. The Phase G flush daemon (TBD) drains
            # the queue on its own schedule.
            if frequency in ("daily_digest", "weekly_digest"):
                _enqueue_digest(
                    shared_dir, frequency, now,
                    source=source, catalog_event=catalog_event,
                    severity=severity, dedup_key=dedup_key,
                    message=message, extra=digest_meta,
                )
                outcome = DispatchOutcome(
                    result=DispatchResult.DEFERRED,
                    source=source, severity=severity, dedup_key=dedup_key,
                    catalog_event=catalog_event,
                )
                _append_log(
                    shared_dir, _log_filename_for(outcome),
                    _audit_record(now, outcome, message),
                )
                return outcome
            # Frequency-driven cooldown floor: operator's choice trumps
            # the source's cooldown_seconds when the source asked for less.
            catalog_floor_cooldown = _frequency_floor_seconds(frequency)

    # ── Recurring-flapper demotion (flapper.py) ──────────────────────────
    # The multi-hour-flap layer ABOVE the per-severity grace. A signature
    # (source + Signal signature) that oscillates K+ times in 24h is
    # auto-demoted: its fires route to the daily digest and its standalone
    # clears come off the immediate-push path, until it stabilizes for a
    # cooldown and self-lifts. Runs here — after the subscription/digest
    # short-circuit above (a muted or already-digest event never reaches
    # this) and BEFORE cooldown/identical/recipient/rate-breaker — because a
    # demoted fire must roll into the digest regardless of cooldown state.
    # Gated on ``digest_meta`` carrying the (coalesce_key, kind,
    # signal_severity) the L1 pairing already supplies; a caller without it
    # (every non-signal_notifier source) is unaffected. alert-severity is
    # exempt inside flapper.evaluate.
    flap = _consult_flapper(shared_dir, source=source, digest_meta=digest_meta, now=now)
    if flap is not None and flap.action != "pass":
        return _demote_flapper(
            shared_dir, now,
            source=source, severity=severity, dedup_key=dedup_key,
            catalog_event=catalog_event, message=message,
            digest_meta=digest_meta, decision=flap,
        )

    effective_cooldown = (
        cooldown_seconds
        if cooldown_seconds is not None
        else _read_source_cooldown(shared_dir, source)
    )
    effective_cooldown = max(effective_cooldown, catalog_floor_cooldown)

    # Identical-content suppression (spec-alert-no-repeat-2026-06-01.md).
    # Independent of the source's cooldown_seconds: a STATE_PERSISTS or
    # PER_EVENT_UNIQUE source with the same (dedup_key, body_hash) inside
    # the 24h floor returns SUPPRESSED_IDENTICAL even if cooldown_seconds
    # is shorter (e.g. an operator dropped it to 60s to debug a flap).
    # STATE_TRACKED sources opt out via category — signal_notifier owns
    # per-Signal-id dedup elsewhere and re-announces fresh ids deliberately.
    #
    # dedup_key=None bypasses both checks (recovery messages,
    # genuinely-unique events). The body_hash is computed against the
    # final message text — same text the operator will see — so any
    # rendering or footer addition above is reflected in the hash.
    body_hash = _body_hash(message)
    category = _DEFAULT_SOURCE_CATEGORY.get(source, SourceCategory.PER_EVENT_UNIQUE)
    if dedup_key is not None:
        last_entry = _last_dispatch_entry(shared_dir, source, dedup_key)
        last_ts = last_entry.ts if last_entry else None
        last_hash = last_entry.body_hash if last_entry else None
        if (
            last_ts is not None
            and last_hash is not None
            and last_hash == body_hash
            and category != SourceCategory.STATE_TRACKED
            and (now - last_ts).total_seconds() < _IDENTICAL_CONTENT_FLOOR_SECONDS
        ):
            return _suppressed(
                DispatchResult.SUPPRESSED_IDENTICAL,
                source, severity, dedup_key, message,
                shared_dir, now,
                reason=(
                    f"identical_within_24h:"
                    f"{int((now - last_ts).total_seconds())}s"
                ),
                catalog_event=catalog_event,
            )
        if (
            last_ts is not None
            and effective_cooldown > 0
            and (now - last_ts).total_seconds() < effective_cooldown
        ):
            return _suppressed(
                DispatchResult.SUPPRESSED_COOLDOWN,
                source, severity, dedup_key, message,
                shared_dir, now,
                reason=f"cooldown:{int((now - last_ts).total_seconds())}s",
                catalog_event=catalog_event,
            )

    if recipient_override is not None:
        channel, chat_id = recipient_override
    else:
        recipient = resolve_recipient(network or {})
        if recipient is None:
            outcome = DispatchOutcome(
                result=DispatchResult.NO_RECIPIENT,
                source=source, severity=severity, dedup_key=dedup_key,
                error="no recipient configured (network.alerts unset and no evolve bot fallback)",
                catalog_event=catalog_event,
            )
            _append_log(shared_dir, _log_filename_for(outcome),
                        _audit_record(now, outcome, message))
            return outcome
        channel, chat_id = recipient

    # ── Global outbound rate breaker (producer-agnostic backstop) ────────
    # The single chokepoint every immediate send passes through. Consulted
    # here — after every per-source gate has passed and the channel is
    # known, before the actual send. Logic lives in rate_breaker.py; this
    # is the one call site. Over-cap / storm → roll into the daily digest
    # (never drop) and emit at most one operator notice per storm episode.
    breaker = _consult_rate_breaker(
        shared_dir, channel=channel, source=source,
        severity=severity, catalog_event=catalog_event, now=now,
    )
    if breaker is not None:
        if breaker.storm_notice:
            _emit_storm_notice(
                shared_dir, network, channel, chat_id, breaker.storm_notice, now,
            )
        if not breaker.allow:
            return _batch_rate_capped(
                shared_dir, now,
                source=source, severity=severity, dedup_key=dedup_key,
                catalog_event=catalog_event, message=message,
                channel=channel, chat_id=chat_id, decision=breaker,
            )

    # Prefer direct Telegram HTTP API — the openclaw CLI hangs in cleanup
    # for minutes per send, which made batched alerts (heal, audit, reports)
    # report FAILED while still delivering. Falls through to the CLI when
    # the bot token isn't on disk.
    use_openclaw = True
    sent_ok: bool = False
    error: str | None = None
    if channel == "telegram":
        sent_ok, error = _dispatch_via_telegram_http(chat_id, message)
        token_missing = error and error.startswith("no-telegram-token")
        if sent_ok or not token_missing:
            use_openclaw = False
    if use_openclaw:
        gateway_port = _resolve_gateway_port(network)
        # Resilient variant: bounded retry/backoff across a gateway restart
        # window. A transient GatewayTransportError no longer hard-fails a
        # send that a retry one moment later would deliver.
        sent_ok, error = _dispatch_via_openclaw_resilient(
            channel, chat_id, message, gateway_port,
        )

    # Record cooldown state for BOTH success and failure paths. The original
    # design only recorded on SENT, which seems correct in isolation — why
    # rate-limit a message that never went out? — but in practice it caused
    # catastrophic spam: when a send path was broken (e.g. the openclaw CLI
    # hanging past the subprocess timeout despite actually delivering the
    # message), every retry within the cooldown window slipped through,
    # generating dozens of identical FAILED entries an hour. Operators saw
    # both the duplicate alerts (when the underlying delivery did work) and
    # the dispatcher log flooded with failures. Treating cooldown as a
    # "this dedup_key was attempted recently" gate rather than "successfully
    # sent recently" gate trades one rare correctness case (a transient
    # blip that resolves before the cooldown elapses) for a much better
    # failure mode.
    #
    # body_hash recorded alongside ts so identical-content suppression
    # works on retries too — a permanently-failing identical message
    # doesn't get to spam the operator just because the prior attempt
    # didn't succeed.
    if dedup_key is not None:
        _record_dispatch(shared_dir, source, dedup_key, now, body_hash)

    # PWA push fanout — PWA Phase 1.2 §6.4.
    #
    # Independent of the primary chat send: the operator may have a
    # broken Telegram bot token but a working iPhone PWA, and the
    # whole point of push is "alert me wherever I am." We fan out
    # whenever:
    #
    #   * the event has a catalog_event (so we know the tag / url),
    #   * the operator has flipped pwa_push on in their channel set,
    #   * at least one registered subscription is active.
    #
    # All three gates are cheap; the no-subscriptions case returns
    # immediately. Failures here never affect the chat-channel outcome —
    # the dispatcher's primary contract is "did the chat send succeed",
    # and push fanout is additive.
    push_result = _fanout_pwa_push_if_enabled(
        shared_dir, catalog_event=catalog_event, message=message,
        severity=severity,
    )

    if not sent_ok:
        outcome = DispatchOutcome(
            result=DispatchResult.FAILED,
            source=source, severity=severity, dedup_key=dedup_key,
            channel=channel, chat_id=chat_id, error=error,
            catalog_event=catalog_event,
            is_permanent_failure=_is_permanent_failure(error),
            is_delivery_target_down=_is_delivery_target_down(error),
        )
        rec = _audit_record(now, outcome, message)
        if push_result is not None:
            rec["pwa_push"] = push_result
        _append_log(shared_dir, _log_filename_for(outcome), rec)
        # Terminal (permanent) failures get dead-lettered: a wrong token /
        # chat-not-found / HTTP-4xx / unparseable body will never deliver,
        # and the gateway-retry above is already exhausted for transient
        # errors. Preserve the message (don't silently drop it) AND surface
        # it in the operator's Recent Messages audit instead of leaving the
        # feed looking idle. Transient failures stay OUT of the dead-letter
        # store — the producer's next tick (or the digest requeue) retries.
        if outcome.is_permanent_failure:
            _record_dead_letter(shared_dir, now, outcome, message)
        return outcome
    outcome = DispatchOutcome(
        result=DispatchResult.SENT,
        source=source, severity=severity, dedup_key=dedup_key,
        channel=channel, chat_id=chat_id,
        catalog_event=catalog_event,
    )
    rec = _audit_record(now, outcome, message)
    if push_result is not None:
        rec["pwa_push"] = push_result
    _append_log(shared_dir, _log_filename_for(outcome), rec)
    return outcome


# ─── HTML validation + dry-run (PR 3 of dispatcher-safety rework) ────────────


# Tags Telegram accepts under parse_mode="HTML". Anything else (or
# even allowed tags with unsupported attributes) returns 400 at send
# time. Reference: https://core.telegram.org/bots/api#html-style.
_TELEGRAM_ALLOWED_HTML_TAGS: frozenset[str] = frozenset({
    "b", "strong",
    "i", "em",
    "u", "ins",
    "s", "strike", "del",
    "a",
    "code", "pre",
    "blockquote",
    "tg-spoiler",
    "span",          # only with class="tg-spoiler"; full attr-check below
    "tg-emoji",
    "br",            # implicit line break — Telegram accepts it
})

# Void / self-closing tags that don't need a corresponding close.
_TELEGRAM_VOID_HTML_TAGS: frozenset[str] = frozenset({"br"})


class _TelegramHTMLValidator(HTMLParser):
    """Pre-flight a message against Telegram's HTML parse_mode rules.

    Catches three classes of error before the wire:
      * unsupported tags (``<script>``, ``<div>``, etc.)
      * unbalanced tags (``<b>hello`` without ``</b>``)
      * mismatched close (``<b>...</i>``)

    Doesn't fully replicate Telegram's parser — the round-trip CI
    test catches edge cases (attributes, deep nesting, entity quirks).
    But it covers the vast majority of breakage in pure Python with
    zero network dependency, so producers can lint their templates
    in unit tests.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _TELEGRAM_ALLOWED_HTML_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
            return
        if tag not in _TELEGRAM_VOID_HTML_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _TELEGRAM_ALLOWED_HTML_TAGS:
            self.errors.append(f"unsupported closing </{tag}>")
            return
        if tag in _TELEGRAM_VOID_HTML_TAGS:
            return  # void tags don't get closed
        if not self.stack:
            self.errors.append(f"closing </{tag}> with no open tag")
            return
        if self.stack[-1] != tag:
            self.errors.append(
                f"mismatched </{tag}> (top of stack is <{self.stack[-1]}>)"
            )
            return
        self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <foo/> form — treated as open+immediate-close. Telegram doesn't
        # use these but the validator tolerates them so producers can
        # write them without surprising errors.
        if tag not in _TELEGRAM_ALLOWED_HTML_TAGS:
            self.errors.append(f"unsupported self-closing tag <{tag}/>")

    def error(self, message: str) -> None:  # pragma: no cover — stdlib stub
        self.errors.append(f"parser error: {message}")


def validate_html_message(message: str) -> tuple[bool, str | None]:
    """Return ``(ok, error)`` for whether ``message`` is safe to send
    under Telegram's ``parse_mode="HTML"``.

    Pure-Python; no network. Use in CI to lint every catalog event's
    rendered output against the producer/template HTML boundary.
    """
    v = _TelegramHTMLValidator()
    try:
        v.feed(message)
        v.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"validator raised: {exc}"
    if v.errors:
        return False, "; ".join(v.errors)
    if v.stack:
        return False, f"unclosed tag(s): {', '.join(f'<{t}>' for t in v.stack)}"
    return True, None


@dataclass(frozen=True)
class DryRunResult:
    """Return value from :func:`dry_run`. ``message`` is the final
    rendered text the dispatcher would have sent; ``ok`` + ``error``
    report whether it passes the HTML validator."""

    ok: bool
    message: str
    error: str | None = None
    catalog_event: str | None = None


def dry_run(
    *,
    source: str,
    message: str | None = None,
    catalog_event: str | None = None,
    payload: dict[str, Any] | None = None,
    network: dict[str, Any] | None = None,
) -> DryRunResult:
    """Render a message through the same pipeline as :func:`send` but
    skip the network / state / subscription checks. Returns the final
    text + an HTML-validation verdict.

    Used by the CI test that asserts every catalog event renders to
    valid HTML. Also useful for ad-hoc operator debugging — answer
    "what would the dispatcher send if I called this with this payload."
    """
    # Stage 1: catalog rendering (mirrors send()).
    rendered = None
    if catalog_event is not None and payload is not None:
        pod_ctx = _resolve_pod_context(network)
        merged = {**pod_ctx, **payload}
        rendered = _render_via_catalog(catalog_event, merged)

    final = rendered if rendered is not None else message
    if final is None:
        return DryRunResult(
            ok=False, message="",
            error="no message or renderable payload",
            catalog_event=catalog_event,
        )

    # Stage 2: subscription footer (mirrors send()).
    if catalog_event is not None:
        footer = f"subscription: {catalog_event}"
        if footer not in final:
            final = f"{final}\n\n{footer}"

    ok, err = validate_html_message(final)
    return DryRunResult(
        ok=ok, message=final, error=err, catalog_event=catalog_event,
    )


# ─── Recipient resolution ────────────────────────────────────────────────────


def resolve_recipient(network: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(channel, chat_id)`` for the operator, or None if unset.

    Resolution order (per spec § "Channel layer"):
      1. ``network.alerts.channel`` + ``network.alerts.chatId`` — the
         existing operator-set "primary alert channel" (back-compat with
         the five existing emitters).
      2. ``network.bots.<primary>.primary_channel`` if explicitly set,
         paired with the matching id from ``primary_user.external_ids``,
         where ``<primary>`` is resolved via ``primary_bot.primary_bot_id``
         (top-level ``network.primary`` → first ``role: "primary"`` bot →
         legacy ``"evolve"`` fallback).
      3. First channel present in ``<primary>.primary_user.external_ids``
         in the registry's notify-priority order.
      4. ``None`` — caller decides whether to log + skip.

    Steps 2-3 go through :func:`external_ids.resolve_notify_target`, which
    reads BOTH live ``external_ids`` shapes (list and bare scalar) and
    returns a bare chat id. Pre-M1-B2 this did ``str(external_ids[ch])``,
    which turned a list-shaped entry into the literal ``"['123']"``.
    """
    alerts = (network.get("alerts") or {}) if isinstance(network, dict) else {}
    channel = alerts.get("channel")
    chat_id = alerts.get("chatId")
    if channel and chat_id:
        return str(channel), str(chat_id)

    # Resolve the primary bot via the shared helper so this stays in sync
    # with every other primary-bot lookup in the codebase.
    from primary_bot import primary_bot_id  # local import: evolve-analyzer is installed
    bots = (network.get("bots") or {}) if isinstance(network, dict) else {}
    pid = primary_bot_id(network) if isinstance(network, dict) else None
    primary_cfg = (bots.get(pid) or {}) if pid else {}
    return _external_ids.resolve_notify_target(
        primary_cfg.get("primary_user"),
        primary_channel=primary_cfg.get("primary_channel"),
        priority=_DEFAULT_CHANNEL_PRIORITY,
    )


# ─── Subprocess dispatch ────────────────────────────────────────────────────


def _dispatch_via_openclaw(
    channel: str, chat_id: str, message: str,
    gateway_port: int | None = None,
    timeout_seconds: int = 60,
) -> tuple[bool, str | None]:
    """Shell out to ``openclaw message send``. Returns ``(ok, error)``.

    Matches the call shape used by ``forge_engine.py`` and
    ``server.py``'s test-alert endpoint. The PATH inherited from the
    LaunchDaemon plist (``/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin``)
    finds the openclaw binary.
    """
    # cwd="/tmp" below: openclaw is Node, which calls process.cwd() at
    # startup. If a caller invokes us from a directory the running user
    # can't stat, Node aborts with EACCES before reaching main(). Same
    # fix as the one applied to audit.py / pod_report.py / heal.py
    # individually before consolidation. See bug history in those modules.
    # OpenClaw 2026.4.29+ renamed --to → -t/--target; older builds aren't supported.
    #
    # gateway_port: the openclaw CLI defaults to 18789, but the gateway plist
    # assigns each bot a unique port. Without OPENCLAW_GATEWAY_PORT set the CLI
    # connects to the wrong port and hangs until our 15s timeout fires.
    import select
    import signal
    import time

    env = None
    if gateway_port is not None:
        env = {**os.environ, "OPENCLAW_GATEWAY_PORT": str(gateway_port)}
    # 60s timeout: openclaw CLI message send commonly takes 15-30s (plugin
    # init + gateway WS connect + Telegram round-trip). The previous 15s
    # window expired *after* the message was already delivered, so the
    # dispatcher logged FAILED and never recorded cooldown state — which
    # bypassed dedup and caused duplicate sends on every retry.
    #
    # start_new_session=True: puts openclaw in its own process group so we
    # can kill it and its background children (openclaw-message) with
    # os.killpg() after the parent exits. Without this, killing the group
    # would also kill the Python process (they'd share a process group).
    cmd = ["openclaw", "message", "send",
           "--channel", channel, "--target", chat_id, "-m", message]
    try:
        # polling-bypass: outbound notification (one-shot side-effect; not polling)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd="/tmp", env=env, start_new_session=True,
        )
    except FileNotFoundError:
        return False, "openclaw binary not found on PATH"
    except Exception as exc:  # noqa: BLE001
        return False, f"openclaw subprocess error: {exc}"

    stdout_buf = b""
    stderr_buf = b""
    deadline = time.time() + timeout_seconds
    timed_out = False
    delivered = False  # Set when openclaw prints "Sent via …. Message ID: N"

    # OpenClaw CLI prints a delivery confirmation as soon as Telegram ACKs the
    # message — typically within 2-5s — but then often hangs on cleanup for
    # tens of minutes (upstream issue with plugin shutdown). Watching stdout
    # for the success marker lets us return SENT promptly and kill the CLI,
    # instead of waiting for it to exit cleanly and reporting a false FAILED.
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            r_fds, _, _ = select.select(
                [proc.stdout, proc.stderr], [], [], min(remaining, 0.5)
            )
        except (ValueError, OSError):
            break
        if not r_fds:
            if proc.poll() is not None:
                break
            continue
        for fd in r_fds:
            try:
                chunk = os.read(fd.fileno(), 65536)
            except OSError:
                chunk = b""
            if chunk:
                if fd is proc.stdout:
                    stdout_buf += chunk
                else:
                    stderr_buf += chunk
        # Success marker — break out as soon as we see it; the CLI may sit
        # in cleanup for minutes after this point.
        if not delivered and (b"Message ID:" in stdout_buf or b"Sent via" in stdout_buf):
            delivered = True
            break
        if proc.poll() is not None:
            break

    # Kill the whole process group (includes openclaw-message background child).
    # evolve owns these processes (no sudo wrapper here) so os.killpg works.
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass

    if delivered:
        return True, None
    if timed_out:
        return False, "openclaw message send timed out after 60s"

    returncode = proc.poll()
    stdout_text = stdout_buf.decode("utf-8", errors="replace")
    stderr_text = stderr_buf.decode("utf-8", errors="replace")
    if returncode == 0:
        return True, None
    return False, (stderr_text or stdout_text or f"openclaw exit {returncode}").strip()[:500]


# ─── Gateway-send retry/backoff (resilience to gateway restarts) ─────────────
#
# The openclaw CLI delivers through the bot's local gateway WS
# (ws://127.0.0.1:<port>). That gateway restarts a few times a day under
# memory pressure (a separate platform concern); a send that lands inside a
# restart window fails with a transient transport error — observed live as
# ``GatewayTransportError: gateway timeout after 10000ms``. A single attempt
# then reports FAILED even though a retry one moment later would have
# succeeded. The result: the operator's chat goes quiet during every restart
# AND the Recent Messages feed freezes (delivered-as-FAILED rows don't land
# in dispatcher.jsonl). Bounded retry across the restart window fixes both.
#
# Only TRANSIENT transport errors are retried. A permanent failure
# (_is_permanent_failure: HTTP 4xx-class, chat-not-found, bad token) will
# never succeed on retry, so it returns immediately — burning retries on it
# is pure latency. The ``delivered`` marker path inside _dispatch_via_openclaw
# means a send that DID reach Telegram returns (True, None) before we get
# here, so a retry can never double-deliver a confirmed message.
_TRANSIENT_GATEWAY_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "gatewaytransporterror",
    "gateway timeout",
    "timed out",
    "timeout after",
    "connection refused",
    "econnrefused",
    "connection reset",
    "websocket",
    "ws closed",
    "socket hang up",
    "binary not found",   # gateway/CLI not yet (re)spawned during a bounce
)

# Backoff schedule BETWEEN attempts (seconds). len()+1 = max attempts. The
# windows are sized to straddle a gateway restart (a few seconds to respawn
# the WS listener) without making a single send block a daemon tick for long:
# worst case here is 2 retries ≈ 2+5s of sleep plus the per-attempt CLI
# timeout. Kept short on purpose — a total gateway outage should surface as a
# loud Dispatcher-Health failure, not be hidden behind minutes of in-process
# retrying.
_GATEWAY_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0)


# Injectable sleep seam so unit tests don't actually wait. Production uses
# time.sleep; tests monkeypatch this to a no-op recorder.
def _retry_sleep(seconds: float) -> None:  # pragma: no cover — trivial
    import time
    time.sleep(seconds)


def _is_transient_gateway_error(error: str | None) -> bool:
    """True when ``error`` looks like a transient gateway-transport failure
    worth retrying across a gateway restart window.

    Conservative on BOTH sides:
      * A permanent failure (``_is_permanent_failure``) is never transient —
        retrying a wrong token / chat-not-found / HTTP-4xx is futile.
      * Only the explicit ``_TRANSIENT_GATEWAY_ERROR_SUBSTRINGS`` shapes
        qualify. An unknown error stays non-retryable so a novel permanent
        failure can't get stuck in a retry loop.
    """
    if not error:
        return False
    if _is_permanent_failure(error):
        return False
    lowered = error.lower()
    return any(s in lowered for s in _TRANSIENT_GATEWAY_ERROR_SUBSTRINGS)


def _dispatch_via_openclaw_resilient(
    channel: str, chat_id: str, message: str,
    gateway_port: int | None = None,
) -> tuple[bool, str | None]:
    """``_dispatch_via_openclaw`` with bounded retry/backoff on transient
    gateway-transport errors. Returns ``(ok, error)`` like the wrapped call;
    ``error`` is the LAST attempt's error on exhaustion.

    Non-transient outcomes (success, or a permanent failure) short-circuit
    immediately — only ``_is_transient_gateway_error`` errors are retried.
    """
    attempts = len(_GATEWAY_RETRY_BACKOFF_SECONDS) + 1
    last_error: str | None = None
    for attempt in range(attempts):
        ok, error = _dispatch_via_openclaw(channel, chat_id, message, gateway_port)
        if ok:
            return True, None
        last_error = error
        if not _is_transient_gateway_error(error):
            return False, error
        if attempt < attempts - 1:
            _retry_sleep(_GATEWAY_RETRY_BACKOFF_SECONDS[attempt])
    return False, last_error


def _dispatch_via_telegram_http(chat_id: str, message: str) -> tuple[bool, str | None]:
    """Send a Telegram message via the bot HTTP API directly.

    Avoids the openclaw CLI shutdown hang that was causing batched alerts to
    report FAILED while still delivering. Reads the bot token from the
    primary bot's openclaw.json — the dispatcher always runs as the evolve
    user (the admin-ui daemon's UserName), so the file is directly readable.

    Returns ``(True, None)`` on Telegram-confirmed delivery, ``(False, error)``
    otherwise. Error strings starting with ``"no-telegram-token"`` signal
    the caller to fall through to the openclaw CLI path.
    """
    import urllib.request
    import urllib.error

    # Resolve the bot token from the primary bot's openclaw.json.
    token_path = Path("/Users/evolve/.openclaw/openclaw.json")
    try:
        cfg = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"no-telegram-token: {exc}"
    bot_token = (cfg.get("channels", {}).get("telegram") or {}).get("botToken")
    if not bot_token:
        return False, "no-telegram-token: channels.telegram.botToken unset"

    # POST to Telegram's sendMessage endpoint. JSON body is the safest path
    # for messages with emoji — encoding-safe and no URL-length cap.
    #
    # parse_mode="HTML" (PR 2 of the dispatcher-safety rework). Only
    # ``<``, ``>``, ``&`` are special and the escape rules are
    # unambiguous (unlike legacy Markdown's `_*[\``` whose semantics
    # depend on pairing). Producers compose messages where:
    #   * literal template text may contain HTML tags (<b>, <i>, <code>)
    #   * interpolated dynamic values are escaped via ``catalog.html_escape``
    # See ``catalog.render_event`` and ``signal_notifier._render_fire``
    # for the producer-side discipline.
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return False, f"telegram returned non-JSON: {body[:200]}"
        if obj.get("ok") is True:
            return True, None
        return False, f"telegram api error: {obj.get('description', body[:200])}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return False, f"telegram http {e.code}: {err_body[:200] or e.reason}"
    except urllib.error.URLError as e:
        return False, f"telegram network error: {e.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"telegram send error: {exc}"


# Permanent, TARGET-level delivery failures. Each of these means the
# chat/channel/token is unreachable with the current config — the bot was
# never /start-ed in DM, was blocked or kicked, the chat migrated to a new
# -100… id, the user deactivated, or the token is wrong/revoked. No amount
# of per-minute retry fixes any of them; only operator action does. Matched
# case-insensitively against the raw error string from EITHER delivery path:
#   * the openclaw CLI's unstructured stderr (e.g. "Telegram send failed:
#     chat not found (chat_id=…)") — the live #3152 shape; and
#   * the Telegram HTTP path's api-error description, which arrives under
#     HTTP 200 with ``{"ok": false, "description": "Bad Request: chat not
#     found"}`` — so it carries NO "http 4" code either.
#
# Deliberately SHORT and specific. An over-broad entry here would suppress a
# legitimate alert (see _is_permanent_failure / _is_delivery_target_down),
# so UNKNOWN errors are intentionally left OFF this list and stay transient
# (today's behavior).
_TARGET_DOWN_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "chat not found",
    "bot was blocked",
    "bot was kicked",
    "user is deactivated",
    "wrong bot token",
    "unauthorized",
    "invalid token",
)


def _is_delivery_target_down(error: str | None) -> bool:
    """True when the error indicates the delivery TARGET (chat/channel/token)
    is unreachable — a channel-level outage, distinct from a per-message
    format error (HTTP 400 parse).

    signal_notifier uses this to switch the whole target into quiet
    digest-on-recovery mode instead of hammering each backlogged signal.
    Conservative: ONLY the explicit ``_TARGET_DOWN_ERROR_SUBSTRINGS`` shapes
    qualify. A bare ``http 4`` (which could be a recoverable-by-edit message
    parse error) does NOT — so a single malformed message body never gets
    mistaken for a channel outage.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(s in lowered for s in _TARGET_DOWN_ERROR_SUBSTRINGS)


def _is_permanent_failure(error: str | None) -> bool:
    """Classify a dispatch error as permanent (won't succeed on retry) or
    transient (worth retrying).

    Permanent — message body / chat_id / token is wrong and no amount of
    retry fixes it. Per-minute retry loops on these are catastrophic;
    signal_notifier reads this flag to mark the offending signal id as
    permanently-failed and stop announcing it.

    Transient — network blips, 5xx server errors, timeouts. State stays
    in place so the next tick retries.

    Two rules, both conservative:

    1. ``http 4`` in the error (covers 400/401/403/404/etc.) — the telegram
       HTTP path prefixes its error with ``"telegram http {code}: …"``.
    2. An explicit allowlist of TARGET-level shapes
       (``_TARGET_DOWN_ERROR_SUBSTRINGS``) — this is what catches the
       openclaw CLI path and the Telegram api-error path, neither of which
       exposes a structured ``http 4`` code (the #3152 "chat not found"
       flood root cause: the CLI's unstructured error was treated as
       transient and retried every 60s for hours).

    Any UNKNOWN CLI error stays transient by default — an over-eager
    "permanent" classification can suppress a legitimate alert.
    """
    if not error:
        return False
    lowered = error.lower()
    if "http 4" in lowered:
        return True
    return _is_delivery_target_down(error)


def _resolve_pod_context(network: dict[str, Any] | None) -> dict[str, str]:
    """Thin wrapper around :func:`evolve_admin.config.resolve_pod_context`.

    Kept as a private dispatcher-local name for callers that still import
    it. New code should import ``resolve_pod_context`` from
    ``evolve_admin.config`` directly — config is the natural home for
    operator-environment resolution helpers (alongside ``get_bot_user``,
    ``get_bot_port``) and has no reverse-import constraints from
    the alerts subpackage.
    """
    from ..config import resolve_pod_context
    return resolve_pod_context(network)


def _resolve_gateway_port(network: dict[str, Any] | None) -> int | None:
    """Return the primary bot's gateway port from network config, or None.

    The openclaw CLI defaults to port 18789, but each bot is assigned a
    unique port in its gateway plist. Without the correct port the CLI
    connects to nothing and hangs. We read from network.json (bots.<id>.port)
    with a fallback to the bot's openclaw.json gateway.port field.
    """
    if not network:
        return None
    try:
        from primary_bot import primary_bot_id
        from ..config import get_bot_port
        pid = primary_bot_id(network)
        if pid:
            return get_bot_port(pid, network)
    except Exception:
        pass
    return None


# ─── Config reads (defensive against missing schema entries) ─────────────────


def _read_dispatcher_enabled(shared_dir: Path) -> bool:
    return _read_config_value(
        shared_dir,
        path="alerts.dispatcher_enabled",
        default=_DEFAULT_DISPATCHER_ENABLED,
    )


def _read_source_enabled(shared_dir: Path, source: str) -> bool:
    return _read_config_value(
        shared_dir,
        path=f"alerts.{source}.enabled",
        default=_DEFAULT_SOURCE_ENABLED.get(source, True),
    )


def _read_source_cooldown(shared_dir: Path, source: str) -> int:
    return int(_read_config_value(
        shared_dir,
        path=f"alerts.{source}.cooldown_seconds",
        default=_DEFAULT_SOURCE_COOLDOWN_SECONDS.get(source, 0),
    ))


def _read_config_value(shared_dir: Path, *, path: str, default: Any) -> Any:
    """Resolve a config_sandbox key. Defensive against:

    - the schema entry not yet existing (Phase 2 adds them)
    - the better-engine-config.json file being absent
    - the config_sandbox module raising for any reason

    Always returns *something* — either the stored value, the entry's
    stock default, or our compiled-in default.
    """
    try:
        from . import _config_lookup  # local import → no circular/import-time cost
        return _config_lookup.lookup(shared_dir, path, default)
    except Exception:
        return default


# ─── State file (cooldown tracking) ──────────────────────────────────────────


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "alerts" / "dispatcher-state.json"


def _load_state(shared_dir: Path) -> dict[str, Any]:
    p = _state_path(shared_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "last_dispatch": {}}


def _save_state(shared_dir: Path, state: dict[str, Any]) -> None:
    p = _state_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
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


def _state_key(source: str, dedup_key: str) -> str:
    return f"{source}::{dedup_key}"


@dataclass(frozen=True)
class _DispatchStateEntry:
    """Snapshot of the dispatcher's per-(source, dedup_key) state.

    ``body_hash`` is None on entries written before the identical-
    content suppression spec landed; the suppression check treats
    None as "no match" so an in-flight upgrade fails open rather than
    blocking alerts on first read of a legacy state file.
    """

    ts: datetime
    body_hash: str | None


def _last_dispatch_entry(
    shared_dir: Path, source: str, dedup_key: str
) -> _DispatchStateEntry | None:
    state = _load_state(shared_dir)
    entry = state.get("last_dispatch", {}).get(_state_key(source, dedup_key))
    if not entry:
        return None
    ts_raw = entry.get("ts")
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    body_hash = entry.get("body_hash")
    if body_hash is not None and not isinstance(body_hash, str):
        body_hash = None  # malformed; treat as legacy
    return _DispatchStateEntry(ts=ts, body_hash=body_hash)


def _record_dispatch(
    shared_dir: Path, source: str, dedup_key: str, now: datetime,
    body_hash: str,
) -> None:
    state = _load_state(shared_dir)
    state.setdefault("last_dispatch", {})[_state_key(source, dedup_key)] = {
        "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "result": DispatchResult.SENT.value,
        "body_hash": body_hash,
    }
    _save_state(shared_dir, state)


def _body_hash(message: str) -> str:
    """Stable 16-char SHA256 prefix of the rendered message text.

    Used by the identical-content suppression layer. 16 chars (8 bytes)
    is more than enough — collision probability across a single
    dispatcher's lifetime is negligible — and keeps state file
    entries compact.
    """
    import hashlib
    return hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()[:16]


# ─── JSONL logs ──────────────────────────────────────────────────────────────


def _log_filename_for(outcome: DispatchOutcome) -> str:
    """Pick the JSONL log a finished-dispatch outcome belongs in.

    Three lanes:
      * ``dispatcher.jsonl`` — successful (or deferred-to-digest) delivery.
        The Recent Messages list on Reports → Subscriptions surfaces these.
      * ``delivery-failures.jsonl`` — the send was attempted upstream and
        failed (telegram HTTP 4xx, slack rate-limit, openclaw subprocess
        error). Surfaced separately on the Dispatcher Health panel so the
        operator's Messages list isn't polluted with HTTP-error payloads
        from retry storms.
      * ``dispatcher-suppressed.jsonl`` — the dispatcher chose not to send
        (master switch off, source disabled, cooldown, subscription off,
        no recipient). Hidden by default in the UI; surfaced under the
        "include suppressed" toggle.

    Centralised here so callers don't have to remember which file each
    DispatchResult belongs to — and so adding a new FAILED code path
    elsewhere in the dispatcher automatically routes correctly.
    """
    if outcome.result == DispatchResult.FAILED:
        return "delivery-failures.jsonl"
    if outcome.result in (
        DispatchResult.SENT,
        DispatchResult.DEFERRED,
        # Rate-capped alerts are downgraded-to-digest, not dropped — they
        # belong in the operator's Recent Messages list (with a
        # breaker_state field) so a quiet phone always has a paper trail.
        DispatchResult.BATCHED_RATE_CAP,
    ):
        return "dispatcher.jsonl"
    return "dispatcher-suppressed.jsonl"


# Filenames the dispatcher writes that should NOT be date-partitioned —
# they're queues drained by another daemon, not append-only log streams.
_FLAT_LOG_FILENAMES: frozenset[str] = frozenset({
    "digest-pending/daily.jsonl",
    "digest-pending/weekly.jsonl",
})


def _resolve_log_path(
    shared_dir: Path, filename: str, record: dict[str, Any],
) -> Path:
    """Map a logical log filename to its on-disk date-partitioned path.

    Log streams (``dispatcher.jsonl``, ``dispatcher-suppressed.jsonl``,
    ``delivery-failures.jsonl``) are date-partitioned under
    ``alerts/<basename>/<YYYY-MM-DD>.jsonl`` — matching the
    ``signals/log/<YYYY-MM-DD>.jsonl`` and ``watchdog/<YYYY-MM-DD>.jsonl``
    patterns. The day is taken from ``record["ts"]`` (an ISO string the
    dispatcher already writes) so out-of-order writes still land in the
    correct day file.

    Anchor: 2026-06-01 incident. ``dispatcher-suppressed.jsonl`` reached
    306 MB / 803,266 lines in 22 days because nothing rotated it. Without
    a partition + retention sweep, every alerts-volume regression is one
    disk-full away from wedging the pod.

    Digest-pending queues stay flat — they're drained by
    ``digest_dispatcher``, not pruned by retention.
    """
    if filename in _FLAT_LOG_FILENAMES:
        return shared_dir / "alerts" / filename
    basename = filename[:-len(".jsonl")] if filename.endswith(".jsonl") else filename
    day = _record_day(record)
    return shared_dir / "alerts" / basename / f"{day}.jsonl"


def _record_day(record: dict[str, Any]) -> str:
    """ISO date (``YYYY-MM-DD``) from ``record["ts"]``, or today (UTC).

    The dispatcher writes RFC3339 timestamps with ``Z`` suffix; the first
    10 chars are the date. Fall back to UTC today for records that
    somehow lack a parseable ts so a missing field never blocks the
    write — losing a log line on a bad record is worse than partitioning
    it under today.
    """
    ts = record.get("ts") or ""
    if (
        len(ts) >= 10
        and ts[4:5] == "-" and ts[7:8] == "-"
        and ts[:4].isdigit() and ts[5:7].isdigit() and ts[8:10].isdigit()
    ):
        return ts[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _append_log(shared_dir: Path, filename: str, record: dict[str, Any]) -> None:
    p = _resolve_log_path(shared_dir, filename, record)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        # Best-effort. Losing a log line is preferable to bubbling up an
        # exception that would prevent the alert from being sent.
        pass


# ─── Log readers (public — used by UI routes and loop monitor) ───────────────


def iter_log_records(
    shared_dir: Path,
    filename: str,
    *,
    since: datetime | None = None,
    include_legacy_flat: bool = True,
) -> Iterable[dict[str, Any]]:
    """Yield records from a dispatcher log stream, newest-day first.

    ``filename`` is the logical log name (``"dispatcher.jsonl"``,
    ``"dispatcher-suppressed.jsonl"``, ``"delivery-failures.jsonl"``) —
    the same value :func:`_log_filename_for` returns.

    Reads each date-partitioned daily file under
    ``alerts/<basename>/<YYYY-MM-DD>.jsonl`` in reverse date order so a
    caller that takes the first N records gets the most recent N. If
    ``since`` is given, day files dated before that day are skipped
    (records with stale ``ts`` inside a day file are NOT filtered —
    callers apply per-record ts filtering when they need precision).

    The pre-rotation flat file (``alerts/<basename>.jsonl``) is read last
    when ``include_legacy_flat=True`` so deployments with a non-empty
    pre-PR file still surface their history during the transition window.
    Once the retention sweep drains the flat-file age beyond the cutoff,
    operators can delete it by hand; the dispatcher itself never writes
    there again.

    Malformed JSON lines are silently skipped — losing a single record
    is preferable to a reader-side traceback on a corrupted tail.
    """
    for p in _resolve_log_files_newest_first(
        shared_dir, filename, since=since, include_legacy_flat=include_legacy_flat
    ):
        yield from _iter_jsonl(p)


def _resolve_log_files_newest_first(
    shared_dir: Path,
    filename: str,
    *,
    since: datetime | None = None,
    include_legacy_flat: bool = True,
) -> Iterable[Path]:
    """Yield the on-disk files backing a logical log stream, newest-day first.

    Single source of truth for the date-partitioned / legacy-flat path
    resolution that both :func:`iter_log_records` (head reader) and
    :func:`iter_log_records_recent` (tail reader) need. Returns *files*,
    not records, so the two readers can differ only in how they walk each
    file's lines. See :func:`iter_log_records` for the path layout and
    ``since`` semantics.
    """
    if filename in _FLAT_LOG_FILENAMES:
        yield shared_dir / "alerts" / filename
        return

    basename = filename[:-len(".jsonl")] if filename.endswith(".jsonl") else filename
    log_dir = shared_dir / "alerts" / basename

    since_day_iso: str | None = None
    if since is not None:
        since_day_iso = since.astimezone(timezone.utc).date().isoformat()

    if log_dir.exists():
        # Sort by filename (ISO YYYY-MM-DD sorts chronologically). Newest first.
        candidates = []
        for p in log_dir.glob("*.jsonl"):
            stem = p.stem
            # Defensive: only accept files whose stems parse as dates.
            try:
                date.fromisoformat(stem)
            except ValueError:
                continue
            if since_day_iso is not None and stem < since_day_iso:
                continue
            candidates.append(p)
        candidates.sort(key=lambda p: p.stem, reverse=True)
        yield from candidates

    if include_legacy_flat:
        legacy = shared_dir / "alerts" / filename
        if legacy.is_file():
            yield legacy


def iter_log_records_recent(
    shared_dir: Path,
    filename: str,
    *,
    max_records: int,
    since: datetime | None = None,
    include_legacy_flat: bool = True,
) -> Iterable[dict[str, Any]]:
    """Yield up to ``max_records`` of the MOST RECENT records, newest-day first.

    Counterpart to :func:`iter_log_records`. ``iter_log_records`` walks each
    day file (newest day first) from the START — fine when a caller consumes
    every record, but a caller that takes only the first N gets the OLDEST N
    of the newest day, because ``_iter_jsonl`` yields a file's lines in file
    (oldest-first) order. On a storm day a single day file can hold thousands
    of lines, so "first N of newest day" surfaces early-morning records and
    hides everything sent since. This reader instead takes the *tail* of each
    day file so the records it yields are genuinely the most recent.

    Within each day file the records are yielded oldest-first (their on-disk
    order); the cross-file order is newest-day-first. Callers that need a
    strict newest-first ordering (e.g. the Messages feed) sort by ``ts`` after
    collecting — the point of this reader is only to guarantee the collected
    set is the recent tail, not the stale head.

    Memory: a ``deque(maxlen=…)`` over the file's line generator is O(lines)
    time but only O(``max_records``) memory, so multi-MB day files are read
    without buffering the whole file. A true seek-from-end would be cheaper on
    time but materially more complex (UTF-8 / partial-line handling); the
    bounded deque is simpler and correct, and ``max_records`` is small
    (hundreds), so the time cost is a cheap linear scan of the recent days.
    """
    if max_records <= 0:
        return
    gathered = 0
    for p in _resolve_log_files_newest_first(
        shared_dir, filename, since=since, include_legacy_flat=include_legacy_flat
    ):
        remaining = max_records - gathered
        if remaining <= 0:
            break
        # Bounded tail of this day file: last ``remaining`` records. The deque
        # keeps only the final ``remaining`` lines seen, discarding earlier
        # ones as it scans — O(file) time, O(remaining) memory.
        tail = collections.deque(_iter_jsonl(p), maxlen=remaining)
        for rec in tail:
            yield rec
            gathered += 1
        if gathered >= max_records:
            break


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        return


def _audit_record(
    now: datetime,
    outcome: DispatchOutcome,
    message: str,
) -> dict[str, Any]:
    excerpt = message[:_AUDIT_EXCERPT_CHARS]
    if len(message) > _AUDIT_EXCERPT_CHARS:
        excerpt += "…"
    rec: dict[str, Any] = {
        "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": outcome.source,
        "severity": outcome.severity.value,
        "dedup_key": outcome.dedup_key,
        "result": outcome.result.value,
        "message_excerpt": excerpt,
    }
    if outcome.channel:
        rec["channel"] = outcome.channel
    if outcome.chat_id:
        rec["chat_id"] = outcome.chat_id
    if outcome.error:
        rec["error"] = outcome.error
    if outcome.catalog_event:
        rec["catalog_event"] = outcome.catalog_event
    return rec


# Logical name of the dead-letter store. Date-partitioned like the other
# dispatcher log streams (alerts/dead-letter/<YYYY-MM-DD>.jsonl) and pruned by
# the same retention sweep (signals.retention._ALERTS_LOG_STREAMS).
_DEAD_LETTER_LOG = "dead-letter.jsonl"


def _record_dead_letter(
    shared_dir: Path,
    now: datetime,
    outcome: DispatchOutcome,
    message: str,
) -> None:
    """Persist a permanently-undeliverable message so it is never silently
    dropped, and surface it in the operator's delivery audit.

    Two writes, both best-effort:

      * ``alerts/dead-letter/<day>.jsonl`` — durable record carrying the FULL
        message body (not just the excerpt) for operator inspection and a
        future replay path.
      * ``alerts/dispatcher/<day>.jsonl`` (the Recent Messages feed) — a
        terminal ``result="dead_letter"`` row so the message we gave up on
        shows in the operator's ground-truth audit instead of vanishing. This
        is distinct from the transient ``result="failed"`` rows the Messages
        route filters out (those are will-retry noise that lives only on the
        Dispatcher Health panel); a dead-letter is the final disposition.
    """
    audit = _audit_record(now, outcome, message)
    feed_row = {**audit, "result": "dead_letter"}
    store_row = {**feed_row, "message": message}
    _append_log(shared_dir, _DEAD_LETTER_LOG, store_row)
    _append_log(shared_dir, "dispatcher.jsonl", feed_row)


# ─── Suppression helpers ─────────────────────────────────────────────────────


def _suppressed(
    result: DispatchResult,
    source: str,
    severity: Severity,
    dedup_key: str | None,
    message: str,
    shared_dir: Path,
    now: datetime,
    *,
    reason: str,
    catalog_event: str | None = None,
) -> DispatchOutcome:
    outcome = DispatchOutcome(
        result=result,
        source=source, severity=severity, dedup_key=dedup_key,
        error=reason,
        catalog_event=catalog_event,
    )
    _append_log(shared_dir, "dispatcher-suppressed.jsonl",
                _audit_record(now, outcome, message))
    return outcome


# ─── Global rate breaker glue (logic lives in rate_breaker.py) ───────────────


# Sources exempt from the global rate breaker. The breaker is consulted
# at the immediate-send chokepoint, so two internal sources must skip it:
#   * ``alert_rate_breaker`` — the breaker's OWN storm-notice/heartbeat
#     send (recursing through send() with a recipient_override). Counting
#     or capping it would let the cap silence the message that announces
#     the cap, and risks a feedback loop.
#   * ``digest_dispatcher`` — the digest FLUSH is the drain that delivers
#     batched alerts. It's one bundled message, not a producer; subjecting
#     it to the cap (or storm digest-only mode) could defer the very
#     delivery that relieves the storm.
_BREAKER_EXEMPT_SOURCES: frozenset[str] = frozenset({
    "alert_rate_breaker",
    "digest_dispatcher",
})


def _is_bypass_alert(severity: Severity, catalog_event: str | None) -> bool:
    """Whether an alert may bypass the normal rate cap (under a higher
    secondary ceiling). CRITICAL severity always bypasses; so does any
    catalog event flagged ``is_safety_critical`` (gateway down, security
    floor). Everything else is subject to the normal cap."""
    if severity == Severity.CRITICAL:
        return True
    if catalog_event:
        try:
            from .catalog import by_key
            entry = by_key(catalog_event)
            if entry is not None:
                if getattr(entry, "is_safety_critical", False):
                    return True
                # Honor the catalog event's OWN declared severity too — a
                # CRITICAL catalog event sent by a caller that didn't mirror
                # the severity into the `severity` arg must still bypass the
                # cap, else a genuinely critical alert could be day-delayed.
                entry_sev = getattr(entry, "severity", None)
                if getattr(entry_sev, "value", None) == Severity.CRITICAL.value:
                    return True
        except Exception:
            # Catalog lookup unavailable/failed → can't confirm a bypass, so
            # treat as a normal (cap-eligible) alert. Conservative: a flaky
            # catalog read must not let everything bypass the backstop.
            return False
    return False


def _consult_rate_breaker(
    shared_dir: Path,
    *,
    channel: str,
    source: str,
    severity: Severity,
    catalog_event: str | None,
    now: datetime,
):
    """Consult the global breaker. Returns a ``BreakerDecision`` or ``None``.

    ``None`` means "don't apply the breaker" — either the source is exempt
    (the breaker's own notice / the digest drain) or the breaker module
    couldn't be loaded (fail open). The caller treats ``None`` as allow.
    """
    if source in _BREAKER_EXEMPT_SOURCES:
        return None
    try:
        from . import rate_breaker
    except Exception:
        return None
    try:
        return rate_breaker.evaluate(
            shared_dir,
            channel=channel,
            is_bypass=_is_bypass_alert(severity, catalog_event),
            now=now,
        )
    except Exception:
        # Backstop must never itself block a legitimate alert.
        return None


def _emit_storm_notice(
    shared_dir: Path,
    network: dict[str, Any] | None,
    channel: str,
    chat_id: str,
    notice: str,
    now: datetime,
) -> None:
    """Deliver the one-per-episode storm notice (or heartbeat).

    Routed back through :func:`send` with the breaker-exempt source and an
    explicit recipient_override so it bypasses the very cap it's
    announcing. dedup_key=None so no cooldown/identical gate applies — the
    breaker itself owns "exactly once + hourly heartbeat". Best-effort: a
    failed notice never affects the alert that triggered it.
    """
    try:
        send(
            shared_dir=shared_dir,
            network=network,
            source="alert_rate_breaker",
            message=notice,
            severity=Severity.WARNING,
            dedup_key=None,
            recipient_override=(channel, chat_id),
            # Subscription-completeness (spec-subscription-completeness-
            # 2026-06-24): storm-mode notices are meta messages ABOUT the
            # alerting system. meta.storm_mode is IMMEDIATE-only + default-on,
            # so the operator gets a handle to mute the heartbeat without
            # changing the breaker's own gating, and binding it can't
            # re-enqueue the notice into a digest queue.
            catalog_event="meta.storm_mode",
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        # Best-effort: a failed storm notice must not affect the alert that
        # triggered it. Log (don't swallow) — the breaker's heartbeat is the
        # recovery path for a dropped episode-start notice.
        import logging
        logging.getLogger(__name__).debug("storm notice send failed: %s", exc)


def _batch_rate_capped(
    shared_dir: Path,
    now: datetime,
    *,
    source: str,
    severity: Severity,
    dedup_key: str | None,
    catalog_event: str | None,
    message: str,
    channel: str,
    chat_id: str,
    decision,
) -> DispatchOutcome:
    """Roll an over-cap / storm alert into the daily digest (never drop).

    Enqueues into the existing daily-digest queue (drained by
    ``digest_dispatcher`` on the operator's schedule) and records a
    BATCHED_RATE_CAP entry in the recent-messages log with the breaker's
    state/reason so the operator can see WHY their phone went quiet.
    """
    _enqueue_digest(
        shared_dir, "daily_digest", now,
        source=source, catalog_event=catalog_event,
        severity=severity, dedup_key=dedup_key, message=message,
    )
    outcome = DispatchOutcome(
        result=DispatchResult.BATCHED_RATE_CAP,
        source=source, severity=severity, dedup_key=dedup_key,
        channel=channel, chat_id=chat_id,
        catalog_event=catalog_event,
        error=f"rate_breaker:{decision.reason}",
    )
    rec = _audit_record(now, outcome, message)
    rec["breaker_state"] = decision.state
    rec["breaker_reason"] = decision.reason
    rec["breaker_window_count"] = decision.window_count
    rec["breaker_attempts_count"] = decision.attempts_count
    _append_log(shared_dir, _log_filename_for(outcome), rec)
    return outcome


# ─── Recurring-flapper glue (logic lives in flapper.py) ──────────────────────


def _consult_flapper(
    shared_dir: Path,
    *,
    source: str,
    digest_meta: dict[str, Any] | None,
    now: datetime,
):
    """Consult the recurring-flapper demotion. Returns a ``FlapperDecision``
    or ``None``.

    ``None`` means "don't apply the flapper": the send carries no fire/clear
    pairing metadata (``digest_meta`` absent or lacking ``coalesce_key`` /
    ``kind``), the source is breaker-exempt (internal notices / the digest
    drain), or the module couldn't be loaded (fail open). The caller treats
    ``None`` — and a ``"pass"`` action — as deliver-normally.

    The Signal's OWN severity is read from ``digest_meta["signal_severity"]``
    (set by ``signal_notifier._digest_pair_meta``), NOT the wrapper severity a
    resolve send uses — so the alert-exempt gate inside ``flapper.evaluate``
    sees the truth.
    """
    if not digest_meta:
        return None
    coalesce_key = digest_meta.get("coalesce_key")
    kind = digest_meta.get("kind")
    if not coalesce_key or kind not in ("fire", "resolve"):
        return None
    if source in _BREAKER_EXEMPT_SOURCES:
        return None
    try:
        from . import flapper
    except Exception:
        return None
    try:
        return flapper.evaluate(
            shared_dir,
            source=source,
            coalesce_key=str(coalesce_key),
            kind=str(kind),
            severity=str(digest_meta.get("signal_severity") or ""),
            now=now,
        )
    except Exception:
        # The demotion layer must never itself block a legitimate alert.
        return None


def _demote_flapper(
    shared_dir: Path,
    now: datetime,
    *,
    source: str,
    severity: Severity,
    dedup_key: str | None,
    catalog_event: str | None,
    message: str,
    digest_meta: dict[str, Any] | None,
    decision,
) -> DispatchOutcome:
    """Route a demoted signature's fire/clear into the daily digest (no page).

    A demoted fire is enqueued to the daily digest (carrying the L1 pairing
    ``digest_meta`` so the digest still collapses/pairs it); a demoted clear is
    taken off the immediate-push path the same way — its standalone "🟢
    Cleared" page is what we're killing. Both return ``DEFERRED`` so the
    operator gets them on the next digest flush and ``signal_notifier`` marks
    the signature handled (no per-tick retry). The audit record carries the
    flap fields so the Recent Messages feed — and ``flap_health`` for the
    Reports / Dispatcher Health panel — explain WHY the condition stopped
    paging, rather than it going silently quiet.
    """
    _enqueue_digest(
        shared_dir, "daily_digest", now,
        source=source, catalog_event=catalog_event,
        severity=severity, dedup_key=dedup_key, message=message,
        extra=digest_meta,
    )
    outcome = DispatchOutcome(
        result=DispatchResult.DEFERRED,
        source=source, severity=severity, dedup_key=dedup_key,
        catalog_event=catalog_event,
        error=f"flap_{decision.action}:{decision.reason}",
    )
    rec = _audit_record(now, outcome, message)
    rec["flap_demoted"] = True
    rec["flap_action"] = decision.action
    rec["flap_reason"] = decision.reason
    rec["flap_count"] = decision.flap_count
    rec["flap_newly_demoted"] = decision.newly_demoted
    rec["flap_coalesce_key"] = decision.coalesce_key
    _append_log(shared_dir, _log_filename_for(outcome), rec)
    return outcome


# ─── Catalog-side rendering (Phase F) ───────────────────────────────────────


def _render_via_catalog(catalog_event: str, payload: dict[str, Any]) -> str | None:
    """Render a catalog event with the caller's payload, or ``None`` if
    rendering fails for any reason.

    Failures include: unknown event_key (typo), missing placeholder
    in the payload, catalog module unavailable. Callers handle the
    None case by falling back to their pre-rendered ``message``
    (Phase F migration tolerates partial adoption).
    """
    try:
        from .catalog import by_key, render_event
    except Exception:
        return None
    entry = by_key(catalog_event)
    if entry is None:
        return None
    try:
        return render_event(entry, payload)
    except (KeyError, ValueError, AttributeError):
        # KeyError = placeholder missing from payload; ValueError = format
        # mismatch (e.g. {amount:.2f} got a string); AttributeError =
        # catalog action wired wrong. In every case, fall back to the
        # caller's legacy message rather than crash the dispatch.
        return None


# ─── PWA Push fanout helpers (Phase 1.2) ────────────────────────────────────


def _fanout_pwa_push_if_enabled(
    shared_dir: Path,
    *,
    catalog_event: str | None,
    message: str,
    severity: Severity,
) -> dict[str, int] | None:
    """Fan out the message to PWA push subscriptions if the channel is on.

    Returns a small ``{"attempted", "sent", "failed", "removed_invalid"}``
    dict for inclusion in the audit log entry, or ``None`` when push
    fanout was not attempted (no catalog_event / channel off / no
    subscriptions module / no subscribers).

    Failures inside the fanout are logged but never propagate — the
    primary chat outcome is the dispatcher's contract.
    """
    if catalog_event is None:
        return None
    try:
        from . import subscriptions as _subs_mod
    except Exception:
        return None
    try:
        if not _subs_mod.read_pwa_push_enabled(shared_dir):
            return None
    except Exception:
        return None
    try:
        from . import push_sender
    except Exception:
        return None
    title, body = _split_message_for_push(message)
    try:
        result = push_sender.fanout_pwa_push(
            shared_dir,
            title=title,
            body=body,
            url="/",
            event_key=catalog_event,
            severity=severity.value,
        )
    except Exception:
        # Belt and braces — the inner function already absorbs per-sub
        # errors; this guard is for catastrophic import/setup failures.
        return None
    if result.attempted == 0:
        return None
    return {
        "attempted": result.attempted,
        "sent":      result.sent,
        "failed":    result.failed,
        "removed_invalid": result.removed_invalid,
    }


def _split_message_for_push(message: str) -> tuple[str, str]:
    """Split a rendered catalog message into a push (title, body) pair.

    The catalog renderer's body_template starts with a one-line header
    (e.g. "🛡️ New security finding") followed by 1-3 detail lines and
    a trailing subscription-id footer. For a phone notification we want:

      - title = the header line (so the user sees the gist in the
        lockscreen-truncated form)
      - body  = the detail lines, footer stripped (the operator already
        knows which subscription they're on)
    """
    raw_lines = message.splitlines()
    # Drop the trailing subscription footer if present so it doesn't
    # crowd the notification body.
    lines: list[str] = []
    for ln in raw_lines:
        if ln.strip().startswith("subscription:"):
            continue
        lines.append(ln)
    # Trim trailing blanks left by the footer-strip.
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ("Evolve", "")
    title = lines[0].strip() or "Evolve"
    body = "\n".join(lines[1:]).strip()
    return (title, body)


# ─── Catalog gating helpers (Phase A2) ──────────────────────────────────────


def _resolve_catalog_subscription(
    shared_dir: Path, catalog_event: str,
) -> tuple[bool, str] | None:
    """Return ``(enabled, frequency_value)`` for an event, or ``None`` if
    the event key is unknown to the catalog.

    Phase A3: reads catalog defaults overlaid by operator overrides
    persisted at ``{shared_dir}/alerts/subscriptions.json``. Operator
    overrides win when set; otherwise the catalog default applies.

    Fails open: if the subscriptions module can't be imported or the
    overrides file is corrupt, fall back to catalog defaults so the
    dispatcher never crashes on a bad preferences file.
    """
    try:
        from .subscriptions import read_subscription
    except Exception:
        # Subscriptions module unavailable — fall back to catalog defaults
        # via direct catalog read so the dispatcher still gates correctly.
        try:
            from .catalog import by_key
        except Exception:
            return None
        entry = by_key(catalog_event)
        if entry is None:
            return None
        return (entry.default_enabled, entry.default_frequency.value)

    sub = read_subscription(shared_dir, catalog_event)
    if sub is None:
        return None
    return (sub.enabled, sub.frequency.value)


def _resolve_catalog_notify(catalog_event: str) -> bool:
    """Return whether ``catalog_event`` reaches the operator's chat channel.

    Thin wrapper over ``catalog.notify_for`` so the send path stays
    decoupled from the catalog import. Fails OPEN — if the catalog can't be
    imported, return ``True`` so the gate can never silently swallow a
    message because of an import error (the louder, safer default, matching
    the unknown-event fall-through in ``send``).
    """
    try:
        from .catalog import notify_for
    except Exception:
        return True
    return notify_for(catalog_event)


def _frequency_floor_seconds(frequency: str) -> int:
    """Cooldown floor implied by a non-digest, non-OFF frequency choice.

    Operator's "Once per day max" should hold as a hard floor even if
    the source passed a shorter cooldown. ``immediate`` / ``daily`` /
    ``weekly`` impose no extra floor — those rely on either the source's
    cooldown or natural cadence.
    """
    return {
        "once_per_day_max":  86_400,
        "once_per_week_max": 604_800,
    }.get(frequency, 0)


def _enqueue_digest(
    shared_dir: Path, frequency: str, now: datetime,
    *,
    source: str,
    catalog_event: str | None,
    severity: Severity,
    dedup_key: str | None,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one event to the digest queue.

    Phase G adds a flush daemon that drains the queue on the operator's
    schedule (daily at ``digest_hour_local`` / weekly Mondays). Until
    Phase G, events accumulate but no chat push happens — by design,
    digest mode is opt-in via subscription preferences and operators
    who pick it accept the deferred delivery.

    ``extra`` (the caller's ``digest_meta``) is merged into the record for
    flush-time consumers — currently the L1 fire/clear pairing keys the
    digest_dispatcher uses to elide a within-grace transient. Reserved keys
    (the ones built below) always win, so render-critical fields can't be
    clobbered by an errant caller.
    """
    if frequency == "daily_digest":
        filename = "digest-pending/daily.jsonl"
    elif frequency == "weekly_digest":
        filename = "digest-pending/weekly.jsonl"
    else:
        return  # caller's bug; nothing to do
    record = {
        "ts": _iso(now),
        "source": source,
        "catalog_event": catalog_event,
        "severity": severity.value,
        "dedup_key": dedup_key,
        # On-disk collapse key for the flush-time de-dup
        # (``digest_dispatcher._dedup_records``). The digest queue is a flat,
        # append-only file drained at most ``_DIGEST_MAX_CHUNKS_PER_FLUSH``
        # chunks per once-daily flush, so a recurring digest-only signal that
        # re-enqueues every tick (storm-mode rate-cap, a repeating
        # signal_notifier fire/resolve) grows the file unbounded and the
        # flush never catches up. ``_dedup_records`` already collapses records
        # sharing ``(catalog_event, dedup_key)`` — but callers on the
        # resolve/recovery and rate-cap paths legitimately pass
        # ``dedup_key=None`` (no cooldown), so those records never collapsed
        # and were the bulk of the 2026-06 leak (4804/4827 records had no
        # dedup_key). Derive a stable fallback for collapse ONLY:
        #   * a real dedup_key wins (caller asked for this collapse identity);
        #   * otherwise fall back to (source, catalog_event, body_hash) so the
        #     SAME recurring body collapses to one line, while genuinely-unique
        #     events (distinct bodies — forge job completions, per-SHA commit
        #     notices) keep distinct hashes and stay separate, preserving the
        #     "dedup_key=None means don't collapse me" contract for real uniques.
        # This is collapse-only metadata; the cooldown/identical-content layer
        # still keys off the caller's ``dedup_key`` exactly as before.
        "digest_collapse_key": (
            dedup_key
            if dedup_key
            else f"auto:{source}:{catalog_event or ''}:{_body_hash(message)}"
        ),
        "message": message,   # full body — flush daemon renders the digest
    }
    if extra:
        # Merge caller metadata UNDER the reserved keys — the record's own
        # fields are authoritative so a stray ``message``/``severity`` in
        # ``extra`` can't corrupt the queue line.
        record = {**extra, **record}
    _append_log(shared_dir, filename, record)


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _excerpt(message: str) -> str:
    if len(message) <= _AUDIT_EXCERPT_CHARS:
        return message
    return message[:_AUDIT_EXCERPT_CHARS] + "…"


# ─── Convenience for callers / tests ─────────────────────────────────────────


def known_sources() -> Iterable[str]:
    """The set of sources the dispatcher recognizes by default. Used by
    tests and by future config-validation tooling."""
    return tuple(_DEFAULT_SOURCE_ENABLED.keys())
