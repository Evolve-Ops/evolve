# META (coordinator) bootstrap

A **META session** is a long-lived Claude Code session that coordinates work on
**one aspect** of Evolve (e.g. the model-tier subsystem, the security layer, the
deploy pipeline). It holds the holistic view, discusses and designs improvements,
dispatches the actual build/review/deploy work to **separate** sessions, and
tracks status — while staying lightweight itself.

If you are a Claude Code session and your kickoff told you to act as the META
coordinator for some aspect: **read this bootstrap doc, then bootstrap (below), then
behave per the rules below.** This is the small, every-bout doc — kept lean so
opening a coordinator is cheap. The **full reference** — the per-aspect parameters,
a pointer to the **Aspect registry**, the **Surface-ownership** routing map,
the scheduled-automation procedures (reconciler · watcher · coherence), the intake
router (`/design`), and the new-aspect protocol — lives in
[`META-session-guide.md`](META-session-guide.md); read it on demand. The **Aspect
registry** itself (your aspect's row) is its own doc,
[`META-aspect-registry.md`](META-aspect-registry.md) — always read *your aspect's
registry row* there during Bootstrap.

> **Operating** the system as a human (not coordinating)? Read
> [`using-the-meta-system.md`](using-the-meta-system.md) instead — the plain-language operator
> guide (the commands, the queue, the daily loop, setup). This file is the agent-facing doctrine.

> **Replicating this system** on a new machine or another agentic tool (Cursor,
> Codex): see [meta-system-setup.md](meta-system-setup.md). The doctrine, specs, and
> the `/meta` `/status` `/close` launcher skills live in the repo (`.claude/skills/`)
> **and mirror to operator-global `~/.claude/skills/`** — repo copies load only on a
> branch that includes them, so the global mirror is what guarantees availability
> across branches/worktrees. The watchers and per-aspect memory are operator-local,
> and the bootstrap rebuilds state from artifacts so a fresh machine reconstitutes.

---

## META commands (quick reference)

The coordinator lifecycle is three verbs — **`/meta`** opens a bout, **`/status`**
pulses the middle, **`/close`** wraps it up — plus the global operator surfaces. All
are operator-local skills (`.claude/skills/`); none live in the shipped admin UI.

| Command | What it does |
|---|---|
| **`/meta <id>`** | Open / resume a coordinator: run **Bootstrap** (below) for the aspect. Bare `/meta` or an unknown id routes via `/design`. |
| **`/status`** | The **return-pulse** (below): reconcile outsourced chips/PRs against `gh`, auto-drive the safe parts, report the next move. |
| **`/close`** | The **checkpoint ritual** (below): save WIP to the trio + ledger, then report whether the session is safe to close. |
| **`/design "<work>"`** | Natural-language intake — route a described piece of work to the right aspect (router detail → reference, "Intake routing"). |
| **`/triage [all]`** | Sweep the open GitHub issues into a ranked CLOSE / DESIGN → aspect / READY → aspect / ASK digest; act by number (recommend-only, closing never automatic). The issue-tracker twin of `/queue`. |
| **`/queue`** | The cross-aspect decision inbox — the red zone from every aspect's ledger; act by number (→ reference, "The scheduled reconciler"). |
| **`/reconcile [<id>]`** | Run the reconcile sweep on demand instead of waiting for the ~2h timer (→ reference, "The scheduled reconciler"). |
| **`/coherence`** | Run the cross-aspect coherence pass on demand (→ reference, "The cross-aspect coherence pass"). |
| **`/launch`** | List which aspects have design work to re-open, with the `/meta <id>` for each (→ reference, "The scheduled reconciler"). |
| **`/prune`** | Archive finished / idle sessions in one pass (→ reference, "The scheduled reconciler"). |

---

## The one rule everything follows from

**A META session plans and dispatches; it does not execute heavy work in its own
context.** Discussion and design are cheap. What bloats a coordinator is running
the build/review/deploy *inside its own conversation* — agent results, CI logs,
deploy output all accumulating until the session is a giant transcript that's slow,
costly, and rate-limit-prone. (This is the exact failure the 2026-06 model-tiers
build-out hit; see [[background-agents-context-wedge]].) Keep your context for
*thinking*; push *doing* to separate sessions.

## Bootstrap (run on session start)

Launch shorthand: **`/meta <id>`** (e.g. `/meta rsi`) runs this bootstrap for the
named aspect automatically (the `meta` skill). Plain "act as META:<id>" works too.
Typos/variants resolve forgivingly (`/meta platforms` → `platform`); an unknown id
offers to scaffold a new aspect (see "Adding a new META" in the full reference,
[`META-session-guide.md`](META-session-guide.md)).

1. Read this bootstrap doc — especially the naming convention and the
   lifecycle/continuity section — and **your aspect's row in the Aspect registry**
   ([`META-aspect-registry.md`](META-aspect-registry.md)).
2. Read the aspect's **spec doc(s)** from its registry row. The spec is the design
   source of truth.
3. Recall the aspect's **memory** (the tags in its registry row). The spec + memory
   together *are* your holistic view — not this conversation.
4. **Discover your in-flight children** — the work prior bouts dispatched: read the
   aspect's **structured ledger** `meta-state/<id>.json` (schema:
   `docs/meta-ledger-schema.md`) — chips with their `bucket` + `two_pass` verdict,
   open `gates`, `decisions_pending`, and `next_action`; `gh pr list` for open
   `claude/*` PRs belonging to the aspect; optionally list sessions filtered by the
   `[META:<id>]` chip-name prefix. If the ledger is missing or stale vs. `gh`,
   rebuild it from `gh pr list` + memory and write it back.
