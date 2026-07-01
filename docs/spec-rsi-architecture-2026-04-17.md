# Better Engine — Architecture (2026-04-17)

Status: **implemented** (2026-04-19). L1–L6 shipped in `packages/analyzer/`; the admin UI surfaces each capability via the Proposals, Generators, Meta-health, and Profile pages (see [archive/specs/spec-ui-rationalization-2026-04-18.md](archive/specs/spec-ui-rationalization-2026-04-18.md) Phase 1–4). Design notes below remain authoritative; deviations are noted in the layer specs.

**What this is.** The internal architecture for **Better Engine** — the unified engine that makes an OpenClaw pod better. "Better" is interpreted broadly: fixing a broken gateway makes a pod better; patching a security vulnerability makes it better; extending a thriving app makes it better; a word-of-the-day makes a moment of the user's day a little better. All of these are improvements. The Engine's job is to surface, at any moment, the single most valuable next thing — across operations, security, cost, RSI-style improvements, and whimsy.

**Naming note.** Earlier drafts of this document were titled "RSI Architecture." RSI (recursive self-improvement — the optimizer-style portion of the work) is *one component* of Better Engine, not the whole. The rename reflects Pod-admin's clarification (2026-04-18) that operations, security, and cost must sit inside the same engine rather than alongside it.

**Relationship to other specs.**
- [archive/specs/spec-better-engine-2026-04-15.md](archive/specs/spec-better-engine-2026-04-15.md) — the **user-facing surface** concept for Better Engine: single-focus UI, `evo`/`evolve` keyword in any bot, Recommendations as first-class, learning from act/dismiss/reject/snooze. That spec described the surface; *this* spec describes the backend engine that feeds it. The two are complementary; this spec refines the earlier one rather than replacing it.
- [archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md](archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md) — Layer 1 implementation spec derived from this document.
- [spec-session-quality-2026-04-15.md](spec-session-quality-2026-04-15.md) — the current productive/maintenance classifier, superseded here as a *primary* signal.
- [archive/specs/spec-recommendations-engine-2026-04-16.md](archive/specs/spec-recommendations-engine-2026-04-16.md) — earlier recommendations work to be reconciled during Better Engine absorption.
- [feedback-loop.md](feedback-loop.md) — current measure → analyze → apply loop.
- [manifest-spec.md](manifest-spec.md) — app manifests, which become the taxonomy seed.

---

## 1. The problem with the current frame

The existing system measures `maintenance_ratio` — the share of a bot's sessions classified as "maintenance" rather than "productive." This was supposed to answer: "is the bot delivering value, or treading water?"

Two structural flaws:

1. **Session is a unit of time, not a unit of purpose.** One session often contains multiple threads; one purpose often spans many sessions; some sessions are exploratory chat without a defined success criterion. Grading a session conflates unrelated things.
2. **The signal bundles LLM quality, user topic choice, config health, and chance.** Only config health is actionable. The rest isn't something Evolve controls and therefore shouldn't try to measure as a headline health number.

A session-quality metric will always be a noisy proxy for "is this bot working?" — and the system it drives will, at best, produce a lot of weak signals that humans don't trust and don't act on. That is the specific failure mode we want to avoid.

## 2. Core reframe: behaviors as the first-class observation

The right unit of observation is a **behavior cluster** — a recurring pattern of what the user actually does with the bot, across any number of sessions. "Tracks meals and asks about nutrition." "Schedules meetings and drafts follow-ups." "Troubleshoots its own config." Behaviors don't map cleanly to sessions; they map to repeated activity.

Three first-class objects, not sessions:

| Object | What it is | Improvement surface |
|---|---|---|
| **Apps** | Behaviors the operator has formalized via manifest | Manifest fields, tools, prompts, crons |
| **Substrate** | The bot's operational layer | Memory, handoffs, AGENTS.md, tier routing, config, auth, compaction |
| **Gaps** | Observed user intents not served by any app | Candidates for gallery install, app extension, or new-app ideation |

