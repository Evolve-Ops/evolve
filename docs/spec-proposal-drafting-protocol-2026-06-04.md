# spec: proposal-drafting protocol — 2026-06-04

Status: draft.

## Background

On 2026-06-04 the operator opened a `cache_ttl_tuner` proposal and
sent the team this paste, verbatim:

> Just step back for a moment and look at all of the words in this
> proposal and all of the fields and all of the gibberish.

The proposal in question:

```
TITLE: Set team-bot-a cacheRetention=long — 38% invalidated over 7d
TAGS:  hygiene · efficiency · pod_operator · UpdateAgentDefaults
PROBLEM: team-bot-a prompt cache: 38% of cached turns invalidated (37/97
  over 7d). Flipping Anthropic ``cacheRetention`` from ``short``
  (5min) to ``long`` (1h) keeps the cache warm across human-paced
  gaps.
ROOT-CAUSE: cache_retention_too_short_for_cadence · high · 85%
ACTION: UpdateAgentDefaults
  bot_id: team-bot-a
  fields: { "agents.defaults.models.*.params.cacheRetention": "long" }
CLAIM: metric=cache_invalidation_ratio · direction=down · magnitude=0
       · window_days=7 · fallback=revert
RISK:  blast_radius=bot · reversibility=auto · touches=model_config
MOTIVATING SIGNALS: 345fbd1c-aa38-4672-b90b-dad6a26335d0
HISTORY: 22h ago · draft → pending · arbiter · ingest accepted
```

What the operator sees:
1. **A title that describes a database row, not a fix.**
2. **A "PROBLEM" that's actually the fix re-worded.**
3. **Six labeled blocks of metadata that read like form fields.**
4. **No explanation of WHY this matters, the WHAT to expect, or the
   TRADE-OFFS.**
5. **No prominent button to act on it.**
6. **A UUID under "motivating signals" that's meaningless to a human.**

The operator drafted their preferred shape:

```
TITLE: Adjust Team Bot A prompt caching to "long"

Summary: Your bot caching is inefficient and can be improved by
changing Anthropic caching from "short" to "long".

Proposed Action: [Click here] to adjust the setting or visit
Cost Optimization/Team Bot A to adjust the setting manually.

Explanation: describe the purpose of caching and point out the
trade-offs of short vs. long. Explain what invalidation means and
why the 38% invalidation is a signal that caching may be too short.
Explain what the expected improvement is: the bot will have more and
better context of your conversations and be able to get more things
correct the first time. Down side: bigger cache can mean larger
more expense per turn (is this so??).

Details: here you can include all of the technical stuff, including
the field structure, etc. Risks, motivating signals, etc.
```

This spec adopts that shape and turns it into a protocol every
generator follows.

## Principle

> A proposal is a **document for a human decision**, not a database
> row. The reader is the operator: not a developer, not the LLM that
> emitted the proposal, not the auditor reading retros next year.
> Every word above the fold should help the operator decide to
> accept, dismiss, or learn more — in that order.
>
> The technical structure (action kind, claim metric, motivating
> signal UUIDs, risk taxonomy) stays available below the fold for
> operators who want it. Above the fold: prose.

## Applicability check (pre-emission)

> Before a generator emits a proposal, it must verify the proposal
> applies to the bot's current configuration. A proposal that can't
> actually be applied is worse than no proposal — the operator wastes
> attention on something they can't act on.

