# Spec — Subscription-complete observability

**Aspect:** `reports` · **Created:** 2026-06-24 · **Status:** DESIGN. Workstream B
dispatched (PR pending); **A + C held for operator design review** before dispatch.

## Motivation

Operator principle (2026-06-24, evo-vps): *every automated message sent to the
Evo bot should be tied to a "subscription," every message should have a
corresponding entry in Reports → Subscriptions → Messages, and expanding a
message should show which subscription it belongs to plus a one-click control to
enable/disable that class.*

The ACL-mask flapping storm (430/hr) surfaced the gaps: the operator could not
find their live messages in the Messages tab, and many storm messages carried no
subscription handle at all. The model is ~80% there already — this initiative
closes it into an enforced contract.

## Grounding (what exists today)

- **Catalog:** `packages/admin/evolve_admin/alerts/catalog.py` — 57 `CatalogEvent`
  entries, namespaced `security.* / system.* / summaries.* / updates.*`. Each has
  `key`, `category`, `label`, `description`, `default_enabled`, `default_frequency`,
  `allowed_frequencies`, `severity`, `producer_source`, `body_template`,
  `sample_payload`, `action`, **`is_safety_critical`**, `announce_unannounced_resolve`.
- **Binding:** dispatch records (`{shared_dir}/alerts/dispatcher/<date>.jsonl`)
  carry `catalog_event` — the subscription handle. Schema:
  `{ts, source, result, severity, catalog_event, dedup_key, message_excerpt}`.
- **Gating / Configure:** `routes_alerts.py` exposes the catalog + per-event
  `{event_key, enabled, frequency}` config and the `alerts.<source>.enabled`
  source toggle. **Today, `catalog_event=None` deliberately skips subscription
  gating** (`digest_dispatcher.py:432`) — i.e. unbound = ungateable.
- **Ledger source:** `dispatcher.jsonl` records every dispatch (sent / deferred /
  suppressed / failed across lanes). Data is complete; the Messages UI is not.

### Measured gap (evo-vps, 2026-06-24, ~8,300 dispatches)
`catalog_event` populated on ~81%. Unbound (`None`): `alert_rate_breaker`
(storm-mode reminders) ALL, `send_surface_probe` ALL, `digest_dispatcher`
meta-sends ALL, ~1,573 `signal_notifier` rows (mostly "Cleared:" resolutions +
unmapped signal types).

## Invariants

### A. Bind-complete (keystone) — *held for review*
**Every dispatched message resolves to a `catalog_event`.** No message reaches a
channel with `catalog_event=None`.

- Add catalog classes for the currently-unbound meta/system senders
  (e.g. `system.storm_mode`, `system.send_probe`, `system.digest`, plus mapping
  the unbound `signal_notifier` resolution/unmapped rows). A `meta.*` namespace
  may be cleaner than overloading `system.*` — decide at design review.
- **Replace the `catalog_event=None → skip gating` escape hatch** with the
  `is_safety_critical` flag as the gating modifier (below). Binding is now
  universal; safety is expressed by the flag, not by being unbound.
- **Producer-side ratchet:** a dispatch with no resolvable `catalog_event` is a
  contract violation — fail the dispatch path test / lint, the same shape as the
  existing producer-legibility ratchet (coalesce_key / human_title / signature).
  This is what turns "every message tied to a subscription" from aspiration into
  an enforced invariant.

**Safety-critical UX decision (operator, 2026-06-24): toggle WITH confirm
warning.** Safety-critical classes remain *disableable* (maximum operator
control) but disabling one triggers a confirm dialog warning it silences a safety
alert. So: bind everything; `is_safety_critical` does NOT block the toggle, it
gates a confirm-warning in the UI. (This supersedes the "always-on, no toggle"
option.)

### B. Ledger-complete — *DISPATCHED (chip B)*
The Messages tab is the complete, current record of dispatched messages.

- **Read-cap fix (dispatched):** `api_alerts_dispatcher_log` read the oldest ~400
  lines of the newest day file and broke, hiding recent sends on high-volume
  days. Fix = tail the newest day file (read recent-first). Branch
  `claude/reports-dispatcher-log-tail`.
- **Follow-on (this spec):** unify the sent / deferred / suppressed / failed lanes
  into one filterable Messages view (today: suppressed behind a checkbox, failed
  in the Dispatcher Health box). Operator filters by lane + subscription, sees one
  complete ledger.

### C. Provenance + inline control on expansion — *held for review*
Expanding a Messages row shows its subscription and a control to enable/disable
that class.

- Resolve the row's `catalog_event` → catalog entry → render `label`,
  `description`, `category`, `severity`, and the source (`producer_source`).
