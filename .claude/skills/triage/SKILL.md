---
name: triage
description: Sweep the open GitHub issue tracker and recommend a disposition for each issue — CLOSE / DESIGN → aspect / READY → aspect / ASK — as a ranked one-line-per-issue digest. The issue-tracker twin of /queue: recommend-only, confirm-first; closing is NEVER automatic. Act by number to close (with a required reason), route to /design, or dispatch a ready chip.
---

`/loose-ends` for the issue tracker. The operator wants the **open issues** turned into a
ranked digest of recommendations, the same way `/queue` turns the ledgers into a decision
inbox. This is the Phase-2 surface of META:substrate Initiative 1 (issue intake);
authoritative design is `docs/spec-substrate-2026-06-15.md` §4.3 — implement that, don't
re-derive it.

It is a **recommend-only sweep** (`docs/spec-substrate-2026-06-15.md` §3 invariant): the
sweep RECOMMENDS a disposition per issue; the operator decides the red zone (close / route /
dispatch). **Closing an issue is NEVER automatic** — it is confirm-first and a reason is
required. Lead every surfaced fork with a recommendation, never a bare "which?".

## 1. Enumerate the open issues (delegate the heavy read to ONE subagent)

Fetching + reading every open issue body in THIS context would bloat the operator's main
thread the way a raw ledger read does. So spawn ONE `Agent` (the `Agent` tool; a cheaper
model is fine — the per-issue triage is mechanical classification against the registry, no
product design) whose whole job is to enumerate, fetch, preliminarily classify, and return
**only the compact ranked digest** (step 4 shape). Brief it with steps 2–4 below. It returns
the digest; the bodies stay in its disposable context.