Sessions remain a storage unit — you query across them to compute behavior observations — but they are no longer a reasoning unit. No one asks "how was this session?" They ask "how is this app doing?" or "what gaps are appearing?"

## 3. Observation model: (noun × verb × mood) tuples

Per session — or per topic-coherent segment within a session — a single structured LLM call produces a list of tuples:

- **Noun** — the domain of activity (fitness, calendar, email, code, travel). Stable, long-lived. Apps naturally align to nouns.
- **Verb** — the form of cognitive work (drafting, planning, troubleshooting, tracking, researching, summarizing, reviewing). Transferable across domains — "drafting" happens in email, documents, code.
- **Mood** — affect signal (urgent, enthusiastic, frustrated, neutral). Not a cluster boundary but a cluster *color*.
- **Engagement signal** — turns spent, user-originated follow-ups, corrections, reruns.

A cluster is a **cell in the (noun × verb) matrix** with a distribution of moods and an engagement profile. "Meetings × scheduling × neutral × thriving" is a different cluster from "meetings × troubleshooting × frustrated × struggling" even though they share a domain.

### Taxonomy: hybrid, seeded from manifests

Three tiers of category vocabulary:

1. **Installed apps** — manifests provide active category vocabulary via `display_name`, `capability_tags`, `session_keywords`. Installing an app is itself a declaration: "I care about this category."
2. **Gallery apps** — uninstalled ecosystem apps contribute candidate categories. When "other" bucket activity matches an uninstalled app, that's a strong install recommendation.
3. **Emergent clusters** — genuinely novel behaviors mined periodically from the "other" bucket, surfaced for LLM judgment on whether they deserve promotion to a new app.

This keeps the taxonomy aligned with operator intent while leaving room for novel behavior to surface.

### Adjacency moves fall out of the matrix structure

- **Same cell, broader scope** — tracking protein → tracking fiber (same noun, same verb, wider coverage)
- **Adjacent noun, same verb** — track fitness → track sleep (same operation, neighboring domain)
- **Adjacent verb, same noun** — schedule meetings → organize meeting notes (same domain, next cognitive step)
- **Chain completion** — three-of-four of a workflow chain (schedule → agenda → notes → follow-ups) suggests the fourth

## 4. The user profile

A persistent, evolving user profile is load-bearing — not auxiliary context. It does three things at once:

1. **Interpretation context for clusters.** Same (fitness × tracking × high engagement) cluster means different things for a marathoner, a rehab patient, and a casual gym-goer.
2. **A proposal target in its own right.** "Based on recent scheduling activity, infer you have school-aged kids — confirm?" Profile enrichment is legitimate improvement.
3. **Personalization substrate for the RSI flywheel.** Different users converge to different generator portfolios; the profile is the per-user memory that makes personalization meaningful.

Shape: a markdown file, sectioned (demographics, vocation, interests, family, communication preferences, values, constraints), where every field carries **provenance** — the sessions or turns it was inferred from. Inferred facts surface as confirm/deny proposals before being committed. The user can read and edit the file directly; privacy stays legible.

Communication-style preferences ("prefers direct tone", "dislikes emoji", "uses passive voice") live here too — they're improvement targets currently invisible to the system.

## 5. The feedback loop

Eleven concrete steps. Every cluster analysis passes through this cycle regardless of which generator acted:

1. **Observe** — structured tuples extracted per session/segment
2. **Aggregate** — roll into (noun × verb) cells with engagement, mood distribution, trend
3. **Classify the cell** — served by an installed app? Matches a gallery app? Truly uncovered?
4. **Generate a hypothesis** — the appropriate generator fires for the cell's state
5. **Attach provenance + falsifiable claim** — which generator, what signal, what metric should move, over what window
6. **Present** — one-line pitch, two-line evidence, explicit claim; or applied autonomously if eligible (§10)
7. **Human decides** (for approval-tier) — approve / reject / defer; all three are data
8. **Apply**
9. **Verify** at the claim's horizon — did the target metric move?
10. **Update the generator's track record** — win, loss, ambiguous
11. **Rebalance** — generators with worse track records fire less often or need stronger signals

