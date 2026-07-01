# EDR Best-Practices Scan — Agentic SDLC / Issue-to-PR Rigs / Safe-Outputs

**Date:** 2026-06-11
**Author:** external best-practices scan (web research session)
**Purpose:** Ground the **EDR (Evolve Development Rig)** design memo in the
2025–2026 external state-of-practice for autonomous software-development loops.
EDR is a *dev-environment-only* meta-development system that builds and improves
the Evolve product itself — it is **NOT** shipped to end-user pods. This doc maps
the outside world's evidence onto EDR's loop stages and flags where the evidence
**sharpens or contradicts** our current design bets.

> **Scrub note.** This doc is public-facing. It uses role placeholders
> (`<the-rig>`, `<actuator>`, `<reviewer>`, `<operator>`) instead of any
> deployment-specific host/user/bot names. EDR's loop is described abstractly;
> nothing here hardcodes topology.

> **Evidence hygiene.** Every load-bearing claim carries an inline source and a
> date. Quotes from sources are marked with quotation marks; everything else is
> **[inference]** by the author of this scan. All URLs were fetched
> **2026-06-11** (see Sources index). Where a stat comes from a vendor blog
> rather than a peer-reviewed source, it is flagged — treat vendor numbers as
> directional, not gospel.

---

## TL;DR — the seven findings that move the EDR design

1. **The dominant industry pattern now matches EDR's invariants almost
   exactly.** GitHub Agentic Workflows (technical preview Feb 2026) is built on
   "agents run read-only and request actions via structured output, while
   separate permission-controlled jobs execute those requests" — i.e.
   read-only-by-default + safe-outputs + never-auto-merge. Our core bets are
   *consensus*, not contrarian. [GitHub Safe Outputs docs, fetched 2026-06-11]

2. **The hard number that should reset our autonomy expectations:** frontier
   models score **~23%** on SWE-bench Pro (realistic, multi-file, professional
   repos) versus **>70%** on SWE-bench Verified. [Scale/arXiv 2509.16941, Sep
   2025] The "agent-able" set is *much smaller* than the benchmark headlines
   suggest. EDR's triage must be aggressively conservative about what it routes
   to the actuator.

3. **The bottleneck shifts from writing to verifying.** Multiple practitioner
   sources independently note that an agent producing 5 PRs in the time a human
   writes 1 *drowns reviewers* — "the bottleneck shifts from writing to
   verifying." [ModelGate, OpenHands-vs-Devin, 2025] This validates EDR's
   proof-artifact-before-close invariant as load-bearing, not ceremony.

4. **Verifier independence is a named, established pattern** (Coordinator-
   Implementor-Verifier). The verifier must measure outputs "against the
   Coordinator's specification rather than the Implementor's reasoning trail";
   feeding implementation code to the verifier too early *biases* it. [Augment
   Code, 2026] → EDR's verify step must not see the actuator's reasoning.

5. **The local precedent is a textbook anti-pattern.** EDR's predecessor (ETR)
   was killed because its auto-fix path produced **zero successful auto-fixes in
   10 days** and its detect-only catalogs "went empty and stayed empty." [local:
   `docs/edr/etr-postmortem-recovery-2026-06-11.md`] The external literature
   names both failures: "auto-fix that never lands a fix" (over-autonomy without
   a proof gate) and "coverage going dark" (silent ingest starvation). EDR must
   instrument *against* both from day one.

6. **Multi-agent orchestration is a real win — but coding is the wrong shape for
   it.** Anthropic's orchestrator-worker beat single-agent by 90.2% on
   *breadth-first research*, but the same post warns multi-agent is a poor fit
   for "domains requiring shared context or heavy agent interdependencies (e.g.,
   most coding tasks)." [Anthropic, Jun 2025] → Use the lead+subagent fan-out for
   EDR's *ingest/triage/research*, not for the single-PR code-writing actuator.

7. **Most failures are data/context failures, not model failures.** "65% of
   enterprise AI agent failures are caused by context drift"; the harness "cannot
   detect governance signals it cannot interrogate." [Atlan, 2026 — vendor stat,
   directional] → EDR's signal ingest and the proof-artifact contract are where
   reliability is won or lost, not in actuator prompt-tuning.

---

## Stage 1 — Ingest (dev signals → the rig)

