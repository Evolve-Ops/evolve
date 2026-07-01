---
name: launch
description: Advance META aspects that have work waiting — without keeping a stable of live coordinators. Dispatches already-designed backlog bites as chips directly (confirm-first), and preps the interactive-open list for aspects that still need design. Run from any session.
---

Two kinds of "launch": **dispatch an already-designed bite as a chip** (no coordinator needed),
or **open an interactive coordinator to DESIGN** (operator in the chair). This skill does the
first directly and preps the second.

1. **Delegate the read + classification to ONE subagent — keep the heavy read out of this
   context.** Reading the decision queue plus every ledger's `next_action` + `backlog` here would
   burn ~190k tokens that then persist all session. So spawn ONE `Agent` (the `Agent` tool; a
   cheaper model is fine — this is mechanical) and brief it:

   > *Run `python3 tools/meta-queue --json` for the decision queue, and Read each `meta-state/*.json`
   > ledger's `next_action` + `backlog` (skip `_README.md`). Classify each actionable item:
   > **READY BITE** — a `backlog` item / `next_action` that is well-specified with NO open design
   > fork (a brief could be written today; needs no coordinator → dispatch as a chip); **NEEDS
   > DESIGN** — a `decisions_pending` fork or a next step that requires a design decision (needs an
   > interactive coordinator). **Return ONLY two grouped lists**: READY BITES (each with `[aspect]`,
   > a one-line brief, and the `output`/PR target) and NEEDS-DESIGN (each with `[aspect]` and the
   > `/meta <id>` to open). No raw ledger contents.*

   `tools/meta-queue` is the single source of truth for the queue projection (it implements
   `docs/meta-ledger-schema.md`'s decision-queue rules).

2. **Present both groups** the subagent returned — READY BITES with their one-line briefs;
   NEEDS-DESIGN with the `/meta <id>` to open.

3. **Dispatch ready bites — confirm-first (this IS the operator's Gate-1 greenlight; never
   auto-dispatch new work). This stays HERE in the main thread.** For each the operator approves:
   - **Pre-dispatch collision check (ADVISORY + confirm-first).** First decide the bite's `scope`
     — the file globs it will touch — then run
     `python3 tools/meta-inflight --aspect <id> --keywords "<kw>" --scope "<glob1,glob2>"` (its
     whole-ledger read happens in-subprocess + one `gh pr list --json` call; only the compact
     overlap report returns to this thread — like `tools/meta-queue`, so it does not bloat context).
     If it reports an overlap, **STOP and present it** (the in-flight chip/PR/session already on this
     aspect / keywords / files): let the operator **merge** the efforts (fold this into the existing
     chip/PR and drop the bite), **proceed** anyway (note why — e.g. intentionally parallel, different
     slice), or **cancel**. A clean "✅ No in-flight overlap found." → dispatch. This closes the
     pre-spawn gap that let sessions launch at cross purposes / redundantly (see
     `docs/spec-substrate-2026-06-15.md` §11).
   - Then `spawn_task` a chip:
   - Title `[META:<id>] <bite>` (add `[Opus]` only if it needs hard design/security judgment).
   - Brief: what it does / touches / does NOT do / how we'll know it worked; "resume-safe:
     immediate empty-commit push + incremental pushes"; "use `git -C <worktree>` for git in any other dir, never `cd <dir> && git` (non-bypassable hooks guardrail)"; **"deposit your outcome at the PR (or
     `<output path>`) — do not report findings in the thread"**; "run the in-chip two-pass review
     before marking done"; **the standing Definition-of-done block below**; name the parent ("Spawned by META:`<id>`")
     **and add the standing-propagation line ("You belong to META:`<id>`: prefix every chip / PR /
     branch YOU spawn with `[META:<id>]` too") so the prefix survives to any grandchildren**; **and
     a standing dispatch-check line ("before you `spawn_task` any sub-chip, first run
     `python3 tools/meta-inflight --aspect <id> --scope <its globs>` and surface any overlap
     confirm-first — never spawn redundant / cross-purpose work")** so the pre-dispatch check
     propagates to grandchildren the same way the prefix does.
   - Record the chip in the aspect's ledger `chips[]` (`bucket: dispatched`, `task_id`, the
     `pr`/`output` pointer, and the **`scope` globs you just declared** — so the next dispatch's
     `tools/meta-inflight` check has file-level precision). The chip owns the PR — the coordinator
     stays PR-less.

   **Standing Definition-of-done block (inject this verbatim into every chip brief).** This is
   the canonical wording. The behavior doc-of-record (`docs/META-bootstrap.md` item 3) describes
   the same contract and points back here; the reconciler briefs
   (`docs/meta-reconcile-procedure.md`) inline a compressed form only because they are injected
   into headless-spawned chips that can't read this skill at dispatch time. Keep all three in step.

   **Two bullets are project-conditional** (resolve via `tools/meta-config` — see
   `docs/spec-substrate-2026-06-15.md` §17): `<preflight_cmd>` = `tools/meta-config preflight_cmd`
   (Evolve: `tools/preflight`) and `<flaky_jobs_doc>` = `tools/meta-config flaky_jobs_doc` (Evolve:
   `docs/ci-flaky-jobs.md`). When `preflight_cmd` is **empty** (a project with no preflight yet), the
   pre-push bullet degrades to *"push, then poll `gh pr checks` until green"* — there is nothing to
   run locally. When `flaky_jobs_doc` is **empty**, **drop the FLAKY bullet** (that project declares no
   known-flaky jobs). For Evolve both are set, so the block below is the verbatim, un-degraded form:

   > **Done = CI green, not pushed.** Pushing is the middle of the work, not the end.
   > - Before EVERY push: rebase on `origin/main`, then run `<preflight_cmd>` (`tools/preflight`,
   >   the local CI-mirror). **Never push red** — CI should confirm your pre-flight, not be where a
   >   deterministic failure first appears. *(No `preflight_cmd` set → skip this; push, then rely on
   >   the poll below.)*
   > - After pushing you are NOT done: poll `gh pr checks <pr>` until the checks **settle**
   >   (don't yield while any check is pending).
   > - A check is RED → fetch that job's failing logs (`gh run view --log-failed`,
   >   `gh pr checks <pr>`), reproduce locally where you can, fix-forward, re-push, re-poll —
   >   up to **3 rounds, with the logs in context**.
   > - FLAKY: if the ONLY red is a job listed in `<flaky_jobs_doc>` (`docs/ci-flaky-jobs.md`), re-run
   >   it (`gh run rerun <run-id> --failed`) — **never edit code to chase a flake**. *(No
   >   `flaky_jobs_doc` set → omit this bullet.)*
   > - Still red after 3 rounds, or blocked on something you can't resolve (e.g. a real
   >   `linux-e2e` failure you can't reproduce on macOS): **STOP and file a blocker** — leave
   >   the PR open, post a comment naming the failing job + what you tried, and report the
   >   blocker. Do not silently declare done.
   > - Declare done **only** when CI is green (or a clearly-flaky-only red with the rerun
   >   queued + noted).
4. **Needs-design:** hand over the `/meta <id>` list. Opening the interactive session is the
   operator's action; `/queue` → `open #N` turns THIS session into one coordinator.

Honest constraint: a session can spawn headless **chips** (step 3) but cannot open an interactive
coordinator you design in — that's yours. Keep it tight: delegate the read → classify → dispatch the
approved ready bites → hand over the design list. Teardown is `/prune`; the inbox is `/queue`.
