# EDR — Evolve Development Rig (design memo)

**Status:** v1 design memo · source of truth for the EDR aspect
**Date:** 2026-06-11
**Author:** EDR META coordinator (design-sync with the operator, 2026-06-11)
**Reads-before:** [`etr-postmortem-recovery-2026-06-11.md`](etr-postmortem-recovery-2026-06-11.md)
(the headstone) · the market-intelligence KB (`docs/market-intelligence/`) ·
seed memory `edr-development-rig`
**Companion:** [`best-practices-agentic-sdlc-2026-06-11.md`](best-practices-agentic-sdlc-2026-06-11.md)
(external best-practices scan, #2745, merged) — its findings are folded into §5–§9.
[`design-edr-tiered-oversight-2026-06-11.md`](design-edr-tiered-oversight-2026-06-11.md)
(the dev-pod frame: tiered oversight as coordination state; Q8–Q11) — shapes how
P1/P2 artifacts are designed, changes nothing in P1 scope.

> This memo is the durable design record. A fresh EDR META must be able to
> reconstruct the whole picture from this file + the memory entry alone. When a
> decision is made, update this file; when a lesson is learned, write memory.

---

## 0. TL;DR

EDR is the **meta-development system that builds and improves Evolve itself** —
a dev-environment-only sibling to the shipped product, not a pod feature. It
recomposes Evolve's *own* RSI loop primitives (signal store → arbiter/generators
→ verify daemon, now importable since `evolve-analyzer` was packaged) over a new
domain (Evolve's own development) with a new actuator (**Claude Code**, not
openclaw bots). It ingests dev signals (GitHub issues, help tickets, feedback,
opt-in pod telemetry, CI failures, market intelligence), triages them — routing
each explicitly as **agent-able vs. human** — and drives remediation through
scoped Claude Code sessions, closing each loop only against a **falsifiable proof
artifact**. Every write flows through reviewable **safe-outputs**; never
auto-merge; auditor-grade review on the code-writing actuator path.

It is the successor to the **killed ETR (Evolve Test Rig)** and is designed
explicitly *not to repeat ETR's failure modes* (§2).

---

## 1. The reframe that organizes everything

There are **two Evolves**:

| | **The product** | **The meta-development system (EDR)** |
|---|---|---|
| Deploys to | every pod | only the dev environment |
| Governed by | the Plex test / mildly-tech-capable constraint | can be as technical as it needs to be |
| Audience | households, small ops | Evolve's own maintainers |
| Actuator | openclaw bots (Slack/Telegram/…) | **Claude Code** (writes code, opens PRs) |
| Trust model | a bot sending messages | **a system that writes code against the Evolve repo** |
| Data sources | a household's own context | issues, tickets, feedback, aggregate opt-in telemetry, CI, market-intel |

"Regular vs. developer version" resolves **not** as two product editions, but as:
the product (shipped) **+** EDR (a dev-env-only sibling that *depends on the
product's libraries*).

**The product's "Issues tab" is a stub pointing this way — but it is NOT the
rig.** It must be rescoped to what a *pod operator* needs (see their own pod's
problems; file upstream), never to the development rig. EDR has no pod surface.

---

## 2. What ETR taught us (design constraints, not optional)

ETR ran ~3 weeks (2026-04-19 → last tick 2026-05-05), was retired 2026-05-22
(−55,112/+13). Full story in the post-mortem. The six lessons become **hard
design constraints** here:

| ETR failure | EDR constraint |
|---|---|
| Auto-fix produced **zero successful fixes in 10 days** and was the first thing cut. | **No headline auto-fix.** Any change-application starts measure-only / propose-only and earns autonomy from a *tracked success rate* — mirroring the RSI autonomy ladder. The first proof is a human-gated PR, not an auto-merge. |
| Detect-only catalogs **went empty and stayed empty** — coverage depended on humans hand-authoring. | **Coverage must be self-sustaining.** EDR's work-list is *fed by real signals* (issues/tickets/CI/telemetry that arrive on their own), never a queue a human must remember to fill. If the inbox is empty it's because the world is quiet, not because nobody authored. |
| The job was redundant with **Signals + monitors + Proposals**. | **Build ON the canonical observation layer, don't fork it.** EDR *consumes and produces* Signals and Proposals via the packaged libraries; it maintains **no parallel findings store and no parallel dashboard** (ETR's `issues/*` + Flask UI were the duplication). |
| `etr_polling.py` was built, tested, and **never wired to a caller**. | **Build the consumer first, or in lockstep.** No infrastructure ahead of a demonstrated consumer. |
| At death: ~7 framework docs + 9 specs for a system whose live function was "file a markdown issue." | **Size to demonstrated need; grow from use, not from roadmap.** Watch the doc-to-working-code ratio. (This memo is deliberately one file.) |
| ETR validated **none** of the grand "codebase improves itself" (L3) vision — it only ever ran L2 detection, briefly. | **Inherit no confidence.** The L3 ambition (a rig that writes code against Evolve) is *genuinely new and unproven*. Prove the smallest closed loop on a real issue before believing anything bigger. |

