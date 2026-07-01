# RSI proposal eligibility — what belongs on Recommendations

**Status**: drafted 2026-06-05.
**Scope**: defines which findings deserve to surface as "Recommendations"
(`surface = improvement`) vs. which belong on Alerts (`surface ∈ {firing,
drift, cleanup}`).
**Companion**: [docs/spec-proposal-drafting-protocol-2026-06-04.md] — Phase A
defined *how* a proposal should read; this spec defines *which proposals
belong on the RSI page at all*.

## Why this exists

The 2026-06-04 surface flip (slice 1B of the Recommendations rework) routed
proposals to Alerts vs. Recommendations using `charter.surface`. That was
the right direction but the wrong granularity: a single generator can emit
both genuine RSI proposals *and* anomaly findings from the same module, and
forcing one charter-level surface on both routes one of them wrong.

The screenshot that triggered this spec — `efficiency_hawk
.session_token_outlier`, an anomaly Investigation with the headline *"One
team-bot-a session cost 30.8× the usual amount"* — landed on Recommendations
because `efficiency_hawk` declares `surface: improvement` at the charter
level. The finding itself fails every test of an RSI proposal.

## What "RSI" means here

Recursive Self Improvement on this pod refers to **the engine proposing
material changes to a bot's setup so it can do *more of what it's for*.**
Not anomaly attribution. Not maintenance. Not drift correction. Those are
*operations*; they belong on Alerts. RSI is *improvement*.

A proposal qualifies as RSI iff all four are true:

1. **Forward-looking.** It proposes a change that would enable *more of a
   desired behavior*. Not "explain what happened" — "do this so the bot can
   do *more of X* going forward."
2. **Pattern-grounded.** Derived from a recurring shape across multiple
   sessions, days, or conversations. Not a single event. Not a threshold
   trip on one measurement.
3. **Objective-aware.** References what the bot is *for* — its persona, its
   stated purpose, the user's goals, the workflows that are working. An RSI
   proposal makes contact with intent, not just metrics.
4. **Material.** Would change a substantive aspect of the bot's setup:
   capability (add an app), persona (AGENTS edit), workflow (instruction),
   model-tier choice driven by observed workload. Not a sys-admin cleanup,
   drift revert, or maintenance tweak.

A finding that fails any one of these four lives on Alerts, regardless of
how polished its presentation is.

## Examples

**Passes (belongs on Recommendations)**:

- *"team-bot-a's user has asked about scheduling 12 times in 30 days; no
  calendar capability exists. Consider adding the Google Calendar app."* —
  pattern-grounded, forward-looking, objective-aware, material.
- *"Tone harvest shows persona drifting from `warm` to `terse`; rewrite the
  voice card to match the new norm."* — pattern-grounded, material AGENTS
  edit.
- *"Maintenance sessions are routing to Sonnet (tier_class=primary). 78%
  of these workloads complete on Haiku in dev. Lower the primary floor."*
  — pattern + objective (cost efficiency for the actual workload).

**Fails (belongs on Alerts)**:

- *"One team-bot-a session cost 30.8× the usual amount."* — single event, no
  pattern, no objective, no material change proposed. **Anomaly.**
- *"Cron `evolve-puller` fired 14× in 4h."* — threshold trip on a sys-admin
  control. **Maintenance.**
- *"Authentication profile has drifted from baseline."* — drift correction.
  **Maintenance.**
- *"Workspace contains an orphan manifest."* — hygiene. **Cleanup.**

## Enforcement mechanism

`Proposal.surface: Surface | None = None` — a per-finding override.

The page renderer reads `p.surface or p.charter_surface`. The override
wins when present; absent, the charter's `surface` field applies. This
preserves backward-compat (existing proposals load with `surface = None`
and route by their charter) while letting generators with mixed output
shapes route each finding to the correct page.

A generator that emits only RSI-shaped findings sets nothing extra and
inherits its charter's `surface: improvement`. A generator like
`efficiency_hawk` that emits both RSI findings and anomaly findings sets
`surface="firing"` (or drift/cleanup) inside the anomaly factory functions
and leaves the RSI factories untouched.

## Per-factory audit (2026-06-05)

The audit covers every factory function across the 28 generators with a
`surface:` field set. Each row applies the 4-criteria test to the
factory's typical output. Rows where Verdict ≠ Current need a change.

### Generators currently `surface=improvement` (Recommendations page today)