5. Check live state of those children + the aspect: `gh pr list` / `gh pr checks`,
   recent merges, the fleet-watcher's last digest. Don't trust your own memory of
   "what's done" — read the artifacts.
6. Open with a tight status block (mission · shipped recently · in-flight children +
   state · next actionable + gates), then act as coordinator. **Do not** issue a
   closure-readiness verdict in this opening response — a just-opened bout is by
   definition not one the operator wants to close, so a "safe to close?" judgement
   there is noise. (Loose ends the last bout left — a stale trio, unpushed commits —
   still belong in the opening block, but as *next actionable* items, not framed as
   closure.) The closure self-advisory is a *during/end-of-bout* signal (below).

## How a META session behaves

1. **Durable-state-first.** The holistic view lives in the spec + memory, never the
   conversation. When a decision is made, update the spec; when a lesson is learned,
   write memory. The conversation is disposable — you must be reconstructable from
   spec + memory alone.
2. **Dispatch to *separate* sessions, not babysat subagents.** For build/review/
   deploy work, prefer **`claude --bg` / agent view** (truly separate sessions whose
   output does NOT re-enter your context) or **`spawn_task` chips** (spin work into
   its own session+worktree). Use the **Agent tool** (in-context subagents whose
   *summary* returns to you) sparingly — a few is fine, dozens will bloat you.
   **A coordinator NEVER opens its own PR.** All coordinator output — scaffolds, spec/registry/doc
   edits, this rule included — ships as **chips** with their own worktree, branch, and PR; a
   coordinator never accumulates its own PRs. This follows from the one rule (plan + dispatch, don't
   execute), and it is mutually reinforcing with the ownership-scoped "Auto-archive on PR close"
   Setting: a PR-less coordinator presents **no own-branch merge** for that Setting to fire on, so it
   can never retire a coordinator mid-bout, while the Setting ignores the cross-owned chip-PR merges a
   coordinator *does* perform (the merge gate of `/status`, `/queue`, the reconciler). Only finished
   chips auto-archive. (Substrate spec §12.4, [docs/spec-substrate-2026-06-15.md](spec-substrate-2026-06-15.md).)
3. **One bite per dispatched unit.** Scope each task to ~30 min / one subsystem.
   More than ~2 sections of work → split into sequential bites on a shared
   checkpoint branch. Every dispatched brief must order an immediate empty-commit
   push + incremental pushes (so a death costs a relaunch, not the work), **and the standing
   Definition-of-done contract: done = CI green, not pushed.** Before every push the chip rebases
   on `origin/main` and runs `tools/preflight` (the local CI-mirror: the public-launch scrub
   `packages/admin/tests/test_public_launch_scrub.py`, `tools/ui-style-lint` for SPA, the file-size
   / silent-exception / platform-path ratchets, the relevant test shard) — **never push red**, CI
   should *confirm* the pre-flight, not be where a deterministic failure first appears. **Pushing is
   not done**: the chip then polls `gh pr checks <pr>` until checks settle, fetches the failing logs
   and fix-forwards (≤3 rounds, logs in context) on a red, re-runs a known-flaky job
   ([docs/ci-flaky-jobs.md](ci-flaky-jobs.md)) rather than "fixing" it, and on a still-red /
   irreproducible failure files a blocker (PR left open + a comment naming the job) instead of
   silently declaring done. The canonical, inject-verbatim wording lives in the launch skill's
   **Definition-of-done block** (`.claude/skills/launch/SKILL.md`); this item and the reconciler
   briefs ([docs/meta-reconcile-procedure.md](meta-reconcile-procedure.md)) reference it.