- **List:** `gh issue list --repo cjalden/evolve --state open --limit 200 --json number,title,labels,updatedAt,author`.
  `gh issue list` **excludes PRs** by design, so the result is issues only. (Default limit is
  30 — pass `--limit 200` so a large backlog isn't silently truncated; if 200 is hit, say so.)
- **Edge-trigger filter (default).** Skip issues already carrying the `meta:triaged` label —
  add `--search "-label:meta:triaged"` to the list call. A re-run then surfaces only the
  **new** issues, not the whole backlog again. `/triage all` re-sweeps everything (drop the
  filter). See step 5 for why the label is the chosen mechanism and how CHANGED is handled.
- **Per-issue detail:** for each surviving issue, fetch the normalized record via
  `python3 tools/meta-issue <N>` — it returns `{number, url, state, title, body, author,
  created_at, age_days, labels[], aspect_hints[], agent_able, proof, comments[]}`. Use
  `aspect_hints[]` (the `<aspect>:` label prefixes, e.g. `edr` from `edr:agent-able`) as a
  routing **prior**, `agent_able` + `proof` as the READY signal, `age_days` + `comments[]`
  for the CLOSE/ASK signal. Don't invent fields — these are the only ones `meta-issue` emits.

## 2. Load the routing knowledge (same classifier as `/design`)

DESIGN/READY both pre-assign the owning aspect by **content** — reuse the exact `/design`
classifier so triage and intake route identically:

- The **"Surface ownership (the routing map)"** table + the **Aspect registry** in
  `docs/META-session-guide.md` — surface → owning aspect, each aspect's mission/invariants.
- Each aspect's **mission + backlog** — glob `meta-state/*.json` (skip `_README`). The
  missions are the richest classifier signal; the map resolves surface mentions.

An aspect-prefixed label (`aspect_hints[]`) is a **validated prior, not the answer** — labels
go stale, so confirm it against the issue's content the way `/design` validates a label-derived
route. A bare `bug`/`feat` label is not an aspect hint; classify those purely by content.

## 3. Per-issue preliminary triage → exactly ONE recommendation

Assign each issue **one** of these four buckets (and only one):

- **CLOSE** — stale / duplicate / wontfix / invalid / out-of-scope / **already-fixed**. A
  reason is **REQUIRED**, and an already-fixed call **MUST be cross-checked** before
  recommending it: search merged PRs and the current code for the fix (`gh pr list --state
  merged --search "<terms>"`, grep the named files/symbols) — an issue can describe a bug that
  has since been fixed. Never recommend CLOSE on a hunch; carry the evidence in the why-line.
  Closing stays confirm-first regardless (§3 invariant) — this bucket only *recommends* it.
- **DESIGN → `<aspect>`** — actionable, but needs a design decision before it can be chipped.
  Pre-assign the owning aspect with the step-2 classifier (label = validated prior). This is
  the issue-shaped twin of `/design "<free text>"` → it routes to the same aspect.
- **READY → `<aspect>`** — actionable AND spec'd enough to dispatch a chip directly: a
  confirmed fix direction and a falsifiable acceptance test. This generalizes the
  `edr:agent-able` label + the `Proof:` line (`agent_able==true` with a non-null `proof` is the
  strong signal) and mirrors `/launch`'s READY BITE — a brief could be written today, no
  coordinator needed.
- **ASK** — needs the reporter to clarify before it can be triaged (under-specified, no repro,
  ambiguous ask). The recommendation is a specific question to post back, not a disposition.

Rank-ordering rule: an issue with `agent_able`+`proof` and a clear owner is more actionable
than one needing design, which is more actionable than one needing reporter info; a
clearly-stale issue sorts to CLOSE near the bottom. Use this to order the digest top-down.

## 4. Ranked digest — one line per issue (the `/queue` shape)

Print the digest the subagent returns, ranked by actionability/severity so the operator reads
top-down and acts on the live ones first. One line per issue:

```
#<N> · <REC> · <aspect|—> · <one-line why>
```

e.g.

```
#2656 · READY   · edr   · agent-able + Proof: pytest; confirmed 3-part fix direction → dispatch
#2657 · DESIGN  · ui    · Pod Health needs a dismiss/ack affordance — needs design
#2659 · DESIGN  · evo-asst · EVOLVE_CALLER_SURFACE never reaches MCP — actionable, needs design
#2065 · ASK     · —     · "safe-upgrade preflight" — scope unclear, ask reporter to confirm intent
```

`REC` ∈ {`CLOSE`, `DESIGN`, `READY`, `ASK`}; `aspect` is the routed owner (`—` for CLOSE/ASK
where none applies). Number the lines so the operator can act by issue number. If nothing
survives the edge-trigger filter, print `✅ No new open issues to triage` (and note `/triage
all` re-sweeps the already-triaged ones).

## 5. Edge-triggered re-runs — the `meta:triaged` label (chosen mechanism)

**Chosen: a GitHub label, not a local state file.** When the operator acts on an issue
(step 6), apply `meta:triaged` to it. The next default run filters those out
(`--search "-label:meta:triaged"`, step 1), so it surfaces only **new** issues. The label is
the lighter mechanism *and* the more coherent one: it lives in the tracker (operator-visible,
survives across accounts/machines — one `$HOME` but the truth is GitHub-side), needs no new
operator-local file to manage or keep in sync, and matches the Phase-1 `meta:routed:<aspect>`
write-back convention. A local state file would be a second source of truth that drifts from
the tracker.

**CHANGED issues** re-surface by **removing the `meta:triaged` label** — which the natural
loops already invite: an ASK whose reporter replies, or a CLOSE/wontfix that gets reopened,
is exactly when someone clears the label, and the next `/triage` picks it back up. `/triage
all` force-re-sweeps everything. (A timestamp-comparing state file could auto-detect CHANGED,
but that is the heavier mechanism §4.3.5 explicitly lets us decline — noted here as the
deliberate trade.)

## 6. Confirm-first actions — the operator drives by number (CLOSING IS NEVER AUTOMATIC)

This stays **HERE** in the main thread (like `/queue`'s Act section) — the heavy enumerate +
classify already happened in the subagent. The sweep recommended; the operator decides. On
each confirmed action, apply the `meta:triaged` label (step 5) so the issue drops off the next
default run.

- **CLOSE `#N`** *(confirm-first — never automatic; §3 invariant)* — only on the operator's
  OK and only with the required reason. Post the reason as a comment, apply a routing/closed
  label, then close:
  - `gh issue comment <N> --repo cjalden/evolve --body-file <file>` (the reason — body-file,
    never a heredoc; see `[[gh_comment_body_file_not_heredoc]]`),
  - `gh issue close <N> --repo cjalden/evolve --reason "not planned"` (or `completed` for
    already-fixed),
  - `gh issue edit <N> --repo cjalden/evolve --add-label meta:triaged`.
- **DESIGN `#N`** — route it to design. Either run `/design <N>` now (the Phase-1 issue-ingest
  path — same classifier, opens the owning coordinator), **or** deposit a `backlog` entry in
  the owning aspect's ledger carrying `issue: <N>` provenance so a later `/launch`/`/meta`
  picks it up. Label `meta:triaged` either way.
- **READY `#N`** — dispatch a chip directly, mirroring `/launch` step 3 (this IS the operator's
  Gate-1 greenlight; never auto-dispatch). For the approved issue:
  - Decide the chip's `scope` (file globs it will touch), then run the **pre-dispatch collision
    check** (ADVISORY + confirm-first): `python3 tools/meta-inflight --aspect <id> --keywords
    "<kw>" --scope "<globs>"`. On an overlap, STOP and present it (merge / proceed / cancel);
    a clean "no overlap" → dispatch.
  - `spawn_task` a chip titled `[META:<id>] <fix> (Closes #N)`. Brief it per `/launch`'s
    standing blocks — what it does / touches / does NOT do; the `proof` line as the acceptance
    test; resume-safe push discipline; `git -C <worktree>` not `cd && git`; deposit outcome at
    the PR; in-chip two-pass; the standing Definition-of-done block; "Spawned by META:`<id>`"
    + the `[META:<id>]` propagation line + the `meta-inflight` dispatch-check line.
  - **The PR body MUST carry `Closes #N`** — GitHub auto-closes the issue on merge, which the
    reconciler already detects. No new closing machinery; the loop closes itself.
  - Record the chip in the aspect's ledger `chips[]` (`bucket: dispatched`, `task_id`, the PR
    pointer, the `issue: <N>` provenance, and the `scope` globs). Label `meta:triaged`.
- **ASK `#N`** — post the clarifying question (`gh issue comment <N> --body-file <file>`),
  label `meta:triaged`. When the reporter replies, the label is removed (step 5) and the next
  `/triage` re-picks it.

Keep it tight: delegate the enumerate+classify → render the ranked digest → act on the
operator's numbers → leave the tracker labeled so re-runs stay edge-triggered. This is the
issue-tracker member of the recommend-only sweep family (`/queue`, `/reconcile`, `/coherence`).
Complements: `/design <N>` (route one issue interactively), `/launch` (dispatch designed
bites), `/queue` (the ledger-side inbox).
