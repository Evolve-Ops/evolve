# Principle: Signals Precede Proposals (Monitor → Signal Store → Generator → Proposal)

**Status:** load-bearing architecture principle (not a soft guideline).
**Adopted:** 2026-05-31, consolidating the producer/generator split that has become the pod-wide pattern (see [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) and [spec-app-permission-drift-2026-05-25.md](spec-app-permission-drift-2026-05-25.md)).

---

## The principle, in two clauses

1. **Every condition worth acting on becomes a Signal first.** Monitors (audit, watchdog, host_health, integration_probe, pod_health, security_warden, test_runner, permission_monitor, app_manifest_monitor, etc.) observe state and emit Signals to `{shared_dir}/signals/`. The Signal is the operator-visible artifact: it appears on the Alerts page, it auto-archives when the condition clears, it deduplicates by signature, it carries a stable identity across observations.

2. **Generators consume Signals and emit Proposals.** When a generator decides a Signal is worth acting on (not just visible), it produces a Proposal that links back to the motivating Signal via `Proposal.motivating_signals[]`. The generator never observes pod state directly to produce a Proposal — it always reads from the Signal store. This means anything that triggers a Proposal is, by construction, already visible to the operator before the action lands.

## What this implies in code

Practical translation across the codebase:

### The monitor/generator split is the pod-wide architectural pattern

Every monitor → generator pair on the pod follows this shape (cited at [spec-app-permission-drift-2026-05-25.md:34-36](spec-app-permission-drift-2026-05-25.md:34)). The reference pairs:

| Monitor (producer) | Generator (consumer) | Domain |
|---|---|---|
| `permission_monitor` | `auth_drift_filler` | auth-profile drift |
| `app_manifest_monitor` | `app_permission_drift` generator | app permission drift |
| `pod_report` | (reads signals for daily report) | health rollup |
| `audit`, `watchdog`, `host_health`, `integration_probe`, `pod_health`, `security_warden`, `test_runner` | various generators | observation surface |

Adding a new generator means first specifying its motivating Signal type and either reusing an existing monitor or adding a new one. "Generator that observes directly" is the anti-pattern.

### Signals are written via `signals.store.observe()` and `sweep_resolve()`

Producers call `signals.store.observe(signature=..., type=..., ...)` for find-or-create with dedup; sweep-style monitors additionally call `signals.store.sweep_resolve(producer=..., kept_signatures=...)` so cleared conditions auto-archive. These are the canonical entry points — code that writes Signal JSON files directly is the violation.

### Generators read Signals, not the underlying state

When `auth_drift_filler` decides whether to propose, it reads from `{shared_dir}/signals/firing/` filtered by the relevant Signal type — it does not re-scan `/Users/<bot>/.openclaw/auth-profiles.json` to make the call. This guarantees that the operator can see (on the Alerts page) every condition that could lead to a Proposal.

### The signal-subscriber daemon dispatches generators on Signal arrival

Per CLAUDE.md §"Event-driven generator dispatch", the `signal-subscriber` daemon watches the firing directory at 1 Hz and dispatches subscribed generators when a matching Signal lands (charters declare `subscribes_to: [<type>, ...]`). The daily sweep stays as a safety net; the event-driven path is the fast loop. Both go through the Signal store, preserving the principle.

### `Proposal.motivating_signals[]` is required

Every Proposal carries a list of Signal IDs that motivated it. The Arbiter uses this for dedup (two generators emitting the same proposal merge); the operator uses it to trace "why did this proposal appear" back to a visible Signal. A Proposal with no motivating Signal is either a bug or an exceptional path that should be specced explicitly.

## Anti-patterns to grep for

These are violations:

- A generator that calls `Path(...).read_text()` on bot state to decide whether to propose
- A Proposal emitted with empty `motivating_signals[]`
- A monitor that writes findings to a per-domain location instead of the Signal store
- A new "investigation surface" that reads condition data outside the Signal store
- A reporting daemon that aggregates from per-monitor JSON files instead of `{shared_dir}/signals/`

## What this principle is NOT

- **Not a ban on per-domain stores.** Monitors can persist their own working data (last-scan-results, calibration state, etc.) outside the Signal store. The principle is that *the visible condition*, the thing that motivates downstream action, lives in the Signal store.
- **Not a demand for one Signal per finding.** A single sweep can `observe()` multiple Signals (one per finding) or `sweep_resolve()` to auto-archive cleared ones. The principle is about who writes Signals (producers) and who reads them (generators + UI), not Signal cardinality.
- **Not a claim that every Proposal needs a recent Signal.** Some proposals are seeded by long-standing conditions (e.g., a chronic drift). The Signal can be older than the Proposal; the link is still required.

## Why this matters

Before this principle was codified, generators that observed state directly produced "where did this come from" mystery proposals — the operator saw a proposed change with no preceding visible signal that motivated it. The split solves three problems at once:

1. **Visibility before action.** Every condition that could become a Proposal is, by construction, on the Alerts page first. The operator sees what the pod sees before any proposed change appears.
2. **Dedup is automatic.** Two generators motivated by the same Signal merge their proposals via `motivating_signals[]`. Pre-Signal-store, duplicate-proposal management was per-generator.
3. **Calibration is observable.** A generator's behavior can be tuned by inspecting which Signals it fires on and which it ignores. Pre-Signal-store, generator behavior was opaque to anyone not reading the source.

It also forces the question "is this condition worth surfacing to the operator at all?" at the right time — when the monitor is being written — rather than at proposal-review time when the operator is already confused.

## References

- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the Signal store design (canonical)
- [spec-app-permission-drift-2026-05-25.md](spec-app-permission-drift-2026-05-25.md) — explicit "mirrors the existing producer/generator split" framing
- CLAUDE.md §"Signal store" and §"Event-driven generator dispatch" — operational layout
- [project_alerts_signal_store](memory/project_alerts_signal_store.md) — pod-wide consolidation status (Phase 4 first then 0/1/2/3/5/6)
- [project_better_engine_pipeline_unification](memory/project_better_engine_pipeline_unification.md) — the 6-PR sprint that migrated scoreboard/compliance/suggestions to Signals→Proposals
- `arbiter.store` — the canonical writer/reader for Proposals (which carry `motivating_signals[]`)
- `signals.store` — the canonical writer/reader for Signals
