---
name: reconcile
description: Run the META reconcile sweep on demand (don't wait for the scheduled ~2h run). Reconciles every aspect's ledger against gh, auto-drives the safe zone (merge green+PASS+reversible, relaunch stalled), then reports what it did and shows the decision queue. Optional arg = a single aspect id to reconcile just that one.
---

The operator wants to run the reconcile pulse **now** instead of waiting for the scheduled
`meta-reconcile` task. Execute the SAME procedure, in the foreground, and ALWAYS report (the
scheduled run is silent; this one talks).

1. **Scope.** If an aspect id was given (`/reconcile ui`), reconcile only that aspect's ledger.
   No arg → all aspects (`meta-state/*.json`, skip `_README.md`).

2. **Delegate the sweep + projection to ONE subagent — keep the heavy read out of this context.**
   The sweep reads all ~16 ledgers (~200KB) and makes many `gh` round-trips; running it here burns
   ~190k tokens that then persist all session. So spawn ONE `Agent` (the `Agent` tool; a cheaper
   model is fine — the policy is mechanical, no product judgment) and have it run the whole sweep
   and the queue projection, returning only the compact result. Brief it:

   > *Run the canonical reconcile procedure (version-controlled at `docs/meta-reconcile-procedure.md`,
   > mirrored to `~/.claude/scheduled-tasks/meta-reconcile/SKILL.md`) over <scope>: load the
   > ledgers → reconcile each chip against live `gh` (update `bucket`/`pr`/`last_commit`, write
   > ledgers back atomically) → act in the safe zone per the mechanical rule (auto-merge ⟺
   > `open_green` AND `verdict_is_pass(two_pass)` AND `reversible==true` AND `operator_merge!=true`
   > — `verdict_is_pass` is the canonical predicate in `docs/meta-ledger-schema.md`, NOT an exact
   > `=="PASS"` match; auto-relaunch stalled ≤2; auto-REVIEW a green-but-unverified reversible
   > non-privileged chip ≤2 (dispatch a `(review)` chip rather than parking it); auto-fix-forward a
   > PR red on a MECHANICAL gate or in merge conflict ≤2, escalating SUBSTANTIVE test failures and
   > fleet-blocking ratchet reds on main) → respect every hard rule there (skip `autonomy:"observe"`
   > aspects; never merge a non-pass / blocking / not-reversible / `operator_merge` chip; never
   > INVENT a verdict; never dispatch a NEW product bite or advance a roadmap). Follow that doc's TOOL
   > DISCIPLINE (single `gh`/`git` commands; never `cd <dir> && git`). Then run `python3
   > tools/meta-queue` for the decision queue. **Return ONLY: (a) a one-block auto-did summary — merged (PR #s),
   > relaunched/reviewed/fixed (chips), buckets updated, collisions/orphans/fleet-blocks — and (b) the verbatim stdout of
   > `tools/meta-queue` (the rendered queue). No raw ledger contents, no per-chip narration.*

   `tools/meta-queue` is the single source of truth for the queue projection (it implements
   `docs/meta-ledger-schema.md`'s decision-queue rules — snooze-skip, held-merge classification,
   gate↔decision dedup, operator-gate filtering, grouping A–D, freshness stamp).

3. **Report — do not be silent.** Print what the subagent returned: the auto-did summary, then the
   rendered decision queue (everything across aspects that still needs the operator, each with its
   recommendation). End with the single most useful next action. Acting on a queue item by number
   stays **here** (read only the one owning ledger) — see `/queue`'s Act section.

Keep it tight: delegate the sweep, then a short "did X · queue has Y" report. This is the
manual trigger for the exact sweep the scheduler runs every ~2h.

**`/reconcile` IS reconcile-then-queue.** The slow part is the sweep (`gh` round-trips); the queue
render is instant, so this command always ends by showing the fresh queue — run it when you want
the latest. If you'd rather not wait, run the sweep in the background instead: the **"Run now"**
button on the `meta-reconcile` scheduled task (or just the ~2h cadence) pokes you when done, then
`/queue` is instant **and** fresh.
