# Alerts / Signal Store — Architecture (2026-05-07)

Status: **draft** (design locked in conversation 2026-05-07; implementation begins with Phase 0 + Phase 4).

**What this is.** The architecture for the consolidated alerting layer of the Evolve admin pod. Today the admin UI scatters alert-like signals across ~12 different surfaces produced by ~30 different monitors. This spec defines a single Signal store that all monitors write to, two display patterns that read from it (a consolidated Alerts page + contextual badges on feature pages), and the bidirectional link between Signals and RSI Proposals that closes the feedback loop between observation and action.

**Naming note.** The admin UI has a "Reports" tab today. As of this spec, "Reports" becomes "Alerts" — what was being delivered is alerts, not reports (they fire conditionally on threshold breaches, carry severity, and route through a notify channel). The scheduled green-digest cadence is preserved as a heartbeat over the same store.

**Relationship to other specs.**
- [spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — defines Better Engine, Generators, and the Proposal pipeline. This spec adds the *complement*: monitors that produce Signals, kept separate from generators that produce Proposals. The two stores cross-link via `Proposal.motivating_signals[]`.
- The existing `arbiter.store` helpers (proposal subdirs by status) are the pattern for the new `signals.store` helpers.
- This spec supersedes the ad-hoc storage paths used today by `pod_report.py` (report artifacts), `evolve_watchdog/observe.py` (JSONL events), `audit.py` (current-findings.json), and `reporter.py` (error banner dismiss state).

---

## 1. The problem with the current frame

Three structural problems with how alerts work today:

**1. Scatter.** 30 distinct alert-like signal sources land on roughly 12 different UI surfaces — the Reports tab, a watchdog-events endpoint with no UI consumer, three security-status pages, an Errors banner with its own dismiss state, inline integration probe warnings, host-health and gateway endpoints with no dashboard at all, plus Telegram-only deliveries for some audit findings. The sysadmin has no single place to ask "what's wrong right now?"

**2. Dead data.** `evolve_watchdog` writes 11 event types to `shared_dir/watchdog/<date>.jsonl` and exposes them via `/api/arbiter/health/watchdog-events` — but no UI page renders them. Host CPU/disk/memory has an API endpoint but no dashboard. Per-bot gateway status is API-only. Real signal exists; nothing surfaces it.

**3. Inconsistent affordances.** Some alerts have snooze (the RSI-pending line). Some have dismiss (the Errors banner, with its own dismiss store at `~/.evolve/report-dismissed.json`). Most have neither. Severity is encoded inconsistently — pod_report uses red/yellow/green, watchdog uses info/warn/alert, audit uses CRITICAL/elevated/info. There's no consistent way to act on what you see.

A consolidation is overdue. The constraint: don't lose the contextual surfaces (an integration-probe failure should still appear next to the integration it concerns) and don't lose the daily heartbeat digest.

## 2. Core reframe: two stores, two upstreams

The existing Better Engine architecture separates *generators* (which propose changes to bots) from the things they observe. This spec extends that separation by naming the observation side and giving it its own store:

| Concept | Role | Output | Lives in |
|---|---|---|---|
| **Generator** (existing) | Proposes changes to bots | `Proposal` (charter-driven, fingerprinted, portfolio-tracked) | `packages/analyzer/generators/<id>/` |
| **Monitor** (this spec) | Observes state, fires when something is unusual or broken | `Signal` | `{shared_dir}/signals/` |

A subsystem can play both roles — `security_warden` is plausibly both a generator (proposes hardening changes) and a monitor (emits "conduct_violation" Signals when it sees something live) — but each output flows into the right store.

**On `evolve_watchdog`.** Originally this spec called for moving `evolve_watchdog` out of `generators/` on the assumption that it was "a monitor wearing a generator costume." Phase 1 implementation revealed otherwise: watchdog emits both Proposals (`ThrottleGenerator`, `Investigation`) AND events. It is genuinely both a generator and a monitor. It stays in `generators/`. The dual-role pattern is the same one called out for `security_warden` in the table above.

## 3. Two axes, not one: severity vs. disposition

A subtle but load-bearing distinction. A 4× cost day on a busy bot is **high-severity-low-disposition**: noteworthy magnitude, nothing to fix. A steady gateway flap at warn is **low-severity-high-disposition**: small magnitude, but action is required. These are independent axes.

Today's UI conflates them. The new model treats them separately:

- **Severity** (`info | warn | alert`) — how big the magnitude of the signal is. One threshold replaces the previous yellow/red split.
- **Flavor** (`activity | maintenance`) — whether action is required.
  - **Activity** — unusual but not necessarily broken. Skim-and-dismiss feed. Most days, most items here get dismissed unread.
  - **Maintenance** — something is broken or degraded and needs fixing. Task inbox. Each item is a TODO.

Producers set both fields at write time. The Alerts UI presents two lanes (one per flavor) so the user's job is different in each: triage in Activity, queue-and-fix in Maintenance.

A signal can also transition flavor during its lifetime — an Activity signal that doesn't abate after several days may be promoted to Maintenance by the producer (or by a user action). This is rare; most signals stay in their original flavor.

## 4. The Signal record

```python
Signal:
    # Identity
    id: str                          # uuid, immutable
    signature: str                   # producer:type:scope_key — for find-or-create
                                     # e.g. "pod_report:cost_spike:admin-bot"
                                     #      "host_health:disk_low:mini"

    # Origin
    producer: str                    # "pod_report" | "watchdog" | "audit" | "warden"
                                     # | "pod_health" | "host_health"
                                     # | "integration_probe" | "error_reporter"
    type: str                        # producer-namespaced — e.g. "cost_spike"
    flavor: "activity" | "maintenance"
    severity: "info" | "warn" | "alert"
    scope: "pod" | "bot" | "host" | "integration"
    bot_id: str | None               # null for pod/host scope

    # Body
    title: str                       # one-line user-facing
    body: str                        # markdown detail
    details: dict                    # typed payload, producer-specific
                                     # (current_value, baseline, samples, etc.)

    # Lifecycle
    state: "firing" | "snoozed" | "resolved" | "dismissed"
    created_at: ts                   # first-seen
    last_observed_at: ts             # most-recent producer touch
    observation_count: int           # +1 each time producer sees it still firing
    snoozed_until: ts | None
    resolved_at: ts | None
    state_history: [                 # audit log
        {ts, from_state, to_state, actor: "producer"|"user:<id>"|"timer", reason?}
    ]

    # Cross-links
    motivated_proposals: [str]       # RSI proposal ids motivated by this signal
                                     # (denormalized; Proposal.motivating_signals is canonical)

    # Delivery
    deliveries: [                    # one entry per send attempt
        {channel, ts, suppressed_reason?}
    ]
```

The `signature` field is the dedup key — producers don't write directly, they call `signals.observe(signature, ...)` and the helper either creates a new Signal or updates an existing active one with the same signature. This means a daily-firing condition like "cost spike on bot X" produces one long-lived Signal that gets bumped each day, not 30 separate Signals.

The `state_history` field is the audit log. Every state transition is recorded with actor and reason — important for the feedback loop with proposals, where "this proposal was rejected because the originating signal was a false positive" needs to be traceable.

`acknowledged` was considered as a fifth state (between firing and resolved, meaning "I see this, working on it") and dropped for v1 to keep the UI simple — snooze covers the same ground with less surface area.

## 5. State machine

```
              create
                │
                ▼
          ┌──────────┐
          │  firing  │◄────── timer fires (snoozed_until reached)
          └──────────┘
               │  │  │
       snooze ─┘  │  └─► dismiss   (user: "false positive / not actionable")
                  │
                  └─► resolve   (producer auto OR user manual)
                       │
                       ▼
                  ┌──────────┐
                  │ resolved │   ← terminal (producer: condition cleared)
                  └──────────┘
                  ┌──────────┐
                  │dismissed │   ← terminal (user: "don't tell me again")
                  └──────────┘
```

Transitions:
- `firing → snoozed` (user, with `snoozed_until`)
- `firing → resolved` (producer auto-resolve, or user manual)
- `firing → dismissed` (user, false positive — feedback signal for tuning)
- `snoozed → firing` (timer expires)
- `snoozed → resolved` (producer auto-resolve while snoozed)

Dismissed is distinct from resolved: resolved means "the condition cleared," dismissed means "the user said don't show this again." The distinction matters for the proposal feedback loop (§7).

## 6. Find-or-create, dedup, and auto-resolve

Producers don't write Signals directly. They call:

```python
store.observe(signature, payload) → Signal
```

Logic:
- An active Signal with that signature exists → bump `last_observed_at`, `observation_count`, merge new fields into `details`, return existing.
- No active Signal, but a recently-resolved one exists (within a configurable re-open window, default 1h) → re-open it (`resolved → firing`), preserving history. This avoids fragmenting a flapping condition into many separate Signals.
- Otherwise → create new firing Signal.

For comprehensive sweep-style monitors (pod_report, audit) that compute all their findings every run, the helper offers:

```python
store.sweep_resolve(producer, run_signatures)
```

At end of run, any active Signal from that producer not in `run_signatures` auto-resolves. This is how a cost-spike signal goes away on its own once the cost returns to baseline — no producer code needed beyond passing the right signatures into the sweep.

Event-style monitors (watchdog, error_reporter) skip sweep and rely on TTL or manual resolution.

## 7. Storage layout

Mirrors the existing arbiter pattern (state field on JSON is authoritative; subdir is a physical index for efficient iteration):

```
{shared_dir}/signals/
├── firing/<id>.json              # state = firing
├── snoozed/<id>.json             # state = snoozed
├── archived/<id>.json            # state ∈ resolved | dismissed
├── signature_index.json          # signature → active signal id (for find-or-create)
├── feedback.jsonl                # rejected-proposal feedback (see §9)
└── log/<YYYY-MM-DD>.jsonl        # append-only state-change log
```

Owned by the `evolve` user. `shared_dir` already has the right ACL — no `/tmp` staging or sudo dance needed (this contrasts with bot-owned config writes; see CLAUDE.md). Atomic writes via temp-file + rename, same convention as `arbiter.store`.

Helper API:
```python
signals.observe(signature, ...)           # find-or-create
signals.iter_active(filter=...)           # firing | snoozed
signals.find(id)
signals.transition(id, new_state, actor, reason?)
signals.sweep_resolve(producer, kept_signatures)
```

Retention:
- Active states: kept indefinitely until terminal.
- `archived/`: 90 days, then prune to log only.
- `log/<YYYY-MM-DD>.jsonl`: 1 year, rolling.
- `signature_index.json`: rebuilt from active dirs on startup; kept current via writes.

## 8. The two display patterns

Same store, two ways of reading it.

### Consolidated Alerts page (triage)

Replaces today's "Reports" tab. Sub-tabs:

- **Activity** — `state ∈ firing|snoozed AND flavor=activity`. Default sort: severity desc, age desc. Per-row: snooze (24h, 7d, custom), dismiss, drill-in.
- **Maintenance** — `state ∈ firing|snoozed AND flavor=maintenance`. Same affordances. This is the "task inbox."
- **Schedule** — digest cadence config. Empty-but-green digest still sends (preserves heartbeat).
- **Thresholds** — the v2 tuning knobs (cost spike factor, session drop factor, etc.). The stale "Per-bot overrides available below" header gets dropped — v2 baselines are already per-bot so the override is a no-op.

The Alerts page is the entry point when the sysadmin doesn't know what's wrong yet. It's also where snooze/dismiss happen — those affordances aren't replicated on every contextual surface.

### Contextual badges (in-place)

Feature pages query the Signal store filtered by scope:

- **Integrations & Keys** — `signals.iter_active(scope="integration", integration_id=X)` shows a badge per integration with title + severity.
- **Cost Measures** — cost-spike signals on the relevant bot row.
- **Security** — audit findings inline next to identity / config / machine sections.
- **Bot detail pages** — per-bot signals (cost, sessions, health) inline.

Contextual displays are *thin*: a small badge or short notice with title + severity + "see in Alerts →" link. They don't reimplement snooze or dismiss. If you want to act on the signal, you click through.

This means: one source of truth, two views, no sync problem. Contextual pages don't store any state of their own about signals; they just read.

## 9. The RSI link

`Proposal.motivating_signals: list[str]` — required field on new proposals. A proposal must trace to at least one motivating Signal. (This is the load-bearing concept that makes Phase 4 the first migration phase: if the link mechanic doesn't fit, we want to find out before migrating any monitors.)

The link is bidirectional in the UI:
- Signal detail shows "→ N proposals" with deep links to each proposal.
- Proposal detail shows "motivated by N signals" with deep links to each signal.

The denormalized `Signal.motivated_proposals[]` field is updated whenever a Proposal is written or its `motivating_signals[]` changes. The Proposal field is canonical; the Signal field is for display efficiency.

### Feedback loop

When a Proposal is rejected with a reason that implicates the originating Signal — `false_positive`, `bad_inference`, `not_actionable` — the rejection writes to `signals/feedback.jsonl`:

```json
{
  "ts": "2026-05-07T12:34:56+00:00",
  "signal_id": "abc-123",
  "signal_signature": "pod_report:cost_spike:admin-bot",
  "proposal_id": "prop-456",
  "verdict": "false_positive",
  "note": "Daily routine — not a spike, this is normal traffic on Mondays"
}
```

Producers can read this stream to tune their detection thresholds. A producer that reads its own feedback and adjusts (e.g. raises the cost-spike factor on the offending bot, or learns a day-of-week baseline) closes the loop — bad signals don't keep firing forever just because the producer is dumb.

This is the credibility test for the architecture: "track the source of the issue so if it's a bad proposal we can correct the signal detection and analysis." If the feedback loop is real, the system gets smarter over time.

## 10. Migration plan

Six phases. Each ends at a state that's stable on its own — pause-able at any boundary.

### Phase 0 — Foundation (invisible)

- `Signal` dataclass + helpers (`observe`, `iter_active`, `find`, `transition`, `sweep_resolve`).
- Store layout (`signals/firing/`, `snoozed/`, `archived/`, `signature_index.json`, `log/<date>.jsonl`).
- `/api/signals` read routes (list with filters, get by id, history view).
- Unit tests covering find-or-create, sweep-resolve, state transitions, snooze-timer wakeup, signature dedup.

**Exit gate:** can write a fake Signal via REPL, see it through the API, transition it, see it archive after retention. No UI yet.

### Phase 4 — RSI link (FIRST)

Phase 4 runs before any monitor migration because the motivating-signals link is the load-bearing concept; better to derisk early than discover late that it doesn't fit.

- Add `motivating_signals: list[str]` to `Proposal` schema. Required on new proposals; existing ones keep empty list (backfill not attempted).
- Generators populate `motivating_signals[]` when creating Proposals. Each generator decides what counts as motivating — for now, a Signal id from an in-memory test fixture is sufficient until real Signals exist (Phase 1).
- UI: Signal detail shows "→ N proposals", Proposal detail shows "motivated by N signals" with deep links both ways.
- `signals/feedback.jsonl` write path: rejected proposal writes a feedback record with verdict + note.
- Generator unit-test reads feedback.jsonl and demonstrates threshold tuning (concrete proof the loop closes).

**Exit gate:** reject a proposal in the UI with verdict=false_positive, see the originating signal flagged in the feedback log, see a generator (in unit test) read the feedback log and adjust.

### Phase 1 — Watchdog migration (proof point)

Watchdog is the right first cut: its events surface (12 distinct event types persisted to `{shared_dir}/watchdog/<date>.jsonl`) had no Alerts-style UI consumer. Lighting them up demonstrates the full pipeline.

- **Dual-write, not move.** `events.write_events` keeps appending to JSONL (so existing dedup readers — `heal.py`, `test_runner.py` — keep working) AND calls `signals.observe()` for each event with the right flavor + producer attribution. Watchdog stays in `generators/` because it does emit Proposals.
- Each watchdog event_type maps to a flavor (Activity vs. Maintenance) and a producer name (`evolve_watchdog` for meta-layer types, `sysadmin_watchdog` for gateway/config, `test_runner`). Mapping table lives in `events.py`.
- Watchdog Proposals populate `motivating_signals[]` via `signal_id_for_event()` lookup against the dual-written Signal.
- Backfill: `signals/backfill.py` replays existing watchdog JSONL into the Signal store as resolved/historical, idempotent by signature.
- New **Alerts page** in admin UI renders Signals via `/api/signals` with Activity / Maintenance / History sub-tabs. Snooze, dismiss, resolve buttons hit the new mutation endpoints.
- Old `/api/arbiter/health/watchdog-events` keeps returning JSONL data with a `Deprecation: true` response header and a `Link: ...; rel="successor-version"` pointing at the new endpoint. Spec previously said "redirects" — pragmatically the response shapes differ, so we deprecate-in-place rather than HTTP-redirect.

**Exit gate:** Alerts page shows real watchdog Signals, snooze persists across reloads, dismissed Signals don't get re-opened by repeated observation (a fresh detection becomes its own signal — see §6).

### Phase 2 — pod_report cutover (visible win)

The user-facing rename and the biggest behavior change in one shot.

- `pod_report.run_report()` emits Signals (one per item in `broken[]`, `trending[]`, `queue[]`) via `observe()`. Stops writing report artifacts.
- The scheduled "Daily" delivery becomes a *digest* that reads the last N hours of Signals from the store. Empty-but-green digest still sends — preserves the heartbeat.
- "Reports" tab → "Alerts" tab. Sub-tabs: Activity / Maintenance / Schedule / Thresholds.
- Drop the stale "Per-bot overrides available below" header.
- Old `/api/reports-alerts/*` routes redirect or 410.
- Backwards-compat: Telegram digest payload format unchanged, just sourced from new store.

**Exit gate:** Reports tab is gone, Alerts tab works, daily digest still arrives in Telegram, threshold tuning still works, the existing "RSI: 2 proposals pending" status line shows up under Activity with snooze working.

### Phase 3 — Audit cluster

Collapses three scattered surfaces into one.

- `audit.py` emits Signals for CRITICAL / identity / config / machine findings.
- Three `/api/security/*-status` pages now render contextual badges from the Signal store.
- Telegram audit delivery becomes a delivery channel attached to the Signal (single delivery path, not two).

**Exit gate:** audit findings appear in Maintenance lane and as inline badges on the security pages; Telegram still fires for CRITICAL.

### Phase 5 — Long-tail monitors

Each is small; parallelizable; pick the order by user value. Recommended starts: `error_reporter` (most visible) and `host_health` (currently invisible — finally has a UI surface).

| Monitor | What lights up |
|---|---|
| `error_reporter` (Errors banner) | Banner becomes a filtered Signal view with consistent dismiss |
| `host_health` (CPU/disk/mem) | Currently invisible — finally has a UI surface |
| `integration_probe` | Badges next to integrations on Integrations & Keys page |
| `pod_health` | Maintenance signals + inline fix-it links |
| `security_warden` conduct-violation | Inline on security page + Maintenance lane |
| `heartbeat` should-alert | Becomes a Signal type rather than a check endpoint |

**Exit gate:** all monitors in the inventory write to the Signal store; no monitor still has its own scattered storage path.

### Phase 6 — Cleanup

- Delete old endpoints (`/api/reports-alerts/*`, `/api/arbiter/health/watchdog-events`, the redundant `/api/security/*-status` if subsumed).
- Delete redundant detection: `pod_report:gateways-down` vs. `watchdog:gateway_instability` — pick the better one and retire the other. (Writing the same condition twice into the Signal store would dedup via signature anyway, but the duplicate compute is waste.)
- Retention/archive automation cron (90-day archive prune, 1-year log roll).
- Update CLAUDE.md with the Signal store layout next to the existing arbiter store docs.

**Exit gate:** no dead code paths, one storage path per monitor concern, CLAUDE.md updated.

## 11. Open questions

1. **Promotion of Activity → Maintenance.** When does a long-running Activity signal get promoted to Maintenance? (Seven days unresolved? User-flagged? Producer-decided?) Default behavior in v1: never auto-promote — producers can re-emit with flavor=maintenance if they want, but there's no automatic escalation. Revisit if Activity signals routinely sit unresolved.

2. **Re-open window per producer.** Default 1h is suggested in §6. Gateway flapping might want 24h so the same incident isn't fragmented. Worth making configurable per producer? Probably yes; set defaults conservatively in v1 and tune from feedback.

3. **Signal feedback ingestion.** The producers that read `feedback.jsonl` and tune themselves — does that happen in the producer code on next run, or is there a separate "tuner" job? Lean: in producer code on next run, simplest. Open if a producer is too lazy a place for tuning logic.

4. **Severity calibration across producers.** "Warn" from one producer should mean roughly the same urgency as "warn" from another. There's no calibration system in v1 — each producer picks its own thresholds. This will become a meta-monitoring concern (a watchdog *over* the alert system itself) that we'll only need once we have enough signal volume to notice misalignment.

## Appendix: monitor inventory (2026-05-07)

The 28 alert-like signal sources mapped to their proposed flavor and migration phase. This is the input to Phase 5 ordering.

| # | Signal source | Producer | Flavor | Phase |
|---|---|---|---|---|
| 1 | cost-spike | pod_report | activity | 2 |
| 2 | session-drop | pod_report | activity | 2 |
| 3 | gateways-down | pod_report | maintenance | 2 (retire in 6 — overlap with watchdog) |
| 4 | audit-stale | pod_report | maintenance | 2 |
| 5 | queue (RSI pending) | pod_report | activity | 2 |
| 6 | proposal_volume_deviation | watchdog | activity | 1 |
| 7 | auto_revert_rate_spike | watchdog | activity | 1 |
| 8 | rejection_rate_spike | watchdog | activity | 1 |
| 9 | verification_reliability_drop | watchdog | activity | 1 |
| 10 | calibration_drift | watchdog | activity | 1 |
| 11 | generator_dominance | watchdog | activity | 1 |
| 12 | observation_extraction_drift | watchdog | activity | 1 |
| 13 | meta_layer_cost_spike | watchdog | activity | 1 |
| 14 | gateway_instability | watchdog | maintenance | 1 |
| 15 | config_drift_unexplained | watchdog | maintenance | 1 |
| 16 | test_failure_pattern | watchdog | maintenance | 1 |
| 17 | CRITICAL findings | audit | maintenance | 3 |
| 18 | identity findings | audit | maintenance | 3 |
| 19 | config findings | audit | maintenance | 3 |
| 20 | machine findings | audit | maintenance | 3 |
| 21 | conduct-violation | security_warden | maintenance | 5 |
| 22 | pod health checks | pod_health | maintenance | 5 |
| 23 | host health (CPU/disk/mem) | host_health | maintenance | 5 |
| 24 | error banner | error_reporter | maintenance | 5 |
| 25 | heartbeat should-alert | heartbeat | maintenance | 5 |
| 26 | gateway status (per-bot) | gateway probe | maintenance | (subsumed by watchdog gateway_instability) |
| 27 | integration probe errors | integration_probe | maintenance | 5 |
| 28 | security audit refresh progress | security audit | (not an alert — job progress) | — |