## 6. Action categories and their cadences

| Category | Cadence | Risk | Verification |
|---|---|---|---|
| Gallery recommendation | Weekly, many candidates | Very low | Install rate + usage post-install |
| App enhancement (manifest/tools/prompts) | Monthly, few per app | Medium | Target-metric delta, 2-week window |
| App ideation (novel) | Rare, human-heavy | High creative | Does the scaffolded app get used? |
| Sysadmin / substrate | Event-driven | Medium-high | Does the error signal disappear? |
| OC-wide improvements (specialization, routing) | Rare | Varies | Varies; some qualitative |

Gallery recommendations are the cheapest, highest-yield move and are under-used in the current system. They should be the first real output of the new architecture — the friendliest demonstration that the loop works.

Ideation is the most expensive and speculative. It should not dominate early outputs.

## 7. Generator ensemble — optimizers and guardians

The system runs an **ensemble of generators** rather than a single optimizer. Each generator has a distinct philosophy, a tracked record, and a defined role. Some propose changes; some enforce constraints. Better-performing generators earn more LLM budget, more prompt attempts, lower confidence thresholds to surface proposals. Under-performers are starved — but never fully, a minimum floor preserves diversity and resilience to shifting user needs.

### Two structural types: optimizers and guardians

| Type | Role | How they "win" |
|---|---|---|
| **Optimizers** | Propose improvements in a positive direction | Adoption × measurable lift |
| **Guardians** | Enforce constraints and catch bad outcomes | Threats caught, false-positive rate |

Optimizers have a positive success metric (e.g., efficiency lift, app adoption). They compete on producing more of it per LLM dollar. Guardians hold constraints (e.g., privacy posture, spend cap, runtime health). They mostly *veto* or *annotate*; their wins are prevented losses, not generated value. Scoring them on the same formula as optimizers is a category error.

This has an important consequence: **guardians don't earn LLM budget by climbing the wins scoreboard.** They have a **duty budget** that runs regardless of recent proposal volume. The cost of a missed credential exposure, a silent runtime failure, or a meta-level RSI degradation is not recoverable; you cannot trade that exposure for a cheaper month.

### Two levels of competition

Competition happens on two axes:

**Intra-dimension competition.** Within a single objective (e.g., efficiency), multiple personas with different philosophies can compete head-to-head on the dimension's own success metric. The winner within a dimension carries that dimension's flag forward. This is the real survival-of-the-fittest, and its main value is as a safeguard — it prevents the first efficiency persona we ship from becoming the locked-in answer. Early on most dimensions will have one persona; competitors are added as dimensions mature and warrant alternative approaches.

**Cross-dimension arbitration.** *Not* competition, because dimensions aren't directly comparable — efficiency wins and privacy wins don't belong on the same scoreboard. Cross-dimension prioritization is handled by the **referee** using user-specified or inferred dimension weights. See Section 8.

### The nine personas

| Persona | Type | Dimension | Philosophy |
|---|---|---|---|
| **Adjacency Explorer** | Optimizer | Utility | Thriving clusters have natural neighbors |
| **Gap Filler** | Optimizer | Capability growth | Uncovered intents want coverage |
| **Efficiency Hawk** | Optimizer | Efficiency | Same outcome, fewer steps |
| **Persona Tuner** | Optimizer | Voice / fit | Tone, communication style, user fit |
| **Deprecator** | Optimizer | Hygiene | Old things accumulate cost and noise |
| **Security Warden** | Guardian | Safety / privacy | Some behaviors are risky regardless of user consent |
| **Budget Hawk** | Guardian | Cost | Total spend must respect a cap |
| **Sysadmin Watchdog** | Guardian | Substrate health | The OpenClaw platform and the bot's operational substrate must both run cleanly — gateway, plugin, launch daemons, ACLs, tool integrations, memory, handoffs, tier config |
| **Evolve Watchdog** | Meta-guardian | Meta-health | The Evolve RSI system itself must not be causing more harm than good |