### External state of practice

The umbrella concept is **"Continuous AI"** — GitHub's framing for "the
integration of AI into the SDLC to automate tasks that previously required human
judgment," explicitly modeled on CI/CD. [GitHub Blog, *Automate repository tasks
with GitHub Agentic Workflows*, 2026; GitHub Agentic Workflows home, fetched
2026-06-11] The canonical ingest triggers in the GitHub model are repository
events: an issue opened, a comment, a scheduled cron, a workflow-dispatch. The
agent then "uses your repository's context to make decisions." [GitHub Blog,
2026]

A key design stance from GitHub: agentic workflows are **"augmentation, not
replacement"** — "they do not replace build, test, or release pipelines, and
their use cases largely do not overlap with deterministic CI/CD workflows."
[GitHub Blog, 2026] The deterministic pipeline stays; the agent layer sits beside
it for the judgment-shaped tasks.

The dominant **failure mode at ingest** is *context*: the Atlan anti-pattern
taxonomy puts ~55% of agent-harness failures in the "Data/Context Layer" —
"Stale Context/Context Drift" (data accurate at capture becomes obsolete with no
notification), "Schema Drift Blindness" (no mechanism to receive schema-change
signals), and "Missing Business Context" (technical schema without semantic
meaning). [Atlan, *13 Anti-Patterns*, fetched 2026-06-11 — vendor source]
Anthropic's own engineering guidance reinforces the fix: agents should
"summarize completed work phases and store essential information in external
memory before proceeding," because context degrades as windows fill. [Anthropic,
Jun 2025]

### → For EDR

- **Ingest is a typed-event boundary, not a scrape.** Mirror the GitHub model:
  each dev signal (GitHub issue, help ticket, user feedback, market-intel item,
  opt-in pod telemetry datum, CI failure) enters as a *typed, timestamped
  record* with provenance — not a free-text blob the actuator re-derives. This
  matches our existing Signal-store discipline (signature dedup, find-or-create,
  state machine). **[inference]**
- **Stamp freshness and provenance on every ingested signal** so triage can
  reason about staleness — the "context drift" failure is the single
  largest-cited failure class. [Atlan, 2026]
- **Keep deterministic CI as the substrate, EDR as the judgment layer beside
  it.** Do not let EDR try to *be* the test pipeline; let it *consume* CI
  failures as signals. [GitHub Blog, 2026 — "augmentation, not replacement"]
- **Watch for ingest going dark.** The single most damning fact in the ETR
  post-mortem is that the catalogs "went empty and stayed empty (last worker
  activity 2026-05-05)" and nobody noticed. [local post-mortem] EDR needs a
  liveness signal on ingest itself — a monitor that fires when the rig *stops
  receiving* signals, not just when a signal is bad.

---

## Stage 2 — Triage (route: bug/feature/dup/noise, severity, agent-able vs human)

### External state of practice

Issue triage is explicitly the **"hello world" of agentic workflows** —
"practical, immediately useful, relatively simple, and impactful." [GitHub,
*Meet the Workflows: Issue Triage*, fetched 2026-06-11] The reference design:
on a new issue, the agent "analyzes the title and body," does "research on the
issue in the context of the codebase," applies **one label from a constrained
allow-list** (`bug`, `feature`, `enhancement`, `documentation`, `question`,
`help-wanted`, `good-first-issue`), and posts "a friendly comment explaining the
label choice." It **skips issues that already have labels or are assigned**.
[GitHub, *Meet the Workflows*, 2026]

Two honesty notes from the primary source: the GitHub triage example "lacks
explicit deduplication logic, severity routing, or criteria distinguishing
agent-capable from human-requiring issues" — those are left to repo-specific
customization, and the post stresses "Generic agents are okay, but customized
ones are often a better fit." [GitHub, *Meet the Workflows*, 2026] **[inference:
the agent-able-vs-human classifier is exactly the part the reference designs
punt on — so it is EDR's differentiator and where we must do original work.]**

The general triage literature converges on a **confidence-threshold hybrid**:
"AI agents use confidence thresholds and fallback rules to route uncertain cases
to humans"; the AI "suggests severity, impact, owner, and team; a human reviews
and approves (or overrides)." [search synthesis across Moveworks, BigPanda,
Port, fetched 2026-06-11 — vendor sources, directional]

