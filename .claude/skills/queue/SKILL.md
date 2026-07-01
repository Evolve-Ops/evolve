---
name: queue
description: Show the cross-aspect META decision queue — everything across ALL aspects that needs the operator (held merges, pending forks, operator gates), each with a recommendation — and act on items by number. Run from ANY session; it is global, not tied to one aspect. This is the operator's inbox / on-demand dashboard.
---

The operator wants to see and drain the **decision queue**: the single cross-aspect inbox of
everything that needs them. It is NOT tied to a META session — run it anywhere, in a scratch
session. The queue is a **computed projection** of the durable ledgers
(`meta-state/*.json`, schema: `docs/meta-ledger-schema.md`), so it is always current as of the
last reconcile — there is no separate queue datastore to drift. (Run `/reconcile` first if you
want it refreshed against live `gh` before viewing.)

## Get the queue (delegate the heavy read to a throwaway subagent)

The projection is a deterministic read of all ~16 ledgers (~200KB). Building it **in this
context** burns ~190k tokens that then persist and get re-read every subsequent turn. So do **not**
read the ledgers here. Delegate the read to ONE subagent whose whole job is to run the projection
script and hand back only its (tiny) output:

> **Spawn one Agent** (the `Agent` tool; a cheap model is fine — this is a mechanical projection,
> no judgment). Brief it: *"Run `python3 tools/meta-queue` from the repo root and return ONLY its
> stdout verbatim — the rendered queue text, nothing else (no preamble, no analysis, no summary).
> If it exits non-zero, return its stderr instead."*

`tools/meta-queue` is the **single source of truth for the projection rules** — it implements the
"decision queue (computed projection)" section of `docs/meta-ledger-schema.md` exactly: glob the
ledgers, skip any item whose `snoozed_until` is still in the future, then collect

- **(A) Rescue** — held chips that need rescue: `bucket==stalled` (dead), `verdict_is_blocking(two_pass)`
  (reviewer flagged — FAIL / DO-NOT-MERGE / a live concern), or `reversible:false` on an open PR
  (irreversible — auditor look);
- **(B) Decisions** — every `decisions_pending[]` fork (with its recommendation);
- **(C) Gates** — operator gates: object gates whose `blocked_on` is `operator`, plus freeform
  string gates (skipping ones clearly gated on a date "≥ 06-18" or another aspect "blocked on X");
- **(D) Verify-then-merge** — green PRs awaiting the operator's merge: `operator_merge:true` while
  `open_green`+`verdict_is_pass(two_pass)` (verified — just needs the click, flagged *clearable*
  when the flag isn't warranted: reversible+non-privileged), or `open_green` with NOT
  `verdict_is_pass` (green-but-unverified — the reconciler auto-reviews the reversible+non-privileged
  ones; what lands here is privileged/at-cap, verify first).

`verdict_is_pass` / `verdict_is_blocking` are the **canonical verdict predicate** (`docs/meta-ledger-schema.md`
→ "The canonical verdict predicate"), NOT an exact `=="PASS"` match — a prose verdict like
`"SHIP (2 non-blocking concerns)…"` reads as a pass and auto-merges. It dedups a gate against a
`decisions_pending` entry sharing its `id` (shows the decision), leads with a freshness stamp
(`~/.claude/meta-reconcile/last-seen.json` `last_run`, fallback newest ledger `updated`), numbers
every item `#N  [aspect]  <what> — <recommendation>`, and prints `✅ Queue clear …` when nothing
needs the operator. The auto-merge-clean case (`open_green` + `verdict_is_pass` + reversible + not
`operator_merge`) is intentionally **not** surfaced — the reconciler merges it; it never needs the
operator.

**Render the returned text verbatim** to the operator. You did NOT read the ledgers — the subagent
did, and threw its context away. (Collisions / orphan PRs need live `gh` — those surface from a
full `/reconcile`, not this cheap ledger-only view. Mention it if relevant.)

## Act (the operator drives by number; write back to the SOURCE ledger)

Acting on a single item is cheap and stays **here** in the main thread — read only the ONE owning
ledger (never re-read all of them), apply the change, write it back atomically (temp+rename), then
re-render what remains (spawn the subagent again if you want the full fresh projection):
- **approve / decide `#N <choice>`** — record the choice; remove that `decisions_pending` entry;
  if the resolution is durable, note it in the aspect's memory topic file.
- **merge `#N`** — only a verify-then-merge item the operator OKs: confirm green
  (`gh pr checks --json name,bucket,state`, never parse text), merge manually (never `--auto`),
  set the chip `bucket: merged`.
- **snooze `#N <when>`** — set `snoozed_until` (a date) on the item; it drops off until then.
- **open `#N`** — launch `/meta <aspect>` to handle it in a full design bout.
- **dismiss `#N`** — drop it; record why.

## Brains-liveness footer (one line)

Confirm the autonomy is actually running. Call `list_scheduled_tasks` and look at `meta-reconcile`
(~2h) and `meta-coherence` (daily). Add one line: `Brains: reconcile ran <ago>, next <in>;
coherence ran <ago>`. **Flag ⚠ + the fix** if any brain is:
- `enabled: false` → "⚠ reconcile disabled — re-enable in the Scheduled sidebar";
- **overdue** — no `lastRunAt` within ~2× its cadence while the app's been open → "⚠ reconcile overdue
  — Run now / `/reconcile`";
- **fired-but-traceless** — a recent `lastRunAt` but its log (`~/.claude/meta-reconcile/log/<date>.jsonl`)
  is empty → the run didn't complete (usually paused on an un-pre-approved tool) → "⚠ reconcile ran but
  did nothing — click Run now once to pre-approve `gh`/`spawn_task`".
A clean line (both ran on schedule, logs present) is the all-good signal.

## Sessions footer (one line)

After the queue, call `list_sessions` and count what `/prune` could clear — sessions with a
merged/closed PR, stale scheduled-task runs, idle `META <id>` coordinators. If any, add one line:
`N sessions look done/idle — /prune to clear, /launch to re-open ones with work`. Don't enumerate
here; `/prune` and `/launch` own the detail.

Keep it tight: delegate the read → render → act on what they say → leave the ledgers current. This
is the surface that replaces watching sessions. Complements: `/reconcile` (fresh sweep + queue),
`/prune` (close finished sessions), `/launch` (re-open ones with work), `/meta <id>` (design bout).
