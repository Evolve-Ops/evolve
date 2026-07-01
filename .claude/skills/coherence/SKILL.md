---
name: coherence
description: Run the cross-aspect coherence pass on demand — reads all ledgers + the ownership map + open PRs, flags overlap / collisions / mis-routed items, posts them to the queue, and reports. The across-aspects complement to /reconcile (which works within each aspect).
---

The operator wants the cross-aspect coherence pass NOW instead of waiting for the scheduled
`meta-coherence` run. Execute the SAME procedure, in the foreground, and ALWAYS report (the
scheduled run is silent).

**Delegate the heavy read to ONE subagent.** All four detectors need the FULL contents of all ~16
ledgers (titles, notes, backlog) + open-PR file lists — reading that here burns ~190k tokens that
then persist all session. So spawn ONE `Agent` (the `Agent` tool; a cheaper model is fine — this is
a bounded reading/reasoning pass) to do the detection and the write-back, and return only the
compact findings + the resulting queue. Brief it:

> *Run the canonical coherence procedure (version-controlled at `docs/meta-coherence-procedure.md`,
> mirrored to `~/.claude/scheduled-tasks/meta-coherence/SKILL.md`): load all ledgers
> (`meta-state/*.json`) + the ownership-map table (guide → "Surface ownership (the routing map)") +
> open `claude/*` PRs. Run the four detectors — **duplicate/overlap (cross-aspect)**, **cross-aspect
> PR collision**, **mis-routed item**, and **within-aspect duplicate** (two non-terminal chips in the
> SAME aspect doing the same work — `dupin:<aspect>:<A>+<B>`; skip pairs clearly sequenced/coordinated
> on purpose) — and post NEW findings to the right aspect's
> `decisions_pending` (`source:"coherence"`, signature as `id`, with a recommendation), dedup by
> signature, clear any resolved ones. Respect the hard rules: cross-aspect overlap + within-aspect
> DUPLICATION only (NOT within-aspect status — that stays the reconciler's), READ + RECOMMEND
> only (never route/merge/reassign/edit non-coherence entries), idempotent + atomic. Follow that
> doc's TOOL DISCIPLINE: Read each ledger one file per call (never `cd` into meta-state or
> `cat`/glob/loop over them — trips "simple_expansion"); single `gh pr view <n> --json files` per
> PR (cap ~12); `git -C <dir>` not `cd <dir> && git` (non-bypassable hooks guardrail). (That doc's
> `python3`/compound-bash ban is a HEADLESS-scheduled stall-avoidance rule — it does NOT bind you,
> an interactive subagent that can prompt for permission; the `cd && git` and per-file-Read rules
> DO still apply.) Then run `python3 tools/meta-queue` so the newly-posted findings appear in the
> rendered queue. **Return ONLY: (a) the findings grouped (cross-aspect overlap / collisions /
> mis-routed / within-aspect duplicates), each with its recommendation, and (b) the verbatim stdout
> of `tools/meta-queue`. No raw ledger dumps.*

`tools/meta-queue` is the single source of truth for the queue projection (it implements
`docs/meta-ledger-schema.md`'s decision-queue rules), so the coherence findings — written as
`decisions_pending` entries — surface in group (B) of its output.

**Report** what the subagent returned: the findings grouped, each with its recommendation, then the
rendered queue; point to `/queue` to act on them. If clean: `✅ No coherence issues (cross-aspect or
within-aspect duplicate) — <N> aspects coherent.`

Keep it tight. Complements: `/reconcile` (within-aspect sweep), `/queue` (act on the queue),
`/launch` + `/prune` (session lifecycle).