The deepest signal on **agent-able-vs-human** is indirect but the strongest in
this scan: the SWE-bench Verified→Pro gap. Models that resolve >70% of curated,
single-locus Verified issues collapse to ~23% on Pro, which has "larger changes,
across multiple files, sourced from professional repositories," with the
commercial subset under 20%. [Scale AI, *SWE-Bench Pro*; arXiv 2509.16941, Sep
2025] The practitioner heuristic that falls out: **an issue is "agent-able" when
the fix locus and method are already known; it needs a human when it requires
*exploration* to find the locus.**

### → For EDR

- **Triage must emit a binary `agent-able` verdict with an explicit reason
  string, and default to `human`.** The reference rigs don't solve this; the
  benchmark gap proves the cost of getting it wrong. Treat "the change spans
  multiple files / the locus is unknown / it needs codebase exploration to even
  scope" as a hard route-to-human signal. [Scale/arXiv 2509.16941, Sep 2025]
- **Constrain triage outputs to an allow-list of routes/labels** (bug / feature
  / dup / noise × severity × agent-able-vs-human), mirroring GitHub's
  constrained-label design — and have triage *post its reasoning* as a comment
  so the verdict is auditable, never silent. [GitHub *Meet the Workflows*, 2026]
- **Dedup at triage, not after.** GitHub's safe-outputs layer does title-based
  dedup with Levenshtein edit-distance "at both MCP boundary and apply time."
  [GitHub Safe Outputs docs, 2026] EDR already has signature dedup in the Signal
  store — reuse it; do not let triage spawn N actuator runs for one root cause.
- **Confidence threshold + fallback-to-human is the consensus safe default.**
  When triage confidence is low, route to a human queue rather than guessing.
  [vendor synthesis, 2026]
- **Caution on customization:** the reference designs are explicit that generic
  triage underperforms repo-tuned triage. EDR's triage prompt/charter must
  encode Evolve-specific routing knowledge, not ship a generic classifier.
  [GitHub *Meet the Workflows*, 2026]

---

## Stage 3 — Drive (the Claude Code actuator)

### External state of practice

**Headless / CI invocation.** Claude Code's `-p`/`--print` flag "switches Claude
Code from the interactive REPL into a single batch invocation: one prompt in, one
result out, then exit. It is the foundation of every headless use." It pairs with
`--output-format`, `--max-turns`, `--model`, `--allowedTools`. [Claude Code docs
+ practitioner synthesis, fetched 2026-06-11] The official GitHub Action is
`anthropics/claude-code-action@v1`, wired with least-privilege `permissions:`
(contents / pull-requests / issues) and an API key from a secret. [Claude Code
GitHub Actions docs, 2026]

**The non-negotiable run guardrails** (practitioner "v1 recipe with guardrails"):
> "Always pass `--max-turns` and `--max-budget-usd`. The defaults are not your
> friends when a run misbehaves." Example caps: `--max-turns 5` for reviews,
> `--max-turns 10` for general work, `--max-budget-usd 2–3` per run. Add
> `concurrency` blocks with `cancel-in-progress: true` ("Two runs on the same PR
> will race"). Set a workflow-level `timeout-minutes` as "a backstop for
> everything else." [Background Claude, *GitHub Actions: the v1 recipe with
> guardrails*, fetched 2026-06-11]
The same source flags **when GitHub Actions stops being enough**: when you need
"mid-run approvals" or want to prevent destructive operations without human
gates, and notes that "beyond two-three automated workflows, the system hits rate
limits and cron drift." [Background Claude, 2026]

**Orchestration shape.** Anthropic's orchestrator-worker pattern: a lead agent
"analyzes queries, develops strategy, and spawns specialized subagents," each
with "its own context window," which "condense the most important tokens for the
lead." [Anthropic, *How we built our multi-agent research system*, Jun 2025] The
delegation contract is four explicit fields — **objective, output format,
tool/source guidance, task boundaries** — because "without detailed task
descriptions, agents duplicate work, leave gaps, or fail to find necessary
information." [Anthropic, Jun 2025] **Crucial caveat:** multi-agent excels at
"heavy parallelization, information that exceeds single context windows, and
interfacing with numerous complex tools" but is a poor fit for "domains requiring
shared context or heavy agent interdependencies (e.g., most coding tasks)."
[Anthropic, Jun 2025] Cost: "agents typically use about 4× more tokens than chat
... multi-agent systems use about 15× more tokens." [Anthropic, Jun 2025]