**What ETR got right and we keep:** the catalog-as-ACL model (authorization
established at review/merge time, not runtime); a forbidden-pattern gate + per-
call audit log + a kill switch; topology-in-config (no hardcoded hosts/bots);
and the clean escalation split (mechanical → cheaper model, judgment →
Opus/human). The failure was in *demand and feedback*, not in the safety design.

---

## 3. Invariants (non-negotiable)

1. **Not in the shipped product.** No dead affordances on pods
   (`product-defaults-in-code`). EDR ships no plist/plugin/UI that any pod loads;
   it is excluded from every pod deploy/install path, enforced by a guard
   (§9.G1). The code-writing actuator is a *different trust model* than a pod.
2. **Reuse `evolve-analyzer` primitives as libraries, never fork.**
   (`dont-reimplement-upstream`, `analyzer-packaged-compat-editable`.)
3. **Read-only by default.** Every write/PR flows through reviewable
   **safe-outputs**; **two-pass review** (`two-pass-review-workflow`); **never
   auto-merge** (`automerge-required-checks-only`); **auditor-grade review on the
   actuator/privileged paths** — construct the actual failure/attack string,
   don't eyeball.
4. **Every work item carries a falsifiable proof artifact before the loop
   closes.** No "looks fixed."
5. **Triage explicitly routes agent-able-vs-human.** Agent-able *if you know
   exactly how*; human *if it needs exploration*.
6. **The loop is Evolve's RSI loop, recomposed — not reinvented.** Ingest = signal
   store; triage = generator + arbiter; drive = the META/Workflow dispatch
   pattern; verify = verify daemon; route = the aspect registry.

---

## 4. Architecture

### 4.1 Separate package, reusing the libraries

EDR is **a separate project that imports Evolve's packaged libraries.** Phase 6.1
packaging is the unlock: `evolve-analyzer` is a real, installable package, so EDR
*imports* the signal store / proposal store / arbiter / generator framework /
verify daemon rather than copying them.

```
┌──────────────────────────── the Evolve repo ────────────────────────────┐
│                                                                          │
│  packages/analyzer  (evolve-analyzer)   ← the RSI primitives, packaged   │
│  packages/admin     (evolve-admin)      ← the product's admin surface    │
│  packages/plugin                        ← the openclaw plugin (TS)       │
│  ─────────────────────────────────────────────────────────────────────  │
│  edr/  (NEW — dev-env-only)                                              │
│    • depends on evolve-analyzer (installed package, compat-editable)     │
│    • ingestion adapters (GitHub issues, help desk, feedback, CI, …)     │
│    • the Claude Code actuator bridge                                     │
│    • EDR's own charter(s) for triage generators                         │
│    • NO plist / NO plugin / NO pod-install path                         │
└──────────────────────────────────────────────────────────────────────────┘
```

**Same-repo vs. separate-repo — recommendation: same-repo, fenced.** EDR's job is
to act *on this repo* (open PRs against Evolve) and it depends on
`evolve-analyzer` which lives here, so a separate repo would have to vendor the
library and check this one out anyway. Living in-repo keeps EDR co-evolving with
the libraries it imports and keeps it agent-legible/version-controlled. The
"not-shipped" invariant is satisfied structurally: EDR is inert files on a pod
(like `docs/`) because no daemon/plugin/affordance loads it, and a guard fails CI
if any pod-install path references `edr/`. **DECIDED 2026-06-11 (the operator):
same-repo, fenced — root-level `edr/`.**

