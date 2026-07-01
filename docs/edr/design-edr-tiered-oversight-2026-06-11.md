# EDR design — tiered oversight: the development organization as a pod

**Status:** design frame · companion to [`design-edr-2026-06-11.md`](design-edr-2026-06-11.md)
**Date:** 2026-06-11 (design-sync with the operator)
**What this is:** the conceptual frame for "bots managing bots" — multiple tiers
of oversight so the operator converses at a high level while lower tiers iterate
on details — and how EDR codifies that structure. **What this is not:** a build
plan. It changes nothing in the memo's P1 scope; it shapes *how* P1/P2 artifacts
are designed so the structure can grow out of them without a framework being
built up front.

> **Doc-ratio honesty (ETR lesson #5):** `docs/edr/` now carries four documents
> against one scaffold package. This file earns its place only as a *decision
> record* — the decisions here (tier model, three moves, anti-goals, phasing
> hooks) would otherwise live in a conversation and be lost. No further EDR
> design docs until P1 ships working code.

---

## 1. The frame

Evolve's product thesis is "a system that manages bots": observe them, propose
improvements, gate changes through approval audiences, verify outcomes. Tiered
oversight is that same loop pointed at a **third bot population — the
development workforce itself**:

| Bot population | Managed by | Coordination state |
|---|---|---|
| Household bots | a pod (the product) | signal store, proposals, profiles, conduct |
| Pods | the hub ("Pods", multi-pod design) | typed artifacts, deposit/apply |
| **Dev workforce** (coordinator + worker sessions) | **EDR** | **this document's subject** |

Coordination is fractal — channel → bot → pod → *session → aspect → operator* —
and the market's own framing (recorded in the intelligence KB, 2026-06-11)
concedes that isolation is the easy part: the dominant community guide admits
"limited detail on inter-agent state synchronization." **The crossing — typed
coordination state between tiers — is the valuable layer.** The META structure
today has the isolation (separate sessions); its coordination state is informal
(markdown registry + memory + the operator's head). EDR's job here is to make
that state typed and durable, using primitives it already imports.

The strategic bonus: this makes the P5 end-state ("dogfood EDR as an Evolve-dev
pod") literal rather than metaphorical. The dev workforce *is* a pod; EDR is its
Evolve. If it works, the story is not "we have agents that write code" (everyone
claims that) but "we run our development organization on the same managed-bot
substrate we ship."

## 2. The tier model (fixed depth)

| Tier | Who | Holds | Does | Must never |
|---|---|---|---|---|
| **L0 — operator** | the human | intent, values, judgment | converses with L1; rules on escalations; sets/changes charters | be required for mechanical decisions a charter already answers |
| **L1 — coordinator** | META sessions (one per aspect) | the aspect's holistic view (spec + memory, never the conversation) | translates intent into scoped work-items; adjudicates; tracks via artifacts | execute heavy work in its own context; merge without review |
| **L2 — worker** | dispatched build/review/research sessions | one bite | executes against a typed brief; pushes artifacts; reports a typed result | expand scope; merge; dispatch sub-work-items of its own |

(L3 — subagents *within* a worker — belongs to the harness, not to EDR. See
anti-goal A4.)

**Depth is fixed at these three tiers.** Each hop costs latency and information
loss, and the "Dream Team" anti-pattern (intelligence KB) applies doubly to
management layers: the temptation to add tiers because spawning is cheap. When
coordination strains, the fix is *better state between existing tiers*, never a
new tier. Revisit only with evidence that state-quality improvements have hit a
ceiling.

**Escalation is a typed edge, not a vibe.** A lower tier escalates on exactly:
`blocked-on-judgment` (needs values/taste), `invariant-conflict` (two charter
rules collide), `budget-exceeded` (bite outgrew its scope), `scope-growth` (the
fix is bigger than the brief). Everything else resolves at its own tier. These
map onto shapes the proposal lifecycle already has (`needs-design-decision`,
approval audiences — the three-user-type routing, recomposed as
operator-judgment / coordinator-adjudicable / worker-autonomous).

## 3. The demand signal (what's brittle today)

The META structure works at current scale, but everything load-bearing is prose
and convention:

1. **Mandates live in hand-crafted kickoff prompts** and a markdown registry
   row. Conventions drift; every new META is artisanal.
2. **Dispatches are prose briefs; results are prose summaries.** There is no
   ledger of dispatched-work → outcome, so nothing can be measured.
3. **Liveness is manual** (`git ls-remote` polling; the ~30-min-wedge rule is a
   hook warning, not a monitor). Coordination is pull-only.
4. **Escalation is uncodified.** When a worker's problem warrants the
   coordinator, or the coordinator's warrants the operator, is judgment-by-feel.
5. **Track record is unmeasured**, so delegation can never be *earned* — every
   dispatch needs the same babysitting forever. This is the real ceiling on
   "converse at a high level": staying high-level requires trusting the tier
   below *differentially*, and trust requires measurement.

## 4. The three moves

### Move 1 — typed coordination state (replaces prose-and-convention)

- **The work ledger is the proposal store.** Every dispatch is a Proposal:
  typed brief, declared proof artifact, lifecycle status,
  `motivating_signals[]`. The coordinator dispatching work *is* writing a
  Proposal; the actuator bridge picking it up *is* the worker tier consuming
  the ledger. The result returns as the typed actuator contract
  (`{pr_url, proof_artifact, proof_status, review_status}` — memo §5.1). The
  hierarchy gets a ledger, not a transcript.
- **Workforce telemetry is the signal store.** Session-dispatched,
  branch-pushed, wedged (no commits ~30 min), PR-opened, proof-passed/failed,
  review-verdict become Signals (producer: the bridge + a small watcher). The
  G4 liveness guard (memo §9) *requires* this anyway — ingest-throughput and
  actuator landing-rate are computed from exactly these events.
- **Mandates are charters-as-data.** The generator charter concept (immutable,
  fingerprinted, `subscribes_to`, track record) extends to a **coordinator
  charter**: an aspect's scope, invariants, escalation rules, and autonomy rung
  *per work-class*, as data instead of a registry row + kickoff prose. The org
  chart becomes inspectable state. (Open question Q10: extend `GeneratorRecord`
  or a new record type.)

### Move 2 — earned autonomy per tier (replaces vibes-based delegation)

The memo's R0→R3 ladder generalizes: **each tier earns autonomy from the tier
above, per work-class, from a measured landing rate** — the same mechanism as
the product's autonomy ladder (U4.1), recomposed. "Agent-able defaults to
human" (memo §6.1) propagates upward as **"coordinator-able defaults to
operator"**: judgment calls route up until precedent (Move 3) or track record
justifies otherwise. Promotion-class decisions are excluded from all auto-lanes
at every tier, exactly as in the product.

Event-driven management comes free from an existing primitive: the product's
**signal-subscriber pattern** (a watcher on `signals/firing/` dispatching
subscribed generators within seconds). A worker's wedge-signal fires; a
coordinator-tier generator subscribes and reacts — relaunch from last
checkpoint, escalate, or re-scope. "Bots managing bots" becomes live rather
than polled, on a daemon shape that already runs in production. (v1 runs from a
local checkout, so the v1 form is a polling tick, not a daemon — the *shape* is
what carries.)

### Move 3 — compress judgment via precedent (the distinctive piece)

The honest constraint on any oversight hierarchy: **judgment doesn't pipeline.**
The operator is in the loop for values and taste, not mechanics. Tiers can
compress mechanical oversight; they cannot compress judgment. What compresses
judgment over time is **precedent**: every operator ruling — an escalation
resolved, a design decision, a rejected proposal with reasons — becomes a
durable, citable decision record. Escalations arrive with similar past rulings
attached; lower tiers resolve against precedent instead of re-asking; only
genuinely novel questions reach the operator. The memory system does this
informally today; EDR makes it first-class. **This — not delegation depth — is
the mechanism by which the tier you converse at rises.** (Open question Q8:
the precedent store's shape; it starts as a *convention* on decision records,
not a schema.)

## 5. Anti-goals

- **A1 — no added depth.** Three tiers. See §2.
- **A2 — coding stays single-session.** Fan out for research/triage/oversight;
  execution is one bounded worker per code work-item (scan finding, memo §5.1).
  Coordinators adjudicate; they never code.
- **A3 — no framework ahead of demand** (ETR lessons #4/#5). Nothing in this
  document is built speculatively; see §6 for what lands when.
- **A4 — don't reinvent the harness.** Claude Code is moving toward agent
  teams/orchestration natively. EDR's durable layer is the typed coordination
  state (signals, proposals, charters, track record, precedent) — *above* the
  session-spawning mechanics. If upstream ships coordination state itself,
  re-evaluate per the don't-reimplement discipline.
- **A5 — over-autonomy is the failure mode, not the goal.** The #4 ecosystem
  complaint (market-demand KB) and ETR's headline failure both warn the same
  way: autonomy is earned per work-class from measured landing rate, and
  defaults stay conservative at every tier.

## 6. Phasing hooks (how this lands without front-loading)

| When | What materializes | Why then |
|---|---|---|
| **P1 (as planned — unchanged)** | The actuator-bridge contract *is* the worker-tier API; the triage generator *is* the first coordinator-tier function. Design both knowing they're tier interfaces (typed, schema'd), nothing more. | P1 proves the loop one layer deep before any structure generalizes. |
| **P2 / G4** | The dispatch ledger + workforce-telemetry signals (dispatched/pushed/wedged/PR'd/verified) land as part of the liveness guard, which needs them regardless. | Consumer-first: the consumer (G4 + landing-rate metric) exists. |
| **When the 2nd coordinator-shaped generator exists** | The coordinator charter (charter-as-data, escalation edges, per-work-class rungs). | One instance is a special case; two is a pattern worth a type. |
| **From the first operator ruling onward** | Precedent records as a *convention* (decision docs / memory entries with a citable shape); schema only when retrieval-by-similarity is actually needed. | Precedent value compounds from day one; schema cost doesn't. |
| **When ≥2 tiers run through EDR** | The operator surface: a digest ("what landed, what's stuck, what awaits your judgment") — the morning-briefing pattern (the ecosystem's killer app, market-demand KB) applied to the dev pod. CLI/report first; never a pod UI. | Until then the registry + `gh pr list` suffice. |

## 7. Open questions (extends the memo's ledger)

- **Q8 — precedent store shape.** Convention-first (see §6); what minimal
  citable form do decision records take, and when does similarity-retrieval
  justify structure?
- **Q9 — operator digest surface.** CLI report, scheduled brief, or
  conversational (the L0↔L1 session itself renders it)? Decide at the ≥2-tier
  trigger.
- **Q10 — coordinator charter record.** Extend `GeneratorRecord`/charter.yaml,
  or a sibling record type? Decide at the 2nd-coordinator trigger.
- **Q11 — v1 event loop.** Local-checkout v1 has no daemon; what polling
  cadence approximates the signal-subscriber shape until a dev-box runtime is
  earned?