The cache_ttl_tuner example surfaces the rule: `cacheRetention` is an
Anthropic-specific parameter. If the bot's `agents.defaults.models`
is set to an OpenAI model, the proposal doesn't apply — switching to
"long" is a no-op there. The generator must read the bot's effective
model config, confirm the provider matches, and skip emission
otherwise. (Logging the skip in telemetry is fine — the operator
shouldn't see it.)

Common applicability checks generators must perform:

- **Provider check.** Read `agents.defaults.models.*.provider` (or
  the equivalent registry path). Only emit if the action's target
  config field is recognized by the provider in use. Anthropic-only
  knobs (`cacheRetention`, `extended_thinking`), OpenAI-only knobs
  (`reasoning_effort` for o1/o3), and Google-only knobs all need
  this gate.
- **Feature-flag check.** If the action requires a feature the bot
  has explicitly disabled, skip.
- **Already-applied check.** If the proposed target value matches
  the current value (operator already set it manually, or a previous
  apply landed), skip — don't emit a no-op proposal.
- **Permission check.** If applying the action requires permissions
  the operator hasn't granted, emit with `action_label` pointing at
  the permission flow rather than the apply button.

Generators document the checks they perform in their charter's
`purpose` block so future maintainers know what gates are in place.

## Required content shape

Every proposal renders in five sections, in this order:

```
┌──────────────────────────────────────────────────────────────┐
│ TITLE                                                        │
│ • Imperative phrase. Names the action + the affected thing.  │
│ • Plain English. No slugs, no model IDs, no field paths.     │
│ • ≤ 80 chars. Fits the queue list AND the modal header.      │
├──────────────────────────────────────────────────────────────┤
│ SUMMARY (2-3 sentences)                                      │
│ • What's wrong + what fixes it, in one breath.               │
│ • Reads cold without the title above.                        │
│ • Imperative voice OK ("your bot is…", "this fixes…").       │
├──────────────────────────────────────────────────────────────┤
│ PROPOSED ACTION                                              │
│ • Falls through these fallback tiers, picking the first      │
│   one the generator can populate:                            │
│                                                              │
│   1. Auto-apply button — when the action has an applier      │
│      (ConfigPatch, UpsertCronJob, etc.) AND the operator's   │
│      authority allows it. Label = verb of the action.        │
│                                                              │
│   2. UI manual path — "or open Cost Optimization → Team Bot A and   │
│      change … yourself." Always shown alongside (1) if the   │
│      UI exposes the relevant knob.                           │
│                                                              │
│   3. CLI command — when there's no UI path but a known       │
│      command does it. Shown as a copy-button code block:     │
│      `sudo evolve-admin set-agent-default team-bot-a               │
│       cacheRetention long`                                   │
│      The generator owns the command string; the spec         │
│      requires it be fully-vetted (no placeholders).          │
│                                                              │
│   4. Have-evo-fix-it — when there's no UI and no fixed CLI   │
│      but the bot/evo can do it. Routes through the Slice 3   │
│      dispatch flow (Have evo fix this / Send to {bot}). The  │
│      dispatch_message authored by the generator says what    │
│      to ask the bot to change.                               │
│                                                              │
│   5. Hand-written instruction — when nothing above applies:  │
│      a copy-paste-ready instruction the operator can give    │
│      to the bot in chat, or steps to do offline.             │
│                                                              │
│ • Always-present decline buttons (see §"Decline buttons"     │
│   below): Snooze and Dismiss.                                │
├──────────────────────────────────────────────────────────────┤
│ EXPLANATION (collapsible if > 6 lines)                       │
│ • Answers four questions in this order:                      │
│     1. What's the underlying concept?                        │
│     2. What did we observe + why does it suggest this is     │
│        a problem? (the **diagnosis** — name the root cause   │
│        when the generator has one)                           │
│     3. Why does the proposed change help?                    │
│     4. What could go wrong? (trade-offs, side effects)       │
│ • Defines terms inline the first time they appear.           │
│ • Cites the observation that motivated the proposal in       │
│   operator language ("38% of cached turns expired before     │
│   the bot's next reply — that's the signal we noticed").    │
│ • The diagnosis paragraph is what distinguishes a thoughtful │
│   proposal from a templated complaint. When the generator    │
│   can't confidently name a root cause, says so honestly:     │
│   "We're not yet sure why this is happening — possibilities  │
│   include X, Y, Z."                                          │
├──────────────────────────────────────────────────────────────┤
│ DETAILS (always collapsed by default)                        │
│ • All current proposal metadata: action kind + fields,       │
│   claim, risk tag, root-cause attribution, motivating signal │
│   UUIDs, history, provenance.                                │
│ • Operators debugging the engine open this. Daily users      │
│   never do.                                                  │
└──────────────────────────────────────────────────────────────┘
```

## Decline buttons

Every proposal — auto-applicable or not, dispatch-target or not —
ships with **Snooze** and **Dismiss** buttons in the action row.
Both are operator-facing affordances; the renderer never omits
them.

### Snooze

Delays the proposal. The proposal moves to `snoozed` status and
disappears from the queue until `snoozed_until` passes, at which
point the snooze-wake daemon transitions it back to `pending` for
re-evaluation. Snooze defaults to 1 week; the operator picks
shorter durations from a dropdown (1h / 1d / 1w / 1m).

When a snoozed proposal wakes up, the generator's idempotency
check runs again: if the underlying condition has resolved
(operator fixed it manually, cache invalidation rate dropped,
etc.), the proposal auto-resolves rather than re-surfacing.

### Dismiss

Suppresses the proposal **and prevents the same proposal from
re-surfacing**. This is harder than Snooze because we have to be
honest about what "the same proposal" means:

- **Signature-based dedup** is the load-bearing mechanism. Every
  generator emits a stable signature for each finding (e.g.
  `cache_ttl_tuner:team-bot-a:cacheRetention_too_short`). Dismiss writes
  the signature to a per-generator suppression list at
  `{shared_dir}/proposals/dismissed_signatures.jsonl`. On the next
  emission pass, the generator checks the list and skips matching
  signatures.

- **Suppression is not permanent by default.** Each entry carries
  a `dismissed_at` timestamp and a default 90-day TTL. After the
  TTL, the suppression lifts and the generator can re-emit if the
  condition still holds. The operator-facing rationale: "you
  said no in June; if the same thing is true in September, ask
  again." A "permanent" option in the dismiss flow takes the TTL
  to never-expire.

- **Per-bot scoping.** Most signatures are per-bot
  (`cache_ttl_tuner:team-bot-a:...`). Dismissing on Team Bot A doesn't suppress
  the equivalent finding on Bot-a unless the operator explicitly
  picks "Dismiss for all bots" — defensive default favors
  re-surfacing.

- **Generators that can't compute a stable signature** (rare:
  per-run findings where the signature would be the finding
  itself) emit `dismiss_scope: instance` so the dismiss only
  affects this specific proposal id, not future findings of the
  same shape. The dismiss button label clarifies: "Dismiss this
  one" rather than "Dismiss this kind."

