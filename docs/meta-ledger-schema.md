# The structured in-flight ledger (`meta-state/<aspect>.json`)

The **in-flight ledger** is the live, machine-readable work-state for one META aspect.
It is the structured half of the durable trio: the **spec** holds design, **memory**
holds durable decisions + lessons, and the **ledger** holds *what is in motion right now*
(chips dispatched, PRs open, gates blocking, decisions awaiting the operator).

Before this file existed, in-flight state was freeform prose buried in each aspect's
memory entry and the `MEMORY.md` index. That had three costs: it bloated the index past
its size limit, `/status` and `/close` had to *parse narrative* to find chip state and
two-pass verdicts, and nothing downstream (a reconciler, a cross-aspect dashboard, the
fleet watcher) could consume it. Structuring it fixes all three.

## Where it lives

Operator-local, one file per aspect, beside the aspect memory:

```
/Users/<op>/.claude/projects/-Users-<op>-GitHub-evolve/memory/meta-state/<aspect>.json
```

It is **working state, not durable truth** — fully reconstructable from `gh pr list` +
the spec + memory on a fresh machine, exactly like the fleet watcher's `last-seen.json`.
The memory index keeps only a terse one-line pointer per aspect; live state lives here.

## Division of labor (do not duplicate)

| Lives in… | …holds | Churns |
|---|---|---|
| **registry row** (`META-aspect-registry.md`) | mission, spec/memory pointers, deploy mechanism, invariants/boundary | rarely |
| **spec doc(s)** | the design, decisions, non-goals | per design change |
| **memory** topic file | durable decisions, lessons, shipped-history narrative | per bout |
| **memory index** line | one terse durable pointer (mission + trio + "live → ledger") | rarely |
| **ledger** (this file) | chips in motion, PR/check state, two-pass verdicts, gates, pending decisions, next action, backlog | every `/status` and `/close` |

