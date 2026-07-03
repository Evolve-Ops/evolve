"""cascade.audit_runner — turn cascade telemetry into Signals + labels.

The bridge between the cascade detectors (anomaly_detector, labeler,
plus span-recorded runaway/dangerous-combo flags) and the pod's
standard alerting + calibration layers:

  Spans (cascade_telemetry producer)
    └── audit_runner
         ├── Signals (cascade_audit producer)  ← operator-facing alerts
         └── Labels (cascade/labels/<day>.jsonl) ← Phase 4 tuner input

Three Signal types, all under producer ``cascade_audit``:

  cascade_anomaly                   — per-bot, per-metric anomaly
                                      verdict (cost-per-turn or
                                      context-tokens-per-turn ratio vs
                                      baseline crossed the origin-aware
                                      threshold). Sweep-resolved on
                                      next run when the condition clears.

  cascade_dangerous_combo           — single span matched the 4-feature
                                      pattern (background trigger +
                                      tier1 + cascade-decided + huge
                                      context). The plugin already
                                      flagged the span; this runner
                                      surfaces it as an operator
                                      Signal. Session-scoped — never
                                      sweep-resolved (the event already
                                      happened; clearing the Signal is
                                      a triage action, not auto).

  cascade_runaway_rate_tripped      — a session crossed the runaway-
                                      rate hard cap ($X in $window
                                      minutes). Severity passed through
                                      from the span's recorded value
                                      (warn vs critical). Session-
                                      scoped, no sweep-resolve.

The labeler's output (LabeledOutcome records) is persisted to
``{shared_dir}/cascade/labels/<YYYY-MM-DD>.jsonl`` for Phase 4 to
consume. The audit_runner is the only place ``labeler.write_labels``
gets called in production.

Pod-scoped (no per-bot fanout) — cascade spans are pod-wide. Designed
for hourly launchd cadence; cheap pure-Python work.

Spec: docs/spec-tier-cascade-2026-05-26.md § audit layer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from evolve_config import get_shared_dir, load_config
from schema.signal import make_signature
from signals import store as signals_store

from cascade.anomaly_detector import (
    BotBaseline,
    classify_anomaly,
    classify_origin,
    compute_baseline_from_spans,
    compute_pod_median_baseline,
)
from cascade.labeler import (
    label_spans,
    write_labels,
)

PRODUCER = "cascade_audit"

# How far back the runner looks for spans. 65 minutes for "recent
# detections" matches the watchdog's "1h + 5min slack" convention —
# we want one full hour of activity plus margin for late spans.
_RECENT_WINDOW_MINUTES = 65

# Baseline window: how much history to feed compute_baseline_from_spans.
# 30 days is the spec default. Auto-bootstrap (pod median) kicks in
# automatically inside compute_baseline_from_spans when a bot has
# insufficient data.
_BASELINE_WINDOW_DAYS = 30


# ─────────────────────────────────────────────────────────────────────────────
# Span loading
# ─────────────────────────────────────────────────────────────────────────────


def _load_spans(
    shared_dir: Path,
    *,
    window_minutes: int,
    now: datetime | None = None,
) -> list[dict]:
    """Read recent cascade_telemetry spans from BOTH the per-bot dirs
    (where ``CascadeTelemetry.ts`` writes) AND the central
    ``observability/spans/`` dir (where Python producers write).

    Returns dict-shaped spans (matching the OpikSpan.to_dict() output).
    Safe on a brand-new pod with no spans yet — returns ``[]``.

    Earlier this read only via ``JsonlBackend`` (central dir only) and
    silently produced zero output on real pods because the plugin
    writes per-bot. The merge helper ``iter_turn_spans`` is the
    authoritative cross-location reader; route everything through it
    to avoid the cross-module path-contract bug class.
    """
    from observability.session_rollup import iter_turn_spans  # noqa: E402

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=window_minutes)
    try:
        return [s.to_dict() for s in iter_turn_spans(
            shared_dir, since=since, until=now, limit=200_000,
        )]
    except Exception:
        # Read paths are allowed to raise per opik_client docs — we
        # treat any read failure as "no data," same as a brand-new pod.
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Detector: dangerous-combo (per-session, session-scoped Signal)
# ─────────────────────────────────────────────────────────────────────────────


def _collect_dangerous_combo_signals(spans: Iterable[dict]) -> list[dict]:
    """Surface every dangerous-combo match as an operator Signal.

    The plugin's DangerousComboDetector already flagged these spans at
    write time. We just translate the span-recorded match into a
    Signal so the operator sees it on the Alerts page.

    Session-scoped — one Signal per session per detection. No sweep-
    resolve (the event already happened; operator triages by Snoozing
    or Resolving the Signal manually).
    """
    out: list[dict] = []
    # De-dupe by session-id so a session that fired the combo on
    # multiple turns only emits one Signal per run. The Signal's
    # observation_count handles repeated firings via observe()'s
    # find-or-create.
    seen: set[str] = set()
    for span in spans:
        attrs = span.get("attributes") or {}
        if not attrs.get("cascade.dangerous_combo.matched"):
            continue
        sid = attrs.get("session_id")
        if not isinstance(sid, str) or sid in seen:
            continue
        seen.add(sid)
        bot_id = span.get("bot_id") or "<unknown>"
        ctx_tokens = attrs.get("cascade.dangerous_combo.context_tokens")
        trigger = attrs.get("cascade.trigger_kind") or "<unknown>"
        sid_short = sid[:8]
        title = f"{bot_id}: dangerous-combo cascade decision (session {sid_short})"
        body = (
            f"Cascade decided tier1 (the most expensive model) for a "
            f"background session with {ctx_tokens or '?'} input tokens.\n\n"
            f"Trigger: `{trigger}`\n"
            f"Session: `{sid_short}` (full id in details)\n\n"
            "The four-feature pattern: background trigger + tier1 + "
            "cascade-decided + huge context. Spec § 2.6 calls this out "
            "as the cost-explosion shape we want to catch BEFORE the "
            "bill arrives. Review the session: was the context "
            "legitimate (e.g. a maintenance audit over a large "
            "manifest), or did the cascade controller over-escalate?"
        )
        out.append(dict(
            signature=make_signature(PRODUCER, "dangerous_combo", sid),
            producer=PRODUCER,
            type="dangerous_combo",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                session_id=sid,
                bot_id=bot_id,
                trigger_kind=trigger,
                context_tokens=ctx_tokens,
                vector="cost",
                magnitude=3,
                severity_active=True,
                what_it_means=(
                    "Cascade picked the most expensive tier for a "
                    "session that had no live user, with a context "
                    "large enough to make the bill substantial. This "
                    "is the exact pattern Phase 4 calibration is "
                    "supposed to teach the controller to avoid."
                ),
                fix_steps=(
                    "1. Open Reports → Sessions and find the session by id\n"
                    "2. Review the prompt+response — does this look like "
                    "legitimate high-context work or a thrashing loop?\n"
                    "3. If legitimate: dismiss the Signal\n"
                    "4. If pathological: file a Phase 4 calibration "
                    "input — the cascade controller's threshold for "
                    "this origin needs tuning"
                ),
            ),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detector: runaway-rate tripped (per-session, session-scoped Signal)
# ─────────────────────────────────────────────────────────────────────────────


def _collect_runaway_rate_signals(spans: Iterable[dict]) -> list[dict]:
    """Surface every runaway-rate trip as an operator Signal.

    The plugin's ModelRouter flagged the span at write time
    (`cascade.runaway_rate.tripped=true`) with severity recorded.
    Once a session trips, the trip is sticky — every subsequent
    span in that session also carries the flag — so we de-dupe by
    session_id and keep the highest recorded severity.
    """
    by_session: dict[str, dict] = {}
    for span in spans:
        attrs = span.get("attributes") or {}
        if not attrs.get("cascade.runaway_rate.tripped"):
            continue
        sid = attrs.get("session_id")
        if not isinstance(sid, str):
            continue
        sev = attrs.get("cascade.runaway_rate.severity") or "warning"
        total = attrs.get("cascade.runaway_rate.total_usd") or 0.0
        entry = by_session.get(sid)
        # Escalate severity if we see a higher one. "warning" → "critical"
        # is the only escalation direction in spec § 2.6.
        if entry is None:
            by_session[sid] = dict(
                bot_id=span.get("bot_id") or "<unknown>",
                severity=sev,
                total_usd=total,
            )
        else:
            if sev == "critical":
                entry["severity"] = "critical"
            entry["total_usd"] = max(entry["total_usd"], total)

    out: list[dict] = []
    for sid, info in by_session.items():
        bot_id = info["bot_id"]
        # Plugin's severity strings ("warning" | "critical") map to
        # signal-store severities ("warn" | "alert").
        sig_sev = "alert" if info["severity"] == "critical" else "warn"
        title = f"{bot_id}: runaway-rate cap tripped (${info['total_usd']:.2f} in window)"
        body = (
            f"Session `{sid[:8]}` burned ${info['total_usd']:.2f} faster "
            f"than the configured per-session safety net allows.\n\n"
            f"Bot: `{bot_id}`\n"
            f"Recorded severity: `{info['severity']}`\n\n"
            "Once tripped, ModelRouter forces tier3 for the rest of the "
            "session regardless of consent — that catches the runaway "
            "but doesn't recover spend already incurred. Review the "
            "session to determine root cause: a thrashing loop in OC, a "
            "stuck reflex, or a legitimate high-cost burst that needs "
            "the cap raised."
        )
        out.append(dict(
            signature=make_signature(PRODUCER, "runaway_rate_tripped", sid),
            producer=PRODUCER,
            type="runaway_rate_tripped",
            flavor="maintenance",
            severity=sig_sev,
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                session_id=sid,
                bot_id=bot_id,
                total_usd=info["total_usd"],
                plugin_severity=info["severity"],
                vector="cost",
                magnitude=4 if sig_sev == "alert" else 3,
                severity_active=True,
                what_it_means=(
                    "ModelRouter's per-session runaway-rate cap kicked "
                    "in. This is the safety net designed to catch broken "
                    "behavior (loops, stuck reflexes) — not intentional "
                    "expensive work. A trip means the session burned "
                    "money faster than any legitimate use case should."
                ),
                fix_steps=(
                    "1. Find the session in Reports → Sessions by id\n"
                    "2. Check the OC log for the bot near the trip time\n"
                    "3. If a loop/reflex bug: file a fix and dismiss\n"
                    "4. If a one-off heavy session: dismiss this Signal\n"
                    "5. If the cap is too tight for this bot: raise "
                    "runawayRateCap.dollarsPerWindow in tiers.json"
                ),
            ),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detector: unknown trigger_kind rate spike (per-bot)
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY THIS DETECTOR EXISTS (audit follow-up #68):
#   The plugin's pre-classification anchor (PR #1737 / #1764) routes
#   heartbeat / cron / subagent / etc. to the correct tier based on
#   ``trigger_kind`` from the OC hook payload. When trigger_kind is
#   ``unknown`` (either missing in ctx, or a value we don't recognize),
#   no anchor fires — the session falls through to the content
#   classifier, which for user-turn-shaped content defaults to
#   ``productive`` → bot default = tier 2.
#
#   That's the right behavior when the trigger is GENUINELY ambiguous
#   (e.g. a user message in a new OC channel we haven't taught
#   inferTriggerKind about). It's the WRONG behavior when a new OC
#   release stops populating ``trigger_kind`` for sessions that USED
#   to be heartbeats — those sessions silently start landing on tier 2
#   instead of tier 3, with no operator visibility until the cost
#   chart shifts.
#
#   This detector watches for sudden spikes in the per-bot unknown
#   rate over 24h. Burst-detection rather than absolute threshold so
#   pods that always have some ambient unknown rate (e.g. due to
#   browser-extension sessions, future channels not yet mapped) don't
#   alert constantly.

# Minimum sample size before we'll alert. Below this the rate is too
# noisy to draw conclusions from.
_UNKNOWN_TRIGGER_MIN_TURNS = 20

# Spike threshold: alert when the unknown rate over the recent window
# exceeds 25% of total turns. Empirical — most bots run with <5%
# ambient unknown (older OC versions before #1737's trigger anchor
# work landed); a jump to 25%+ is a clear signal that something in
# the OC client started withholding trigger metadata.
_UNKNOWN_TRIGGER_ALERT_THRESHOLD = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# Detector: tier-routing disagreement (per-bot, bot-scoped Signal)
# ─────────────────────────────────────────────────────────────────────────────
#
# Catches the regression class that's bitten the pod repeatedly: the
# plugin's routing diverges from what the operator's config says it
# should be doing. Examples already in the audit log:
#
#   • PR #1742 — silent fallback model promotion: human turns landed
#     on tier3 instead of tier2 because the primary derivation was
#     wrong in the deploy chain.
#   • PR #1737 / #1764 — trigger anchor overwrite: heartbeat sessions
#     landed on tier2 instead of tier3 because the content classifier
#     re-classified to "productive" after the anchor had pinned them.
#   • PR #1765 / #1774 — role-aware primary flip: member bots silently
#     started replying with tier3 on human chat.
#
# Every one of those was a SILENT regression — only visible in the cost
# chart days later. This detector validates per-bot observed routing
# against the bot's current config and fires when they disagree.
#
# Checks performed (per-bot, per-(driver, trigger) bucket):
#
#   1. ``classifier`` + background trigger_kind → tier3
#      (routing.backgroundTier, default tier3). Catches PR #1737-class
#      regressions where the trigger anchor stops pinning.
#
#   2. ``classifier`` + maintenance trigger_kind → tier3
#      (routing.maintenanceTier, default tier3). Same shape as #1.
#
#   3. ``operator_default`` driver → tier matching
#      userTierOverride.defaultTier (Phase A, PR #1786). Catches drift
#      between what the operator set in the UI dropdown and what the
#      plugin actually applies.
#
#   4. ``spend_cap`` / ``runaway`` drivers → tier3 (or the refuse
#      sentinel from PR #1777). Catches L5 audit-class miswiring where
#      the safety nets fire but routing escapes the breaker.
#
# NOT validated (depends on per-session state we can't reconstruct
# passively):
#
#   • ``user_request`` (chip / `evo tier X`) — value varies per user.
#   • ``user_default`` (Phase C, `evo tier-default X`) — varies per
#     user; would need per-session user_key correlation to validate.
#   • ``cascade`` (Phase 3 controller) — value varies per session.
#   • ``classifier`` + user_turn / unknown — no override applied;
#     falls through to bot default. No expectation.
#
# Threshold semantics
# ===================
# Per-bucket, we alert when:
#   • bucket has ≥ _TIER_AUDIT_MIN_TURNS observations (noisy below)
#   • observed_wrong / total ≥ _TIER_AUDIT_DIVERGENCE_THRESHOLD
#
# Wrong-fraction over absolute count keeps small flares (e.g. one bot
# user manually escalating tier mid-session) from showing up as a
# Signal — has to be a SUSTAINED disagreement to trip.
#
# Sweep-resolve: yes. Once the underlying bug is fixed, the next run
# stops emitting this signature and the Signal auto-archives.

_TIER_AUDIT_MIN_TURNS = 20

# Alert when more than this fraction of a (driver, trigger) bucket
# routes to a tier that disagrees with the bot's config. 25% is the
# same threshold used by _collect_unknown_trigger_rate_signals — a
# notable but not catastrophic divergence, the band where "something
# is regressing" beats out "ambient noise."
_TIER_AUDIT_DIVERGENCE_THRESHOLD = 0.25

# Trigger kinds that the plugin's pre-classification anchor pins to
# tier3 via routing.backgroundTier. Mirrors triggerKindToSessionClass
# in TurnObserver.ts — kept in sync manually because the detector
# runs in Python while the anchor lives in TypeScript. Drift here
# would itself be caught by the unknown_trigger_rate detector.
_BACKGROUND_TRIGGER_KINDS = frozenset({"heartbeat", "cron_app"})
_MAINTENANCE_TRIGGER_KINDS = frozenset({
    "subagent", "summarizer", "classifier",
    "task_extractor", "fallback",
})

# Choice → tier mapping. Mirrors ModelRouter._resolveTierFromChoice.
_TIER_AUDIT_CHOICE_TO_TIER = {
    "fast": "tier3",
    "standard": "tier2",
    "power": "tier1",
}

# Refuse sentinel from PR #1777. When the safety net fires AND tier3
# is unconfigured, the plugin returns this string so OC fails to
# resolve and the turn refuses — the breaker is correctly stopping
# cost. We count tier_used=null with this model as a valid safety-net
# outcome, not a divergence.
_TIER_AUDIT_REFUSE_SENTINEL = "evolve/safety-net-blocked-tier3-unconfigured"


def _load_bot_tier_config(bot_id: str) -> dict:
    """Read the bot's evolve-tiers.json.

    The evolve user has macOS ACL read on every bot's .openclaw dir
    via set_evolve_read_acl (deploy.py). Returns ``{}`` on any read
    error so the detector skips bots we can't reach instead of
    crashing the audit pass.
    """
    path = Path(f"/Users/{bot_id}/.openclaw/evolve-tiers.json")
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _expected_tier_for(
    driver: str,
    trigger_kind: str | None,
    config: dict,
) -> str | None:
    """Map ``(driver, trigger_kind)`` to the tier we expect ``tier_used``
    to be at, per the bot's config. Returns ``None`` when we have no
    static expectation (per-session state required).

    Pure function; testable in isolation.
    """
    if driver == "classifier":
        routing = config.get("routing") or {}
        if trigger_kind in _BACKGROUND_TRIGGER_KINDS:
            tier = routing.get("backgroundTier", "tier3")
            return tier if isinstance(tier, str) else "tier3"
        if trigger_kind in _MAINTENANCE_TRIGGER_KINDS:
            tier = routing.get("maintenanceTier", "tier3")
            return tier if isinstance(tier, str) else "tier3"
        # user_turn / unknown / None — classifier doesn't drive an
        # override; falls through to bot default. No expectation.
        return None
    if driver == "operator_default":
        override = config.get("userTierOverride") or {}
        choice = (override.get("defaultTier") or "").strip().lower()
        return _TIER_AUDIT_CHOICE_TO_TIER.get(choice)
    if driver in ("spend_cap", "runaway"):
        return "tier3"  # safety nets always downgrade to tier3
    # user_request / user_default / cascade — per-session state we
    # can't recover. Skipped.
    return None


def _collect_tier_routing_disagreement_signals(
    spans: Iterable[dict],
    *,
    config_loader: "callable | None" = None,
) -> list[dict]:
    """Per-bot Signal when observed routing disagrees with current config.

    ``config_loader(bot_id) -> dict`` is injectable for tests; defaults
    to :func:`_load_bot_tier_config` which reads from disk.

    Produces ONE Signal per affected bot — body lists every diverging
    (driver, trigger) bucket. Operator triages by bot, not by bucket.
    """
    loader = config_loader or _load_bot_tier_config

    by_bot_attrs: dict[str, list[dict]] = defaultdict(list)
    by_bot_models: dict[str, dict[tuple, list[str | None]]] = defaultdict(
        lambda: defaultdict(list),
    )
    for span in spans:
        attrs = span.get("attributes") or {}
        bot_id = span.get("bot_id") or attrs.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id:
            continue
        by_bot_attrs[bot_id].append(attrs)

    out: list[dict] = []
    for bot_id, attr_list in by_bot_attrs.items():
        config = loader(bot_id) or {}

        # Bucket spans by (driver, trigger_kind). The "trigger_kind" key
        # is normalized to None for "", missing, or "unknown" — the
        # _expected_tier_for helper treats all three identically.
        buckets: dict[
            tuple[str, str | None],
            list[tuple[str | None, str | None]],  # (tier_used, model)
        ] = defaultdict(list)
        for attrs in attr_list:
            driver = attrs.get("cascade.tier_chosen_by")
            if not isinstance(driver, str):
                continue
            trigger_raw = attrs.get("cascade.trigger_kind")
            trigger: str | None
            if trigger_raw in (None, "", "unknown"):
                trigger = None
            elif isinstance(trigger_raw, str):
                trigger = trigger_raw
            else:
                trigger = None
            tier_used = attrs.get("cascade.tier_used")
            model = attrs.get("cascade.model") or attrs.get("model")
            buckets[(driver, trigger)].append((
                tier_used if isinstance(tier_used, str) else None,
                model if isinstance(model, str) else None,
            ))

        divergences: list[dict] = []
        for (driver, trigger), observations in buckets.items():
            expected = _expected_tier_for(driver, trigger, config)
            if expected is None:
                continue
            n_total = len(observations)
            if n_total < _TIER_AUDIT_MIN_TURNS:
                continue
            # Count "wrong" — observed tier_used differs from expected.
            # Special case for spend_cap / runaway: the refuse sentinel
            # IS the intended behavior when tier3 isn't configured (PR
            # #1777). A null tier_used with the sentinel model isn't a
            # divergence, it's the breaker working as designed.
            n_wrong = 0
            n_refuse = 0
            for tier_used, model in observations:
                if tier_used == expected:
                    continue
                if (
                    driver in ("spend_cap", "runaway")
                    and tier_used is None
                    and model == _TIER_AUDIT_REFUSE_SENTINEL
                ):
                    n_refuse += 1
                    continue
                n_wrong += 1
            rate = n_wrong / n_total
            if rate < _TIER_AUDIT_DIVERGENCE_THRESHOLD:
                continue
            # Tally observed tiers for the body — operator wants to
            # know "where DID it land?" not just "not where I told it."
            observed_dist: dict[str, int] = defaultdict(int)
            for tier_used, _ in observations:
                key = tier_used if tier_used else "<bot_default>"
                observed_dist[key] += 1
            divergences.append({
                "driver": driver,
                "trigger_kind": trigger,
                "expected_tier": expected,
                "observed": dict(observed_dist),
                "n_wrong": n_wrong,
                "n_refuse": n_refuse,
                "n_total": n_total,
                "rate": rate,
            })

        if not divergences:
            continue

        # Sort divergences by rate desc — worst offender first in body.
        divergences.sort(key=lambda d: d["rate"], reverse=True)
        n_buckets = len(divergences)
        worst = divergences[0]
        title = (
            f"{bot_id}: tier routing disagrees with config "
            f"({n_buckets} bucket{'s' if n_buckets != 1 else ''})"
        )
        # Body lists each diverging bucket with its expected vs observed.
        lines: list[str] = []
        for d in divergences:
            trig_label = d["trigger_kind"] or "user_turn/unknown"
            obs_summary = ", ".join(
                f"{tier}={count}" for tier, count in
                sorted(d["observed"].items(), key=lambda kv: -kv[1])
            )
            pct = round(d["rate"] * 100, 1)
            lines.append(
                f"  • `chosen_by={d['driver']}` + `trigger={trig_label}` → "
                f"expected `{d['expected_tier']}` but {pct}% landed elsewhere "
                f"({d['n_wrong']}/{d['n_total']}; observed: {obs_summary})"
            )
        body = (
            f"Observed routing diverges from this bot's current "
            f"evolve-tiers.json on {n_buckets} (driver, trigger) "
            f"bucket{'s' if n_buckets != 1 else ''}:\n\n"
            + "\n".join(lines)
            + "\n\nWhat this catches: the silent-routing-regression class "
            "(PRs #1737/#1742/#1765 were all this shape — the plugin "
            "routed differently than the deploy chain intended, and "
            "the only symptom was a cost-chart shift days later).\n\n"
            "What to check:\n"
            "  • Did a recent deploy change the bot's tier config? "
            "Inspect `~/.openclaw/evolve-tiers.json` and compare to "
            "the AI Optimization page.\n"
            "  • Did an OC upgrade change how the plugin reads the "
            "config? `openclaw --version` on the mini; check for "
            "schema drift.\n"
            "  • Open the affected sessions in Reports → Sessions to "
            "see the actual routing decision per turn."
        )
        out.append(dict(
            signature=make_signature(
                PRODUCER, "tier_routing_disagreement", bot_id,
            ),
            producer=PRODUCER,
            type="tier_routing_disagreement",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                bot_id=bot_id,
                divergences=divergences,
                worst_bucket={
                    "driver": worst["driver"],
                    "trigger_kind": worst["trigger_kind"],
                    "expected_tier": worst["expected_tier"],
                    "rate": worst["rate"],
                },
                threshold=_TIER_AUDIT_DIVERGENCE_THRESHOLD,
                min_turns=_TIER_AUDIT_MIN_TURNS,
                vector="routing",
                magnitude=3,
                severity_active=True,
                what_it_means=(
                    "The cascade telemetry layer shows the plugin "
                    "routed turns to tiers that disagree with what "
                    "the operator's evolve-tiers.json says they "
                    "should land on. Past examples (PR #1737, #1742, "
                    "#1765) of this regression were only caught by "
                    "the cost chart shifting days later."
                ),
                fix_steps=(
                    "1. Run `bash scripts/verify_tier_chain.sh "
                    f"{bot_id}` on the mini — does it report a static "
                    "config mismatch?\n"
                    "2. Compare the bot's evolve-tiers.json against "
                    "the AI Optimization page's view of the same "
                    "fields (UI is the canonical writer).\n"
                    "3. Check the gateway log for recent reload "
                    "events — has the plugin actually picked up the "
                    "current config? grep gateway.log for "
                    "`ModelRouter reload`.\n"
                    "4. If the deploy chain looks right and the "
                    "plugin reloaded, this is likely a new regression "
                    "in TurnObserver — open the audit log for the "
                    "session(s) listed in details.divergences."
                ),
            ),
        ))
    return out


def _collect_unknown_trigger_rate_signals(spans: Iterable[dict]) -> list[dict]:
    """Surface bots whose share of unknown-trigger turns has spiked.

    Watches ``cascade.trigger_kind`` across the recent-window spans
    and emits a per-bot Signal when:
      • ≥ _UNKNOWN_TRIGGER_MIN_TURNS total turns observed for the bot
      • Share of trigger_kind ∈ {"unknown", "", None} ≥
        _UNKNOWN_TRIGGER_ALERT_THRESHOLD (25%)

    Catches the silent-tier-2 risk from a future OC release that
    stops populating trigger metadata: without this detector, the
    only visible symptom would be a cost chart shift days later.

    Signal scope is per-bot (each affected bot gets its own); sweeps
    auto-resolve via signature dedup (same bot keeps re-emitting the
    same signature until the rate drops back below threshold).
    """
    by_bot_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"unknown": 0, "total": 0}
    )
    for span in spans:
        attrs = span.get("attributes") or {}
        bot_id = span.get("bot_id") or attrs.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id:
            continue
        # cascade.trigger_kind is populated by TurnObserver.resolveModelRouting
        # alongside the pre-classification anchor. Missing / "" / "unknown"
        # all count as "no anchor fired this turn."
        trigger = attrs.get("cascade.trigger_kind")
        counts = by_bot_counts[bot_id]
        counts["total"] += 1
        if trigger is None or trigger == "" or trigger == "unknown":
            counts["unknown"] += 1

    out: list[dict] = []
    for bot_id, counts in by_bot_counts.items():
        total = counts["total"]
        unknown = counts["unknown"]
        if total < _UNKNOWN_TRIGGER_MIN_TURNS:
            continue
        rate = unknown / total
        if rate < _UNKNOWN_TRIGGER_ALERT_THRESHOLD:
            continue
        # Round percentages for human-readable body / title.
        pct = round(rate * 100, 1)
        title = (
            f"{bot_id}: high rate of unknown trigger_kind ({pct}% of "
            f"{total} recent turns)"
        )
        body = (
            f"`{unknown}/{total}` turns in the recent window had no "
            f"recognized ``trigger_kind`` in the OC hook payload.\n\n"
            f"This matters because the plugin's pre-classification "
            f"anchor (PR #1737) uses ``trigger_kind`` to route "
            f"heartbeat / cron / subagent sessions to tier 3 BEFORE "
            f"the content classifier runs. When trigger_kind is "
            f"missing, those sessions fall through to the content "
            f"classifier, default to ``productive``, and land on "
            f"tier 2 (the bot's primary, usually Sonnet) instead of "
            f"tier 3 (the floor, usually Haiku).\n\n"
            f"What to check:\n"
            f"  • Did OC release a new version recently? Check "
            f"`openclaw --version` on the mini.\n"
            f"  • Does the `trigger_kind` field still appear in "
            f"`agent_end` ctx dumps? grep `gateway.log` for "
            f"`Evolve diag: agent_end ctx dump`.\n"
            f"  • If OC silently dropped the field, the cost chart "
            f"will shift toward tier 2 over the next few hours — "
            f"this Signal gets you there earlier."
        )
        out.append(dict(
            signature=make_signature(
                PRODUCER, "unknown_trigger_rate_spike", bot_id,
            ),
            producer=PRODUCER,
            type="unknown_trigger_rate_spike",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                bot_id=bot_id,
                unknown_count=unknown,
                total_count=total,
                unknown_rate=rate,
                threshold=_UNKNOWN_TRIGGER_ALERT_THRESHOLD,
                vector="routing",
                magnitude=3,
                severity_active=True,
                what_it_means=(
                    "The plugin's pre-classification anchor only fires "
                    "for known trigger_kinds. A spike in 'unknown' means "
                    "those sessions silently route to the bot default "
                    "tier instead of the configured background floor."
                ),
                fix_steps=(
                    "1. Compare OC version on the mini against the "
                    "version when the rate was healthy.\n"
                    "2. Check inferTriggerKind in "
                    "packages/plugin/src/observer/TurnObserver.ts — does "
                    "it cover the new shape OC is emitting?\n"
                    "3. If yes (covered), check that the "
                    "agent_end ctx is being populated by OC at the "
                    "expected key (source / channel / trigger).\n"
                    "4. Most operator-visible fix: extend "
                    "_triggerKindToSessionClass to recognize whatever "
                    "the new OC version is sending."
                ),
            ),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detector: anomaly (per-bot, per-metric, bot-scoped Signal)
# ─────────────────────────────────────────────────────────────────────────────


def _spans_in_window(spans: Iterable[dict], minutes: int, now: datetime) -> list[dict]:
    """Filter spans to those whose end_time falls in the recent window."""
    cutoff = now - timedelta(minutes=minutes)
    out: list[dict] = []
    for sp in spans:
        ts = sp.get("end_time") or sp.get("start_time")
        if not isinstance(ts, str):
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt >= cutoff:
            out.append(sp)
    return out


def _compute_bot_baselines(
    spans: Iterable[dict],
    *,
    window_days: int,
    earliest_span_at: datetime | None,
) -> dict[str, BotBaseline]:
    """Per-bot baselines from the full window, with pod-median fallback.

    Two-stage auto-bootstrap (matches the auto-bootstrap directive):

      Pass 1 — compute each bot's baseline assuming no fallback.
      Pass 2 — compute pod-median across the populated baselines,
               then re-compute each bot's baseline with the pod-median
               as the fallback. Bots with insufficient data inherit
               the pod-median.
    """
    # Group spans by bot_id once.
    by_bot: dict[str, list[dict]] = defaultdict(list)
    for sp in spans:
        bid = sp.get("bot_id")
        if isinstance(bid, str):
            by_bot[bid].append(sp)

    # Holdout-preferred subsets (per spec § 2.3 Component 5). Pass
    # holdout spans to compute_baseline_from_spans when available;
    # the function counts observations and falls back internally.
    pass1: list[BotBaseline] = []
    for bid, bspans in by_bot.items():
        baseline = compute_baseline_from_spans(
            iter(bspans),
            bot_id=bid,
            window_days=window_days,
            earliest_span_at=earliest_span_at,
        )
        pass1.append(baseline)

    pod_median = compute_pod_median_baseline(pass1)

    out: dict[str, BotBaseline] = {}
    for bid, bspans in by_bot.items():
        baseline = compute_baseline_from_spans(
            iter(bspans),
            bot_id=bid,
            window_days=window_days,
            earliest_span_at=earliest_span_at,
            pod_fallback=pod_median,
        )
        out[bid] = baseline
    return out


def _collect_anomaly_signals(
    recent_spans: Iterable[dict],
    baselines: dict[str, BotBaseline],
) -> list[dict]:
    """Compare recent observations against per-bot baselines, emit Signals.

    Two metrics are checked per bot:

      1. cost_per_turn       — recent mean vs baseline mean
      2. context_tokens_per_turn — recent mean vs baseline mean

    Anomaly origin (and therefore threshold) is computed per-span,
    then the SINGLE WORST origin's threshold is applied to the bot's
    aggregate. This keeps the Signal-per-bot count low (one per bot
    per metric per origin at most) while letting the most-permissive
    origin (e.g. ui_chip with no inform threshold) dominate when an
    operator was driving the cost.
    """
    # Group recent spans by (bot_id, origin) → list of (cost, ctx_tokens).
    by_bucket: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"cost": [], "ctx": []}
    )
    for sp in recent_spans:
        bid = sp.get("bot_id")
        if not isinstance(bid, str):
            continue
        attrs = sp.get("attributes") or {}
        origin = classify_origin(
            attrs.get("cascade.trigger_kind"),
            attrs.get("cascade.tier_used"),
            attrs.get("cascade.tier_chosen_by"),
            attrs.get("cascade.consent_source"),
        )
        bucket = by_bucket[(bid, origin)]
        cost = sp.get("total_cost") or 0.0
        if isinstance(cost, (int, float)) and cost > 0:
            bucket["cost"].append(float(cost))
        usage = sp.get("usage") or {}
        # `input_tokens` is what the plugin writes (CascadeTelemetry.ts);
        # `prompt_tokens` was a guess that produced silent zeros — the
        # context_tokens_per_turn anomaly Signal never fired because of it.
        ctx = usage.get("input_tokens") or 0
        if isinstance(ctx, (int, float)) and ctx > 0:
            bucket["ctx"].append(float(ctx))

    out: list[dict] = []
    for (bid, origin), bucket in by_bucket.items():
        baseline = baselines.get(bid)
        if baseline is None or baseline.source == "insufficient_data":
            # Anomaly detection effectively disabled until baseline
            # accumulates (or pod median bootstraps in). Skip.
            continue

        for metric_key, baseline_stat, label in (
            ("cost", baseline.cost_per_turn, "cost_per_turn"),
            ("ctx", baseline.context_tokens_per_turn, "context_tokens_per_turn"),
        ):
            values = bucket[metric_key]
            if not values:
                continue
            # Compare aggregate (mean) of recent values to baseline mean.
            recent_mean = sum(values) / len(values)
            verdict = classify_anomaly(
                observed_value=recent_mean,
                baseline_value=baseline_stat.mean,
                origin=origin,  # type: ignore[arg-type]
            )
            if not verdict.is_anomalous:
                continue
            # Map "inform" → "info" + magnitude 2, "warn" → "warn" + magnitude 3.
            sig_sev: Any = "info" if verdict.severity == "inform" else "warn"
            magnitude = 2 if verdict.severity == "inform" else 3
            # scope_key uniquely identifies the affected resource within
            # the producer's domain. The metric name is already in the
            # `type` field; don't double-encode it here.
            scope_key = f"{bid}:{origin}"
            title = (
                f"{bid}: {label} {verdict.ratio:.1f}x baseline "
                f"(origin={origin}, severity={verdict.severity})"
            )
            body = (
                f"Recent mean {label.replace('_', ' ')}: "
                f"{recent_mean:.4f} (baseline mean: "
                f"{verdict.baseline_value:.4f}; threshold: "
                f"{verdict.threshold_used:.1f}x).\n\n"
                f"Origin: `{origin}` — "
                f"thresholds are origin-aware per spec § 2.6.\n"
                f"Baseline source: `{baseline.source}` "
                f"(n={baseline_stat.n} observations over "
                f"{baseline.window_days}d).\n\n"
                "Anomalies are auto-resolved on the next run when the "
                "condition clears."
            )
            out.append(dict(
                signature=make_signature(PRODUCER, f"anomaly_{label}", scope_key),
                producer=PRODUCER,
                type=f"anomaly_{label}",
                flavor="maintenance",
                severity=sig_sev,
                scope="bot",
                bot_id=bid,
                title=title,
                body=body,
                details=dict(
                    bot_id=bid,
                    metric=label,
                    origin=origin,
                    ratio=verdict.ratio,
                    observed_value=verdict.observed_value,
                    baseline_value=verdict.baseline_value,
                    threshold_used=verdict.threshold_used,
                    detector_severity=verdict.severity,
                    baseline_source=baseline.source,
                    baseline_n=baseline_stat.n,
                    vector="cost" if label == "cost_per_turn" else "operations",
                    magnitude=magnitude,
                    severity_active=True,
                    what_it_means=(
                        f"The recent {label.replace('_', ' ')} for this bot "
                        f"is {verdict.ratio:.1f}x its baseline. Origin "
                        f"`{origin}` allows up to "
                        f"{verdict.threshold_used:.1f}x before "
                        "anomaly fires. Above that suggests either a "
                        "new high-cost pattern (legitimate but worth "
                        "noting) or a cascade misbehavior (Phase 4 "
                        "calibration input)."
                    ),
                    fix_steps=(
                        "1. Open Reports → Bots → this bot's recent sessions\n"
                        "2. Identify which sessions are driving the cost\n"
                        "3. If a new legitimate pattern: dismiss the Signal\n"
                        "4. If unexpected: file Phase 4 calibration input"
                    ),
                ),
            ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detector: plugin telemetry silent failure (pod-scoped, per-bot signal)
# ─────────────────────────────────────────────────────────────────────────────


# How fresh recent-transcripts.json must be (in seconds) for us to claim
# "this bot is active right now." 1 hour matches the audit_runner's own
# cadence — we never want to alarm on a bot that's been quiet all day.
_PLUGIN_ACTIVITY_WINDOW_SEC = 3600

# Per-bot heartbeat artifact written by the plugin's RecentTranscriptCapture
# on every user turn. It lands at a bot-writable + evolve-readable path,
# so we can stat it from the audit_runner (which runs as evolve) to detect
# whether the bot's plugin is alive. Distinct from cascade spans — the
# whole point of this detector is to catch "plugin alive on the writes
# it can do, dead on the writes it can't (EACCES on per-bot subdirs)."
_PLUGIN_HEARTBEAT_PATH = "metrics/{bot_id}/recent-transcripts.json"


def _collect_plugin_telemetry_failure_signals(
    shared_dir: Path,
    recent_spans: list[dict],
    *,
    now: datetime,
) -> list[dict]:
    """Detect per-bot silent failure of cascade telemetry writes.

    The plugin writes cascade spans to ``{shared}/{bot}/spans/spans-<day>.jsonl``.
    Some failure modes (EACCES on a missing pre-created dir; disk full;
    permission regression after an OC upgrade) leave the plugin's
    other writes intact but break the cascade path specifically. The
    plugin warns once-per-process to its gateway log — invisible to
    operators who don't tail bot logs.

    This detector closes that gap. For each per-bot metrics dir that
    exists with a fresh ``recent-transcripts.json`` (proof the plugin is
    alive and writing SOMETHING), if zero cascade spans landed for that
    bot today, fire a Signal.

    Spec: this addresses the systemic concern raised in PR #1689 ("any
    future class of plugin-side write failure will hide just as silently
    in /Users/<bot>/.openclaw/logs/gateway.log").
    """
    metrics_root = shared_dir / "metrics"
    if not metrics_root.is_dir():
        return []

    # Index recent spans by bot_id so we can answer "did bot X write
    # any cascade span today?" in O(1) per bot.
    today_utc = now.date()
    spans_by_bot_today: dict[str, int] = {}
    for sp in recent_spans:
        bid = sp.get("bot_id")
        if not isinstance(bid, str):
            continue
        end = sp.get("end_time") or sp.get("start_time")
        if not isinstance(end, str):
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if end_dt.date() == today_utc:
            spans_by_bot_today[bid] = spans_by_bot_today.get(bid, 0) + 1

    out: list[dict] = []
    activity_cutoff_ts = now.timestamp() - _PLUGIN_ACTIVITY_WINDOW_SEC
    for bot_dir in metrics_root.iterdir():
        if not bot_dir.is_dir():
            continue
        bot_id = bot_dir.name
        heartbeat = bot_dir / "recent-transcripts.json"
        if not heartbeat.is_file():
            continue
        try:
            mtime = heartbeat.stat().st_mtime
        except OSError:
            continue
        if mtime < activity_cutoff_ts:
            # Bot inactive this past hour — quiet bot, not broken plugin.
            continue
        # Plugin IS writing recent-transcripts.json. Did it write any
        # cascade spans today?
        if spans_by_bot_today.get(bot_id, 0) > 0:
            continue
        # Active + zero spans = silent failure of the cascade write path.
        heartbeat_age_min = int((now.timestamp() - mtime) / 60)
        title = f"{bot_id}: plugin cascade telemetry silently failing"
        body = (
            f"Bot `{bot_id}` is alive — its plugin updated "
            f"`recent-transcripts.json` {heartbeat_age_min}min ago — but "
            f"zero cascade telemetry spans landed today.\n\n"
            f"This is the failure signature of a write-access problem on "
            f"`/Users/Shared/evolve/{bot_id}/spans/` (typically EACCES "
            f"because the dir wasn't pre-created with bot-user ownership). "
            f"The plugin emits a warn-once-per-process to "
            f"`/Users/{bot_id}/.openclaw/logs/gateway.log` — search for "
            f"`CascadeTelemetry: cannot create spans dir` to confirm.\n\n"
            f"Fix: `sudo evolve-admin deploy {bot_id}` — that re-fires "
            f"`fix_shared_dir_permissions` which pre-creates the missing "
            f"`spans/` + `cascade/` subdirs. No gateway restart needed."
        )
        out.append(dict(
            signature=make_signature(PRODUCER, "plugin_telemetry_failure", bot_id),
            producer=PRODUCER,
            type="plugin_telemetry_failure",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                bot_id=bot_id,
                heartbeat_age_min=heartbeat_age_min,
                spans_today=spans_by_bot_today.get(bot_id, 0),
                vector="operations",
                magnitude=3,
                severity_active=True,
                what_it_means=(
                    "The plugin is alive (writing transcripts) but can't "
                    "write cascade spans. Most commonly: EACCES on a "
                    "shared subdir that wasn't pre-created. Until fixed, "
                    "cascade telemetry data for this bot is missing — "
                    "no shadow verdicts, no labels, no anomaly detection."
                ),
                fix_steps=(
                    f"1. `sudo evolve-admin deploy {bot_id}` on the mini\n"
                    f"2. Wait for the next bot turn (~minutes)\n"
                    f"3. Verify `/Users/Shared/evolve/{bot_id}/spans/spans-"
                    f"{today_utc.isoformat()}.jsonl` now exists + is "
                    f"growing\n"
                    f"4. Tail the bot's gateway log for "
                    f"`CascadeTelemetry: cannot create spans dir` — should "
                    f"not appear after the deploy completes"
                ),
            ),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detector: payload-drift rate (per-bot signal — plugin can't measure struggle)
# ─────────────────────────────────────────────────────────────────────────────


# Threshold for firing the struggle-detector-blind Signal. If MORE than
# this fraction of a bot's recent turns recorded a payload_drift,
# StruggleDetector is going blind on a class of turns and the cascade
# controller is making decisions on stale/missing struggle data.
_PAYLOAD_DRIFT_RATE_THRESHOLD = 0.10  # 10% over the recent window

# Minimum sample size — don't yell about 100% drift over 2 turns.
_PAYLOAD_DRIFT_MIN_SPANS = 20


def _collect_payload_drift_signals(
    recent_spans: list[dict],
) -> list[dict]:
    """Detect per-bot elevation of `cascade.struggle.payload_drift`.

    Per spec § 2.5 (payload-drift fail-safe): when StruggleDetector
    can't measure a turn (no_messages / messages_not_array /
    empty_on_failure), it records a payload_drift reason on the span.
    Until this PR the field was written by the plugin and consumed
    by nobody — the documented audit-layer Signal didn't exist.

    Fires `cascade_audit:struggle_detector_blind:<bot_id>` (warn) when
    >10% of a bot's recent turns drifted, AND the sample is at least
    20 turns. The detector going blind means cascade is making
    decisions on stale/missing struggle data — operator-visible
    cost-correctness gap.

    Auto-resolves on next run when the rate falls below threshold.
    """
    by_bot_total: dict[str, int] = {}
    by_bot_drifted: dict[str, int] = {}
    by_bot_reasons: dict[str, dict[str, int]] = {}
    for sp in recent_spans:
        bot_id = sp.get("bot_id")
        if not isinstance(bot_id, str):
            continue
        attrs = sp.get("attributes") or {}
        # Only count spans where struggle COULD have been measured — a
        # span without any struggle data isn't drift, it's no signal.
        if "cascade.struggle.score" not in attrs:
            continue
        by_bot_total[bot_id] = by_bot_total.get(bot_id, 0) + 1
        drift = attrs.get("cascade.struggle.payload_drift")
        if isinstance(drift, str) and drift:
            by_bot_drifted[bot_id] = by_bot_drifted.get(bot_id, 0) + 1
            reasons = by_bot_reasons.setdefault(bot_id, {})
            reasons[drift] = reasons.get(drift, 0) + 1

    out: list[dict] = []
    for bot_id, total in by_bot_total.items():
        if total < _PAYLOAD_DRIFT_MIN_SPANS:
            continue
        drifted = by_bot_drifted.get(bot_id, 0)
        rate = drifted / total
        if rate <= _PAYLOAD_DRIFT_RATE_THRESHOLD:
            continue
        reasons = by_bot_reasons.get(bot_id, {})
        # Sort reasons by count desc for readable body text.
        reason_summary = ", ".join(
            f"{r}={c}" for r, c in sorted(
                reasons.items(), key=lambda kv: -kv[1],
            )
        )
        title = (
            f"{bot_id}: struggle detector blind on "
            f"{rate * 100:.0f}% of turns ({drifted}/{total})"
        )
        body = (
            f"Bot `{bot_id}`: StruggleDetector recorded "
            f"`payload_drift` on {drifted} of the last {total} measured "
            f"turns ({rate * 100:.0f}%).\n\n"
            f"Drift reasons: {reason_summary}\n\n"
            "When struggle can't be measured for a turn, the cascade "
            "controller falls back to defaults rather than escalating. "
            "Operator-visible impact: under-escalation under struggle — "
            "the bot keeps using the same tier even when the user is "
            "clearly stuck.\n\n"
            "Most common causes:\n"
            "- `no_messages`: agent_end fired without a messages array "
            "  (OC version change?)\n"
            "- `messages_not_array`: schema drift (OC sent an object "
            "  where an array was expected)\n"
            "- `empty_on_failure`: turn errored before any LLM call\n\n"
            "Fix path: check the bot's gateway log around the affected "
            "turns; if a recent OC upgrade landed, RUNTIME_NOTES.md "
            "may carry a known-incompatible field rename."
        )
        out.append(dict(
            signature=make_signature(
                PRODUCER, "struggle_detector_blind", bot_id,
            ),
            producer=PRODUCER,
            type="struggle_detector_blind",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details=dict(
                bot_id=bot_id,
                drifted_count=drifted,
                total_measured=total,
                rate_pct=round(rate * 100, 1),
                reasons=reasons,
                vector="operations",
                magnitude=3,
                severity_active=True,
                what_it_means=(
                    "StruggleDetector silently returned null on too many "
                    "turns. CascadeController's tier decisions are made "
                    "on incomplete data — escalation paths gated on "
                    "measured struggle don't fire when struggle is null. "
                    "Spec § 2.5 calls this the 'payload-drift fail-safe' "
                    "case — we prefer null over a bad guess, but >10% "
                    "drift means cascade is mostly flying blind."
                ),
                fix_steps=(
                    "1. Check `/Users/<bot>/.openclaw/logs/gateway.log` "
                    "for `StruggleDetector` diagnostic lines around the "
                    "affected turns\n"
                    "2. Check RUNTIME_NOTES.md for recent OC version "
                    "incompatibilities — schema changes to agent_end's "
                    "messages array are the usual cause\n"
                    "3. If a known incompatibility, file a plugin-side "
                    "fix to accept the new shape; the audit_runner will "
                    "auto-resolve this Signal once drift falls under 10%"
                ),
            ),
        ))
    return out


def _collect_preflight_disagreement_signals(
    spans: list[dict],
) -> list[dict]:
    """Per-bot Signals when the pre-flight intent router misroutes.

    See ``cascade.preflight_audit`` for the categorization +
    threshold logic. This function is the audit_runner shim: convert
    the stats dict into Signal payloads.

    Three Signal types, all per-bot:

      preflight_over_escalation
          tier1 picked but turn was trivially handled (< $0.05,
          ≤200 output tokens, ≤1 tool call, success=true).
          → operator should tighten the over-escalating regex/haiku
          rule. Body lists the top-firing reasons so the operator
          knows which rule to look at.

      preflight_under_escalation
          tier3 picked but turn struggled (struggle.score >= 0.4 OR
          success=false OR cost > $0.20).
          → operator should loosen the over-quiet patterns, or accept
          that the legacy classifier was right to handle this prompt.

      preflight_cascade_corrected
          pre-flight picked tier2/3 but cascade later escalated the
          NEXT turn in the same session to tier1.
          → pre-flight under-shot; cascade is doing its job but
          ideally pre-flight would have caught it on turn 1.

    All three auto-resolve when the rate falls below threshold and
    sample is still ≥ MIN_DECISIONS. Low-volume bots (< MIN_DECISIONS
    in the window) never fire any of these — sample too small to draw
    conclusions.
    """
    from cascade.preflight_audit import (  # noqa: E402
        compute_preflight_stats,
        PREFLIGHT_MIN_DECISIONS,
        PREFLIGHT_OVER_ESCALATION_THRESHOLD,
        PREFLIGHT_UNDER_ESCALATION_THRESHOLD,
        PREFLIGHT_CASCADE_CORRECTED_THRESHOLD,
        PREFLIGHT_SPARSE_MIN_DECISIONS,
        PREFLIGHT_SPARSE_MIN_COUNT,
        PREFLIGHT_SPARSE_OVER_ESCALATION_THRESHOLD,
        PREFLIGHT_SPARSE_UNDER_ESCALATION_THRESHOLD,
        PREFLIGHT_SPARSE_CASCADE_CORRECTED_THRESHOLD,
    )

    stats = compute_preflight_stats(spans)
    out: list[dict] = []

    def _decide_emit(
        decisions: int,
        rate: float,
        count: int,
        high_threshold: float,
        sparse_threshold: float,
    ) -> tuple[bool, str, float, str]:
        """Decide whether to fire a Signal and at what confidence.

        Returns ``(fire, severity, applied_threshold, sample_note)``:

          - ``fire``    — True if any tier of the threshold model says emit
          - ``severity`` — "warn" for the high-confidence path (≥30
            decisions, normal threshold), "info" for the sparse-bot path
            (≥5 decisions, higher threshold + absolute-count floor)
          - ``applied_threshold`` — the threshold value that the rate
            cleared; goes into details so the operator can see why
          - ``sample_note`` — short suffix for the body copy noting
            confidence when sparse, empty when normal

        Tier model:
          decisions ≥ 30, rate ≥ high_threshold        → warn
          5 ≤ decisions < 30, count ≥ 3,
                              rate ≥ sparse_threshold → info
          else                                        → don't fire

        Rationale: low-volume bots (household / personal-assistant
        deployments) would never accumulate 30 graded decisions in
        the audit window — the original ≥30 floor blinded the audit
        layer to their misrouting entirely. The sparse path catches
        the same patterns at lower confidence so the operator gets
        a heads-up; the higher rate + absolute-count floor keeps
        single-shot noise from triggering false alarms.
        """
        if decisions >= PREFLIGHT_MIN_DECISIONS:
            if rate >= high_threshold:
                return (True, "warn", high_threshold, "")
            return (False, "", 0.0, "")
        if decisions >= PREFLIGHT_SPARSE_MIN_DECISIONS:
            if count >= PREFLIGHT_SPARSE_MIN_COUNT and rate >= sparse_threshold:
                return (
                    True, "info", sparse_threshold,
                    " (small sample — pattern worth checking)",
                )
        return (False, "", 0.0, "")

    for bot_id, bot_stats in stats["by_bot"].items():
        decisions = bot_stats["decisions"]
        if decisions < PREFLIGHT_SPARSE_MIN_DECISIONS:
            # Truly too small — < 5 decisions can't draw any conclusions
            # even at the sparse confidence level. The 1-of-2 case is
            # genuine noise. Bots with 5-29 decisions take the sparse
            # path below; bots with ≥30 take the normal path.
            continue

        rates = bot_stats["rates"]
        by_reason = bot_stats.get("by_reason", {})

        # Build a per-reason summary for the body — "which rule is the
        # top offender." Sorted by count desc, top 5.
        def _top_reasons_for_category(category: str) -> list[dict]:
            reasons = []
            for reason, counts in by_reason.items():
                n = counts.get(category, 0)
                if n > 0:
                    reasons.append({"reason": reason, "count": n,
                                    "total_for_reason": counts.get("count", 0)})
            reasons.sort(key=lambda r: -r["count"])
            return reasons[:5]

        # over_escalation: too many tier1 calls on trivial turns
        rate = rates["over_escalation_rate"]
        n = bot_stats["categories"].get("over_escalation", 0)
        fire, severity, applied_threshold, sample_note = _decide_emit(
            decisions, rate, n,
            PREFLIGHT_OVER_ESCALATION_THRESHOLD,
            PREFLIGHT_SPARSE_OVER_ESCALATION_THRESHOLD,
        )
        if fire:
            sig = make_signature(PRODUCER, "preflight_over_escalation", bot_id)
            out.append(dict(
                signature=sig,
                producer=PRODUCER,
                type="preflight_over_escalation",
                severity=severity,
                title=f"{bot_id}: pre-flight over-escalated {n} turns to tier1",
                body=(
                    f"Over the recent audit window, {n} of {decisions} "
                    f"pre-flight-driven decisions on {bot_id} routed to "
                    f"tier1 for turns that were trivially handled "
                    f"(<$0.05, ≤200 output tokens, ≤1 tool call, "
                    f"success=true). That's an over-escalation rate of "
                    f"{rate:.1%}, above the {applied_threshold:.0%} "
                    f"alert threshold{sample_note}."
                ),
                details={
                    "bot_id": bot_id,
                    "decisions": decisions,
                    "over_escalation_count": n,
                    "over_escalation_rate": rate,
                    "threshold": applied_threshold,
                    "sparse_sample": severity == "info",
                    "top_reasons": _top_reasons_for_category("over_escalation"),
                },
                magnitude=1 + int(rate * 5),
                severity_active=True,
                what_it_means=(
                    "The pre-flight router is picking tier1 (Opus) for "
                    "prompts that don't need it. Each over-escalation "
                    "is ~3-5x the cost of the right tier without "
                    "delivering proportional quality. The top_reasons "
                    "field names the specific rules to look at — "
                    "regex:design_imperative firing on `design.com`, "
                    "haiku:tier1 over-triggering on short questions, etc."
                ),
                fix_steps=(
                    f"1. Inspect `cascade.preflight.reason` distribution "
                    f"for {bot_id} (admin UI Cost Optimization page once "
                    f"wired)\n"
                    f"2. Tighten the top-offending rule — narrow the "
                    f"regex, or for haiku: tune the prompt to be more "
                    f"conservative\n"
                    f"3. Per-bot opt-out: set "
                    f"`bots.{bot_id}.preflight.haiku_enabled = false` "
                    f"in network.json to disable haiku for this bot"
                ),
            ))

        # under_escalation: too many tier3 calls on struggling turns
        rate = rates["under_escalation_rate"]
        n = bot_stats["categories"].get("under_escalation", 0)
        fire, severity, applied_threshold, sample_note = _decide_emit(
            decisions, rate, n,
            PREFLIGHT_UNDER_ESCALATION_THRESHOLD,
            PREFLIGHT_SPARSE_UNDER_ESCALATION_THRESHOLD,
        )
        if fire:
            sig = make_signature(PRODUCER, "preflight_under_escalation", bot_id)
            out.append(dict(
                signature=sig,
                producer=PRODUCER,
                type="preflight_under_escalation",
                severity=severity,
                title=f"{bot_id}: pre-flight under-escalated {n} turns to tier3",
                body=(
                    f"Over the recent audit window, {n} of {decisions} "
                    f"pre-flight-driven decisions on {bot_id} routed to "
                    f"tier3 for turns that struggled (struggle.score≥0.4 "
                    f"OR success=false OR cost>$0.20). That's an "
                    f"under-escalation rate of {rate:.1%}, above the "
                    f"{applied_threshold:.0%} threshold{sample_note}."
                ),
                details={
                    "bot_id": bot_id,
                    "decisions": decisions,
                    "under_escalation_count": n,
                    "under_escalation_rate": rate,
                    "threshold": applied_threshold,
                    "sparse_sample": severity == "info",
                    "top_reasons": _top_reasons_for_category("under_escalation"),
                },
                magnitude=1 + int(rate * 5),
                severity_active=True,
                what_it_means=(
                    "The pre-flight router is picking tier3 (Haiku) for "
                    "prompts that needed a stronger model. Users on these "
                    "turns are getting degraded responses or paying "
                    "tier3 with poor outcomes. The top_reasons names the "
                    "rules to investigate — usually a tier3 regex like "
                    "factual_lookup firing on prompts that look short "
                    "but actually need depth."
                ),
                fix_steps=(
                    f"1. Inspect the top under-escalating reasons in "
                    f"the details. If a regex rule keeps firing on "
                    f"complex prompts that happen to look short, narrow "
                    f"the regex.\n"
                    f"2. If `haiku:tier3` is the culprit, tune the "
                    f"haiku prompt or raise the confidence floor.\n"
                    f"3. If under-escalation persists, consider "
                    f"disabling the tier3 layer entirely: "
                    f"`bots.{bot_id}.preflight.enabled = false`"
                ),
            ))

        # cascade_corrected: pre-flight under-shot; cascade picked up the slack
        rate = rates["cascade_corrected_rate"]
        n = bot_stats["categories"].get("cascade_corrected", 0)
        fire, severity, applied_threshold, sample_note = _decide_emit(
            decisions, rate, n,
            PREFLIGHT_CASCADE_CORRECTED_THRESHOLD,
            PREFLIGHT_SPARSE_CASCADE_CORRECTED_THRESHOLD,
        )
        if fire:
            sig = make_signature(PRODUCER, "preflight_cascade_corrected", bot_id)
            out.append(dict(
                signature=sig,
                producer=PRODUCER,
                type="preflight_cascade_corrected",
                severity=severity,
                title=f"{bot_id}: cascade corrected pre-flight on {n} turns",
                body=(
                    f"Over the recent audit window, {n} of {decisions} "
                    f"pre-flight-driven decisions on {bot_id} were "
                    f"followed by a cascade escalation to tier1 on the "
                    f"NEXT turn in the same session — i.e., the cascade "
                    f"controller observed struggle that pre-flight "
                    f"missed. That's a cascade-correction rate of "
                    f"{rate:.1%}, above the "
                    f"{applied_threshold:.0%} threshold{sample_note}."
                ),
                details={
                    "bot_id": bot_id,
                    "decisions": decisions,
                    "cascade_corrected_count": n,
                    "cascade_corrected_rate": rate,
                    "threshold": applied_threshold,
                    "sparse_sample": severity == "info",
                    "top_reasons": _top_reasons_for_category("cascade_corrected"),
                },
                magnitude=1 + int(rate * 5),
                severity_active=True,
                what_it_means=(
                    "Pre-flight is missing cases that cascade catches "
                    "on the next turn. Cascade is doing its job but "
                    "ideally pre-flight would have caught the signal "
                    "first. The top_reasons field shows which pre-flight "
                    "rules are most often followed by cascade overrides — "
                    "those are the rules to tune up (more aggressive "
                    "tier1 picks for the matching prompt shapes)."
                ),
                fix_steps=(
                    "1. Sample sessions tagged with these reasons; what "
                    "did the user ask that pre-flight missed?\n"
                    "2. If a clear pattern emerges, add a new tier1 "
                    "regex pattern OR tune the haiku prompt to catch it\n"
                    "3. RSI follow-up: this signal is precisely the "
                    "data Phase 4 RSI tuning consumes"
                ),
            ))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Top-level run
# ─────────────────────────────────────────────────────────────────────────────


def run(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """One pass: load spans, fire Signals, persist labels, sweep-resolve.

    Returns a small report dict for logging.
    """
    now = now or datetime.now(timezone.utc)
    # Load enough history for baseline computation. Recent-window
    # detections still need to see those same spans (a recent span IS
    # part of the baseline window).
    baseline_window_minutes = _BASELINE_WINDOW_DAYS * 24 * 60
    all_spans = _load_spans(
        shared_dir, window_minutes=baseline_window_minutes, now=now,
    )

    # Earliest span timestamp — feeds the auto-bootstrap clock.
    earliest_dt: datetime | None = None
    for sp in all_spans:
        ts = sp.get("start_time") or sp.get("end_time")
        if isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if earliest_dt is None or dt < earliest_dt:
                    earliest_dt = dt
            except (ValueError, TypeError):
                continue

    # Pass 1: compute baselines from the FULL window (including
    # recent). This is the spec's "holdout-preferred" path —
    # compute_baseline_from_spans handles holdout filtering internally
    # via the cascade.holdout attribute.
    baselines = _compute_bot_baselines(
        all_spans,
        window_days=_BASELINE_WINDOW_DAYS,
        earliest_span_at=earliest_dt,
    )

    # Pass 2: anomaly detection scoped to the recent window only.
    recent_spans = _spans_in_window(all_spans, _RECENT_WINDOW_MINUTES, now)

    # Collect Signals.
    detections: list[dict] = []
    detections.extend(_collect_dangerous_combo_signals(recent_spans))
    detections.extend(_collect_runaway_rate_signals(recent_spans))
    detections.extend(_collect_unknown_trigger_rate_signals(recent_spans))
    detections.extend(_collect_tier_routing_disagreement_signals(recent_spans))
    detections.extend(_collect_anomaly_signals(recent_spans, baselines))
    detections.extend(_collect_plugin_telemetry_failure_signals(
        shared_dir, recent_spans, now=now,
    ))
    detections.extend(_collect_payload_drift_signals(recent_spans))
    detections.extend(_collect_preflight_disagreement_signals(recent_spans))

    # Emit Signals.
    kept_signatures: set[str] = set()
    n_observe_failures = 0
    for d in detections:
        kept_signatures.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:  # noqa: BLE001
            n_observe_failures += 1
            print(
                f"[cascade_audit] observe failed for {d['signature']}: {exc}",
                flush=True,
            )

    # Sweep-resolve cleared anomalies — but ONLY for the anomaly_*
    # types. Dangerous-combo and runaway-rate are point-in-time events
    # that already happened; they don't auto-resolve when the next
    # run doesn't see them, the operator triages them.
    n_resolved = 0
    if not dry_run:
        try:
            # Build the set of types we want sweep_resolve to consider
            # for auto-clearing. Two families participate:
            #
            #   anomaly_*                 — per-bot, per-metric anomaly
            #                               verdicts. Listed exhaustively
            #                               (the two metric types) so a
            #                               Signal fired LAST run but not
            #                               this run gets resolved even
            #                               when we observed zero spans
            #                               this run.
            #   plugin_telemetry_failure  — fires when a bot's plugin
            #                               can't write cascade spans
            #                               despite being active. Once
            #                               the operator runs the deploy
            #                               fix, the next audit_runner
            #                               tick sees fresh spans and the
            #                               signature drops out of
            #                               kept_signatures, which
            #                               auto-resolves the Signal.
            #
            # Dangerous-combo and runaway-rate types are intentionally
            # excluded — those are point-in-time events; the operator
            # triages them by dismiss/resolve.
            sweepable_types = {
                d["type"] for d in detections if d["type"].startswith("anomaly_")
            }
            sweepable_types |= {
                "anomaly_cost_per_turn",
                "anomaly_context_tokens_per_turn",
                "plugin_telemetry_failure",
                "struggle_detector_blind",
                # Auto-resolve when the unknown trigger_kind rate drops
                # back below threshold. Audit #68 (2026-05-29): when an
                # OC release that withheld trigger metadata gets fixed
                # (or the operator extends inferTriggerKind to cover
                # the new shape), the next audit_runner tick should
                # clear the Signal without manual operator action.
                "unknown_trigger_rate_spike",
                # Auto-resolve when observed routing comes back into
                # agreement with config — e.g. operator changed the
                # AI Optimization picker, plugin reloaded, next pass
                # sees the matching tier distribution. The whole point
                # of this check is to be self-clearing once the
                # underlying bug is fixed.
                "tier_routing_disagreement",
                # Pre-flight intent router miscalibration Signals. All
                # three auto-resolve when the rate falls back under
                # threshold — operator tunes a regex / haiku prompt,
                # next audit pass sees the new rate and clears.
                "preflight_over_escalation",
                "preflight_under_escalation",
                "preflight_cascade_corrected",
            }
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept_signatures,
                types=sweepable_types,
                reason="auto-resolve: cascade condition cleared on next run",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[cascade_audit] sweep_resolve failed: {exc}", flush=True)

    # Persist labels for Phase 4 (no Signal emission — labels are
    # calibration input, not operator alerts).
    n_labels = 0
    if not dry_run:
        try:
            outcomes = list(label_spans(recent_spans))
            if outcomes:
                report = write_labels(outcomes, shared_dir)
                n_labels = report.get("written", len(outcomes))
        except Exception as exc:  # noqa: BLE001
            print(f"[cascade_audit] write_labels failed: {exc}", flush=True)

    return {
        "spans_total": len(all_spans),
        "spans_recent": len(recent_spans),
        "bots_with_baseline": len([b for b in baselines.values() if b.source != "insufficient_data"]),
        "signals_fired": len(detections),
        "signals_resolved": n_resolved,
        "observe_failures": n_observe_failures,
        "labels_persisted": n_labels,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="cascade.audit_runner — Signals + labels from cascade telemetry",
    )
    parser.add_argument("--network", default=None,
                        help="Path to network.json (auto-resolved when omitted)")
    parser.add_argument("--shared-dir", default=None,
                        help="Override shared dir (default: from network.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print would-be signals; don't write or sweep-resolve")
    args = parser.parse_args(argv)

    if args.shared_dir:
        shared_dir = Path(args.shared_dir)
    else:
        config = load_config(args.network)
        shared_dir = get_shared_dir(config)

    report = run(shared_dir, dry_run=args.dry_run)
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
