# META Substrate — coordinator-system aspect (spec)

- **Aspect id:** `substrate` (chips: `[META:substrate]`; ledger: `meta-state/substrate.json`)
- **Date:** 2026-06-15
- **Status:** draft (aspect carved this date per the guide's "Adding a new META" protocol; Initiative 1 below is the first body of work)
- **Memory:** `[[substrate-meta-2026-06-15]]`

---

## 1. Why this aspect exists

The META coordinator system — the skills, ledgers, scheduled sweeps, intake routing, and
the docs that tie them together — is **itself under active development**, yet it had no
home among the product aspects. Those aspects build *Evolve*; this one builds *the system
we use to build Evolve*.

It is deliberately **dual-home**:

- **Evolve repo** — `docs/META-session-guide.md`, `docs/meta-ledger-schema.md`,
  `docs/meta-reconcile-procedure.md`, `docs/meta-system-setup.md`, and any `tools/meta-*`
  helpers (all version-controlled; the scheduled-task procedures are mirrored to the
  operator-local `~/.claude/scheduled-tasks/*/SKILL.md`).
- **Claude Code harness** — the `.claude/skills/*` skill files (`/meta`, `/design`,
  `/launch`, `/queue`, `/status`, `/close`, `/reconcile`, `/coherence`, `/prune`,
  `/help`), the operator-local ledgers under `~/.claude/.../meta-state/`, the
  `~/.claude/settings.json` grants the unattended sweeps depend on, and the scheduled
  tasks themselves.

A single aspect spanning both homes is correct precisely because the *streamlining* work
crosses the boundary (e.g. a new intake source touches both a repo helper and a skill file).

## 2. Scope & ownership boundary

**OWNS:** the coordinator skills and their contracts; the ledger schema
(`docs/meta-ledger-schema.md`) + the `meta-state/<aspect>.json` shape; the scheduled
sweeps (`meta-reconcile`, `meta-coherence`, `meta-loose-ends`, future) and their
**unattended-run permission contract** (documented home: `docs/meta-system-setup.md`); the
intake + routing knowledge (the aspect registry — now its own doc,
`META-aspect-registry.md` — plus the `META-session-guide.md` surface-ownership map
and "Adding a new META" protocol); and **NEW** GitHub-issue intake (Initiative 1).

**DOES NOT OWN:** product behavior (the product aspects — this routes work *to* them, never
designs their content); the Claude Code harness internals (upstream — this aspect only
configures and consumes them); per-aspect ledger *content*.

**Boundary test:** a GitHub issue's *content* routes to its product aspect; the *mechanism*
that ingests and triages issues lives here.

## 3. Invariants

- **Recommend-only sweeps; operator-only in the red zone.** Closing an issue, merging a PR,
  dispatching new work, or carving/merging an aspect are confirm-first. Automated passes
  surface recommendations into `/queue`; they never act unilaterally.
- **Durable trio + ledger; conversation disposable.** Decision → spec; lesson → memory;
  live state → ledger. A fresh coordinator reconstructs from these alone.
- **Aspect ids are stable: ≤10 chars, kebab, never renamed once a chip carries `[META:<id>]`.**
- **Unattended-run permission contract.** Scheduled sweeps run in Ask mode honoring **only**
  `~/.claude/settings.json` allow rules (not project `settings.local.json`). `Write(...)`
  globs MUST be filesystem-anchored (`//Users/...` or `~/...`) — a single leading `/` is
  project-root-relative and silently fails to match (this stalled the sweeps for weeks).
  See `[[scheduled-tasks-only-user-allowlist]]`; document the contract in
  `docs/meta-system-setup.md`.
- **Skills load per-branch; keep global copies.** See `[[project-skills-load-per-branch]]`.

---

## 4. Initiative 1 — GitHub-issue intake

### 4.1 Motivation + the existing seam

Today `/design "<free text>"` is the only intake. Reported bugs/requests live in the GitHub
issue tracker, disconnected from triage. Goal: **one triage path for both ad-hoc ideas and
reported issues.** This generalizes a pattern one aspect already pioneered — the repo label
**`edr:agent-able`** (*"a human judged this fixable by a dispatched agent; requires a `Proof:`
line in the body"*). That is exactly the issue→triage→dispatch seam; Initiative 1 lifts it
fleet-wide.

### 4.2 Phase 1 — `/design <github issue>`

`/design 2656` or `/design <issue-url>` triages identically to `/design "<free text>"`.

1. **Issue-ref detection + fetch** in the `design` skill: a `#?N` / issue-URL arg → fetch via
   the `tools/meta-issue` helper (§4.4); else free-text (today's behavior).
2. **Label → routing prior:** an aspect-prefixed label (`<aspect>` / `<aspect>:agent-able`,
   generalizing `edr:agent-able`) is a *prior*, validated by content (labels go stale). The
   helper already surfaces `aspect_hints` (label prefixes before `:`).
3. **Provenance:** the new optional `issue: <N>` field on chips/backlog (§4.4).
4. **Loop closure:** an issue-born chip's PR carries `Closes #N` → GitHub auto-closes on
   merge, which the reconciler already detects. No new closing machinery.
5. **Write-back (recommended):** comment/label the issue (`meta:routed:<aspect>`) so the
   tracker reflects the triage outcome.

### 4.3 Phase 2 — `/triage` (scan open issues → recommend close vs design)

`/loose-ends` for the issue tracker; a new skill in the recommend-only sweep family.

1. Fetch all open issues (`gh issue list` excludes PRs).
2. Per-issue preliminary triage → one recommendation: **CLOSE** (stale/dup/wontfix/invalid/
   out-of-scope/already-fixed — cross-check merged PRs + code; reason required); **DESIGN →
   `<aspect>`** (actionable, reuse the `/design` classifier to pre-assign aspect); **READY →
   `<aspect>`** (actionable + spec'd enough to chip directly — generalizes `edr:agent-able` +
   the `Proof:` line; mirrors `/launch`'s READY BITE); **ASK** (needs reporter info).
3. Ranked digest, one line per issue (the `/queue` / `/loose-ends` shape).
4. Confirm-first actions (closing is **never** automatic): CLOSE → `gh issue close` + label;
   DESIGN → `/design #N` or deposit a `backlog` entry with `issue:N`; READY → dispatch a chip
   with `Closes #N`.
5. Edge-triggered: tag handled issues `meta:triaged` (or a state file) so re-runs surface
   only new/changed ones.
6. Later: a daily `meta-issue-triage` scheduled task posting recommendations into `/queue`,
   recommend-only — same maturity path (and the same §3 permission contract) as the existing
   sweeps.

### 4.4 Shared substrate

- **`tools/meta-issue <ref>`** — `#N`/URL → normalized record `{number, url, state, title,
  body, author, created_at, age_days, labels[], aspect_hints[], agent_able, proof,
  comments[]}`. Repo-pure (only talks to GitHub via `gh`; the *skill* matches against the
  registry). **Built + tested 2026-06-15** (verified against `#2656`: extracted
  `aspect_hints:["edr"]`, `agent_able:true`, and the `Proof:` pytest line).
- **Label convention** — formalize `<aspect>` / `<aspect>:agent-able` (generalizing
  `edr:agent-able`); optionally auto-create the labels from the registry.
- **Ledger provenance field** — add an optional `issue: <N>` to the chip schema (and note
  backlog strings may embed `#N`) in the **existing** `docs/meta-ledger-schema.md`.

### 4.5 Open forks (recommendation first)

- **Triage state of truth — GitHub or the ledger?** → GitHub issue = operator-facing intent
  (label+comment); ledger = execution state (`issue:N`); PR `Closes #N` = the join.
- **`/triage` interactive vs scheduled?** → interactive + confirm-first first; add the
  recommend-only sweep once trusted (the `/reconcile` → `meta-reconcile` path).
- **Adopt the `Proof:` requirement fleet-wide for READY?** → yes (a falsifiable acceptance
  test per auto-chippable issue).

### 4.6 Build order

1. `tools/meta-issue` helper **[DONE]** + add the `issue:` field to `docs/meta-ledger-schema.md`.
2. `/design <issue>` (Phase 1) — unblocks issue-triage immediately.
3. Aspect-label convention + (optional) auto-create labels.
4. `/triage` interactive skill (Phase 2 steps 1–5).
5. Optional: `meta-issue-triage` scheduled sweep (Phase 2 step 6).

## 5. Backlog seed (mirrored into the ledger)

- I1.b1 — `tools/meta-issue` **[done]**; add `issue:` field to `docs/meta-ledger-schema.md`.
- I1.b2 — teach `/design` to ingest `#N`/issue-URL (Phase 1; needs the skills, which live on `main`).
- I1.b3 — aspect-label convention + (optional) label auto-create.
- I1.b4 — `/triage` interactive skill (Phase 2).
- I1.b5 (later) — `meta-issue-triage` scheduled sweep.
- HK — document the unattended-run permission contract (§3) in `docs/meta-system-setup.md`.

## 6. Open questions

- Q1: Aspect id `substrate` (forced ≤10 chars by the carve protocol; chosen over the longer
  `meta-substrate`). Confirm before the first chip — then sticky. Alternatives: `meta-sys`, `coord`.
- Q2: Auto-create the aspect labels on the repo, or only on first use?
- Q3: Should `/design`'s free-text and issue paths share one helper that emits a normalized
  "intake record", so future sources (email, Slack) plug in the same way?

---

## 7. Initiative 2 — Smooth operation (operational health)

### 7.1 Diagnosis (2026-06-15)

The framework accumulated **operational debt with no janitor, while the automation engine was
down**. Measured state:

- **258 git worktrees** (56 locked) and **423 local branches already merged into `origin/main`**
  but never deleted — the real source of "which branch has the latest code?".
- The **main checkout was parked off-`main`** (on `docs/edr-best-practices-scan-2026-06-11`),
  so reading docs from it returned stale truth (this misled a live session).
- The **reconciler** — the engine that auto-merges clean PRs and relaunches stalled chips — was
  **stalling** (permission anchoring [fixed], desktop-dispatcher lag, leftover tray-prompt
  sessions). With the engine down, the PR backlog only grows.
- **14 open PRs**, of which **5 are unmanaged dependabot** noise; **0 truly conflicting**; 2
  `BLOCKED` (behind-main / a failing required check).

**Root cause:** no teardown/GC discipline + the engine down → worktrees, branches, and PRs
accumulate faster than anything clears them, and "latest code" becomes ambiguous because
nothing returns to a known-good baseline.

### 7.2 Threads

- **T1 — Worktree + branch GC.** One-time sweep (remove worktrees on merged+unlocked branches;
  delete the merged branches; triage locked/unmerged), **return the main checkout to `main`**,
  then a *standing rule*: chips tear down their worktree on `done`, plus a scheduled `meta-gc`
  sweep (or `/prune` extension) so it never re-accumulates.
- **T2 — Reconciler reliability** *(the engine; once it runs, T3 self-heals)*. Confirm the
  permission fix; resolve the desktop-dispatcher lag / residual tray prompts; clear stalled
  sessions. The unattended-run permission contract (§3) is a prerequisite.
- **T3 — PR-flow policy.** A dependabot policy (batch / auto-merge minor+patch); fix-forward
  `BLOCKED` PRs; coherence on multiple concurrent PRs from one aspect.
- **T4 — Branch-of-truth invariant.** `origin/main` = truth; the main checkout stays on `main`;
  worktrees are ephemeral + per-task; never read doc-state from a parked checkout. Document in
  `META-session-guide.md` + `meta-system-setup.md`.

### 7.3 Order

T1 + T2 first (T3 mostly self-heals once T2 runs); T4 documents the fix so it doesn't recur.

### 7.4 GC safety contract

The sweep is **safe by construction** — it uses git's own refusal semantics, never `--force`:

- **Conservative set** = a worktree whose branch is merged into `origin/main` **and** is
  unlocked **and** is neither the main checkout nor the active worktree. Locked worktrees
  (likely in-use sessions) and unmerged branches (open PRs, unpushed work) are excluded.
- `git worktree remove` (no `--force`) **refuses** a worktree with uncommitted/untracked
  changes; `git branch -d` (not `-D`) **refuses** an unmerged branch. So a logic error cannot
  destroy in-flight work — it can only fail loudly.
- Always print the remove/delete list before executing; report what git refused (→ the
  aggressive/manual triage list).

---

## 8. Initiative 3 — Portable substrate (run META from any account / harness)

### 8.1 The split that governs difficulty

The substrate has a **harness-agnostic half** (docs, `tools/meta-*` helpers, the
`meta-state/*.json` ledgers, the markdown memory — all plain files) and a
**Claude-Code-coupled half** (`.claude/skills/*`, `spawn_task`/Agent/Workflow,
`~/.claude/hooks/*`, `~/.claude/scheduled-tasks/*`, the `settings.json` grants +
unattended-run permission contract). Portability difficulty tracks that split.

### 8.2 T1 — Other Claude Code accounts (same laptop, same OS user, different login emails): ALREADY SHARED — no build

Verified 2026-06-17 (claude-code-guide + `~/.claude` inspection). The three accounts run
as **one OS user under one `$HOME`**, so there is exactly one `~/.claude/` and **no
per-account / per-org subdirectory**. Everything the substrate needs —
`settings.json`, `skills/`, `hooks/`, `scheduled-tasks/`, and `projects/.../memory/` +
`meta-state/*.json` — is `$HOME`-scoped and therefore shared across all three accounts by
construction. The only per-account state is the OAuth credential (macOS Keychain).

Consequences:
- The operator's chosen **shared-ledger / one-coordinated-world** model is satisfied
  for free — there is one ledger set.
- **Do NOT set `CLAUDE_CONFIG_DIR`** per account: it points each account at a separate
  `~/.claude-<x>` tree and would *fork* the very ledger we want shared. Isolation is the
  opposite of the requirement.
- **One real wrinkle — scheduled-task account binding.** Scheduled sweeps
  (`meta-reconcile`, `meta-coherence`, `meta-loose-ends`, `meta-fleet-watch`) run under
  *whichever account is active/last-logged-in when they fire*, and cannot be pinned. If
  the three accounts exist to spread rate-limit/quota, sweep cost lands unpredictably.
  Policy options: (a) keep one "automation account" active for the sweeps; (b) accept
  whichever-active. Operator decision; low stakes.

T1 work = **documentation, not engineering**: a "Multi-account" section in
`docs/meta-system-setup.md` recording the above (shared by construction; never
`CLAUDE_CONFIG_DIR`; the scheduled-task quota wrinkle + the chosen policy).

### 8.3 T2 — Cursor (deferred): re-implementation, not a copy

Half A ports for free (a Cursor agent pointed at `META-session-guide.md` + the ledgers
*is* a coordinator). Half B must be rebuilt as thin adapters: skills → `.cursor/rules/*.mdc`;
`spawn_task` chips → Cursor background-agents / `cursor-agent` CLI; scheduled sweeps → OS
cron; **hooks → no analog** (the cd&&git rewriter and a future prefix hook regress to
prose-only on Cursor). The coordinator *discipline* (trio + ledger + recommend-only sweeps)
ports perfectly; the *automation* ports partially, lower-fidelity. The enabling refactor is
to keep all real logic in the harness-agnostic core (docs + ledger schema + `tools/meta-*` +
procedures-as-markdown) and ensure **no logic hides only in a SKILL.md**, so each harness
adapter stays thin. Deferred per operator (CC-accounts first).

### 8.4 Order
T1 = a doc (near-zero); T2 (Cursor) deferred until the operator prioritizes it.

## 9. Initiative 4 — `[META:<id>]` prefix propagation

### 9.1 Root cause
The prefix is a **model-applied convention, not tool-enforced** (`META-bootstrap.md`
§Naming: *"The convention isn't tool-enforced"*; §Cost calls it an *"Enforcement gap"*). It
is reliably written only when the aspect id is salient — which the `/meta <id>` launcher
guarantees (forces the session title to `META <id>`, the id is the sole argument, the body
repeats the prefix rule) but the other entry points do not:
- **`/design`** derives the aspect mid-conversation, never retitles the session, and doesn't
  re-assert the rule → flaky.
- **child-spawned chips** are fresh sessions with no standing instruction to *keep*
  prefixing → any sub-chip / grandchild loses it entirely.

### 9.2 Fix — two layers (same shape as the cd&&git fix: prose where cheap, mechanism where prose can't bind)
- **Prose:** `design/SKILL.md` ends routing with the same *"lead your next response with
  `META <id>`"* retitle `/meta` uses; the chip-brief template gains a standing *"you are
  `META:<id>` — prefix every chip / PR / branch you spawn with `[META:<id>]`"* line.
  Converts the `/design` leg + first-generation children.
- **Mechanism (durable):** a PreToolUse hook on the spawn tool (`Task`/`spawn_task`) that
  reads a session-scoped *active-aspect* marker (written by `/meta` and `/design` at
  bootstrap) and auto-prepends `[META:<id>]` to any title lacking it. Deterministic analog
  of the H2 cd&&git hook; survives forgetfulness + grandchildren. **Gated on one feasibility
  check** (claude-code-guide): does PreToolUse fire on `Task`/`spawn_task` and allow
  `updatedInput` mutation of the *title*? Yes → durable fix; No → prose + reconciler
  tolerance is the ceiling.

## 10. Initiative 5 — substrate token/context budget

### 10.1 Diagnosis (2026-06-18)

The whole value of the META substrate is being a **cheap, always-available coordinator
surface**. But a single `/queue` was measured at **~190k tokens (≈14% of the operator's 5h
limit)** — the overhead defeats the purpose. Three compounding causes, all owned by this
aspect (full write-up: memory `substrate-token-budget-2026-06-18`):

1. **Per-session auto-load tax.** `MEMORY.md` (~53KB, already over its own ~200-line limit) +
   the project `CLAUDE.md` (~17KB) load on **every** session, META or not — ~17k tokens before
   the operator types anything.
2. **Raw whole-ledger reads land in MAIN context.** `/queue` (and `/reconcile`, `/coherence`,
   `/launch`) read all ~16 `meta-state/*.json` ledgers (~200KB ≈ ~50k tokens) **raw into the
   operator's main thread**, where they then persist and get re-read on every subsequent turn —
   the cost recompounds. `/queue` is *designed* to be ledger-only (no `gh`); the cost is purely
   how much it loads and where it lands.
3. **Ledger bloat.** ~95% of all chip entries are terminal (`done`/`live`/`merged`) and never
   pruned, despite the schema saying to prune at `/close`. The corpus is ~200KB; the largest
   single ledger (`platform.json`) is 53KB — bigger than the whole memory index. The bloat is
   dominated by un-pruned terminal-chip `note`/`output` bodies and multi-hundred-char
   `bout`/`next_action` prose walls.

**Root cause:** the read surfaces dump *durable, mostly-finished* state into the *live,
per-turn* context, and nothing keeps that state small. The fix is **shrink what loads + distill
it where it's read**, not cache 190k of dead ledger (the prompt cache has a ~5-min TTL and only
discounts back-to-back turns within one session — it does not help a cold reopen).

### 10.2 The four levers

| Lever | What | Where |
|---|---|---|
| **E1** | `tools/meta-queue` — a deterministic **projection** script that renders the decision queue (~3k of text) from the raw ledgers (~50k of JSON) so the read surfaces never load raw JSON into main context. | sibling "cheap-read" chip |
| **E2** | Run the read-heavy skills (`/queue`, `/reconcile`, `/coherence`, `/launch`) in a **throwaway subagent** that reads the ledgers in its own disposable context and returns only the rendered queue to the operator's main thread. | sibling "cheap-read" chip |
| **E3** | `tools/meta-ledger-prune` (idempotent batch migrator: collapse prior-bout terminal chips to `{id, title, pr, bucket, two_pass}`, backup-first, `--dry-run` default) **+ `/close` and reconciler enforcement** (prune on every write-back) **+ an explicit ~8KB per-ledger size budget** and one-line `bout`/`next_action` rule in `docs/meta-ledger-schema.md`. Attacks cause (3) at the source. | **this chip** |
| **E4** | `MEMORY.md` consolidation — fold the index back under its ~150-line / ~200-char-per-line budget, attacking cause (1). | operator-supervised follow-up |

E1 + E2 are delivered by the sibling **"cheap-read"** chip; **E3 is this chip** (tooling +
enforcement only). E1/E2 stop new raw reads from landing in main context; E3 keeps the thing
being read small; together they bound `/queue` to a few KB.

### 10.3 What is gated on the operator (not in these PRs)

E3's tooling deliberately stops short of mutating the operator's live ledgers, because the
`meta-state/` dir is **not git-tracked** — there is no `git revert` for it:

- **E3b — the one-time live-ledger `--apply`.** Run `tools/meta-ledger-prune --apply` against
  the live dir once these PRs merge. It writes a timestamped backup of the whole dir first
  (the only undo), then collapses ~55 prior-bout terminal chips (≈ −33KB at time of writing).
  Operator-supervised; reversible via the backup. The `--dry-run` default is what these PRs
  ship as the safe, repeatable preview.
- **E4 — `MEMORY.md` consolidation.** Same shape: a content edit to a non-git-tracked file,
  done with the operator, not auto-applied.

Both are reversible and low-risk, but they touch operator-local state, so they wait behind a
human hand rather than riding the PR merge.

---

## 11. Initiative 6 — Cross-session collision avoidance (dispatch-time check + `scope` field)

### 11.1 Diagnosis (2026-06-18)

With many aspects running in parallel, the same work can get launched twice or two sessions
launched at cross purposes. The substrate's collision detection was **after-the-fact and
partial**, leaving three gaps:

1. **No dispatch-time dedup.** `/launch` and `/design` spawn work with **no pre-spawn in-flight
   check** — nothing asks "is anyone already on this?" *before* the `spawn_task`. Overlap is
   caught only later.
2. **The later catch is cross-aspect-only.** The one automated overlap pass, `/coherence`
   (`meta-coherence`), runs **daily**, looks **only across aspects**, and is **read + recommend
   only**. Two duplicate chips **within one aspect** are caught by **nothing** — the reconciler
   reconciles each chip's *status* against `gh` and never compares two chips to each other.
3. **Mid-flight scope is invisible by design.** A dispatched child runs in its own session and —
   correctly, to avoid cross-session write races — **writes nothing to the shared ledger**. So
   what a child is *actually touching* is invisible until its PR lands. The collision surfaces at
   the worst time: as a merge conflict or a redundant PR.

The cost is the `#2834→#2840→#2841`-class repair churn (the "Serialize the contended files" rule
in `META-bootstrap.md` is the existing, *manual* mitigation) and wasted parallel sessions.

### 11.2 Design — dispatch-time check + a declared `scope` field

The operator greenlit a **dispatch-time check plus a scope field** — explicitly **not** a live
claim-board (§11.3). It is the **dual of Initiative 5's `tools/meta-queue`**: where meta-queue
answers *"what needs me?"*, this answers *"who is already on this?"*, and it reuses I5's machinery
(the ledger-dir resolver/loader, the terminal-bucket set, the projection-script-in-a-subagent
pattern so the heavy read never lands in the operator's main context).

**(a) `tools/meta-inflight` — the check (Deliverable 1).** A stdlib-only executable.
`tools/meta-inflight --aspect <id> [--keywords …] [--scope <globs>]` scans three in-flight
signals and prints overlaps ranked by match strength (or a clean "no overlap found"):

- **non-terminal chips** across every ledger (`bucket` not in `{done, live, merged}`) — matched
  on each chip's declared `scope` globs, title, note, and `pr`/`task_id`; un-dispatched
  `backlog[]` strings count too;
- **open fleet PRs** — one `gh pr list --json` call (never text-parsed); "fleet" = a `claude/*`
  head branch **or** a `[META:<id>]` title prefix (real fleet PRs land on non-`claude/` branches
  too); the `files` field gives file-level overlap, the title gives aspect + keywords;
- **live `[META:*]` sessions** — only when the calling skill supplies them via `--sessions-json`
  (the script is stdlib-only and cannot list sessions itself — the same script-vs-skill liveness
  split `tools/meta-queue` uses; omitted ⇒ reported as "not supplied").

Ranking is **scope ≫ keyword > aspect**: a file-level scope overlap is the strongest signal and
surfaces **across aspects** (two aspects touching one file is exactly the collision to catch); an
aspect-only match is suppressed on a *specific* query (so it doesn't flood) but answers an
aspect-only query ("what's running in `apps`?"). `--json` for programmatic callers; a `gh` failure
degrades to a note rather than blocking (the tool is advisory). Pinned by
`packages/admin/tests/test_meta_inflight.py`.

**(b) The `scope` field (Deliverable 2).** An optional `scope` on a chip — a list of file globs the
chip *intends* to touch, **declared by the coordinator at dispatch**. The child still writes nothing
to the ledger (the race-avoidance invariant holds; `scope` is a dispatch-time *intent*, not a
child-maintained claim). It is what gives the check **file-level precision**. Documented in
`docs/meta-ledger-schema.md` ("Dispatch-time collision check").

**(c) Wiring into the dispatch paths (Deliverable 3) — ADVISORY + confirm-first.** Consistent with
the standing "never auto-dispatch new work" gate, the check is run **before any spawn** and any
overlap is surfaced for the operator to **merge** (fold into the in-flight effort), **proceed**
(a genuinely distinct slice — noted), or **cancel**:
- `/launch` step 3 runs it per approved bite (and records the bite's `scope` in the new chip row);
- `/design` runs it at **intake** (the earliest catch — before a second coordinator even opens);
- the **chip-brief preamble** (`META-bootstrap.md` naming-convention rule 2) gains a third
  standing line so the check **propagates to grandchildren** the same way the `[META:<id>]` prefix
  line does (both are model-applied, so both must be re-asserted each generation).

**(d) Within-aspect duplicate detector in coherence (Deliverable 4).** The daily `/coherence` pass
keeps its three cross-aspect detectors and gains a fourth — `dupin:<aspect>:<A>+<B>` — flagging two
non-terminal chips in the **same** aspect doing the same work (skipping pairs that are clearly,
deliberately sequenced). This is the **after-the-fact safety net** for the dispatch-time check:
the case it couldn't catch (a duplicate dispatched before the check existed, or waved through).
Per-chip *status* reconciliation stays the reconciler's; redundant-*pair* detection is now
coherence's.

### 11.3 Explicitly deferred to Phase 2 — a live claim-board

A **live claim-board** — children continuously writing their in-flight file/scope claims to shared
state so collisions are visible in real time — was considered and **deliberately deferred**. It
would re-introduce exactly the cross-session write contention the "child writes nothing" invariant
(§11.1 gap 3) avoids, for a marginal gain over a dispatch-time intent declaration plus the daily
within-aspect sweep. The dispatch-time check + `scope` field is the chosen Phase 1; the claim-board
is Phase 2, to be revisited only if dispatch-time intent + the coherence safety net prove
insufficient in practice.

---

## 12. Initiative 7 — Ownership-scoped session auto-archive

### 12.1 Diagnosis (2026-06-19)

The Claude Code / CCD **"Auto-archive on PR close"** Setting over-fires: it archives long-lived
`/meta` coordinators, `/queue`, and `/design` sessions **mid-life**. Root cause is a trigger on
**association, not ownership** — it retires a session when *any* PR the session **touched or
merged** closes, rather than only when the session's **own** worktree-branch PR merges. A
coordinator (and `/queue`, and the reconciler) owns no PR of its own but routinely **merges other
sessions' chip PRs** (`/queue`'s `merge #N`, the reconciler's auto-merge of green chips).
Association-based triggering reads those merges as "this session's work is done" and kills a session
that is still coordinating. This is why the Setting is — and stays — **OFF**
(`[[feedback-auto-archive-on-pr-close-disabled]]`).

### 12.2 Rule — auto-archive is ownership-scoped (normative)

A session is **auto-archivable** only when **all three** hold:

- **(a)** its `cwd` is a **dedicated worktree** under `.claude/worktrees/` (not the repo root); **and**
- **(b)** the **merged** PR's **head branch == that worktree's owned branch** (the branch the worktree
  was created on); **and**
- **(c)** the session has **no other open owned PR**.

A merge event that fails any of (a)–(c) — most importantly a session merging a PR whose head branch
it does **not** own — is **not** an archive trigger.

**Repo-root sessions are never auto-archivable.** Every `/meta` coordinator, `/queue`, `/design`, and
scheduled-brain run executes from the repo-root checkout and **owns no branch**, so condition (a)
(and therefore (b)) can never hold. They are cleared only by the **supervised** `/prune` sweep, never
automatically.

**The discriminator already exists** in `list_sessions` metadata — no new bookkeeping is needed:

| | `cwd` | owned branch | `prNumber` |
|---|---|---|---|
| **chip** (auto-archivable) | a `.claude/worktrees/<name>` dir | yes | its own PR |
| **coordinator** (never auto-archivable) | repo root | none | none |

A correct implementation keys on *(chip-shaped, own-branch PR merged)* and ignores every *(merged a
PR I do not own)* event.

### 12.3 The `archive_session` supervision constraint (normative)

`archive_session` is **unavailable in unsupervised / headless mode** (verified 2026-06-19). Therefore
**no scheduled or otherwise automatic substrate-side prune can archive anything** — a headless
`meta-prune`-style sweep can *recommend* archives into `/queue` but cannot execute them. Two
consequences fix the division of labor:

- The **only** automatic archiver is the **upstream CCD "Auto-archive on PR close" Setting** — a Claude
  Code / CCD harness feature that sits **upstream of this aspect's ownership boundary** (§2: "the Claude
  Code harness internals (upstream)"). The substrate does not own it; it can only specify the contract
  (§12.2) the Setting must satisfy and choose whether to enable it.
- Until that Setting is corrected to be **ownership-scoped** per §12.2, it stays **OFF**, and **`/prune`**
  — interactive, where the `archive_session` prompt succeeds — is the **supervised** sweep that clears
  merged-chip and idle sessions.

### 12.4 Coordinators are PR-less — the archive loophole is removed (normative)

Tighten the existing coordinator discipline to an **absolute**: a coordinator **NEVER opens its own
PR**. All coordinator output — scaffolds, spec / doc / registry edits, this section included — ships as
**chips** with their own worktree, branch, and PR.

This **removes** the two carve-outs that are exactly what let a still-active design bout be archived
when a PR merges:

- the *"...or, at most, a single bout-deliverable PR"* allowance; **and**
- the parenthetical carve *"a coordinator that does open a PR has, for that PR, acted as a chip — so
  archiving it once that PR merges is correct, not premature."*

The PR-less rule and the ownership-scoped Setting are mutually reinforcing: the rule guarantees a
coordinator presents **no own-branch merge** for the Setting to fire on, and the Setting **ignores**
the cross-owned merges a coordinator *does* perform (§12.2). Neither alone is sufficient — a tolerated
coordinator PR would re-open the §12.1 failure even under an ownership-scoped Setting — so both are
normative.

### 12.5 Companion guide edit (gate CLEARED — prose now aligned)

The matching **prose** edit lives in `docs/META-bootstrap.md` — **How a META session behaves** rule 2
(the auto-archive paragraph). It was originally **GATED** behind two in-flight efforts that
edit/restructure the operator-facing docs and would have collided:

- **PR #3021** (`[META:public]` carve, branch `meta-public-carve`); and
- the substrate **E5** guide-consolidation work (a follow-on within §10's Initiative-5 family).

Both have since settled — PR #3021 merged, and **E5 shipped as PR #3129**, which **split** the old
`docs/META-session-guide.md` doctrine doc: the every-bout lifecycle prose (**How a META session
behaves**, **Naming convention**, **Session lifecycle**) moved to the new lean `docs/META-bootstrap.md`,
while the on-demand reference (registry · ownership map · reconciler · `/design` intake) stayed in
`META-session-guide.md`. The companion edit therefore targets **`docs/META-bootstrap.md` → "How a META
session behaves" §2** (not the old §82–85 / §430–437 line ranges in `META-session-guide.md`), and **it
has now been made** — the rule 2 prose deletes both §12.4 carve-outs and reads as an absolute PR-less
rule. This spec section remains the durable record of the corrected model.

### 12.6 Reversibility

Docs-only and fully reversible: this section adds normative text to the substrate spec and changes no
code, schema, or operator-local state. The behavior it *specifies* — the Setting staying OFF and the
PR-less rule — is already the operating posture; §12 records the corrected rationale and the ownership
contract a future Setting fix must meet.

---

## 13. Initiative 8 — auto-sync the operator-global `~/.claude/skills` mirror

### 13.1 The gap (the live defect, 2026-06-22)

The launcher/coordinator skills (`/meta`, `/design`, `/close`, …) live in **two** places (§1, and
`[[project-skills-load-per-branch]]`):

- the **repo** copy at `.claude/skills/<n>/SKILL.md` — version-controlled source of truth, but it
  loads **only on a branch / worktree whose tree contains it**; and
- the **operator-global mirror** at `~/.claude/skills/<n>/SKILL.md` — the copy that loads across
  **every** branch / worktree and **every** Claude Code account (one `$HOME`, §8.2).

The mirror is what actually guarantees availability. But it had **exactly one refresh path**: a
**manual** loop —

```bash
for s in meta design close …; do
  git show origin/main:.claude/skills/$s/SKILL.md > ~/.claude/skills/$s/SKILL.md
done
```

— and **no auto-sync**. So the mirror silently drifts whenever someone forgets the loop after a skill
change merges. **Proven failure:** the mirror was stale since **Jun 14**, so the merged Initiative-4
`[META:<id>]` prefix mechanism (the `prepend-meta-prefix.sh` hook, §9) was a **silent no-op for 8
days** — the hook was installed and correct, but the *marker-write* step it depends on lived only in
the stale `/meta` `/design` `/close` skills, so the marker was never written and the hook always
passed through. A stale mirror is the most insidious kind of failure: everything *looks* installed.

This is the same **prose-can't-bind** shape as H2 (the cd&&git hook, §setup-doc) and I4 (the prefix
hook, §9): a discipline that must be performed by hand eventually isn't. The fix is the same — make a
**mechanism** do it deterministically.

### 13.2 Mechanism — `tools/meta-skills-sync` + a SessionStart hook

**(a) `tools/meta-skills-sync` (the tool).** A stdlib-shell (`bash` + `git`) executable that refreshes
`~/.claude/skills/<n>/SKILL.md` from `origin/main:.claude/skills/<n>/SKILL.md`. Contract:

- **No network.** It reads the **locally-known** `origin/main` ref (`refs/remotes/origin/main`) — it
  **never** fetches (network on session start is banned). The mirror tracks whatever the local
  `origin/main` already says `main` is — byte-for-byte what the manual `git show origin/main:…` loop
  did.
- **Additive / overwrite-from-origin only.** It writes a mirror file **iff** its bytes differ from
  origin's (byte-exact `cmp`, idempotent). It **NEVER deletes** a skill present in the mirror but
  absent on `origin/main` — that could be a local-only / operator skill. Sync is one-directional.
- **HEAD-unchanged fast path.** When `origin/main` HEAD equals the last **successfully** synced ref
  (cached at `~/.claude/meta-state/skills-sync-last-ref`) the common case costs a couple of `git`
  calls (repo validation + a `git rev-parse`) and exits, skipping the ~N `git show`s — measured
  ~0.12s in the real hook path. The cache is written **only on a clean apply** (zero write
  failures), so a partial failure retries next run. `--force` bypasses the fast path; `--dry-run`
  never reads or writes the cache.
- **Repo discovery.** `--repo <path>` forces it; otherwise it tries the `--cwd`'s git toplevel, then
  `$EVOLVE_REPO`, then `~/GitHub/evolve`, then `/Users/Shared/evolve-repo`. No usable repo / no
  resolvable `origin/main` ⇒ **clean no-op, exit 0** (the only non-zero exit is a usage error).
- `--dry-run` reports `N would refresh, M unchanged` and mutates nothing; apply prints `N refreshed,
  M unchanged`.

**(b) `tools/hooks/meta-skills-sync-on-start.sh` (the SessionStart hook).** Locates a runnable copy of
the tool (session checkout → defaults → operator-local install) and runs it. **Fail-safe contract
(mirrors H2/P3):**

- `trap 'exit 0' EXIT` + `set -euo pipefail` — **any** error / signal exits 0.
- **Emits nothing on stdout** — a SessionStart hook's stdout is *injected into the model context*
  (verified against the CC hook docs), so the tool's summary is swallowed (`>/dev/null 2>&1`); the
  hook is silent by construction and never alters the session.
- **Never blocks / never materially delays.** SessionStart cannot block the session (its exit code is
  ignored) but the session **waits synchronously** for the hook, so the fast path + a short `timeout`
  (10s) on the settings entry keep it cheap. No network, bounded time.
- **No-ops silently outside an Evolve checkout.**

### 13.3 Semantics — affects the NEXT session (by design)

Skills load at session **start**, so a refresh during SessionStart takes effect on the **next**
session — *identical* to the manual loop's documented "restart to pick up a change". The win is not
instant-apply; it is that the mirror **self-heals on every Evolve session** instead of drifting for
days. With multiple merges/day on the laptop's local `origin/main`, the mirror is at most one session
behind, never 8 days.

### 13.4 (c) Discipline — belt-and-suspenders

The auto-sync is the mechanism; the discipline remains: **any chip that ships a `.claude/skills/` or
`tools/hooks/` change must refresh the mirror post-merge** (run `tools/meta-skills-sync --force`, or
the documented manual loop). The auto-sync covers the forgotten case at next-session granularity; the
post-merge refresh makes the change live immediately. Documented in `docs/meta-system-setup.md`.

### 13.5 Going live is operator-gated

The hook is a **global SessionStart hook — it runs at the start of every Claude Code session across
all accounts**. That blast radius means the actual `~/.claude/settings.json` registration is held for
an **operator merge / install**. The PR proves both states (mirror-current ⇒ fast-path no-op;
artificially-staled skill ⇒ refreshed) and ships the version-controlled tool + hook + test; the
operator flips it on. Reversible: remove the `hooks.SessionStart` block (and optionally the two
`~/.claude/hooks/` copies).

### 13.6 Reversibility

The repo half (tool + hook + test + these docs) is ordinary version-controlled code, fully revertible.
The operator-local half is one additive `hooks.SessionStart` entry + two file copies under
`~/.claude/hooks/`; disabling is removing that block. The tool only ever **overwrites-from-origin or
creates** mirror files and **never deletes**, so the worst case of a logic error is a mirror file
matching `origin/main` (the intended state) — there is no data-loss path.

## 14. Initiative 9 — marker self-registration (propagate `[META:<id>]` down the spawn tree)

### 14.1 The gap (the live defect, 2026-06-22)

I4 (§9) installed a PreToolUse hook (`prepend-meta-prefix.sh`) that prepends `[META:<id>] ` to a
spawned chip / subagent title — but **only when the spawning session has written an active-aspect
marker for its cwd** (`~/.claude/meta-state/active-aspect/<key>`). The marker is the hook's *only*
input, and **only `/meta` and `/design` write one** (at bootstrap, once the id resolves). That covers
*coordinators*. It does **not** cover the children they spawn:

- A **chip** session is a fresh session. It never runs `/meta`, so it never writes a marker, so the
  PreToolUse hook sees no marker for the chip's cwd and passes through. The sub-agents / sub-chips the
  *chip* spawns are therefore **un-prefixed** — the `[META:<id>]` lineage goes dark **one level below
  the coordinator**.

**Proven, 2026-06-22:** instrumenting the live hook showed the `[META:<id>]` prefix on **only 2 of
~40 live sessions** — exactly the handful that had run `/meta`/`/design`. Every chip-spawned descendant
was unattributed. This is the same **prose-can't-bind** shape as the rest of the substrate's hooks: a
discipline that must be re-performed at every level eventually isn't.

### 14.2 Fix — a SessionStart writer keyed off the session TITLE

The coordinator already *carries* its aspect in a place every descendant inherits: the **session
title**. A chip spawned by a `[META:rsi]`-prefixed coordinator is itself titled `[META:rsi] …` (that
is exactly what the PreToolUse hook guarantees one level up). So the propagation rule becomes a fixed
point: **any session whose title carries the tag self-registers its own marker on startup, and its own
spawns then get prefixed by the PreToolUse hook — which titles its children with the tag, which makes
*them* self-register, …** The tag flows down the entire tree (coordinator → chip → grandchild → …).

**`tools/hooks/meta-marker-register-on-start.sh` (the SessionStart hook).** On every session start it
reads the `session_title` + `cwd` from the payload and, if the title matches either tag form, writes
the active-aspect marker for that cwd — the *same* file, key, and reader as the `/meta`/`/design`
writer. It is the **writer complement** to the skill-side write: skills register coordinators from an
explicit id; this hook registers any tagged child from its title. Two title forms (belt-and-suspenders
so coordinators self-register too, even via `/design` which doesn't retitle reliably):

- **chip / child:** `^\[META:(<id>)\]` — e.g. `[META:rsi] Coalesce …`
- **coordinator:** `^META (<id>)$` — e.g. `META substrate` (the title `/meta` forces)

`<id>` is validated with the **exact** strict-kebab regex from `prepend-meta-prefix.sh`
(`^[a-z0-9][a-z0-9-]{0,30}$`). A non-matching or invalid title (`Fix the widget`, `[META:Bad Id]`,
`[META:../etc]`, `META not a kebab`) writes **nothing** — the hook only ever writes a clean, valid id.

**Key agreement is the load-bearing invariant.** The cwd→key computation is mirrored **byte-for-byte**
from `maa_key()` in `meta-active-aspect.sh` / the inline copy in `prepend-meta-prefix.sh`; if it drifts,
the marker is written under a key the reader never looks for and the whole thing silently no-ops. The
hook is self-contained (it does **not** source the helper, so a missing helper can't break it), and the
equivalence is guarded **end-to-end** by `test_meta_marker_register.sh`, which writes via the hook,
computes the key via the real helper, and asserts `prepend-meta-prefix.sh` then prefixes a spawn from
the same cwd.

### 14.3 Fail-safe contract (mirrors I8 / the H2 cd&&git hook)

The worst case is **today's behavior** — a chip's spawns stay un-prefixed — never a blocked, delayed,
or errored-loud session start:

- `trap 'exit 0' EXIT` + `set -euo pipefail` — **any** error / signal exits 0.
- **Emits nothing on stdout** — a SessionStart hook's stdout is *injected into the model context*
  (verified against the CC hook docs), so this hook is silent by construction.
- **No network. Bounded / instant** — a couple of `jq` + `shasum` calls and one small atomic write
  (temp-in-same-dir + `mv`). Most sessions hit the no-match branch and exit at once.
- **Never deletes / clears** a marker (that is `/close`'s job); writes are idempotent overwrites.

### 14.4 Known limitations

- **cwd-key collision (last-writer-wins).** The marker is keyed by canonical cwd, so two sessions in
  the *same* cwd share one marker — whichever started last wins. This is acceptable because chips run
  in their **own worktrees** (unique cwd), so collisions don't arise in the propagation path; it is
  documented, not solved here. (A future session-id-keyed marker would remove it, at the cost of the
  PreToolUse hook needing the spawning session's id, which the payload doesn't currently carry.)
- **Affects sessions started after install.** The hook fires at session **start**, so already-open
  sessions need a restart to begin self-registering — identical to every other SessionStart-driven
  mechanism here.

### 14.5 Going live is operator-gated; reversibility

Like I8, this is a **global SessionStart hook** (runs at the start of every Claude Code session, all
accounts), so the `~/.claude/settings.json` registration is an **additive** second `hooks.SessionStart`
entry held for an operator install. Reversible: remove that block (and optionally the
`~/.claude/hooks/meta-marker-register-on-start.sh` copy); the hook only ever **creates / overwrites**
its own marker file and **never deletes**, so a logic error's worst case is a marker matching the
intended state — there is no data-loss path. With the hook gone, marker-writing falls back to the
`/meta`/`/design` skills (coordinators only) — i.e. exactly today's behavior.

## 15. Initiative 10 — merge-rule v2: recognize verdicts + restrict `operator_merge` + auto-resolve CI reds

### 15.1 The gap (the live defect, 2026-06-23)

Operator report: *"the meta framework should merge PRs when able, not wait for operator approval — it
is becoming too chaotic"* and *"CI checks block PRs and don't get resolved."* Live inspection
confirmed both as **mechanism bugs**, not doctrine gaps:

- **The auto-merge rule strands reviewed PRs.** `meta-reconcile`'s log showed `"merged":0` on **every
  run for days**, while green, MERGEABLE, *already-reviewed* PRs piled up (3–7 queued each run). Root
  cause: the mechanical rule does an **exact string compare** `two_pass == "PASS"`, but chips record
  the verdict as free prose — e.g. `"SHIP (2 non-blocking concerns) — independent adversarial
  reviewer…"`, `"SHIP / CONCERNS-no-blockers…"`. A ship-ready PR whose verdict reads "SHIP" never
  matches, so it never auto-merges. The doctrine (How-a-META §5, *"auto-merge is the default, a
  denylist not an allowlist"*) was correct; the **mechanism silently implemented an allowlist of one
  literal string**. Compounding: `operator_merge:true` was being set on *reversible, non-privileged*
  chips, needlessly holding green+reviewed PRs for a human click.
- **CI reds don't get resolved.** The same log showed `"fixed":0` on every run — the fix-forward that
  is *supposed* to clear mechanical reds wasn't firing — and the dominant red is structural: the
  whole-repo no-growth / file-size ratchets (`server.py`, `cli.py`; baseline `tools/file-size-baseline.txt`)
  are **absolute-cap scoped**, so when one merge pushes a capped file over cap, `origin/main` reds and
  **every** subsequent PR inherits the red until a human re-freezes — and the re-freezes thrash (6 of
  18 consecutive `main` commits were pure cap-refreeze churn, off-by-N: `#3107→#3109→#3114→#3113`).
  See [[green_pr_reds_main_on_whole_repo_ratchet]] + [[feedback_server_py_no_growth_cap_and_bundled_gate]].

The two are one knot: even a corrected merge rule cannot *flow* while a whole-repo ratchet keeps
redding `main`.

### 15.2 Fix — three substrate changes (normative), one routed CI-gate change

Operator chose **Recognize + restrict + auto-review** (over "green CI is enough" / "vocabulary-only").
Authoritative home: the rule in `docs/meta-ledger-schema.md`, the sweep in
`docs/meta-reconcile-procedure.md`, kept in lockstep by `/status`, `/reconcile`, `/queue`.

1. **Normalize the verdict.** A single `verdict_is_pass(two_pass)` predicate (defined once in
   meta-ledger-schema.md, referenced — never re-derived — by the procedure + skills): PASS-equivalent
   iff the verdict starts-with/equals `{PASS, SHIP, APPROVE, APPROVED, LGTM}` (case-insensitive) **and**
   carries no blocking marker (`FAIL`, `BLOCK*`, "DO NOT MERGE", or `CONCERNS` unless explicitly
   non-blocking). `pending`/empty/`required …`/`n/a …` = **not** mergeable. **Safety invariant: this
   only recognizes a verdict a reviewer already wrote — it never invents PASS where none exists.** The
   rule becomes `auto-merge IFF bucket==open_green AND verdict_is_pass(two_pass) AND reversible AND !operator_merge`.
2. **Restrict `operator_merge:true`** to `privileged OR !reversible`. Forward rule for new chips; for
   existing ledgers the reconciler emits an **advisory queue flag** ("operator_merge on a
   reversible+non-priv chip — clearable") rather than silently overriding a deliberate human hold, and
   never edits another aspect's ledger.
3. **Auto-resolve, don't poke.** (a) A green-but-*unverified* reversible/non-privileged PR triggers an
   **auto-dispatched review chip** (bounded `review_count<2`) whose in-chip two-pass writes the verdict;
   the next sweep merges on its PASS — instead of poking the operator. Privileged/irreversible still
   poke for a human review. (b) The fix-forward for mechanical reds is hardened to actually fire, plus
   **fleet-block detection**: when `main` is red on a whole-repo ratchet, route a fix chip to the
   *owning* aspect and flag the queue, rather than leaving every other PR to inherit the red.

**Companion (routed to `diligence`, not built here):** diff-scope the file-size / no-growth ratchets so
a PR fails **only if it grows a capped file above baseline** (preserving shrink-only) — removing the
root cause of the fleet-block. The substrate fleet-block detection in 3(b) is the interim handling
until that lands.

### 15.3 Sequencing, scope-exclusion, reversibility

The merge-rule chip (M1) **deliberately excludes `docs/META-session-guide.md`** to avoid colliding with
the in-flight E5 guide-split (caught by `meta-inflight` at dispatch); the guide §5 doctrine is already
correct, so only the *mechanism* docs/skills change. Because M1 changes the auto-merge policy *itself*,
it carries `operator_merge:true` (a human confirms the new rule before it goes live) and gets
auditor-grade in-chip two-pass. After merge, the scheduled `~/.claude/scheduled-tasks/meta-reconcile/SKILL.md`
must be re-mirrored from the procedure (operator-local) for the headless sweep to adopt the rule.
Reversible: every change is a doc/skill edit revertable with `git revert`; the normalizer only ever
*recognizes* recorded verdicts, so a logic error's worst case fails closed (holds a PR), never
auto-merges an unreviewed/blocking one — which the auditor pass explicitly tests.

## 16. Initiative 11 — multi-project substrate (run META in a second project on the same machine)

**Problem.** The operator wants the Evolve dev substrate available in a **second, separate
project** on the **same machine and `$HOME`**: **`cjalden/calgraph`** (a Python backend; first
external consumer). This is the case Initiative 3 ("Portable substrate", §8) never covered —
§8 generalized across *accounts / tools / one repo*, not a *second repo alongside Evolve*.

**The linchpin fact — `~/.claude/` is home-scoped, so ~60% of the substrate is ALREADY live in
any repo on this machine by construction.** The 10 launcher skills (global `~/.claude/skills/`
mirror, cwd-independent), the PreToolUse/SessionStart hooks (user-level, fire every session),
per-aspect memory (`~/.claude/projects/<slug>/memory/`, auto-isolated by project slug), and the
cwd-keyed active-aspect marker (auto-isolated) need **zero** work for a second project.

**What's genuinely Evolve-bound (the whole generalization surface — it's small):**
1. **Repo slug `cjalden/evolve`** — hardcoded in **7 files**: skills `meta`/`status`, docs
   `meta-reconcile`/`meta-coherence`/`meta-system-setup`, tools `meta-inflight`/`meta-issue`
   (the two tools already take `--repo` with a default → just repoint the default at a config).
2. **Doctrine docs `docs/META-*.md`** — skills read them repo-relative; a fresh project lacks them.
3. **The aspect registry** (`META-aspect-registry.md`, extracted from `META-session-guide.md`) — Evolve's aspects; CalGraph needs its own.
4. **`tools/preflight` + `tools/ui-style-lint`** — Evolve's CI-mirror / SPA-linter; a Python
   project's chip DoD needs its own (ruff/pyright/pytest).
5. **Scheduled sweeps** (reconcile/coherence/fleet-watch/loose-ends/shipped-digest) hardcode
   repo+slug — **phase-2, deferred**; interactive `/meta` `/status` `/close` work per-project via
   the config first, and the scheduled procedures are repointed to read the config as a follow-on.

### 16.1 Decision — B3 staged-extract (operator-approved 2026-06-30)

A thin per-project **`.claude/meta.json`** parameterizes the hardcoded bits; a later
`meta-substrate-sync` tool pushes doctrine + generic tools into a consuming repo. **Evolve stays
the substrate's dev-home**; CalGraph is the first external consumer.

- **Rejected A (vendor/fork the substrate into CalGraph):** two copies drift — contradicts the
  aspect's "one system" charter.
- **Rejected B2 (a dedicated `meta-substrate` repo):** the clean eventual end-state, but it
  migrates Evolve's substrate *out* — more upfront work than a second project earns today. B2
  stays the target once a 3rd project justifies it.

### 16.2 `.claude/meta.json` schema

A small, extensible per-project config at the checkout root. All fields optional; a missing/blank
field falls back to Evolve's own value, so a checkout with no `meta.json` behaves exactly as before
(safe mid-flight migration). Evolve ships its own `meta.json` restating these defaults, so Evolve's
behavior is byte-for-byte unchanged.

```json
{
  "repo_slug":     "cjalden/evolve",             // OWNER/REPO for gh calls
  "registry_path": "docs/META-aspect-registry.md",  // the aspect-registry doc, repo-relative
  "preflight_cmd": "tools/preflight"              // the "run what CI runs" command
}
```

The **single resolver** is `tools/meta_config.py` (stdlib-only): it walks up from the cwd to the
nearest `.claude/meta.json`, returns the merged config, and falls back to the defaults on absent /
unreadable / malformed / non-object / blank-field input (never raises). `tools/meta-config` is the
thin CLI wrapper (`tools/meta-config [field] | --all`) so markdown procedures and shell one-liners
resolve the slug without a hardcode. The two Python tools import the resolver for their `--repo`
default (override still works); the skill/doc procedures reference the config as the slug source —
headless procedures read the JSON with the Read tool rather than shelling out (their tool discipline
bans command substitution).

### 16.3 Bite plan (P1 keystone → P2 sync tool → P3 seed CalGraph)

- **P1 (keystone — this initiative's first bite, Evolve repo):** introduce `.claude/meta.json` +
  the `meta_config` resolver (+ `meta-config` CLI) + a test (present / absent-fallback / malformed-
  fallback); author Evolve's own `meta.json`; repoint every functional hardcoded `cjalden/evolve` to
  read the config (fallback to `cjalden/evolve` so migration is safe before every consumer is
  repointed); record this §16. Reversible, non-privileged config/parameterization change.
- **P2 (after P1):** `meta-substrate-sync <target>` — copies `docs/META-*.md` + the generic
  `tools/meta-*` into a target repo and scaffolds its `.claude/meta.json`; add a "second project,
  same machine" section to `docs/meta-system-setup.md`.
- **P3 (gated on the operator cloning `cjalden/calgraph` locally):** seed CalGraph's substrate
  skeleton — run the sync, author its `.claude/meta.json` (Python preflight), a starter empty
  registry, a minimal `tools/preflight` (ruff/pyright/pytest; grows with the repo). CalGraph's own
  aspects define themselves as its first real subsystems land.

**Operator prep gate:** P3 needs a local CalGraph clone (the repo is empty today) —
`git clone cjalden/calgraph ~/GitHub/calgraph`.

## 17. Initiative 12 — substrate hardening (2026-06-30 audit)

This is the **umbrella design-record** for the hardening initiative that follows the 2026-06-30
self-audit. Subsequent bites (the A/B/C backlog below) reference this section for the shared
rationale rather than restating it. Source: memory `substrate-audit-2026-06-30`.

### 17.1 Method + verdict — the pipeline SHIPS

The audit was a retrospective on ~2 weeks of META use in Evolve (window 2026-06-16 → 30), run as
**four parallel read-only auditors** (throughput/flow, failure-mode clustering, surface/complexity,
portability). It was motivated by the CalGraph replication ([[substrate-multi-project-calgraph-2026-06-30]]):
harden the framework **before** porting so CalGraph inherits the best version, not the leaks.

**Verdict: the pipeline ships — the problems are leaks + latent port-coupling, not velocity.**
427 fleet PRs, **95.3% merged**, median 25 min open, 0 stalled. Dispatch-and-ship works. What to
**keep**: the lifecycle trio (`/meta` `/status` `/close`) + `/queue`; trio-as-continuity (registry +
spec + ledger); artifact-driven bootstrap; and the deterministic hooks that **are** enforced (the
`cd`&&`git` rewriter, the `[META:<id>]` prefix + marker-register hooks, the skills-mirror auto-sync).

### 17.2 The three root causes

Everything that is *not* working reduces to three root causes:

1. **Concurrency by CONVENTION, not construction.** One shared main dev checkout — chips, siblings,
   and the reconciler all default their Bash cwd there, so a mutating git op collides / eats another
   session's uncommitted WIP (**≥6 incidents**). Compounded by **unlocked shared CI ratchets**
   (file-size / no-growth baselines have no serialization → **~32 repair PRs**, 3–4-PR chains like
   `#3107 → #3109 → #3113 → #3114` — THE dominant churn source). The guarantee is held by prose that
   drifts, not by construction. *This bite (B3) closes the checkout half; B1/P3 closes the ratchet half.*
2. **Auto-drive trusts self-reported STRINGS as completed facts.** A `two_pass` verdict written at
   dispatch → merged-unreviewed (#3347); `--draft` with no barrier → empty-scaffold merges (#2846,
   #3122); a required-checks subset / whitespace parse → false-green. Review then leaks downstream as
   "corrects #N" second PRs instead of in-chip. *A reviewed-looking string is not a review.*
3. **Shipped ≠ live (silent no-ops).** An 8-day-stale global skills mirror made the I4 prefix hook a
   **silent no-op Jun 14 → 22**; harness artifacts merge but don't propagate; failures are **silent**
   (everything *looked* installed and did nothing).

Two **port-relevant** findings ride alongside: reconcile **under-updates in-flight buckets** (~40% of
non-terminal chips stale; truncated PR search re-dispatched done work; 7 casing variants of "merged";
undated `decisions_pending`); and **Evolve-coupling beyond the P1 slug** — the `*evolve*` memory-slug
glob (`tools/meta-queue`, `tools/meta-inflight`) is a **latent bug** on a non-Evolve project (resolves
to no dir, silent misfire), the aspect registry is embedded *in* `META-session-guide.md` (must
externalize to `registry_path`), and the `live` bucket + pod/promote/canary model is baked into the
ledger state machine (CalGraph is ship-on-merge → chips strand). Surface bloat is real but lower
priority (10 skills with launch⊕design / coherence⊕reconcile overlap; 4 sweeps where reconcile +
shipped-digest suffice; merge-rule restated 5×). **NOTE:** `meta-issue` is **not** dead — it is being
wired by in-flight Initiative 1 (#3355 `/design-ingest`, #3364 `/triage`); do not cut it.

### 17.3 The A/B/C improvement backlog

- **Tier A — port-blocking generalizations** (critical path for CalGraph *correctness*): parameterize
  the memory-slug glob; externalize the aspect registry to `registry_path`; per-project `deploy_model`
  (drop the `live` bucket when there is no pod); make `preflight_cmd` / `flaky_jobs_doc` optional with
  graceful degrade. Sequences after P2, before P3.
- **Tier B — harden the top leaks into determinism** (highest value; benefits *both* repos):
  serialize / eliminate the shared ratchets (make them **diff-relative** — no shared mutable baseline
  number; CalGraph defaults that way from day 1); gate auto-drive on **facts, not strings** (block
  `gh pr create` on an empty diff; reject a verdict written in the same pulse that opened the PR);
  **pin each chip's cwd to its own worktree** (it cannot `cd` to the shared dev checkout). ← *B3, this bite.*
- **Tier C — leanness / hygiene** (minimal CalGraph + less Evolve maintenance): collapse the 4 sweeps
  to reconcile + digest; merge `/launch` + `/design`; single-source the merge-rule + DoD; normalize the
  bucket enum + date `decisions_pending`; fix reconcile in-flight staleness (untruncated recency +
  branch-existence check pre-relaunch); inline the registry row-fetch so `/meta` stops loading the 43 KB
  guide for one row.

### 17.4 Operator decision + bite sequencing

The operator **green-lit tiers A + B** (C deferred, taken opportunistically). Two constraints on the
ratchet fix (B1): it must be **diff-relative** (a PR fails only if it grows a capped file above
baseline — no shared mutable number to thrash) and **CalGraph-first** (CalGraph gets the diff-relative
default from day 1; Evolve retrofits). Sequencing:

- **B3 (this bite)** — the cross-checkout mutation guard hook + this umbrella §17. Independent of the
  serialized chain; ships first because it is a self-contained, fail-safe, reversible guard.
- **A-config → A-registry → B2** — serialized behind P2 (`#meta-substrate-sync`) because they touch the
  shared config / registry / ledger surface and would collide if run in parallel.
- **B1** — folded into **P3** (the diff-relative ratchet work).
- **Tier C** — deferred; picked up opportunistically once A/B land.

### 17.5 B3 — the cross-checkout mutation guard (this bite)

B3 addresses root cause #1's checkout half by **construction**: a PreToolUse(Bash) hook,
[`tools/hooks/guard-cross-checkout-mutation.sh`](../tools/hooks/guard-cross-checkout-mutation.sh),
that **blocks** (`permissionDecision: "deny"`) a high-confidence **mutating** git/shell op targeting a
git checkout **other than the session's own worktree**. It is the deterministic *complement* to the
`cd`&&`git` rewriter (H2 / Initiative-none): that hook clears read-only cross-dir git; this one stops
the destructive inverse (a chip reaching out to `git reset --hard` / `checkout` / `commit` / `stash
pop` / `clean` the shared dev checkout, wiping a concurrent session's WIP).

- **"Own worktree" is generic, not a hardcoded Evolve path** — derived from the PreToolUse payload's
  `cwd` via `git rev-parse --show-toplevel`, so it is portable to CalGraph unchanged.
- **Fail-safe contract (mirrors the H2 hook's doctrine):** the worst case is today's behavior (no
  block), **never** a false block of a chip in its own tree. It blocks only when it positively
  identifies **both** a mutating verb **and** a target toplevel that differs from own (both resolved
  through the same `git rev-parse --show-toplevel`, so the compare is canonical/symlink-safe). On any
  uncertainty — unresolvable toplevel, no explicit foreign path, ambiguous/read-only verb, an absolute
  shell operand, a `$()`/glob/quote path token, or a missing dep — it emits nothing and passes through.
- **Known limitation (by design):** a *bare* mutation that runs foreign only because a prior separate
  Bash call left the persisted cwd elsewhere is not caught (the payload cwd then reads as that checkout,
  so nothing is foreign); the hook binds the **explicit in-command** reach-out (`cd <foreign> && …`,
  `git -C <foreign> …`), the documented incident shape.

Regression-guarded by [`tools/hooks/test_guard_cross_checkout_mutation.sh`](../tools/hooks/test_guard_cross_checkout_mutation.sh);
install/disable steps live in the setup doc's "Enforcement layer: the cross-checkout mutation guard"
subsection. Reversible: the hook is a single non-privileged file registered as a user-level PreToolUse
matcher; removing the matcher (or the file) reverts to today's prose-discipline behavior.

### 17.6 A-config — remaining config-surface gaps (shipped)

**A-config: shipped in #3370.** Closes the Tier-A config gaps on top of P1 (`.claude/meta.json` +
`tools/meta_config.py`, #3366) and A-registry (`registry_path`, #3369): (1) **`memory_slug`** — the
latent-bug fix; `tools/meta-queue`'s `resolve_ledger_dir` (reused by `tools/meta-inflight`) resolves
the ledger-dir glob token through `meta_config.memory_slug()` (explicit → derive `*<checkout-basename>*`
→ `*evolve*`) instead of the hardcoded `*evolve*` that silently matched nothing on a non-Evolve
checkout; (2) **`deploy_model`** (`pod-canary` | `ship-on-merge`) — decouples the pod-only `live`
bucket; the ledger-schema bucket semantics are now conditional on it (`merged` is terminal under
ship-on-merge, `live` under pod-canary), while `is_terminal_bucket` stays model-agnostic so no chip
strands; (3) **optional `preflight_cmd` / `flaky_jobs_doc`** — the chip Definition-of-Done degrades
gracefully when either is empty (documented in `.claude/skills/launch/SKILL.md` + this behavior in
`docs/meta-reconcile-procedure.md`). Every field has a safe fallback; Evolve's `.claude/meta.json`
restates all values explicitly, so Evolve is byte-for-byte unchanged.

### 17.7 B2 — fact-gate merges (shipped)

**B2 shipped in #3375.** Closes root cause #2's two bluntest shapes by converting self-reported
strings to verified **facts**: (1) **empty-diff PR-create guard** —
[`tools/hooks/guard-empty-pr.sh`](../tools/hooks/guard-empty-pr.sh), a PreToolUse(Bash) hook that
**blocks** (`permissionDecision: "deny"`) a `gh pr create` whose head branch has an empty three-dot
diff (`base...HEAD`) vs its base, killing the empty-scaffold-merge root cause (#2846, #3122);
fail-safe — blocks only on a positively-empty diff against a resolvable base, passes through on any
uncertainty (regression-guarded by `tools/hooks/test_guard_empty_pr.sh` over a real git clone).
(2) **Verdict-from-artifact** — the auto-merge gate now requires an independent two-pass review
section *in the PR body* with a pass-equivalent overall verdict, not just a ledger `two_pass` string
a chip can write in the same pulse that opened the PR (the #3347 root cause); checked by
[`tools/meta-verdict-check`](../tools/meta-verdict-check), which reuses `verdict_is_pass` from
`tools/meta-queue` (no fork) and only adds the PR-body-artifact requirement. Wired into the canonical
rule (`docs/meta-ledger-schema.md` "The PR-body artifact requirement"), the reconcile procedure, the
`/status` skill, and the setup doc; pinned by `packages/admin/tests/test_meta_verdict_check.py`.
CHECKS stay `--json` (unchanged) — only the verdict *section* is parsed, in one tested place.

### 17.8 B4 — anti-redundancy dispatch-claim layer (shipped)

B4 closes root cause #3's remaining shape — **redundant work** — which Initiative-6 `meta-inflight`
only partly addressed (operator-corroborated 2026-07-01). Three gaps remained: (1) the mid-flight
window is invisible — `meta-inflight` scans ledgers + open PRs, but a *just-dispatched* chip has
neither (no PR yet; nothing in a ledger until a coordinator writes it), so a 2nd dispatch can't see
it; (2) the "run meta-inflight before you spawn" instruction is advisory prose the model forgets, and
it dies one level below the coordinator; (3) reconcile re-dispatches a "stalled" chip that already
pushed when the ledger is stale / the PR search was truncated (the #3237 class).

Operator decision: **record-always + WARN, never block** (fuzzy title/scope overlap must not block
legit parallel work). The fix turns the guard from *convention* into *construction*:

1. **Claim hook** — [`tools/hooks/record-dispatch-claim.sh`](../tools/hooks/record-dispatch-claim.sh),
   a PreToolUse hook on `Agent` + `mcp__ccd_session__spawn_task` that, on every spawn from a session
   with an active-aspect marker, ALWAYS records
   `~/.claude/meta-state/claims/<id>.json = {aspect, title, time, ttl_seconds, …}`. It reuses the
   EXACT cwd→key + aspect-validation from `prepend-meta-prefix.sh`, is **side-effect-only** (emits
   nothing; never alters/blocks a spawn), and **self-propagates down the spawn tree** (the prefix hook
   + `meta-marker-register-on-start.sh` give each chip a marker, so grandchild dispatches record
   claims too — closing gaps 1 and 2). Fail-safe: any error → no claim, exit 0 (worst case = today's
   invisibility). Guarded by `tools/hooks/test_record_dispatch_claim.sh`.
2. **Fourth signal** — `tools/meta-inflight` reads `meta-state/claims/` alongside ledger chips / PRs /
   sessions; an active claim that keyword/aspect-overlaps the about-to-dispatch work surfaces as an
   overlap. THIS is where the "warn" lands: a no-PR-yet sibling is now visible pre-dispatch.
3. **TTL / self-expiry** — claims carry `ttl_seconds` (default 4h; warn-only ⇒ err generous);
   `meta-inflight` ignores AND prunes expired claims on read (`--prune` standalone), so a dead chip's
   claim self-expires and never warns forever.
4. **Reconcile-hardening (gap 3)** — before relaunching a "stalled" chip,
   `docs/meta-reconcile-procedure.md` + `.claude/skills/status/SKILL.md` now check **branch existence /
   PR state** (`git ls-remote`, `gh pr view --json`, read-only + JSON) — never re-dispatch a chip that
   already pushed a branch or opened a PR — and use an **untruncated** recency lookup.

Registry + schema: `docs/meta-ledger-schema.md` ("The dispatch-claim registry"); install + fail-safe:
`docs/meta-system-setup.md` ("the dispatch-claim layer"). Deferred (out of scope): blocking/ask on
overlap (operator chose warn-only), a structured `scope` field on `spawn_task` (the tool can't carry
one — keyword-match is the design), and any CalGraph-repo change (it inherits via idempotent re-sync).
Reversible + non-privileged (a record-only hook + read-side change).

**B4 shipped in #3383.**
