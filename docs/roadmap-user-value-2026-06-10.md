# Roadmap: Functioning → Astoundingly Useful — 2026-06-10

**Status:** draft for design sync
**Companion to:** [roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md) (credibility axis). This doc is the second axis: **usefulness**. The 80→100 roadmap makes Evolve's claims true; this roadmap makes the pods worth having.

---

## 1. Why this roadmap

Most of Evolve is the admin experience: install, operate, watchdog, heal. But a smoothly run pod that is underused is not worth much. The end value of an OpenClaw is what it does for its users — and there is a boundary (the platform/app quandary): we cannot invent a user's workflows for them. What we *can* do, and largely haven't systematized, is everything short of that:

- **Elicit** the workflows they already have (wizard, purpose capture).
- **Deliver** the proven-valuable patterns by default (proactive briefings, watchers, triage).
- **Verify** that the valuable thing actually keeps happening (our ops DNA, pointed at user value instead of daemon health).
- **Notice** when value is latent or decaying, and say so (usage baseline, effectiveness layer).
- **Compound** what works across bots and pods (sharing, Lessons).

**Definition of done for this roadmap:** an operator three weeks in would fight to keep their pod — not because it's well-administered, but because it does things they'd miss.

**Mandate boundary (what we explicitly don't do):** invent workflows users don't have; build content/skills better left to the community or upstream; chase feature parity with platform assistants (Spark et al. — see the substrate strategy); treat engagement volume as the goal (a bot that quietly does its one job well is a success, not a churn risk).

---

## 2. Audit: what we already have (2026-06-10)

### 2.1 Vision & framing (strong, mostly coherent)

| Asset | Where | State |
|---|---|---|
| Personas: Marcus / Diana / Carla | [product-vision.md](product-vision.md) | Defined; each carries a gap list. Carla activation explicitly not done. |
| Plex test (no jargon in primary surfaces) | [principle-plex-test.md](principle-plex-test.md) | Load-bearing principle |
| Applications-as-contracts framing | [applications-vs-skills.md](applications-vs-skills.md) | Clear; our strongest differentiator |
| Safety as flagship ("vigilant by default") | product-vision.md | Framed; not yet legible *to users* (see U4) |
| Voice/tone (Tailscale/Notion/Plex register) | [operator-message-style.md](operator-message-style.md) | CI-enforced |

### 2.2 Shipped user-value capability

| Layer | What ships | Honest state |
|---|---|---|
| **App framework** | Manifest v4, Forge pipeline, two-tier audits, coherence Pass A | Production-quality; the load-bearing asset |
| **Gallery** | 13 apps at the Atlas quality bar (post 2026-06-06 backfill), incl. Morning Briefing + EA suite (Evening Sweep, Pre-Meeting Brief, Commitment Tracker) | Shipped; static — no install-time guidance about *which* apps fit *this* user |
| **Skills installs** | 6 revivals shipped (Obsidian, Dropbox, Notion, GitHub-MCP, Runway, Linear); MCP pattern proven | Shipped; platform items P3–P10 open; HA blocked upstream |
| **Observation layer** | ObservationTuples (noun×verb×mood×engagement), per-bot user profiles w/ DNT, POD_CONDUCT injection | Collection works; almost nothing *consumes* it yet |
| **Metrics** | Tile metrics: turns/sessions/cost with deltas, `apps.used_7d/28d`, human-vs-scheduled trigger split | Rich raw data; no "is this pod delivering value" rollup |
| **Better Engine** | 28 generators, portfolio balancing, Improvements surface | Heavily ops-weighted; user-value generators (app_suggester, engagement_amplifier, persona_tuner) thin/Phase-2 |
| **Evo** | 18+ subcommands, proposal reading, alert relay | `wizard`, `gallery`, `guide` stubbed |

### 2.3 Spec'd but not built (the backlog this roadmap sequences)

| Spec | What it is | Status |
|---|---|---|
| [Effectiveness layer](spec-effectiveness-layer-2026-06-09.md) | Layer-2 synthesis: "is the bot good at the job it was hired for" → 0–3 evidence-cited suggestions; bot `purpose` capture w/ archetypes | Spec'd; gated on 80→100 Phase 1 loop closure |
| [Manifest v7](spec-manifest-v7-2026-05-20.md) | Spec/Instance/Lessons split; first-class `event_triggers`, `bot_guidance`, `privacy`, `audience_scoping`; cross-pod sharing | **Correction (2026-06-10, [slicing addendum](spec-manifest-v7-slicing-2026-06-10.md) §1):** largely shipped incrementally — discriminator, migration, `event_triggers`, `bot_guidance`, Reflect/Adopt, Lessons, within-pod sharing all on main. Remaining: `privacy{}`/`audience_scoping{}`, native-write cutover, Lessons→Adopt loop, cross-pod |
| [Add-bot wizard](spec-add-bot-wizard-2026-05-28.md) + [conversational reference](reference-conversational-bot-creation-2026-05-19.md) | Evo-hosted conversational creation: why-before-what, audience analysis, role→integrations→apps | Design frozen; deferred on substrate deps |
| Evo help tools | BM25 help search/read in chat | Designed, not shipped |
| Alert subscriptions / Improvements page completion | Per-operator routing; "Ideas" surface for effectiveness suggestions | Partial |

### 2.4 The honest gap

We built the **plumbing of usefulness** (apps, skills, observation, metrics) and the **machinery of improvement** (generators, proposals), but the layer that connects them to a person's actual life — *activation, default proactivity, value measurement, and grounded suggestion* — is spec'd, stubbed, or missing. The user-value generators are outnumbered ~5:23 by ops generators. Nothing today answers "is this pod underused, and what would change that?"

---

## 3. What the field says (forum refresh, 2026-06-10)

Full method note: primary sources (HN, GitHub, official blog, practitioner long-reads) verified; Reddit secondhand; the ecosystem is now flooded with AI content farms — those findings are flagged. This refresh confirms and sharpens the May snapshot.

1. **Morning briefing is still the killer app — now adversarially confirmed.** An engineer who observed ~1,000 deployments called daily briefings "the only genuinely functional use case" with sustained usage. Our gallery flagship is correctly chosen.
2. **The funnel leaks at weeks 1–3.** Week 1 is setup tax ("0% work out of the box", ~$40–50 burned on kinks); week 3 is the conversion point ("first week feels like a novelty; by week three it feels like infrastructure"). Drop-off causes, in order: scheduled-task reliability ("breaks every other morning, telling me it fixed itself"), cost surprise, silent memory failures → delegation-trust collapse, "this would work better as a script."
3. **Power users share four habits:** they arrive with existing workflows ("OpenClaw won't invent them for you"); their instances *push* rather than wait (briefings, watchers, heartbeats); they externalize memory to files/Obsidian and distrust conversation memory; they isolate blast radius (draft-only email, sandboxed accounts) — and per-channel model routing for cost.
4. **The #1 value complaint is "set it up but don't know what to use it for."** Close behind: "it can't touch my real stuff" — users wall the agent off from real email/work out of prompt-injection fear, capping value at toy domains. Trust, not capability, is the ceiling.
5. **Ecosystem since May:** Microsoft shipped its own personal assistant *built on the OpenClaw framework* (Build, June 2); a packaging-layer explosion (Picnic, Klaus, Eve, HolaClaw, microVM variants) — our category is now crowded at the "easy install" end but empty at the "makes it genuinely useful" end; NYT profiled SMB owners running multi-Mac agent fleets (Carla-shaped demand, verified); top ClawHub skills are now self-improvement/proactivity skills [unverified counts, consistent direction] — organic demand for exactly our RSI-applied-to-applications layer.

**Mapping to our position:** every drop-off cause and power-user habit is either an existing Evolve strength not yet pointed at users (reliability watchdogging, cost breakers, model routing, safety machinery) or a spec sitting in our backlog (wizard, effectiveness layer, event triggers, audience scoping). Nothing in the field data calls for a capability we haven't already framed. The roadmap is therefore a *sequencing* problem, not a discovery problem.

---

## 4. Strategy: three theses

**T1 — Activation is the highest-leverage gap.** The field's biggest value complaint ("don't know what to use it for") and the weeks-1–3 drop-off are exactly what a packaging layer exists to fix. The wizard's why-before-what design + archetype starter packs + briefing-on-day-1 is our answer. Target: **time-to-first-proactive-value < 24h** from pod install.

**T2 — Our ops DNA is a user-value feature in disguise.** The top stated reason proactive setups die is scheduled-task unreliability — and watchdogging scheduled things is the most mature muscle we have. Pointing Signals at *user-facing delivery* ("the briefing didn't arrive") rather than only daemon health turns our admin strength into the thing power users say they can't get elsewhere. Same story for cost breakers (drop-off cause #2) and tier routing (power-user habit #4).

**T3 — Measure value before optimizing it** ([principle-instrument-outcomes-before-optimization](principle-instrument-outcomes-before-optimization.md)). We must not build suggestion machinery on unmeasured ground — that's how the 138-observation queue happened. A cheap, pure-Python value baseline (the data already exists in tiles + tuples) comes first; the effectiveness layer then has ground truth to cite and a scoreboard to move.

---

## 5. Phases

Effort grades: S = ~1 session, M = 2–5 sessions, L = >5 or calendar-bound.

### U0 — Value baseline (measure first) — **S/M, no dependencies, start now**

The data exists (tile metrics, trigger-kind splits, `apps.used_7d`, ObservationTuples, briefing run logs). Missing is the rollup and the signal.

| # | Deliverable | Notes |
|---|---|---|
| U0.1 | Per-bot **utilization baseline**: active-human-days, proactive deliveries/wk, app-usage coverage (used/installed), engagement trend | Pure Python over existing stores; lands in tile + a Value view |
| U0.2 | **`bot_underused` Signal** (tri-state honest: distinguishes "no data" from "no use") | Producer added to monitor allowlist + schema stock defaults — known drift trap |
| U0.3 | Definition doc: what counts as value, anti-Goodhart guardrails (quiet-but-working ≠ underused; one verb done reliably ≠ low engagement) | Short; reviewed at design sync |

**Proof artifact:** the live pod's bots ranked by utilization baseline, with at least one defensible `bot_underused` firing (the likely candidate is the pod's one bot that was never onboarded to a channel).