| Generator | Factory | Trigger | RSI Verdict | Proposed surface | Rationale |
|---|---|---|---|---|---|
| `app_birth_detector` | `_build_spec` | orphan workspace cluster | **RSI** | `improvement` (unchanged) | Pattern (clustered workspace artifacts) → forward-looking proposal to formalize a new app. Material capability change. |
| `app_suggester` | (top-level) | observed capability gap | **RSI** | `improvement` (unchanged) | Pattern (recurring intent without a capability) → propose an app. Objective-aware. |
| `persona_tuner` | `_build_tone_proposal` | tone-cell drift | **RSI** | `improvement` (unchanged) | Pattern (multi-session tone shift) → AGENTS edit. Material persona change. |
| `efficiency_hawk` | `_build_streamline_proposal` (observe.py) | engagement-cluster outlier | **RSI** | `improvement` (unchanged) | Pattern (high engagement-per-session cluster) → AgentsAppend streamline. |
| `efficiency_hawk` | `make_tier_misrouting` | maintenance-on-Sonnet pattern | **RSI** | `improvement` (unchanged) | Pattern (workload class vs. tier choice) → TierAdjustment. Material. |
| `efficiency_hawk` | `make_background_dominance` | background-trigger dominance | **RSI** | `improvement` (unchanged) | Pattern (background turn share) → automation rebalance. Borderline; the Investigation form makes it weaker, but the underlying signal is RSI-shaped. |
| `efficiency_hawk` | `make_daily_spend_proposal` (signal_proposals.py) | DailySpendHighSignal | **Anomaly** | **`firing`** | Threshold trip on one day. No pattern beyond "today crossed line." |
| `efficiency_hawk` | `make_automation_dominance_proposal` (signal_proposals.py) | AutomationDominanceSignal | **RSI** | `improvement` (unchanged) | Signal-path twin of `make_background_dominance`. Pattern, not anomaly. |
| `efficiency_hawk` | `make_cron_wakes_agent_proposal` | CronWakesAgentSignal | **Maintenance** | **`drift`** | Config nit ("cron is wrapped around an agent call"). Sys-admin tweak, not RSI. |
| `efficiency_hawk` | `make_cron_overactive_proposal` | CronOveractiveSignal | **Maintenance** | **`drift`** | Cron threshold trip. Sys-admin observation. |
| `efficiency_hawk` | `make_context_bloat_proposal` | ContextBloatSignal | **Maintenance** | **`firing`** | Memory file grew. Hygiene investigation. |
| `efficiency_hawk` | `make_session_token_outlier_proposal` | SessionTokenOutlierSignal | **Anomaly** | **`firing`** | **The screenshot.** Single session, no pattern, no objective, no material change. |
| `efficiency_hawk` | `make_heartbeat_no_model_override_proposal` | HeartbeatNoOverrideSignal | **Maintenance** | **`drift`** | Config check ("heartbeat lacks model override"). Sys-admin tweak. |

### Generators currently `surface ∈ {firing, drift, cleanup}` — checked for missed RSI

Spot-checked: `budget_hawk.make_tier_downgrade` (TierAdjustment based on
sustained spend pattern) and `primary_model_floor_advisor
._build_tier_adjustment_proposal` (TierAdjustment from workload class
analysis) both *plausibly* pass the RSI test. Leaving on their current
charter surface for this pass; flagging as **Phase 2 upgrade candidates**.
The risk of routing a TierAdjustment to Recommendations when it isn't yet
ranked as a capability suggestion is non-trivial — better to validate the
downgrade direction first and only then consider promotion.

All other Alert-surface generators (auth_drift_filler, bot_config_integrity,
cron_caps_filler, sysadmin_watchdog, security_warden, workspace_inventory,
workspace_security, gateway_diagnostician, exec_outcome_investigator,
cost_root_cause_correlator, cost_spike, test_failure_responder,
test_gate_backfill, app_permission_drift, app_permission_review,
manifest_quality, plugin_curator, evolve_watchdog, session_quality,
bloat_investigator, cache_ttl_tuner) emit drift/cleanup/firing findings
exclusively. No change.

## What this spec does NOT do

- It does **not** build the synthesis layer (pattern miner over
  observation tuples → objective-aware proposal). That's larger work,
  tracked separately as the pattern-mining workstream.
- It does **not** rewrite any existing proposal's content. Phase A's
  operator-first text still applies to every finding, wherever it lands.
- It does **not** retire any generator. The anomaly-shaped factories
  inside `efficiency_hawk` continue producing the same findings; they just
  land on Alerts instead of Recommendations.

## Test rules

- `tests/test_proposal_surface_override.py` — pins that `Proposal.surface`
  serializes round-trip, defaults to None, and accepts the four legal
  values.
- `tests/test_efficiency_hawk_anomaly_surfaces.py` — pins that the six
  anomaly/maintenance factories enumerated above emit proposals with
  `surface != "improvement"`. A regression that drops the override on any
  of these would re-route a session_token_outlier back to Recommendations.
- `tests/test_proposal_payload_includes_override.py` — pins that the
  server endpoint honors `p.surface` over `charter.surface` when both are
  present.

## Phase 2 (not in this PR)

1. Re-evaluate `budget_hawk` and `primary_model_floor_advisor` TierAdjustment
   factories for promotion to `surface=improvement`. Validate by checking
   whether operators actually act on them as capability decisions or as
   cost-firefighting.
2. Begin pattern-miner spec: walk observation tuples per bot
   (`{noun, verb, mood, engagement}`), surface engagement clusters and
   friction clusters as *structured findings* that downstream synthesizer
   generators consume. This is the substrate Recommendations is missing.
3. Build an objective-aware synthesizer that reads bot persona / purpose /
   user profile, matches pattern findings against capability gaps, and
   emits Proposal-shaped recommendations only when a material change is
   warranted.