### 4.2 Reuse map (what's recomposed vs. what's novel)

| Loop stage | Reused primitive (import, don't fork) |
|---|---|
| **Ingest** (dedupe dev signals) | signal store `observe()` — find-or-create + signature dedup (`alerts-signal-store`) |
| **Triage** (classify, severity, agent-able-vs-human, route) | a **generator** (charter `subscribes_to:`) + the arbiter Proposal lifecycle |
| **Drive** (scope + dispatch fixes) | the **META-session dispatch pattern** + Workflow + bg agents (`background-agents-context-wedge`, `orchestration-flow-rules`) |
| **Verify & close** (against the proof artifact) | the **verify daemon** + authority/track-record gating |
| **Route ownership** | the **aspect registry** (META-aspect-registry) |

**Novel engineering (the only genuinely new code):**
1. The **Claude Code actuator bridge** (§5) — approved work-item → scoped,
   two-pass-reviewed session → PR reported back against a proof artifact.
2. **Ingestion adapters** (§4.3) — GitHub issues, help desk, feedback, telemetry,
   CI, market-intel — each normalizing an external source into a Signal.

Everything else is recomposition.

### 4.3 Ingestion adapters

Each adapter is a thin normalizer: *external source → Signal* via
`signals.store.observe()` (signature dedup + reopen window). Sweep-style sources
call `sweep_resolve()` so cleared conditions auto-archive.

| Adapter | Source | Signal `type` (proposed) | Notes |
|---|---|---|---|
| `github_issues` | the repo's GitHub Issues | `dev_issue` | The **"hello world"** ingestion — start here. Label/state → signal state. |
| `ci_failures` | failed CI runs (gh API) | `dev_ci_failure` | High-signal, self-feeding; a natural early adapter (the failure *is* the proof of a problem). |
| `help_desk` | support tickets | `dev_support_ticket` | Source TBD (Q3). |
| `user_feedback` | rejected-proposal feedback, in-product feedback | `dev_feedback` | Reuses the existing `signals/feedback.jsonl` shape. |
| `pod_telemetry` | aggregate **opt-in** pod telemetry | `dev_telemetry_pattern` | Opt-in only; privacy-by-architecture (`per-bot-inference`, `user-observation-optout`). A cross-pod pattern, not one pod's data. (Q4) |
| `market_intel` | the market-intelligence KB + scans | `dev_market_signal` | Lowest-frequency; feeds *positioning-aware* spec work, not bug-fixing. |

Adapters are read-only and additive. Ship them **one at a time, consumer-first** —
no adapter lands without a triage path that acts on its signals (ETR lesson #4).

### 4.4 Deployment & end-state

- **v1 deploy = none.** EDR runs from a **local maintainer checkout** (DECIDED
  2026-06-11, the operator — Q2), invoked manually / on a light cadence. No pod deploy
  mechanism, no launchd daemon a pod would load. Heavier runtimes (a
  dedicated dev box, or CI-triggered — the dogfood direction) are *earned once the
  loop is proven*, not built up front.
- **End-state aspiration: dogfood EDR as a hardened "Evolve-dev pod"** — Evolve
  managing its own development the way it manages a household. This is **gated on
  solving the high-privilege code-writing actuator's trust model** (§5.4) and is
  explicitly *not* a v1 goal. We earn it. The conceptual frame that makes this
  literal — the dev workforce (coordinator + worker sessions) as a third managed
  bot population, with typed coordination state, per-tier earned autonomy, and a
  precedent store — is the tiered-oversight companion doc
  ([`design-edr-tiered-oversight-2026-06-11.md`](design-edr-tiered-oversight-2026-06-11.md)).

---

## 5. The Claude Code actuator bridge (the novel core)

This is the one piece with no existing analogue. It is the interface between *an
approved EDR work-item* and *a dispatched Claude Code session that produces a
reviewable PR*.

### 5.1 Contract

**Input:** an approved Proposal (status `approved_*`) whose triage marked it
`agent_able` and attached: a scoped brief, the target aspect/owner, and a
**declared proof artifact** (the falsifiable check that must pass to close).

**The bridge guarantees:**
- The session is **scoped to one bite** (~30 min / one subsystem;
  `orchestration-flow-rules`, `background-agents-context-wedge`).
- It is a **single bounded session per code work-item — not a multi-agent swarm.**
  The scan (#2745) and Anthropic's orchestrator-worker write-up are explicit that
  multi-agent fan-out wins on breadth-first *research* but is a poor fit for
  *coding*, which needs shared context. EDR fans out lead+subagents for
  **ingest / triage / research**; the **code actuator stays one session.**
- It runs **read-only by default**; the only mutation it can emit is a **PR**
  (safe-outputs model, §8) — never a direct write to main, never a merge.
- It orders an **immediate empty-commit push + incremental pushes** (death costs a
  relaunch, not the work).
- On completion it reports back a **typed result**: `{pr_url, proof_artifact,
  proof_status, review_status}` — nothing else re-enters EDR's state.

**Output → verify stage:** the verify daemon checks the PR against the declared
proof artifact; only a *verified* proof transitions the Proposal toward closed.

#### 5.1.1 v1 bridge decisions (2026-06-12, operator-approved)

1. **Dispatch mechanics.** The bridge generates the scoped brief and launches a
   **headless session in an isolated git worktree** (immediate empty-commit
   push, incremental pushes, PR-open only) — automated mechanics, but only ever
   from an *approved* Proposal. Build split: P1.3a = lifecycle + brief
   generation; P1.3b = the dispatch runner + result capture.
2. **The approval gate (R1).** A human approves each dispatch via a small CLI
   (`python -m edr.bridge approve <proposal-id>`) that moves the Proposal
   through the real arbiter status lifecycle; the bridge only picks up approved
   ones. A human-routed item (`agent_able=False`) cannot be "approved" into the
   agent lane — that is a triage re-run after relabeling, not an approval.
3. **The honest G3 gap in v1.** On a local checkout the dispatched session
   inherits the operator's `gh` auth, which *can* merge. v1 enforcement is
   **contractual + verified**: the brief forbids merging, and the bridge checks
   post-hoc that the PR was not merged by the session — any violation is flagged
   loudly in the typed result. A true PR-only scoped token is a **P2 hardening
   item** (G3 then becomes structural).
4. **Result persistence.** The typed result lands on the Proposal itself —
   `provenance.signals["actuator"]` (mirroring the triage slot convention) with
   `{pr_url, proof_artifact, proof_status, review_status, g3_merge_check}` —
   and the Proposal moves to `applied`, which is exactly the state the verify
   stage (P1.4) owns.

### 5.2 The proof-artifact discipline

A work-item cannot close without a **falsifiable** proof artifact declared *at
triage time* and verified *after the PR*. Examples:
- bug fix → a failing test that now passes (the test is written first, red, then
  green);
- CI failure → the previously-red job is green on the PR;
- feature → a named acceptance check / e2e scenario;
- doc → a structural lint / link check (weak proof — flagged as such).

This is the single most important anti-ETR mechanism: it forbids "auto-fix that
produces zero successful fixes" from *looking* successful.

**The verifier is independent and blind to the actuator's reasoning.** The verify
step checks the PR against the *declared proof artifact / spec* — it must NOT be
fed the actuator's chain-of-thought or implementation rationale ("providing
implementation code to verification agents too early biases their outputs" —
Coordinator-Implementor-Verifier, Augment 2026, via #2745). A *separate* session
runs the proof; a clean reasoning trail is not a passing proof.

### 5.3 The autonomy ladder (earned, not assumed)

Mirrors the RSI arbiter's track-record gating. EDR does **not** start with
autonomy:

| Rung | What EDR may do | Gate to advance |
|---|---|---|
| **R0 — observe** | ingest + triage + *propose* (write a Proposal with a brief + proof artifact). No dispatch. | always on |
| **R1 — assist** | dispatch a Claude Code session that opens a PR; **human reviews + merges every time**. | a human opts a class of work in |
| **R2 — gated-apply** | same, but EDR may auto-*request-review* / auto-run the proof; human still merges. | a tracked success rate on R1 for that work-class |
| **R3 — dogfood-pod** | the end-state; the actuator trust model is solved. | explicit operator appetite + a trust-model design (§5.4) |

**Promotion is excluded from all auto-lanes** (same rule as the product's RSI
ladder). v1 lives at **R0→R1**.

### 5.4 The actuator trust model (the gating problem for R3)

A system that can write code against its own repo and open PRs is a privileged
actuator. Before any dogfood-pod (R3), we owe a written trust model: least-
privilege tokens (PR-open scope only, never merge/admin); branch protection that
*requires* human merge; the safe-outputs sandbox; per-dispatch audit log; a kill
switch; and auditor-grade two-pass review on the bridge code itself. **Deferred
to its own memo when R3 appetite is real.** (Q5.)

---

## 6. Triage & routing model

Triage is a **generator** (charter `subscribes_to: [dev_issue, dev_ci_failure,
…]`) that, per incoming Signal, writes a Proposal carrying:

- **classification** — bug / feature / dup / noise / needs-design-decision;
- **severity** — reuse the signal severity scale;
- **agent-able-vs-human** — the routing decision (§6.1);
- **target aspect/owner** — from the aspect registry;
- **a declared proof artifact** (required for agent-able items);
- **`motivating_signals[]`** — the link back to the ingested Signal(s).

Dedup is free: the signal store's signature index collapses duplicate inbound
issues into one active signal; the arbiter dedups duplicate Proposals.

### 6.1 The agent-able-vs-human decision (load-bearing)

> **Agent-able if you know exactly how to fix it; human if it needs exploration.**

**The `agent-able` verdict DEFAULTS TO HUMAN.** Frontier models score ~23% on
SWE-bench *Pro* (realistic multi-file, professional repos) vs. >70% on Verified
(Scale / arXiv 2509.16941, 2025, via #2745) — the genuinely agent-able set is far
smaller than benchmark headlines suggest. Triage routes to the actuator *only*
when the **fix locus and method are already known**; anything needing exploration
is a human / aspect-META item. Optimistic mis-routing is the expensive error, so
the default is conservative — EDR earns a wider agent-able envelope from a tracked
landing-rate (§5.3), it does not assume one.

- **Agent-able** → the brief can state the change precisely + a falsifiable proof
  artifact exists → dispatch a Claude Code session (R1+). Examples: a flaky test
  with a known cause; a CI failure with an obvious fix; a typo/lint/dep bump; a
  well-specified small feature.
- **Human** → the fix needs design judgment, architecture, or exploration → route
  to a human/aspect-META as a *triaged, enriched* item (not auto-dispatched).
  Examples: anything touching a privileged path; ambiguous repros; cross-cutting
  refactors; design decisions.

This routing is the heart of EDR's value: it spends Claude Code on the mechanical
majority and human/Opus attention on judgment — ETR's "clean escalation split,"
done right and fed by real signals.

The triage generator stays **cheap by default** (`rsi-low-cost-preference`): pure-
Python classification first, LLM escalation only when the signal is ambiguous.

---

## 7. Safe-outputs & review gates

The trust spine. The external scan (#2745) confirms this **is** the industry
consensus model, not a contrarian bet: GitHub Agentic Workflows (technical
preview, Feb 2026) runs agents **read-only** and routes mutations through
**separate, permission-controlled safe-outputs jobs**, and "PRs are never merged
automatically." We are building to the floor, not above it. **Staleness caveat:**
GitHub Agentic Workflows is still technical-preview — treat specific config keys
(`safe-outputs`, `staged:`, handler `max:`) as *directional* and re-verify
against current docs before building against them; the qualitative patterns are
well-corroborated.

1. **Read-only by default.** The actuator session reads freely; the *only* write
   it can emit is a proposed PR.
2. **Safe-outputs envelope.** A mutation is a *proposed, reviewable artifact*
   (a PR), never an applied change. The reviewer (human + independent agent) is
   the gate.
3. **Two-pass review before merge** (`two-pass-review-workflow`): the build
   session self-reviews (silent-failure checklist), then an *independent* review
   pass. Privileged paths (the actuator bridge, any config/auth/sudoers/deploy
   surface) get **auditor-grade** review — construct the real failure string.
4. **Never auto-merge** (`automerge-required-checks-only`): poll all checks via
   JSON, then a human merges. No `gh pr merge --auto`.
5. **Proof artifact verified before close** (§5.2).
6. **Per-dispatch audit log + kill switch** (ETR's sound primitive, kept).

---

## 8. Phased build plan (each phase ships a proof artifact)

Sized to demonstrated need; each phase is independently useful and is *proven*
before the next. **Build the consumer first** at every step.

| Phase | Scope | Proof artifact (the falsifiable close condition) |
|---|---|---|
| **P0 — scaffold** | The `edr/` package: depends on `evolve-analyzer` (compat-editable), imports the signal + proposal stores, a smoke test that writes & reads one EDR Signal and one Proposal through the real libraries. The §9.G1 not-shipped guard. | A test that round-trips a Signal→Proposal through the imported libraries **green in CI**, and the guard fails when a pod-install path references `edr/`. |
| **P1 — the first closed loop** *(= terminal DoD, §10)* | `github_issues` adapter → triage generator → **manually-approved** dispatch of a Claude Code session on **one real GitHub issue** → PR opened → verify against the declared proof artifact → loop closed. Human review gate intact; R1 autonomy only. | **A real GitHub issue, end-to-end: ingested → triaged (agent-able) → a scoped Claude Code session dispatched → a PR opened → its declared proof artifact verifies → the loop closes — with the human/safe-output review gates intact.** Recorded with the issue#, the PR#, and the proof. |
| **P2 — self-feeding coverage** | Add `ci_failures` adapter (high-signal, self-feeding); the triage generator subscribes; a second real loop closes from a CI failure with **no human authoring the work-item**. | A CI-failure-originated loop closes end-to-end with zero hand-authored catalog entries (directly refutes ETR failure #2). |
| **P3 — autonomy laddering** | Track-record gating: a work-class earns R2 (auto-request-review / auto-run-proof; human still merges) from a measured success rate. | A documented success-rate threshold crossed for one work-class, with the ladder transition logged. |
| **P4 — breadth** | More adapters (help_desk, user_feedback, pod_telemetry, market_intel), consumer-first, one at a time. | Each adapter lands only with a triage path that acts on it. |
| **P5 — dogfood-pod (R3)** | The end-state, gated on the actuator trust-model memo (§5.4) + explicit operator appetite. | Out of v1 scope; its own design memo first. |

**No phase begins before its predecessor's proof artifact verifies.** This is the
ETR-bloat antidote in procedural form.

---

## 9. Guards & open questions

### Guards (CI-enforced where marked)
- **G1 (not-shipped):** a CI guard fails if any pod deploy/install path
  (`deploy.py`, plist/launchd renderers, the plugin) references `edr/`. *Enforces
  invariant #1 structurally.* **[to build in P0]**
- **G2 (library-reuse):** EDR imports `evolve-analyzer`; it must not copy its
  modules. A lint/test asserts no forked analyzer code under `edr/`.
- **G3 (no auto-merge):** the actuator bridge has no merge capability; a test
  asserts the dispatched session's token/credential scope excludes merge.
- **G4 (liveness / don't-go-dark):** a monitor fires when **ingest throughput
  drops to zero** (the rig stopped *receiving* signals — the external failure
  class that emptied ETR's catalogs), and EDR tracks **actuator landing-rate**
  (PRs that close on a verified proof ÷ dispatched) as a first-class health
  metric (scan #2745). *Built alongside the first adapter, not ahead of it.*

### Open questions (drive each to resolution with the operator at the phase that needs it)
- **Q1 — same-repo vs. separate-repo** for `edr/`. **RESOLVED 2026-06-11 (the operator):
  same-repo, fenced, root-level `edr/` (§4.1).**
- **Q2 — where v1 runs.** **RESOLVED 2026-06-11 (the operator): a local maintainer
  checkout, invoked manually / on a light cadence (§4.4).**
- **Q3 — help-desk source** (what is the actual ticket system?). *P4.*
- **Q4 — pod-telemetry opt-in shape** (what aggregate signal, with what consent
  surface?). *P4; must satisfy the privacy invariants.*
- **Q5 — the actuator trust model** for R3 (own memo). *P5.*
- **Q6 — relationship to the product's RSI/Better Engine** — EDR applies the
  *same loop to a different domain*; confirm there's no surface where the two
  should share more than the libraries. *Design-level; revisit at P2.*
- **Q7 — the product's "Issues tab" rescope** — what a pod *operator* needs (see
  their pod's problems, file upstream) is a *separate, product-side* work item.
  Should EDR own drafting that rescope spec, or hand it to the product roadmap?
- **Q8–Q11 — tiered oversight** (precedent-store shape, operator-digest surface,
  coordinator-charter record, v1 event loop) — see the
  [tiered-oversight companion](design-edr-tiered-oversight-2026-06-11.md) §7;
  each resolves at the phasing trigger named there (§6).

---

## 10. Definition of done

**Terminal DoD (v1):** Phase P1's proof artifact verifies —

> **One real GitHub issue, end-to-end: ingested into the signal store → triaged
> (classified, severity, marked agent-able, routed, with a declared falsifiable
> proof artifact) → a scoped Claude Code session dispatched as the actuator → a
> PR opened → the PR's proof artifact verified by the verify path → the loop
> closed — with the human / safe-output review gates intact (read-only default,
> two-pass review, no auto-merge).**

Recorded with the concrete issue#, PR#, and proof. That single closed loop —
fed by a real signal, not a hand-authored catalog; closing on a verified proof,
not a "looks fixed" — is the thing ETR never achieved and the bar EDR must clear
before any broader ambition is believed.

### ✅ ACHIEVED 2026-06-12 — the v1 DoD record

| Stage | Artifact |
|---|---|
| Issue (real) | #2658 — alert dismissal on Reports → Alerts didn't clear the Pod Health surface |
| Ingest | `dev_issue` Signal via the real `signals.store.observe()` (6 real issues; signature dedup live) |
| Triage | Proposal `edr-triage-gh-2658` — the only agent-able verdict of 6 (operator label `edr:agent-able` + executable `Proof:` line); 5 routed human |
| Approve (R1, human) | `pending → approved_human` via `arbiter.state_machine.transition`, actor `user` |
| Dispatch | one headless worker session, isolated worktree, branch `edr/gh-2658-…` |
| PR | #2789 — fix direction (a), +422/−2, proof shown red→green in the body |
| Two-pass review | coordinator pass caught 2 new silent-exception swallows (the repo's except-pass ratchet went red, 13→15); fixed in review; ratchet green |
| Independent verify | `verified` — proof red on origin/main (exit 4), green on the PR head (exit 0), in clean worktrees; re-run after the review commit |
| G3 merge-check | `clean` (worker never merged); merge performed by the human operator |
| Close | `applied → succeeded`, close evidence carries `merged_by` + timestamps |
| Lifecycle cleanup | issue closed → next adapter sweep auto-resolved the Signal (`resolved 1`, zero hand-curation) |

The maiden run also *demonstrated the gates working*, not just passing: the
ratchet caught real worker debt before merge, and `eligible: 0` held until the
human approval landed. CI on the worker PR: 20/20.

**Beyond v1:** self-feeding coverage (P2), earned autonomy (P3), breadth (P4),
and — only when its trust model is solved and the operator wants it — the
dogfood-pod end-state (P5).

---

## 11. Positioning note (per the industry-intelligence practice)

EDR is dev-env-only, so its "positioning" is mostly internal: it is the engine
that lets Evolve answer the market's **#1 complaint (reliability)** and **#2
(security)** *faster* — by turning real issues/CI/feedback into verified fixes
with less human mechanical toil. It also **dogfoods Evolve's own thesis**: the
RSI loop that improves a household's bots is the same loop, recomposed, improving
Evolve itself. The market frames multi-agent work purely by *isolation
boundaries* and "has limited detail on inter-agent state synchronization"
(zenvanriel, 2026-06-11) — EDR's whole value is in the *crossing* (signal store +
proposal store + proof-gated dispatch), which is exactly Evolve's uncontested
layer. If EDR ever surfaces externally, that is the story: *not* "we have agents
that write code" (everyone claims that), but "we have a **proof-gated, never-
auto-merged, agent-able-vs-human-routed** loop that actually closes."
