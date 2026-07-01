# Circuit Breakers — design spec

**Status:** Draft for discussion · 2026-05-21
**Author:** pod-admin + claude (design dialogue)
**Motivating incident:** a multi-month cost-anomaly audit on a real deployment surfaced ~50 pod-days exceeding the configured threshold over a 90-day window, with **zero same-day threshold alerts fired in the 38-day log window.** The existing detect-and-notify alerter was awake hourly and blind hourly.

---

## 1. Motivation

The cost-anomaly audit makes the case as cleanly as possible: detection-only systems have empirically failed on this pod. The alerter ran, observed nothing, and notified no one — for over a month. By the time a human happened to look, two consecutive admin-bot days at >$150 each had already passed undetected.

The fix is not a better alerter. The fix is a paradigm shift: **act first, notify second.** When unwanted spending or behavior is detected, an automated mechanism halts the responsible activity *immediately* and notifies after, rather than asking a human to be vigilant.

The metaphor is electrical circuit breakers — a familiar, broadly understood pattern. Lean into the metaphor explicitly in UI, naming, and language.

---

## 2. Concept

A **circuit breaker** is a per-bot (or pod-wide) toggle that, when tripped, suppresses some class of activity on the bot. Tripping happens automatically when detector rules fire, or manually by the admin or the bot's primary user.

### Two levels

| Level | Name | What it blocks | What still works | Reset auth |
|---|---|---|---|---|
| **L1** | Cost breaker | Background activity: heartbeats, crons, scheduler-spawned turns, auto-agents | User messaging through normal channels (Slack/Telegram/etc.) | Anyone who can trip can reset |
| **L2** | Full halt | Everything. Gateway is taken down. | Nothing — bot is offline | L2 trips from non-admin sources can be reset by admin only; admin trips reset normally |

L2 collapses what we initially considered separately as "security" and "nuclear." If the bot isn't usefully working, taking it all the way down is simpler and more secure.

### Scope dimensions

Three orthogonal axes:

- **Bot scope**: per-bot or pod-wide
- **Level**: L1 (cost) or L2 (full halt)
- **Trip source**: auto (detector), admin (UI/evo/CLI), primary user (bot chat)

All combinations are valid except: detector cannot auto-trip pod-wide L2 in v1 (too blast-radius-y; require admin confirm).

### Duration

Every trip has a duration: 1h, 4h, 24h, 7d, or indefinite. Default for auto-trips: 24h. Default for manual: depends on entry point (see §4).

---

## 3. User-visible behavior

### 3.1 Admin UI surface

**Per-bot breaker control** lives on each bot tile in the dashboard:

- **Indicator** (always visible): small breaker icon. Grey/closed when all clear. Red/amber badge with count when one or more breakers are tripped.
- **Click target**: opens the breaker modal for that bot. Single click target serves both "I want to trip something" and "I want to manage what's tripped."

**Pod-wide breaker control** is a single button on the dashboard header. Replaces today's "Pause All Bots" button — same physical location, same destructive-tier confirm, but richer modal behind it.

**Modal contents (per-bot or pod-wide, same shape):**

```
┌─ Circuit breakers — team-bot-a ──────────────────────┐
│                                                │
│ CURRENTLY TRIPPED                              │
│   • Cost breaker  · auto · 04:12pm · 24h left │
│     Reason: 130 heartbeat turns/hr on Sonnet  │
│     [Reset] [Extend +24h]                      │
│                                                │
│ TRIP A NEW BREAKER                             │
│   Cost breaker                                 │
│     Blocks background activity. User chat      │
│     still flows.                               │
│     Duration: [24h ▾]  Reason: [optional]      │
│     [Trip cost breaker →]                      │
│                                                │
│   Full halt                                    │
│     Gateway down. Bot fully offline. Use for   │
│     security concerns or to stop everything.   │
│     Duration: [24h ▾]  Reason: [optional]      │
│     [Trip full halt →]                         │
│                                                │
│ [Close]                                        │
└────────────────────────────────────────────────┘
```

The "Trip" buttons are themselves the confirm — the modal opening is the deliberate-act step, no second modal.

