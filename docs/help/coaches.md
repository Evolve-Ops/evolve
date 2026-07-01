---
title: "Help: Coaches Page"
slug: coaches
audience: public
last_reviewed: 2026-06-06
concepts:
  - coaches
  - generators
  - charters
  - track-record
  - authority-factor
ui_surface: admin.self-improvement   # generators/charters live on the Recommendations page
related_specs: []
---

# Help: Coaches Page

The Coaches page (accessible from the Recommendations page) shows every charter the registry has loaded, plus each one's record: track record, authority factor, current status. Click a row for the charter inspector, recent suggestions, and pause/resume controls.

Coaches are the source of all v2 suggestions. They fall into three types, and the page colors their icons accordingly.

The active portfolio (under `packages/analyzer/generators/`, June 2026): `app_birth_detector`, `app_permission_drift`, `app_permission_review`, `app_suggester`, `auth_drift_filler`, `bloat_investigator`, `bot_config_integrity`, `budget_hawk`, `cache_ttl_tuner`, `cost_root_cause_correlator`, `cost_spike`, `cron_caps_filler`, `efficiency_hawk`, `engagement_amplifier`, `evolve_watchdog`, `exec_outcome_investigator`, `gateway_diagnostician`, `manifest_quality`, `model_discovery`, `persona_tuner`, `pod_capability_lift`, `security_warden`, `session_quality`, `sysadmin_watchdog`, `user_profile_inferrer`, `workspace_inventory`, `workspace_security`. Charters ship in code at `packages/analyzer/generators/<id>/charter.yaml` (what this coach watches for) and are immutable at runtime.

Generators added or retired since May 2026:

- **New (June 2026):** `engagement_amplifier` (pattern-deepening proposals), `pod_capability_lift` (cross-bot capability-gap aggregator).
- **Retired:** `primary_model_floor_advisor` ([PR 2214](https://github.com/evolve-ops/evolve/pull/2214)) — its job is now done by the routing-disagreement audit and the Cost Efficiency Score's tier-routing component. `plugin_curator` (2026-06-06) — the triggers it remediated were retired in the plugin-posture rework ([spec](../spec-plugin-posture-rework-2026-06-06.md)). `test_failure_responder` + `test_gate_backfill` (2026-06-08) — removed with the app-test surface ([decision memo](../decision-app-tests-2026-06-08.md)); their coverage moved to the audit + coherence systems.
- **Investigate-before-propose:** `bloat_investigator`, `exec_outcome_investigator`, and (newly) `cron_caps_filler` ([PR 2319](https://github.com/evolve-ops/evolve/pull/2319)) consult the config_intent record before proposing reverts so deliberate deviations don't generate noise.

---

## Types

| Type | Purpose | How it earns attention |
|------|---------|-----------------------|
| **optimizer** | Proposes improvements — new apps, tighter configs, tone shifts. | Competes on track record × urgency × dimension weight. |
| **guardian** | Runs at duty — checks for problems and annotates other suggestions. | Runs at track record 1.0; doesn't compete for slots. |
| **meta_guardian** | Watches the system itself (volume deviations, check-in drops). | Feeds the Meta-health tab. |

Guardians and meta_guardians don't suffer track record penalties when they "miss" — they run on every cycle regardless. Optimizers are the ones whose track record moves with verified outcomes.

---

## Columns

| Column | Meaning |
|--------|---------|
| Coach | Charter ID. Icon shows type. |
| Dimension | Which quality this coach contributes to (cost, safety, utility, …). |
| Status | **active** (green), **paused** (yellow), **paused for review** (red). |
| Track record | Bounded [0.5, 1.5]. Computed from verified wins and losses: `1.0 + 0.3 × (wins − losses) / n`, clamped. |
| Wins / Losses | Verified successes vs. failures from the check-in. The percentage is the success rate over the verified subset. |
| Emitted | Lifetime suggestions emitted. |
| Last check-in | When the check-in last graded one of this coach's suggestions. |

The table sorts active first, then paused, then paused for review; within a status, highest emissions lead.

**Load errors:** if a charter fails to parse or its fingerprint doesn't match the stored record, a red-bordered error card appears above the table. This usually means the charter (what this coach watches for) changed in code but the record on disk wasn't reconciled; fix it by reviewing the change, then either updating the record manually or reverting the charter.

---

## Inline Pause / Resume on the table

Each row carries an inline Pause or Resume button ([PR 2153](https://github.com/evolve-ops/evolve/pull/2153)) — no need to open the detail modal for the common "this coach is too noisy, mute it" case. Clicking prompts for a reason that's recorded on the coach's track record.

---

## Detail modal

Click a row for:

- **Header** — ID, type pill, dimension pill, cadence (on_demand / hourly / daily / weekly), budget policy (competitive / duty / meta), current status.
- **Purpose line** — the human-readable why from the charter.
- **Stat cards** — track record, emitted, applied, verified ✓/✗, rejected, vetoed.
- **Pause / Resume** — same action as the inline button above; the modal's Pause records a longer reason.
- **Charter invariants** — the machine-checked rules the charter declares. Each is `invariant_id — check_kind: {params}`. These run at ingest: a suggestion that violates an invariant is rejected before it enters the queue.
- **Recent suggestions** — last 10 emitted, with status and urgency badges, newest first.

---

## When to pause a coach

- It's emitting lots of suggestions you keep rejecting (even after the rejection_rate_spike watchdog event). Pausing while you investigate is better than rejecting ten more.
- Its track record is at the floor (0.5) and you're confident the dimension isn't something you want addressed right now.
- You're running an experiment that the coach would interfere with.

When you pause, the reason you enter is stored on the record. Resume the coach to clear the pause.

**Paused for review** is different from paused — it's set by the arbiter's internal checks (fingerprint mismatch, repeated charter violations). It's not a button in the UI; it's a state you read. To exit this state, fix the underlying issue (usually a charter mismatch) and the registry reactivates on next load.

---

## Common questions

**Track record is 1.00 for a coach that's emitted 50 suggestions — why?**
Track record only moves on *verified* outcomes. If the check-in hasn't graded any of this coach's suggestions yet, the denominator is zero and track record stays at its 1.0 default. Check the "Wins / Losses" column: if it reads 0/0, the coach has no verified track record.

**Why doesn't my guardian have variable track record?**
Guardians run at duty (budget_policy = duty). Their track record is fixed at 1.0 — they don't compete for attention, they observe and annotate. Only optimizers move with their track record.

**A coach has a high rejection rate but high track record. How?**
Track record tracks *check-in outcomes*, not human rejections. A coach can suggest things verifiers confirm ("this reduces gateway restarts") even if humans reject most of them as not-worth-it. The two numbers tell different stories; look at both before acting.

**What happens to the coach's code when I pause it?**
Nothing. The charter and code stay on disk. Pausing just flips a flag on the record so the arbiter's `active_generators()` skips it on the next cycle.

**Can I edit a charter from here?**
No — charters are immutable at runtime. Changes ship as code (PR-reviewed). The registry enforces this via the fingerprint check. If you want to change a coach's behavior, edit `packages/analyzer/generators/<id>/charter.yaml` and ship it.