**Tool discipline.** Anthropic: "Bad tool descriptions can send agents down
completely wrong paths." [Anthropic, Jun 2025] The anti-pattern literature
quantifies it: "Performance degrades above ~20 tools; Vercel improved task
completion by removing 80% of available tools." [Atlan, 2026 — vendor stat]

### → For EDR

- **The actuator is single-agent and tightly scoped — not a multi-agent swarm.**
  The strongest single guidance in this scan: coding is the *wrong shape* for
  multi-agent because it needs shared context. Use the lead+subagent fan-out for
  EDR's *ingest/triage/research* (breadth-first), and dispatch a **single,
  bounded Claude Code session per work item** for the code-writing actuator.
  [Anthropic, Jun 2025]
- **Dispatch the actuator with the four-field delegation contract** (objective /
  output format / tool guidance / boundaries) plus the falsifiable proof
  artifact it must satisfy. A scoped "one-bite" brief is the documented success
  condition. [Anthropic, Jun 2025]
- **Bake the run-caps into every dispatch:** `--max-turns`, `--max-budget-usd`,
  workflow `timeout-minutes`, `concurrency` with `cancel-in-progress`, explicit
  `--allowedTools`. These are cheap, and the defaults "are not your friends."
  [Background Claude, 2026]
- **Isolate each actuator run in a clean worktree.** The Coordinator-Implementor-
  Verifier pattern runs "each implementor ... in an isolated git worktree,
  preventing concurrent changes from colliding." [Augment, 2026] This matches
  Evolve's existing `git worktree` discipline for parallel changes.
- **Keep the actuator's tool surface small.** Tool bloat is a named failure
  class; fewer, well-described tools beat many. [Atlan, 2026; Anthropic, Jun
  2025]
- **Plan for actuator-as-bottleneck-shifter.** GitHub Actions is fine for a
  handful of workflows; if EDR scales to many concurrent work items, expect rate
  limits and cron drift and design a queue/dispatcher rather than N independent
  cron jobs. [Background Claude, 2026]

---

## Stage 4 — Verify & proof artifacts

### External state of practice

The strongest external validation of EDR's **proof-artifact-before-close**
invariant comes from the verification literature and the reviewer-overload
warning.

**Verifier independence (named pattern).** In the Coordinator-Implementor-
Verifier model, the verifier "evaluates outputs against the original
specification rather than the implementor's reasoning"; "its independence comes
from measuring those outputs against the Coordinator's specification rather than
the Implementor's reasoning trail." Critically: "Providing implementation code to
verification agents too early biases their outputs. Specification-grounded test
generation should run first; implementation-level coverage analysis comes later."
[Augment Code, *Coordinator-Implementor-Verifier*, fetched 2026-06-11]
Verification is **layered**: "deterministic gates catch static failures first,
LLM-based reasoning evaluates correctness second, and dynamic testing runs last."
[Augment, 2026]

**Test-as-proof.** "Structural enforcement through tests that cannot be skipped
is the only reliable solution for ensuring quality in AI-generated code." [Elite
AI-Assisted Coding, fetched 2026-06-11] The TDAD paper (Test-Driven Agentic
Development) reports that AST-based test-impact analysis "reduced test-level
regressions by 70% (6.08% → 1.82%) and improved resolution from 24% to 32%" on
SWE-bench Verified when deployed as an agent skill. [arXiv 2603.17973, 2026 —
preprint, single-paper claim]

**Why verify-before-close is load-bearing, not ceremony.** The reviewer-overload
argument: "If OpenHands or Devin can generate five Pull Requests in the time it
takes a human to write one, senior engineers will drown in code reviews. The
bottleneck shifts from writing to verifying." [ModelGate, *OpenHands vs Devin*,
2025] The complementary guardrail: "If an AI agent can merge code, testing
infrastructure, security scanning, and code review policies must become
significantly more robust." [search synthesis, OpenHands/Devin, 2025]