Five optimizers, three guardians, one meta-guardian.

**Note on Substrate Guardian.** Earlier drafts listed a separate "Substrate Guardian" for the bot-layer substrate (memory, handoffs, tier config) distinct from Sysadmin Watchdog's platform layer (gateway, plugin, daemons, ACLs). In practice the spec series folded both into a single Sysadmin Watchdog with detector banks for each layer — the conceptual distinction between "platform" and "bot substrate" is useful when reading, but as one generator with clear detectors rather than two generators with overlapping observation streams. The folding happened during L2 implementation planning (see [archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md](archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md)) and is endorsed here.

### Why Efficiency Hawk and Budget Hawk are distinct

Efficiency optimizes steps per outcome; Budget enforces a total spend cap. They can be in tension. An Efficiency proposal might route more traffic to Sonnet (fewer turns per session but more dollars per turn). The Budget Hawk can veto or force an alternative when monthly spend is near cap. This is a feature — healthy tension between generators produces better tradeoffs than a single cost-aware optimizer would.

### Why Security Warden matters

Other generators optimize for user value, but an RSI system proposing changes to tool scope, memory contents, and app permissions has real risk surface. The Security Warden is often an *antagonist* to expansion proposals: "this would grant banking-credential access for a minor efficiency win — veto." That is the desired behavior; it is the checks-and-balances part of the ensemble.

It also watches the observation stream itself: credential exposure in profile inference, sensitive data in memory candidates, prompt-injection patterns in user input, exfiltration patterns in outbound summaries.

### Sysadmin Watchdog covers two layers of the stack

The Sysadmin Watchdog's detectors split internally between two layers:

- **Platform layer** — things the bot *runs on top of*: gateway stability, plugin load errors, launch daemon health, file ACLs and permissions, tool integration timeouts, corrupted config files, disk and resource pressure. When these degrade, the bot *can't work at all* — often in ways the user can't see, wasting cycles on problems they'd never think to diagnose.
- **Bot-substrate layer** — things the bot *uses*: memory freshness, handoff quality, tier routing, AGENTS.md content, compaction settings, auth profile composition. When these degrade, the bot's *work* degrades.

A gateway crash and a stale handoff are different problems with different remediations, but they belong to the same generator because the observation stream and the charter invariants are the same ("never silently resolves; always emits actionable proposal or escalation"). Separate detector modules keep the diagnostic paths clean; a single persona keeps scoring, autonomy, and track-record accounting simple.

### Why Evolve Watchdog — the meta-guardian

The scariest failure mode for an RSI system is **slow self-degradation that nobody notices.** Generators start proposing noise; users start ignoring them; calibration drifts in unhelpful directions; the verification signal becomes unreliable; and the whole flywheel spins the wrong way while still appearing to function. Without a meta-guardian this can compound for months before anyone realizes Evolve has become a net negative.

The Evolve Watchdog monitors the RSI system itself for warning signs:

- Proposal volume drifting too high (noise) or too low (stale)
- Auto-revert rate climbing (claims not holding)
- User rejection rate climbing on particular generators
- Verification signal reliability degrading (e.g., claims keep reverting but target metrics don't respond predictably)
- One generator dominating in ways that hurt diversity
- Calibration updates that correlate with worse downstream outcomes
- Meta-layer costs ballooning disproportionately to user-facing value
- Observation-model drift (extraction quality declining over time)

When flags fire, the Evolve Watchdog can throttle specific generators, pause automatic application of a class of change, roll back a calibration update, or escalate to the human operator. It is, effectively, the immune system of the RSI flywheel.

Philosophical subtlety: the Evolve Watchdog itself can go wrong. A paranoid watchdog throttles everything; a permissive one lets degradation continue. For now its thresholds are human-tunable and its actions conservative (throttle, pause, escalate — not aggressive rollback of other personas' work). A more autonomous meta-guardian is deferred until we have good track-record data on a human-tunable one.

### Scoring varies by persona type

| Type | Score formula | What it prioritizes |
|---|---|---|
| Optimizer | `(adoption_rate × avg_metric_lift) / cost_per_proposal` | Impact per LLM dollar; discourages obvious-only wins and safe-only plays |
| Guardian (constraint) | `(threats_caught - false_positives × annoyance_weight) × severity` | Catches that mattered; penalizes false alarms |
| Meta-guardian | Composite of downstream system health metrics | Whole-system trajectory, not individual proposals |

No attempt to make these comparable across types. They live in different ledgers.

## 8. Dimensions and the referee

Dimensions are first-class objects. Each dimension has its own success metric, its own personas, and its own weight in the user's priority stack. Proposals live within a dimension; they are not compared across dimensions on a common scale.

### Candidate dimensions

| Dimension | Personas in dimension | Metric flavor |
|---|---|---|
| **Utility / efficacy** | Adjacency Explorer | Positive lift (adoption, resolution rate) |
| **Capability growth** | Gap Filler, Ideation | Positive lift (new behaviors enabled, adoption of novel apps) |
| **Efficiency** | Efficiency Hawk (+ competitors over time) | Positive lift (outcomes-per-turn, turns-per-outcome) |
| **Cost** | Budget Hawk | Constraint (stay under cap); veto mechanic |
| **Safety / privacy** | Security Warden | Constraint (prevent exposure); veto + risk annotation |
| **Substrate health** (platform and bot layer) | Sysadmin Watchdog | Mixed: lift on drift resolution + veto on critical failures; internal detectors split by layer |
| **Voice / fit** | Persona Tuner | Positive lift (user-perceived fit, correction rate) |
| **Hygiene** | Deprecator | Positive lift (cruft removed without regression) |
| **Meta-health** | Evolve Watchdog | Composite (RSI system trajectory) |

### Dimension weights

Weights are a first-class, user-facing, adjustable object — probably stored as part of the user profile. Each weight influences:

- How much airtime winning proposals in that dimension get (surfacing order, prominence)
- How much LLM budget the dimension's personas receive
- How ties and conflicts are arbitrated

Weights should be both **user-settable** ("I care about privacy 3× novelty") and **inferable** ("user rejected two cost proposals in a row; lift Cost weight 15% until next explicit adjustment"). Inferred adjustments should be transparent and revertable.

Default distributions exist per bot archetype (personal assistant vs research bot vs coding bot), but all weights move over time as preferences become clear.

### The referee's responsibilities

The referee is load-bearing and deserves its own naming. It does:

1. **Holds the dimension weights** and keeps them current (both user-set and inferred).
2. **Arbitrates conflicts.** If two proposals would interfere (Efficiency wants Sonnet; Budget says stay on Haiku), the referee decides by weights and current constraint state — or surfaces both as a tradeoff for human choice.
3. **Handles vetoes.** Guardian vetoes short-circuit the surfacing pipeline. Security and Budget vetoes are hard stops. Sysadmin vetoes gate productive-activity proposals until substrate issues clear.
4. **Orders surfacing.** When many proposals clear review in the same cycle, the referee controls what the human sees first and what runs autonomously.
5. **Rate-limits whole-system noise.** Even with per-generator throttles, the referee caps total proposals surfaced to the user per week. Noise discipline at the top level, not just the leaf level.

### No single persona owns the composite

The composite "make the user's life better" is real but fuzzy. No single persona should try to optimize it directly — that persona would tend to devour the others, because the composite is vague enough that any local optimum can be rationalized as life-improving.

Each persona owns a narrow metric it can defend. The referee (plus user weights) arbitrates tradeoffs. The composite *emerges* from the ensemble; it is never an objective any single agent pursues.

## 9. Better Engine — user surfaces and actions

The referee ranks; Better Engine is how the ranking reaches the user. Two surfaces, one underlying stream of ranked proposals, four actions the user can take on any surfaced item.

### The single-focus principle

At any moment, the user sees **one thing** — the highest-ranked proposal that needs their attention. Not a dashboard, not a list, not a triage queue. One thing. When that thing is handled, the next one surfaces. When nothing meaningful is pending, Better Engine surfaces **whimsy** (a curiosity item, word of the day, a delightful factoid) — whimsy is both user-pleasant and a useful signal: a system constantly whimsical may mean generators aren't firing when they should.

This UX principle is the essence of what was captured in [archive/specs/spec-better-engine-2026-04-15.md](archive/specs/spec-better-engine-2026-04-15.md). It predates this spec and is preserved intact. What this document adds is the backend — the persona ensemble, referee, and verification loop — that gives Better Engine trustworthy, falsifiable, adaptive content to rank and surface.

### Two surfaces

**Admin UI — Type 1 (pod sysadmin).** The primary dashboard shows the single top-ranked proposal whose `approval_audience = pod_operator`, plus a short portfolio list underneath. Covers pod-wide and cross-bot items: security alerts, cost alerts, sysadmin fixes, app-gallery recommendations, architectural suggestions.

**Bot conversation (`evo` / `evolve` keyword) — Type 2 (personal-bot user).** The user types `evo` (or `evolve`) into any bot conversation and receives the top-ranked proposal for *that bot*, as a conversational pitch in the bot's voice. Covers bot-local items: extend this app, adjust that tier, curate this memory. Proposals with `approval_audience = bot_primary_user` flow here. Infrastructure is already shipped ([packages/plugin/src/better/KeywordHandler.ts](packages/plugin/src/better/KeywordHandler.ts)) and routes to the existing Better Engine; migration is about changing what sits behind the keyword, not the keyword itself.

Type 3 users (shared-team, non-primary) are observation sources only — they don't receive surfacings.

### Four user actions

Every surfaced proposal offers the same four actions:

| Action | State transition | Meaning |
|---|---|---|
| **Act** | → `approved_human` → apply pipeline | User wants this change; the system proceeds to apply and verify |
| **Dismiss** | → `dismissed` | Soft negative — user doesn't want to act now but isn't rejecting the idea; informs calibration weakly |
| **Reject** | → `rejected` | Hard negative — user rejects the proposal; calibration weighs this more heavily |
| **Snooze** | → `snoozed_until(ts)` → back to `pending` at ts | User wants this later; proposal disappears from the surface and returns after the snooze window |

Dismiss and reject are distinct on purpose. "Not now" is different from "no." Generators whose proposals are frequently dismissed should reconsider timing or framing; generators whose proposals are frequently rejected should reconsider whether the proposal is good at all.

### Snooze mechanics

A snoozed proposal:
- Leaves the surface immediately
- Sits in `snoozed/` with a wake-up timestamp
- Returns to `pending` when the wake-up time passes, re-enters the referee's ranking
- If the underlying condition has resolved by wake-up, the generator that produced it should supersede it with a "no longer relevant" note rather than resurfacing stale

The user picks the snooze window (hour / day / week / month, or custom). Default probably "tomorrow."

### Whimsy

When the ranked pending queue is effectively empty (no non-whimsy proposal above a minimal urgency threshold), Better Engine surfaces a whimsy item. Whimsy items are:

- Not generated by the persona ensemble's proposal pipeline
- Produced by a small separate source (word of the day, interesting fact, short encouragement)
- Never block a real proposal — if a real proposal arrives mid-whimsy-display, the real one wins

The presence of whimsy is also a signal the Evolve Watchdog (§7) monitors: abnormally high whimsy rate may mean the system is under-detecting.

### Referee = Better Engine, conceptually

The "referee" in §8 and "Better Engine" are the same layer named from two perspectives. §8 describes the ranking and arbitration logic; §9 describes its user-facing outputs and action model. In implementation, they are the same module — the Better Engine — sitting above the Arbiter's proposal lifecycle machinery.

## 10. Autonomy spectrum

Not everything should require explicit user approval. If every change needs a human click, we've built a notification system, not an improvement system. The Evolve system must do the *work* of improvement; the user verifies through how the bot actually performs.

### Gating criteria

Two questions, both must be yes for autonomous action:

1. **Small blast radius** — if this is wrong, is the harm bounded?
2. **Cleanly reversible** — can the system itself revert without human intervention?

### What passes both tests (autonomous candidates)

- Memory curation (prune, consolidate) — observable, revertible via backup
- AGENTS.md appends from observed patterns — additive only
- User profile inference — surfaces confirm/deny later; inference happens now
- Tier routing adjustments — fully reversible via cost/quality deltas
- Classifier calibration updates — already autonomous today
- Manifest tag/keyword tweaks (not scope or tools) — low-risk
- Compaction settings — reversible
- Gallery recommendations *pre-filtered* into a short list — surfaced, not installed

### What requires explicit approval

- Installing a new app (changes what the bot can do)
- Creating a novel app from ideation
- Removing or deprecating anything significant
- Config changes touching channels, auth, tools, permissions
- Architectural changes (bot specialization, new bot)
- Anything Security Warden flags, regardless of other-generator confidence

### Verification + auto-revert: the safety rail

Every autonomous change is paired with an **autonomous revert condition**. The generator commits at change-time to:

- A target metric
- Expected direction of movement
- Verification window (e.g., 14 days)

A background verifier checks expired claims. If the claim did not hold, the change is auto-reverted with a record of the failure. No human required.

Corollary: **a change whose success can only be judged by human gestalt is by definition not autonomous-eligible.** Autonomy requires measurable claims. This constraint is a feature.

## 11. Earned authority — autonomy and approval coupled

The autonomy tier and the generator-competition system map to each other:

- **Autonomous changes** are the *training ground.* Frequent, cheap, measurable. Generators accumulate track records fast enough for competition to mean something.
- **Approval-tier changes** are the *earned territory.* Generators with proven track records on autonomous work earn the privilege to propose higher-stakes changes. A generator that has shipped 20 verified autonomous wins over three months earns prominent placement for its "propose a new app" suggestions.

The reward structure falls out of this naturally — no separate reward machinery needed. Early in a bot's life, narrow autonomy and more human-in-loop. Mature bot with proven generators, wider autonomy. The same mechanism serves both phases; only the parameters change.

## 12. Competition, not evolution — for now

We considered a full evolutionary overlay (generators mutate their own configurations, spawn variants, have offspring replace failures). The aesthetic appeal is real but the yield is low early:

- Verification cycles are 1–2 weeks
- Multiple data points needed per generator before a track record is trustworthy
- Each evolutionary "generation" would therefore be months

**Competition + differential resourcing gets ~80% of the adaptation benefit without the genetic-algorithm machinery.** We earn the right to add mutation later, once the plain competitive system shows ceilings worth breaking through. Possibly year two.

## 13. Calibration — three stacked layers

All three learn from the same accounting: `(provenance, verification_result)` pairs, aggregated at different grain levels.

| Layer | What it learns | Grain |
|---|---|---|
| Signal calibration | Which thresholds correlate with adoption and lift | Per signal |
| Generator calibration | Which techniques produce more wins | Per generator |
| User calibration | Which generator preferences fit this user | Per user × generator |

The existing `calibration/` infrastructure already does a weak version of signal calibration. The generator and user layers are new.

## 14. Build order

Working backward from the architecture, the correct sequence:

1. **Verification + revert scaffolding.** No autonomy without it. No competition without it. Load-bearing for everything else.
2. **Evolve Watchdog scaffolding (passive).** Logging, threshold definitions, and instrumentation from day one — even before there's enough signal to fire. The intent is "we will notice if this goes wrong" baked in at the foundation. Active monitoring and throttle actions come online later, once volume supports it.
3. **Sysadmin Watchdog migrated from existing detectors.** The current `analyze.py` already detects billing drift, permission errors, config weirdness. That functionality maps most cleanly onto Sysadmin Watchdog (platform layer). Formalize it as a guardian within the new structure rather than leaving it as a detector grab-bag.
4. **Two or three optimizers at the autonomous tier.** Enough diversity for intra-dimension signal to appear. Proposed starting set: Adjacency Explorer (Utility), Gap Filler (Capability growth), Deprecator (Hygiene — picks up stale apps and memory bloat).
5. **Track records + differential resourcing.** The competitive mechanic itself. Guardians get duty budgets; optimizers get competitive budgets; scoring is per-type.
6. **Observation model upgrade.** Structured (noun × verb × mood) tuple extraction replacing current keyword + outcome-string scheme.
7. **User profile substrate.** Inference + provenance + confirm/deny flow. Dimension weights live here.
8. **Referee + cross-dimension arbitration.** Once there are enough dimensions with active personas, the referee becomes necessary rather than implicit.
9. **Gradual autonomy expansion** as verify/revert proves reliable.
10. **Approval-tier proposals earned by track record.**
11. **Remaining personas** (Persona Tuner, Deprecator, Security Warden, Efficiency Hawk, Budget Hawk) added as data volume supports. Security Warden is the one exception to "wait until data supports" — add it before any autonomous change can touch tool scope, auth, or memory content.
12. **Evolve Watchdog active monitoring + throttles** turn on once there's enough generator and proposal volume to produce a meaningful meta-signal.
13. **Evolutionary overlay** (optional, late).

**The honest reframe of the whole thing:** the system's real job is not classifying sessions or clustering behaviors. It's running a **safe, measured hypothesis-testing loop** with an ensemble of optimizers and guardians, arbitrated by a referee across weighted dimensions, against a verification substrate that can quietly revert its own mistakes — all watched by a meta-guardian that keeps the flywheel from spinning the wrong way. Observation, taxonomy, noun/verb/mood extraction, user profile, cluster analysis — all input machinery feeding that loop.

## 15. Open questions

- **Segmentation within a session** — one LLM call producing a list of tuples per session is probably right, but we haven't sanity-checked reliability on long multi-purpose sessions.
- **The right verification-window per action category** — gallery recommendations need days; manifest enhancements need weeks; ideation needs months. How do we handle claims with very different horizons in the same verify daemon?
- **Cross-bot learning** — can a generator's track record on bot A inform its starting confidence on bot B, or does user-specific personalization dominate?
- **How Budget Hawk is parameterized** — does the user set the cap directly, or does the system propose caps based on historical spend and user wealth inference? Does it interact with dimension weights (Cost weight) or are those orthogonal?
- **Dimension weights — storage, defaults, inference rate.** Where exactly do weights live in the user profile? What are the default distributions per bot archetype? How quickly should inferred adjustments take effect — immediate, or buffered over a window to avoid thrashing?
- **Duty-budget sizing for guardians.** Optimizers earn budget competitively; guardians get duty budgets. But how much? A Security Warden that runs too cheaply misses things; one that runs too richly is wasteful. Probably tunable, but what's a reasonable starting point per guardian type?
- **Intra-dimension competition mechanics.** If two efficiency personas exist, do they see the same observation stream and compete on whose proposal gets surfaced? A/B across users? Something else? The aesthetic appeal is clear; the operational mechanics aren't.
- **Evolve Watchdog thresholds.** What counts as "too many proposals," "calibration drift," "verification signal degradation"? These are the parameters that determine whether the meta-guardian is paranoid or permissive. Probably a learning loop of its own — the Watchdog's own thresholds should calibrate against outcomes.
- **Who watches the watcher.** Evolve Watchdog itself can go wrong (paranoid, permissive, miscalibrated). For now it's human-tunable with conservative actions. Longer term, is there a cleaner answer than "the human reviews the watchdog's actions occasionally"?
- **Reconciling with the existing [recommendations engine](archive/specs/spec-recommendations-engine-2026-04-16.md)** — how much of its scoring and presentation layer is salvageable vs replaced by the generator portfolio?