If a fact is durable (an invariant, a lesson, a shipped PR's history) it does **not**
belong in the ledger — write it to the registry/spec/memory. The ledger answers only
"what is in flight and what's the next move."

## Schema

```jsonc
{
  "aspect": "ui",                       // the META id (matches the registry slug)
  "mission": "one-line mission",        // mirror of the registry row's mission (orientation only)
  "spec": ["docs/spec-ui-meta-2026-06-13.md"],   // spec doc(s) — pointer
  "memory": ["project_ui_meta_2026_06_13"],      // memory topic slug(s) — pointer
  "updated": "2026-06-14",              // date this ledger was last written (writer stamps it)
  "autonomy": "auto",                   // OPTIONAL (default "auto"): "auto" = the scheduled reconciler may act in the safe zone (merge/relaunch); "observe" = reconcile + poke only. Per-aspect kill-switch.
  "bout": "where the aspect is in its arc — one line",
  "next_action": "the single most useful next step (+ gate if any)",

  "chips": [                            // everything IN MOTION: backlog→done
    {
      "id": "T2",                       // short stable handle used in prose ("T2", "B2", "D-1")
      "title": "non-blocking github/status",
      "task_id": "task_a26665fd",       // spawn_task id, or null if not yet dispatched
      "pr": 2890,                       // PR number, or null
      "branch": "claude/...",           // branch, or null
      "output": null,                   // non-PR chips: where the outcome lands (file path / "memory:<slug>" / doc). Build chips: null (the PR is the output). See "Chips deposit outcomes" below.
      "scope": ["packages/admin/web/usage.js", "tools/meta-*"],  // OPTIONAL: file globs this chip INTENDS to touch, declared by the coordinator AT DISPATCH (the child still writes nothing to the ledger). `tools/meta-inflight` matches on it for file-level dispatch-time collision detection (META:substrate Initiative 6). See "Dispatch-time collision check" below.
      "issue": 2656,                    // OPTIONAL: source GitHub issue # when this chip was born from an issue ([META:substrate] Initiative 1 intake provenance). The PR body carries `Closes #N` so merge auto-closes the issue; the reconciler's merge-detection then closes the loop. (A `backlog[]` string may likewise embed `#N`.)
      "bucket": "dispatched",           // see buckets below
      "two_pass": "PASS",               // the reviewer/chip verdict. Canonical enum PASS | CONCERNS | FAIL | pending | null, BUT free prose is expected and supported ("SHIP (2 non-blocking concerns) — …", "SHIP / CONCERNS-no-blockers") — it is read through `verdict_is_pass()` (see "The canonical verdict predicate" below), NEVER an exact `== "PASS"` match.
      "privileged": false,              // touches sudoers/auth/appliers/deploy/security?
      "reversible": true,               // cheaply git-revert+redeploy-able?
      "operator_merge": false,          // OPTIONAL (default false): true = a human must click merge even when green+PASS+reversible; the auto-lane holds it and the queue surfaces it. The structured form of the old "DO NOT SELF-MERGE" note. VALID ONLY when `privileged==true` OR `reversible==false` (see "When operator_merge is warranted" below) — on a reversible, non-privileged chip it needlessly strands a green+reviewed PR, so the reconciler flags it as clearable. Orthogonal to `privileged` (review-depth) and `reversible` (irreversibility).
      "dispatched": "2026-06-13",       // date the chip was spawned, or null
      "last_commit": "259a4486a",       // last pushed commit — SHA or ISO time; relaunch checkpoint + stall input, or null
      "note": "parallelize+bounded-deadline, server-only; no auto-merge"
    }
  ],

  "gates": [                            // blockers needing the operator or a dependency.
    "promote+kickstart, eyeball card on live pod (operator Gate-2)",            // EITHER a terse string (like backlog)…
    { "id": "GATE-2", "desc": "promote+kickstart", "blocked_on": "operator" }   // …OR an object when you want a structured blocked_on
  ],

  "decisions_pending": [                // RED-ZONE forks for the operator (feeds the decision queue)
    {
      "id": "usage-next",
      "fork": "advance to E.B per-app telemetry or E.C value-loop first?",
      "recommendation": "E.B first — telemetry is the input the value-loop needs",
      "reversible": true,
      "raised": "2026-06-14",
      "snoozed_until": null,             // OPTIONAL date: while in the future, this item is hidden from the queue
      "source": "operator"              // OPTIONAL: "operator" | "coordinator" | "coherence" (who raised it; the coherence pass tags its findings)
    }
  ],

  "backlog": [                          // not-yet-dispatched ideas — terse strings, promote to a chip on dispatch
    "B-next scanner classifier (soak-first)"
  ]
}
```

### `bucket` — the chip state machine

| bucket | meaning |
|---|---|
| `backlog` | identified, not yet dispatched (prefer the `backlog[]` string list for these; use a chip entry once it has a task_id) |
| `dispatched` | spawn_task'd / running, no PR yet |
| `draft` | PR open as draft (scaffold-before-impl) |
| `open_red` | PR open, a check is failing |
| `open_green` | PR open, all checks green, awaiting the merge decision |
| `stalled` | dispatched/building but no new commit ~30 min ≈ dead chip → relaunch from `last_commit` |
| `merged` | merged to main, not yet promoted to the fleet |
| `live` | promoted + verified on the pod |
| `done` | terminal complete (== live, or shipped where there's no promote step) |
| `blocked` | waiting on a `gate` or another chip |

**`live` vs `merged` depends on the project's `deploy_model`** (`.claude/meta.json`, resolved
via `tools/meta-config deploy_model` — see `docs/spec-substrate-2026-06-15.md` §17):

- **`pod-canary`** (Evolve's model, the default): merge is the *middle* of the arc — a chip is
  merged, then **promoted + verified on the pod**, and `live` is its terminal, fully-shipped
  state. `done` == `live`.
- **`ship-on-merge`** (a project with no pod, e.g. CalGraph): there is no promote/verify step,
  so **`merged` IS the terminal bucket** and **no `live` is ever expected**. `done` == `merged`.

This is a *doctrine/legibility* distinction, not a queue-mechanics one: the merge **decision**
is past once a chip is `merged` under *either* model, so `is_terminal_bucket` (below) accepts
`merged`, `live`, AND `done` as terminal regardless of `deploy_model` — a `merged` ship-on-merge
chip and a `live` pod-canary chip both correctly drop out of the queue / the in-flight collision
scan. What `deploy_model` governs is only whether a coordinator should *expect* a `live` step to
follow a merge (pod-canary) or treat `merged` as the finish line (ship-on-merge). Evolve's `live`
bucket behavior is unchanged.

**Terminal buckets — `is_terminal_bucket`, the tolerant family.** A chip is **terminal**
(past the merge/close decision; never a held merge, never in flight) when its `bucket` is one
of `merged` / `live` / `done` — **or any drifted variant of them**. Hand-edited ledgers reliably
drift this vocabulary: trailing deploy/verify state (`merged-deployed`,
`merged-deployed-verified`), casing (`MERGED`), and other-terminal synonyms (`landed`, `closed`,
`closed_superseded`, `superseded`, `dismissed`, `investigated-resolved`). The **canonical
predicate** is `is_terminal_bucket(bucket)` in `tools/meta-queue` (case-insensitive; the exact
family `{merged, live, done, landed, closed, superseded, dismissed, investigated-resolved}` plus
a prefix match on `merged-` / `closed-` / `superseded-`). Every membership test for "is this chip
past the merge?" — the decision-queue terminal short-circuit, the `tools/meta-inflight`
collision check — **reads through this predicate, never a bare `bucket in {merged,live,done}`
set**: an exact-set test stranded every drifted-bucket merged chip in the queue as a phantom
two-pass FAIL (a live snapshot inflated 9 real items to 16). The drift itself is debt — writers
*should* canonicalize onto the enum above — but consumers stay tolerant so a stale verdict on
long-merged work never resurfaces.

### The canonical verdict predicate (`verdict_is_pass` / `verdict_is_blocking`)

The `two_pass` field is **free prose**, not a bare enum. Chips and reviewers write the
verdict as a human sentence — the live shapes seen on real PRs are
`"SHIP (2 non-blocking concerns) — independent adversarial reviewer…"` and
`"SHIP / CONCERNS-no-blockers…"`. The merge gate used to do an **exact** `two_pass == "PASS"`
string match; every such fully-reviewed PR failed that match and stranded forever — the
headless reconciler merged **0** of them for days while green, reviewed, reversible PRs piled
up. The fix is **one** canonical reading of the verdict, defined here and referenced
everywhere (the procedure, the skills, and the executable projection `tools/meta-queue`,
pinned by `packages/admin/tests/test_meta_queue.py`) — **never re-derived as a divergent
copy**:

```python
PASS_PREFIXES      = ("pass", "ship", "approve", "lgtm")     # "approve" also covers "approved"
NONBLOCKING        = ("non-blocking", "non blocking", "nonblocking",
                      "no-blocker", "no blocker", "no concern")