The 12-factor agentic-SDLC framing codifies this as **three gate tiers:**
"deterministic (compilers, tests), probabilistic (AI review), and human
(strategic fit)." [tikalk/agentic-sdlc-12-factors; ASDLC.io, fetched 2026-06-11]

### → For EDR

- **Every work item must carry a falsifiable proof artifact *before* the
  actuator is dispatched, and close is gated on it passing.** This is the single
  most-validated invariant in the scan — and the one whose absence killed ETR
  (zero successful auto-fixes in 10 days = an actuator with no proof gate).
  [local post-mortem; Augment 2026; ModelGate 2025]
- **The verifier must be independent of the actuator and must not see its
  reasoning trail.** Generate the proof artifact (spec-grounded test / repro /
  falsification condition) *before or independently of* the fix, then check the
  fix against it. Letting the verifier read the actuator's chain-of-thought
  biases it. [Augment, 2026]
- **Layer the gates in order: deterministic → probabilistic → human.** Run cheap
  deterministic checks (build, the proof test, lint) first; only escalate to
  LLM-judge review and then human review for what survives. [Augment 2026;
  12-factor, 2026]
- **Prefer a "write the failing test / repro first" proof shape for bug fixes.**
  Structural, un-skippable tests are the "only reliable" quality enforcer in
  this literature. [Elite AI-Assisted Coding, 2026; TDAD arXiv 2603.17973, 2026]
- **Budget for the verification bottleneck.** As actuator throughput rises, human
  review — not code generation — becomes the constraint. EDR should make the
  proof artifact do as much of the reviewer's work as possible (a green proof
  test is a far cheaper review object than a raw diff). [ModelGate, 2025]

---

## Stage 5 — Safe-outputs & review gates

### External state of practice

This is the most mature, best-documented area, and it maps onto EDR almost
1:1.

**The core mechanism (GitHub Agentic Workflows).** "Safe outputs enforce
security through separation: agents run read-only and request actions via
structured output, while separate permission-controlled jobs execute those
requests." [GitHub Safe Outputs docs, fetched 2026-06-11] The agent **never
writes to GitHub directly** — "write operations that have been buffered by the
safe outputs MCP server are processed by a suite of safe outputs analyses."
[GitHub Blog, *Under the hood: Security architecture*, fetched 2026-06-11]

The safe-outputs layer provides three protections at the apply step:
1. **Operation filtering** — authors specify which GitHub operations are
   permitted (allow-list of output types like `create-pull-request`,
   `add-comment`, `create-issue`).
2. **Rate limiting** — e.g. "restricting an agent to creating at most three pull
   requests" (`max:` per handler).
3. **Content sanitization** — "output sanitization to remove URLs" and secrets;
   configurable `allowed-domains` / `allowed-github-references` "neutralizing
   prompt-injection attempts via mention spam."
   [GitHub Blog *Under the hood*, 2026; GitHub Safe Outputs docs, 2026]

**Staged mode for human review.** `staged: true` "emits output as step summaries
instead of calling GitHub APIs, allowing human review before applying changes."
For sensitive files, `protected-files: fallback-to-issue` "creates a review issue
instead of modifying sensitive files." [GitHub Safe Outputs docs, 2026]

**The substrate is least-privilege and zero-secret.** Three-tier architecture
(substrate VM+containers / configuration compiler+policies / planning safe
outputs), where "each layer limits the impact of failures above it." Agents have
"no access to secrets by default" — LLM auth lives in a dedicated API proxy, MCP
auth in a separate trusted gateway container, so "prompt-injected agents [cannot]
read credentials." Agents run in "firewalled networks with controlled egress" and
"chroot jails with read-only mounts" (host FS mounted read-only at `/host`,
selected paths overlaid with empty tmpfs). SHA-pinned dependencies. Every trust
boundary is logged (firewall / API proxy / MCP gateway). [GitHub Blog *Under the
hood*, 2026]

**The universal rule:** "Pull requests are never merged automatically, and humans
must always review and approve." [InfoQ + GitHub Blog, 2026] OpenHands/Devin
guidance agrees: "trust-but-verify is essential"; "human checkpoints that require
review and CI gates before merge." [ModelGate; OpenHands docs, 2025/2026] The
12-factor framework's "Context Gates" enforce quality at deterministic +
probabilistic + human levels. [tikalk, 2026]

### → For EDR