### U1 — First valuable week (activation) — **M/L, depends on U0 for measurement only**

| # | Deliverable | Notes |
|---|---|---|
| U1.1 | **Bot `purpose` capture** (archetype + mission, declared/inferred) at creation and backfilled for existing bots | Already spec'd in the effectiveness layer — ship this slice early; it's also the wizard's anchor |
| U1.2 | **Add-bot wizard, evo-hosted** (build the frozen design) | Pull forward from its substrate deferral: ship Claude-assumed, consistent with the multi-runtime deferral decision. Why-before-what → audience → role→integrations → apps → consent+tone |
| U1.3 | **Archetype starter packs**: role → preinstalled app set from the gallery (PA → Briefing+Calendar+Email Triage; project bot → Commitment Tracker+Note-taker; etc.) | Gallery dependency machinery already supports this |
| U1.4 | **Day 0–7 journey doc** (Marcus): stage gates, friction points, where each existing surface fires | Identified missing in audit; cheap; drives U1.2/U1.3 acceptance |
| U1.5 | Wizard ending state: first proactive app scheduled, first briefing within 24h | The activation metric from U0 starts moving |

**Proof artifact:** a fresh bot created end-to-end through the conversational wizard, with purpose captured, a starter pack installed, and a briefing delivered within 24h — transcript in a decision doc.