HARD_BLOCK         = ("fail", "do not merge", "don't merge", "dont merge")  # disqualify unconditionally
UNVERIFIED_MARKERS = ("pending", "wip", "tbd", "todo")      # not-done hedge → unverified (zero overlap: "ship" ⊉ "wip")

def verdict_is_pass(two_pass):           # mergeable IFF a reviewer already approved
    s = (two_pass or "").strip().lower()
    if not s: return False
    if not any(s.startswith(p) for p in PASS_PREFIXES):    return False   # must OPEN with a pass token
    if any(m in s for m in HARD_BLOCK):                    return False   # FAIL / DO-NOT-MERGE → never
    if any(u in s for u in UNVERIFIED_MARKERS):            return False   # "ship pending QA" → not done
    if any(p in s for p in NONBLOCKING):                   return True    # concerns explicitly de-fanged
    return "concern" not in s and "block" not in s                       # else any live concern/blocker → no

def verdict_is_blocking(two_pass):       # reviewer AFFIRMATIVELY said no (≠ merely unverified)
    s = (two_pass or "").strip().lower()
    if not s: return False
    if any(m in s for m in HARD_BLOCK):  return True
    if any(p in s for p in NONBLOCKING): return False
    return "concern" in s or "block" in s
```

Three states result, **mutually exclusive** for any well-formed verdict:
**pass-equivalent** (`verdict_is_pass`), **blocking** (`verdict_is_blocking`), and
**unverified** (neither — `pending` / empty / `"required AUDITOR-GRADE…"` / `"n/a …"`).

**CRITICAL SAFETY — this only RECOGNIZES a verdict a reviewer/chip already wrote; it never
invents one.** It is deliberately **conservative**: an ambiguous verdict reads as *not-pass*
(held for the operator), never as pass (auto-merged). A false hold is safe; a false merge is
not. Note the consequences: a verdict that merely *opens* with a pass token but carries a live
blocker (`"SHIP — one blocking issue"`) is **blocking, not pass**; and `"fail"` is matched as a
substring, so even `"PASS, no failures"` reads as not-pass — write a clean `"PASS"` to merge.

#### Truth table (the live strings, pinned by the test)

| `two_pass` | `verdict_is_pass` | `verdict_is_blocking` | merge gate result |
|---|:---:|:---:|---|
| `PASS` | ✅ | — | auto-merge |
| `SHIP (2 non-blocking concerns) — …reviewer…` | ✅ | — | auto-merge |
| `SHIP / CONCERNS-no-blockers…` | ✅ | — | auto-merge |
| `LGTM, no blockers` / `APPROVED` | ✅ | — | auto-merge |
| `required AUDITOR-GRADE…` | — | — | **unverified** → auto-review (or poke) |
| `pending` / `` (empty) / `null` / `n/a …` | — | — | **unverified** → auto-review (or poke) |
| `SHIP pending QA` / `approve pending review` | — | — | **unverified** (a pass-prefix + not-done hedge) → auto-review |
| `FAIL …` | — | ✅ | **hold** → bounce to chip |
| `CONCERNS: cross-bot leak` | — | ✅ | **hold** → reviewer flagged |
| `SHIP — one blocking issue` | — | ✅ | **hold** (prefix alone never merges) |
| `PASS — DO NOT MERGE until release` | — | ✅ | **hold** |

#### The PR-body artifact requirement (B2 — verdict from artifact, not self-report)

`verdict_is_pass` above recognizes a verdict **string** — but the string it reads, the ledger
`two_pass` field, is *self-reported*: a chip can write it in the **same pulse that opened the
PR**, with no review behind it. That is exactly what stranded #3347 (a `two_pass` verdict
written at dispatch → merged-unreviewed with a real bug) and what #3372/#3373 lacked. So a
pass-equivalent `two_pass` is **necessary but not sufficient** for auto-merge.

**The gate also requires the FACT behind the string: the PR *body* must contain an independent
two-pass review section — a `## Two-pass review …` / `## Independent two-pass review` heading
(the convention chips and auto-review chips already emit) — whose OVERALL verdict is
pass-equivalent.** A ledger `two_pass` value with **no corresponding PR-body review section is
NOT sufficient**: such a chip is treated as green-but-unverified (auto-review / poke), never
auto-merged. The artifact is the thing a chip cannot fake by setting a ledger string.