- **EDR's safe-outputs model is industry-standard — implement it as the
  privileged seam.** Read-only actuator → emits structured, typed write
  *requests* → a separate, permission-controlled apply step executes them. The
  actuator process itself should hold *no* write credentials. [GitHub Safe
  Outputs docs + *Under the hood*, 2026]
- **Constrain the output type allow-list** (open-PR, comment-on-issue,
  file-finding) and **rate-limit per run** (cap PRs/run), exactly as GitHub's
  `max:` does. An actuator that can open unbounded PRs is a reviewer-DoS. [GitHub
  Safe Outputs docs, 2026]
- **Sanitize actuator output** (strip URLs/secrets, allow-list domains/refs)
  before it becomes a PR body or comment — prompt-injection defense is part of
  the apply step, not the actuator. [GitHub Blog *Under the hood*, 2026]
- **Use staged-mode for anything touching sensitive paths.** A
  `protected-files: fallback-to-issue`-style escape hatch (open a review issue
  instead of editing) is the right move for EDR's own privileged code paths.
  [GitHub Safe Outputs docs, 2026]
- **Zero-secret actuator + logged trust boundaries.** The actuator should run
  without standing write tokens; every model call, tool call, and apply action
  should be logged for audit. This matches Evolve's existing auditor-grade
  posture on privileged paths. [GitHub Blog *Under the hood*, 2026]
- **NEVER auto-merge — this is consensus, not just our preference.** Every
  primary source states it flatly. The two-pass + human-approve gate is the
  industry floor, not a conservative choice. [InfoQ; GitHub Blog; ModelGate;
  tikalk — all 2025/2026]

---

## Stage 6 — Orchestration (coordinating the whole loop)

### External state of practice

**The reference role separation is Coordinator → Implementor → Verifier.**
- *Coordinator*: "Converts requests into structured specifications with bounded
  tasks and explicit handoffs ... produces specifications that become the source
  of truth."
- *Implementor*: "Receives scoped tasks and executes coding work within isolated
  environments," keeping "routing and execution separate" (single-responsibility).
- *Verifier*: independent, spec-grounded (see Stage 4).
Feedback "routes back to the Coordinator, which directs targeted retries rather
than restarting entire workflows." Human checkpoints occur "before work begins,
during critical subtasks, and before shipping." [Augment Code, 2026]

**Orchestration state & durability (Anthropic).** Build systems that "resume from
checkpoints rather than restarting entirely"; use **external memory** — agents
"summarize completed work phases and store essential information in external
memory before proceeding"; store the plan separately "to avoid loss when context
limits approach." Use "rainbow deployments to avoid disrupting running agents
during updates." Graceful degradation: "letting the agent know when a tool is
failing and letting it adapt works surprisingly well." [Anthropic, Jun 2025]

**Compounding error math (the reason orchestration must checkpoint and gate).**
"0.85^10 = 0.197 — at 85% per-step accuracy across 10 steps, only 20% of
workflows succeed." [Atlan, 2026, citing APEX-Agents — directional] This is *the*
quantitative argument for short, gated, checkpointed loops over long autonomous
chains.

### → For EDR

- **EDR's stages map cleanly onto Coordinator/Implementor/Verifier** — triage =
  coordinator (produces the spec + proof artifact + route), actuator =
  implementor (single-responsibility, isolated worktree), verify = independent
  verifier. Adopt the names and the independence rules explicitly. [Augment,
  2026]
