# Smarter generators — design spec

**Status:** Draft. Pre-implementation; awaiting design approval before any code lands.

**Date:** 2026-05-28.

**Origin:** Security-Bot self-diagnosed a multi-week cost issue on 2026-05-28 in two prompts — bloated `.openclaw/workspace/memory/*.md` files (e.g. `2026-05-02.md` at 196 KB) being repeatedly written into the cache envelope on every Haiku heartbeat. The data Security-Bot used (per-call ledger, cache-write tokens, file sizes) was sitting in the per-call JSONL and on disk the whole time; Evolve never asked the question. The user framed this as the *exact* class of thing the system was meant to catch.

**Adjacent:**

- [feedback_generators_consider_intent](../memory/feedback_generators_consider_intent.md) — the same shape, applied earlier to `auth_drift_filler`. Context-free baseline checks fire contextually-wrong proposals.
- [feedback_distinguish_tooling_failure_from_findings](../memory/feedback_distinguish_tooling_failure_from_findings.md) — tri-state status; this spec extends the principle to generators.
- [project_cost_alerting_blackout_2026_05_20](../memory/project_cost_alerting_blackout_2026_05_20.md) — prior cost-monitoring incident; that pass shipped spike/burst detection. This pass adds *slow-creep* and *efficiency-per-call* coverage on top.
- [project_safety_nets_shipped_2026_05_23](../memory/project_safety_nets_shipped_2026_05_23.md) — daily_cap auto-trip + heartbeat-session-bloat. Same family, but absolute-spend gates; slow creep stays under them.
- [feedback_rsi_low_cost_preference](../memory/feedback_rsi_low_cost_preference.md) — generators/monitors default to pure Python; LLM is escalation, not default. This spec follows that constraint.
- [feedback_diagnosis_must_survive_live_inspection](../memory/feedback_diagnosis_must_survive_live_inspection.md) — investigation outputs must reference live system state, not memory or guesses.
- [docs/spec-rsi-architecture-2026-04-17.md](spec-rsi-architecture-2026-04-17.md) — the generator/monitor model this spec extends.

---

## Problem

Three problems compound:

**1. Coverage gaps in existing detectors.** A lot of cost-watchdog detection exists ([cost_watchdog.py](../packages/analyzer/cost_watchdog.py) — `detect_cost_spike`, `detect_context_bloat`, `detect_session_token_outlier`, `detect_heartbeat_session_bloat`, `detect_automation_dominance`, etc.). But Security-Bot's bloat *was* `detect_context_bloat`'s job and the function silently missed it — [`workspace_md_sizes`](../packages/analyzer/cost_watchdog.py:250) scans top-level `.openclaw/workspace/` only, and Security-Bot's bloated files live in `workspace/memory/` (a subdir). The detector returned an empty dict, the signal never fired, and the issue rode under the per-day cap for weeks.

**2. No slow-creep family of detectors.** Every cost detector we have fires on *change*: spike vs prior period, magnitude over absolute floor, session bloat within a session. A bot that quietly drifts from $0.10/heartbeat to $0.40/heartbeat over six weeks — *because cache write volume tripled* — never crosses any threshold we measure. Security-Bot's signature was "cost-per-call drifting up on Haiku at a baseline that *should* be near-flat."

**3. Generators stop one step short of root cause.** `budget_hawk.forensics` and `efficiency_hawk.cost_efficiency` each carry one or two domain-specific investigation hops (summarizer-on-trivial, classifier-noise, background-dominance, tier-misrouting). But the *pattern* — fetch evidence beyond the firing signal, attribute root cause, propose against the cause not the symptom — isn't a system property. Most generators either propose a baseline restore (auth_drift_filler shape) or pass through a Signal as an Investigation Proposal (`cost_spike` shape) without forensics. When Pod-Admin asks "why is Security-Bot expensive?", the generators don't ask "what's in the cache envelope?" — they propose lowering the heartbeat cadence.

The Security-Bot case sits at the intersection: a missing data point (subdir not scanned) compounded by a missing detector family (slow creep) compounded by a generator that, even if it had fired, would have proposed the symptom fix.

---

## Principle

**Generators investigate before proposing. The system gathers more of the data we already produce, and uses it to attribute cause rather than restore baseline.**