This is checked deterministically by **`tools/meta-verdict-check <pr>`** (fetches
`gh pr view --json body`, extracts the review section's overall verdict, and classifies it
through the **same** `verdict_is_pass` / `verdict_is_blocking` above — it does **not** fork the
predicate; it only adds the artifact requirement on top). It stays consistent with the standing
"never parse text for CHECKS — use `--json`" rule: **CHECKS remain JSON**; only the verdict
*section* is parsed from the body markdown, in one centralised, tested place
(`packages/admin/tests/test_meta_verdict_check.py`). Exit `0` = qualifies, `1` = does not,
`3` = `gh` failure. Conservative like the predicate: a missing section, an unextractable overall
verdict, or **any** review section carrying a blocking verdict → does-not-qualify (a false hold
is safe; a false merge is not).

### The auto-merge rule, expressed against the schema

`/status`, the scheduled reconciler, and `tools/meta-queue` evaluate this mechanically — no
prose-parsing beyond the canonical predicate above:

```
auto-merge  ⟺  bucket == "open_green"  AND  verdict_is_pass(two_pass)  AND  PR-body carries a pass-equivalent two-pass review section (tools/meta-verdict-check qualifies)  AND  reversible == true  AND  operator_merge != true
hold + surface (verified, awaiting the operator's merge click)  ⟸  operator_merge == true  AND  bucket == "open_green"  AND  verdict_is_pass(two_pass)
hold + surface  ⟸  verdict_is_blocking(two_pass)            (reviewer flagged: FAIL / DO-NOT-MERGE / live concern)
hold + auditor-grade + human look  ⟸  reversible == false  (could lock out access / brick the pod)
auto-review (or poke)  ⟸  bucket == "open_green"  AND  ( NOT verdict_is_pass  OR  no PR-body review artifact )  AND  NOT verdict_is_blocking   (green-but-unverified; see below)
relaunch from checkpoint  ⟸  bucket == "stalled"
bounce back into chip  ⟸  verdict_is_blocking(two_pass)  or  chip skipped its in-chip review (unverified on an open PR)
```