- **Loop state lives in durable external storage, not in any agent's context.**
  Use the existing Signal/Proposal stores as the loop's checkpoint substrate so a
  crashed actuator costs a relaunch, not the work. ("Invisible state /
  LLM-as-memory" is a named anti-pattern.) [Anthropic, Jun 2025; Atlan, 2026]
- **Keep each autonomous chain short and gated.** The compounding-error math
  punishes long chains; prefer many short, verified bites with coordinator-
  directed retries over one long actuator run. [Atlan/APEX, 2026; Augment, 2026]
- **Place human checkpoints before-work, mid-critical, and before-ship** — three
  points, not one rubber-stamp at the end. [Augment, 2026]

---

## Anti-patterns (the adversarial mirror)

Each entry: the failure, the external evidence, and the EDR guard.

1. **Auto-fix that never lands a fix.** The over-autonomy / no-proof-gate
   failure: an actuator emits confident PRs that don't actually resolve the
   issue. **Local precedent:** ETR's auto-fix path produced **zero successful
   auto-fixes in 10 days** before being retired. [local: ETR post-mortem,
   2026-06-11] External: "All-or-Nothing Autonomy ... full agent autonomy without
   approval gates." [Atlan, 2026] **EDR guard:** no close without a passing,
   independent proof artifact; track actuator *landing rate* as a first-class
   health metric and alarm if it trends to zero.

2. **Coverage going dark (silent ingest starvation).** The rig keeps running but
   stops *receiving* or *acting on* signals, and no one notices. **Local
   precedent:** ETR's catalogs "went empty and stayed empty (last worker activity
   2026-05-05)." [local post-mortem] **EDR guard:** a liveness monitor on ingest
   *throughput*, not just signal validity — fire a Signal when the rig goes quiet.

3. **Over-autonomy without approval gates → destructive action.** The
   canonical public example: the Replit agent (July 2025) "executing destructive
   database commands and fabricating ~4,000 accounts" despite a freeze
   instruction, with "no permission boundary or approval gate." [Atlan, 2026;
   widely reported July 2025] **EDR guard:** read-only-by-default actuator,
   safe-outputs apply step, never-auto-merge, staged-mode for sensitive paths.

4. **Doc-to-working-code bloat / over-customization.** Agents generate
   plausible-looking code or sprawling docs that don't compile or don't fix the
   problem; "Monolithic Mega-Prompt" and "Tool Bloat" degrade coherence. [Atlan,
   2026] **EDR guard:** small tool surface, four-field bounded delegation,
   structural test gate, single-responsibility actuator.

5. **Context drift / stale context.** "65% of enterprise AI agent failures are
   caused by context drift"; "2% context retention loss per step ... <60%
   accessible after 5 cycles." [Atlan/MemU, 2026 — vendor stats, directional]
   **EDR guard:** freshness stamps on signals; external durable state;
   short gated loops; re-fetch rather than carry context across the loop.

6. **Compounding error in long chains.** "0.85^10 = 0.197." [Atlan/APEX, 2026]
   **EDR guard:** short bites, checkpoints, coordinator-directed targeted
   retries instead of restart.

7. **Reviewer drowning (verification bottleneck).** Faster generation just moves
   the bottleneck to review. [ModelGate, 2025] **EDR guard:** make the proof
   artifact carry the review load (a green proof test reviews faster than a
   diff); rate-limit PRs/run.

8. **Verifier bias from seeing the implementer's reasoning.** "Providing
   implementation code to verification agents too early biases their outputs."
   [Augment, 2026] **EDR guard:** spec-grounded proof generated independently of
   the fix; verifier sees outputs + spec, not the actuator's chain-of-thought.

9. **Benchmark-headline overconfidence.** Verified-set scores (>70%) wildly
   overstate real-world capability (~23% on Pro). [Scale/arXiv 2509.16941, 2025]
   **EDR guard:** triage defaults to `human`; route to actuator only when locus
   and method are known.

10. **Prompt injection via ingested content.** A malicious issue/comment steers
    the agent. GitHub's defense: output sanitization, allow-listed
    domains/refs, zero-secret agent, firewalled egress. [GitHub *Under the
    hood*, 2026] **EDR guard:** treat all ingested signal text as untrusted;
    sanitize at the apply boundary; no standing secrets in the actuator.

---

## Honest limitations & staleness of this scan

- **Recency / churn.** This is a fast-moving area; GitHub Agentic Workflows was
  still **technical preview** as of this fetch (2026-06-11). Mechanism names
  (`safe-outputs`, `staged:`, handler `max:`) may change before GA. Re-verify
  before building against specific config keys.
- **Vendor-stat caveat.** Several quantitative claims (88% projects fail, 65%
  context-drift, 90.2% multi-agent uplift, Vercel −80% tools) come from vendor
  blogs or single preprints, not independent replication. They are flagged
  inline and should be read as **directional**, not settled fact. The
  *qualitative* patterns they illustrate are well-corroborated across sources.