The dismiss UI shows the operator what's being suppressed in
plain English: *"You won't see proposals about Team Bot A's prompt-cache
window being too short for the next 90 days."* So the operator
knows what they're agreeing to.

This mechanism deliberately matches the existing
`audit_poller`'s `manifest.audit_accepted` dedup (see PR #1316
+ memory `feedback_generators_consider_intent`). Generalizing
it to all generators is a Phase A.5 deliverable in the migration
plan below.

## Voice rules

These are non-negotiable. CI enforces them where it can (length
budgets, slug detection); the generator-author + a one-time human
review enforces the rest.

1. **Second person.** "Your bot's prompt cache" not "the team-bot-a
   bot's prompt cache."
2. **No slugs.** Field paths (`agents.defaults.models.*.params.
   cacheRetention`), root-cause keys (`cache_retention_too_short_
   for_cadence`), action class names (`UpdateAgentDefaults`), and
   signal ids never appear in Title, Summary, Action, or
   Explanation. They live in Details only.
3. **Lead with the why.** The first sentence of Summary names the
   problem from the operator's perspective ("your bot is wasting
   work re-reading the same context"), not the metric that
   triggered it ("invalidated_ratio=0.38").
4. **Specifics over abstractions.** "38 out of 97 turns over the
   last week" beats "elevated invalidation ratio."
5. **Define on first use.** "Prompt caching (Anthropic's feature
   that lets the model skip re-reading the system prompt on
   follow-up turns)…" — written so a Plex-test operator who hasn't
   read Anthropic's docs can still follow.
6. **Name trade-offs explicitly.** Even when the proposal is
   obviously good, the Explanation closes with "what could go
   wrong" — 1-2 sentences. This builds trust over time; it makes
   the engine feel like a colleague who tells you the catch, not
   a tool that always claims wins.
7. **Speak in the affirmative.** "Switching to `long` keeps the
   cache warm…" not "Without switching to `long`, the cache
   wouldn't stay warm…"
8. **Length budgets** (CI-enforceable):
   - Title ≤ 80 chars
   - Summary ≤ 400 chars (~3 sentences)
   - Explanation ≤ 1500 chars (~6 short paragraphs)
   - Action button label ≤ 30 chars

