# Manifest Reflex

**Status:** design draft, 2026-05-09
**Author:** Claude (drafting for Pod-Admin's review)
**Related:**
[manifest-spec.md](manifest-spec.md),
[spec-better-engine-conversational-approval-2026-04-18.md](spec-better-engine-conversational-approval-2026-04-18.md),
[spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md),
[spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md)

## Problem

Bots in the pod (team-bot-a, admin-bot, team-bot-b, team-bot-c, security-bot, personal-bot, …) routinely create
artifacts in service of conceptual *applications* — Python scripts, `.md`
trackers, `.json` data stores, cron entries — without going through any of the
four sanctioned forge entry points:

1. Gallery template install
2. Evo chat forge wizard
3. RSI BuildApp proposal
4. Spec wizard

When a user asks admin-bot *"please help me track my protein intake"* and admin-bot
writes `~/workspace/ops/tools/protein-tracker.py` plus a daily cron, an
`ApplicationManifest` is **not** created. The application registry at
`{shared_dir}/applications/{bot_id}/` drifts from the on-disk reality.
Consequences: testing, telemetry, gallery promotion, lifecycle, and the
"applications" dashboard all become unreliable.

There is already a **periodic scanner**
([packages/admin/evolve_admin/applications/scanner.py](../packages/admin/evolve_admin/applications/scanner.py))
that walks workspaces and emits manifests with
`source = MANIFEST_SOURCE_DISCOVERED`, but it runs after the fact, by which
point an un-manifested app has been live for hours-to-days, may have been
edited by the bot multiple times, and the scanner's clustering of files into
"applications" is heuristic and fuzzy.

The goal is to push manifest authorship as close to the moment of creation
as possible — ideally inside the same turn the bot writes the file.

## Vocabulary

- **App-shaped artifact** — a persistent unit of functionality the bot
  produces: a script, a cron entry, a long-lived data file, a tracker
  document, or a coherent directory of those.
- **Reflex** — the bot's in-the-moment recognition that *"I am about to
  ship something app-shaped"* and the action it takes (author or update a
  manifest) as a result.
- **Sanctioned path** — one of the four existing forge entry points above.
  These all produce manifests.
- **Reflex path** — the new fifth entry, fired from inside an active bot
  session.

## Trigger conditions — what counts as "app-shaped"?

**The rule is one line: anything the bot writes that will outlive this
turn is an app.**

Earlier drafts of this spec gated the reflex on "user intent" (the bot
was responding to a request that implied ongoing functionality), but
2026-05-09 review (Pod-Admin, open question §5) rejected that carve-out:
even a single notes file the bot keeps for its own use is an app of a
sort — Evolve's job is to track all of it, not just the ones that look
project-shaped. If clusters of small "single notes" apps accumulate,
that itself becomes a signal for RSI to propose consolidation into a
notes-taking app.

So the reflex fires whenever the bot creates or modifies content that
has both of:

1. **Persistence** — file is *not* in `/tmp`, *not* under a system
   surface (`.openclaw/`, `workspace/evolve/`, `{shared_dir}/proposals/`,
   etc.). If the file is intended to outlive the current turn, it
   counts.
2. **Material content** — the file has non-trivial content (text,
   code, data, a cron entry that runs something). Empty placeholder
   files don't count.

Things that are **not** app-shaped (and must not fire the reflex, to
avoid noise):

- One-off draft content the bot writes to show the user *and then
  deletes or doesn't keep* (transient scratch).
- Edits inside an *already-manifested* app's file list — those are
  `update_manifest` events, handled differently (see §"Updates").
- Files inside `~/.openclaw/`, `~/workspace/evolve/`,
  `{shared_dir}/proposals/`, etc. — system surfaces.
- Files in `/tmp/*`.

The scanner's `_infer_layer()` already classifies layer (script / data
/ state / reference); the reflex should produce manifests with
appropriate layer values so detection is consistent between in-flight
and periodic paths.

## Architecture