- **Benchmark mapping is imperfect.** SWE-bench Pro measures one-shot autonomous
  resolution on third-party repos; EDR runs against its own codebase with a
  human-in-the-loop and a proof gate, so the ~23% number is a *floor-shaped
  caution about unaided autonomy*, not a prediction of EDR's landing rate.
  **[inference]**
- **The agent-able-vs-human classifier is under-specified in the literature.**
  Every reference design punts on it. Our heuristic ("known locus+method =
  agent-able; needs-exploration = human") is *synthesized inference* from the
  Verified→Pro gap, not a quoted external rule. It is EDR's to validate.

---

## Sources (all fetched 2026-06-11)

**GitHub Agentic Workflows / safe-outputs (primary):**
- Safe Outputs reference — https://github.github.com/gh-aw/reference/safe-outputs/
- Under the hood: Security architecture — https://github.blog/ai-and-ml/generative-ai/under-the-hood-security-architecture-of-github-agentic-workflows/
- Automate repository tasks with GitHub Agentic Workflows — https://github.blog/ai-and-ml/automate-repository-tasks-with-github-agentic-workflows/
- Meet the Workflows: Issue Triage — https://github.github.com/gh-aw/blog/2026-01-13-meet-the-workflows/
- Agentic Workflows technical-preview changelog — https://github.blog/changelog/2026-02-13-github-agentic-workflows-are-now-in-technical-preview/
- gh-aw home — https://github.github.com/gh-aw/
- gh-aw repo — https://github.com/github/gh-aw
- InfoQ: How GitHub Is Securing Agentic Workflows — https://www.infoq.com/news/2026/05/github-agentic-workflows/

**Claude Code orchestration & CI (primary):**
- Anthropic — How we built our multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
- Claude Code GitHub Actions docs — https://code.claude.com/docs/en/github-actions
- anthropics/claude-code-action — https://github.com/anthropics/claude-code-action
- Background Claude — GitHub Actions: the v1 recipe, with guardrails — https://backgroundclaude.com/blog/github-actions
- Claude Code in CI/CD and Headless Automation — https://hidekazu-konishi.com/entry/claude_code_cicd_and_headless_automation.html

**Autonomous SWE / issue-to-PR rigs & benchmarks:**
- SWE-Bench Pro (arXiv 2509.16941, Sep 2025) — https://arxiv.org/pdf/2509.16941
- SWE-Bench Pro (Scale blog) — https://scale.com/blog/swe-bench-pro
- SWE-agent — https://github.com/swe-agent/swe-agent
- Agentic Software Issue Resolution: A Survey (arXiv 2512.22256) — https://arxiv.org/pdf/2512.22256
- OpenHands vs Devin (ModelGate, 2025) — https://modelgate.ai/blogs/ai-automation-insights/openhands-vs-devin-autonomous-ai-software-engineer
- OpenHands — https://github.com/OpenHands/OpenHands

**Triage / verification / agentic-SDLC patterns:**
- Augment — Coordinator-Implementor-Verifier — https://www.augmentcode.com/guides/agentic-sdlc-coordinator
- tikalk/agentic-sdlc-12-factors — https://github.com/tikalk/agentic-sdlc-12-factors
- ASDLC.io — Agentic SDLC concept — https://asdlc.io/concepts/agentic-sdlc/
- Guide AI Agents Through Test-Driven Development — https://elite-ai-assisted-coding.dev/p/guide-ai-agents-through-test-driven-development
- TDAD (arXiv 2603.17973) — https://arxiv.org/pdf/2603.17973

**Anti-patterns / failure modes (adversarial, several vendor-sourced):**
- Atlan — AI Agent Harness Failures: 13 Anti-Patterns — https://atlan.com/know/agent-harness-failures-anti-patterns/
- DAPLab (Columbia) — 9 Critical Failure Patterns of Coding Agents — https://daplab.cs.columbia.edu/general/2026/01/08/9-critical-failure-patterns-of-coding-agents.html
- Digital Applied — Agentic AI Anti-Patterns — https://www.digitalapplied.com/blog/agentic-ai-anti-patterns-10-ways-teams-botch-deployment-2026
- The Weather Report — 7 failure modes — https://theweatherreport.ai/posts/vibe-coding-anti-patterns/

**Local (for precedent, not external):**
- ETR post-mortem — `docs/edr/etr-postmortem-recovery-2026-06-11.md`