## Worked example — cache_ttl_tuner

### Before (current emission)

```
TITLE: Set team-bot-a cacheRetention=long — 38% invalidated over 7d
PROBLEM: team-bot-a prompt cache: 38% of cached turns invalidated
  (37/97 over 7d). Flipping Anthropic ``cacheRetention`` from
  ``short`` (5min) to ``long`` (1h) keeps the cache warm across
  human-paced gaps.
[action / claim / risk / signals / history as metadata blocks]
```

### After (this protocol)

```
TITLE: Make Team Bot A's prompt cache last longer

SUMMARY: Team Bot A is paying to re-read the same system prompt 38% of
the time because its prompt-cache window (5 minutes) is shorter
than the gap between your messages. Switching to the longer (1
hour) window keeps the cache warm so the bot doesn't redo work
between turns.

PROPOSED ACTION:
[ Switch Team Bot A to long-window caching ]
Or open Cost Optimization → Team Bot A and change "Prompt cache window"
from "short" to "long" yourself.
[ Snooze 1w ]   [ Dismiss ]

EXPLANATION:
Anthropic's prompt-cache feature lets the model skip re-reading
the system prompt on follow-up turns. Anthropic charges less for
those cached reads, so a warm cache = lower cost per turn.

Diagnosis. The cache has a TTL ("time to live"). Team Bot A is set to
"short" (5 minutes), which is fine for back-to-back chatbot
turns but expires during human-paced conversations where you
reply minutes later. We watched Team Bot A over the last 7 days: 37 of
97 cached turns expired before your next reply, then got fully
re-read and re-charged. That 38% invalidation rate is the signal
that the TTL is mis-tuned for your usage rhythm. The root cause
we're naming: TTL shorter than the typical inter-turn gap.

Why "long" helps: Team Bot A holds the cache for an hour after each
turn. Most human-paced gaps (5–20 min) stay inside that window,
so the model can skip re-reading the prompt and use the cached
copy instead.

What could go wrong: the longer TTL means slightly higher cache-
storage cost per cached turn. In practice this is dominated by
the savings from cache hits — but if your usage shifts to lots
of short-burst conversations later, the math might flip and
you'd want to revert. We auto-revert if the invalidation rate
doesn't improve after 7 days. This also assumes Team Bot A is using
Anthropic models (we checked: yes, it is). The setting is a
no-op on OpenAI or Google models.

DETAILS (collapsed):
  Action kind:        UpdateAgentDefaults
  Target field:       agents.defaults.models.*.params.cacheRetention
  Before:             "short"
  After:              "long"
  Metric tracked:     cache_invalidation_ratio
  Window:             7 days
  Fallback:           revert
  Root-cause:         cache_retention_too_short_for_cadence (85%)
  Risk:               blast_radius=bot · reversibility=auto
  Motivating signal:  345fbd1c-aa38-4672-b90b-dad6a26335d0
  History:            22h ago — arbiter accepted ingest
```

The Title halves in length. The Summary tells the operator what's
wrong and what fixes it without name-checking a single field path.
The Action is a button. The Explanation teaches the concept,
shows the trade-off, and explains why we're confident. Details
are still there for anyone who wants them.

## Schema additions