- Inline enable/disable wired to the existing Configure PUT
  (`{event_key, enabled, frequency}`); `is_safety_critical` rows show the
  confirm-warning on disable (per A's decision). Optional: frequency control inline.
- Pure wiring on top of A — depends on A so every row has a resolvable class.

## Sequencing

1. **B** (read-cap) — dispatched; urgent + self-contained.
2. **A** (bind-complete contract + catalog pass + ratchet) — keystone for C.
3. **C** (expansion provenance + control) — wiring on A.

## Open questions (resolve at A+C design review)

- `meta.*` namespace vs overloading `system.*` for the meta-senders?
- Which currently-unbound classes are genuinely `is_safety_critical`
  (dead-letter, dispatcher-health, bring-up) vs ordinary?
- Does the ~1,573 `signal_notifier` NULL bucket need new classes, or do those
  rows map to existing catalog keys that just aren't being stamped on the
  resolve/clear path?
- Lane-unification (B follow-on): one view with filters, or keep Health separate?

## Boundary

`reports`-owned (Subscriptions + Messages UX + signal-producer quality). Proposal
*generation* quality stays with `rsi`. No new remote write surface. SPA changes
honor style-guide + both-theme.

---

# Phase 2 — Subscription consolidation (operator IA)

**Status:** DESIGN (operator-approved taxonomy, 2026-06-24). Build pending.

## Problem
Phase 1 made every message bind to a `catalog_event`, but `catalog_event` == the
operator-facing Subscription (1:1), yielding ~65 toggles. Operator: "WAY too many
— need 10–15; group into coherent groups; some don't belong at all" (e.g.
`proposal_rejected` = "we told you the thing we decided not to tell you";
`briefing_activated` = sending a message to confirm a user's own in-app action).

## The move: two layers
- **Internal events (~65)** — KEEP. They drive routing, dedup, digest rollup,
  body templates, producer mapping. Phase 1's "every signal → catalog_event"
  ratchet still holds.
- **Operator Subscriptions (13)** — NEW grouping layer ABOVE events. Each
  `CatalogEvent` declares a `subscription` membership. **Gating moves from
  per-event to per-Subscription.** Configure lists 13; the Messages-tab
  provenance (Phase 1 C) shows the event's Subscription + the group toggle.

## The 13 (operator-approved)
1. Needs your decision — proposal_ready, forge_job_ready
2. Security findings [safety-critical] — audit_finding, cve_finding, config_drift, key_rotation_overdue
3. Bot permissions & autonomy — autonomy_posture_drift, autonomy_review, autonomy_limit_hit, autonomy_demoted, autonomy_promotion_candidate
4. Cost warnings — daily_threshold, burst_detected, heartbeat_session_bloat
5. Cost enforcement [safety-critical] — hard_cap_hit, breaker_tripped, tier_downgrade, gateway_stopped, session_budget_exceeded, forge_session_cap
6. Pod & gateway health [safety-critical] — gateway_state_change, gateway_autorestart_failed, watchdog, daemon_error_spike, repo_puller_wedged (+recovered as its closing bracket), stalled_cron, sudoers_refresh_failed, stuck_proposal
7. App & message delivery [safety-critical: send_surface_broken] — app_scheduled_work_failure, app_script_failure, app_delivery_missed, app_delivery_unmeasurable, pod_delivery_regression, send_surface_broken, manifest_validation_failed, app_install_failed
8. Bot configuration & plugins — identity_doc_missing, plugin_health_issue, exec_outcome_failure, oc_cli_misinvocation
9. Software updates — openclaw_available, openclaw_blocked, openclaw_surface_drift, evolve_repo, plugin_available
10. Daily pod report — daily_pod_report
11. Weekly summaries — weekly_rsi_review, weekly_bot_trends
12. Alerting system [safety-critical] — storm_mode, dispatcher_health
13. Upstream issue tracking (dev profile, default OFF) — upstream_issue_resolved, upstream_issue_activity, upstream_issues_watcher_auth

**Operator decisions:** (a) system-health → THREE groups (6/7/8, not merged);
(b) Cost SPLIT into warning (#4, freely mutable) vs enforcement (#5,
safety-critical, warns on mute). Safety-critical is now a Subscription-level
property; a group is safety-critical if it contains a "blind-to-blindness" member.

## Removed/internalized (8) — not operator subscriptions
- decisions.proposal_rejected — "told you the thing we decided not to tell you." STOP notifying (keep internal feedback.jsonl record if used for signal-tuning).
- decisions.proposal_outcome_checkin ("did this help?") — STOP.
- decisions.briefing_activated — don't send a message to confirm a user's own in-app action. STOP producing.
- decisions.proposal_applied — daily report covers it. STOP (fold into #10).
- meta.digest — the digest envelope isn't a toggle; per-class frequency controls contents. Internal.
- meta.send_probe (green-light) — drop the all-clear; the FAILURE is send_surface_broken (#7). Internal.
- meta.alert_repeat_loop — internal noise-detection; folds into #12 or stays internal.
- meta.unclassified — dev routing safety-net; keep routing-internal, hide from Configure UI.

## Build shape (≈3 chips, sequential)
1. **Backend grouping/remap** — define the 13-Subscription registry (id, label, description, default_enabled, is_safety_critical); add `subscription` to every `CatalogEvent`; move gating per-event → per-Subscription; Configure GET returns 13 groups, PUT toggles a group; `_catalog_event_for_signal` → event → subscription → enabled. Keep the per-event catalog identity for routing/body.
2. **Configure + Messages UI** — Configure lists 13 groups (not 65 events); Phase-1-C provenance shows the group; inline toggle operates on the group; safety-critical confirm at group level.
3. **Producer removals** — stop producing/notifying the 8; `briefing_activated` stops sending entirely; verify nothing else references the removed keys.

**Invariant preserved:** every signal still maps to a `catalog_event` (Phase 1
ratchet); the new ratchet adds: every non-internal `catalog_event` maps to exactly
one of the 13 Subscriptions.