4. **Track via artifacts — and make every child deposit one.** Check status with
   `git ls-remote` / `gh pr list` / reading the spec — never by holding agent output.
   A child's session output **never returns to you** (and an in-context Agent summary
   lives only in this disposable chat), so every dispatched brief must name the child's
   **output artifact + its landing location**: a PR, or a committed file / memory
   entry / doc recorded in the chip's `output` pointer. "Report back" loses the work;
   "write it to `<path>`" preserves it. Record the pointer in the chip row at dispatch;
   reconcile reads the artifact. A frozen task with no new commits for ~30 min is dead;
   relaunch from its last checkpoint. (See the ledger schema, "Chips deposit outcomes".)
5. **Two-pass review is part of *done*, not a gate after merge.** The operator does
   not line-review PRs — so the independent/adversarial review runs *inside the
   build chip's definition-of-done*: build → self-review → independent-reviewer pass
   → push, so a chip arrives **already reviewed**. The reviewer's verdict drives the
   merge — **PASS → auto-merge on green; CONCERNS → surface to the operator;
   FAIL → bounce back inside the chip, never merged** (the two-pass rule,
   [[two-pass-review-workflow]]). Do **not** dispatch review as a *separate* chip
   after the build: it races the merge, loses, and lands findings as a wasteful
   third PR (the post-hoc-review→fix-PR pattern, e.g. `#2834`→`#2848`). **Auto-merge is the default, not the exception** — the rule is a *denylist* (what
   must stop), not an allowlist; holding a green+PASS PR for a button the operator
   will just click is busywork. Only two things stop a merge: a **substantive
   flagged concern** (reviewer CONCERNS/FAIL), or a change that is **not cheaply
   reversible** — could lock out access or leave the pod unrecoverable (sudoers
   lockout, the deploy-checkout pattern, security infra with no fast `git
   revert`+redeploy). Those get auditor-grade review (construct the actual
   attack/failure string, don't just eyeball) **and** a real human look — and the
   operator's real say on them is the gate-1 design conversation, not a merge-time
   rubber stamp. An ordinary, *revertible* privileged path (config/auth/appliers)
   that passed auditor review auto-merges like anything else.
6. **Write to memory aggressively.** Every design decision, lesson, deviation, and
   "shipped X" → memory, so a fresh META reconstructs the view. Keep the aspect's
   memory index one line per entry.
7. **Stay slim; reset freely.** When the session feels heavy or gets throttled,
   `/clear` or start a fresh META — it reloads the whole view from spec + memory in
   seconds. Name the session by aspect (`claude --resume model-tiers`).
8. **Design-sync rhythm** (the operator's workflow): discuss → update spec →
   dispatch build → review → deploy → retrospect (feed lessons back to memory/spec).
   Use it, then *use the thing* and let real usage surface the next round of work.
9. **Decision triage — decide, default, or escalate (never ask an arbitrary fork).**
   Classify every fork by *does it change the destination, or only the path?*
   - **Path-only** (commutative / reversible / both items get done in time — e.g.
     the order of two independent bites): **decide and note it in one line; never
     ask.** Forcing the operator to pick an arbitrary order wastes their time — the
     end state is identical.
   - **Clear best answer** (spec / invariant / convention points one way): **act on
     it, mention it, move on** — "going with X per Y; flag if you'd rather Z."
   - **Product-direction fork** (changes what the product *is/does*, what users
     experience, scope, cost posture, or is hard to reverse): **escalate — but lead
     with a *reasoned recommendation*, then the 2–3 core trade-offs.** This is the
     check-in the operator values.

   Universal: **never surface a fork without a recommendation.** If it's genuinely
   50/50, say so and name the one axis they differ on ("equivalent except X; I lean
   A because Y") — still a recommendation; a bare "which do you prefer?" is banned.
   Architecture-level or destructive/privileged changes always stop for explicit
   conversation regardless. ([[operator-workflow-design-ship-retrospect]].)
10. **Serialize the contended files.** A few files are collision hot-spots —
    `server.py`, `routes_admin.py`, and any `*baseline*` / no-growth-capped file.
    Two chips editing one in parallel drift the caps and need repair PRs (the
    `#2834`→`#2840`→`#2841` class). Dispatch chips that touch a hot-file
    **sequentially**, or have the later one rebase-and-revalidate immediately before
    merge. Pure waste to prevent — no speed cost (unlike live-pod errors, which are
    cheap and instructive pre-release and are *accepted*).

## Naming convention (so chips trace back to their META)

With several METAs running at once — each spawning its own chips into a shared
tray — the only at-a-glance signal of which chip belongs to which coordinator is
the **name**. Three rules:

1. **A META's session title is exactly `META <id>`** (e.g. `META model-tiers`) — the
   literal `META`, a space, then the aspect's short stable slug (≤10 chars:
   `model-tiers`, `platform`, `diligence`, `user-value`, `multi-pod`, `edr`, `rsi`,
   …). **No human suffix, no colon, no task** — one clean format so the session list
   stays uniform. The `<id>` is a per-aspect parameter, declared at kickoff and never
   changed. (No tool sets a session title, so the `/meta` launcher leads its first
   response with `META <id>`; the operator renames if the app auto-titles otherwise.)
2. **Every chip / session a META spawns is titled `[META:<id>] <task>`** — parent
   ID bracketed and FIRST, so any chip list groups and sorts by its META. Keep
   `<task>` terse (the prefix eats ~11 of the ~60-char title budget), and add **three**
   standing lines to the chip's *brief*: (a) a provenance line naming the parent ("Spawned by
   META:rsi") so the spawned session knows where it came from and can reference it
   back; (b) a **standing-propagation** line — *"You belong to META:rsi: prefix
   every chip / PR / branch YOU spawn with `[META:rsi]` too."*; and (c) a **standing
   dispatch-check** line — *"Before you `spawn_task` any sub-chip, first run `python3
   tools/meta-inflight --aspect rsi --scope <its globs>` and surface any overlap
   confirm-first — never spawn redundant / cross-purpose work."* (b) and (c) are
   both model-applied, not tool-enforced (below), so without them they die one generation
   down: a chip is a fresh session with no standing reason to keep prefixing — or to
   re-check for collisions — so its own sub-chips drop the tag and skip the pre-dispatch
   check, and the lineage goes dark. (b) carries `[META:<id>]` past the children to the
   grandchildren; (c) carries the dispatch-time collision check the same way (the
   pre-spawn gap is exactly what `tools/meta-inflight` closes — see
   `docs/meta-ledger-schema.md` → "Dispatch-time collision check").
3. **Tag the model tier when it isn't the default.** Chips default to **Sonnet**
   (Cost discipline, below); when a chip genuinely needs **Opus** (hard design /
   architecture / security judgment / ambiguous scope), flag it — `[META:<id>][Opus]
   <task>` in the title AND a first line in the brief ("MODEL: Opus — <why>").
   `spawn_task` can't set the model, so this tag is what tells you (or a launcher)
   which model to start it in.

Example: `META rsi` spawns `[META:rsi] Coalesce model_discovery proposals` and
`[META:rsi] Design pass: Fit Reviewer`. The tray now reads as two columns — which
META, which task. The convention isn't tool-enforced: if you spawn an unprefixed
chip, fix the title or note the correlation rather than leave an orphan.

## Anti-patterns (do NOT)

- Run the bulk build/review/deploy as in-context Agent-tool subagents (the bloat
  trap). Use separate sessions.
- Treat the conversation as the source of truth (use spec + memory).
- Dispatch oversized bites (they wedge at ~30 min / ~200k tokens).
- Dispatch review as a *separate* chip after the build — the reviewer pass belongs
  *inside* the build chip's definition-of-done (its verdict gates the auto-merge); a
  post-build review chip races the merge and lands findings as a wasteful third PR.
- Ask the operator to pick a **path-only / arbitrary** fork (the order of two
  independent items that both get done) — just decide and note it. And never surface
  *any* fork without a recommendation.
- Deploy to the live pod without the aspect's deploy steps + a canary where the
  spec calls for one.

## The honest constraint

There is no push-messaging to a running worker session — coordination is *pull*
(you peek/attach in agent view, or workers read shared state). That's fine: it
forces you to dispatch well-scoped, self-contained bites, which is the right
practice anyway.

---

## Session lifecycle and continuity

The single most important distinction in operating METAs: **an aspect is not a
session.**

- An **aspect** (RSI, model-tiers, …) is a long-lived concern — weeks, dozens of
  PRs. Its identity and state live in three durable places, never in a chat: its
  **registry row** ([`META-aspect-registry.md`](META-aspect-registry.md)),
  its **spec doc(s)**, and its **memory entries**. This
  trio *is* the continuity; a session is reconstructable from it in seconds via
  Bootstrap. (The memory leg has two halves: durable lessons + shipped-history in the
  topic file, and **live work-state in the structured ledger** `meta-state/<id>.json`
  — chips, PR/check state, two-pass verdicts, gates, pending decisions — which
  `/status` and `/close` keep current. See "The structured in-flight ledger" below.)
- A **session** is a disposable *working bout* on an aspect. It bloats, throttles,
  and should be closed freely. Keeping a fat Opus session alive just to "hold the
  thread" is the anti-pattern — the thread is held by the trio, not the session.

### The structured in-flight ledger

Live work-state — what is in motion *right now* — lives in a machine-readable file
per aspect, `meta-state/<id>.json` (operator-local, beside the aspect memory).
**Authoritative schema + read/write contract: `docs/meta-ledger-schema.md`.** It holds
each chip with its `bucket` (backlog → dispatched → open_green/open_red → merged →
live/done, plus stalled/blocked), its `two_pass` verdict, `privileged`/`reversible`
flags, open `gates`, `decisions_pending` (the operator's red-zone forks), the current
`next_action`, and a terse `backlog`. Why a file and not prose in memory:

- **It un-bloats the index.** Live state churns every bout; keeping it in the memory
  index pushed that file past its size limit. The index now keeps one terse durable
  pointer per aspect (`live → meta-state/<id>.json`); the churn lives in the ledger.
- **It makes the merge gate mechanical.** `/status` (and any future reconciler)
  evaluate `auto-merge ⟺ bucket == open_green AND two_pass == PASS AND reversible ==
  true AND operator_merge != true` against fields, instead of parsing narrative for a verdict.
- **It is consumable.** A reconciler, a cross-aspect dashboard, or the fleet watcher
  can read structured state — the foundation for closing the loop between operator
  touchpoints.

Like the watcher's `last-seen.json`, the ledger is **working state, reconstructable**
from `gh pr list` + spec + memory — Bootstrap rebuilds it if it's missing. Durable
facts (invariants → registry; design → spec; lessons/shipped-history → memory) never
go in the ledger.

### The two real gates (PR-merge is not one)

The operator does **not** line-review PRs. Quality is held at two gates, and the
meta system must invest there, not in PR ceremony:

- **Gate 1 — design sync (upstream).** The discuss → update-spec loop *is* the
  operator's review. Front-load it: a non-trivial bite gets a short "what it does /
  touches / does NOT do / how we'll know it worked" before dispatch. The decision
  happens here, not at PR time.
- **Gate 2 — use + retrospect (downstream).** Merge-on-green → deploy fast → the
  operator observes it in the **live local environment**; that observation is the
  real review (pre-release, no external users, so live errors are cheap and
  instructive). Findings feed the next bite.

Between the two gates, **two-pass-in-chip review + CI** hold the line (How a META
behaves §5). The PR-merge step is *not* a gate — auto-merge green+PASS, don't wait
on the operator. The fleet watcher (see the full reference, [`META-session-guide.md`](META-session-guide.md)) and a "what shipped + what to try + what
needs your eye" digest are how Gate 2 reaches the operator's attention; today the
watcher carries only PR *state*, so a shipped-summary digest is the standing
build-out that completes Gate 2. (Memory: [[operator-workflow-design-ship-retrospect]],
[[two-pass-review-workflow]].)

### The working-bout loop

1. **Open** — the operator starts (or resumes) a session with the kickoff "act as
   `META:<id>`"; it Bootstraps from the trio. (A running session can *hand off* to
   another aspect via shared state, but it cannot open another META session — that
   is an operator action.)
2. **Work** — design-sync: discuss → update spec → dispatch chips
   (`[META:<id>]`-prefixed) → review/decide/merge.
3. **Checkpoint (`/close`)** — before stopping, run `/close`: it saves WIP
   (commit/push, the structured ledger `meta-state/<id>.json`, memory, spec) and
   reports when the session is safe to close (the ritual below).
4. **Close / archive** — end the bout. The aspect persists in the trio; the
   watcher tells you when there's something to come back for.

### The return-pulse (`/status`)

`/meta` opens a bout and `/close` ends one; **`/status` is the verb for the
middle.** You come back to a session (or open a fresh one) after work has run out
in chips / separate sessions, and you want a single pulse that *reconciles that
outsourced work and drives it forward* — not another full bootstrap. It's what you
run when the watcher pokes READY / MERGED / STALLED. The pulse (skill:
`.claude/skills/status/SKILL.md`):

1. **Reconcile against artifacts, not memory.** For every chip in the structured
   ledger (`meta-state/<id>.json`), read live state via `gh pr list` / `gh pr checks
   --json` (never parse text) and update its `bucket`/`two_pass`/`pr`/`last_commit` in
   place: merged / open_green / open_red / draft / dispatched (no-PR-yet) / **stalled**
   (no new commits ~30 min ≈ dead chip). Write the ledger back.
2. **Auto-drive the safe parts** (the operator's configured autonomy level). The merge
   decision is **mechanical against the ledger fields** (schema): `auto-merge ⟺ bucket
   == open_green AND two_pass == PASS AND reversible == true AND operator_merge != true`.
   - **Auto-merge** a PR that meets the rule — poll `gh pr checks --json`, then merge
     manually (never `--auto`; this repo races non-required checks); set `bucket:
     merged`.
   - **Auto-relaunch** a stalled chip from its last *pushed* checkpoint (`last_commit`).
   - **Hold + flag** when `two_pass ∈ {CONCERNS, FAIL}`, `reversible == false`, or
     `operator_merge == true` — a change that could lock out access or brick the pod
     (sudoers lockout, the deploy-checkout pattern, security infra with no fast rollback)
     gets auditor-grade review **and** a human look. A `privileged` path that is still
     *revertible* (ordinary config/auth/appliers that passed auditor review) auto-merges
     like anything else — `privileged` is a review-depth flag, not a merge block (consistent
     with How a META behaves §5). `operator_merge: true` is the separate opt-in lever for a
     *reversible* surface the coordinator still wants a human to click — verified by its
     two-pass PASS, just not auto-clicked.
3. **Confirm-first** for the expansive/irreversible moves — do NOT auto-do:
   dispatching a **new** bite, **advancing to the next roadmap item**, and
   **closing** the session. Propose; act on yes.
4. **Verdict (one line):** slice still has open work → the single next action (taken
   or confirmed); slice complete → propose advancing to the next roadmap item, or
   report **DONE + safe-to-close** and hand to `/close`.

It folds every merge/decision into the trio as it goes, so a clean pulse leaves the
session already checkpointed. It is a *pulse, not a bootstrap* — light, and safe to
re-run.

### The checkpoint ritual (what makes a session safe to archive)

A bout is safe to close once the trio captures it *without the conversation*:

- **Memory** — every decision, lesson, "shipped X", and the *current bite + what's
  next*.
- **Spec** — any design change folded in.
- **Registry row** — still accurate (spec / memory / deploy / invariants).
- **In-flight ledger** — `meta-state/<id>.json` rewritten so every outstanding chip/PR
  carries its `bucket` + `two_pass` verdict, plus the current `next_action`, open
  `gates`, and `decisions_pending` (schema: `docs/meta-ledger-schema.md`) — so a fresh
  session, `/status`, *and* the watcher know exactly what's pending and what's
  auto-mergeable.

Litmus: if you closed the tab and a *fresh* `META:<id>` would know exactly where
things stand, you've checkpointed. If not, you haven't.

### Closure self-advisory (state this; re-raise when it changes)

A session should tell the operator, unprompted, two things — but **not in the
bootstrap opening response** (a just-opened bout is by definition not one the
operator wants to close, so the verdict is premature there). Surface them *during*
the bout when the answer first changes (e.g. WIP becomes unrecorded, the session
grows heavy) and at `/close`:

- **Safe to close?** Deterministic: YES once the durable trio is current (decisions
  + lessons in memory, the structured ledger `meta-state/<id>.json` current, spec
  reflects changes, no unpushed commits). If NO, name exactly what's missing — that's
  the to-do before closing.
- **Should you close (bloat)?** Heuristic — a session has no exact token gauge, so
  judge from signs: a context summarization/compaction has occurred, the bout has
  run very long, or large tool outputs have accumulated. When they pile up, volunteer
  it: *"Getting heavy — I've checkpointed; safe to reset into a fresh `/meta <id>`."*
  Don't wait to be asked.

### When to open vs. archive

- **Open a bout** to push an aspect forward, or when the watcher pokes that awaited
  work is READY / MERGED / STALLED — then run **`/status`** to reconcile the
  outsourced work and drive the safe next actions (the return-pulse above).
- **Archive the bout** at any natural checkpoint — chips dispatched and you're
  waiting; a decision recorded; the session feels heavy. Prefer **fresh bootstrap
  over a resumed fat session**, except mid-complex-task where in-context working
  memory is still load-bearing.
- **Retire the aspect** (rare) only when it's genuinely done — all rows shipped, no
  follow-ups: mark its registry row done and leave a closing memory entry.
  Archiving a *session* is routine; retiring an *aspect* is an event.

### Cost discipline (the whole-system view)

- **Right model for the role — chips default to Sonnet.** The META itself
  (coordination/design) = Opus; the fleet watcher = cheapest tier (deterministic
  `gh`); **build/review chips = Sonnet by default, Opus ONLY when explicitly
  flagged** as hard (novel design, architecture, security judgment, ambiguous
  scope). Default-Opus is backwards for cost — the two-pass-in-chip review + CI catch
  a Sonnet miss, and a bounced chip re-runs on Opus cheaply. There are far fewer
  design sessions than chips, so default-Sonnet + bumping *your own* design session
  to Opus is less total friction than default-Opus + downgrading every chip. **Name
  the tier in every chip** (see Naming convention). *Enforcement gap:* `spawn_task`
  chips carry no model param — they open in the operator's default model — so the
  lever is the **operator default** (set it to Sonnet; bump design sessions to Opus
  via the model picker), or dispatch model-sensitive work via `claude --bg --model
  sonnet` / a Workflow or Agent with `model` set. Model tier and **permission mode
  are orthogonal**: a Sonnet chip runs in bypass-permissions exactly like an Opus one
  — cheaper does not mean more approval prompts.
- **Event-driven, not poll-driven:** wake the expensive (Opus) tier on a watcher
  event, never on a timer.
- **Ephemeral over idle:** close bouts and re-bootstrap; don't pay to idle fat
  sessions. The trio + watcher are what make this safe.
- **Slim the bout:** compact a long *active* design session; just close an idle one.

---

## The full reference (read on demand)

[`META-session-guide.md`](META-session-guide.md) holds the lookups a bout needs only
occasionally — kept out of every bootstrap read so opening a coordinator stays cheap:

- **Per-aspect parameters** — what the five registry columns (spec · memory · deploy · invariants) mean.
- **Adding a new META (aspect)** — the scaffold protocol (the inverse of Bootstrap).
- **Aspect registry** — the table of every aspect (spec · memory · deploy · invariants/boundary) is its own doc, [`META-aspect-registry.md`](META-aspect-registry.md). Read *your* row on Bootstrap.
- **Surface ownership (the routing map)** — surface → owning aspect, plus the carve-first + deposit rules.
- **The scheduled reconciler (`meta-reconcile`)** — the unattended pulse + the `/queue` · `/reconcile` · `/prune` · `/launch` operator surfaces.
- **The fleet watcher (`meta-fleet-watch`)** — the observe-only predecessor poke.
- **The cross-aspect coherence pass (`meta-coherence`)** — overlap / collision detection (`/coherence`).
- **Intake routing (`/design`)** — the natural-language front door + the carve-vs-route gate.