Three sub-principles:

1. **Detection is cheap; investigation is moderately cheap; LLM is escalation.** Pure-Python detectors gather facts; pure-Python attribution rules name the most-likely cause; LLM only enters when attribution rules deadlock or operator-facing prose is needed. Aligns with [feedback_rsi_low_cost_preference](../memory/feedback_rsi_low_cost_preference.md).

2. **Generators produce one of three outputs: a targeted action proposal, an investigation proposal (with collected evidence), or no-op-with-trail.** No more "drift detected → restore baseline" loops. Every proposal carries a `root_cause_attribution` block visible to the operator.

3. **Coverage is a property of the producer set, not of any one detector.** Adding detectors is the cheap path. Adding generators that consume *cross-detector evidence* is the multiplier. The system should make both easy.

---

## Inventory: what we already have

Worth being honest about so the spec doesn't re-invent.

### Detectors in `cost_watchdog.py`

| Detector | Scope | Notes |
|---|---|---|
| `detect_daily_spend` | per-bot daily $ cap | absolute floor |
| `detect_cost_spike` | 7d vs prior-7d multiplier | spike-shape |
| `detect_maintenance_ratio_high` | per-day maintenance/total | from rollups |
| `detect_automation_dominance` | background trigger_kind share | spike-shape, share-floor |
| `detect_cron_wakes_agent` | cron schedule analysis | config-smell |
| `detect_cron_overactive` | cron call frequency | rate-based |
| `detect_context_bloat` | workspace/*.md sizes | **misses subdirs — see Gap A** |
| `detect_session_token_outlier` | per-session token max vs baseline | spike-shape |
| `detect_heartbeat_no_model_override` | openclaw.json config smell | static config |
| `detect_heartbeat_session_bloat` | within-session turn growth | within-session, not cross-session |
| `detect_model_override_violated` | model used vs configured | config compliance |

### Generators consuming Signals

| Generator | Family | Investigation depth |
|---|---|---|
| `cost_spike` | optimizer | passthrough → Investigation proposal (no forensics) |
| `budget_hawk` | guardian | forensics.py: 3 cause-attribution detectors |
| `efficiency_hawk` | optimizer | cost_efficiency.py: 2 cause-attribution detectors |
| `auth_drift_filler` | reflex | baseline-restore (no investigation) — known anti-pattern |
| `cache_ttl_tuner`, `cron_caps_filler` | reflex | suspected same anti-pattern; not yet audited |
| Several others | various | mixed |

### Data sources available but underused

- **Per-call cost JSONL** — has cache_read_tokens, cache_write_tokens, input_tokens, output_tokens, model, trigger_kind, session_id, timestamp. Today driven mostly by aggregations (daily totals, week-over-week, session sums). Not analyzed as a per-call efficiency stream.
- **Session_summary records** — session_class, turn_count, total_cost. Used by tier-misrouting and bloat detectors; not used for cost-per-class baselines.
- **Observation tuples** — (noun × verb × mood × engagement) per bot per day. Used by efficiency_hawk's cluster engine; not joined with cost.
- **Bot config + manifest + audit history** — used as inputs but not as *peer-comparison* inputs (i.e. "is security-bot's heartbeat cost normal for an auditor-class bot?").
- **Signal history** — firing/snoozed/archived states per signature. Not currently consulted by generators to ask "has this been investigated before? what did the operator do last time?"
- **Proposal feedback (`signals/feedback.jsonl`)** — rejected-proposal reasons. Designed as signal-tuning input; should also be consulted by generators to suppress proposals the operator has already declined for the same reason.

---

## Gaps surfaced by the Security-Bot case

Five concrete gaps, in implementation-cost order (cheapest first):

### Gap A — `workspace_md_sizes` doesn't recurse

[`workspace_md_sizes`](../packages/analyzer/cost_watchdog.py:250) scans `.openclaw/workspace/` top-level only. Security-Bot's bloat lives at `.openclaw/workspace/memory/*.md`. Fix: scan a known-OC-subdir allowlist (`memory/`, `journal/`, `memory/journal/`) one level deep. Hold the "don't recurse" stance for unknown subdirs (user archives, app data) — recursing arbitrarily produces noise on Team-Bot-B's RUN_LOG-style directories.

### Gap B — no per-file growth-rate detector

`detect_context_bloat` fires on *current size > threshold*. A file growing 20 KB/day for a month never crosses the floor in one shot, but the *trajectory* is the operator-actionable signal. Add `detect_workspace_growth_rate`: 7d size delta per file, fire on > threshold growth/day regardless of absolute size. Same data source, second axis.

### Gap C — no per-call efficiency baseline

The most diagnostic single metric for Security-Bot was *cost-per-Haiku-call*: Haiku calls *should* be near-free, Security-Bot's were ~$0.07 because of cache writes. Add `detect_efficiency_drift`: rolling-7d cost-per-call by `(bot_id, model_tier)` compared to rolling-28d baseline; fire when current is N× baseline AND absolute call volume is above floor. Pure aggregation over existing per-call records. Critically: scope is *cost-per-call*, not cost-total, so a quiet bot whose call shape changes is detectable.

### Gap D — no cache-write-volume detector

Cache writes are the most direct proxy for "context envelope being shoved in." Add `detect_cache_write_volume`: rolling-7d cache_write_tokens per call by bot; fire on > N×baseline. Distinct from Gap C because it isolates the bloat mechanism (envelope size) from confounders (output length, model swap). When B and D both fire on the same bot, the attribution is automatic: "this file grew and your cache envelope grew with it."

### Gap E — no cross-bot peer comparison

Security-Bot, team-bot-a, and team-bot-b are all auditor-class bots running Haiku heartbeats. Security-Bot's cost-per-call should land within a band of team-bot-a's and team-bot-b's. If it's an outlier, that's a stronger signal than any single-bot threshold. Add a `peer_baseline` resolver on top of existing metrics; generators reference it for "is this normal for bots in the same role-class?" Implementation reads bot role from `network.json::pod.bots[].role` (or derives from primary-model + cadence shape if role isn't set yet).

### (Not a gap, but worth noting)

The `detect_heartbeat_session_bloat` detector exists and is correct — it catches *within-session* growth from the OC sub-runs issue, not *cross-session* context envelope size. Don't conflate them when adding C/D.

---

## The generator architecture upgrade

The detector additions (A–E) get us the data. The generators need to *use* it well.

### Today: most generators are Detect → Propose

```
Signal fires → Generator wraps Signal in a Proposal → Operator sees "Security-Bot is expensive"
                                                          → Operator does the investigation
```

### Proposed: Detect → Investigate → Attribute → Propose

```
Signal fires → Generator's investigate() step gathers correlated evidence
            → Generator's attribute() step names most-likely cause
            → Generator's propose() step targets the *cause*, not the symptom
            → Proposal carries a root_cause_attribution block
                ├── primary_signal (the firing one)
                ├── correlated_signals (other firing signals on same bot/timeframe)
                ├── evidence (file sizes, recent edits, peer comparisons, config_intent reads)
                ├── attribution (rule that named the cause, or "ambiguous")
                └── confidence (high/medium/low — from rule strength + evidence completeness)
```

### What "investigate" is

A bounded set of cheap lookups, all pure-Python. Each generator declares which it needs in its `charter.yaml`; the runner provides them on the context.

Standard investigation toolkit (shared, lives in `packages/analyzer/investigation/`):

| Tool | Purpose | Cost |
|---|---|---|
| `correlated_signals(bot_id, since)` | Other Signals firing on same bot in same window | dict read |
| `recent_config_changes(bot_id, paths, since)` | git/mtime check on bot config & workspace files | stat() calls |
| `config_intent(bot_id, key)` | Read `_intent_memory.py` annotations | file read |
| `peer_baseline(metric, role_class)` | Same-role-class metric distribution | aggregate |
| `proposal_history(bot_id, signature)` | Past proposals + operator decisions for this signature | signature_index lookup |
| `rejection_history(bot_id, signature)` | Feedback.jsonl entries matching signature | scan |
| `file_top_contributors(bot_id, dir, n)` | Largest files in dir, sorted | listdir + stat |
| `time_series(metric, bot_id, days)` | Daily rollup of a metric | rollup read |

Existing generator-specific forensics (`budget_hawk.forensics`, `efficiency_hawk.cost_efficiency`) migrate to this shape over time. They don't need to break — the toolkit makes the *next* generator easier, then displaces the per-generator one when convenient.

### What "attribute" is

A short ordered list of attribution rules per generator. Each rule is a pure function `(evidence) → AttributionResult | None`. First match wins. Final fallback is `("ambiguous", evidence)` — the proposal becomes an Investigation Proposal carrying the gathered evidence for the operator to interpret.

Attribution rules are *narrow on purpose*. Each one encodes "if signals X and Y both fire and evidence Z is present, the cause is almost certainly W." When a rule fires, the proposal targets W. When none does, the operator sees the evidence and decides. This is the [feedback_distinguish_tooling_failure_from_findings](../memory/feedback_distinguish_tooling_failure_from_findings.md) discipline applied to causes: "we don't know" is a first-class output.

### What "propose" produces

Two-tier outputs:

- **Action proposal** — when attribution is confident AND a safe action is available. Includes a `targets_cause: <attribution_key>` field so retrospectives can verify the action addressed the named cause.
- **Investigation proposal** — when attribution is ambiguous OR no safe action exists for the named cause. Includes the gathered evidence in the proposal body. Critically: an investigation proposal is *not* a placeholder. It's a structured snapshot of "here's everything we know, here's what's suspicious, here's where to look."

The third output — **no-op-with-trail** — is when investigation finds the firing signal is explained by recorded intent (e.g. operator explicitly set this config). The generator writes a record (so the calibration system sees the suppression) but no proposal lands.

---

## Concrete additions, ordered

### Phase 1 — coverage fixes (≈1 PR each, small)

1. **Gap A**: Extend `workspace_md_sizes` to scan known OC subdirs. Add `memory/` and `memory/journal/` to the scan set. Test with security-bot's actual file layout.
2. **Gap B**: `detect_workspace_growth_rate` — 7d size delta per file. Lives in `cost_watchdog.py` next to the other size detectors. New Signal type `workspace_growth`.
3. **Gap C**: `detect_efficiency_drift` — per-(bot, model) cost-per-call rolling baseline. Lives in `cost_watchdog.py`. New Signal type `efficiency_drift`.
4. **Gap D**: `detect_cache_write_volume` — per-bot cache_write_tokens-per-call rolling baseline. Lives in `cost_watchdog.py`. New Signal type `cache_envelope_growth`.

These are pure additions to existing producer modules. Each is verifiable end-to-end on the current pod (we know security-bot should fire on A/B/D; we know team-bot-a should fire on cron-frequency from Security-Bot's transcript).

### Phase 2 — investigation toolkit + first generator migration

5. **Investigation toolkit** as `packages/analyzer/investigation/`. Initial tool set: `correlated_signals`, `recent_config_changes`, `config_intent`, `file_top_contributors`, `time_series`. (`peer_baseline`, `proposal_history`, `rejection_history` land in Phase 3.)
6. **`bloat_investigator` generator** — consumes `context_bloat`, `workspace_growth`, `cache_envelope_growth` Signals. Attribution rules:
   - All three firing on same bot, same files → "growing memory dir, heartbeat reads them, cache envelope bloating"; action: rotate-memory-dir proposal (operator-confirmed L2 applier).
   - `context_bloat` + `cache_envelope_growth` without growth-rate → "memory dir is stable but oversized"; action: trim/rotate suggestion (operator-confirmed).
   - `efficiency_drift` without `cache_envelope_growth` → "cost-per-call up but envelope flat — model swap or output length change"; investigation proposal with the evidence.
   - Otherwise → investigation proposal listing the firing signals + file_top_contributors.

This generator is the *reference implementation* of the new shape. It uses no LLM; all attribution is pure rules over the gathered evidence. If `bloat_investigator` lands and works on the Security-Bot case retroactively, the pattern is validated.

### Phase 3 — broader generator audit + cross-cutting tools

7. Audit existing generators against the three-output shape. Known suspects (from [feedback_generators_consider_intent](../memory/feedback_generators_consider_intent.md)): `auth_drift_filler`, `cache_ttl_tuner`, `cron_caps_filler`. Each gets a charter update + an investigate step + an attribution rule list. Reflexes that genuinely don't need investigation (e.g. fix-broken-file-permissions class) stay as-is, but they get a `kind: reflex` marker so the audit pass is visibly complete.
8. **`peer_baseline` resolver** — same-role-class metric distribution. Requires `network.json::pod.bots[].role` to be filled in for the pod (small one-time write).
9. **`proposal_history` + `rejection_history` lookups** — generators consult these in their investigate step. Suppresses repeat proposals of things the operator already declined for the same reason.
10. **Calibration loop** — when an action proposal lands and the named cause is verifiable post-application (e.g. metric returns to baseline after the action), record success against the attribution rule. Failed attributions get demoted in rule order; consistently-firing rules get promoted. Builds on existing `calibration.py`.

### Phase 4 — operator surface

11. Alerts page shows the `root_cause_attribution` block on every proposal — primary signal, correlated signals (as chips), confidence, and the rule that named the cause. Make the *reasoning* visible. This is the [feedback_safety_summary_less_useful_than_audit](../memory/feedback_safety_summary_less_useful_than_audit.md) lesson: structured evidence beats prose.
12. Generator detail view shows per-rule attribution success rate (from Phase 3 calibration loop). When a rule consistently mis-attributes, it's visible.

---

## What this is *not*

- **Not an LLM-driven generator.** Investigation is rule-based. LLM enters only at Phase 4-ish, for synthesizing operator-facing prose around the structured attribution — and only after structured rules have run.
- **Not a refactor of existing generators that work.** `budget_hawk.forensics` and `efficiency_hawk.cost_efficiency` already do the shape; Phase 3 migrates their internals to the shared toolkit *when convenient*, not as a blocking step.
- **Not a new applier.** All action proposals route through existing L1/L2 appliers; no new write paths. New Signal types are just Signals.
- **Not a new monitor entry point.** Detection happens inside `cost_watchdog.py` and friends, same as today. Producer modules grow detector functions; they don't fork.

---

## Acceptance criteria

A second Security-Bot-shape event must:

1. Fire `context_bloat` on the actual bloated files (Gap A).
2. Fire `workspace_growth` while the trajectory is still under the absolute threshold (Gap B).
3. Fire `cache_envelope_growth` weeks before the operator notices spend (Gap D).
4. Trigger `bloat_investigator` with attribution `"growing memory dir, heartbeat reads them"`.
5. Produce a proposal that names the *files* and proposes rotation, not "lower the heartbeat cadence."
6. Carry a `root_cause_attribution` block that an operator can audit.
7. The applier path for the rotation action is an existing L2 file write, not a new mechanism.

Backtested on Security-Bot's actual data (May 21 worst day), all six should hold.

---

## Open questions

- **Should `peer_baseline` require operator role tagging on bots, or infer role from primary-model + cron-shape + heartbeat-cadence?** Inferred role is more robust but might mis-classify edge cases. Probably: infer with manual override.
- **How often should the slow-creep detectors run?** Daily seems right (matches `budget_hawk`'s hourly + `cost_spike`'s daily). Hourly is wasteful on creep; daily catches anything operator-actionable.
- **Should investigation evidence be cached?** A bloat investigation reads file sizes that change daily. Re-running each generator invocation is fine and avoids cache invalidation bugs.
- **Where do attribution-rule definitions live — code or charter?** Code. Charter holds invariants and toolkit declarations; attribution rules are too coupled to evidence shapes to externalize cleanly. Re-evaluate after Phase 3.
- **Calibration of "rule fired but cause was wrong" — how do we know?** Post-action metric verification: if the cause-targeted action lands and the firing signal stays firing for N more days, the attribution was wrong. Builds on existing claim-metric framework.

---

## Sequence for the user

Phase 1 first — four small PRs, all coverage fixes, all verifiable on the current pod. Phase 2 (toolkit + `bloat_investigator`) is the architectural payoff; everything after is the same shape applied broader. Stop after Phase 1 if the simpler additions catch enough; the toolkit only pays for itself when there's a second generator that benefits from it. Phase 3's generator audit may surface that some reflexes (auth_drift_filler etc.) need more than a tooling pass — that's a separate spec when we get there.