**Reset of L2 from any path** requires admin re-auth (already-logged-in admin re-enters password or clicks an explicit "Yes, I'm sure" sub-modal). Cost breaker reset has no such gate.

### 3.2 Evo bot commands

Evo gains two new tools, both with the existing destructive-tier `confirm: true` gate for L2:

| Command | Behavior |
|---|---|
| `evo trip <bot> cost [duration]` | Trip L1 cost breaker on the named bot |
| `evo trip <bot> full [duration]` | Trip L2 full halt on the named bot |
| `evo trip pod cost [duration]` | Trip L1 on every bot |
| `evo trip pod full [duration]` | Trip L2 on every bot (equivalent of today's pause-all) |
| `evo reset <bot> cost` | Reset L1 |
| `evo reset <bot> full` | Reset L2 (requires admin auth context) |
| `evo reset pod` | Reset all pod-wide trips |
| `evo breakers status` | Show current trips across the pod |

Natural-language paraphrases route to these commands via evo's existing tool dispatch.

### 3.3 Primary user via bot chat

The bot's primary user (Marcus, Diana, Carla, etc.) interacts with their bot — not with evo. The bot itself can trip its own breakers, but never another bot's.

**Recognized intents and behavior:**

| User says (paraphrased) | Bot does |
|---|---|
| "Pause your background activity" / "stop running things in the background" | Trip own L1 cost breaker, 24h. Acknowledge. No confirm needed (recoverable, bot still responds). |
| "Resume normal activity" / "turn the breaker back on" | Reset own L1 cost breaker. Acknowledge. |
| "Shut yourself down" / "halt everything" / "I don't trust you right now" | Trip own L2 full halt — **but require explicit confirm first** because the user is about to silence the channel they're using to talk to the bot. |
| "Resume" while in L2 | Cannot self-reset from L2 chat (channel is dead). Document this in the trip confirmation message. |

**The L2 confirmation message:**

> ⚠️ This will fully halt me. You won't be able to message me until I'm reactivated. Anything you send won't be delivered. Your admin can bring me back, or I'll come back on my own in 24 hours.
>
> Reply **CONFIRM** to halt me, or anything else to cancel.

Defaults from chat entry point: L1 = 24h, L2 = 24h. Indefinite trips are not available from chat (must come from admin UI).

### 3.4 CLI

```
evolve-admin breaker trip <bot|pod> <cost|full> [--duration 24h] [--reason "..."]
evolve-admin breaker reset <bot|pod> <cost|full>
evolve-admin breaker status [--bot <bot>]
```

### 3.5 Auto-recovery (how the bot comes back)

Three pathways:

1. **TTL-based auto-recovery (primary).** Every trip has a duration. When the duration elapses, heal.py — which already polls every 5 min and already reads the pause flag — clears the breaker and, for L2, runs `launchctl bootstrap` to restart the gateway. For L1 there's nothing to restart; clearing the flag is enough.

   Latency: up to 5 min after expiry. Acceptable. Indefinite trips never auto-recover.

2. **Manual reset.** Admin UI button, evo tool, CLI. Force-clears regardless of TTL. For L2 manual reset, the gateway bootstrap happens immediately.

3. **Failsafe — if heal.py is broken.** Manual reset paths all work without heal. heal-liveness is independently monitored by pod_health; if heal is stale, a Signal fires telling the admin to fix heal before relying on auto-recovery.

**Fail-open property:** if the breaker state file is corrupt, missing, or unreadable, heal.py treats the bot as not-tripped and restarts the gateway normally. Same behavior as today's pause-state.json reader. Better to fail-open than to brick a bot on a truncated JSON file.

### 3.6 Channel-side "out of office" message

When L2 trips, the bot posts a single status message to its primary channel(s):

> 🔴 I've been halted (circuit breaker tripped). I won't reply to messages until I'm reactivated.
>
> Auto-recovery: at [time] · Trip reason: [reason]

When L2 clears, post a corresponding "I'm back" message.

For L1 trips, no channel message is needed (user-facing behavior is unchanged from the user's perspective).

### 3.7 Notification

On every trip:

- **Bot's primary user** — notified through the bot's primary channel (if L1) or via the "out of office" message (if L2). Plain-language explanation.
- **Admin via evo bot** — notified through evo's primary channel with technical details: which breaker, why, what the detector saw, what the recommended remediation is.
- **Admin UI** — banner / alerts page entry. Persists until trip clears.

The audit-of-cause and remediation recommendation is generated **asynchronously after** the trip, not synchronously. The trip itself doesn't wait on LLM analysis. See §5.3.

---

## 4. Auth model

| Action | Admin (UI/evo with admin context) | Primary user (bot chat) | Detector (auto) |
|---|---|---|---|
| Trip L1 cost (own bot) | ✓ | ✓ no confirm | ✓ |
| Trip L1 cost (other bot) | ✓ | — | — |
| Trip L1 cost (pod-wide) | ✓ | — | ✓ (with cooldown) |
| Trip L2 full (own bot) | ✓ | ✓ with explicit confirm | ✓ on security/policy triggers |
| Trip L2 full (other bot) | ✓ | — | — |
| Trip L2 full (pod-wide) | ✓ with destructive confirm | — | — (admin-only) |
| Reset L1 (own bot) | ✓ | ✓ | n/a |
| Reset L2 (own bot, but they tripped it) | ✓ | ✗ (channel dead, can't talk) | n/a |
| Reset L2 (someone else tripped) | ✓ | ✗ | n/a |
| Reset pod-wide L2 | ✓ admin re-auth | — | — |

The asymmetry to keep in mind: **trip is fail-safe (stops activity), reset is the dangerous direction (re-exposes).** Auth gates are heavier on reset for high-impact breakers.

---

## 5. Technical architecture

### 5.1 Detection layer

Three independent signals, **OR semantics** (any one trips):

#### 5.1.1 Activity-shape (primary)

Rule: turn count per unit time, broken down by `(channel, source, model_tier)`. Crosses threshold when:
- `auto_source_rate` (turns/hr where `source ∈ {heartbeat, cron, scheduler, auto}`) exceeds bot-specific baseline by Nx, AND
- `human_source_rate` (turns/hr where `source = human` AND `channel ∈ {slack, telegram, discord}`) is **not** correspondingly elevated, AND
- tier-mix shifts up (tier-2 or higher share of auto turns rises)

Why primary: activity-shape doesn't depend on cost reporting being accurate. The audit showed `cost: 0` records — silent cost-recording failure is a real mode. Turn-existence is much harder to lie about.

#### 5.1.2 Gateway-recorded cost rate (secondary)

Rule: $/hr sustained over N hours where auto-source share > Y%. Uses the existing TurnObserver cost data. Lags activity-shape by minutes to hours depending on cost-reconciliation cadence.

#### 5.1.3 Provider-side cost (tertiary, where available)

Rule: pull Anthropic console / OpenAI usage API hourly, compare against gateway-recorded spend for the same window. Trip on:
- Absolute provider-side spend rate exceeding bot-specific cap
- Divergence > 10% between provider and gateway (this itself is a signal that the gateway is mis-reporting — could indicate a real bug)

Not all providers expose this. Optional, not gating.

#### 5.1.4 Security signals → L2

The existing `security_warden` generator already produces Signals for policy violations. Specific severity-or-above signals trigger L2 trip on the violating bot. The exact mapping is defined in security_warden's charter, not here.

#### 5.1.5 Bot-specific baselines

Each bot has a rolling 7-day baseline for activity-shape and cost-rate. Cold-start bots (< 3 days of data) use static caps instead, defined per-bot in network.json or a new breaker-config file.

### 5.2 Enforcement layer

**L1 cost breaker — gateway up, plugin/shim veto.**

The TS plugin (or a small Python gateway shim, TBD — see open questions) reads the per-bot breaker state file before processing each turn. If an L1 cost breaker is tripped and the turn's `source ∈ {heartbeat, cron, scheduler, auto}`, the gateway rejects the turn with a structured error. The rejection is itself recorded as a turn (with `rejected_by_breaker: cost`) for forensics — good for "how many turns did the breaker save us?" reporting.

User-channel turns (source = human, channel = slack/telegram/etc.) flow through unchanged.

**L2 full halt — gateway down, launchctl bootout.**

Reuses the existing `pause_all` mechanism in [recovery.py](packages/admin/evolve_admin/recovery.py), generalized to per-bot scope. For a per-bot L2 trip: write the breaker state, then `launchctl bootout` just that bot's gateway daemon. For pod-wide L2: same physical action as today's pause-all, just driven from the new code path.

### 5.3 State store

**File layout** (under `{shared_dir}/breakers/`):

```
{shared_dir}/breakers/
├── <bot_id>/
│   ├── cost.json        # L1 cost breaker state for this bot
│   └── full.json        # L2 full halt state for this bot
├── pod/
│   ├── cost.json        # pod-wide L1 (all bots)
│   └── full.json        # pod-wide L2 (all bots)
└── log/
    └── <YYYY-MM-DD>.jsonl   # append-only audit log
```

A breaker file's **existence indicates a trip is active.** No file = no trip. The state field within is informational; existence is the source of truth.

**Schema** (per breaker file):

```json
{
  "bot_id": "team-bot-a",                     // or "pod" for pod-wide
  "type": "cost",                      // "cost" | "full"
  "state": "tripped",                  // "tripped" only (cleared trips delete the file)
  "tripped_at": "2026-05-21T16:12:00Z",
  "expires_at": "2026-05-22T16:12:00Z", // or null for indefinite
  "initiated_by": "auto",              // "auto" | "admin:<id>" | "user:<bot_id>" | "evo:<admin_id>"
  "reason": "130 heartbeat turns/hr on Sonnet, 7-day baseline 8/hr",
  "motivating_signals": ["signal-id-1", "signal-id-2"],
  "trip_id": "uuid",                   // for audit-log correlation
  "audit_summary": null,               // populated async after trip; see §5.4
  "audit_recommendation": null
}
```

**File-format discipline:** flat JSON, no pickle-derived shapes, trivially readable without importing any Python package. This matters because heal.py reads the existing pause flag *directly* (bypasses `evolve_admin.recovery` import) and any future enforcer faces the same sys.path constraint. Same constraint applies to the TS plugin reader.

**Atomic writes:** temp file + `os.replace`. Same pattern as `_atomic_write_json` in recovery.py. Owned by the `evolve` user; the `breakers/` dir is created under `{shared_dir}` which already has the right ACL.

**Audit log** (`{shared_dir}/breakers/log/<YYYY-MM-DD>.jsonl`): append-only, immutable record of every trip and reset. One JSON object per line, with `trip_id`, `action` (`trip` | `reset` | `extend` | `auto_recover`), timestamp, initiator, reason, breaker payload. Retention: 1 year (mirrors the signal-store log retention).

### 5.4 Audit-of-cause (asynchronous)

After a trip lands and state is persisted, an async generator runs to produce `audit_summary` and `audit_recommendation`. This is where the RSI/generator infrastructure earns its keep:

- Read the last 1-4 hours of turn data on the tripped bot
- Identify the dominant burn pattern (model overrides, runaway sessions, cache misses, etc.)
- Produce a plain-language summary and a recommendation ("override haiku for heartbeat in agents.defaults; this looked like the same shape as the 2026-05-20 security-bot incident")
- Write back to the breaker file's `audit_summary` and `audit_recommendation` fields
- Notify evo / admin with the analysis

The trip itself **does not wait** on this. The async analysis is decorative, not blocking. If the generator fails, the trip is still in effect; the admin just doesn't get the recommendation.

### 5.5 Suppression contract — "don't fight the breaker"

**This is the failure mode the user explicitly flagged: when a bot is tripped, the existing monitoring/healing/alerting infrastructure must not freak out and try to "fix" the state by restarting things, generating panic alerts, or piling on with more notifications.**

The contract: **any monitor, generator, or daemon that observes or acts on bot state MUST consult the breaker state before generating alerts or taking corrective action against a bot that is currently tripped.**

#### 5.5.1 Two enforcement points

**Operational (action-taking) consumers** read breaker state directly and skip their action:

- **heal.py** — already pause-aware. Extend the existing check from "is the pod paused?" to "is bot X tripped (L2)?" For an L2-tripped bot, skip restart. For an L1-tripped bot, no behavior change (gateway should be up). When TTL expires, heal becomes the reaper that clears the file and bootstraps the gateway.

- **verify daemon** (the one that re-verifies after proposals are applied) — must not treat an L2-tripped bot's gateway-down state as a regression. Add breaker-state check before flagging.

- **apply.py** and other proposal appliers — must not apply config changes against a tripped bot. Queue them, re-check on next cycle, apply when the breaker clears.

- **Plugin gateway code** — reads breaker state on every non-user-channel turn to enforce L1.

**Observational (signal-producing) consumers** are suppressed at the signal-store layer:

- `signals.store.observe()` consults breaker state when a new firing signal is being created. If the bot has an active breaker AND the signal's category is "suppressible during trip" (cost, gateway-down, no-recent-activity, etc.), the signal is still recorded for forensics — but tagged `suppressed_by_breaker: <trip_id>` and not surfaced as a new firing alert in the Alerts UI.

- When the breaker clears, suppression naturally lifts. Signals that are still applicable re-promote to firing; signals that resolved during the trip stay archived.

- Some signal categories are **never suppressed** (e.g. the signal store itself becoming unreachable, heal.py being stale, gateway processes wedged in ways unrelated to the trip).

#### 5.5.2 Inventory — every monitor/generator/daemon to audit

| Producer | Type | What it does | Suppression behavior |
|---|---|---|---|
| heal.py | daemon | restarts gateways | Already pause-aware. Extend for per-bot L2. Reaper role for TTL expiry. |
| pod_report | monitor | reports pod state | Suppress "bot offline" signals when bot is L2-tripped. |
| audit | monitor | security audit | Mostly read-only; no behavior change needed but should include breaker state in reports. |
| cost_watchdog | monitor | cost spike detection | **Don't pile on.** When a bot already has L1 cost breaker tripped, don't fire new cost-anomaly signals on the same bot. Resume after breaker clears. |
| host_health | monitor | hardware/OS state | No suppression — host-level signals are independent of bot state. |
| error_reporter | monitor | surfaces errors | Suppress "gateway 500" type errors when the bot is L2-tripped. |
| integration_probe | monitor | external integration health | Skip probes for L2-tripped bots (gateway is down by design). |
| pod_health | monitor | comprehensive health checks | Annotate health output with breaker state. Don't fail health checks on L2-tripped bots for "gateway down." |
| security_warden | monitor → generator | security signals | **Can trigger L2 auto-trip on severe-enough signals.** Doesn't suppress itself; keeps generating signals. |
| test_runner | monitor | runs tests | Skip test runs against L2-tripped bots. |
| budget_hawk | generator | cost-related proposals | Don't generate proposals against L1- or L2-tripped bots. Wait for clear. |
| efficiency_hawk | generator | efficiency proposals | Same as budget_hawk. |
| cache_ttl_tuner | generator | cache tuning | Same. |
| auth_drift_filler | generator | auth-config drift | Don't write to tripped bots. |
| cron_caps_filler | generator | cron-cap drift | Don't write to tripped bots. |
| persona_tuner | generator | persona evolution | Don't write to tripped bots. |
| plugin_curator | generator | plugin curation | Don't write to tripped bots. |
| user_profile_inferrer | generator | user profile updates | Skip (no new user turns to learn from on L2). |
| evolve_watchdog | monitor | meta-watchdog | No suppression — should still fire if heal/admin/breaker itself is broken. |
| sysadmin_watchdog | monitor | sysadmin-class issues | No suppression. |
| gateway_diagnostician | generator | gateway diagnosis | Don't treat L2-tripped gateways as broken. |
| Alerter (cron) | monitor | the existing $-threshold alerter | **Don't pile on.** L1 cost trip means spend went to zero; alerter should not fire "spend went to zero" or continue to fire on yesterday's spike that's already been responded to. |

This list is the audit deliverable for implementation Phase 6 (see §8). Every entry needs an explicit check.

#### 5.5.3 The "no negative feedback loop" rule

State this as a hard invariant:

> **No daemon, monitor, or generator may take action that would cause a tripped breaker to re-trip itself, or that would undermine the trip.**

Concretely:
- Don't restart a tripped gateway (heal.py).
- Don't apply config that would resume background activity (apply.py).
- Don't enqueue more work on a tripped bot (continuity engine).
- Don't alert about the very thing the trip already addressed (cost_watchdog, alerter).

Violations of this rule are observability bugs of the worst kind — they create circular firing squads. Add a CI check / integration test that simulates a trip and verifies no producer takes a forbidden action.

---

## 6. Migration from pause-state.json

[recovery.py](packages/admin/evolve_admin/recovery.py) currently owns the only "stop the bots" primitive. Readers of `pause-state.json` (verified):

- heal.py — direct file read (bypasses recovery.py import on purpose)
- `/api/recovery/status` endpoint → dashboard
- `evolve-admin pause-status` CLI
- Evo tools (`pod_state.pause_state`, `action.pod.pause_all`/`resume_all`)
- Tests

The TS plugin does **not** read pause-state today. Crons / heartbeats / scheduler do **not** read it.

**Migration strategy: compat-write, lazy reader migration.**

1. **Phase 1 (additive):** introduce new `{shared_dir}/breakers/` schema. Every pod-wide L2 trip writes BOTH the new schema AND the legacy `pause-state.json` (with a `migrated_to_breakers: true` field for clarity). No reader changes. Existing pause-all behavior is unchanged.

2. **Phase 2 (reader migration):** update heal.py to read the new schema (small PR — heal.py's reader is one block of code). Once heal reads new schema, retire the compat write. Update the web API endpoint to read new schema too. Update evo tools.

3. **Phase 3 (renames):** `/api/recovery/*` → keep for backwards compat, add `/api/breakers/*` alongside. CLI: keep `evolve-admin pause-all` as an alias for `evolve-admin breaker trip pod full`.

The "Pause All Bots" button in the dashboard header stays in the same physical place. Behind the scenes it now opens the modal with pod-wide options instead of immediately confirming a single action. UX evolves; muscle memory preserved.

---

## 7. Backtest as ship gate

The detector cannot ship until it passes a backtest against the private 90-day cost-anomaly corpus (kept out of this repo; maintained alongside the deployment's billing data).

**Required test corpus** (positive cases — detector MUST trip):
- The originating same-day spike on the security-monitoring bot.
- A heartbeat-on-wrong-model spike (background bot held an expensive default).
- A multi-day expensive-default spike (two consecutive days at >5× a normal day).
- A single-day cost spike on a security-monitoring bot.
- The ~40 (bot, date) pairs in the heartbeat-on-wrong-model category.
- A three-consecutive-day heartbeat-cadence-plus-cache-miss pattern.

**Required negative cases (detector MUST NOT trip):**
- All `channel=telegram source=human` cache-write-no-reuse "runaways" (these are legitimate user chats)
- All `channel=slack source=human` cache-write-no-reuse "runaways"
- Long single sessions where one user had a deep conversation

**Pass criteria:**
- Recall on positive cases: ≥ 90% (≥ 8 of 9 high-confidence incidents trip)
- False positives on negative cases: 0 (zero tolerance — false-positive trips on user chat are unacceptable)

The backtest harness is a deliverable: replays cached turn JSONLs through the candidate detector, reports per-incident trip/no-trip, fails CI on regression. Lives at `packages/analyzer/breakers/backtest.py` or similar.

---

## 8. Implementation phases

**Phase 1: detector + backtest (observe-only).**
Build the activity-shape detector and the backtest harness. Run it against the 90-day audit data. Tune until pass criteria met. **No enforcement, no UI, no trips written.** Detector logs "would have tripped X" entries for review.

**Phase 2: state store + manual-trip primitive.**
Create `{shared_dir}/breakers/` schema. Implement `breakers.store.trip()`, `breakers.store.reset()`, `breakers.store.read()`. Audit log. CLI: `evolve-admin breaker trip|reset|status`. Tests.

**Phase 3: enforcement layers.**
- L2: extend `recovery.py` to take per-bot scope. The new `breakers.enforce_l2(bot_id)` calls into the existing `_bootout_gateway` machinery.
- L1: TS plugin (or Python gateway shim) gains a breaker-state reader and veto logic for non-user-channel turns.
- heal.py: extend the existing pause-flag reader to consult per-bot breaker state. Add TTL-reaper role.

**Phase 4: UI + evo tools.**
- Dashboard: per-bot breaker control on each bot tile; pod-wide breaker on header (replaces "Pause All Bots"); modal.
- Evo: new tools `action.bot.trip_breaker`, `action.bot.reset_breaker`, `action.pod.trip_breaker`, `action.pod.reset_breaker`. `pod_state.breakers` read companion.
- Channel-side "out of office" message for L2.

**Phase 5: auto-trip from detector.**
Connect Phase 1 detector to Phase 2 state store. Detector runs every N minutes, evaluates rules, trips when threshold crossed. Auto-trip notifies admin via evo immediately; async audit-of-cause follows.

**Phase 6: suppression contract retrofit.**
Audit every producer in §5.5.2. Add the breaker-state check to each. Add the "no negative feedback loop" CI integration test.

**Phase 7: provider-side cost verification (optional, where APIs allow).**
Hourly pull from Anthropic console / OpenAI usage. Cross-check against gateway-recorded spend. New detector input.

Phases 1-2 can ship together if convenient. Phase 6 can run partially-in-parallel with 3-5 (the more important suppression checks — heal.py, cost_watchdog, apply.py, the alerter — should land before auto-trip in Phase 5 to avoid circular alerts during the auto-trip era).

---

## 9. Decisions (formerly open questions)

Resolved during design dialogue 2026-05-21:

1. **L1 enforcement: TS plugin.** Reads the breaker state file directly. Lowest latency, single process, no extra hop. The TS plugin needs a file-watch (or short TTL-cached poll on each non-user-channel turn). Reliability across OC plugin reloads is verified in Phase 3.

2. **Source veto semantics.** Veto when `source ∈ {heartbeat, cron, scheduler}` OR `channel = "unknown"`. Allow when `source ∈ {user, human}` AND `channel ∈ {slack, telegram, discord, web}`. Anything else → allow (fail-open in user's favor). Codified in `breakers.classify._is_auto_source()`.

3. **Detector cooldown after auto-trip.** Allow immediate re-trip. A second auto-trip within 48h of the first auto-clear also fires a `recurrent_breaker_trip` Signal so the admin sees the pattern and can address root cause rather than letting the breaker mask it indefinitely.

4. **Cross-bot dependency: per-bot only.** Auto-trip never cascades to other bots. If team-bot-a and security-bot share a heartbeat misconfiguration, that surfaces as two separate auto-trips and the admin correlates via evo. Cascading auto-trip is a v2 consideration if pattern data shows it's needed.

5. **Conversational trip recognition: keyword commands only in v1.** The bot recognizes deterministic commands (or evo dispatches via its existing tool surface). Natural-language paraphrase recognition is deferred to v2 once we have a corpus of real attempted phrasings.

6. **pod_health heal-stale check: audit task during Phase 3.** Confirm pod_health independently observes heal liveness. If missing, add it before relying on heal as the TTL reaper.

---

## 10. Out of scope / deferred

- **Active proxy reply for L2-halted bots** (bot can't respond; today we accept the silence and rely on the channel-side OOO message). Possible v2.
- **Selective L1** — e.g., "block crons but allow heartbeats" — too fine-grained for v1. Two levels only.
- **Breaker scheduling** — "trip every weekday from 11pm to 7am." Not a v1 need; can be added later as automated tripping with a cron trigger.
- **Per-app-within-bot breakers** — tripping just the security app vs the gallery app on a single bot. Premature; revisit when applications-as-contracts has a richer runtime story.
- **Cross-pod / fleet-level breakers** — Evolve is single-pod today.

---

## 11. Naming reference

- **Breaker** — the toggle
- **Trip** — verb for activating the breaker (it goes from clear to tripped)
- **Reset** — verb for clearing the breaker (it goes from tripped to clear)
- **Cost breaker** / **L1** — background-activity-only halt
- **Full halt** / **L2** — gateway-down halt
- **Pod-wide breaker** — all bots, single trip
- **TTL** — auto-recovery duration

Avoid: "kill" (suggests permanent), "pause" (overloaded with the legacy pause-all term; reserve for backward-compat surface), "disable" (too vague).

---

*End of draft. Open questions and inventory in §5.5.2 and §9 are the highest-value review targets.*