Two new optional fields on `Proposal` carry the new content. The
existing `problem` and `admin_surface_summary` stay for back-compat
(generators we haven't migrated yet keep working); the renderer
prefers the new fields when present.

```python
@dataclass
class Proposal:
    # ... existing fields ...

    # ── Operator-facing content (2026-06-04 protocol) ──────────────
    # Spec: docs/spec-proposal-drafting-protocol-2026-06-04.md.
    #
    # Generators that haven't been migrated to the new shape leave
    # these None; the renderer falls back to admin_surface_summary +
    # problem.
    summary: str | None = None
    explanation: str | None = None
    # Action button label. None falls back to a generic
    # "Take this on" / "Apply this change" derived from action.kind.
    action_label: str | None = None
    # Optional pointer to the page where the operator can apply this
    # manually. Shown as a sentence after the action button.
    manual_path: str | None = None  # e.g., "Cost Optimization → Team Bot A"
    # Tier 3: fully-vetted CLI command. Rendered as a copy-button
    # code block. The generator owns the string — no placeholders.
    cli_command: str | None = None
    # Tier 5: hand-written, copy-paste-ready instruction the
    # operator can give to a bot or run themselves. Rendered as a
    # code block with a copy button.
    manual_instruction: str | None = None
    # Stable signature for Dismiss-based suppression. Defaults to
    # trigger_observations[0] when None; generators are encouraged
    # to set this explicitly so a future change to the observation
    # shape doesn't invalidate prior dismissals.
    dismiss_signature: str | None = None
    # Whether Dismiss should suppress (a) future findings with the
    # same signature ("kind") or (b) only this specific proposal
    # instance. Defaults to "kind"; generators that can't compute a
    # stable per-finding signature set "instance".
    dismiss_scope: Literal["kind", "instance"] = "kind"
```

These are operator-facing strings only. None of them gate the
applier or change the action's structural behavior — they're
pure UI content. The existing `problem`, `admin_surface_summary`,
`provenance`, etc. stay as the structural source of truth.

## Rendering protocol

The proposal-detail modal (`renderProposalCard` + the modal
opened by clicking a row) renders:

1. **Title** — `proposal.admin_surface_summary` (or the new
   `proposal.title` if we add one later — for now reuse the
   existing field, which is already humanized after PR #2 +
   Slice 2).
2. **Summary** — `proposal.summary` if present, else falls back
   to `proposal.problem`.
3. **Proposed Action** — the existing Act / Take-this-on button,
   relabeled per `proposal.action_label` if set. The sentence
   below uses `proposal.manual_path` if set, else a generic
   "or do this manually."
4. **Explanation** — `proposal.explanation` if present, rendered
   as markdown (paragraphs, no inline code unless the term is a
   product name). Collapsed behind a "Why?" toggle if > 6 lines.
5. **Details** — always collapsed. Behind a "Show technical
   details" toggle. Renders the current metadata blocks
   verbatim — action kind, claim, risk, root-cause attribution,
   motivating signals, history. Operators debugging the engine
   open this; daily users don't.

The new sections render only when the proposal has been migrated
to the new shape (i.e. `summary` is not None). Pre-migration
proposals render today's layout exactly as they do now. That's
the safety valve — generators can be migrated one at a time and
the queue never breaks.

## Generator-author conventions

A generator that emits proposals under this protocol does **four
extra things at emit time** beyond what it does today:

### 0. Run the applicability check first

Confirm the proposal applies to the bot's current configuration
(see §"Applicability check"). If it doesn't, log + skip. The
operator never sees no-op proposals.

### 1. Author the Summary

Write 2-3 sentences naming the problem from the operator's
perspective and the proposed fix. The Summary is the only text
guaranteed to be read; treat it as the headline below the
headline. Examples:

- **bloat_investigator**: "Your bot is paying for the same 43 KB
  workspace file to be loaded into every turn's context.
  Trimming it cuts each turn's cost without changing the bot's
  behavior."
- **cron_caps_filler**: "One of the security bot's cron jobs runs without
  a turn limit, so a runaway loop could spend unbounded money
  before anything notices. This adds the standard 20-turn /
  $1.00 cap."
- **plugin_curator**: "Atlas has no plugin allowlist set up
  yet, so any plugin in load.paths gets loaded automatically.
  Adopting the baseline allowlist gives you a single place to
  review what's running."

### 2. Author the Explanation

Three short paragraphs answering: (a) what's the concept, (b)
why does this help, (c) what could go wrong. Total ≤ 1500
chars. Write it like an editor would write a sidebar for a
news article — concrete, second-person, no jargon. The
worked example above is the reference voice.

### 3. Set action_label + manual_path (and fallback paths)

Walk the §"PROPOSED ACTION" fallback ladder and populate the
highest-tier path the generator can support:

- **Tier 1 (auto-apply)** — `action_label` is the button verb.
  "Apply this change", "Add the cron", "Switch to long-window
  caching". `manual_path` is the operator-facing path to the
  equivalent UI control ("Cost Optimization → Team Bot A", "Plugins
  → Atlas", "Maintenance → Cron Jobs"). Both shown.
- **Tier 2 (UI manual only)** — no auto-apply but the UI has a
  knob. `action_label` becomes "Open <page>"; the rendered
  button navigates to the page rather than applying directly.
  `manual_path` describes the steps.
- **Tier 3 (CLI command)** — no UI path but a known command does
  it. Populate `cli_command` (new optional field, see schema)
  with a fully-vetted, no-placeholder command string. The
  renderer shows it as a copy-button code block.
- **Tier 4 (dispatch to evo / bot)** — set `dispatch_target` per
  the Slice 3 spec. The renderer derives the action label
  ("Have evo fix this" / "Send to {bot}"); generators don't
  override.
- **Tier 5 (hand-written instruction)** — populate
  `manual_instruction` (new optional field) with a copy-paste-
  ready instruction. The renderer shows it as a code block
  with a copy button. Typically a chat message the operator
  pastes to the bot.

For Investigation proposals with `dispatch_target` set,
`action_label` is auto-derived ("Have evo fix this" / "Send to
{bot}") per the Slice 3 spec — generators don't override it.

### 4. Set the suppression signature

Author a stable signature for the finding (e.g.
`cache_ttl_tuner:team-bot-a:cacheRetention_too_short`). Used by Dismiss
to suppress re-emission. See §"Decline buttons" → Dismiss for
the contract. Defaults to the proposal's existing
`trigger_observations[0]` if not set — but a hand-authored
signature is preferred since it survives generator-side schema
changes that touch the observation shape.

## LLM-side authoring (optional helper)

For generators that already use an LLM call at emission time
(`bloat_investigator`, the upcoming `app_usage_advisor`), the
authoring of Summary + Explanation can fold into that call's
prompt — see `packages/analyzer/_proposal_authoring.py` (to be
added) for the shared prompt template + structured-output
schema.

For pure-Python generators (`cache_ttl_tuner`, `plugin_curator`,
`cron_caps_filler`), the Summary + Explanation are
hand-authored constants in the generator's code, parameterized
with the relevant data. The handful of templates per generator
is much smaller than the prose authoring burden suggests —
most generators emit one or two shapes of proposal.

## CI enforcement (where it's possible)

Two test files enforce the protocol where it's machine-checkable:

1. **`test_proposal_content_shape.py`** — for every generator
   that emits via a known shape, verify:
   - `summary` is set and ≤ 400 chars
   - `explanation` is set and ≤ 1500 chars
   - `action_label` is set or absent (per the dispatch_target
     auto-derivation rule)
   - No reserved slugs in `summary` / `explanation` (regex sweep
     for field paths, action class names, root-cause keys).
2. **`test_proposal_voice_rules.py`** — light-touch lint:
   - No `_` in Summary / Explanation (snake_case slugs leaked)
   - No backticks except around product names ("Anthropic's
     `claude-3.5-sonnet`" OK; "agents.defaults.models" not OK)
   - "Trade-offs" / "what could go wrong" / "downside" / "risk"
     appears somewhere in Explanation (gates the "name the
     trade-off" rule)

These tests run on a synthetic generator emission, not the
live store — so they exercise the generator's authoring code,
not whatever happens to be on disk.

## Migration plan

Phased per-generator rollout. **Don't rip-and-replace.** The
fallback rendering for pre-migration proposals means the
queue keeps working while we migrate one generator at a time.

### Phase A — schema + renderer (this PR or next)

- Add the four new optional fields to `Proposal`.
- Update the proposal-detail modal renderer to use them when
  present, fall back to today's layout when absent.
- Update the queue card renderer if needed (Summary may
  replace the current problem-line preview for migrated
  proposals).
- Ship empty — no generator migrated yet, but everything
  works.

### Phase A.5 — universal Snooze + Dismiss

- Snooze already works for every proposal status that allows
  the `pending → snoozed` transition (today's behavior).
  Confirm the modal renders the duration dropdown consistently
  per the §"Decline buttons" rules.
- **Dismiss with signature-based suppression** generalizes the
  existing `audit_poller` pattern. Adds:
  - A shared `dismissed_signatures.jsonl` store at
    `{shared_dir}/proposals/dismissed_signatures.jsonl`
  - A common helper `arbiter.dismissals.is_suppressed(sig)` that
    every generator calls in its emission pass
  - A `dismiss` action handler that writes the entry with TTL +
    bot scope
  - Operator-visible suppression list in Settings → Proposals →
    Suppressions so the operator can review + lift dismissals
- Audit the existing generators' signatures: confirm each emits
  a stable per-finding signature; if not, document the
  `dismiss_scope: instance` fallback in that generator's charter.

### Phase B — `cache_ttl_tuner` worked example

- Migrate `cache_ttl_tuner` to author Summary / Explanation /
  action_label / manual_path. This is the proposal the operator
  pasted; ship it first as the visible proof-of-concept.
- The "before / after" goes in the PR description.

### Phase C — sweep generators in pareto order

Migrate generators in the order of how often they appear on the
test pod queue:

1. `cache_ttl_tuner` — done in Phase B
2. `bloat_investigator` — high-traffic
3. `cost_spike` — high-traffic
4. `plugin_curator` — common hygiene
5. `cron_caps_filler` — common hygiene
6. `app_permission_review` — common hygiene
7. `primary_model_floor_advisor` — already partially migrated in PR #2
8. `audit_poller` (app_audit_tier3) — already partially migrated in Slice 2
9. The rest — long tail

Each generator migration is a small PR with: the generator
code change, a test in `test_proposal_content_shape.py`
covering its emission, and an end-to-end before/after in the
PR description.

### Phase D — drop the fallback

Once every generator emits the new shape and a deploy + observation
window confirms no surprises, remove the fallback rendering path.
This is months out and explicitly NOT in the initial scope.

## Non-goals

- **Auto-generating Summary / Explanation across the board via
  LLM.** Tempting but expensive (every emission would cost
  cents). Pure-Python generators hand-author the strings;
  LLM-using generators can fold authoring into their existing
  call. No new always-on LLM dependency.
- **Multiple "voice" variants.** No "casual mode" / "technical
  mode" toggle. One voice, written for the Plex-test operator.
  Power users see Details if they want depth.
- **Localization.** English-only. The voice rules assume
  English; revisit if/when we ship a non-English operator
  surface.
- **Changing the underlying action mechanics.** ConfigPatch,
  UpsertCronJob, Investigation, etc. all behave exactly as
  today — the protocol is presentation-layer.

## Open questions

1. **Title field — separate from `admin_surface_summary` or
   reuse it?** PR #2 humanized `admin_surface_summary` for
   several generators; the existing field is already the "short
   plain-English headline." Lean: reuse it (don't add a new
   `title`). Revisit if we find generators that want different
   text on the queue card vs the modal header.

2. **Markdown in Explanation — how much?** The example above
   uses paragraphs only. Bulleted lists OK. Code blocks NO
   (those belong in Details). Headings NO (Explanation is
   already a section header). Bold for emphasis OK, sparingly.
   Codify in `test_proposal_voice_rules.py`.

3. **What about Investigation proposals where the body IS the
   explanation?** Today `action=Investigation(context=body)`
   stuffs the technical context into the action body. Under the
   new protocol the Explanation supersedes that. Investigation
   proposals migrate by moving the explanatory part of `context`
   into `explanation` and leaving only the action-relevant
   structured data in `context`.

4. **LLM-authored Summary + Explanation — schema for the
   structured output?** Defer to the `_proposal_authoring.py`
   helper PR. Lean: a small Pydantic-ish schema with
   `summary`, `explanation`, `action_label`, `manual_path`,
   and a `confidence` score (skip emission if confidence <
   threshold).

## Related

- [docs/spec-recommendations-rework-2026-06-02.md](spec-recommendations-rework-2026-06-02.md)
  — the rework parent
- [docs/spec-take-this-on-evo-dispatch-2026-06-04.md](spec-take-this-on-evo-dispatch-2026-06-04.md)
  — Slice 3 dispatch; `action_label` interacts with
  `dispatch_target`
- `feedback_design_constraint_mildly_tech_capable` (memory) —
  the Plex-test audience the voice rules target
- `feedback_message_style_kit_like` (memory) — Team Bot A-style
  message conventions; this spec applies a similar shape to
  proposals