Two layers, in order of decreasing fidelity:

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer A — In-flight reflex (preferred, runs per turn)           │
│                                                                  │
│  ┌─ A1. System-prompt instruction (POD_CONDUCT addition) ──────┐ │
│  │  + record_application tool exposed by Evolve plugin         │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌─ A2. Post-turn audit (TurnObserver / agent_end) ────────────┐ │
│  │  detects un-manifested writes; nudges next turn or auto-    │ │
│  │  drafts a manifest at confidence < 1.0                      │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (anything Layer A missed)
┌──────────────────────────────────────────────────────────────────┐
│  Layer B — Periodic scan (existing scanner.py, daily)            │
│                                                                  │
│  Walk workspace, diff vs. manifests, emit Signal +               │
│  draft-manifest for each gap.                                    │
└──────────────────────────────────────────────────────────────────┘
```

Both layers feed the **same downstream sink** — a draft `ApplicationManifest`
with `source ∈ {bot_created, discovered}`. The reflex isn't a parallel
universe; it's a faster path into the same store.

### Layer A1 — System-prompt instruction + `record_application` tool

The cheapest, lowest-risk approach. Two pieces:

**Piece 1: a new tool exposed by the Evolve OpenClaw plugin.**

```
record_application(
    app_id: str,                  # slug, e.g. "protein-tracker"
    name: str,                    # human-readable
    purpose: str,                 # 1–2 sentence description
    files: list[str],             # absolute paths, will be made relative
    crons: list[CronEntry] = [],
    inputs: list[str] = [],
    outputs: list[str] = [],
    update: bool = False,         # true → patch existing manifest
)
```

The tool runs inside the bot session and writes directly to
`{shared_dir}/applications/{bot_id}/{app_id}.json` via
[manifest.save_manifest](../packages/admin/evolve_admin/applications/manifest.py)
with `source = "bot_created"` and
`source_detail = f"reflex: session {session_id}: {first_user_message[:80]}"`.

The tool sits next to the existing `DeferTool` in
[packages/plugin/src/tools/](../packages/plugin/src/tools/), gated by the
plugin's `manage` capability tier so it's only available where forge is
allowed.

**Piece 2: a new section in `POD_CONDUCT.md`** that the existing
session_surface.py mechanism injects into every session's
`systemAppend`. Roughly:

> **Application manifest reflex.** When you build something durable for the
> user — a script, a cron, a tracker — call
> `record_application(...)` in the same turn. If you're modifying an
> existing app, call it with `update=True`. If you're not sure whether
> something is app-shaped, ask the user. The manifest is how the rest of
> the pod (testing, monitoring, gallery) finds your work; un-manifested
> apps drift and break.

This block is added to the existing `<!-- summary-start/end -->` extraction
flow at
[session_surface.py](../packages/analyzer/session_surface.py).

**Why this is the preferred core:** zero new hook surface, no plugin
recompile beyond the new tool, falls back gracefully (if the bot
forgets, Layer A2 catches it), and aligns with how POD_CONDUCT and
DeferTool already shape behavior.

### Layer A2 — Post-turn audit (defensive)

The existing TurnObserver already gets `agent_end` callbacks per turn and
already has conversation access. Add a small audit pass that:

1. Scans the turn's tool-use entries for `Write`, `Edit`, `Bash` operations
   touching paths that match the §"Trigger conditions" predicate.
2. Looks up whether each path is already in a manifest's `files[]` for this
   bot (read once, cached for the session).
3. For un-manifested writes, choose one of three actions based on a
   simple rule:

   | Situation | Action |
   |---|---|
   | Bot already called `record_application` this turn covering this file | nothing — A1 handled it |
   | Bot wrote to a path under `~/workspace/{ops,bin,tools,projects}/` and didn't call A1 | append a one-line `systemAppend` injection on the *next* turn: *"You wrote `<path>` last turn but didn't call `record_application`. If that's app-shaped, please call it now."* |
   | Bot wrote to a clearly app-shaped path (cron + script) and ignored a prior nudge | auto-draft a manifest at `confidence = 0.4`, `source = "bot_created"`, `source_detail = "reflex auto-drafted (bot did not self-report)"`, and emit a Signal so the operator sees it |

This layer's value is a closed loop: the bot's behavior gets corrected
within a session, not days later when the scanner runs. The "next-turn
nudge" works because the same agent will likely still be in the
conversation — and if it isn't, no harm done.

### Layer B — Periodic scan as safety net

Keep the existing scanner. Two changes:

1. When the scanner finds an artifact with no matching manifest, instead of
   immediately writing a `discovered` manifest, **first emit a Signal** in
   the new Signal store (see [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md))
   with producer `manifest_reflex_scanner` and a signature like
   `unmanifested:{bot_id}:{path_hash}`. The Signal includes the proposed
   manifest dict as `payload`.
2. The scanner still writes a draft manifest (so the system continues
   working), but with `confidence < 0.5` and a `motivating_signals[<sig>]`
   pointer so the operator can find the audit trail.

Rationale: every scanner-caught artifact is, by definition, evidence that
Layer A failed. We want that visibility. The Signal store already has the
right shape (state machine, dedup, archival).

## Manifest fields — bot vs. forge responsibility

The bot, via `record_application`, must populate the **intent** fields
(things only it knows from context):

| Field | Bot fills | Forge backfills |
|---|---|---|
| `id`, `name`, `bot_id` | ✅ | — |
| `purpose` (description) | ✅ | — |
| `source`, `source_detail` | tool sets `bot_created` automatically; bot supplies `source_detail` text | — |
| `files[]` | ✅ paths | forge: hashes, sizes, layer classification |
| `crons[]` | ✅ schedule + script | forge: cron `id`, last-run telemetry |
| `inputs[]`, `outputs[]` | ✅ rough strings | — |
| `confidence` | tool defaults to 1.0 for A1 path | A2/B reduce based on path |
| `status` | defaults to `active` | scheduler/scanner adjust to `dormant`/`paused` |
| `pkg_id`, `pkg_version`, `gallery_version` | — | forge sets only if app gets promoted to gallery |
| `test_command`, `test_cases[]` | optional (bot may suggest) | forge backfills via testgen pass |
| `improvement_history[]` | — | forge appends every time the app changes |
| `install_job` | — | only set when forge actually runs a job |

**Key principle:** the reflex path **does not create a `ForgeJob`**. The
bot has *already done* the work — there's nothing for the forge engine to
"run." The job-shaped lifecycle (queued → running → awaiting_approval →
…) is reserved for paths where forge is the actor (gallery, evo wizard,
RSI BuildApp). The reflex just records what happened.

This matches what the scanner does today — `MANIFEST_SOURCE_DISCOVERED`
manifests have no `install_job`. We're adding a sibling source,
`bot_created`, with the same property.

## Interaction with existing forge entry points

| Entry point | Creates ForgeJob? | Creates Manifest? | Source value |
|---|---|---|---|
| Gallery install | ✅ | ✅ | `gallery_installed` |
| Evo wizard | ✅ | ✅ | `user_created` |
| RSI BuildApp | ✅ (via [build_app.py](../packages/analyzer/arbiter/appliers/build_app.py)) | ✅ | `rsi_proposed` |
| Spec wizard | ✅ | ✅ | `user_created` |
| **Reflex (A1, A2)** | ❌ | ✅ | `bot_created` |
| Scanner (B) | ❌ | ✅ | `discovered` |

## Updates to existing apps

When the bot edits a file already in some manifest's `files[]`, the
reflex path should **not** create a new manifest. Instead:

- A1 path: bot calls `record_application(app_id="protein-tracker",
  files=[...], update=True)` — the tool merges into the existing record,
  appends a freeform entry to `improvement_history`.
- A2 path: detection short-circuits when the file is already in a
  manifest, no nudge fires.

The trickier case is *adding a new file to an existing app* — the bot
might not know which manifest the file "belongs to." For v1, treat this
as a new manifest unless the bot explicitly passes `app_id` to
`record_application`. The scanner can re-cluster later.

## Privacy & cost

- The `record_application` tool runs inside the bot's own session, so
  per the user-profile precedent (no centralized inference), **no data
  leaves the bot's process**. Manifests land in `{shared_dir}` which the
  bot already has write ACL to.
- Tool-call cost: one extra LLM call per app-shaped turn. Cheap. The
  tool's schema description is short; the bot doesn't need to consider
  it on most turns.
- The POD_CONDUCT addition is a few lines; no observable cost impact.

## Failure modes

| Failure | Mitigation |
|---|---|
| Bot ignores POD_CONDUCT instruction | A2 nudges; B catches eventually |
| Bot calls `record_application` with bogus `files[]` (e.g. files that don't exist) | tool validates path existence and bot ownership; rejects with explanation |
| Bot creates a manifest for something that isn't really app-shaped | manifest goes in with `confidence = 1.0` from A1 but operator can dismiss; not an error per se, just noise |
| Bot creates duplicate manifests across turns | `app_id` collision: tool detects and switches to `update=True` semantics |
| Two bots create the same `app_id` | not possible — manifests are per-bot |
| Reflex fires during gallery install (double-record) | gallery flow already wrote the manifest; tool detects existing manifest with non-`bot_created` source and short-circuits to a no-op + log |

## Defense-in-depth ordering (recap)

1. **POD_CONDUCT instruction** — the bot is told.
2. **`record_application` tool** — the bot is given an easy way to comply.
3. **Post-turn audit** (A2) — the bot is reminded next turn if it forgot.
4. **Auto-draft after second miss** (A2) — the system records something
   even if the bot never complies.
5. **Periodic scanner** (B) — daily sweep with Signal emission for any
   gap that survived 1–4.

## Resolutions (2026-05-09)

Each open question above was reviewed with Pod-Admin; the answers below
shape what's in scope for v1 and what becomes follow-up work.

1. **A2 auto-draft threshold** — keep the "auto-draft on second miss"
   design as the default. Refine in production if it produces too
   much noise.

2. **`bot_created` ↔ RSI** — a `bot_created` manifest is **two**
   pieces of evidence the system should learn from:

   a. **Failure to anticipate.** The bot was reactive, not proactive.
      In a perfect world the bot would ask itself *"why didn't I think
      of this earlier?"* — looking at the user's recent transcripts
      for behavioral signals that, in hindsight, should have led the
      bot to recommend the app first.
   b. **Pattern to model.** This is a shape of work the user wants
      from this bot. Future generators should bias toward proposing
      similar things proactively when comparable signals re-appear.

   Concretely: the runner emits a Signal of type `bot_created_app`
   when a fresh manifest lands. A future generator (out of scope here)
   reads those signals plus recent transcripts and proposes either a
   SOUL/AGENTS update ("be more proactive about X") or a gallery
   suggestion. The generator is its own work item; the Signal is the
   seam.

3. **Tool capability tier** — `manage` and `full`, as initially
   specced. `monitor`-tier bots remain read-only (no manifest
   creation, since `monitor` is the audit-only tier).

4. **Retroactive reconciliation** — yes, the system should
   periodically check for activity that wasn't aligned with a
   manifest. This is **Layer B in the original architecture** — the
   periodic scanner. Bumping its priority: build the
   scanner+Signal-emission integration as the next PR, not a
   later-someday follow-up.

5. **App vs. one-off file (exclusions)** — no exclusions by file type
   or "intent." Even a single notes file is an app. The reflex fires
   for anything that persists past the current turn. The proliferation
   of small notes-style apps is itself a useful signal: RSI will look
   for clusters of similar small apps and propose consolidation into
   a single notes-taking app.

   The §"Trigger conditions" section has been rewritten to reflect
   this — the "user intent" gate is gone.

6. **Reflex-drafted vs. discovered visibility** — useful for Evolve
   developers debugging the manifest-spotting code, mostly invisible to
   sysadmin operators. The data is already there (`source` field on
   each manifest); the admin UI doesn't need a special chip. Dev
   queries can filter on `source = bot_created` whenever needed.

7. **Tests for `bot_created` apps** — the forge process owns testgen
   review. Forge can:
   - Add tests when the manifest needs them and none was provided.
   - Set `test_exemption_reason` when tests would be unnecessary
     (trivial apps like single-note trackers).

   Implication: `bot_created` manifests should flow into the same
   forge-review queue as `discovered` manifests. The existing forge
   infrastructure (`validate_test_gate`, `forge_engine`) operates on
   manifests by source-agnostic shape, so this should mostly be a
   wiring confirmation rather than new code. Track as follow-up.

## App posture review (PR4 — inventory layer)

Per Pod-Admin's 2026-05-09 design review, the two separate generators
proposed earlier (`why-didn't-I-anticipate` + `cluster-consolidation`)
collapse into a **single weekly reflection** that produces a per-bot
*guidance document* — and the bot reads its own posture document at
session start. That changes the architecture from "more telemetry"
into "closed-loop self-tuning."