The **PR-body artifact conjunct** (B2, above) is the fact-gate: a pass-equivalent ledger
`two_pass` merges **only** when `tools/meta-verdict-check <pr>` confirms an independent
two-pass review section in the PR body with a pass-equivalent overall verdict. A ledger string
with no such section is **green-but-unverified**, so it routes to auto-review, not auto-merge —
closing the "verdict written at dispatch, merged unreviewed" hole (#3347).

A `privileged` path that is still `reversible` (ordinary config/auth/appliers that passed
auditor review) auto-merges like anything else — `privileged` is a *review-depth* flag, not
a merge block. `reversible == false` blocks the merge for a human look.

**Green-but-unverified is auto-resolved, not just poked.** A green, reversible,
**non-privileged** chip whose verdict is *unverified* (not pass, not blocking) is no longer
left for the operator: the reconciler **auto-dispatches a review chip** (`[META:<id>] <title>
(review)`) whose in-chip two-pass writes a real verdict to the PR body + ledger, so the next
sweep auto-merges on its PASS. Bounded `review_count < 2`, then escalate. A **privileged or
irreversible** unverified chip still **pokes** for a human review (auditor-grade). See
`docs/meta-reconcile-procedure.md` step 4.

#### When `operator_merge` is warranted

A coordinator that wants a human to click merge on a *reversible* chip anyway — a sensitive
surface the auto-lane would otherwise grab — sets `operator_merge == true`: the structured,
parse-free form of the old "DO NOT SELF-MERGE" chip-note convention. Such a chip still gets its
full two-pass review (a pass-equivalent verdict means it is verified and ready);
`operator_merge` only withholds the final click.

`operator_merge == true` is **valid only when `privileged == true` OR `reversible == false`** —
the cases where a human's eyes on the final click genuinely add safety. On a **reversible,
non-privileged** chip it adds nothing but friction: it strands a green, reviewed, cheaply
revertible PR waiting for a click the operator will just make (this was a second cause of the
0-merges pile-up). This is a **forward rule for new chips**. For *existing* ledgers the
reconciler does **not** silently override a human's deliberate hold (and never edits another
aspect's ledger): it emits an **advisory** queue note — `operator_merge set on a
reversible+non-privileged chip #N — clearable` — so the operator can clear the flag and let the
chip auto-merge (e.g. apps #3108, #3112). `tools/meta-queue` renders this advisory inline on
the verify-then-merge line.

## Chips deposit outcomes — they never just return them in a thread

A dispatched child runs in its **own session** (`spawn_task` / `claude --bg`) whose output
never re-enters the coordinator's context — and even an in-context Agent subagent's summary
lives only in the *disposable* conversation, lost on the next `/clear`. So a child's outcome
must be a **durable artifact at a location named before dispatch**, not a thread return:

- **Build chip** → the artifact is the PR (`pr`) + its two-pass verdict (`two_pass`).
- **Investigation / design chip** → the artifact is a committed file, a memory entry, or a
  doc, recorded in `output` (e.g. `"docs/findings-usage-perf.md"` or
  `"memory:project_usage_perf"`). "Report your findings" is not a deliverable; "write your
  findings to `<path>`" is.

The **ledger is the rendezvous**: at *dispatch* the coordinator writes the chip row with its
`output`/`pr` pointer (where the answer *will* land); the *child* fills that artifact (it
does **not** write the shared ledger — that avoids cross-session write races); at *reconcile*
`/status` (or the scheduled reconciler) reads the artifact and folds the result back into the
row — never a thread. A chip is not `done` until its outcome is in a PR / spec / memory /
`output` file. This is what keeps a child's work from evaporating when its session closes or
the coordinator resets.

## The decision queue (computed projection)

The operator's inbox is **not a separate file** — it is the cross-aspect union, computed on
demand, of everything in the ledgers that needs a human:

- every `decisions_pending[]` entry whose `snoozed_until` is absent or in the past,
- every chip the auto-merge rule **holds**: `verdict_is_blocking(two_pass)` (reviewer flagged),
  `reversible:false` (irreversible — auditor look), `operator_merge:true` while
  green+`verdict_is_pass` = verified-awaiting-operator-click (with the *clearable* advisory when
  the flag isn't warranted), green-but-unverified (`open_green` AND NOT `verdict_is_pass` — the
  reconciler auto-reviews the reversible+non-privileged ones rather than parking them here), or
  `stalled`,
- every `gate` that needs the operator — object gates with `blocked_on: "operator"`, plus
  freeform string gates (skipping ones clearly gated on a date or another aspect).

Dedup a gate against a `decisions_pending` entry that shares its `id` (show the decision — it
carries the recommendation).

`/queue` renders this from any session (it is global, not per-aspect) and lets the operator act
by number — approve / merge / snooze / open / dismiss — writing the resolution back to the
**owning ledger**. Because the queue is a projection, there is nothing to keep in sync: drain it
and the ledgers remain the single source of truth. The scheduled reconciler's edge-triggered
poke is computed from the same set.

This projection is implemented deterministically by **`tools/meta-queue`** (rendered text by
default, `--json` for programmatic callers) — it is the executable form of the rules above, so the
two must not drift (a test pins them: `packages/admin/tests/test_meta_queue.py`). The read-heavy
skills (`/queue`, `/reconcile`, `/coherence`, `/launch`) run it inside a **throwaway subagent** so
the ~200KB whole-ledger read never lands in — and never persists across — the operator's main
context; the scheduled reconciler computes its red zone from the same script (`--json`).

## Dispatch-time collision check (`tools/meta-inflight`)

The decision queue answers *"what needs me?"*; its dual answers *"who is already on this?"*
**before** a `/launch` or `/design` spawns work — so sessions don't get launched at cross
purposes or redundantly. Cross-session collision detection was otherwise after-the-fact and
partial (the daily `/coherence` pass is cross-aspect-only + recommend-only; the dispatch paths
had no pre-spawn check; and a child writes nothing to shared state, so its mid-flight scope is
invisible until the PR). See `docs/spec-substrate-2026-06-15.md` §11.

`tools/meta-inflight --aspect <id> [--keywords …] [--scope <globs>]` is that check. It scans
four signals and prints overlaps ranked by match strength (or a clean "no overlap found"):

- **non-terminal chips** in every ledger (`bucket` not terminal per `is_terminal_bucket` — i.e.
  not `done`/`live`/`merged` *nor a drifted variant* like `merged-deployed`/`MERGED`) — matched on
  the chip's `scope` globs (below), title, note, and `pr`/`task_id`; un-dispatched `backlog[]`
  strings count too,
- **open fleet PRs** — one `gh pr list --json` call (`claude/*` head branch OR a `[META:<id>]`
  title prefix); the `files` field gives file-level overlap,
- **live `[META:*]` sessions** — only when the calling skill supplies them via `--sessions-json`
  (the script is stdlib-only and cannot list sessions itself, the same script-vs-skill split
  `tools/meta-queue` uses for liveness),
- **active dispatch claims** (the claims registry, below) — closes the *invisible mid-flight
  window*: a just-dispatched chip has no PR yet AND is not in a ledger yet, so the first three
  signals can't see it, and a 2nd dispatch of the same work lands redundantly. A claim is written
  the instant a chip/subagent spawns, so this signal makes that no-PR-yet work visible immediately.

### The dispatch-claim registry (`~/.claude/meta-state/claims/`)

A **claim** is a small JSON file the `tools/hooks/record-dispatch-claim.sh` PreToolUse hook writes
on **every** `Agent` / `mcp__ccd_session__spawn_task` spawn from a session that has an active-aspect
marker (spec §17 / Initiative 12, B4). It is **operator-machine-local transient runtime state** —
the same category as the active-aspect marker at `~/.claude/meta-state/active-aspect/`, **not** the
durable per-aspect ledger — so it lives home-scoped at `~/.claude/meta-state/claims/<claim-id>.json`,
NOT under the project-memory `meta-state/` ledger dir. Shape:

```jsonc
{
  "aspect": "substrate",          // from the active-aspect marker (validated strict-kebab)
  "title": "[META:substrate] …",  // the spawn's display title (Agent.description / spawn_task.title)
  "time": 1751000000,             // epoch seconds at spawn
  "ttl_seconds": 14400,           // default 4h; override via $META_CLAIM_TTL_SECONDS
  "tool": "Agent",                // which spawn surface
  "scope": [],                    // always [] — the spawn input carries no structured scope
  "session": "…"                  // OPTIONAL: spawning session id, when the payload has one
}
```

- **Why record-always + WARN, never block** (operator decision): fuzzy title overlap must not block
  legitimate parallel work. The hook **only performs the side-effect** (write the claim) and returns
  passthrough — no `updatedInput`, no `permissionDecision` — so the spawn proceeds unchanged. The
  "warn" is surfaced by `meta-inflight` reading the registry, never by the hook. On ANY error the
  hook records nothing and exits 0 (worst case = today's invisibility).
- **Self-propagation down the spawn tree.** Because the prefix hook titles a spawned chip
  `[META:<id>] …` and `meta-marker-register-on-start.sh` makes any `[META:<id>]`-titled session
  self-register its own marker, a chip — and its grandchildren — all carry a marker and all record
  claims. The claim layer therefore fires at every level, closing the "dies one level below the
  coordinator" gap the advisory prose had.
- **Matching.** A claim has no structured scope (the spawn input can't carry one), so `meta-inflight`
  matches it on **aspect + title keywords** — keyword-match is the design, not a limitation.
- **TTL / self-expiry.** A claim only needs to bridge dispatch → the work becoming visible another
  way (its PR listed, or a coordinator recording it in a ledger). `meta-inflight` **ignores** a claim
  once `now − time > ttl_seconds` AND **prunes** it from disk on read (`--prune` is the standalone
  maintenance path), so a dead chip's claim self-expires and never warns forever. The default TTL is
  generous (4h) because the policy is warn-only: an over-long TTL costs at most a spurious advisory,
  while an over-short one reintroduces the invisibility gap.

The **`scope` field** is what gives the check file-level precision. It is a list of file globs
the chip *intends* to touch, **declared by the coordinator at dispatch** — the child still writes
nothing to the ledger (race-avoidance by schema is preserved; `scope` is a dispatch-time intent,
not a child-maintained claim). A scope overlap (two chips, or a chip and a PR, touching the same
file) outranks an aspect/keyword match — including **across aspects**, which is exactly the
collision that mid-flight invisibility used to hide until the PR. The "Serialize the contended
files" rule (`docs/META-bootstrap.md` → "How a META session behaves") is the operator's lever
once the check surfaces such an overlap.

The check is **advisory + confirm-first**, consistent with the never-auto-dispatch-new-work gate:
the dispatch skills run it and present any overlap so the operator can merge the efforts, proceed
anyway, or cancel. It never blocks, edits, or merges. Like `tools/meta-queue`, it is the
executable form of these rules, pinned by a test (`packages/admin/tests/test_meta_inflight.py`).

## The read/write contract

- **`/meta` (bootstrap)** — READS the ledger to render the in-flight block. If the file is
  missing, it reconstructs from `gh pr list` + memory and writes a fresh one.
- **`/status` (return-pulse)** — READS the ledger for the expected children, reconciles each
  `chip` against live `gh` state, updates `bucket`/`two_pass`/`pr`/`last_commit`, drives the
  auto-merge rule above, appends any new fork to `decisions_pending`, and WRITES the ledger
  back. A clean pulse leaves the ledger current.
- **`/close` (checkpoint)** — WRITES the ledger as the authoritative refresh: every chip with
  its current `bucket` + `two_pass`, the `next_action`, open `gates`, `decisions_pending`,
  and stamps `updated`. As part of that write it **prunes prior-bout terminal chips** to the
  one-line archived form (see "Size budget & pruning" below) so the ledger never accumulates
  dead history. The litmus is unchanged: a fresh `/meta` reading only the trio (now
  including this file) must know exactly where things stand.
- **`meta-reconcile` (scheduled, unattended)** — READS every ledger, reconciles each chip
  against `gh`, and (for `autonomy != observe`) drives the mechanical rule autonomously:
  auto-merges green + `verdict_is_pass` + reversible (holding any `operator_merge:true` chip for
  the operator, with the clearable advisory where the flag isn't warranted), relaunches stalled
  chips (bounded ≤2), **auto-dispatches a review chip** for a green-but-unverified
  reversible+non-privileged chip (bounded ≤2) and **fix-forwards** a PR red on a mechanical gate
  (bounded ≤2), and pokes the operator only for the red zone (blocking verdicts, irreversible
  holds, dead chips, privileged/irreversible unverified, fleet-blocking ratchet reds,
  `decisions_pending`, collisions, orphan PRs). WRITES `bucket`/`pr`/`last_commit` back; never
  sets `two_pass`, never invents a verdict, never dispatches a *new product* bite. On each
  write-back it also **collapses prior-bout terminal chips** to the archived form (same rule as
  `/close`). It is the unattended sibling of `/status`. Behavior doc (version-controlled):
  `docs/meta-reconcile-procedure.md`, mirrored to
  `~/.claude/scheduled-tasks/meta-reconcile/SKILL.md`.

## Conventions

- **Low-friction by design — don't reshape hand-edits to match an example.** `gates[]` and
  `backlog[]` entries may be plain strings; `last_commit` may be a SHA or a timestamp; extra
  descriptive fields on a chip are fine. The only fields any consumer depends on are the
  auto-merge inputs (`bucket`, `two_pass`, `reversible`, `operator_merge`), the discovery fields (`pr`,
  `task_id`), and — when present — the dispatch-time `scope` globs (`tools/meta-inflight`). Everything
  else is for the operator's and coordinator's legibility.
- Atomic writes (write temp + rename) so a half-write never corrupts the ledger.
- `updated` is a date string; do not invent timestamps — use the real date.
- Keep `note` to one short clause; long narrative goes in the memory topic file.
- When a chip reaches `done`/`live`/`merged`, leave it in `chips[]` for the bout it completed
  in, then collapse it to the one-line archived form at the next write-back (its history is in
  the PR + the memory topic file). See "Size budget & pruning" below.
- The handle (`id`) is stable across the chip's life so prose ("T2 merged") stays resolvable.

## Size budget & pruning

The ledger holds *what is in motion*, not *what shipped*. Once a chip's work is done, its
detail is durably recorded in two places already — the **PR** (git history, the diff, the
review thread) and the aspect's **memory topic file** — so keeping its long `note`/`output`
in the ledger is pure dead weight. Left unpruned, terminal chips dominate the file (the
corpus once hit ~200KB, ~95% terminal chips, the largest single ledger 53KB — bigger than the
whole memory index), and every raw ledger read lands that weight in the operator's context.
These are **hard guidelines** every writer (`/close`, `/status`, `meta-reconcile`) honors:

- **Per-ledger size budget ≈ 8KB.** A ledger over ~8192 bytes is a smell — tighten it. The
  usual culprits are prose walls and un-pruned terminal chips, in that order.
- **`bout` and `next_action` are EACH one line (~280 char ceiling).** They answer "where is
  this aspect in its arc" and "what is the single next move" — not a changelog. Multi-sentence
  history belongs in the memory topic file, never here.
- **Prior-bout terminal chips collapse to the one-line archived form.** A chip whose `bucket`
  is `done`/`live`/`merged` and which is NOT from the current bout (its `dispatched` /
  `last_commit` date predates the ledger's `updated` date) keeps only
  `{id, title, pr, bucket, two_pass}`; the long `note`/`output` and the now-irrelevant
  process fields (`branch`, `task_id`, `privileged`, `reversible`, `dispatched`, `last_commit`)
  are dropped. The handle stays so prose like "T2 merged" still resolves. A current-bout
  terminal chip and every non-terminal chip (backlog / dispatched / draft / open_* / stalled /
  blocked) are kept verbatim.

**Tooling:** `tools/meta-ledger-prune` is the idempotent batch migrator that enforces the
collapse rule across every ledger at once. `--dry-run` (the default) prints per-file
before/after byte counts and flags over-budget files + over-length prose; `--apply` first
writes a timestamped backup of the whole `meta-state/` dir (it is NOT git-tracked — the backup
is the only undo) and then rewrites each changed ledger atomically. Writers do the same
collapse inline as they write, so the bloat never re-accumulates between batch runs.