### U2 — Proactive by default, and provably reliable — **M, parallel with U1**

| # | Deliverable | Notes |
|---|---|---|
| U2.1 | **Proactive-delivery monitor**: a Signal when a scheduled user-facing app misses its delivery window (briefing run-logs already exist to check against) | Tri-state; distinguish "didn't run" / "ran, didn't deliver" / "can't tell". This is T2 made concrete |
| U2.2 | Heal path for missed deliveries + honest recovery message (🟢 per operator-message-style) | Counters "breaks every other morning, telling me it fixed itself" — we tell the truth |
| U2.3 | **`event_triggers[]` first-class** — ship as the first v7 slice | Watchers ("when X happens, tell me") are the second proactive pattern after briefings |
| U2.4 | Watcher/digest templates in gallery built on U2.3 | Evening Sweep & Commitment Tracker already model the shape |

**Proof artifact:** a deliberately broken briefing cron detected, healed, and honestly reported within one cycle; one event-triggered watcher running from a gallery template.

### U3 — Effectiveness layer (grounded suggestion) — **L, gated on 80→100 Phase 1 loop closure**

The spec is written ([spec-effectiveness-layer-2026-06-09.md](spec-effectiveness-layer-2026-06-09.md)); U0 gives it ground truth, U1.1 gives it purpose context. Scope here is sequencing, not redesign:

| # | Deliverable | Notes |
|---|---|---|
| U3.1 | Layer-2 synthesis emitting 0–3 evidence-cited suggestions per bot | Classification: thriving / retire / modify / surface |
| U3.2 | "Ideas" surface (calmer than proposals) + evo delivery | Routes through existing Improvements machinery |
| U3.3 | Mature app_suggester + engagement_amplifier as Layer-2 inputs, not proposal emitters | Fixes the observation-queue failure class |
| U3.4 | Verification = adoption + usage shift over weeks (per spec) | Closes the loop on usefulness the way Phase 1 closes it on ops |

**Proof artifact:** one suggestion grounded in real usage evidence ("12 scheduling asks, 9 resolved by hand → calendar app"), adopted, with measured usage shift.

### U4 — Trust unlocks real data — **M, can interleave after U1**

The field ceiling is trust, not capability. We have the safety machinery; the work is making it *legible and graduated* so users connect real accounts.

| # | Deliverable | Notes |
|---|---|---|
| U4.1 | **Autonomy ladder per integration**: draft-only → act-with-approval → autonomous-within-rules, visible and per-bot | Email Triage already draft-only by convention; make it a first-class, promotable posture |
| U4.2 | **`privacy{}` + `audience_scoping{}`** — second v7 slice | Machine-checkable trust boundary; prerequisite for Carla |
| U4.3 | Blast-radius legibility: extend the existing audit+score surface to answer "what can this bot touch, what has it touched" | Note: extend audit+score, not plain-language bullets — that approach was tried and retired |
| U4.4 | **Externalized-memory default**: workspace markdown + Obsidian pattern packaged as the recommended memory posture | Packaging of the proven power-user habit; do not build memory tech (upstream's job) |

**Proof artifact:** one bot promoted up the autonomy ladder on a real integration by a deliberate operator action, with the audit surface reflecting it.

### U5 — Persona activation waves — **L, after U1–U3 prove on Marcus**

| # | Deliverable | Notes |
|---|---|---|
| U5.1 | **Carla wave**: client-facing project bots — visibility boundaries (needs U4.2), escalation rules, bot retirement/closure package | Pulled ahead of Diana (design sync 2026-06-10): demand externally verified (SMB agent-fleet coverage, June 2026); likely best commercial target |
| U5.2 | **Diana wave**: multi-bot handover + cross-bot synthesis through evo | Architecture exists; onboarding incomplete (per product-vision gap list) |

**Proof artifact:** one real multi-bot scenario each, run on a live pod, written up as a reference doc (the Atlas-session pattern).

### Cross-cutting

- **Manifest v7** ships in slices inside the phases that need them (U2.3 event_triggers, U4.2 privacy/audience), with the Spec/Instance/Lessons split + sharing landing alongside U3 — Lessons are the community-compounding loop the ClawHub self-improvement-skill demand points at.
- **Standing field-research cadence**: quarterly snapshot doc under `docs/research/`, primary-sources-only method note (content-farm contamination is now severe; verify against release notes/source per existing practice).
- **Dogfood retrospectives**: each phase ends with a "used it on the live pod for a week" note, in the v1.5-sprint pattern.

---

## 6. Sequencing logic & interaction with 80→100

1. **U0 first and now** — cheapest item, principle-mandated, and every later phase needs its scoreboard. No collision with 80→100.
2. **U1 and U2 in parallel** — independent tracks (creation flow vs. delivery reliability); both attack the weeks-1–3 funnel leak.
3. **U3 waits for 80→100 Phase 1** (optimizer loop closure + soak) — building Layer 2 on an unclosed Layer 0/1 loop repeats the observation-queue mistake. The soak is calendar-bound, so U1/U2/U4 fill that time naturally.
4. **U4 interleaves** — each ladder/legibility item is small and independently shippable.
5. **U5 last** — persona waves multiply whatever the Marcus path proves; running them before activation works would multiply friction instead.
6. **Non-collision rule:** 80→100 owns security hardening, store concurrency, platform expansion; this roadmap consumes those (e.g., U4 trust story is more credible after Phase 2) but never blocks them.

---

## 7. Design-sync decisions (2026-06-10)

1. **U0 metric set** — delegated to the U0 spec session: it must propose the metric definitions, the explicit exclusions (anti-Goodhart: no optimizing toward chattiness), and a recommendation on the weekly "what your pod did for you" digest (in-mandate vs. noise).
2. **Wizard pull-forward** (U1.2) — **decided: spec delta + build now.** Ship Claude-assumed, evo-hosted, consistent with the multi-runtime deferral decision.
3. **Briefing-by-default** (U1.5) — **decided: default-on, opt-out in the wizard.** Every new bot with a channel gets a briefing scheduled within its first 24h unless declined at creation. Ships in code per product-defaults-in-code.
4. **Carla timing** — **decided: Carla ahead of Diana within U5** (reflected in §5). No Carla spec work pulled ahead of U3.
5. **v7 slicing** — **decided: phase-bound slices**, working assumption pending the slicing addendum (Round 1).

## 8. Execution pathway (Round 1 spawned 2026-06-10)

Tiered spec depth: deep specs only for new mechanisms; addenda for already-spec'd items; build-direct for the rest. Specs run **parallel** (disjoint mechanisms, disjoint files); builds run **gated** on per-spec design sync, then parallel where dependencies allow.

**Round 1 — four parallel spec sessions** (each lands one `docs/spec-*.md` PR as a decision doc: options + recommendation + open questions):

| Session | Deliverable |
|---|---|
| U0 value baseline | Metric definitions, exclusions, `bot_underused` Signal design, digest recommendation |
| U2 delivery monitor | Proactive-delivery Signal (tri-state), heal path, honest recovery messaging, relation to app-audit tiers |
| U4 autonomy ladder | Per-integration posture model (draft-only → approval → autonomous-within-rules), promotion mechanics, audit+score surfacing |
| v7 slicing + wizard delta | Slice→phase map for manifest v7; evo-hosted wizard build delta incl. purpose capture + briefing default-on |

**Gate:** operator design sync on each spec PR. **Round 2:** build sessions sequenced U0 → (U1 track ∥ U2 track), U4 singles interleaved, U3 after 80→100 Phase 1 closes.

### Round 1 outcome (specs landed 2026-06-10: #2605, #2607, #2608, #2610)

All four specs landed as decision docs. Cross-spec recommendations accepted at the 2026-06-10 follow-up sync:

1. **v7 native-write cutover pulled forward** ("Slice 3a"): lands right after Slice 2, not with the U3 wave — stops the dual-shape tax accruing (slicing addendum §6.1).
2. **Watchers ride `schedules[]`** (1–15 min poll), no new trigger sources; `event_triggers[]` stays chat-only until a real app forces push (slicing addendum §3.2, reinforced by delivery-monitor §6.4).
3. **Purpose stored in `network.json`** bot block (wizard delta §4).
4. **Weekly value digest: in-mandate** — ships as U0-B5 after a 2-week rollup soak, consolidated with the planned bot-trends digest into ONE weekly message (value-baseline spec §8).
5. **Schema-counter discipline:** constant re-syncs to 22 with a guard test first; subsequent field blocks (delivery_contract, privacy/audience_scoping) take the next free number at land time — specs' provisional numbers are not binding.
6. All other per-spec recommendations stand as build defaults; per-spec open questions resolve at build time per their stated recommendations.

### Round 2 — build waves

| Wave | Sessions (parallel within wave) | Lands |
|---|---|---|
| **W1 — ✅ shipped 2026-06-10/11** | v7 Slice-1 closeout + counter guard (#2636, trigger-gating follow-up #2641) · U0 B1+B2 (#2634) · wizard M1 (#2638) · U2 PR1 (#2642) | `delivery_contract` took schema **v23**, so Slice 2 takes **v24** (the guard test enforces). Pod note: #2641's paused-trigger gating needs a gateway plugin reload to take effect |
| **W2 — ✅ shipped 2026-06-10/11** | U0 B3+B4 (#2650, proof doc `docs/validation-value-baseline-2026-06-10.md`) · U2 PR2 (#2652) · wizard M2 (#2661) · ladder Phase A (#2663) · v7 Slice 2 (#2660, schema v24 → next free v25) | Collateral: #2651/#2655/#2660-drive-by (lifecycle routes 500-fix), #2664/#2667 (sudoers #1956-revert restoration). **Open gates carried forward:** U0 §2.2 guardrail stays ON until a *natural* `bot_underused` firing (first candidates cross the 28d age gate ~06-25/06-29); ladder §7 live proof blocked — no pod bot has an email integration; Slice-2 pod backfill not yet run (folded into Slice 3a) |
| **W3 — ✅ build lanes shipped 2026-06-11** | wizard M3 (#2676 — packs as tagged gallery bundles, briefing default-on + v2.1 no-data mode, consent seeds `privacy{}`) · Slice 3a (#2677+#2678 — native v7-arc writes; Slice-2 pod backfill run, clean) · ladder Phase B (#2679 — the loop + `autonomy-limits` daemon) · U2 drill chip still gated to **06-18** | Pod verifications 2026-06-11: `autonomy-limits` + `delivery-monitor` daemons installed & running; **conversation-hook payloads carry NO `tool_use` blocks** (checked all 9 bots' raw captures) → rung-3 caps run instruction-only on this pod until an upstream/alternative count source exists — candidate upstream ask |
| **W4 — M4 ran 2026-06-11: half-proof + a P0 find** | **Creation leg ✅ proven** (decision doc #2691): a real bot in ~3 min of conversation + ~90 s provision, purpose/briefing/consent persisted, and the first real-world v7-arc native mints (Slice 3a proof ✅). **Delivery leg structurally unreachable at 3 layers** — day-1 bots have no channel; C-A4 rightly refuses the briefing without one (but the failure was silent); and a **pod-wide P0**: OC 2026.6.1 (installed Jun 3) silently removed gateway `POST /api/message` — every gallery app's delivery had been dead for 8 days, visible only as "unmeasurable". Fixes same-day: #2695 (delivery migrated to `openclaw message send` + exit-status-aware monitor), #2685/#2687 (run bugs), #2693 (consent-seed carry to Instance) | The roadmap's premise demonstrated on our own pod: a "functioning" pod silently not delivering its #1 value, caught only because U2's instrumentation + the U1 proof run existed. Canary soak restarts from #2695; first clean delivery day = Jun 12 |
| **W5 — build legs ✅ shipped 2026-06-11; U1 delivery leg ✅ PROVEN 2026-06-12** | #2707 (wizard channel offer-now token paste · briefing auto-activation on the zero→some-channel transition · loud post-wrap install failures via `system.app_install_failed` · live-channel stamping at forge approval) · #2704 send-surface probe (safe-upgrade tri-state gate + post-upgrade canary send + pod-scope `pod_delivery_regression` escalation) · #2706 collateral: durable CLI device-scope invariant (OC-upgrade-narrowed device scopes repaired on every deploy + hourly drift signal; day-1 bots seeded) — all deployed; admin-ui restarted post-#2707 | Delivery soak from #2695 healthy: first natural `on_time` ledger rows landed 06-11. **U1 re-proof closed 2026-06-12** (see W5-closeout below). **Gated:** U2 drill ≥ **06-18** (evaluates soak from #2695) · U0 B5 ≥ **06-24** (natural `bot_underused` window ~06-25/29). Still held: U3 (80→100 Phase 1 soak + natural firing), Slice 3 proper, U5 Carla. Open operator decisions: email integration on one bot (ladder live proof) · upstream ask for `tool_use` in hook payloads · scrub guard as required check. **New-bot cost defaults — SETTLED 2026-06-12** (see Cost-defaults decision below). |

### Cost-defaults decision — SETTLED 2026-06-12 (finding [#2787](finding-new-bot-activation-cost-2026-06-12.md))

`ledger` day-1 spend $30.26 (82% forge builds + 18% first-audits, all `power` role, zero user value yet) = one-time setup tax; the cap exists but runs outside provisioning's enforcement scope. Operator accepted all four recommendations:

- **A — product-default new-bot daily cap: `$10/day` first 7d → `$5/day`** (graduated, in code per product-defaults-in-code; per-bot is the cost unit).
- **B — `$12` one-time provisioning ceiling**, projected at creation and enforced at forge dispatch — i.e. **make the cap actually govern provisioning** (forge-dispatch + audit-scheduler under the cost check, or a creation budget). "The real fix."
- **C — route the forge provisioning-build + first-audit path from the `power` role → the `standard` role** (~4–5× saving). Stated as a **role** change, not a model-name change (no-provider-literals); model-tiers-owned surface.
- **D — defer the day-1 tier-3 audit to +7d / first real usage** (skip same-day re-audit of just-built-clean apps); diligence-owned posture → auditor-grade review.
- **UI rider** (build-only): wizard projects **cost-to-first-value** and shows alert/hard-cap at creation. Gives U1's "time-to-first-value < 24h" its partner: **"cost-to-first-value < $N, shown and accepted at creation."**

Dispatch: user-value owns A+B+UI; C handed to model-tiers, D to diligence.

### W5 closeout — U1 delivery leg PROVEN (2026-06-12)

Operator pasted `ledger`'s Telegram bot token (real UI path: **Skills page → Telegram skill → "+ Ledger"**); #2707 auto-activation fired through it (robust to real entrypoint). Surfacing the token paste exposed three more delivery blockers — all now fixed and deployed:

- **Gallery briefing still POSTed the removed `/api/message`** (#2695's migration only covered the EA-pack) → fixed by **#2792** (gallery briefing → `openclaw message send` + approval-time `_stamp_scheduled_delivery_contracts`) + **#2795** (deploy-time builtin re-seed). Pod runs canary mode, so these only went live when `evolve-stable` promoted #2788→#2800 (soak/promote at 23:08Z).
- **v7-arc re-forge couldn't pick up the corrected Spec** — `assemble_context_package` read the empty Instance build_spec on rebuild → fixed by **#2803** (`manifest.build_spec or job.context_snapshot["build_spec"]`).
- **Telegram pairing (blocker B)** — cleared when the operator `/start`ed `@ledger_evo_test_bot`.

**Delivery PROVEN** (#2804 docs): kickstart fire 16:32 PDT → `BRIEFING_SENT` → gateway `outbound send ok messageId=4` to the operator's Telegram DM + `briefing-runs/2026-06-12.json` written. The 16:32 fire is off-window, so today's 07:00 row is honestly `unmeasurable`; **first true `on_time` row = 06-13 07:00** (the briefing is now IN delivery-monitor scope — the layer-3 fix). `ledger` stays the U2-drill subject through 06-18, then retires (META actions the teardown = a U5 retirement dry-run). Companion finds during the proof: security 644-token leak → #2781; cost-to-first-value ($5.65 day-1, no cap) → #2787 (→ new-bot cost-defaults design-sync, open). U2 watcher half also PROVEN (#2783).

### Cost-defaults slice — CLOSED 2026-06-14 (finding [#2787](finding-new-bot-activation-cost-2026-06-12.md))

All four accepted fixes (A–D) + the UI rider shipped and were reviewed sound. The `$30` day-1 setup tax is now governed at its source — the daily cap arms for the new bot, provisioning has a real one-time ceiling enforced *at dispatch*, the dominant spenders moved off the `power` role, the redundant same-day re-audit is deferred, and the operator sees and accepts a cost-to-first-value projection at creation:

- **A — [#2816](https://github.com/evolve-ops/evolve/pull/2816):** graduated new-bot daily hard cap ($10/day first 7d → $5/day; precedence = explicit per-bot override > graduated new-bot > pod default). Arms a real L1 breaker for the bot during its window. Independently reviewed sound.
- **C — [#2817](https://github.com/evolve-ops/evolve/pull/2817):** forge provisioning-build + first-audit routed from the `power` → `standard` role (~4–5× saving; framed as a **role** change, not a model-name swap — no-provider-literals). Independently reviewed sound.
- **D — [#2822](https://github.com/evolve-ops/evolve/pull/2822):** day-1 first tier-3 audit deferred to +7d / first real usage (skip the same-day adversarial re-audit of just-built-clean apps). Post-merge review **SOUND** — deferral is time-bounded on always-cadence-due apps so the hourly sweep guarantees it runs past +7d; `force_due` + `only_apps` bypass so real/manual audits are never deferred. (Superseded an empty first merge #2815.)
- **B — [#2852](https://github.com/evolve-ops/evolve/pull/2852):** $12 one-time provisioning ceiling that **actually governs** provisioning (new `packages/analyzer/provisioning_budget.py`). Auditor-grade review **SOUND** — a real enforced stop *at dispatch* for both spenders (forge gate in `run_forge_job` before any LLM/manifest write; audit gate before `run_tier3_for_app`); window spend sums all window turns from the authoritative ledger; the gate fires before any write so no half-built app, and a Signal is always emitted on trip; complementary to A with no double-count. (Recovered an empty scaffold merge #2846.) This is "the real fix" — A alone could not govern provisioning because the spenders run on admin-side daemons outside the L1 cost-breaker scope.
- **Wave-3 UI — [#2870](https://github.com/evolve-ops/evolve/pull/2870):** wizard "cost-to-first-value" panel shown + accepted at the creation/commit screen via `/api/wizard/cost-projection`, which reads the **same resolvers the enforcers use** (`better_engine_config.new_bot_cost_projection()` + `spend_alert.daily_spend_alert_threshold_usd`). The SPA renders server-resolved numbers with **zero dollar literals** (no drift from enforcement), fail-open, theme-token. META-verified sound on the no-hardcoded-literals crux.

**Three by-design residuals of B — ACCEPTED no-fix** (recorded so they're not re-litigated):
1. **Concurrent-dispatch overshoot** — a second provisioning path for the same bot inside the window (e.g. briefing_activation overlapping the pack worker) each passes an independent read-then-spend check → overshoot of ~one build per concurrent path; still bounded, never the unbounded $30 runaway. Atomic reservation judged not worth the weight.
2. **Audit-side ceiling arm mostly shadowed by D** within the provisioning window (live only on D's fail-open path; the daily-breaker arm is always live) — coherent defense-in-depth, not dead code.
3. **Budget-pause and time-defer share the `apps_first_audit_deferred` counter** (trail entries distinguish them) — trivial metrics conflation.