PR4 ships **only the inventory layer** of that reflection. It runs
weekly, gathers data, renders a structured markdown snapshot per bot,
and injects it into systemAppend. No LLM in PR4 — just facts. PR5
will add the LLM reflection section ("answer these questions:
clusters? splits? orphan dispositions? missed signals? forward
guidance?") on top of the same document.

### Architecture (PR4)

```
inputs (no LLM):
  • bot_created_app Signals (last 7 days)         [from PR1 runner]
  • unmanifested_app Signals (last 7 days)        [from PR2 scanner]
  • Manifests in {shared_dir}/applications/<bot>/  (mtime-windowed)
  • Workspace orphan walk under /Users/<bot>/.openclaw/workspace/
                          │
                          ▼
              app_posture_review.py
              (weekly Sunday 04:30, run as evolve)
                          │
                          ▼
       {shared_dir}/app_posture/<bot>.md           (canonical, overwritten)
       {shared_dir}/app_posture/<bot>/log/<YYYY-MM-DD>.md  (audit log)
                          │
                          ▼
              session_surface.py
              loads doc → systemAppend on next session_start
                          │
                          ▼
              Bot reads its own posture as context for the session
```

### What gets gathered

- **Manifests changed this week** — flagged by `is_recent` based on
  `updated_at`, so the bot sees which apps moved and which were quiet.
- **Self-recorded apps** — `bot_created_app` Signals from the manifest
  reflex runner, scoped to the last 7 days. Tells the bot: "you
  proactively recorded these N apps this week."
- **Scanner-discovered apps** — `unmanifested_app` Signals from the
  periodic scanner. Tells the bot: "the scanner found these N apps
  you didn't self-record — Layer A1 missed them."
- **Workspace file orphans** — files in the bot's workspace not in any
  manifest's `files[]`. Capped at 50 entries with a truncation note;
  bots with thousands of orphans need scanner-driven cleanup, not a
  giant posture doc.
- **Cron orphans** — *deferred*. The evolve user has no `crontab -u`
  sudo grant, and the script behind a cron entry usually shows up as
  a workspace orphan anyway, so we skip cron-orphan detection in PR4
  rather than introduce a new sudoers grant. The doc records this as
  a caveat.

### Why a document, not Signals

Signals are good for events. They're terrible for *behavioral
guidance*. The bot needs a place to read prose ("you have a
habits-tracker — when the user mentions habits again, propose
update vs new"). The existing bot-guide infrastructure in
`session_surface.py` is the right vehicle; we add a sibling
`app_posture/<bot>.md` so auto-generated content stays distinct
from operator-authored bot guides.

### Why one LLM call (in PR5), not five

Cluster analysis, split analysis, orphan disposition, missed-signal
analysis, and forward guidance are all the same reasoning task.
Asking five separate LLM calls would force the model to context-switch
and miss connections (e.g., "this orphan file probably belongs in the
app you're recommending I split"). One rich prompt, one synthesis.

### PR4 → PR5 boundary

The PR4 doc has stable section headings (`## This week`, `## Orphan
files`, `## Inventory summary`, etc.) so the PR5 LLM layer can append
a `## Reflection` section without re-parsing the existing markdown.

## App posture LLM reflection (PR5)

PR5 adds the synthesis layer on top of PR4's inventory doc. **One LLM
call per bot, per week**, with the inventory rendered into the prompt.
The model answers five synthesis questions in markdown that gets
appended to the same `app_posture/<bot>.md` document.

### The five questions

```
## Reflection
### Clusters         — are any apps actually parts of one larger app?
### Splits           — are any apps doing too many distinct things?
### Orphan dispositions  — for each orphan: fold-into / new manifest / stale?
### Missed signals   — for each app self-recorded this week, what behavioral
                       signal could have been caught earlier?
### Forward guidance for next week — 2-4 short concrete rules for the
                                     bot to act on.
```

### Why one call, not five

Cluster analysis, split analysis, orphan disposition, missed-signal
analysis, and forward guidance are all the same reasoning task — they
require global context to answer well. Asking five separate LLM calls
would force the model to context-switch and miss connections (e.g.,
"this orphan file probably belongs in the app you're recommending I
split"). One rich prompt, one synthesis.

### Per-bot LLM credentials

The reflection runs against each bot's own Anthropic API key (read from
`~/.openclaw/agents/main/agent/auth-profiles.json` via the `evolve`
ACL grant). No centralized inference — privacy by architecture, per
the existing pod rule. tier3 model by default (cost-conscious; this
runs once a week per bot).

### Validation

The prompt instructs the LLM to reference data only as it appears in
the inventory and to wrap app_ids and file paths in backticks. After
the response lands, `validate_reflection` walks every backticked token
and flags app_ids / file paths that don't exist in the inventory.
**Hallucinated references are NOT removed** — the reflection text is
appended as-is, and a `### Validation notes` sub-section lists the
unknown references. Operators / future generators see the full
reasoning plus the warnings; the reflection isn't rejected wholesale
over a stray name.

### Gating

Default-OFF in PR5. Operators opt in by setting:

```json
{
  "app_posture": {
    "reflect_enabled": true
  }
}
```

in network.json. This lets the inventory layer (PR4) soak in
production for a few cycles before we start spending LLM calls. The
CLI also supports `--reflect` / `--no-reflect` to override the
network flag for a single run, plus `--reflect-dry-run` which appends
a synthetic placeholder reflection (no LLM call) — useful for testing
the append plumbing without spending tokens.

### Failure-soft throughout

Missing API key, HTTP error, empty response, validation mismatch — each
degrades to "no reflection appended this week." The PR4 inventory doc
is always written first, so a failed reflection still leaves the bot
with a useful posture document.

### Closing the loop

The bot reads its own reflection at next session_start via
`session_surface.py` (already wired in PR4). The same data the bot
generates next week (Signals, manifests, transcripts) becomes input
to the next reflection. **No separate generator** — the document
itself is the behavioral nudge, and behavior produces signals, and
signals shape next week's reflection.

### Out of scope for PR5

- **Proposal emission.** When the reflection identifies a structural
  change (merge two apps, split one, delete a stale orphan), it could
  emit a Proposal to the arbiter pipeline. PR5 produces narrative
  guidance only; structural changes happen through operator review of
  the doc. Adding Proposal emission is a follow-up.
- **Per-bot reflect override.** PR5 has pod-wide gating; bot-specific
  overrides come if needed.

## Transcript-aware reflection (PR6)

PR5 reflected on inventory metadata alone — for each `bot_created_app`
Signal, the LLM only saw the app_id, purpose, and files. The Missed
Signals question ("why didn't I anticipate this?") had no actual user
messages to ground in. PR6 adds that grounding.

### Source

The bot already has a per-bot rolling buffer of recent user turns at
`{shared_dir}/metrics/{bot_id}/recent-transcripts.json`, written by
`RecentTranscriptCapture` for security_warden's credential-detection
job. Privacy invariants (locked 2026-05-05, reused as-is here):

- **User text only** — assistant replies are not captured.
- **200 turns / 48h** retention, raw at capture.
- **Default-on opt-out** via `network.json` → `bots[botId].securityScanning`.
- **Owned by evolve user**; no cross-bot leakage.

For each `bot_created_app` Signal, PR6 looks up user turns from that
session that occurred BEFORE the Signal's `first_observed_at`
(i.e., before the moment the bot called `record_application`). Those
are the load-bearing messages for "why didn't I anticipate."

### Embedding into the prompt

The reflection prompt grows a new `[TRANSCRIPT EXCERPTS]` block,
inserted between `[INVENTORY]` and the synthesis questions. One
sub-section per bot_created_app Signal, each containing the user's
preceding turns (capped). The Missed Signals question is updated to
direct the LLM to reference the transcript excerpts when present and
to gracefully say "(no transcript context)" when not.

### Bounded budget

To keep the prompt size predictable:

- **Per turn**: max `TRANSCRIPT_TURN_CHAR_CAP = 400` chars (long user
  messages are truncated with an ellipsis).
- **Per signal**: max 8 turns OR 1500 chars total (whichever caps
  first). When over budget, drops oldest first — the conversation
  immediately before the bot acted is more informative than the
  session opener.
- **Per prompt total**: `TRANSCRIPT_TOTAL_CHARS = 6000`. A signal that
  would push past this cap is omitted (with a note in the prompt
  explaining how many were skipped). Better to under-include than
  silently truncate mid-block.

### Failure-soft

Same posture as PR5: missing buffer file, malformed JSON, no entries
matching the session_id, or `securityScanning: false` on the bot — each
degrades to "no transcript context for that signal" and the LLM
answers Missed Signals from inventory metadata alone. The prompt's
question text explicitly handles both cases.

### Reuse vs. new capture

PR6 deliberately reuses the existing `recent-transcripts.json` buffer
rather than creating a new capture path. Three reasons:

1. **Already captured, already privacy-reviewed.** Adding a parallel
   buffer would duplicate the privacy surface area for no signal gain.
2. **Same retention boundary.** 48h matches the weekly cadence of
   reflection — older weeks would have already aged out anyway.
3. **Same opt-out switch.** Operators flipping `securityScanning:
   false` correctly disables both security_warden's read AND the
   reflection's read of that bot's transcripts.

If a future use case wanted longer-window transcripts (e.g., monthly
reflection), that's a separate capture decision — not a PR6 concern.

## Structural-proposal emission (PR7)

PR5 + PR6 produce *narrative* reflection — operators read the doc and
act manually. PR7 adds a parallel *actionable* output: when the LLM has
high-confidence structural recommendations, file them as Proposals to
the existing arbiter pipeline so they show up in the admin UI's
proposal review surface.

### What gets emitted

The reflection prompt now ends with a fenced ```yaml proposals:``` block
that the LLM is instructed to fill in. Four kinds:

| `kind` | Fields | Meaning |
|---|---|---|
| `merge_apps` | `apps: [a, b, ...]` | These N apps should consolidate into one |
| `split_app` | `app: x`, `suggested_parts: [...]` | This one app should split |
| `delete_orphan` | `path: x` | Stale workspace file; propose deletion |
| `fold_orphan` | `path: x`, `into_app: y` | Orphan file should join an existing app |

Each entry also carries a `confidence` (0.0–1.0, LLM self-report) and a
`rationale` (free text). The runner drops anything below 0.6 confidence;
the prompt instructs the LLM to skip rather than file low-confidence
items.

### Why Investigation, not specific Action variants

The existing v2 schema has Action variants (`DeprecateApp`, `BuildApp`,
`ManifestUpdate`, etc.) that come with appliers — code that executes
the change automatically when an operator approves. PR7 deliberately
uses `Investigation` for ALL reflection-driven proposals because:

1. **No applier yet for merge/split/delete-orphan/fold-orphan.**
   Each of those would need its own non-trivial applier (manifest
   surgery, file system operations, cron updates). Out of scope here.
2. **`Investigation` is the right semantic match** — "the LLM has a
   structural suggestion the operator should review." The operator
   reads the proposal in the admin UI and acts manually.
3. **Future-proof.** When merge/split/delete appliers do land,
   PR7's Investigation history remains readable; new generators can
   start emitting specific variants.

The Investigation's `context` field renders the structured payload as
markdown so an operator viewing the proposal sees rationale + payload
+ confidence without opening the posture doc separately.

### Inventory grounding

Each candidate is validated against the posture's known app_ids and
file paths *before* the proposal is built. Hallucinated references
(LLM proposes merging `habits` with `frobinator` when frobinator
doesn't exist) result in the candidate being dropped — never a filed
proposal on imaginary apps.

### Idempotent re-runs

Each proposal's id is derived from a hash of `(bot_id, kind, sorted
refs)`. Two consecutive weekly runs that produce the same suggestion
produce the same id. The arbiter's `find_proposal` lookup catches the
duplicate before the second write — and a fingerprint-based
`find_open_duplicate` is the belt-to-the-suspenders.

The fingerprint matching depends on `trigger_observations` carrying a
candidate-specific marker. Without it, two `Investigation` proposals
from the same bot collide on fingerprint regardless of payload (the v2
fingerprint inputs include `action.kind` + `target_surface`, and
Investigation has no target_surface). Fixed by prepending
`f"app_posture_reflection:candidate:{proposal_id}"` to each proposal's
trigger list.

### Gating

Two-flag model (independent on/off):

```json
{
  "app_posture": {
    "reflect_enabled": true,
    "emit_proposals_enabled": true
  }
}
```

`reflect_enabled` (PR5) gates the LLM call entirely. `emit_proposals_enabled`
(PR7) gates the Proposal-write step. Operators can run reflection without
proposal emission while soaking the YAML output — when emission is off but
the LLM produced candidates, the cycle log surfaces "would_file: N" so
operators can see what would have been filed before flipping the switch.

CLI flags `--emit-proposals` / `--no-emit-proposals` override the
network flag for one-off runs (useful in `--reflect-dry-run` testing).

### Failure-soft

Same posture as PR5/PR6: PyYAML missing, malformed YAML block, all
candidates below threshold, arbiter package unavailable — each
degrades to "no proposals filed this cycle" with a stderr log. The
narrative reflection still appends to the doc.

## fold_orphan applier (PR8)

PR7 filed every reflection-driven proposal as `Investigation` — surface
context to the operator, no automatic apply. PR8 lands the first
specific-action applier so one of the four kinds (`fold_orphan`)
becomes operator-approve-and-it-executes.

### Why fold_orphan first

Of the four kinds, fold_orphan has the simplest semantics: append a
file path to an existing app's `files[]`. One manifest mutation; no
file system operations beyond the manifest itself; no cross-manifest
reasoning. Compared to:

- `merge_apps`: touches N manifests, reconciles file lists, may need
  forge re-builds.
- `split_app`: needs LLM-driven boundary decisions (which files to
  which side).
- `delete_orphan`: file system mutation; data loss if wrong.

Starting with the safest, most surgical case lets us validate the
applier-pipeline plumbing before tackling the harder kinds.

### Implementation

The existing `ManifestUpdate` Action variant already has a
`set_fields` operation (used by `test_gate_backfill`). PR8 adds a
sibling operation `add_files` with semantics:

1. Read the manifest at apply time (NOT proposal-creation time — a
   proposal sitting in `pending/` for days won't clobber concurrent
   file additions made by other paths like the manifest reflex
   runner or scanner).
2. For each path in `fields["files"]`, append to `data["files"]` if
   not already present (deduped against both string entries and v5+
   dict entries).
3. Atomic write via temp-file + rename. Reversible — the existing
   `capture_snapshot` / `revert` plumbing on the ManifestUpdate
   applier handles add_files unchanged.

The `RiskTag.reversibility` flips from `manual` (Investigation) to
`auto` (ManifestUpdate) for fold_orphan proposals — meaningful
operationally because the arbiter's safe-apply allowlist routes auto-
reversible changes differently.

### Why not change PR7 wholesale to specific-action variants

The other three kinds (merge, split, delete) need their own appliers
that do non-trivial work. Building all four at once is speculative —
we don't yet have production data on which kinds the LLM proposes
most. PR8 lands one applier as a forcing function for the design
pattern; subsequent PRs add the remaining three as needed.

### admin_surface_summary carries rationale for ManifestUpdate proposals

`Investigation.context` rendered the rationale into the markdown body
operators saw when opening a proposal. `ManifestUpdate` has no body —
the action is a structured field-list. The emitter folds the
rationale into `admin_surface_summary` instead so operators see it on
the alerts UI tile without opening the proposal detail.

## RetireOrphan applier (PR9)

Architectural surprise discovered while building this PR: **evolve has
no delete grant on bot workspaces**. The `set_evolve_read_acl` ACL
gives evolve READ access to `/Users/<bot>/.openclaw/`, but no write
or delete. A naive `delete_orphan` applier that unlinks the file
would either fail with EPERM or require new infrastructure
(per-bot launchd helper running as the bot user, or a new sudoers
grant for a narrow delete script).

PR9's response: **don't delete; retire and exclude.** The applier:

1. Reads the orphan content from
   `/Users/<bot>/.openclaw/workspace/<path>` (evolve has read).
2. Copies content to
   `{shared_dir}/app_posture/<bot>/orphan_archive/<YYYY-MM-DD>-<basename>`
   (evolve owns shared_dir).
3. Appends the path to
   `{shared_dir}/app_posture/<bot>/orphan_exclusions.json` —
   future weekly posture reviews skip files in this list, so the
   LLM stops re-proposing the retirement every week.

The actual workspace file is **left in place**. The bot can clean
it up at its own pace via natural agent action; we don't need to
mediate that. From the operator's perspective the orphan is "gone"
in the sense that matters (out of the registry), and there's a
content snapshot for safety.

### Why "retire" not "delete"

Calling the action `RetireOrphan` (not `DeleteOrphan`) and the
problem text "retire orphan file" (not "delete orphan file") keeps
the user-facing language honest about what actually happens. The
LLM-side YAML kind stays `delete_orphan` (operator-facing intent);
the action variant + admin UI text reflect the safer semantic.

### Path-traversal refusal

The action's `path` field is workspace-relative. Before doing
anything, the applier resolves the full path and verifies it stays
inside the workspace. An LLM-generated `../../etc/passwd` is
refused with a clear error. The confidence + inventory-grounding
gates upstream already filter most of these, but defense-in-depth.

### Reversibility

The applier captures the prior exclusions list in its snapshot.
Revert restores it; the archive copy stays put (cheap to leave
around, useful for audit). Operators get the standard arbiter undo
affordance.

### Idempotent re-apply

If a path is already in exclusions (e.g. previous week's apply, or
the proposal got re-emitted), the applier returns ok=True with
`already_retired: True` rather than duplicating. Fingerprint dedup
handles most of this upstream; this is belt-to-the-suspenders.

## Motivating-Signal id linkage (PR10)

Every v2 Proposal carries a `motivating_signals: list[str]` field
linking it to the Signal ids that triggered it. PR7 left this empty
for app_posture proposals because `SignalSummary` (the posture's
data carrier for signals) only had the `signature` — not the
underlying `Signal.id` from the store. PR10 closes that gap with
a one-line change to the dataclass + a list comprehension at
emission time.

Now each structural proposal links to up to 8 of the
`bot_created_app` Signals in the posture's window. The denormalized
inverse on each Signal (`Signal.motivated_proposals[]`) is left
untouched for now; the alerts UI's "→ N proposals" affordance
typically queries the proposal side, and writing back to N Signals
per proposal would require a write loop that's worth a follow-up
PR if we want the inverse to be authoritative.

Defensive: the list comprehension filters out empty `id` strings
so older synthetic test fixtures (which constructed `SignalSummary`
without populating `id`) don't get a malformed proposal that the
arbiter rejects.

## Follow-up work (in priority order)

After PR10 lands:

1. **merge_apps applier** — manifest surgery across N manifests. Pick
   a target app (the LLM should specify), append other apps' files +
   crons + capability_tags into it, mark the others deprecated via
   `DeprecateApp` (separate proposal? batched?). Most complex of
   the four.

2. **split_app applier** — needs LLM-driven file partitioning.
   Probably comes last because the LLM has to be more specific in
   its YAML output (which files go to which new app), and the
   forge probably needs to re-build the resulting apps.

3. **Layer A2 — TurnObserver post-turn audit.** The defensive
   "next-turn nudge / second-miss auto-draft" pass. Build only
   after PR5–PR10 produce a few weeks of data showing where in-turn
   intervention would help most.

4. **Inverse Signal-side linkage.** PR10 populates
   `Proposal.motivating_signals[]` but doesn't write the inverse
   `Signal.motivated_proposals[]`. The alerts UI's "→ N proposals"
   affordance can query either side; if we want the Signal-side to
   be authoritative, a small follow-up calls
   `signals.store.attach_proposal()` for each linked Signal at
   emission time.

5. **Workspace-side delete helper** *(if and when it's worth it)* —
   a per-bot launchd job running as the bot user that periodically
   reads its own `orphan_exclusions.json` and unlinks files in it
   that haven't been touched since the retirement timestamp. Closes
   the "the file is still there" caveat. Out of scope for PR9
   because retire-and-exclude is sufficient for the registry-honesty
   goal; physical removal is a hygiene nice-to-have.

6. ~~**Motivating-Signal id linkage**~~ — done in PR10.

7. ~~**delete_orphan applier**~~ — done in PR9 as RetireOrphan.

8. ~~**fold_orphan applier**~~ — done in PR8.

9. ~~**Proposal emission from reflection**~~ — done in PR7.

10. ~~**Transcript reading**~~ — done in PR6.

11. ~~**Layer B — Scanner+Signal integration**~~ — done in PR2.

12. ~~**"Why didn't I think of that?" generator**~~ — collapsed into
    PR5–PR7 reflection; not a separate generator.

12. ~~**App-cluster consolidation generator**~~ — collapsed into
    PR5–PR7 reflection; not a separate generator.

9. ~~**Forge testgen wiring confirmation**~~ — done in PR3.

## Implementation sketch (only if approved)

Roughly four concrete changes, none large:

1. New tool: `packages/plugin/src/tools/RecordApplicationTool.ts`,
   wired into [packages/plugin/src/index.ts](../packages/plugin/src/index.ts)
   under the `manage`/`full` capability tier. Calls a small Python
   helper via the plugin's existing subprocess pattern (see DeferTool
   for the precedent), which in turn calls
   [`evolve_admin.applications.manifest.save_manifest`](../packages/admin/evolve_admin/applications/manifest.py).

2. POD_CONDUCT addition: a new `<!-- manifest-reflex-start/end -->`
   block in `/Users/Shared/evolve/POD_CONDUCT.md`, extracted by an
   updated [session_surface.py](../packages/analyzer/session_surface.py).

3. TurnObserver audit: a new method on
   `packages/plugin/src/observer/TurnObserver.ts` invoked in `agent_end`,
   reading the turn's tool-use list, comparing against a per-session
   manifest cache, and either appending a `systemAppend` nudge for the
   next turn or kicking off the auto-draft path.

4. Scanner update: change
   [scanner.py](../packages/admin/evolve_admin/applications/scanner.py)
   so that, before writing each newly discovered manifest, it calls
   `signals.store.observe(...)` with a stable signature.

Open question 4 ("retroactive reconciliation") would add a fifth piece;
the others are independently shippable. Suggest landing in the order:
POD_CONDUCT (§A1 prompt) → record_application tool (§A1 tool) →
Scanner+Signal integration (§B) → TurnObserver audit (§A2). The last is
both the largest change and the one most easily replaced if we find
the first three are sufficient.
